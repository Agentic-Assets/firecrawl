/** Offline observations only. Output contains raw sensitive data, including base64 source bytes; it is not redacted. Never emits collector/admission artifacts. */
import { createHash } from "node:crypto";
import { constants, openSync, closeSync, writeFileSync, readSync, lstatSync, fstatSync, realpathSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { assertBuildoutInventoryPage, buildoutInventoryUrl, buildoutQueryFingerprint } from "./sources/buildout.js";
import { registeredBuildoutFirmFor } from "./sources/buildout-registry.js";
import type { SourceKey } from "./types.js";

const sha = (text: string | Buffer) => createHash("sha256").update(text).digest("hex");
const obj = (v: unknown): v is Record<string, any> => v !== null && typeof v === "object" && !Array.isArray(v);
const timestamp = (v: unknown) => {
  if (typeof v !== "string") return NaN;
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,3}))?Z$/.exec(v);
  if (!match) return NaN;
  const value = Date.parse(v);
  const canonical = `${match[1]}.${(match[2] ?? "").padEnd(3, "0")}Z`;
  return Number.isFinite(value) && new Date(value).toISOString() === canonical ? value : NaN;
};

export function auditBuildoutPage(raw: Buffer, sourceKey: string, generation: string, runStart: string, runEnd: string, now: string) {
  const firm = registeredBuildoutFirmFor(sourceKey as SourceKey);
  if (!firm) throw new Error("source is not a registered Buildout source");
  if (!generation.trim() || !Number.isFinite(timestamp(runStart)) || !Number.isFinite(timestamp(runEnd)) || !Number.isFinite(timestamp(now)) || timestamp(runStart) > timestamp(runEnd)) throw new Error("valid generation and ordered time window required");
  let cache: unknown;
  try { cache = JSON.parse(raw.toString("utf8")); } catch { throw new Error("cache is not valid JSON; original input remains untouched"); }
  if (!obj(cache) || !obj(cache.data) || !Array.isArray(cache.data.inventory)) throw new Error("cache has no parseable inventory array; preserve original input");
  const data = cache.data;
  const observation = obj(cache.observation) ? cache.observation : {};
  const page = cache.page;
  const reasons: string[] = [];
  const check = (ok: boolean, reason: string) => { if (!ok) reasons.push(reason); };
  check(cache.pluginKey === firm.pluginKey, "plugin_mismatch");
  check(Number.isSafeInteger(page) && page >= 0, "invalid_page");
  check(cache.queryFingerprint === buildoutQueryFingerprint(firm.inventoryOpts), "query_mismatch");
  check(cache.generationId === generation && observation.generationId === generation, "generation_missing_or_mismatch");
  check(typeof cache.cachedAt === "string" && cache.cachedAt === observation.observedAt, "observation_timestamp_mismatch");
  const observed = timestamp(observation.observedAt);
  check(Number.isFinite(observed) && observed >= timestamp(runStart) && observed <= timestamp(runEnd) && observed >= timestamp(now) - 24 * 3600_000 && observed <= timestamp(now) + 5 * 60_000, "observation_outside_window");
  check(observation.cacheDisposition === "live", "source_observation_not_live");
  check(observation.transport === "direct_json" || observation.transport === "firecrawl_json", "transport_invalid");
  check(Number.isSafeInteger(page) && page >= 0 && observation.endpoint === buildoutInventoryUrl(firm.pluginKey, page, firm.inventoryOpts.inventorySort), "endpoint_mismatch");
  let unsafeNumericValues = 0;
  const pending: Array<{ value: unknown; depth: number }> = [{ value: cache, depth: 0 }];
  let visited = 0;
  while (pending.length) {
    const { value, depth } = pending.pop()!;
    if (++visited > 1_000_000 || depth > 256) throw new Error("cache structure exceeds bounded evidence limits; original input remains untouched");
    if (typeof value === "number" && (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value)))) unsafeNumericValues++;
    else if (Array.isArray(value) || obj(value)) for (const key in value) {
      if (Object.hasOwn(value, key)) pending.push({ value: value[key as keyof typeof value], depth: depth + 1 });
      if (pending.length + visited > 1_000_000) throw new Error("cache structure exceeds bounded evidence limits; original input remains untouched");
    }
  }
  check(typeof observation.parsedJsonSha256 === "string" && observation.parsedJsonSha256 === sha(JSON.stringify(data)), "parsed_data_hash_mismatch");
  check(unsafeNumericValues === 0, "unsafe_numeric_values");
  try { assertBuildoutInventoryPage(data, page, null, true); } catch { reasons.push("page_shape_invalid"); }
  const identities = new Map<string, number>();
  const urls = new Map<string, number>();
  const items: Array<{ index: number; row: unknown; disposition: string; reasons: string[] }> = data.inventory.map((row: unknown, index: number) => {
    const issues: string[] = [];
    if (!obj(row)) return { index, row, disposition: "unresolved", reasons: ["row_not_object"] };
    const id = typeof row.id === "string" || typeof row.id === "number" ? String(row.id) : "";
    if (!/^[1-9][0-9]{0,15}$/.test(id) || !Number.isSafeInteger(Number(id))) issues.push("native_id_missing_or_invalid");
    else identities.set(id, (identities.get(id) ?? 0) + 1);
    if (typeof row.show_link !== "string") issues.push("listing_url_missing_or_invalid");
    else {
      try {
        const url = new URL(row.show_link);
        const host = new URL(firm.listingsPage).hostname.toLowerCase().replace(/^www\./, "");
        if (url.protocol !== "https:" || url.username || url.password || url.port || url.hostname.toLowerCase().replace(/^www\./, "") !== host) issues.push("listing_url_unowned_or_invalid");
        else urls.set(row.show_link, (urls.get(row.show_link) ?? 0) + 1);
      } catch { issues.push("listing_url_missing_or_invalid"); }
    }
    if (!["sale", "closed", "under_contract", "also_for_sale_or_lease"].every(k => typeof row[k] === "boolean")) issues.push("lifecycle_or_tenure_flags_missing_or_invalid");
    return { index, row, disposition: "observed", reasons: issues };
  });
  for (const item of items) {
    if (!obj(item.row)) continue;
    const id = String(item.row.id ?? "");
    if ((identities.get(id) ?? 0) > 1) item.reasons.push("duplicate_native_id");
    if (typeof item.row.show_link === "string" && (urls.get(item.row.show_link) ?? 0) > 1) item.reasons.push("duplicate_listing_url");
    if (item.reasons.length) item.disposition = "unresolved";
  }
  return { schema: "buildout-page-audit-v1", scope: "selected_page_only", authority: "none", publisher_authenticated: false, private_unreviewed: true, status: reasons.length ? "provenance_incomplete" : "parsed_json_consistent_fidelity_unverified", provenance_reasons: reasons, data_fidelity: { unsafeNumericValues, parsed_representation: "JavaScript JSON.parse; duplicate keys and decimal precision are not verified", parsed_data_hash: "SHA256 of JSON.stringify parsed data, not original bytes" }, sourceKey, company: firm.company, listingsPage: firm.listingsPage, cacheFileSha256: sha(raw), input_byte_authority: "originalCacheBytesBase64", originalCacheBytesBase64: raw.toString("base64"), generationExpected: generation, generationObserved: cache.generationId ?? null, callerWindow: { runStart, runEnd, now }, cachedAt: cache.cachedAt ?? null, page: page ?? null, declaredTotal: data.meta?.total ?? null, observedRows: items.length, coverage: "unknown", observation, unreviewedSourceWrapper: cache, rows: items };
}

export function serializePrivateAudit(result: unknown, limit = 40 * 1024 * 1024): string {
  const encoded = JSON.stringify(result, null, 2) + "\n";
  if (Buffer.byteLength(encoded) > limit) throw new Error("audit output exceeds the bounded evidence limit; original input remains untouched");
  return encoded;
}

/** The descriptor remains open throughout; metadata checks are not an adversarial immutability proof. */
export function readStableInput(fd: number, afterFirstChunk?: () => void): Buffer {
  const before = fstatSync(fd, { bigint: true });
  const limit = 16 * 1024 * 1024;
  if (!before.isFile() || before.size > BigInt(limit)) throw new Error("input must be a bounded regular file");
  const buffer = Buffer.alloc(limit + 1);
  let used = 0;
  while (used < buffer.length) {
    const count = readSync(fd, buffer, used, Math.min(64 * 1024, buffer.length - used), null);
    if (!count) break;
    used += count;
    if (afterFirstChunk) { const callback = afterFirstChunk; afterFirstChunk = undefined; callback(); }
  }
  if (used > limit) throw new Error("input exceeded the bounded evidence limit");
  const after = fstatSync(fd, { bigint: true });
  const keys = ["dev", "ino", "mode", "nlink", "uid", "gid", "size", "mtimeNs", "ctimeNs"] as const;
  if (BigInt(used) !== before.size || keys.some(key => before[key] !== after[key])) throw new Error("input changed during read; original input remains untouched");
  return buffer.subarray(0, used);
}

function cli() {
  const args = ["SOURCE", "INPUT", "OUTPUT", "GENERATION", "RUN_START", "RUN_END", "NOW"].map(k => process.env[`BUILDOUT_AUDIT_${k}`]);
  if (args.some(v => !v)) throw new Error("BUILDOUT_AUDIT_SOURCE, INPUT, OUTPUT, GENERATION, RUN_START, RUN_END and NOW are required");
  const [source, input, output, generation, start, end, now] = args as string[];
  const parent = resolve(dirname(output));
  const parentStat = lstatSync(parent);
  if (!parentStat.isDirectory() || parentStat.isSymbolicLink() || realpathSync(parent) !== parent || (parentStat.mode & 0o077) !== 0 || parentStat.uid !== process.getuid?.()) throw new Error("output parent must be an owned private real directory");
  for (let ancestor = dirname(parent); ; ancestor = dirname(ancestor)) {
    const st = lstatSync(ancestor);
    const trustedOwner = st.uid === 0 || st.uid === process.getuid?.();
    const writableByOthers = (st.mode & 0o022) !== 0;
    const rootSticky = st.uid === 0 && (st.mode & 0o1000) !== 0;
    if (!st.isDirectory() || st.isSymbolicLink() || !trustedOwner || (writableByOthers && !rootSticky)) throw new Error("output ancestors must be trusted directories");
    if (ancestor === dirname(ancestor)) break;
  }
  const inputFd = openSync(input, constants.O_RDONLY | constants.O_NOFOLLOW);
  let bytes: Buffer;
  try {
    bytes = readStableInput(inputFd);
  } finally { closeSync(inputFd); }
  const result = auditBuildoutPage(bytes, source, generation, start, end, now);
  const encoded = serializePrivateAudit(result);
  const fd = openSync(output, "wx", 0o600);
  try { writeFileSync(fd, encoded); } finally { closeSync(fd); }
  process.stdout.write(JSON.stringify({status: result.status, provenance_reasons: result.provenance_reasons, observedRows: result.observedRows, output}) + "\n");
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) cli();
