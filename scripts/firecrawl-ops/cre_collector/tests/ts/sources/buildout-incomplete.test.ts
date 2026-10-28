import test from "node:test";
import assert from "node:assert/strict";

// Deny any direct transport before importing the producer. The Firecrawl
// method below is replaced with an offline fixture before each invocation.
globalThis.fetch = async () => { throw new Error("unexpected network transport"); };
const { srcBuildout, buildoutCache, buildoutFailureCache } = await import("../../../sources/buildout.js");
const { firecrawl } = await import("../../../lib/scrape.js");

test("gappy non-strict inventory retains rows and marks both monitor transactions truncated", async () => {
  const original = firecrawl.scrape;
  const requested: number[] = [];
  let missingPage = true;
  const savedJitter = process.env.BUILDOUT_PAGE_JITTER_MS;
  const savedStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  try {
    delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    process.env.BUILDOUT_PAGE_JITTER_MS = "0";
    (firecrawl as any).scrape = async (url: string) => {
      const page = Number(new URL(url).searchParams.get("page"));
      requested.push(page);
      if (page === 1 && missingPage) return { rawHtml: "not json" };
      const row = { id: page + 1, sale: page === 0, display_name: `Property ${page}`,
        show_link: `https://broker.example/property/${page}` };
      return { rawHtml: JSON.stringify({ inventory: [row], meta: { total: 3, limit: 1 } }) };
    };
    const opts = { jsonAttempts: 1, pageConcurrency: 1 };
    const sale = await srcBuildout("Fixture", "partial-test", "https://broker.example/", "sale", Infinity, true, opts);
    const lease = await srcBuildout("Fixture", "partial-test", "https://broker.example/", "lease", Infinity, true, opts);
    assert.deepEqual(requested, [0, 1, 2]);
    assert.deepEqual(sale.listings.map((row) => row.id), ["1"]);
    assert.deepEqual(lease.listings.map((row) => row.id), ["3"]);
    assert.equal(sale.truncated, true);
    assert.equal(lease.truncated, true);
    assert.equal(sale.currentInventoryProof, undefined);
    buildoutCache.clear();
    await assert.rejects(
      srcBuildout("Fixture", "strict-test", "https://broker.example/", "sale", Infinity, true,
        { ...opts, requireCompletePages: true }),
      /inventory pages failed/
    );
    buildoutCache.clear();
    missingPage = false;
    const complete = await srcBuildout("Fixture", "complete-test", "https://broker.example/", "sale", Infinity, true, opts);
    assert.equal(complete.truncated, false);
  } finally {
    (firecrawl as any).scrape = original;
    buildoutCache.clear();
    buildoutFailureCache.clear();
    if (savedJitter === undefined) delete process.env.BUILDOUT_PAGE_JITTER_MS; else process.env.BUILDOUT_PAGE_JITTER_MS = savedJitter;
    if (savedStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS; else process.env.CRE_REQUIRE_FRESH_DETAILS = savedStrict;
  }
});
