import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as cheerio from "cheerio";
import { assertAvisonYoungDetailDoc, avisonYoungBaseListing, enrichAvisonYoungListing, fetchAvisonYoungDirectDoc } from "../../../sources/avison-young.js";
import type { ScrapedDoc } from "../../../types.js";
const feeds = JSON.parse(readFileSync(new URL("../../fixtures/raw_data/avison-young-identity-role-feed.json", import.meta.url), "utf8"));
const cases = [1, 2, 3, 4].map(page => {
  const feed = feeds.find((row: any) => row.id === (page <= 2 ? 18005 : 17384));
  const base = avisonYoungBaseListing(feed, new Map());
  return { page, base, url: page % 2 ? base.sharpLaunchUrl : base.externalUrl,
    html: readFileSync(new URL(`../../fixtures/raw_data/avison-young-identity-role-${page}.html`, import.meta.url), "utf8") };
});
function doc(row: typeof cases[number], html = row.html): ScrapedDoc {
  return { rawHtml: html, markdown: "", links: [], metadata: { statusCode: 200, sourceURL: row.url } };
}
function check(row: typeof cases[number], html = row.html, base = row.base) { return assertAvisonYoungDetailDoc(doc(row, html), row.url, base); }
for (const row of cases) {
  test(`Avison response-backed identity roles page ${row.page} ${row.page === 1 ? "retains weak hotel native refusal" : "qualifies complete owned subject"}`, () => {
    const before = structuredClone(row.base);
    if (row.page === 1) assert.throws(() => check(row), /identity/);
    else assert.doesNotThrow(() => check(row));
    assert.deepEqual(row.base, before);
  });
  if (row.page === 1) continue;
  for (const mutation of ["unknown-aliases", "title", "og-title", "description", "duplicate-description", "canonical", "observed-url", "h1-street", "h1-city", "h1-unit", "h1-attached-unit-s", "duplicate-h1", "duplicate-hero", "hero-location", "structured-street", "structured-city", "structured-state", "structured-zip", "structured-duplicate", "no-structure"]) {
    if (row.page === 3 && mutation.startsWith("structured")) continue;
    test(`Avison identity role page ${row.page} refuses ${mutation}`, () => {
      const $ = cheerio.load(row.html); const base = { ...row.base };
      if (mutation === "unknown-aliases") { base.name = "Other Property"; base.headline = "Unknown Campaign"; }
      if (mutation === "title") $("title").text("Other Property");
      if (mutation === "og-title") $('meta[property="og:title"]').attr("content", "Other Property");
      if (mutation === "description") $('meta[property="og:description"]').attr("content", "Other Property, 999 Other Road, Other City, TX 99999");
      if (mutation === "duplicate-description") $("head").append($('meta[property="og:description"]').clone());
      if (mutation === "canonical") $("head").append('<link rel="canonical" href="https://www.avisonyoung.us/properties/other">');
      if (mutation === "observed-url") { assert.throws(() => assertAvisonYoungDetailDoc({ ...doc(row), metadata: { statusCode: 200, sourceURL: "https://www.avisonyoung.us/properties/other" } }, row.url, base), /identity/); return; }
      if (mutation === "h1-street") $("h1").text("999 Other Road");
      if (mutation === "h1-city") $("h1").text("Medical Office in Other City TX");
      if (mutation === "h1-unit") $("h1").text("SuiteBC");
      if (mutation === "h1-attached-unit-s") $("h1").text("SuiteS");
      if (mutation === "duplicate-h1") $("body").append($("h1").clone());
      if (mutation === "duplicate-hero") $("body").append($(".hero__sub-title").clone());
      if (mutation === "hero-location") $(".hero__sub-title").text("Other City, TX");
      if (mutation.startsWith("structured")) {
        const node = $('script[type="application/ld+json"]'); const record = JSON.parse(node.text());
        if (mutation === "structured-street") record.address.streetAddress = "999 Other Road";
        if (mutation === "structured-city") record.propertyListing.address.addressLocality = "Other City";
        if (mutation === "structured-state") record.address.addressRegion = "TX";
        if (mutation === "structured-zip") record.address.postalCode = "99999";
        node.text(JSON.stringify(record));
        if (mutation === "structured-duplicate") $("head").append(node.clone());
      }
      if (mutation === "no-structure") $("main, script[type=\"application/ld+json\"]").remove();
      assert.throws(() => check(row, $.html(), base), /identity|structure/);
    });
  }
}
const hotel = cases[1]!;
for (const headline of [undefined, "Unknown Campaign"]) test(`Avison official campaign cannot invent alias ${headline}`, () => {
  assert.throws(() => check(hotel, hotel.html, { ...hotel.base, headline }), /identity/);
});
test("Avison accepted paired medical pages preserve extra official contact and feed/page rate facts", async t => {
  const previous = process.env.AVISON_YOUNG_DETAIL_TRANSPORT; process.env.AVISON_YOUNG_DETAIL_TRANSPORT = "direct";
  t.after(() => { if (previous === undefined) delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT; else process.env.AVISON_YOUNG_DETAIL_TRANSPORT = previous; });
  const pages = cases.slice(2); const calls: string[] = [];
  const result = await enrichAvisonYoungListing({ ...pages[0]!.base, preserveChildCollections: undefined }, true, async url => { calls.push(url); const row = pages.find(row => row.url === url)!; return fetchAvisonYoungDirectDoc(url, 30000, async () => ["93.184.216.34"], async () => ({ status: 200, body: row.html, location: null })); });
  assert.deepEqual(calls, pages.map(row => row.url));
  assert.equal(result.contactsDetailed.length, 2);
  assert.equal(result.leaseRateMin, 19.5); assert.equal(result.leaseRateMax, undefined);
  assert.equal(result.rawSharpLaunch.availabilities_max_rent, 19.5);
  assert.match(result.markdown, /19\.50-\$20\.50/);
  assert.equal(result.preserveChildCollections, undefined);
});

for (const heading of ["Medical office suites available for lease", "MEDICAL OFFICE SUITES AVAILABLE FOR LEASE", "Medical Office Suites Available For Lease"]) test(`Avison complete marketing offering role is case-independent: ${heading}`, () => {
  const row = cases[2]!; const $ = cheerio.load(row.html); $("h1").text(heading);
  assert.doesNotThrow(() => check(row, $.html()));
});
for (const heading of ["suites", "SUITES", "Suites", "SuiteS", "Medical office suites available for lease in Other City TX", "Medical office suites available for lease SuiteS"]) test(`Avison incomplete or conflicting offering role refuses: ${heading}`, () => {
  const row = cases[2]!; const $ = cheerio.load(row.html); $("h1").text(heading);
  assert.throws(() => check(row, $.html()), /identity/);
});

test("Avison supported alternate keeps hotel feed/page facts without granting weak native identity", async t => {
  const previous = process.env.AVISON_YOUNG_DETAIL_TRANSPORT; process.env.AVISON_YOUNG_DETAIL_TRANSPORT = "direct";
  t.after(() => { if (previous === undefined) delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT; else process.env.AVISON_YOUNG_DETAIL_TRANSPORT = previous; });
  const pages = cases.slice(0, 2); const calls: string[] = [];
  const result = await enrichAvisonYoungListing({ ...pages[0]!.base, preserveChildCollections: undefined }, false, async url => {
    calls.push(url); const row = pages.find(row => row.url === url)!;
    return fetchAvisonYoungDirectDoc(url, 30000, async () => ["93.184.216.34"], async () => ({ status: 200, body: row.html, location: null }));
  });
  assert.deepEqual(calls, pages.map(row => row.url));
  assert.equal(result.salePriceUsd, 14500000);
  assert.equal(result.rawSharpLaunch.sale_price, 14500000);
  assert.match(result.markdown, /14,000,000/);
  assert.equal(result.name, pages[0]!.base.headline);
  assert.equal(result.headline, pages[0]!.base.headline);
  assert.equal(result.rawSharpLaunch.name, "Red Lion Harrisburg Hershey");
  assert.equal(result.preserveChildCollections, true);
  assert.equal(result.detailScrape.urls.length, 1);
  assert.match(JSON.stringify(result), /identity/);
});
