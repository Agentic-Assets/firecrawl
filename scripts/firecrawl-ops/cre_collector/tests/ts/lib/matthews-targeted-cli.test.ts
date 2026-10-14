process.argv = [process.argv[0]!, process.argv[1]!];
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const collector = fileURLToPath(new URL("../../../", import.meta.url));
const sale = "https://www.matthews.com/properties/target-sale";
const lease = "https://www.matthews.com/properties/leasing-target";
function invoke(options: { optIn?: boolean; header?: boolean; mutation?: string } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "matthews-targeted-cli-"));
  const header: any = { sourceKey: "matthews", generationId: "matthews-targeted-0123456789abcdef0123456789abcdef", generationStartedAt: new Date(Date.now() - 1000).toISOString() };
  const payload: any = { items: [{ sourceKey: "matthews", externalId: "target-sale", url: sale, transaction: "sale", claimId: "private-witness" }, { sourceKey: "matthews", externalId: "leasing-target", url: lease }], ...(options.header === false ? {} : { matthewsTargetedRefresh: header }) };
  const env = { ...process.env, CRE_REFRESH_GENERATION: header.generationId, CRE_REFRESH_STARTED_AT: header.generationStartedAt, CRE_REQUIRE_FRESH_DETAILS: "1" };
  if (options.mutation === "mixed") payload.items.push({ sourceKey: "srs", externalId: "other", url: sale });
  if (options.mutation === "env-generation") env.CRE_REFRESH_GENERATION = "unbound-generation";
  if (options.mutation === "env-start") env.CRE_REFRESH_STARTED_AT = "2020-01-01T00:00:00Z";
  const inventoryUrls = options.mutation === "one-absent" ? [sale] : options.mutation === "all-absent" ? ["https://www.matthews.com/properties/not-requested"] : [sale, lease];
  const input = join(dir, "claims.json"), out = join(dir, "output.json"), calls = join(dir, "calls.jsonl"), preload = join(dir, "preload.mts");
  writeFileSync(input, JSON.stringify(payload));
  writeFileSync(preload, `
import { appendFileSync } from "node:fs";
import { MATTHEWS_FETCH_TIMEOUT_MS } from ${JSON.stringify(new URL("../../../sources/matthews.ts", import.meta.url).href)};
const oldTimeout = globalThis.setTimeout;
globalThis.setTimeout = ((callback, delay, ...args) => oldTimeout(callback, delay === MATTHEWS_FETCH_TIMEOUT_MS ? 2 : 0, ...args));
globalThis.fetch = async (input, options) => {
  const url = String(input);
  appendFileSync(${JSON.stringify(calls)}, JSON.stringify({ url, redirect: options?.redirect, cache: options?.cache }) + "\\n");
  if (url === "https://www.matthews.com/sitemap.xml") return new Response(${JSON.stringify(`<urlset>${inventoryUrls.map(url => `<url><loc>${url}</loc></url>`).join("")}</urlset>`)});
  if (![${JSON.stringify(sale)}, ${JSON.stringify(lease)}].includes(url)) throw new Error("Unexpected mocked endpoint");
  return new Response('<html><head><link rel="canonical" href="' + url + '"></head><body><h1 id="propertyTitle">Synthetic Property</h1><div id="propertyAddress">123 Main St, Tulsa, OK 74103</div></body></html>');
};
`);
  const args = ["--import", "tsx", "--import", preload, "collect.ts", "--source", "matthews", "--enrich-input", input, "--out", out, "--concurrency", "1", ...(options.optIn === false ? [] : ["--matthews-targeted-refresh"])];
  const child = spawnSync(process.execPath, args, { cwd: collector, env, encoding: "utf8", timeout: 15_000, maxBuffer: 256 * 1024 });
  const result = { child, payload, data: existsSync(out) ? JSON.parse(readFileSync(out, "utf8")) : null, calls: existsSync(calls) ? readFileSync(calls, "utf8").trim().split("\n").filter(Boolean).map(row => JSON.parse(row)) : [] };
  rmSync(dir, { recursive: true, force: true });
  return result;
}

test("real bound collect CLI publishes truthful sale/lease partial envelope and one inventory observation", () => {
  const result = invoke();
  assert.equal(result.child.error, undefined);
  assert.equal(result.child.status, 0, result.child.stderr);
  const data = result.data;
  assert.equal(data.runMeta.mode, "enrich");
  assert.deepEqual(data.runMeta.matthewsTargetedRefresh, result.payload.matthewsTargetedRefresh);
  assert.equal(data.runMeta.freshness.generationId, result.payload.matthewsTargetedRefresh.generationId);
  assert.equal(data.runMeta.freshness.generationStartedAt, result.payload.matthewsTargetedRefresh.generationStartedAt);
  assert.equal(data.runMeta.freshness.requireFreshDetails, true);
  assert.equal(Object.hasOwn(data.runMeta, "maxItemsPerSource"), false);
  assert.equal(Object.hasOwn(data.runMeta, "transactions"), false);
  assert.equal(data.totalListings, 2);
  assert.equal(data.sources[0].requested, 2);
  assert.equal(data.sources[0].listingsCollected, 2);
  assert.equal(result.calls.filter((r: any) => r.url.endsWith("/sitemap.xml")).length, 1);
  assert.deepEqual(data.listings.map((row: any) => row.transactionMode), ["sale", "lease"]);
  for (const row of data.listings) {
    assert.equal(row.freshnessProvenance.generationId, result.payload.matthewsTargetedRefresh.generationId);
    assert.ok(Date.parse(row.inventoryObservedAt) >= Date.parse(result.payload.matthewsTargetedRefresh.generationStartedAt));
    assert.ok(Date.parse(row.detailObservedAt) >= Date.parse(row.inventoryObservedAt));
    assert.equal(Object.hasOwn(row, "claimId"), false);
  }
});

for (const options of [{ header: false }, { optIn: false }, { mutation: "mixed" }, { mutation: "env-generation" }, { mutation: "env-start" }]) {
  test(`real collect refuses unbound targeted flag/header/source context ${JSON.stringify(options)} before transport/output`, () => {
    const result = invoke(options);
    assert.notEqual(result.child.status, 0);
    assert.match(result.child.stderr, /Matthews targeted/);
    assert.deepEqual(result.calls, []);
    assert.equal(result.data, null);
  });
}

test("ordinary real enrich CLI keeps plain output despite inherited full-generation env", () => {
  const result = invoke({ header: false, optIn: false });
  assert.equal(result.child.status, 0, result.child.stderr);
  assert.equal(result.data.runMeta.mode, "enrich");
  assert.equal(Object.hasOwn(result.data.runMeta, "freshness"), false);
  assert.equal(Object.hasOwn(result.data.runMeta, "matthewsTargetedRefresh"), false);
  assert.equal(result.data.listings.length, 1);
  assert.equal(result.calls.some((r: any) => r.url.endsWith("/sitemap.xml")), false);
  assert.equal(Object.hasOwn(result.data.listings[0], "inventoryObservedAt"), false);
});

test("config accepts only the explicit targeted boolean and retains unknown flag refusal", () => {
  const accepted = spawnSync(process.execPath, ["--import", "tsx", "--input-type=module", "-e", "import {flags} from './lib/config.ts'; console.log(JSON.stringify(flags));", "--", "--matthews-targeted-refresh"], { cwd: collector, encoding: "utf8", timeout: 10_000 });
  assert.equal(accepted.status, 0, accepted.stderr);
  assert.equal(JSON.parse(accepted.stdout)["matthews-targeted-refresh"], true);
  const rejected = spawnSync(process.execPath, ["--import", "tsx", "--input-type=module", "-e", "import './lib/config.ts';", "--", "--matthews-targeted-unknown"], { cwd: collector, encoding: "utf8", timeout: 10_000 });
  assert.notEqual(rejected.status, 0);
  assert.match(rejected.stderr, /Unknown option/);
});

for (const [mutation, expected] of [["one-absent", 1], ["all-absent", 0]] as const) {
  test(`real targeted CLI ${mutation} omits only absent claims with honest partial counts`, () => {
    const result = invoke({ mutation });
    assert.equal(result.child.status, 0, result.child.stderr);
    assert.equal(result.data.runMeta.mode, "enrich");
    assert.deepEqual(result.data.runMeta.matthewsTargetedRefresh, result.payload.matthewsTargetedRefresh);
    assert.equal(result.data.sources[0].requested, 2);
    assert.equal(result.data.sources[0].listingsCollected, expected);
    assert.equal(result.data.sources[0].error, undefined);
    assert.equal(result.data.listings.length, expected);
    assert.equal(result.calls.length, 1 + expected);
    assert.equal(result.calls.filter((r: any) => r.url.endsWith("/sitemap.xml")).length, 1);
    if (expected) assert.equal(result.data.listings[0].id, "target-sale");
  });
}
