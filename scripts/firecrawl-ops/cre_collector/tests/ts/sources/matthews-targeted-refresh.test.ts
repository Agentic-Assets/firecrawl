process.argv = [process.argv[0]!, process.argv[1]!];
import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import { groupEnrichItems, resolveEnricher, runEnrichGroups } from "../../../lib/enrich.js";
import { firecrawl } from "../../../lib/scrape.js";
import { MATTHEWS_FETCH_TIMEOUT_MS } from "../../../sources/matthews.js";

const sale = "https://www.matthews.com/properties/target-sale";
const lease = "https://www.matthews.com/properties/leasing-target";
const sitemapUrl = "https://www.matthews.com/sitemap.xml";
const page = (url: string) => `<html><head><link rel="canonical" href="${url}"></head><body><h1 id="propertyTitle">Synthetic Property</h1><div id="propertyAddress">123 Main St, Tulsa, OK 74103</div></body></html>`;
const sitemap = (urls: string[]) => `<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${urls.map(url => `<url><loc>${url}</loc></url>`).join("")}</urlset>`;
const seed = (url = sale, externalId = "target-sale", transaction?: "sale" | "lease") => ({ sourceKey: "matthews", externalId, url, ...(transaction ? { transaction } : {}), claimId: "synthetic-witness", claimedAt: "2001-01-01T00:00:00Z", inventoryObservedAt: "2001-01-01T00:00:00Z" });
const context = () => ({ sourceKey: "matthews", generationId: "matthews-targeted-0123456789abcdef0123456789abcdef", generationStartedAt: new Date(Date.now() - 1000).toISOString() });

async function setup(t: TestContext, header: any, inventory: string = sitemap([sale, lease]), detail = page, httpStatus = 200) {
  const env = Object.fromEntries(["CRE_REFRESH_GENERATION", "CRE_REFRESH_STARTED_AT", "CRE_REQUIRE_FRESH_DETAILS"].map(key => [key, process.env[key]]));
  process.env.CRE_REFRESH_GENERATION = header.generationId;
  process.env.CRE_REFRESH_STARTED_AT = header.generationStartedAt;
  process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
  const oldFetch = globalThis.fetch, oldTimeout = globalThis.setTimeout, oldScrape = firecrawl.scrape;
  const requested: string[] = [];
  const received: Record<string, number> = {};
  let generic = 0;
  (firecrawl as any).scrape = async () => { generic++; throw new Error("Forbidden fallback"); };
  globalThis.setTimeout = ((callback: (...args: any[]) => void, delay?: number, ...args: any[]) => oldTimeout(callback, delay === MATTHEWS_FETCH_TIMEOUT_MS ? 2 : 0, ...args)) as typeof setTimeout;
  globalThis.fetch = async (input, options) => {
    const url = String(input); requested.push(url);
    assert.equal(options?.cache, "no-store");
    assert.equal(options?.redirect, "manual");
    received[url] = Date.now();
    if (url === sitemapUrl) return new Response(inventory);
    return new Response(httpStatus === 200 ? detail(url) : null, { status: httpStatus, headers: httpStatus === 308 ? { location: sale } : undefined });
  };
  t.after(() => {
    globalThis.fetch = oldFetch; globalThis.setTimeout = oldTimeout; (firecrawl as any).scrape = oldScrape;
    for (const [key, value] of Object.entries(env)) value === undefined ? delete process.env[key] : process.env[key] = value;
    assert.equal(generic, 0);
  });
  return { requested, received };
}

async function run(items: any[], header: any) {
  return (runEnrichGroups as any)(groupEnrichItems(items), resolveEnricher, () => "Matthews", header);
}

test("bound sale and legacy unknown lease share one fresh inventory response and emit actual separate clocks", async t => {
  const header = context();
  const evidence = await setup(t, header);
  const input = [seed(sale, "target-sale", "sale"), seed(lease, "leasing-target")];
  const result = await run(input, header);
  assert.equal(evidence.requested.filter(url => url === sitemapUrl).length, 1);
  assert.equal(result.listings.length, 2);
  assert.equal(result.sources[0].requested, 2);
  assert.equal(result.sources[0].listingsCollected, 2);
  for (const row of result.listings) {
    assert.equal(row.id, row.url === sale ? "target-sale" : "leasing-target");
    assert.equal(row.transactionMode, row.url === sale ? "sale" : "lease");
    assert.equal(row.transactionType, row.url === sale ? "Sale" : "Lease");
    assert.equal(row.canonicalUrl, row.url);
    assert.equal(row.freshnessProvenance.generationId, header.generationId);
    assert.equal(row.freshnessProvenance.detailScope, "detail_page");
    assert.equal(row.freshnessProvenance.cacheDisposition, "live");
    assert.ok(Date.parse(row.inventoryObservedAt) >= evidence.received[sitemapUrl]);
    assert.ok(Date.parse(row.detailObservedAt) >= evidence.received[row.url]);
    assert.ok(Date.parse(row.detailObservedAt) >= Date.parse(row.inventoryObservedAt));
    assert.notEqual(row.inventoryObservedAt, input[0].inventoryObservedAt);
    assert.equal(Object.hasOwn(row, "claimId"), false);
    assert.equal(Object.hasOwn(row, "claimedAt"), false);
  }
});

for (const [name, items, inventory] of [
  ["missing member", [seed()], sitemap([lease])],
  ["duplicate requested native ID", [seed(), seed()], sitemap([sale])],
  ["duplicate requested normalized URL", [seed(), seed(sale + "/")], sitemap([sale])],
  ["ambiguous selected sitemap member", [seed()], sitemap([sale, sale + "/"])],
  ["explicit tenure mismatch", [seed(lease, "leasing-target", "sale")], sitemap([lease])],
  ["wrong native ID", [seed(sale, "different-id")], sitemap([sale])],
  ["foreign URL", [seed("https://elsewhere.invalid/properties/target-sale")], sitemap([sale])],
  ["HTML masquerading as sitemap", [seed()], `<html><a href="${sale}">Property</a></html>`],
  ["malformed sitemap shape", [seed()], `<urlset><url><loc>${sale}</loc></url>`],
  ["missing url closing tag", [seed()], `<urlset><url><loc>${sale}</loc></urlset>`],
  ["missing loc closing tag", [seed()], `<urlset><url><loc>${sale}</url></urlset>`],
  ["nested error structure", [seed()], `<urlset><url><error><html>Access denied</html></error><loc>${sale}</loc></url></urlset>`],
  ["embedded error structure", [seed()], `<urlset><error><html>Access denied</html></error><url><loc>${sale}</loc></url></urlset>`],
  ["raw loc credentials", [seed()], sitemap([sale.replace("https://", "https://user:password@")])],
  ["raw loc query", [seed()], sitemap([sale + "?wrong=1"])],
  ["raw loc fragment", [seed()], sitemap([sale + "#wrong"])],
  ["raw loc port", [seed()], sitemap([sale.replace(".com/", ".com:444/")])],
  ["duplicate loc in one inventory entry", [seed()], `<urlset><url><loc>${sale}</loc><loc>${lease}</loc></url></urlset>`],
] as const) {
  test(`targeted batch refuses ${name} without detail dispatch`, async t => {
    const header = context(); const evidence = await setup(t, header, inventory);
    let result: any;
    try { result = await run([...items], header); } catch { result = { listings: [] }; }
    assert.deepEqual(result.listings, []);
    assert.ok(evidence.requested.every(url => url === sitemapUrl));
  });
}

for (const mutation of ["wrong-env-generation", "wrong-env-start", "not-strict", "stale-start", "future-start", "invalid-start", "missing-generation", "invalid-generation", "extra-header-field", "wrong-source", "mixed-source", "empty", "over-batch"] as const) {
  test(`targeted context ${mutation} refuses before any transport`, async t => {
    const header: any = context(); const evidence = await setup(t, header);
    let items: any[] = [seed()];
    if (mutation === "wrong-env-generation") process.env.CRE_REFRESH_GENERATION = "old-generation";
    if (mutation === "wrong-env-start") process.env.CRE_REFRESH_STARTED_AT = "2020-01-01T00:00:00Z";
    if (mutation === "not-strict") delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    if (mutation === "stale-start") header.generationStartedAt = process.env.CRE_REFRESH_STARTED_AT = new Date(Date.now() - 601_000).toISOString();
    if (mutation === "future-start") header.generationStartedAt = process.env.CRE_REFRESH_STARTED_AT = new Date(Date.now() + 60_000).toISOString();
    if (mutation === "invalid-start") header.generationStartedAt = process.env.CRE_REFRESH_STARTED_AT = "not-a-time";
    if (mutation === "missing-generation") delete header.generationId;
    if (mutation === "invalid-generation") header.generationId = process.env.CRE_REFRESH_GENERATION = "synthetic-non-worker-generation";
    if (mutation === "extra-header-field") header.mode = "full";
    if (mutation === "wrong-source") header.sourceKey = "srs";
    if (mutation === "mixed-source") items.push({ ...seed(), sourceKey: "srs" });
    if (mutation === "empty") items = [];
    if (mutation === "over-batch") items = Array.from({ length: 51 }, (_, i) => seed(`https://www.matthews.com/properties/id-${i}`, `id-${i}`));
    await assert.rejects(() => run(items, header));
    assert.deepEqual(evidence.requested, []);
  });
}

for (const [name, detail, status] of [
  ["canonical mismatch", () => page(sale + "-other"), 200],
  ["captcha", () => page(sale).replace("Synthetic Property", "Access denied"), 200],
  ["permanent redirect alias", page, 308],
  ["not found", page, 404],
] as const) {
  test(`bound ${name} does not emit fresh detail or falsely increase counts`, async t => {
    const header = context(); const evidence = await setup(t, header, sitemap([sale]), detail, status);
    const result = await run([seed()], header);
    assert.deepEqual(result.listings, []);
    assert.equal(result.sources[0].requested, 1);
    assert.equal(result.sources[0].listingsCollected, 0);
    assert.equal(evidence.requested.filter(url => url === sitemapUrl).length, 1);
  });
}

test("ordinary enrich with full-generation env still never invents inventory proof", async t => {
  const header = context(); const evidence = await setup(t, header);
  const result = await (runEnrichGroups as any)(groupEnrichItems([seed(sale, "target-sale", "sale")]), resolveEnricher, () => "Matthews");
  assert.equal(result.listings.length, 1);
  assert.equal(evidence.requested.includes(sitemapUrl), false);
  assert.equal(Object.hasOwn(result.listings[0], "inventoryObservedAt"), false);
  assert.equal(Object.hasOwn(result.listings[0], "freshnessProvenance"), false);
});

for (const [name, inventory, leaseTransaction] of [
  ["missing", sitemap([sale]), undefined],
  ["ambiguous", sitemap([sale, lease, lease + "/"]), undefined],
  ["tenure mismatch", sitemap([sale, lease]), "sale"],
] as const) {
  test(`valid batch with one ${name} member preserves its other authoritative result`, async t => {
    const header = context(); const evidence = await setup(t, header, inventory);
    const result = await run([seed(sale, "target-sale", "sale"), seed(lease, "leasing-target", leaseTransaction)], header);
    assert.deepEqual(result.listings.map((row: any) => row.id), ["target-sale"]);
    assert.equal(result.sources[0].requested, 2);
    assert.equal(result.sources[0].listingsCollected, 1);
    assert.equal(result.sources[0].error, undefined);
    assert.deepEqual(evidence.requested, [sitemapUrl, sale]);
  });
}

test("targeted fresh sitemap preserves valid optional leaf metadata", async t => {
  const header = context();
  const evidence = await setup(t, header, `<urlset><url><loc>${sale}</loc><lastmod>2026-10-14</lastmod><changefreq>daily</changefreq><priority>0.8</priority></url></urlset>`);
  const result = await run([seed(sale, "target-sale", "sale")], header);
  assert.equal(result.listings.length, 1);
  assert.deepEqual(evidence.requested, [sitemapUrl, sale]);
});
