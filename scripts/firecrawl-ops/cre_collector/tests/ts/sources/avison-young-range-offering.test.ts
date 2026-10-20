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
const check = (rawHtml = html, row = base, requestedUrl = evidence.response_url, owners = owner) =>
  assertAvisonYoungDetailDoc(doc(rawHtml, requestedUrl), requestedUrl, row, owners);

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
  assert.equal(result, assertAvisonYoungDetailDoc(doc(html), evidence.response_url, base, owner));
  assert.equal(base.street, "1563 - 1641 El Dorado St");
  assert.deepEqual(base, before);
});

test("same witnessed range permits either endpoint but not an interior locator", () => {
  const lower = withEndpoint(html, "1563");
  assert.doesNotThrow(() => check(lower));
  assert.throws(() => check(withEndpoint(html, "1600")), /identity/);
});

for (const mutation of [
  "missing-offering-prose",
  "endpoint-only-typed-address",
  "reversed-feed-range",
  "wrong-city",
  "wrong-zip",
  "extra-offered-range",
  "contradictory-poi",
  "malformed-poi",
  "wrong-geo",
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
    if (mutation === "reversed-feed-range") row.street = "1641 - 1563 El Dorado St";
    if (mutation === "wrong-city") {
      row.city = "Oakland";
      record.address.addressLocality = "Oakland";
      record.propertyListing.address.addressLocality = "Oakland";
    }
    if (mutation === "wrong-zip") {
      row.postalCode = "95003";
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
    recordNode.text(JSON.stringify(record));
    const owners = mutation === "missing-feed-owner" ? undefined
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
  let detailCalls = 0;
  try {
    for (const key of envKeys) delete process.env[key];
    globalThis.fetch = async (input) => {
      const url = new URL(String(input));
      if (url.hostname === "www.avisonyoung.us") {
        return new Response("<script>SharpLaunch.PSE.create('0123456789abcdef0123456789abcdef')</script>");
      }
      assert.equal(url.hostname, "pse-api.sharplaunch.com");
      return new Response(JSON.stringify({ items: url.searchParams.get("entity") === "website" ? [feedRow] : [] }));
    };
    firecrawl.scrape = async (url: string) => {
      detailCalls++;
      return { ...doc(html, url), markdown: "The properties located at 1563-1641 El Dorado Street." };
    };
    const source = await import("../../../sources/avison-young.js?range-offering-caller=unique");
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
  } finally {
    globalThis.fetch = oldFetch;
    firecrawl.scrape = oldScrape;
    for (const [key, value] of previousEnv) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});
