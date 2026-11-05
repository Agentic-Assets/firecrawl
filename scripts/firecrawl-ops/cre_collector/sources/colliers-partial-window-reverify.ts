/** Replay a sealed source window. Caller owns checkpoint, gates and sink. */
import { createHash } from "node:crypto";
import { isDeepStrictEqual } from "node:util";
import type { Readable } from "node:stream";
import { deriveColliersPartialDetail } from "./colliers-partial-verify.js";
import { scanColliersPartial } from "./pure/colliers-partial-spool.js";
import { selectColliersWindow } from "./pure/colliers-partial-window.js";

const MAX_LINE = Math.ceil(16 * 1024 * 1024 / 3) * 4 + 16384 + 128;
const REASONS = new Set(["identity_conflict", "detail_identity", "detail_not_found", "detail_empty",
  "detail_invalid_json", "detail_invalid_response", "detail_invalid_utf8", "detail_oversized",
  "detail_http_failure", "detail_timeout", "detail_provider_rejected", "detail_nonjson_html"]);
type Binding = { generation: string; attempt: number; startedAt: string; finishedAt: string; detailFinishedAt: string };

/** Sink must stay private and be discarded unless all verification succeeds. */
export async function reverifyColliersWindow(
  openSource: () => Readable, verifySource: () => Promise<void>,
  openEvidence: () => Readable, verifyEvidence: () => Promise<void>,
  binding: Binding, cursor: number, expectedRecords: number, expectedCursor: number,
  sink: (item: { listing: any; source: any; decodedDetail: string; nativeBase64: string }) => Promise<void>, limit?: number,
) {
  if (!Number.isSafeInteger(expectedRecords) || expectedRecords < 1 || expectedRecords > 2048)
    throw new Error("invalid partial evidence count");
  await verifySource(); await verifyEvidence();
  const selection = await selectColliersWindow(openSource, verifySource, binding, cursor, limit);
  if (selection.available === null) throw new Error("partial source total missing");
  const details = new Map<number, { source: any; decodedDetail: string; nativeBase64: string }>();
  let lineNo = 0, buffered: Buffer[] = [], size = 0, sourceStopped = false;
  const accept = (raw: Buffer) => {
    const row = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw));
    if (lineNo++ === 0) {
      if (!isDeepStrictEqual(row, { schema: "colliers-window-evidence/v1", sourceKey: "colliers",
        generation: binding.generation, attempt: binding.attempt })) throw new Error("partial evidence header differs");
      return;
    }
    if (sourceStopped) throw new Error("partial evidence continues after source stop");
    const member = selection.window[lineNo - 2];
    if (!member) throw new Error("partial evidence exceeds selected window");
    const candidate = member.candidate, p = candidate.start + candidate.offset;
    if (row.kind === "held") {
      if (!REASONS.has(row.reason) || !isDeepStrictEqual(row.candidate, candidate) ||
          (!member.eligible && row.reason !== "identity_conflict") ||
          (member.eligible && row.reason === "identity_conflict")) throw new Error("partial held identity differs");
      if (row.reason === "detail_not_found" && ![404, 410].includes(row.status))
        throw new Error("partial held HTTP status differs");
      if (row.reason === "detail_http_failure" &&
          (!Number.isInteger(row.status) || row.status < 300 || row.status > 599 ||
           [401, 403, 404, 410, 429].includes(row.status)))
        throw new Error("partial stopped HTTP status differs");
      sourceStopped = ["detail_http_failure", "detail_timeout", "detail_provider_rejected", "detail_nonjson_html"].includes(row.reason);
    } else if (row.kind === "detail") {
      if (!member.eligible || !row.source || row.source.mapProjectId !== candidate.mapProjectId ||
          row.source.start !== candidate.start || row.source.offset !== candidate.offset || row.source.page !== candidate.page ||
          row.source.pageSize !== candidate.pageSize || row.source.inventoryObservedAt !== candidate.observedAt)
        throw new Error("partial detail identity differs");
    } else throw new Error("partial evidence record differs");
    const encoded = row.nativeBase64;
    if (encoded !== undefined) {
      if (typeof encoded !== "string" || encoded.length > Math.ceil(16 * 1024 * 1024 / 3) * 4)
        throw new Error("partial native bytes invalid");
      const native = Buffer.from(encoded, "base64");
      if (native.length > 16 * 1024 * 1024 || native.toString("base64") !== encoded) throw new Error("partial native encoding invalid");
      if (row.kind === "detail") {
        if (createHash("sha256").update(native).digest("hex") !== row.source.nativeSha256)
          throw new Error("partial native hash differs");
        details.set(p, { source: row.source, decodedDetail: new TextDecoder("utf-8", { fatal: true }).decode(native), nativeBase64: encoded });
      }
    } else if (row.kind === "detail") throw new Error("partial native bytes missing");
  };
  for await (const input of openEvidence()) {
    const chunk = Buffer.isBuffer(input) ? input : Buffer.from(input);
    let start = 0;
    for (let i = 0; i < chunk.length; i++) if (chunk[i] === 10) {
      const part = chunk.subarray(start, i); size += part.length;
      if (size > MAX_LINE) throw new Error("partial evidence line exceeds bound");
      buffered.push(part); accept(Buffer.concat(buffered, size)); buffered = []; size = 0; start = i + 1;
    }
    const tail = chunk.subarray(start); size += tail.length;
    if (size > MAX_LINE) throw new Error("partial evidence line exceeds bound");
    if (tail.length) buffered.push(tail);
  }
  if (size || lineNo !== expectedRecords + 1) throw new Error("partial evidence partition differs");
  let computedCursor = cursor;
  for (const member of selection.window.slice(0, expectedRecords))
    computedCursor = ((member.candidate.start + member.candidate.offset) % selection.total) + 1;
  if (computedCursor !== expectedCursor) throw new Error("partial cursor differs");
  await verifyEvidence();
  let emitted = 0;
  const replay = await scanColliersPartial(openSource(), binding.generation, binding.attempt, binding.startedAt, binding.finishedAt,
    async page => {
      for (const [offset] of page.identities.entries()) {
        const p = page.start + offset, detail = details.get(p);
        if (!detail) continue;
        const mapped = await deriveColliersPartialDetail({ ...page, cards: page.identities },
          detail.source, detail.decodedDetail, binding, selection.total, selection.available!);
        await sink({ listing: mapped.listing, source: detail.source, decodedDetail: detail.decodedDetail, nativeBase64: detail.nativeBase64 });
        details.delete(p); emitted++;
      }
    }, () => {});
  await verifySource(); await verifyEvidence();
  if (replay.heldPages || replay.stopped || replay.total !== selection.total || replay.holes !== selection.holes || details.size)
    throw new Error("partial replay partition changed");
  return { emitted, held: expectedRecords - emitted, proposedCursor: computedCursor };
}
