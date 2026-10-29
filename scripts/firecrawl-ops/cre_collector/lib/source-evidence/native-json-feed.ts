/** Private bounded capture of explicitly scoped native JSON source responses. */
import { createHash } from "node:crypto";
import { gzipSync } from "node:zlib";
import { observationNow } from "../observation-clock.js";
import { publishPrivatePage } from "./private-page.js";

const PREFIX = "CRE_NATIVE_JSON_FEED_CAPTURE";
const MAX_BODY = 16 * 1024 * 1024;
const MAX_ENTRY = Math.ceil((MAX_BODY + 65536) * 4 / 3) + 16384;
const digest = (value: Buffer) => createHash("sha256").update(value).digest("hex");
let sequence = 0;
export type NativeJsonProfile = "srs_search_post_v1" | "kidder_bulk_post_v1" | "kidder_paged_post_v1" | "hanley_embedded_array_v1";
export function nativeJsonFeedCaptureEnabled(): boolean { return Boolean(process.env[`${PREFIX}_DIR`]); }
export class NativeJsonFeedCaptureError extends Error {
  constructor() { super("Native JSON feed capture failed"); this.name = "NativeJsonFeedCaptureError"; }
}
function limit(name: string, maximum: number, defaultValue = maximum): number {
  const raw = process.env[`${PREFIX}_${name}`];
  if (raw === undefined) return defaultValue;
  if (!/^[1-9][0-9]*$/.test(raw)) throw new Error();
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value > maximum) throw new Error();
  return value;
}
export function publishNativeJsonFeed(input: {
  profile: NativeJsonProfile; sourceKey: "srs" | "kidder-mathews" | "hanley";
  requestVariant: "strict" | "standard" | "direct" | "firecrawl";
  requestBody: string | null; page: number | null; offset: number | null;
  status: number | null; body: string;
}): void {
  const dir = process.env[`${PREFIX}_DIR`];
  if (!dir) return;
  try {
    const hanley = input.profile === "hanley_embedded_array_v1";
    const profiles = { srs_search_post_v1: "srs", kidder_bulk_post_v1: "kidder-mathews",
      kidder_paged_post_v1: "kidder-mathews", hanley_embedded_array_v1: "hanley" } as const;
    if (process.env[`${PREFIX}_SINGLE_WRITER`] !== "1" || process.env[`${PREFIX}_SOURCE_KEY`] !== input.sourceKey ||
        profiles[input.profile] !== input.sourceKey ||
        (hanley ? (input.page !== null || input.offset !== null || input.requestBody !== null ||
          !["direct", "firecrawl"].includes(input.requestVariant)) :
          (typeof input.requestBody !== "string" || !Number.isSafeInteger(input.page) || (input.page as number) < 0 ||
           !Number.isSafeInteger(input.offset) || (input.offset as number) < 0 || !["strict", "standard"].includes(input.requestVariant))) ||
        (input.profile === "kidder_bulk_post_v1" && (input.requestVariant !== "strict" || input.page !== 0 || input.offset !== 0)) ||
        (input.profile === "kidder_paged_post_v1" && (input.requestVariant !== "standard" || input.offset !== (input.page as number) * 50)) ||
        (input.profile === "srs_search_post_v1" && input.offset !== (input.page as number) * 12) ||
        (hanley && input.requestVariant === "firecrawl" && input.status !== null) ||
        (input.status === null ? !(hanley && input.requestVariant === "firecrawl") :
          (!Number.isInteger(input.status) || input.status < 200 || input.status > 299))) throw new Error();
    const body = Buffer.from(input.body, "utf8");
    if (body.length > MAX_BODY) throw new Error();
    const stored = gzipSync(body);
    const next = sequence + 1;
    const receipt = { sourceKey: input.sourceKey, requestVariant: input.requestVariant,
      requestSha256: input.requestBody === null ? null : digest(Buffer.from(input.requestBody, "utf8")),
      sequence: next, page: input.page, offset: input.offset, status: input.status, capturedAt: observationNow(),
      bodySha256: digest(body), bodyBytes: body.length, storedSha256: digest(stored), storedBytes: stored.length };
    const provenance = { representation: hanley ? "collector_decoded_html_embedded_json_literal_not_wire" : "collector_decoded_utf8_text_not_wire",
      utf8Decoding: hanley ? "collector_string_encoding_unobserved" : input.profile === "kidder_bulk_post_v1" ? "fatal_utf8" : "replacement_possible",
      httpStatusOrigin: input.status === null ? "unobserved" : "direct_response", finalUrl: "unobserved",
      sourceFreshness: "unverified", identity: "unverified", visibility: "unverified", coverage: "unverified" };
    const payload = Buffer.from(JSON.stringify({ schema: "cre-native-json-feed-page/v1", profile: input.profile,
      receipt, storageCodec: "gzip_base64", responseBodyBase64: stored.toString("base64"), provenance }) + "\n", "utf8");
    publishPrivatePage(dir, `page-${next}-${digest(payload)}.json`, payload,
      /^page-[1-9][0-9]*-[a-f0-9]{64}\.json$/, MAX_ENTRY, limit("MAX_PAGES", input.sourceKey === "srs" ? 256 : 128, 128), limit("MAX_BYTES", 128 * 1024 * 1024));
    sequence = next;
  } catch { throw new NativeJsonFeedCaptureError(); }
}
