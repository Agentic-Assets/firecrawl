/** Private Buildout parsed-page custody. This is not an HTTP response capture. */
import { createHash } from "node:crypto";
import { buildoutFeedFor, type BuildoutFeedCoordinates } from "../../sources/buildout-feeds/index.js";
import { observationNow } from "../observation-clock.js";
import { preservesJsonValue } from "../json-value.js";
import { publishPrivatePage } from "./private-page.js";

const MAX_BODY_BYTES = 16 * 1024 * 1024;
const MAX_ENTRY_BYTES = Math.ceil(MAX_BODY_BYTES * 4 / 3) + 64 * 1024 * 6 + 16384;
const MAX_PAGES = 128;
const MAX_BYTES = 128 * 1024 * 1024;
const sha256 = (value: Uint8Array | string) => createHash("sha256").update(value).digest("hex");

function timestampKey(value: string): string {
  const match = /^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,6}))?Z$/.exec(value);
  const millis = Date.parse(value);
  if (!match || match[1]!.startsWith("0000-") || !Number.isFinite(millis)
      || new Date(millis).toISOString().slice(0, 19) !== match[1]) throw new Error();
  return `${match[1]}.${(match[2] ?? "").padEnd(6, "0")}`;
}

export class BuildoutRepresentationError extends Error {
  constructor() { super("Buildout parsed value cannot be preserved exactly"); }
}

export function requireBuildoutJsonValue(value: unknown): void {
  if (!preservesJsonValue(value)) throw new BuildoutRepresentationError();
}

export class BuildoutNativeCaptureError extends Error {
  constructor() { super("Buildout native capture failed"); }
}

function bound(name: string, maximum: number): number {
  const value = process.env[name];
  if (value === undefined) return maximum;
  if (!/^[1-9][0-9]*$/.test(value)) throw new Error();
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number > maximum) throw new Error();
  return number;
}

/** Bind an enabled capture to the source's exact configured feed variant. */
export function assertBuildoutCaptureScope(pluginKey: string, cacheSlug: string | undefined): Readonly<BuildoutFeedCoordinates> | undefined {
  if (!process.env.CRE_BUILDOUT_NATIVE_CAPTURE_DIR) return;
  try {
    const source = process.env.CRE_BUILDOUT_NATIVE_CAPTURE_SOURCE_KEY;
    const feed = source && cacheSlug ? buildoutFeedFor(source, cacheSlug) : null;
    if (process.env.CRE_BUILDOUT_NATIVE_CAPTURE_SINGLE_WRITER !== "1" || !source || !feed
        || feed.pluginKey !== pluginKey) {
      throw new Error();
    }
    return feed;
  } catch {
    throw new BuildoutNativeCaptureError();
  }
}

export function publishBuildoutParsedPage(input: {
  pluginKey: string; cacheSlug: string | undefined; page: number; inventorySort: string | undefined;
  data: unknown; sourceObservedAt: string | null; cacheDisposition: "generation_cache" | "live";
  transport: "direct_json" | "firecrawl_json" | null;
}): void {
  const directory = process.env.CRE_BUILDOUT_NATIVE_CAPTURE_DIR;
  if (!directory) return;
  try {
    const feed = assertBuildoutCaptureScope(input.pluginKey, input.cacheSlug);
    if (!feed) throw new Error();
    const sourceKey = process.env.CRE_BUILDOUT_NATIVE_CAPTURE_SOURCE_KEY!;
    if (!Number.isSafeInteger(input.page) || input.page < 0
        || (input.inventorySort !== undefined && input.inventorySort !== "created_at asc, id asc")
        || (input.sourceObservedAt !== null && (typeof input.sourceObservedAt !== "string" || input.sourceObservedAt.length > 128))
        || !["generation_cache", "live"].includes(input.cacheDisposition)
        || ![null, "direct_json", "firecrawl_json"].includes(input.transport)) throw new Error();
    requireBuildoutJsonValue(input.data);
    const json = JSON.stringify(input.data);
    if (json === undefined || json.length > MAX_BODY_BYTES || Buffer.byteLength(json, "utf8") > MAX_BODY_BYTES) throw new Error();
    const body = Buffer.from(json, "utf8");
    const inventorySort = input.inventorySort ?? null;
    const capturedAt = observationNow();
    if (input.sourceObservedAt !== null && timestampKey(input.sourceObservedAt) > timestampKey(capturedAt)) throw new Error();
    const payload = Buffer.from(JSON.stringify({
      schema: "cre-buildout-parsed-page/v1",
      receipt: { sourceKey, page: input.page,
        ...(feed.feedKey !== feed.sourceKey ? { feedKey: feed.feedKey } : {}),
        providerScopeSha256: sha256(JSON.stringify([sourceKey, input.pluginKey, inventorySort])),
        inventorySort, capturedAt, sourceObservedAt: input.sourceObservedAt,
        cacheDisposition: input.cacheDisposition, transport: input.transport,
        bodySha256: sha256(body), bodyBytes: body.length },
      responseBodyBase64: body.toString("base64"),
      provenance: { representation: "collector_parsed_json_not_response_bytes", numbers: "javascript_number_precision",
        identity: "unverified", visibility: "unverified", coverage: "unverified" },
    }) + "\n", "utf8");
    publishPrivatePage(directory, `page-${input.page}-${sha256(payload)}.json`, payload,
      /^page-(?:0|[1-9][0-9]*)-[a-f0-9]{64}\.json$/, MAX_ENTRY_BYTES,
      bound("CRE_BUILDOUT_NATIVE_CAPTURE_MAX_PAGES", MAX_PAGES),
      bound("CRE_BUILDOUT_NATIVE_CAPTURE_MAX_BYTES", MAX_BYTES));
  } catch {
    throw new BuildoutNativeCaptureError();
  }
}
