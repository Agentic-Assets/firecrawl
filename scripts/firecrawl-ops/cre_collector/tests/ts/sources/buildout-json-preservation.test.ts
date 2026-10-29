import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { buildoutCache, buildoutFailureCache, buildoutInventory, fetchBuildoutInventoryPage,
  readBuildoutPageCache, srcBuildout, writeBuildoutPageCache } from "../../../sources/buildout.js";
import { firecrawl } from "../../../lib/scrape.js";
import { publishBuildoutParsedPage } from "../../../lib/source-evidence/buildout-page.js";

const [source,, plugin] = JSON.parse(readFileSync(new URL("../../../data/cre-buildout-sources.json", import.meta.url), "utf8"))[0];
const envKeys = ["BUILDOUT_CACHE_DIR", "CRE_REFRESH_GENERATION", "CRE_REQUIRE_FRESH_DETAILS", "BUILDOUT_USE_PAGE_CACHE",
  "CRE_BUILDOUT_NATIVE_CAPTURE_DIR", "CRE_BUILDOUT_NATIVE_CAPTURE_SOURCE_KEY", "CRE_BUILDOUT_NATIVE_CAPTURE_SINGLE_WRITER"];
async function fixture(fn: (dir: string) => Promise<void>) {
  const prior = Object.fromEntries(envKeys.map(k => [k, process.env[k]]));
  const previousFetch = globalThis.fetch, previousScrape = firecrawl.scrape;
  const dir = mkdtempSync(join(tmpdir(), "buildout-json-"));
  try {
    for (const k of envKeys) delete process.env[k];
    process.env.BUILDOUT_CACHE_DIR = dir; process.env.CRE_REFRESH_GENERATION = "fixture-generation";
    buildoutCache.clear(); buildoutFailureCache.clear();
    await fn(dir);
  } finally {
    globalThis.fetch = previousFetch; (firecrawl as any).scrape = previousScrape;
    for (const k of envKeys) { if (prior[k] === undefined) delete process.env[k]; else process.env[k] = prior[k]; }
    buildoutCache.clear(); buildoutFailureCache.clear(); rmSync(dir, { recursive: true, force: true });
  }
}
const body = '{"__creFreshness":{"source":"page"},"inventory":[{"id":1,"sale":true,"closed":false,"under_contract":false,"also_for_sale_or_lease":false,"show_link":"https://example.test/?propertyId=1-sale","__creFreshness":{"source":"row"},"zero":0}],"meta":{"total":1,"limit":30}}';
const opts = { preferDirectJson: true, cacheSlug: source, usePageCache: true };

test("page and row provider fields survive direct fetch, cache, and memory reuse", () => fixture(async () => {
  let requests = 0;
  globalThis.fetch = async () => { requests++; return new Response(body); };
  const page = await fetchBuildoutInventoryPage("Fixture", plugin, 0, opts);
  assert.deepEqual(page.__creFreshness, { source: "page" });
  assert.deepEqual(page.inventory[0].__creFreshness, { source: "row" });
  const cached = readBuildoutPageCache("Fixture", plugin, 0, opts);
  assert.deepEqual(cached.__creFreshness, { source: "page" });
  assert.deepEqual(cached.inventory[0].__creFreshness, { source: "row" });
  const inventory = await buildoutInventory("Fixture", plugin, opts);
  const repeated = await buildoutInventory("Fixture", plugin, opts);
  assert.equal(inventory, repeated);
  assert.deepEqual(inventory.items[0].__creFreshness, { source: "row" });
  const result = await srcBuildout("Fixture", plugin, "https://example.test/", "sale", Infinity, false, opts);
  assert.equal(result.listings[0].freshnessProvenance.generationId, "fixture-generation");
  assert.equal(result.listings[0].freshnessProvenance.cacheDisposition, "live");
  assert.ok(requests >= 1);
}));

test("publisher and cache refuse unsafe values and retain normal zero", () => fixture(async (dir) => {
  process.env.CRE_BUILDOUT_NATIVE_CAPTURE_DIR = dir;
  process.env.CRE_BUILDOUT_NATIVE_CAPTURE_SOURCE_KEY = source;
  process.env.CRE_BUILDOUT_NATIVE_CAPTURE_SINGLE_WRITER = "1";
  const base = { pluginKey: plugin, cacheSlug: source, page: 0, inventorySort: undefined,
    sourceObservedAt: null, cacheDisposition: "live" as const, transport: "direct_json" as const };
  for (const value of [JSON.parse("1e309"), JSON.parse("-0"), undefined]) {
    const data = { inventory: [{ value }] };
    assert.throws(() => publishBuildoutParsedPage({ ...base, data }), /capture failed/);
    assert.throws(() => writeBuildoutPageCache("Fixture", plugin, 0, opts, data), /cannot be preserved exactly/);
  }
  assert.deepEqual(readdirSync(dir), []);
  publishBuildoutParsedPage({ ...base, data: { inventory: [{ value: 0 }] } });
  assert.equal(readdirSync(dir).length, 1);
}));

test("direct unsafe parsed data does not trigger Firecrawl fallback", () => fixture(async () => {
  for (const literal of ["1e309", "-0"]) {
    let direct = 0, fallback = 0;
    globalThis.fetch = async () => { direct++; return new Response(`{"inventory":[{"value":${literal}}],"meta":{"total":1,"limit":30}}`); };
    (firecrawl as any).scrape = async () => { fallback++; throw new Error("unexpected"); };
    await assert.rejects(fetchBuildoutInventoryPage("Fixture", plugin, 0, opts), /cannot be preserved exactly/);
    assert.equal(direct, 1); assert.equal(fallback, 0);
  }
}));

test("Firecrawl parsed unsafe data stops before cache serialization", () => fixture(async (dir) => {
  let calls = 0;
  (firecrawl as any).scrape = async () => { calls++; return { rawHtml: '{"inventory":[{"value":-0}],"meta":{"total":1,"limit":30}}' }; };
  await assert.rejects(fetchBuildoutInventoryPage("Fixture", plugin, 0, { cacheSlug: source }), /cannot be preserved exactly/);
  assert.equal(calls, 1); assert.deepEqual(readdirSync(dir), []);
}));

test("representation error during recovery stops before another transport", () => fixture(async () => {
  const prior = process.env.CRE_SCRAPE_MAX_ATTEMPTS; process.env.CRE_SCRAPE_MAX_ATTEMPTS = "1";
  let directPageOne = 0, fallback = 0;
  globalThis.fetch = async (input) => {
    const page = Number(new URL(String(input)).searchParams.get("page"));
    if (page === 0) return new Response('{"inventory":[{"id":1}],"meta":{"total":2,"limit":1}}');
    directPageOne++;
    if (directPageOne === 1) return new Response("error", { status: 503 });
    return new Response('{"inventory":[{"id":2,"value":-0}],"meta":{"total":2,"limit":1}}');
  };
  (firecrawl as any).scrape = async () => { fallback++; throw new Error("synthetic transport error"); };
  try {
    await assert.rejects(buildoutInventory("Recovery", "synthetic-plugin", { ...opts, requireCompletePages: true,
      recoveryPasses: 1, recoveryCooldownMs: 0, jsonAttempts: 1, maxRecoveryPages: 1 }), /cannot be preserved exactly/);
    assert.equal(directPageOne, 2); assert.equal(fallback, 1);
  } finally { if (prior === undefined) delete process.env.CRE_SCRAPE_MAX_ATTEMPTS; else process.env.CRE_SCRAPE_MAX_ATTEMPTS = prior; }
}));

test("unsafe Firecrawl response refuses before strict shape failure or recovery", () => fixture(async () => {
  let calls = 0;
  (firecrawl as any).scrape = async () => { calls++; return { rawHtml: calls === 1
    ? '{"inventory":[{"id":1}],"meta":{"total":2,"limit":1}}'
    : '{"inventory":null,"value":-0,"meta":{"total":2,"limit":1}}' }; };
  await assert.rejects(buildoutInventory("Synthetic", "synthetic-plugin", { preferDirectJson: false,
    cacheSlug: "synthetic", requireCompletePages: true, recoveryPasses: 1, recoveryCooldownMs: 0 }),
    /cannot be preserved exactly/);
  assert.equal(calls, 2);
}));
