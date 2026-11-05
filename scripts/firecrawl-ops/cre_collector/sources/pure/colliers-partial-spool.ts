/** Stream attempt-local evidence. Caller owns path/descriptor, hash and generation binding. */
import type { Readable } from "node:stream";
import { replayColliersPage } from "./colliers-partial-replay.js";
import { groupColliersMapLocations, parseColliersReceiptCards } from "./colliers-receipt.js";

const MAX_LINE_BYTES = 16 * 1024 * 1024;
type Identity = { mapProjectId: string; detailPv: string | null; detailUrl: string | null };
export type Candidate = Identity & { start: number; page: number; pageSize: number; offset: number; observedAt: string };

function supportedDetail(card: Identity): boolean {
  if (!card.mapProjectId || /[\x00-\x1f\x7f]/.test(card.mapProjectId) ||
      Buffer.from(card.mapProjectId, "utf8").toString("utf8") !== card.mapProjectId ||
      !card.detailUrl || !card.detailPv || card.detailPv.length > 512 || /[\x00-\x1f\x7f]/.test(card.detailPv)) return false;
  try {
    const url = new URL(card.detailUrl);
    return url.protocol === "https:" && url.hostname === "my.rcm1.com" && !url.port &&
      !url.username && !url.password && !url.hash &&
      ["/handler/landing.aspx", "/handler/modern.aspx"].includes(url.pathname.toLowerCase()) &&
      url.searchParams.getAll("pv").length === 1 && url.searchParams.get("pv") === card.detailPv;
  } catch { return false; }
}

// A failed retry may have no identity-bearing payload at all. Only the exact
// provider error-only shape can be superseded without hiding captured cards.
function failureOnlyPair(row: any): boolean {
  if (!row || !Array.isArray(row.documents) || row.documents.length !== 1) return false;
  const payload = row.documents[0];
  if (!payload || Object.keys(payload).sort().join(",") !== "list,map") return false;
  return ["list", "map"].every(key => {
    if (typeof payload[key] !== "string") return false;
    try {
      const value = JSON.parse(payload[key]);
      return value && typeof value === "object" && !Array.isArray(value) &&
        Object.keys(value).sort().join(",") === "error,success" &&
        value.success === false && typeof value.error === "string" && value.error.length > 0;
    } catch { return false; }
  });
}

/**
 * A successful pair that triggered count drift is evidence of possible
 * collisions, but its changed range is never a candidate for this window.
 */
function driftIdentities(pair: any, total: number, available: number): Identity[] {
  if (!pair || !Array.isArray(pair.documents) || pair.documents.length !== 1) throw new Error("drift pair missing");
  if (!pair.documents[0] || Object.keys(pair.documents[0]).sort().join(",") !== "list,map") throw new Error("drift pair shape invalid");
  const { list: listText, map: mapText } = pair.documents[0];
  if (typeof listText !== "string" || typeof mapText !== "string") throw new Error("drift pair invalid");
  const list = JSON.parse(listText), map = JSON.parse(mapText);
  const changedTotal = Number(list?.total), changedAvailable = Number(list?.totalAvail);
  if (!Number.isSafeInteger(changedTotal) || !Number.isSafeInteger(changedAvailable) ||
      (changedTotal === total && changedAvailable === available) ||
      typeof list?.html !== "string" || !Array.isArray(map?.projectLocations)) throw new Error("drift counts invalid");
  const cards = parseColliersReceiptCards(list.html, groupColliersMapLocations(map.projectLocations), pair.start);
  // Regular pagination requests a full page even when a changed total leaves
  // fewer rows; require exactly the changed-total bounded response.
  const size = Math.min(pair.pageSize, changedTotal - pair.start + 1);
  return replayColliersPage({ start: pair.start, pageSize: size, list: listText, map: mapText, cards },
    changedTotal, changedAvailable);
}

/** No source access or write authorization follows from these candidates. */
export async function scanColliersPartial(stream: Readable, generation: string, attempt: number, startedAt: string, finishedAt: string,
  onPage: ((page: { start: number; page: number; pageSize: number; observedAt: string; list: string; map: string; identities: Identity[] }) => Promise<void>) | undefined,
  onIdentity: (card: Candidate, supported: boolean) => void,
  onHole?: (start: number, size: number, reason: string) => void) {
  if (!generation || !Number.isSafeInteger(attempt) || attempt < 1) throw new Error("invalid attempt binding");
  const startMs = Date.parse(startedAt), endMs = Date.parse(finishedAt);
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs < startMs) throw new Error("invalid attempt interval");
  let chunks: Buffer[] = [], lineBytes = 0, lineNo = 0;
  let pending: any = null, total: number | null = null, available: number | null = null;
  const ranges: Array<[number, number]> = [];
  let heldPages = 0, holes = 0, providerHoles = 0, stopped = false, indexed = 0, countDrift = false;
  const processLine = async (bytes: Buffer) => {
    const row = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    if (lineNo++ === 0) {
      if (!row || Object.keys(row).sort().join(",") !== "attempt,generation,schema,sourceKey" ||
          row.schema !== "colliers-partial-pages/v1" || row.sourceKey !== "colliers" ||
          row.generation !== generation || row.attempt !== attempt) throw new Error("spool header differs");
      return;
    }
    const observedMs = typeof row?.observedAt === "string" ? Date.parse(row.observedAt) : NaN;
    if (!row || row.transaction !== "sale" || row.pass !== 1 || !Number.isSafeInteger(row.page) || row.page < 1 ||
        !Number.isSafeInteger(row.start) || row.start < 1 ||
        !Number.isFinite(observedMs) || new Date(observedMs).toISOString() !== row.observedAt)
      throw new Error("invalid spool observation");
    if (observedMs < startMs || observedMs > endMs)
      throw new Error("partial source observation clock outside capture interval");
    if (countDrift && row.kind !== "recovery_hole") { stopped = true; return; }
    if (row.kind === "unqualified_response_pair") {
      if (pending && !failureOnlyPair(pending)) heldPages++;
      pending = row; return;
    }
    if (row.kind === "recovery_hole") {
      // The final request can ask for a full page even when fewer rows remain;
      // its error-only pair then legitimately covers a smaller terminal hole.
      if (total === null || available === null || !Number.isSafeInteger(row.pageSize) || row.pageSize < 1 ||
          row.start > total || row.pageSize > total - row.start + 1 || typeof row.reason !== "string") {
        pending = null; stopped = true; return;
      }
      if (pending && !failureOnlyPair(pending)) {
        let identities: Identity[] | undefined;
        try {
          if (countDrift || row.reason !== "count_drift" || pending.start !== row.start ||
              pending.page !== row.page || !Number.isSafeInteger(pending.pageSize) ||
              row.pageSize !== Math.min(pending.pageSize, total - pending.start + 1) ||
              Date.parse(pending.observedAt) > observedMs) throw new Error("drift hole differs");
          identities = driftIdentities(pending, total, available);
        } catch { heldPages++; }
        if (identities) {
          countDrift = true;
          for (const [offset, card] of identities.entries()) {
            onIdentity({ ...card, start: pending.start, page: pending.page, pageSize: pending.pageSize,
              offset, observedAt: pending.observedAt }, false);
          }
        }
      } else if (pending && (pending.start !== row.start || pending.page !== row.page ||
          !Number.isSafeInteger(pending.pageSize) || pending.pageSize < row.pageSize)) heldPages++;
      pending = null; holes++;
      ranges.push([row.start, row.start + row.pageSize - 1]);
      if (onHole) onHole(row.start, row.pageSize, row.reason);
      if (row.reason === "provider_failure" && row.pageSize === 1) providerHoles++;
      else if (["budget_exhausted", "requested_limit", "page_cap"].includes(row.reason) ||
               (["recovery_stopped", "count_drift"].includes(row.reason) && countDrift)) { /* explicit unexamined range */ }
      else stopped = true;
      return;
    }
    if (row.kind !== "parsed_page") { pending = null; throw new Error("unsupported spool observation"); }
    const pair = pending;
    pending = null;
    if (!pair || pair.start !== row.start || pair.page !== row.page || pair.pass !== row.pass ||
        Date.parse(pair.observedAt) > Date.parse(row.observedAt) ||
        !Array.isArray(pair.documents) || pair.documents.length !== 1 || !Array.isArray(row.documents)) { heldPages++; return; }
    const source = pair.documents[0];
    let cards: Identity[];
    try {
      const list = JSON.parse(source.list);
      const candidateTotal = total ?? Number(list.total);
      const candidateAvailable = available ?? Number(list.totalAvail);
      cards = replayColliersPage({ start: row.start, pageSize: pair.pageSize, list: source.list, map: source.map, cards: row.documents }, candidateTotal, candidateAvailable);
      total = candidateTotal;
      available = candidateAvailable;
    } catch { heldPages++; return; }
    ranges.push([row.start, row.start + pair.pageSize - 1]);
    // A visitor can stop work on denial; never swallow its errors as page holds.
    if (onPage) await onPage({ start: row.start, page: row.page, pageSize: pair.pageSize, observedAt: row.observedAt, list: source.list, map: source.map, identities: cards });
    for (const [offset, card] of cards.entries()) {
      const item = { ...card, start: row.start, page: row.page, pageSize: pair.pageSize, offset, observedAt: row.observedAt };
      indexed++;
      onIdentity(item, supportedDetail(item));
    }
  };
  for await (const input of stream) {
    const chunk = Buffer.isBuffer(input) ? input : Buffer.from(input);
    let start = 0;
    for (let i = 0; i < chunk.length; i++) if (chunk[i] === 10) {
      const segment = chunk.subarray(start, i); lineBytes += segment.length;
      if (lineBytes > MAX_LINE_BYTES) throw new Error("spool observation exceeds bound");
      chunks.push(segment); await processLine(Buffer.concat(chunks, lineBytes));
      chunks = []; lineBytes = 0; start = i + 1;
    }
    const tail = chunk.subarray(start); lineBytes += tail.length;
    if (lineBytes > MAX_LINE_BYTES) throw new Error("spool observation exceeds bound");
    if (tail.length) chunks.push(tail);
  }
  if (lineNo === 0 || lineBytes !== 0) throw new Error("incomplete spool");
  if (pending) heldPages++;
  if (countDrift && total !== null) {
    // Every position in the original reported scope must be either a
    // validated page or an explicitly recorded hole; gaps are not a proof.
    let next = 1;
    for (const [start, end] of ranges.sort((a, b) => a[0] - b[0])) {
      if (start > next) { stopped = true; break; }
      next = Math.max(next, end + 1);
    }
    if (next <= total) stopped = true;
  }
  return { heldPages, holes, providerHoles, stopped, total, available, indexed, countDrift };
}
