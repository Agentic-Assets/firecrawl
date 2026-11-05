/** Stream attempt-local evidence. Caller owns path/descriptor, hash and generation binding. */
import type { Readable } from "node:stream";
import { replayColliersPage } from "./colliers-partial-replay.js";

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
  let heldPages = 0, holes = 0, providerHoles = 0, stopped = false, indexed = 0;
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
        !Number.isFinite(observedMs) || new Date(observedMs).toISOString() !== row.observedAt ||
        observedMs < startMs || observedMs > endMs) throw new Error("invalid spool observation");
    if (row.kind === "unqualified_response_pair") {
      if (pending && !failureOnlyPair(pending)) heldPages++;
      pending = row; return;
    }
    if (row.kind === "recovery_hole") {
      if (pending && (!failureOnlyPair(pending) || pending.start !== row.start || pending.pageSize !== row.pageSize))
        heldPages++;
      pending = null; holes++;
      if (total === null || !Number.isSafeInteger(row.pageSize) || row.pageSize < 1 ||
          row.start > total || row.pageSize > total - row.start + 1) { stopped = true; return; }
      if (typeof row.reason !== "string") { stopped = true; return; }
      if (onHole) onHole(row.start, row.pageSize, row.reason);
      if (row.reason === "provider_failure" && row.pageSize === 1) providerHoles++;
      else if (["budget_exhausted", "requested_limit", "page_cap"].includes(row.reason)) { /* explicit unexamined range */ }
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
  return { heldPages, holes, providerHoles, stopped, total, available, indexed };
}
