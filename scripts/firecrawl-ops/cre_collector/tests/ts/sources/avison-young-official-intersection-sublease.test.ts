import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import * as cheerio from "cheerio";
import { firecrawl } from "../../../lib/scrape.js";
import { prune } from "../../../lib/util.js";
import { assertAvisonYoungDetailDoc, avisonYoungBaseListing } from "../../../sources/avison-young.js";
import type { ScrapedDoc } from "../../../types.js";

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
const officialRows = new Map([...rows].map(([id, row]) => [id, { ...row, url: undefined }]));
const envKeys = [
  "CRE_REQUIRE_FRESH_DETAILS",
  "CRE_REQUIRE_FRESH_PROPERTY_DETAILS",
  "CRE_REFRESH_GENERATION",
  "CRE_REFRESH_STARTED_AT",
  "AVISON_YOUNG_DETAIL_TRANSPORT",
  "AVISON_YOUNG_DETAIL_LIMIT",
];
let moduleImportSequence = 0;
const freshStartedAt = "2026-10-20T00:00:00.000Z";

async function collect(
  id: string,
  websiteRows = [{ ...rows.get(id), url: undefined }],
  fresh = false,
  detailPage = pages.get(id)!,
) {
  const oldFetch = globalThis.fetch;
  const oldScrape = firecrawl.scrape;
  const previousEnv = new Map(envKeys.map((key) => [key, process.env[key]]));
  const detailUrls: string[] = [];
  const apiEntities: string[] = [];
  const scrapeOptions: Array<Record<string, unknown>> = [];
  try {
    for (const key of envKeys) delete process.env[key];
    if (fresh) {
      process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
      process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS = "1";
      process.env.CRE_REFRESH_GENERATION = "synthetic-4294-generation";
      process.env.CRE_REFRESH_STARTED_AT = freshStartedAt;
    }
    globalThis.fetch = async (input) => {
      const url = new URL(String(input));
      if (url.hostname === "www.avisonyoung.us") {
        return new Response("<script>SharpLaunch.PSE.create('0123456789abcdef0123456789abcdef')</script>", { status: 200 });
      }
      assert.equal(url.hostname, "pse-api.sharplaunch.com");
      const entity = url.searchParams.get("entity")!;
      apiEntities.push(entity);
      const items = entity === "website" ? websiteRows : [{ id: "synthetic-team-member" }];
      return new Response(JSON.stringify({ items }), { status: 200 });
    };
    firecrawl.scrape = async (...args: any[]) => {
      const [url, options] = args as [string, Record<string, unknown> | undefined];
      detailUrls.push(url);
      scrapeOptions.push(options ?? {});
      return { rawHtml: detailPage, markdown: "", links: [], metadata: { statusCode: 200, sourceURL: url, url } };
    };
    const source = await import(`../../../sources/avison-young.js?official-role-${id}-${moduleImportSequence++}`);
    const transaction = rows.get(id)!.transaction[0];
    return {
      result: await source.srcAvisonYoung(transaction, 1, false),
      detailUrls,
      apiEntities,
      scrapeOptions,
    };
  } finally {
    globalThis.fetch = oldFetch;
    firecrawl.scrape = oldScrape;
    for (const [key, value] of previousEnv) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

function stageAvisonListing(listing: Record<string, any>, transactionMode: string) {
  const stagedInput = {
    ...listing,
    sourceKey: "avison-young",
    transactionMode,
  };
  const result = spawnSync("python3", ["-c", [
    "import json,sys",
    "from cre_ingest import to_row",
    "row=to_row(json.load(sys.stdin),{},'2026-10-20T00:01:00Z')",
    "assert row is not None",
    "print(json.dumps(row,default=list))",
  ].join("; ")], {
    cwd: fileURLToPath(new URL("../../../", import.meta.url)),
    input: JSON.stringify(stagedInput),
    encoding: "utf8",
    timeout: 10_000,
    maxBuffer: 128 * 1024,
    env: { ...process.env, PYTHONPATH: "." },
  });
  assert.equal(result.error, undefined);
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

function assertRole(id: string, html: string, row = officialRows.get(id)!, owners = [id]) {
  const base = avisonYoungBaseListing(row, new Map());
  const url = row.external_url;
  const doc: ScrapedDoc = { rawHtml: html, markdown: "", links: [], metadata: { statusCode: 200, sourceURL: url, url } };
  return assertAvisonYoungDetailDoc(doc, url, base, { matchingExternalUrlRowIds: owners });
}

for (const id of ["18247", "17626"]) {
  test(`strict fresh ${id} official detail retains source identity through canonical staging`, async () => {
    const sourceRow = rows.get(id)!;
    const result = await collect(id, [{ ...sourceRow, url: undefined }], true);
    assert.deepEqual(result.result.listings.map((listing: any) => listing.id), [id]);
    assert.deepEqual(result.detailUrls, [sourceRow.external_url]);
    assert.equal(result.scrapeOptions.length, 1);
    assert.equal(result.scrapeOptions[0]?.maxAge, 0);
    const listing = result.result.listings[0]!;
    assert.equal(listing.detailError, undefined);
    assert.equal(listing.detailUnavailable, undefined);
    assert.equal(listing.preserveChildCollections, undefined);
    assert.ok(Date.parse(listing.detailObservedAt) >= Date.parse(freshStartedAt));
    assert.equal(listing.freshnessProvenance.detailScope, "detail_page");
    assert.equal(listing.freshnessProvenance.generationId, "synthetic-4294-generation");
    assert.equal(listing.freshnessProvenance.cacheDisposition, "live");
    assert.equal(listing.rawSharpLaunch.name, sourceRow.name);
    assert.equal(listing.rawSharpLaunch.address, sourceRow.address);

    const staged = stageAvisonListing(listing, sourceRow.transaction[0]);
    assert.equal(staged.external_id, String(id));
    assert.equal(staged.transaction_type, sourceRow.transaction[0]);
    assert.equal(staged.slug, "avison-young");
    assert.equal(staged.address, sourceRow.address.trim());
    assert.equal(staged.source_url, sourceRow.external_url);
    assert.equal(staged.raw_data.rawSharpLaunch.name, sourceRow.name);
    assert.equal(staged.raw_data.rawSharpLaunch.address, sourceRow.address);
    assert.equal(staged.raw_data.freshnessProvenance.generationId, "synthetic-4294-generation");
    assert.equal(staged.raw_data.detailJsonLd.url, sourceRow.external_url);
  });
}

for (const id of ["18247", "17626"]) {
  test(`official ${id} role remains bound without the incidental property-info box`, () => {
    const $ = cheerio.load(pages.get(id)!);
    $(".building__property-info-box").remove();
    assert.ok(assertRole(id, $.html()));
  });

  test(`official ${id} building family rejects a lost overview instead of falling through`, async () => {
    const $ = cheerio.load(pages.get(id)!);
    $(".building__overview-text").remove();
    assert.throws(() => assertRole(id, $.html()), /identity/);
    const result = await collect(id, [{ ...rows.get(id), url: undefined }], false, $.html());
    assert.match(result.result.listings[0]?.detailError ?? "", /identity/);
  });

  for (const mutation of ["wrong-og-url", "wrong-metadata-url", "missing-remote", "duplicate-remote", "duplicate-record"]) {
    test(`official ${id} role refuses shared-envelope mutation ${mutation}`, () => {
      const $ = cheerio.load(pages.get(id)!);
      const node = $('script[type="application/ld+json"]').first();
      const record = JSON.parse(node.text());
      if (mutation === "wrong-og-url") $('meta[property="og:url"]').attr("content", "https://www.avisonyoung.us/properties/other");
      if (mutation === "wrong-metadata-url") {
        const url = rows.get(id)!.external_url;
        assert.throws(() => assertAvisonYoungDetailDoc({
          rawHtml: $.html(), markdown: "", links: [],
          metadata: { statusCode: 200, sourceURL: url, url: "https://www.avisonyoung.us/properties/other" },
        }, url, avisonYoungBaseListing(officialRows.get(id)!, new Map()), { matchingExternalUrlRowIds: [id] }), /identity/);
        return;
      }
      if (mutation === "missing-remote") {
        $("script").filter((_index, script) => /\/\*\s*remote\s*:/.test($(script).text())).first().remove();
      }
      if (mutation === "duplicate-remote") {
        const remote = $("script").filter((_index, script) => /\/\*\s*remote\s*:/.test($(script).text())).first();
        remote.after(remote.clone());
      }
      if (mutation === "duplicate-record") $("head").append(node.clone());
      node.text(JSON.stringify(record));
      assert.throws(() => assertRole(id, $.html()), /identity/);
    });
  }
}

for (const id of ["18247", "17626"]) {
  test(`official source caller accepts the captured ${id} intersection/sublease page without changing feed identity`, async () => {
    const sourceRow = rows.get(id)!;
    assert.ok(assertRole(id, pages.get(id)!));
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

test("office feed names containing Sublease retain the established exact-named-H1 role", () => {
  const $ = cheerio.load(pages.get("17626")!);
  $("h1").text(rows.get("17626")!.name);
  assert.ok(assertRole("17626", $.html()));
});

test("official office sublease accepts consistent populated map claims as well as explicit absence", () => {
  const $ = cheerio.load(pages.get("17626")!);
  const mapScript = $("script").filter((_index, script) => $(script).text().includes("poi: []")).first();
  const before = mapScript.text();
  const changed = before.replace("poi: []", 'poi: [{"name":"Bank of America Building Sublease","address":"2600 W Big Beaver Road","lat":42.5631684,"lng":-83.1800816}]');
  assert.notEqual(changed, before);
  mapScript.text(changed);
  assert.ok(assertRole("17626", $.html()));
});

for (const mutation of ["reversed-roads", "extra-road", "wrong-h1", "wrong-typed-address", "wrong-city", "wrong-zip", "wrong-geo", "wrong-title", "wrong-title-separator", "wrong-record-name-separator", "wrong-point-name-separator", "wrong-canonical", "wrong-remote", "conflicting-poi", "malformed-poi-null", "malformed-poi-array", "duplicate-listing", "missing-owner", "duplicate-owner"]) {
  test(`official intersection role refuses ${mutation}`, () => {
    const html = pages.get("18247")!;
    const $ = cheerio.load(html);
    const node = $('script[type="application/ld+json"]').first();
    const record = JSON.parse(node.text());
    let owners = ["18247"];
    const row = { ...officialRows.get("18247")! };
    if (mutation === "reversed-roads") $("h1").text("FM 93 & Taylor Valley Rd");
    if (mutation === "extra-road") $("h1").text("Taylor Valley Rd & FM 93 & Other Rd");
    if (mutation === "wrong-h1") $("h1").text("Taylor Valley Rd and FM 93");
    if (mutation === "wrong-typed-address") record.propertyListing.address.streetAddress = "Taylor Valley Rd";
    if (mutation === "wrong-city") record.address.addressLocality = "Temple";
    if (mutation === "wrong-zip") record.propertyListing.address.postalCode = "76502";
    if (mutation === "wrong-geo") record.propertyListing.geo.latitude = 31.1;
    if (mutation === "wrong-title") $("title").text("Other Property");
    if (mutation === "wrong-title-separator") $('meta[property="og:title"]').attr("content", "Taylor Valley Rd / FM 93 | Land");
    if (mutation === "wrong-record-name-separator") record.name = "Taylor Valley Rd FM 93 | Land";
    if (mutation === "wrong-point-name-separator") {
      const poiScript = $("script").filter((_i, script) => $(script).text().includes("poi:")).first();
      const changed = poiScript.text().replace("Taylor Valley Rd & FM 93 | Land", "Taylor Valley Rd / FM 93 | Land");
      assert.notEqual(changed, poiScript.text());
      poiScript.text(changed);
    }
    if (mutation === "wrong-canonical") $('link[rel="canonical"]').attr("href", "https://www.avisonyoung.us/properties/other");
    if (mutation === "wrong-remote") $("script").filter((_i, script) => $(script).text().includes("remote:")).first().text("/* remote: 18248 */");
    if (mutation === "conflicting-poi") {
      const poiScript = $("script").filter((_i, script) => $(script).text().includes("poi:")).first();
      poiScript.text('var map={poi:[{"name":"Taylor Valley Rd & FM 93 | Land","address":"Taylor Valley Rd","lat":31.054209,"lng":-97.44573}]};');
    }
    if (mutation === "malformed-poi-null") {
      const poiScript = $("script").filter((_i, script) => $(script).text().includes("poi:")).first();
      poiScript.text(poiScript.text().replace("poi: [", "poi: nullish, poi: ["));
    }
    if (mutation === "malformed-poi-array") {
      const poiScript = $("script").filter((_i, script) => $(script).text().includes("poi: [")).first();
      const changed = poiScript.text().replace(/(poi:\s*\[[^\]]*\])/, "$1 trailing");
      assert.notEqual(changed, poiScript.text());
      poiScript.text(changed);
      assert.match($.html(), /poi:\s*\[[^\]]*\]\s+trailing/);
    }
    if (mutation === "duplicate-listing") $("head").append(node.clone());
    if (mutation === "missing-owner") owners = [];
    if (mutation === "duplicate-owner") owners = ["18247", "18248"];
    node.text(JSON.stringify(record));
    assert.throws(() => assertRole("18247", $.html(), row, owners), /identity/);
  });
}

for (const mutation of ["missing-all-zip", "malformed-matching-zip"]) {
  test(`official intersection role refuses ${mutation}`, () => {
    const $ = cheerio.load(pages.get("18247")!);
    const node = $('script[type="application/ld+json"]').first();
    const record = JSON.parse(node.text());
    const row = { ...officialRows.get("18247")! };
    const addresses = [record.address, record.propertyListing.address];
    if (mutation === "missing-all-zip") {
      row.zip = undefined;
      for (const address of addresses) delete address.postalCode;
    } else {
      row.zip = "7651";
      for (const address of addresses) address.postalCode = "7651";
    }
    node.text(JSON.stringify(record));
    assert.throws(() => assertRole("18247", $.html(), row), /identity/);
  });
}

for (const mutation of ["wrong-tenure", "wrong-heading", "wrong-city-hero", "missing-expiry", "expired-claim", "negated-expiry", "historical-expiry", "contradictory-claim", "wrong-address", "wrong-zip", "missing-owner", "duplicate-owner", "native-host", "malformed-poi-null", "malformed-poi-array", "conflicting-poi"]) {
  test(`official sublease role refuses ${mutation}`, () => {
    const html = pages.get("17626")!;
    const $ = cheerio.load(html);
    const node = $('script[type="application/ld+json"]').first();
    const record = JSON.parse(node.text());
    const row = { ...officialRows.get("17626")! };
    let owners = ["17626"];
    if (mutation === "wrong-tenure") row.transaction = ["sale"];
    if (mutation === "wrong-heading") $("h1").text("Sublease opportunity in Troy");
    if (mutation === "wrong-city-hero") $(".hero__sub-title").text("Detroit, Michigan");
    if (mutation === "missing-expiry") $(".building__overview-text").text("Class A office sublease available.");
    if (mutation === "expired-claim") $(".building__overview-text").text("Sublease expired July 31, 2029.");
    if (mutation === "negated-expiry") $(".building__overview-text").text("No sublease expires July 31, 2029.");
    if (mutation === "historical-expiry") $(".building__overview-text").text("Prior availability: Sublease expires July 31, 2029.");
    if (mutation === "contradictory-claim") $(".building__overview-text").text("• Sublease expires July 31, 2029 • The offering is withdrawn.");
    if (mutation === "wrong-address") record.address.streetAddress = "2601 W Big Beaver Road";
    if (mutation === "wrong-zip") record.propertyListing.address.postalCode = "48083";
    if (mutation === "missing-owner") owners = [];
    if (mutation === "duplicate-owner") owners = ["17626", "17627"];
    if (mutation === "native-host") row.external_url = "https://ayubankofamericabuilding.sharplaunch.com";
    if (mutation === "malformed-poi-null") {
      const poiScript = $("script").filter((_i, script) => $(script).text().includes("poi:")).first();
      poiScript.text(poiScript.text().replace("poi: null", "poi: nullish"));
    }
    if (mutation === "malformed-poi-array") {
      const poiScript = $("script").filter((_i, script) => $(script).text().includes("poi: []")).first();
      const changed = poiScript.text().replace("poi: []", "poi: [] trailing");
      assert.notEqual(changed, poiScript.text());
      poiScript.text(changed);
      assert.match($.html(), /poi:\s*\[\]\s+trailing/);
    }
    if (mutation === "conflicting-poi") {
      const poiScript = $("script").filter((_i, script) => $(script).text().includes("poi: []")).first();
      poiScript.text(poiScript.text().replace("poi: []", 'poi: [{"name":"Bank of America Building Sublease","address":"2601 W Big Beaver Road","lat":42.5631684,"lng":-83.1800816}]'));
    }
    node.text(JSON.stringify(record));
    assert.throws(() => assertRole("17626", $.html(), row, owners), /identity/);
  });
}
