import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as cheerio from "cheerio";
import { firecrawl } from "../../../lib/scrape.js";
import { assertAvisonYoungDetailDoc, avisonYoungBaseListing, fetchAvisonYoungDirectDoc } from "../../../sources/avison-young.js";

process.argv = [process.argv[0]!, process.argv[1]!];

const fixtureDir = new URL("../../fixtures/raw_data/", import.meta.url);
const evidence = JSON.parse(readFileSync(
  new URL("avison-young-named-association-18071.json", fixtureDir), "utf8",
));
const html = readFileSync(
  new URL("avison-young-named-association-18071.html", fixtureDir), "utf8",
);
const base = avisonYoungBaseListing(evidence.feed_row, new Map());
const owner = { matchingExternalUrlRowIds: ["18071"] };
const doc = (rawHtml: string, url = evidence.response_url) => ({
  rawHtml,
  markdown: "",
  links: [],
  metadata: { statusCode: 200, sourceURL: url, url },
});
const check = (
  rawHtml = html,
  row = base,
  requestedUrl = evidence.response_url,
  owners: typeof owner | null = owner,
) =>
  assertAvisonYoungDetailDoc(doc(rawHtml, requestedUrl), requestedUrl, row, owners ?? undefined);

function withEndpoint(raw: string, endpoint: string): string {
  const $ = cheerio.load(raw);
  $("title").text(`${endpoint} El Dorado St`);
  $('meta[property="og:title"]').attr("content", `${endpoint} El Dorado St`);
  const node = $('script[type="application/ld+json"]').first();
  const record = JSON.parse(node.text());
  record.name = `${endpoint} El Dorado St`;
  node.text(JSON.stringify(record));
  return $.html()
    .replaceAll('"address":"1641 El Dorado St"', `"address":"${endpoint} El Dorado St"`)
    .replaceAll('"name":"1641 El Dorado St"', `"name":"${endpoint} El Dorado St"`);
}

test("registered owned range offering accepts one endpoint as a checked locator and preserves the full feed range", () => {
  const before = structuredClone(base);
  const result = check();
  assert.ok(result);
  assert.equal(base.street, "1563 - 1641 El Dorado St");
  assert.deepEqual(base, before);
});

test("same witnessed range permits either endpoint but not an interior locator", () => {
  const lower = withEndpoint(html, "1563");
  assert.doesNotThrow(() => check(lower));
  assert.throws(() => check(withEndpoint(html, "1600")), /identity/);
});

test("an additional POI is allowed only when it repeats the same witnessed endpoint and coordinates", () => {
  const extended = `${html}<script>var extraMap={poi:[{"lat":37.424636,"lng":-121.976756,"address":"1641 El Dorado St","name":"1641 El Dorado St"}]};</script>`;
  assert.doesNotThrow(() => check(extended));
});

for (const mutation of [
  "missing-offering-prose",
  "missing-h1",
  "endpoint-only-typed-address",
  "missing-second-typed-address",
  "reversed-feed-range",
  "wrong-city",
  "wrong-zip",
  "unit-range",
  "intersection-range",
  "extra-offered-range",
  "contradictory-poi",
  "malformed-poi",
  "wrong-geo",
  "inconsistent-title-endpoint",
  "wrong-canonical-url",
  "wrong-og-url",
  "wrong-remote-id",
  "competing-real-estate-listing",
  "missing-feed-owner",
  "duplicate-feed-owner",
]) {
  test(`range offering refuses ${mutation}`, () => {
    const $ = cheerio.load(html);
    const row = { ...base };
    const recordNode = $('script[type="application/ld+json"]');
    const record = JSON.parse(recordNode.text());
    if (mutation === "missing-offering-prose") {
      $(".building__overview-text p").first().text("A well-located investment opportunity in San Jose.");
      $('meta[property="og:description"]').attr("content", "A well-located investment opportunity in San Jose.");
      record.description = "A well-located investment opportunity in San Jose.";
    }
    if (mutation === "endpoint-only-typed-address") {
      record.address.streetAddress = "1641 El Dorado St";
      record.propertyListing.address.streetAddress = "1641 El Dorado St";
    }
    if (mutation === "missing-h1") $("h1").remove();
    if (mutation === "missing-second-typed-address") delete record.propertyListing.address;
    if (mutation === "reversed-feed-range") row.street = "1641 - 1563 El Dorado St";
    if (mutation === "unit-range") row.street = "1563 - 1641 El Dorado St, Suite 2";
    if (mutation === "intersection-range") row.street = "1563 - 1641 El Dorado St & Other Rd";
    if (mutation === "wrong-city") {
      row.city = "Oakland";
      record.address.addressLocality = "Oakland";
      record.propertyListing.address.addressLocality = "Oakland";
    }
    if (mutation === "wrong-zip") {
      record.address.postalCode = "95003";
      record.propertyListing.address.postalCode = "95003";
    }
    if (mutation === "extra-offered-range") {
      $(".building__overview-text").append("<p>Also offered: 10-12 Other Street.</p>");
    }
    if (mutation === "contradictory-poi") {
      $("body").append('<script>var extraMap={poi:[{"lat":37.424636,"lng":-121.976756,"address":"1600 El Dorado St","name":"1600 El Dorado St"}]};</script>');
    }
    if (mutation === "malformed-poi") {
      $("body").append("<script>var extraMap={poi:unknown};</script>");
    }
    if (mutation === "wrong-geo") record.propertyListing.geo.latitude = 37.5;
    if (mutation === "inconsistent-title-endpoint") $("title").text("1563 El Dorado St");
    if (mutation === "wrong-canonical-url") $('link[rel="canonical"]').attr("href", "https://www.avisonyoung.us/properties/other");
    if (mutation === "wrong-og-url") $('meta[property="og:url"]').attr("content", "https://www.avisonyoung.us/properties/other");
    if (mutation === "wrong-remote-id") $("body").html($("body").html()?.replace("remote: 18071", "remote: 18072") ?? "");
    if (mutation === "competing-real-estate-listing") {
      $("body").append(`<script type="application/ld+json">${JSON.stringify({ ...record, name: "Other Property" })}</script>`);
    }
    recordNode.text(JSON.stringify(record));
    const owners = mutation === "missing-feed-owner" ? null
      : mutation === "duplicate-feed-owner" ? { matchingExternalUrlRowIds: ["18071", "18072"] }
        : owner;
    assert.throws(() => check($.html(), row, evidence.response_url, owners), /identity/);
  });
}

test("real source caller stages the original range and retained provider witnesses", async () => {
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
  const feedRow = { ...evidence.feed_row, url: undefined };
  let websiteRows: any[] = [feedRow];
  let detailCalls = 0;
  try {
    for (const key of envKeys) delete process.env[key];
    globalThis.fetch = async (input) => {
      const url = new URL(String(input));
      if (url.hostname === "www.avisonyoung.us") {
        return new Response("<script>SharpLaunch.PSE.create('0123456789abcdef0123456789abcdef')</script>");
      }
      assert.equal(url.hostname, "pse-api.sharplaunch.com");
      return new Response(JSON.stringify({ items: url.searchParams.get("entity") === "website" ? websiteRows : [] }));
    };
    firecrawl.scrape = async (url: string) => {
      detailCalls++;
      return { ...doc(html, url), markdown: "The properties located at 1563-1641 El Dorado Street." };
    };
    const callerModulePath: string = "../../../sources/avison-young.js?range-offering-caller=unique";
    const source = await import(callerModulePath);
    const result = await source.srcAvisonYoung("sale", 1, false);
    assert.equal(detailCalls, 1);
    assert.deepEqual(result.listings.map((listing: any) => listing.id), ["18071"]);
    const listing = result.listings[0]!;
    assert.equal(listing.detailError, undefined);
    assert.equal(listing.street, "1563 - 1641 El Dorado St");
    assert.equal(listing.rawSharpLaunch.address, evidence.feed_row.address);
    assert.equal(listing.detailJsonLd.address.streetAddress, evidence.feed_row.address);
    assert.deepEqual(listing.detailJsonLd.propertyListing.geo, { "@type": "GeoCoordinates", latitude: 37.424636, longitude: -121.976756 });
    assert.equal(listing.externalUrl, evidence.response_url);
    assert.match(listing.markdown, /properties located at 1563-1641 El Dorado Street/i);

    // URL ownership is checked against the complete feed before active/transaction filtering.
    websiteRows = [feedRow, { ...feedRow, id: 18072, status: "inactive" }];
    const ownerModulePath: string = "../../../sources/avison-young.js?range-offering-owner=unique";
    const duplicateOwnerSource = await import(ownerModulePath);
    const duplicateOwnerResult = await duplicateOwnerSource.srcAvisonYoung("sale", 1, false);
    assert.match(duplicateOwnerResult.listings[0]!.detailError ?? "", /identity/);
  } finally {
    globalThis.fetch = oldFetch;
    firecrawl.scrape = oldScrape;
    for (const [key, value] of previousEnv) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});
