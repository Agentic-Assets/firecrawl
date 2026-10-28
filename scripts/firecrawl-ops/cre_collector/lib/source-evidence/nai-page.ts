import { createHash, randomBytes } from "node:crypto";
import { constants, lstatSync, opendirSync, openSync, closeSync, fchmodSync, fstatSync,
  writeFileSync, readFileSync, fsyncSync, linkSync, unlinkSync } from "node:fs";
import { join, resolve } from "node:path";

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
  let temp: string | undefined;
  let tempOwned = false;
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
    const target = join(dir, `page-${offset}-${hash(payload)}.json`);
    // All quota checks and publication below are synchronous, so concurrent
    // async callers in this worker cannot interleave a check and a write.
    let usedBytes = 0;
    let pageCount = 0;
    let targetExists = false;
    const entries = opendirSync(dir);
    try {
      for (let item = entries.readSync(); item !== null; item = entries.readSync()) {
        const name = item.name;
        // Unknown, temporary and unsafe entries are refused, not ignored.
        if (!/^page-(?:0|[1-9][0-9]*)-[a-f0-9]{64}\.json$/.test(name)) throw new Error();
        const file = lstatSync(join(dir, name));
        if (!file.isFile() || file.isSymbolicLink() || file.uid !== process.getuid() ||
            (file.mode & 0o777) !== 0o600 || file.nlink !== 1 || file.size > MAX_ENTRY_BYTES) throw new Error();
        usedBytes += file.size;
        pageCount++;
        if (!Number.isSafeInteger(usedBytes) || pageCount > maxPages || usedBytes > maxBytes) throw new Error();
        if (name === target.slice(dir.length + 1)) targetExists = true;
      }
    } finally { entries.closeSync(); }
    if (targetExists) {
      const fd = openSync(target, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      try {
        const before = fstatSync(fd);
        if (!before.isFile() || before.uid !== process.getuid() || (before.mode & 0o777) !== 0o600 || before.nlink !== 1 || before.size !== payload.length) throw new Error();
        const bytes = readFileSync(fd); const after = fstatSync(fd); const named = lstatSync(target);
        const identity = (value: typeof before) => [value.dev, value.ino, value.size, value.mtimeMs, value.ctimeMs, value.mode, value.uid, value.nlink].join(":");
        if (identity(before) !== identity(after) || identity(after) !== identity(named) || !bytes.equals(payload)) throw new Error();
      } finally { closeSync(fd); }
      return target;
    }
    if (pageCount >= maxPages || payload.length > maxBytes - usedBytes) throw new Error();
    const current = lstatSync(dir);
    if (!current.isDirectory() || current.dev !== stat.dev || current.ino !== stat.ino ||
        current.uid !== stat.uid || current.mode !== stat.mode) throw new Error();
    temp = join(dir, `.page-${randomBytes(16).toString("hex")}.tmp`);
    const fd = openSync(temp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
    tempOwned = true;
    try { fchmodSync(fd, 0o600); writeFileSync(fd, payload); fsyncSync(fd); } finally { closeSync(fd); }
    linkSync(temp, target);
    unlinkSync(temp); tempOwned = false; temp = undefined;
    const parent = openSync(dir, constants.O_RDONLY);
    try { fsyncSync(parent); } finally { closeSync(parent); }
    return target;
  } catch {
    if (temp && tempOwned) { try { unlinkSync(temp); } catch { /* retain only redacted failure */ } }
    throw new Error("NAI native capture failed");
  }
}
