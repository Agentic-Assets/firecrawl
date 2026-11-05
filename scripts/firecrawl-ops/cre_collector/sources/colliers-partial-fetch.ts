/** Bounded source response for supervised partial-detail work. */
import { readBoundedBytes, ResponseBodyDeadlineError, ResponseBodyLimitError, settleResponseBodyRead, cancelResponseBody } from "../lib/response-body.js";
import { colliersHeaders, colliersSlpInitUrl, ColliersAccessStop } from "./colliers.js";
import { NativeEvidenceTerminalError } from "../lib/source-evidence/terminal-error.js";

const MAX_DETAIL_BYTES = 16 * 1024 * 1024;
const TIMEOUT_MS = 30_000;

type HoldReason = "detail_not_found" | "detail_empty" | "detail_invalid_json" | "detail_invalid_response" | "detail_invalid_utf8" | "detail_oversized";
export class ColliersDetailHold extends Error {
  declare readonly evidence?: Uint8Array;
  constructor(readonly reason: HoldReason, evidence?: Uint8Array, readonly status?: number) {
    super(reason);
    if (evidence !== undefined) Object.defineProperty(this, "evidence", { value: evidence, enumerable: false });
  }
}
export class ColliersSettledSourceStop extends Error {
  declare readonly evidence?: Uint8Array;
  constructor(readonly reason: "detail_http_failure" | "detail_timeout" | "detail_provider_rejected" | "detail_nonjson_html",
    readonly status?: number, evidence?: Uint8Array) {
    super(reason);
    if (evidence !== undefined) Object.defineProperty(this, "evidence", { value: evidence, enumerable: false });
  }
}

/** Caller must store decoded text privately and bind its digest before admission. */
export async function fetchColliersPartialDetail(token: string, fetchImpl: typeof fetch = fetch,
  maxBytes: number = MAX_DETAIL_BYTES): Promise<{ detail: any; decodedText: string; nativeBytes: Uint8Array }> {
  if (typeof token !== "string" || !token || token.length > 512 || /[\x00-\x1f\x7f]/.test(token) ||
      !Number.isSafeInteger(maxBytes) || maxBytes < 1 || maxBytes > MAX_DETAIL_BYTES) throw new Error("invalid partial detail input");
  const controller = new AbortController();
  const deadlineAt = performance.now() + TIMEOUT_MS;
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, TIMEOUT_MS);
  try {
    const response = await fetchImpl(colliersSlpInitUrl(token), { headers: colliersHeaders(), redirect: "manual", signal: controller.signal });
    const stop = [401, 403, 429].includes(response.status) || response.headers.has("retry-after");
    if (stop || !response.ok) {
      if (!await cancelResponseBody(response.body, 1000)) throw new NativeEvidenceTerminalError("Colliers detail response cleanup unsettled");
      if (stop) throw new ColliersAccessStop("Colliers partial detail refused or delayed");
      if ([404, 410].includes(response.status)) throw new ColliersDetailHold("detail_not_found", undefined, response.status);
      throw new ColliersSettledSourceStop("detail_http_failure", response.status);
    }
    if (!response.body) throw new ColliersDetailHold("detail_empty", new Uint8Array(0));
    const reader = response.body.getReader();
    let bytes: Uint8Array;
    try {
      bytes = await readBoundedBytes(reader, { maxBytes, signal: controller.signal, deadlineAt, retainPrefix: true });
    } catch (error) {
      controller.abort();
      if (!await settleResponseBodyRead(reader, error, 1000)) throw new NativeEvidenceTerminalError("Colliers detail body cleanup unsettled", { cause: error });
      if (error instanceof ResponseBodyLimitError) throw new ColliersDetailHold("detail_oversized", error.prefix);
      if (timedOut || error instanceof ResponseBodyDeadlineError) throw new ColliersSettledSourceStop("detail_timeout");
      throw error;
    } finally { try { reader.releaseLock(); } catch { /* preserve failure */ } }
    if (bytes.length === 0) throw new ColliersDetailHold("detail_empty", bytes);
    let decodedText: string;
    try { decodedText = new TextDecoder("utf-8", { fatal: true }).decode(bytes); }
    catch { throw new ColliersDetailHold("detail_invalid_utf8", bytes); }
    if (decodedText.trimStart().startsWith("<")) throw new ColliersSettledSourceStop("detail_nonjson_html", undefined, bytes);
    let detail: any;
    try { detail = JSON.parse(decodedText); }
    catch { throw new ColliersDetailHold("detail_invalid_json", bytes); }
    if (detail && typeof detail === "object" && !Array.isArray(detail) && detail.success === false)
      throw new ColliersSettledSourceStop("detail_provider_rejected", undefined, bytes);
    if (!detail || typeof detail !== "object" || Array.isArray(detail))
      throw new ColliersDetailHold("detail_invalid_response", bytes);
    return { detail, decodedText, nativeBytes: bytes };
  } finally { clearTimeout(timer); }
}
