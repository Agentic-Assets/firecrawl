import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { chmod, mkdtemp, readFile, readdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { assertBuildoutCaptureScope, publishBuildoutParsedPage } from "../../../lib/source-evidence/buildout-page.js";
import { fetchBuildoutInventoryPage } from "../../../sources/buildout.js";

const tuples = JSON.parse(await readFile(new URL("../../../data/cre-buildout-sources.json", import.meta.url), "utf8"));
const [sourceKey, , pluginKey] = tuples[0];
const envKeys = ["CRE_BUILDOUT_NATIVE_CAPTURE_DIR", "CRE_BUILDOUT_NATIVE_CAPTURE_SOURCE_KEY",
  "CRE_BUILDOUT_NATIVE_CAPTURE_SINGLE_WRITER", "CRE_BUILDOUT_NATIVE_CAPTURE_MAX_PAGES",
  "CRE_BUILDOUT_NATIVE_CAPTURE_MAX_BYTES"] as const;
function configure(directory: string, t: any) {
  const old = envKeys.map((key) => process.env[key]);
  t.after(() => envKeys.forEach((key, index) => {
    if (old[index] === undefined) delete process.env[key]; else process.env[key] = old[index];
  }));
  process.env.CRE_BUILDOUT_NATIVE_CAPTURE_DIR = directory;
  process.env.CRE_BUILDOUT_NATIVE_CAPTURE_SOURCE_KEY = sourceKey;
  process.env.CRE_BUILDOUT_NATIVE_CAPTURE_SINGLE_WRITER = "1";
  delete process.env.CRE_BUILDOUT_NATIVE_CAPTURE_MAX_PAGES;
  delete process.env.CRE_BUILDOUT_NATIVE_CAPTURE_MAX_BYTES;
}

test("retains parsed rows, duplicates, nulls and cache metadata without plugin key", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "buildout-capture-"));
  configure(directory, t);
  const data: any = { meta: { total: 2 }, inventory: [{ id: 1, other: [false, null, []] }, { id: 1 }] };
  Object.defineProperty(data, "__creFreshness", { value: { observedAt: "2026-10-28T11:00:00Z" }, enumerable: false });
  publishBuildoutParsedPage({ pluginKey, cacheSlug: sourceKey, page: 0, inventorySort: undefined,
    data, sourceObservedAt: "2026-10-28T11:00:00Z", cacheDisposition: "generation_cache", transport: null });
  const names = await readdir(directory);
  assert.equal(names.length, 1);
  const bytes = await readFile(join(directory, names[0]!));
  const entry = JSON.parse(bytes.toString("utf8"));
  assert.deepEqual(JSON.parse(Buffer.from(entry.responseBodyBase64, "base64").toString("utf8")), data);
  assert.equal(entry.receipt.sourceObservedAt, "2026-10-28T11:00:00Z");
  assert.equal(entry.receipt.cacheDisposition, "generation_cache");
  assert.equal(entry.receipt.transport, null);
  assert.equal(entry.receipt.providerScopeSha256,
    createHash("sha256").update(JSON.stringify([sourceKey, pluginKey, null])).digest("hex"));
  assert.equal(entry.provenance.representation, "collector_parsed_json_not_response_bytes");
  assert.ok(!bytes.toString("utf8").includes(pluginKey));
  assert.ok(!names[0]!.includes(pluginKey));
});

test("strict validation failure still leaves a page captured before validation", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "buildout-invalid-"));
  configure(directory, t);
  const oldFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = oldFetch; });
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return new Response(JSON.stringify({ meta: { total: 2, limit: 30 }, inventory: [{ id: null, unknown: false }] }));
  };
  await assert.rejects(fetchBuildoutInventoryPage("Synthetic", pluginKey, 0,
    { cacheSlug: sourceKey, preferDirectJson: true, requireCompletePages: true }), /expected 2 inventory rows/);
  assert.equal(calls, 1);
  const names = await readdir(directory);
  assert.equal(names.length, 1);
  const entry = JSON.parse(await readFile(join(directory, names[0]!), "utf8"));
  assert.equal(JSON.parse(Buffer.from(entry.responseBodyBase64, "base64").toString("utf8")).inventory[0].unknown, false);
});

test("unsupported source, unsafe directory and quota refuse without exposing coordinates", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "buildout-refuse-"));
  configure(directory, t);
  assert.throws(() => assertBuildoutCaptureScope(pluginKey, "svn"), /Buildout native capture failed/);
  process.env.CRE_BUILDOUT_NATIVE_CAPTURE_MAX_PAGES = "1";
  const page = { pluginKey, cacheSlug: sourceKey, page: 0, inventorySort: undefined, data: null,
    sourceObservedAt: null, cacheDisposition: "live" as const, transport: null };
  publishBuildoutParsedPage(page);
  assert.throws(() => publishBuildoutParsedPage({ ...page, page: 1 }), /Buildout native capture failed/);
  assert.equal((await readdir(directory)).length, 1);
  const unsafe = await mkdtemp(join(tmpdir(), "buildout-unsafe-"));
  await chmod(unsafe, 0o755);
  process.env.CRE_BUILDOUT_NATIVE_CAPTURE_DIR = unsafe;
  assert.throws(() => publishBuildoutParsedPage(page), (error: unknown) => {
    assert.equal((error as Error).message, "Buildout native capture failed");
    assert.ok(!(error as Error).message.includes(pluginKey));
    return true;
  });
});

test("publisher failure does not trigger Firecrawl fallback", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "buildout-publish-failure-"));
  configure(directory, t);
  await chmod(directory, 0o755);
  const oldFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = oldFetch; });
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return new Response(JSON.stringify({ meta: { total: 1, limit: 30 }, inventory: [{ id: 1 }] }));
  };
  await assert.rejects(fetchBuildoutInventoryPage("Synthetic", pluginKey, 0,
    { cacheSlug: sourceKey, preferDirectJson: true, requireCompletePages: true }), /Buildout native capture failed/);
  assert.equal(calls, 1);
});

test("successful direct primitive retains actual transport even without freshness annotation", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "buildout-primitive-"));
  configure(directory, t);
  const oldFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = oldFetch; });
  globalThis.fetch = async () => new Response("42");
  assert.equal(await fetchBuildoutInventoryPage("Synthetic", pluginKey, 0,
    { cacheSlug: sourceKey, preferDirectJson: true }), 42);
  const names = await readdir(directory);
  const entry = JSON.parse(await readFile(join(directory, names[0]!), "utf8"));
  assert.equal(entry.receipt.transport, "direct_json");
  assert.equal(entry.receipt.sourceObservedAt, null);
  assert.equal(JSON.parse(Buffer.from(entry.responseBodyBase64, "base64").toString("utf8")), 42);
});

test("unrecognized sort and invalid or future source clocks refuse", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "buildout-clock-"));
  configure(directory, t);
  const page = { pluginKey, cacheSlug: sourceKey, page: 0, inventorySort: undefined, data: {},
    sourceObservedAt: null as string | null, cacheDisposition: "generation_cache" as const, transport: null };
  assert.throws(() => publishBuildoutParsedPage({ ...page, inventorySort: "other" }), /capture failed/);
  assert.throws(() => publishBuildoutParsedPage({ ...page, sourceObservedAt: "not a timestamp" }), /capture failed/);
  assert.throws(() => publishBuildoutParsedPage({ ...page, sourceObservedAt: "9999-12-31T23:59:59Z" }), /capture failed/);
  assert.deepEqual(await readdir(directory), []);
});
