import { NativeEvidenceTerminalError } from "./terminal-error.js";
import { createHash } from "node:crypto";
import { lstatSync } from "node:fs";
import { resolve } from "node:path";
import { publishPrivatePage } from "./private-page.js";

export const NAI_CAPTURE_MAX_BODY_BYTES = 16 * 1024 * 1024;
export const NAI_CAPTURE_MAX_REQUEST_BYTES = 64 * 1024;
export const NAI_CAPTURE_MAX_PAGES = 128;
export const NAI_CAPTURE_MAX_ATTEMPT_BYTES = 128 * 1024 * 1024;
const MAX_ENTRY_BYTES = Math.ceil(NAI_CAPTURE_MAX_BODY_BYTES * 4 / 3) + NAI_CAPTURE_MAX_REQUEST_BYTES * 6 + 16_384;
const hash = (bytes: Uint8Array) => createHash("sha256").update(bytes).digest("hex");

export type NaiNativePage = {
  requestUrl: string;
  finalUrl: string;
  status: number;
  startedAt: string;
  finishedAt: string;
  requestBody: string;
  responseBody: Uint8Array;
};

/** Atomically publish one immutable private page. Inputs are source assertions. */
export async function publishNaiNativePage(directory: string, page: NaiNativePage): Promise<string> {
  try {
    const dir = resolve(directory);
    const stat = lstatSync(dir);
    if (!stat.isDirectory() || stat.isSymbolicLink() || typeof process.getuid !== "function" ||
        stat.uid !== process.getuid() || (stat.mode & 0o077) !== 0) throw new Error();
    // This assertion comes from the owning coordinator. No cross-process lock
    // is created: shared attempt directories are unsupported and must not be used.
    if (process.env.CRE_NAI_NATIVE_CAPTURE_SINGLE_WRITER !== "1") throw new Error();
    const limit = (name: string, fallback: number) => {
      const value = process.env[name];
      if (value === undefined) return fallback;
      if (!/^[1-9][0-9]*$/.test(value)) throw new Error();
      const number = Number(value);
      if (!Number.isSafeInteger(number) || number > fallback) throw new Error();
      return number;
    };
    const maxPages = limit("CRE_NAI_NATIVE_CAPTURE_MAX_PAGES", NAI_CAPTURE_MAX_PAGES);
    const maxBytes = limit("CRE_NAI_NATIVE_CAPTURE_MAX_BYTES", NAI_CAPTURE_MAX_ATTEMPT_BYTES);
    const requestBytes = Buffer.from(page.requestBody, "utf8");
    if (requestBytes.length > NAI_CAPTURE_MAX_REQUEST_BYTES || page.responseBody.byteLength > NAI_CAPTURE_MAX_BODY_BYTES) throw new Error();
    const request = JSON.parse(page.requestBody);
    const offset = request?.variables?.offset;
    const filter = request?.variables?.filter;
    const keys = (value: unknown, expected: string[]) => !!value && typeof value === "object" && !Array.isArray(value) &&
      Object.keys(value).sort().join("\0") === expected.sort().join("\0");
    if (!keys(request, ["operationName", "query", "variables"]) || !keys(request.variables, ["offset", "limit", "filter"]) ||
        !keys(filter, ["contentTypesIds", "sourcesIds", "locationsIds", "title"]) ||
        request.operationName !== "GET_LISTINGS_PUBLIC_POSTS" || !Number.isSafeInteger(offset) || offset < 0 ||
        typeof request.query !== "string" || !Number.isSafeInteger(request.variables.limit) ||
        request.variables.limit < 1 || request.variables.limit > 100 ||
        JSON.stringify(filter.contentTypesIds) !== "[4,10]" || !Array.isArray(filter.locationsIds) || filter.locationsIds.length !== 0 || filter.title !== "" ||
        !Array.isArray(filter.sourcesIds) || filter.sourcesIds.length < 1 || filter.sourcesIds.length > 40 ||
        filter.sourcesIds.some((id: unknown) => !Number.isSafeInteger(id) || (id as number) <= 0) ||
        new Set(filter.sourcesIds).size !== filter.sourcesIds.length) throw new Error();
    const requestUrl = new URL(page.requestUrl); const finalUrl = new URL(page.finalUrl);
    for (const url of [requestUrl, finalUrl]) {
      if (url.protocol !== "https:" || url.username || url.password || url.hash || url.search) throw new Error();
    }
    const validTime = (s: string) => /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/.test(s) &&
      Number.isFinite(Date.parse(s)) && new Date(s).toISOString() === s;
    if (!validTime(page.startedAt) || !validTime(page.finishedAt) || page.startedAt > page.finishedAt ||
        !Number.isInteger(page.status) || page.status < 100 || page.status > 599) throw new Error();
    const requestSha256 = hash(requestBytes);
    const payload = Buffer.from(JSON.stringify({
      schema: "cre-nai-public-posts-page/v1",
      receipt: { sourceKey: "nai-global", requestUrl: page.requestUrl, finalUrl: page.finalUrl, method: "POST",
        status: page.status, startedAt: page.startedAt, finishedAt: page.finishedAt,
        bodySha256: hash(page.responseBody), bodyBytes: page.responseBody.byteLength },
      requestBody: page.requestBody, requestBodySha256: requestSha256, requestBodyBytes: requestBytes.length,
      responseBodyBase64: Buffer.from(page.responseBody).toString("base64"),
      provenance: { bodyHashKind: "fetch_delivered_body_bytes_not_wire", identity: "unverified", visibility: "unverified", completeness: "unverified" },
    }) + "\n", "utf8");
    if (payload.length > MAX_ENTRY_BYTES) throw new Error();
    return publishPrivatePage(dir, `page-${offset}-${hash(payload)}.json`, payload,
      /^page-(?:0|[1-9][0-9]*)-[a-f0-9]{64}\.json$/, MAX_ENTRY_BYTES, maxPages, maxBytes);
  } catch {
    throw new NativeEvidenceTerminalError("NAI native capture failed");
  }
}
