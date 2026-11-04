import { NativeEvidenceTerminalError } from "./terminal-error.js";
/** Bounded private capture of collector-decoded Pyramid response text. */
import { createHash } from "node:crypto";
import { gzipSync } from "node:zlib";
import { observationNow } from "../observation-clock.js";
import { publishPrivatePage } from "./private-page.js";

const PREFIX = "CRE_PYRAMID_NATIVE_CAPTURE";
const MAX_BODY = 16 * 1024 * 1024;
const MAX_ENTRY = Math.ceil((MAX_BODY + 65536) * 4 / 3) + 16384;
const digest = (value: Buffer) => createHash("sha256").update(value).digest("hex");
export class PyramidNativeCaptureError extends NativeEvidenceTerminalError {
  constructor() { super("Pyramid native capture failed"); this.name = "PyramidNativeCaptureError"; }
}
function limit(name: string, maximum: number): number {
  const value = process.env[`${PREFIX}_${name}`];
  if (value === undefined) return maximum;
  if (!/^[1-9][0-9]*$/.test(value)) throw new Error();
  const n = Number(value);
  if (!Number.isSafeInteger(n) || n > maximum) throw new Error();
  return n;
}
export function publishPyramidPage(body: string, url: string, page: number, pass: number, status: number, headers: Headers): void {
  const dir = process.env[`${PREFIX}_DIR`];
  if (!dir) return;
  try {
    if (process.env[`${PREFIX}_SINGLE_WRITER`] !== "1" || !Number.isSafeInteger(page) || page < 1 ||
        !Number.isSafeInteger(pass) || pass < 1 || pass > 5 || !Number.isInteger(status) || status < 200 || status > 299) throw new Error();
    const bytes = Buffer.from(body, "utf8");
    if (bytes.length > MAX_BODY) throw new Error();
    const compressed = gzipSync(bytes);
    const header = (name: string) => { const value = headers.get(name); return value !== null && /^[0-9]{1,16}$/.test(value) ? value : null; };
    const data = { schema: "cre-pyramid-decoded-page/v1", receipt: { sourceKey: "pyramid-brokerage", requestUrl: url,
      page, pass, status, capturedAt: observationNow(), totalHeader: header("x-wp-total"), totalPagesHeader: header("x-wp-totalpages"),
      bodySha256: digest(bytes), bodyBytes: bytes.length, storedSha256: digest(compressed), storedBytes: compressed.length },
      storageCodec: "gzip_base64", responseBodyBase64: compressed.toString("base64"), provenance: {
        representation: "collector_decoded_utf8_text_not_wire", utf8Decoding: "replacement_possible",
        httpStatusOrigin: "collector_final_response", finalUrl: "unobserved",
        sourceFreshness: "unverified", identity: "unverified", visibility: "unverified", coverage: "unverified" } };
    const payload = Buffer.from(JSON.stringify(data) + "\n", "utf8");
    publishPrivatePage(dir, `page-${pass}-${page}-${digest(payload)}.json`, payload,
      /^page-[1-5]-[1-9][0-9]*-[a-f0-9]{64}\.json$/, MAX_ENTRY, limit("MAX_PAGES", 128), limit("MAX_BYTES", 128 * 1024 * 1024));
  } catch { throw new PyramidNativeCaptureError(); }
}
