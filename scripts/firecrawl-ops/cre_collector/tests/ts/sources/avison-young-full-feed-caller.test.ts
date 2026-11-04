import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { firecrawl } from "../../../lib/scrape.js";

process.argv = [process.argv[0]!, process.argv[1]!];

const fixtureDir = new URL("../../fixtures/raw_data/", import.meta.url);
const evidence = JSON.parse(readFileSync(
  new URL("avison-young-official-display-role-18194.json", fixtureDir),
  "utf8",
));
const detailHtml = readFileSync(
  new URL("avison-young-official-display-role-18194.html", fixtureDir),
  "utf8",
);
const selectedSaleRow = evidence.feed_row;

async function collectFromWebsiteRows(rows: any[], cacheKey: string) {
  const source = await import(`../../../sources/avison-young.js?feed-owner-case=${cacheKey}`);
  const oldFetch = globalThis.fetch;
  const oldScrape = firecrawl.scrape;
  const envKeys = [
    "CRE_REQUIRE_FRESH_DETAILS",
    "CRE_REQUIRE_FRESH_PROPERTY_DETAILS",
    "CRE_REFRESH_GENERATION",
    "AVISON_YOUNG_DETAIL_TRANSPORT",
    "AVISON_YOUNG_DETAIL_LIMIT",
  ];
  const previousEnv = new Map(envKeys.map((key) => [key, process.env[key]]));
  let requestCount = 0;
  let detailCalls = 0;

  try {
    for (const key of envKeys) delete process.env[key];
    globalThis.fetch = async (input) => {
      requestCount += 1;
      const url = new URL(String(input));
      if (url.hostname === "www.avisonyoung.us") {
        return new Response(
          "<script>SharpLaunch.PSE.create('0123456789abcdef0123456789abcdef')</script>",
          { status: 200 },
        );
      }
      assert.equal(url.hostname, "pse-api.sharplaunch.com");
      const entity = url.searchParams.get("entity");
      if (entity === "website") {
        return new Response(JSON.stringify({ items: rows }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      assert.equal(entity, "team_member");
      return new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    };
    firecrawl.scrape = async (url: string) => {
      detailCalls += 1;
      return {
        rawHtml: detailHtml,
        markdown: "",
        links: [],
        metadata: { sourceURL: url, url, statusCode: 200 },
      };
    };

    const result = await source.srcAvisonYoung("sale", 1, false);
    return { result, requestCount, detailCalls };
  } finally {
    globalThis.fetch = oldFetch;
    firecrawl.scrape = oldScrape;
    for (const [key, value] of previousEnv) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

test("srcAvisonYoung indexes official URL owners before inventory filters", async () => {
  const unique = await collectFromWebsiteRows([selectedSaleRow], "unique");
  assert.equal(unique.requestCount, 3);
  assert.equal(unique.detailCalls, 1);
  assert.deepEqual(unique.result.listings.map((row: any) => row.id), ["18194"]);
  assert.equal(unique.result.listings[0]?.detailError, undefined);

  for (const [label, duplicate] of [
    ["inactive", { ...selectedSaleRow, id: 18195, status: "inactive" }],
    ["other-transaction", { ...selectedSaleRow, id: 18196, transaction: ["lease"] }],
  ] as const) {
    const collision = await collectFromWebsiteRows([selectedSaleRow, duplicate], label);
    assert.equal(collision.requestCount, 3, label);
    assert.equal(collision.detailCalls, 1, label);
    assert.deepEqual(collision.result.listings.map((row: any) => row.id), ["18194"], label);
    assert.match(collision.result.listings[0]?.detailError ?? "", /identity does not match/, label);
  }

  const sameIdCopy = await collectFromWebsiteRows([
    selectedSaleRow,
    { ...selectedSaleRow, transaction: ["lease"] },
  ], "same-id");
  assert.equal(sameIdCopy.requestCount, 3);
  assert.equal(sameIdCopy.detailCalls, 1);
  assert.deepEqual(sameIdCopy.result.listings.map((row: any) => row.id), ["18194"]);
  assert.equal(sameIdCopy.result.listings[0]?.detailError, undefined);
});
