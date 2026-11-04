import { NativeEvidenceTerminalError } from "./terminal-error.js";
import { createHash } from "node:crypto";
import { publishPrivatePage } from "./private-page.js";

const MAX_BODY_BYTES = 16 * 1024 * 1024;
const MAX_PAGES = 128;
const MAX_BYTES = 128 * 1024 * 1024;
const MAX_ENTRY_BYTES = Math.ceil(MAX_BODY_BYTES * 4 / 3) + 16_384;
const REQUEST_URL = "https://pse-api.sharplaunch.com/data?entity=website&status=active";

function bound(name: string, maximum: number): number {
  const value = process.env[name];
  if (value === undefined) return maximum;
  if (!/^[1-9][0-9]*$/.test(value)) throw new Error();
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number > maximum) throw new Error();
  return number;
}

function containsCredential(body: string, secret: string): boolean {
  if (body.includes(secret)) return true;
  // SharpLaunch integration keys are alphanumeric; reject equivalent JSON
  // Unicode escapes too, without placing the key in errors or metadata.
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(secret)) throw new Error();
  const pattern = Array.from(secret, (char) => {
    const code = char.charCodeAt(0).toString(16).padStart(4, "0");
    return `(?:${char.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}|\\\\u${code})`;
  }).join("");
  return new RegExp(pattern, "i").test(body);
}

/** Retain collector-decoded text locally; source-owned projection precedes admission. */
export function publishAvisonNativePage(directory: string, body: string, secret: string,
  startedAt: string, finishedAt: string): string {
  try {
    if (process.env.CRE_AVISON_NATIVE_CAPTURE_SINGLE_WRITER !== "1" || !secret || containsCredential(body, secret)) throw new Error();
    const validTime = (value: string) => /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/.test(value)
      && Number.isFinite(Date.parse(value)) && new Date(value).toISOString() === value;
    if (!validTime(startedAt) || !validTime(finishedAt) || startedAt > finishedAt) throw new Error();
    const bytes = Buffer.from(body, "utf8");
    if (bytes.length > MAX_BODY_BYTES) throw new Error();
    const payload = Buffer.from(JSON.stringify({
      schema: "cre-avison-sharplaunch-website-page/v1",
      receipt: { sourceKey: "avison-young", requestUrl: REQUEST_URL, method: "GET",
        httpOutcome: "success_status_unobserved", startedAt, finishedAt },
      responseBodyBase64: bytes.toString("base64"),
      provenance: { bodyKind: "collector_decoded_text_reencoded_utf8_not_fetch_or_wire_bytes",
        identity: "unverified", visibility: "unverified", completeness: "unverified" },
    }) + "\n", "utf8");
    const digest = createHash("sha256").update(payload).digest("hex");
    return publishPrivatePage(directory, `page-website-${digest}.json`, payload,
      /^page-website-[a-f0-9]{64}\.json$/, MAX_ENTRY_BYTES,
      bound("CRE_AVISON_NATIVE_CAPTURE_MAX_PAGES", MAX_PAGES),
      bound("CRE_AVISON_NATIVE_CAPTURE_MAX_BYTES", MAX_BYTES));
  } catch {
    throw new NativeEvidenceTerminalError("Avison Young native capture failed");
  }
}
