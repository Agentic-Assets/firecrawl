/** Replay a sealed source window. Caller owns checkpoint, gates and sink. */
import { createHash } from "node:crypto";
import { isDeepStrictEqual } from "node:util";
import type { Readable } from "node:stream";
import { deriveColliersPartialDetail } from "./colliers-partial-verify.js";
import { ColliersIdentityError } from "./colliers.js";
import { scanColliersPartial } from "./pure/colliers-partial-spool.js";
import { selectColliersWindow } from "./pure/colliers-partial-window.js";

const MAX_NATIVE_BYTES = 16 * 1024 * 1024;
const MAX_LINE = Math.ceil((MAX_NATIVE_BYTES + 1) / 3) * 4 + 16384 + 128;
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
  stopBeforePosition?: number,
) {
  if (!Number.isSafeInteger(expectedRecords) || expectedRecords < 0 || expectedRecords > 2048 ||
      (stopBeforePosition !== undefined && (!Number.isSafeInteger(stopBeforePosition) || stopBeforePosition < 1)))
    throw new Error("invalid partial evidence count");
  const detailFinish = Date.parse(binding.detailFinishedAt), sourceStart = Date.parse(binding.startedAt);
  if (!Number.isFinite(detailFinish) || !Number.isFinite(sourceStart) || detailFinish < sourceStart ||
      detailFinish > Date.now() + 5 * 60_000) throw new Error("partial detail finish invalid");
  await verifySource(); await verifyEvidence();
  const selection = await selectColliersWindow(openSource, verifySource, binding, cursor, limit);
  if (selection.available === null) throw new Error("partial source total missing");
  if (stopBeforePosition !== undefined && stopBeforePosition > selection.total)
    throw new Error("partial rotation boundary outside source");
  const first = selection.window[0]?.candidate;
  const next = selection.window[expectedRecords]?.candidate;
  const firstPosition = first ? first.start + first.offset : null;
  const rotationComplete = stopBeforePosition !== undefined && !!next &&
    next.start + next.offset === stopBeforePosition;
  if (expectedRecords === 0 && !rotationComplete) throw new Error("empty partial evidence is unverified");
  const details = new Map<number, { source: any; decodedDetail: string; nativeBase64: string }>();
  const identityHolds = new Map<number, { decoded: string; native: Buffer; observedAt: string }>();
  let lineNo = 0, buffered: Buffer[] = [], size = 0, sourceStopped = false;
  let sourceStopPosition: number | undefined;
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
    if (!row || typeof row !== "object" || Array.isArray(row)) throw new Error("partial evidence record differs");
    if (row.kind === "held") {
      const keys = ["candidate", "kind", "reason"];
      for (const key of ["status", "observedAt", "nativeBase64"]) if (row[key] !== undefined) keys.push(key);
      if (Object.keys(row).sort().join(",") !== keys.sort().join(",") ||
          !REASONS.has(row.reason) || !isDeepStrictEqual(row.candidate, candidate) ||
          (!member.eligible && row.reason !== "identity_conflict") ||
          (member.eligible && row.reason === "identity_conflict")) throw new Error("partial held identity differs");
      if (row.reason === "identity_conflict") {
        if (row.status !== undefined || row.observedAt !== undefined || row.nativeBase64 !== undefined)
          throw new Error("partial identity hold evidence differs");
        return;
      }
      const observed = typeof row.observedAt === "string" ? Date.parse(row.observedAt) : NaN;
      if (!Number.isFinite(observed) || new Date(observed).toISOString() !== row.observedAt ||
          observed < sourceStart || observed > detailFinish) throw new Error("partial held observation invalid");
      const withBody = ["detail_identity", "detail_empty", "detail_invalid_json", "detail_invalid_response",
        "detail_invalid_utf8", "detail_oversized", "detail_provider_rejected", "detail_nonjson_html"].includes(row.reason);
      if (withBody !== (row.nativeBase64 !== undefined) ||
          (row.reason === "detail_not_found" && ![404, 410].includes(row.status)) ||
          (row.reason === "detail_http_failure" && (!Number.isInteger(row.status) || row.status < 300 || row.status > 599 ||
            [401, 403, 404, 410, 429].includes(row.status))) ||
          (!["detail_not_found", "detail_http_failure"].includes(row.reason) && row.status !== undefined))
        throw new Error("partial held response metadata differs");
      if (withBody) {
        if (typeof row.nativeBase64 !== "string" || row.nativeBase64.length > Math.ceil((MAX_NATIVE_BYTES + 1) / 3) * 4)
          throw new Error("partial held bytes invalid");
        const native = Buffer.from(row.nativeBase64, "base64");
        if (native.toString("base64") !== row.nativeBase64 ||
            native.length > MAX_NATIVE_BYTES + (row.reason === "detail_oversized" ? 1 : 0) ||
            (row.reason === "detail_empty" ? native.length !== 0 : native.length === 0) ||
            (row.reason === "detail_oversized" && native.length !== MAX_NATIVE_BYTES + 1))
          throw new Error("partial held bytes differ");
        let decoded: string | undefined;
        try { decoded = new TextDecoder("utf-8", { fatal: true }).decode(native); } catch { /* invalid bytes */ }
        if (row.reason !== "detail_oversized" && (row.reason === "detail_invalid_utf8") !== (decoded === undefined))
          throw new Error("partial held encoding differs");
        if (decoded !== undefined && !["detail_empty", "detail_oversized"].includes(row.reason)) {
          let value: any, valid = true;
          try { value = JSON.parse(decoded); } catch { valid = false; }
          const html = decoded.trimStart().startsWith("<");
          if ((row.reason === "detail_invalid_json" && (valid || html)) ||
              (row.reason === "detail_nonjson_html" && !html) ||
              (row.reason === "detail_invalid_response" && (!valid || (value && typeof value === "object" && !Array.isArray(value)))) ||
              (row.reason === "detail_provider_rejected" && (!valid || !value || typeof value !== "object" || Array.isArray(value) || value.success !== false)) ||
              (row.reason === "detail_identity" && (!valid || !value || typeof value !== "object" || Array.isArray(value) || value.success === false)))
            throw new Error("partial held classification differs");
          if (row.reason === "detail_identity") identityHolds.set(p, { decoded, native, observedAt: row.observedAt });
        }
      }
      sourceStopped = ["detail_http_failure", "detail_timeout", "detail_provider_rejected", "detail_nonjson_html"].includes(row.reason);
      if (sourceStopped) sourceStopPosition = p;
      return;
    }
    if (row.kind !== "detail" || Object.keys(row).sort().join(",") !== "kind,nativeBase64,source" ||
        !member.eligible || !row.source || row.source.mapProjectId !== candidate.mapProjectId ||
        row.source.start !== candidate.start || row.source.offset !== candidate.offset || row.source.page !== candidate.page ||
        row.source.pageSize !== candidate.pageSize || row.source.inventoryObservedAt !== candidate.observedAt)
      throw new Error("partial detail identity differs");
    const encoded = row.nativeBase64;
    if (typeof encoded !== "string" || encoded.length > Math.ceil(MAX_NATIVE_BYTES / 3) * 4)
      throw new Error("partial native bytes invalid");
    const native = Buffer.from(encoded, "base64");
    if (native.length > MAX_NATIVE_BYTES || native.toString("base64") !== encoded ||
        createHash("sha256").update(native).digest("hex") !== row.source.nativeSha256)
      throw new Error("partial native hash differs");
    details.set(p, { source: row.source, decodedDetail: new TextDecoder("utf-8", { fatal: true }).decode(native), nativeBase64: encoded });
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
  for (const member of selection.window.slice(0, expectedRecords)) {
    const position = member.candidate.start + member.candidate.offset;
    if (position === sourceStopPosition) break;
    computedCursor = (position % selection.total) + 1;
  }
  if (computedCursor !== expectedCursor) throw new Error("partial cursor differs");
  await verifyEvidence();
  let emitted = 0;
  const replay = await scanColliersPartial(openSource(), binding.generation, binding.attempt, binding.startedAt, binding.finishedAt,
    async page => {
      for (const [offset] of page.identities.entries()) {
        const p = page.start + offset, detail = details.get(p), held = identityHolds.get(p);
        if (held) {
          const candidate = selection.window.find(member => member.candidate.start + member.candidate.offset === p)?.candidate;
          if (!candidate?.detailPv) throw new Error("partial held page identity missing");
          const hash = (value: string | Uint8Array) => createHash("sha256").update(value).digest("hex");
          const source = { generation: binding.generation, attempt: binding.attempt, start: page.start,
            page: page.page, pageSize: page.pageSize, offset, inventoryObservedAt: page.observedAt,
            detailObservedAt: held.observedAt, mapProjectId: candidate.mapProjectId,
            listSha256: hash(page.list), mapSha256: hash(page.map), detailTokenSha256: hash(candidate.detailPv),
            decodedDetailSha256: hash(held.decoded), nativeSha256: hash(held.native) };
          let rejected = false;
          try { await deriveColliersPartialDetail({ ...page, cards: page.identities }, source, held.decoded, binding, selection.total, selection.available!); }
          catch (error) { if (error instanceof ColliersIdentityError) rejected = true; else throw error; }
          if (!rejected) throw new Error("partial identity hold classification differs");
          identityHolds.delete(p);
        }
        if (!detail) continue;
        const mapped = await deriveColliersPartialDetail({ ...page, cards: page.identities },
          detail.source, detail.decodedDetail, binding, selection.total, selection.available!);
        await sink({ listing: mapped.listing, source: detail.source, decodedDetail: detail.decodedDetail, nativeBase64: detail.nativeBase64 });
        details.delete(p); emitted++;
      }
    }, () => {});
  await verifySource(); await verifyEvidence();
  if (replay.heldPages || replay.stopped || replay.total !== selection.total || replay.holes !== selection.holes || details.size || identityHolds.size)
    throw new Error("partial replay partition changed");
  return { emitted, held: expectedRecords - emitted, proposedCursor: computedCursor,
    sourceStopped, rotationComplete, firstPosition };
}
