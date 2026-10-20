import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { firecrawl } from "../../../lib/scrape.js";

process.argv = [process.argv[0]!, process.argv[1]!];

const load = (name: string) => readFileSync(new URL(`../../fixtures/raw_data/${name}`, import.meta.url), "utf8");
const rows = new Map([
  ["18247", JSON.parse(load("avison-4294-feed-18247.json"))],
  ["17626", JSON.parse(load("avison-4294-feed-17626.json"))],
]);
const pages = new Map([
  ["18247", load("avison-4294-official-18247.html")],
  ["17626", load("avison-4294-official-17626.html")],
]);
const envKeys = [
  "CRE_REQUIRE_FRESH_DETAILS",
  "CRE_REQUIRE_FRESH_PROPERTY_DETAILS",
  "CRE_REFRESH_GENERATION",
  "AVISON_YOUNG_DETAIL_TRANSPORT",
  "AVISON_YOUNG_DETAIL_LIMIT",
];

async function collect(id: string, websiteRows = [{ ...rows.get(id), url: undefined }]) {
  const oldFetch = globalThis.fetch;
  const oldScrape = firecrawl.scrape;
  const previousEnv = new Map(envKeys.map((key) => [key, process.env[key]]));
  const detailUrls: string[] = [];
  const apiEntities: string[] = [];
  try {
    for (const key of envKeys) delete process.env[key];
    globalThis.fetch = async (input) => {
      const url = new URL(String(input));
      if (url.hostname === "www.avisonyoung.us") {
        return new Response("<script>SharpLaunch.PSE.create('0123456789abcdef0123456789abcdef')</script>", { status: 200 });
      }
      assert.equal(url.hostname, "pse-api.sharplaunch.com");
      const entity = url.searchParams.get("entity")!;
      apiEntities.push(entity);
      return new Response(JSON.stringify({ items: entity === "website" ? websiteRows : [] }), { status: 200 });
    };
    firecrawl.scrape = async (url: string) => {
      detailUrls.push(url);
      return { rawHtml: pages.get(id)!, markdown: "", links: [], metadata: { statusCode: 200, sourceURL: url, url } };
    };
    const source = await import(`../../../sources/avison-young.js?official-role-${id}-${Math.random()}`);
    const transaction = rows.get(id)!.transaction[0];
    return { result: await source.srcAvisonYoung(transaction, 1, false), detailUrls, apiEntities };
  } finally {
    globalThis.fetch = oldFetch;
    firecrawl.scrape = oldScrape;
    for (const [key, value] of previousEnv) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

for (const id of ["18247", "17626"]) {
  test(`official source caller accepts the captured ${id} intersection/sublease page without changing feed identity`, async () => {
    const sourceRow = rows.get(id)!;
    const result = await collect(id);
    assert.deepEqual(result.apiEntities.sort(), ["team_member", "website"]);
    assert.deepEqual(result.detailUrls, [sourceRow.external_url]);
    assert.deepEqual(result.result.listings.map((listing: any) => listing.id), [id]);
    const listing = result.result.listings[0]!;
    assert.equal(listing.detailError, undefined);
    assert.equal(listing.rawSharpLaunch.name, sourceRow.name);
    assert.equal(listing.rawSharpLaunch.address, sourceRow.address);
    assert.equal(listing.externalUrl, sourceRow.external_url);
    assert.equal(listing.detailJsonLd.url, sourceRow.external_url);
    assert.equal(listing.detailJsonLd.name, id === "18247" ? "Taylor Valley Rd &amp; FM 93 | Land" : sourceRow.name);
  });
}

test("official intersection role still refuses a duplicate feed URL owner before tenure filtering", async () => {
  const row = { ...rows.get("18247"), url: undefined };
  const duplicate = { ...row, id: 18248, status: "inactive", transaction: ["lease"] };
  const result = await collect("18247", [row, duplicate]);
  assert.match(result.result.listings[0]?.detailError ?? "", /identity/);
});
