import test from "node:test";
import assert from "node:assert/strict";

// No fallback transport may escape the synthetic provider fixture.
globalThis.fetch = async () => { throw new Error("unexpected network transport"); };
const { srcBuildout, buildoutCache, buildoutFailureCache } = await import("../../../sources/buildout.js");
const { firecrawl } = await import("../../../lib/scrape.js");

const row = (id: number) => ({ id, sale: true, display_name: `Property ${id}`,
  show_link: `https://broker.example/property/${id}` });
const page = (inventory: unknown, total: unknown = 4, limit: unknown = 2) => ({ inventory, meta: { total, limit } });

for (const [name, pages, retained] of [
  ["short page", [page([row(1)]), page([row(3), row(4)])], ["1", "3", "4"]],
  ["changed metadata", [page([row(1), row(2)]), page([row(3), row(4)], 5)], ["1", "2", "3", "4"]],
  ["missing inventory", [page([row(1), row(2)]), page(null)], ["1", "2"]],
  ["invalid first total", [page([row(1), row(2)], "4")], ["1", "2"]],
  ["zero first limit", [page([row(1), row(2)], 4, 0)], ["1", "2"]],
  ["duplicate identity", [page([row(1), row(2)]), page([row(2), row(4)])], ["1", "2", "2", "4"]],
  ["missing identity", [page([row(1), { ...row(2), id: undefined }]), page([row(3), row(4)])], ["1", null, "3", "4"]],
] as const) {
  test(`non-strict ${name} retains readable rows and reports incomplete coverage`, async () => {
    const original = firecrawl.scrape;
    const jitter = process.env.BUILDOUT_PAGE_JITTER_MS;
    const strict = process.env.CRE_REQUIRE_FRESH_DETAILS;
    const requests: number[] = [];
    try {
      process.env.BUILDOUT_PAGE_JITTER_MS = "0";
      delete process.env.CRE_REQUIRE_FRESH_DETAILS;
      (firecrawl as any).scrape = async (url: string) => {
        const index = Number(new URL(url).searchParams.get("page"));
        requests.push(index);
        assert.ok(index < pages.length, "only fixture pages may be requested");
        return { rawHtml: JSON.stringify(pages[index]) };
      };
      const opts = { pageConcurrency: 1, jsonAttempts: 1 };
      const result = await srcBuildout("Fixture", `shape-${name}`, "https://broker.example/", "sale", Infinity, true, opts);
      assert.deepEqual(result.listings.map((listing) => listing.id), retained);
      assert.equal(result.truncated, true);
      assert.equal(result.currentInventoryProof, undefined);
      assert.deepEqual(requests, pages.length === 1 ? [0] : [0, 1]);
      buildoutCache.clear();
      await assert.rejects(srcBuildout("Fixture", `strict-${name}`, "https://broker.example/", "sale", Infinity, true,
        { ...opts, requireCompletePages: true }));
    } finally {
      (firecrawl as any).scrape = original;
      buildoutCache.clear();
      buildoutFailureCache.clear();
      if (jitter === undefined) delete process.env.BUILDOUT_PAGE_JITTER_MS; else process.env.BUILDOUT_PAGE_JITTER_MS = jitter;
      if (strict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS; else process.env.CRE_REQUIRE_FRESH_DETAILS = strict;
    }
  });
}
