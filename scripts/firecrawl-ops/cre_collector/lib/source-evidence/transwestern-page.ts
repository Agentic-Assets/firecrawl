/** Private parsed-feed capture. This is not a response-byte or freshness claim. */
import { createHash } from "node:crypto";
import { observationNow } from "../observation-clock.js";
import { preservesJsonValue } from "../json-value.js";
import { publishPrivatePage } from "./private-page.js";

const MAX_BODY = 16 * 1024 * 1024;
const MAX_ENTRY = Math.ceil(MAX_BODY * 4 / 3) + 64 * 1024 * 6 + 16384;
const PREFIX = "CRE_TRANSWESTERN_NATIVE_CAPTURE";
const sha = (data: string | Buffer) => createHash("sha256").update(data).digest("hex");
const buckets: Record<string, string[]> = { sale: ["Sale", "Sale or Lease"], lease: ["Lease", "Sublease", "Sale or Lease"] };
function bound(name: string, max: number): number {
  const value = process.env[`${PREFIX}_${name}`];
  if (value === undefined) return max;
  if (!/^[1-9][0-9]*$/.test(value)) throw new Error();
  const n = Number(value);
  if (!Number.isSafeInteger(n) || n > max) throw new Error();
  return n;
}
export function publishTranswesternParsedPage(data: unknown, transaction: string, bucket: string, occurrence: number,
  freshRequested: boolean): void {
  const directory = process.env[`${PREFIX}_DIR`];
  if (!directory) return;
  try {
    if (process.env[`${PREFIX}_SINGLE_WRITER`] !== "1" || !Object.hasOwn(buckets, transaction)
      || !Number.isSafeInteger(occurrence) || buckets[transaction]![occurrence] !== bucket
      || typeof freshRequested !== "boolean") throw new Error();
    if (!preservesJsonValue(data)) throw new Error();
    const json = JSON.stringify(data);
    if (json === undefined || Buffer.byteLength(json, "utf8") > MAX_BODY) throw new Error();
    const body = Buffer.from(json, "utf8");
    const value = { schema: "cre-transwestern-parsed-page/v1", receipt: {
      sourceKey: "transwestern", transaction, bucket, occurrence, capturedAt: observationNow(),
      freshRequested, bodySha256: sha(body), bodyBytes: body.length,
    }, responseBodyBase64: body.toString("base64"), provenance: {
      representation: "collector_parsed_json_not_response_bytes", parser: "unwrap_extract_or_repair_possible",
      numbers: "javascript_number_precision_and_serialization", httpStatus: "unobserved", finalUrl: "unobserved",
      sourceFreshness: "unverified", identity: "unverified", visibility: "unverified", coverage: "unverified",
      originalBodyHashVerification: "caller_asserted_not_database_reverified",
    } };
    const payload = Buffer.from(JSON.stringify(value) + "\n", "utf8");
    publishPrivatePage(directory, `page-${transaction}-${occurrence}-${sha(payload)}.json`, payload,
      /^page-(?:sale|lease)-(?:0|[1-9][0-9]*)-[a-f0-9]{64}\.json$/, MAX_ENTRY,
      bound("MAX_PAGES", 128), bound("MAX_BYTES", 128 * 1024 * 1024));
  } catch { throw new Error("Transwestern native capture failed"); }
}
