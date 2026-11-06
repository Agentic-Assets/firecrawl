import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { firecrawl } from "../../../lib/scrape.js";

const fixtures = new URL("../../fixtures/raw_data/", import.meta.url);
const row = JSON.parse(readFileSync(new URL("avison-young-official-display-role-18194.json", fixtures), "utf8")).feed_row;
const html = readFileSync(new URL("avison-young-official-display-role-18194.html", fixtures), "utf8");
const badUrl = "https://www.avisonyoung.us/properties/unrelated-sale";
const bad = { ...row, id: 17000, order_id: 17000, url: badUrl, external_url: badUrl };

async function runCase(denied: boolean) {
  const keys = ["CRE_REQUIRE_FRESH_DETAILS", "CRE_REQUIRE_FRESH_PROPERTY_DETAILS", "CRE_REFRESH_GENERATION",
    "AVISON_YOUNG_DETAIL_TRANSPORT", "AVISON_YOUNG_DETAIL_CONCURRENCY"];
  const previous = new Map(keys.map(key => [key, process.env[key]]));
  const originalFetch = globalThis.fetch;
  const originalScrape = firecrawl.scrape;
  const observed: any[] = [];
  let outcomes: any[] = [];
  const calls: string[] = [];
  try {
    delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
    process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS = "1";
    process.env.CRE_REFRESH_GENERATION = "partial-test";
    process.env.AVISON_YOUNG_DETAIL_CONCURRENCY = "1";
    globalThis.fetch = async input => {
      const url = new URL(String(input));
      if (url.hostname === "www.avisonyoung.us") return new Response("SharpLaunch.PSE.create('0123456789abcdef0123456789abcdef')");
      return new Response(JSON.stringify({ items: url.searchParams.get("entity") === "website" ? [bad, row] : [] }),
        { status: 200, headers: { "content-type": "application/json" } });
    };
    firecrawl.scrape = async (url: string) => {
      calls.push(url);
      return { rawHtml: html, markdown: "", links: [],
        metadata: { sourceURL: url, url, statusCode: denied && url === badUrl ? 403 : 200 } };
    };
    const source = await import(`../../../sources/avison-young.js?isolation=${denied}`);
    let failure: unknown;
    try {
      await source.srcAvisonYoung("sale", 100, false,
        listing => observed.push(listing), result => { outcomes = result; });
    } catch (error) { failure = error; }
    return { source, failure, observed, outcomes, calls };
  } finally {
    globalThis.fetch = originalFetch;
    firecrawl.scrape = originalScrape;
    for (const [key, value] of previous) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  }
}

test("identity mismatch holds the row and retains a later validated peer", async () => {
  const { source, failure, observed, outcomes } = await runCase(false);
  assert.ok(failure instanceof source.AvisonYoungPartialDetailError);
  assert.deepEqual((failure as InstanceType<typeof source.AvisonYoungPartialDetailError>).heldIds, ["17000"]);
  assert.deepEqual(observed.map(row => row.id), ["18194"]);
  assert.deepEqual(outcomes.map(row => [row.id, row.state]), [["17000", "failed"], ["18194", "validated"]]);
});

test("access denial stops before requesting a later peer", async () => {
  const { source, failure, observed, calls } = await runCase(true);
  assert.ok(failure instanceof source.AvisonYoungAccessDeniedError);
  assert.deepEqual(observed, []);
  assert.deepEqual(calls, [badUrl]);
});
