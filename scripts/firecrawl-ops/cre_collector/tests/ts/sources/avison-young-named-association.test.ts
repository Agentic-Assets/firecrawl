import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as cheerio from "cheerio";
import { firecrawl } from "../../../lib/scrape.js";
import { assertAvisonYoungDetailDoc, avisonYoungBaseListing, enrichAvisonYoungListing,
  indexAvisonYoungOfficialFeedUrls, avisonYoungOfficialUrlKey, fetchAvisonYoungDirectDoc } from "../../../sources/avison-young.js";

process.argv = [process.argv[0]!, process.argv[1]!];
const dir = new URL("../../fixtures/raw_data/", import.meta.url);
const fixture = (id: number) => ({
  ...JSON.parse(readFileSync(new URL(`avison-young-named-association-${id}.json`, dir), "utf8")),
  html: readFileSync(new URL(`avison-young-named-association-${id}.html`, dir), "utf8"),
});
const named = fixture(17440);
const base = avisonYoungBaseListing(named.feed_row, new Map());
const context = { matchingExternalUrlRowIds: ["17440"] };
const doc = (html: string, url = named.response_url) => ({ rawHtml: html, markdown: "", links: [],
  metadata: { statusCode: 200, sourceURL: url, url } });
const check = (html = named.html, row = base, owners: typeof context | undefined = context) =>
  assertAvisonYoungDetailDoc(doc(html), named.response_url, row, owners);

test("named property uses exact publisher linkage and labeled offering address without rewriting feed street", async () => {
  assert.ok(check());
  const previous = process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
  process.env.AVISON_YOUNG_DETAIL_TRANSPORT = "direct";
  try {
    const enriched = await enrichAvisonYoungListing({ ...base, sharpLaunchUrl: undefined }, false,
      async url => fetchAvisonYoungDirectDoc(url, 30000, async () => ["93.184.216.34"],
        async () => ({ status: 200, location: null, body: named.html })), context);
    assert.equal(enriched.detailError, undefined);
    assert.equal(enriched.street, "The Shoppes at Glen Laurel");
    assert.equal(enriched.rawSharpLaunch.address, named.feed_row.address);
    assert.equal(enriched.name, named.feed_row.name);
    assert.match(enriched.markdown, /148 Briarcliff Drive/);
    assert.match(enriched.markdown, /29\.50\/SF/);
    assert.ok(enriched.documents.some((item: any) => item.url.endsWith("The_Shoppes_at_Glen_Laurel_Retail_Brochure.pdf")));
    assert.equal(enriched.buildingSizeSqft, 10000);
  } finally {
    if (previous === undefined) delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
    else process.env.AVISON_YOUNG_DETAIL_TRANSPORT = previous;
  }
});

for (const mutation of ["missing-remote", "wrong-remote", "duplicate-remote", "wrong-title", "wrong-og-title",
  "wrong-h1", "wrong-canonical", "missing-canonical", "foreign-og", "wrong-subject", "duplicate-record", "wrong-structured-street",
  "wrong-city", "wrong-state", "wrong-zip", "missing-zip", "wrong-feed-zip", "feed-city-punctuation", "feed-state-punctuation", "foreign-headline", "missing-address", "wrong-label",
  "wrong-section", "duplicate-address", "conflicting-address", "extra-address", "duplicate-hero", "wrong-hero",
  "hero-unit", "address-range", "address-unit", "address-intersection", "map-conflict", "map-malformed", "map-quoted", "map-single-quoted", "map-name-conflict", "description-conflict", "description-no-zip", "named-description-no-zip", "description-bare-street", "description-bare-unit", "description-bare-range", "description-bare-intersection", "named-description-bare-street",
  "h2-conflict", "numeric-subject", "range-subject", "unit-subject", "intersection-subject", "punctuation-subject"]) {
  test(`named property refuses ${mutation}`, () => {
    const $ = cheerio.load(named.html); const row = { ...base };
    const label = $('.property-info-list li').first();
    const ldNode = $('script[type="application/ld+json"]'); const ld = JSON.parse(ldNode.text());
    if (mutation === "missing-remote") $('script').filter((_, e) => $(e).text().includes("remote:")).remove();
    if (mutation === "wrong-remote") $('script').filter((_, e) => $(e).text().includes("remote:")).text("/* remote: 17441 */");
    if (mutation === "duplicate-remote") $('body').append('<script>/* remote: 17440 */</script>');
    if (mutation === "wrong-title") $('title').text("Other Plaza");
    if (mutation === "wrong-og-title") $('meta[property="og:title"]').attr("content", "Other Plaza");
    if (mutation === "wrong-h1") $('h1').text("Other Plaza");
    if (mutation === "wrong-canonical") $('link[rel="canonical"]').attr("href", named.response_url + "/other");
    if (mutation === "missing-canonical") $('link[rel="canonical"]').remove();
    if (mutation === "foreign-og") $('meta[property="og:url"]').attr("content", "https://foreign.example/property");
    if (mutation === "wrong-subject") ld.name = "Other Plaza";
    if (mutation === "duplicate-record") $('head').append(`<script type="application/ld+json">${JSON.stringify(ld)}</script>`);
    if (mutation === "wrong-structured-street") ld.address.streetAddress = "149 Briarcliff Drive";
    if (mutation === "wrong-city") ld.address.addressLocality = "Other City";
    if (mutation === "wrong-state") ld.address.addressRegion = "TX";
    if (mutation === "wrong-zip") ld.propertyListing.address.postalCode = "27528";
    if (mutation === "missing-zip") delete ld.propertyListing.address.postalCode;
    if (mutation === "wrong-feed-zip") row.postalCode = "27528";
    if (mutation === "feed-city-punctuation") row.city = "Clayton#";
    if (mutation === "feed-state-punctuation") row.state = "NC+";
    if (mutation === "foreign-headline") row.headline = "149 Briarcliff Drive";
    if (mutation === "missing-address") label.remove();
    if (mutation === "wrong-label") label.find('span').first().text("Location");
    if (mutation === "wrong-section") $('.building__property-info-title').text("Nearby Locations");
    if (mutation === "duplicate-address") label.after(label.clone());
    if (mutation === "conflicting-address") label.after('<li><span>Address</span><span>149 Briarcliff Drive, Clayton, NC</span></li>');
    if (mutation === "extra-address") $('body').append('<div class="property-info-list"><ul><li><span>Address</span><span>149 Briarcliff Drive, Clayton, NC</span></li></ul></div>');
    if (mutation === "duplicate-hero") $('.hero__sub-title').after($('.hero__sub-title').clone());
    if (mutation === "wrong-hero") $('.hero__sub-title').text("149 Briarcliff Drive, Clayton, NC");
    if (mutation === "hero-unit") $('.hero__sub-title').text("148 Briarcliff Drive Unit 2, Clayton, NC");
    for (const [key, value] of [["address-range", "148-150 Briarcliff Drive"], ["address-unit", "148 Briarcliff Drive Unit 2"], ["address-intersection", "148 Briarcliff Drive & Other Road"]]) {
      if (mutation === key) { label.find('span').last().text(value + ", Clayton, NC"); $('.hero__sub-title').text(value + ", Clayton, NC"); }
    }
    if (mutation === "map-conflict") $('body').append('<script>var bootstrap={poi:[{"address":"149 Briarcliff Drive","name":"Other"}]};</script>');
    if (mutation === "map-malformed") $('body').append('<script>var bootstrap={poi:unknown};</script>');
    if (mutation === "map-quoted") $('body').append('<script>var bootstrap={"poi":[{"address":"149 Briarcliff Drive","name":"Other"}]};</script>');
    if (mutation === "map-single-quoted") $('body').append("<script>var bootstrap={'poi':[{\"address\":\"149 Briarcliff Drive\",\"name\":\"Other\"}]};</script>");
    if (mutation === "map-name-conflict") $('body').append('<script>var bootstrap={poi:[{"address":"148 Briarcliff Drive","name":"149 Briarcliff Drive"}]};</script>');
    if (mutation === "description-conflict") $('meta[property="og:description"]').attr("content", "Other Plaza, 149 Briarcliff Drive, Clayton, NC 27527");
    if (mutation === "description-no-zip") $('meta[property="og:description"]').attr("content", "149 Briarcliff Drive, Clayton, NC");
    if (mutation === "named-description-no-zip") $('meta[property="og:description"]').attr("content", "The Shoppes at Glen Laurel, 149 Briarcliff Drive, Clayton, NC");
    if (mutation === "description-bare-street") $('meta[property="og:description"]').attr("content", "149 Briarcliff Drive");
    if (mutation === "description-bare-unit") $('meta[property="og:description"]').attr("content", "148 Briarcliff Drive Unit 2");
    if (mutation === "description-bare-range") $('meta[property="og:description"]').attr("content", "148-150 Briarcliff Drive");
    if (mutation === "description-bare-intersection") $('meta[property="og:description"]').attr("content", "148 Briarcliff Drive & Other Road");
    if (mutation === "named-description-bare-street") $('meta[property="og:description"]').attr("content", "The Shoppes at Glen Laurel, 149 Briarcliff Drive");
    if (mutation === "h2-conflict") $('body').append('<h2>149 Briarcliff Drive, Clayton, NC</h2>');
    const subjects: Record<string, string> = { "numeric-subject": "149 Briarcliff Drive", "range-subject": "148-150 Briarcliff Drive",
      "unit-subject": "Glen Laurel Unit 2", "intersection-subject": "Glen Laurel & Briarcliff", "punctuation-subject": "Glen Laurel #B" };
    if (subjects[mutation]) {
      const subject = subjects[mutation];
      row.name = row.headline = row.street = subject;
      $('title,h1').text(subject); $('meta[property="og:title"]').attr('content', subject);
      ld.name = ld.address.streetAddress = ld.propertyListing.address.streetAddress = subject;
    }
    ldNode.text(JSON.stringify(ld));
    assert.throws(() => check($.html(), row), /identity|structure/);
  });
}

test("named-property association refuses missing or ambiguous complete-feed ownership", () => {
  for (const owners of [undefined, [], ["17441"], ["17440", "17441"], ["17440", "17440"]]) {
    assert.throws(() => assertAvisonYoungDetailDoc(doc(named.html), named.response_url, base,
      owners === undefined ? undefined : { matchingExternalUrlRowIds: owners }), /identity/);
  }
});

test("agreeing physical map and optional ZIP corroborate rather than replace labeled address", () => {
  for (const key of ["poi", '"poi"', "'poi'"]) {
    const $ = cheerio.load(named.html);
    $('body').append(`<script>var bootstrap={${key}:[{"address":"148 Briarcliff Drive","name":"The Shoppes at Glen Laurel"}]};</script>`);
    $('.hero__sub-title').text("148 Briarcliff Drive, Clayton, NC 27527");
    $('meta[property="og:description"]').attr('content', "The Shoppes at Glen Laurel, 148 Briarcliff Drive, Clayton, NC");
    assert.ok(check($.html()));
  }
});

test("bare agreeing address descriptions supplement the complete primary association", () => {
  for (const value of ["148 Briarcliff Drive", "The Shoppes at Glen Laurel, 148 Briarcliff Drive"]) {
    const $ = cheerio.load(named.html);
    $('meta[property="og:description"]').attr('content', value);
    assert.ok(check($.html()));
  }
});

test("recognized Building page with a missing hero refuses instead of falling through to generic JSON-LD", () => {
  const $ = cheerio.load(named.html); $('.hero__sub-title').remove();
  assert.throws(() => check($.html()), /identity/);
  assert.throws(() => assertAvisonYoungDetailDoc(doc($.html()), named.response_url, base), /identity/);
});

test("fully witnessed range offering accepts endpoint map address without rewriting its range", () => {
  const range = fixture(18071); const row = avisonYoungBaseListing(range.feed_row, new Map());
  assert.match(range.html, /"address":"1641 El Dorado St"/);
  const accepted = assertAvisonYoungDetailDoc(doc(range.html, range.response_url), range.response_url, row,
    { matchingExternalUrlRowIds: ["18071"] });
  assert.ok(accepted);
  assert.equal(accepted.address.streetAddress, range.feed_row.address);
  assert.equal(accepted.propertyListing.address.streetAddress, range.feed_row.address);
});

test("real source caller applies named association only with all-feed unique URL ownership", async () => {
  const oldFetch = globalThis.fetch; const oldScrape = firecrawl.scrape;
  const keys = ["CRE_REQUIRE_FRESH_DETAILS", "CRE_REQUIRE_FRESH_PROPERTY_DETAILS", "CRE_REFRESH_GENERATION", "AVISON_YOUNG_DETAIL_TRANSPORT", "AVISON_YOUNG_DETAIL_LIMIT"];
  const saved = new Map(keys.map(key => [key, process.env[key]]));
  try {
    for (const key of keys) delete process.env[key];
    for (const collision of [false, true]) {
      const rows = collision ? [named.feed_row, { ...named.feed_row, id: 17441, status: "inactive" }] : [named.feed_row];
      let reads = 0; let details = 0;
      globalThis.fetch = async input => {
        reads++; const url = new URL(String(input));
        if (url.hostname === "www.avisonyoung.us") return new Response("<script>SharpLaunch.PSE.create('0123456789abcdef0123456789abcdef')</script>");
        assert.equal(url.hostname, "pse-api.sharplaunch.com");
        return new Response(JSON.stringify({ items: url.searchParams.get("entity") === "website" ? rows : [] }));
      };
      firecrawl.scrape = async (url: string) => { details++; return fetchAvisonYoungDirectDoc(url, 30000,
        async () => ["93.184.216.34"], async () => ({ status: 200, location: null, body: named.html })); };
      const source = await import(`../../../sources/avison-young.js?named-caller=${collision}`);
      const result = await source.srcAvisonYoung("lease", 1, false);
      assert.equal(reads, 3); assert.equal(details, 2);
      assert.deepEqual(result.listings.map((row: any) => row.id), ["17440"]);
      if (collision) assert.match(result.listings[0]?.detailError ?? "", /identity/);
      else {
        assert.equal(result.listings[0]?.detailJsonLd?.name, named.feed_row.name);
        assert.equal(result.listings[0]?.street, named.feed_row.address);
        assert.ok(result.listings[0]?.documents.length);
      }
      const index = indexAvisonYoungOfficialFeedUrls(rows);
      assert.deepEqual(index.get(avisonYoungOfficialUrlKey(named.response_url)!), collision ? ["17440", "17441"] : ["17440"]);
    }
  } finally {
    globalThis.fetch = oldFetch; firecrawl.scrape = oldScrape;
    for (const [key, value] of saved) { if (value === undefined) delete process.env[key]; else process.env[key] = value; }
  }
});
