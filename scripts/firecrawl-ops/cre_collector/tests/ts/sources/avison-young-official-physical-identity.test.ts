import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as cheerio from "cheerio";
import { assertAvisonYoungDetailDoc, avisonYoungBaseListing } from "../../../sources/avison-young.js";
const html = readFileSync(new URL("../../fixtures/raw_data/avison-young-official-physical-identity.html", import.meta.url), "utf8");
const feed = JSON.parse(readFileSync(new URL("../../fixtures/raw_data/avison-young-official-physical-identity-feed.json", import.meta.url), "utf8"));
const base = avisonYoungBaseListing(feed, new Map());
function check(body = html, member = base) {
  return assertAvisonYoungDetailDoc({ rawHtml: body, markdown: "", links: [], metadata: { statusCode: 200, sourceURL: base.externalUrl } }, base.externalUrl, member);
}
test("Avison official offering binds agreeing structured and visible complete address despite prose metadata", () => {
  const before = structuredClone(base);
  assert.doesNotThrow(() => check());
  assert.deepEqual(base, before);
});
test("Avison official physical family accepts an agreeing bare tuple", () => {
  const $ = cheerio.load(html);
  $('meta[property="og:description"]').attr("content", "160 E. Horizon Drive, 160 E. Horizon Drive, Henderson, NV 89015");
  assert.doesNotThrow(() => check($.html()));
});
test("Avison official physical family refuses a bare nonnumeric conflicting street tuple", () => {
  const $ = cheerio.load(html);
  $('meta[property="og:description"]').attr("content", "Other Property, Other Road, Henderson, NV 89015");
  assert.throws(() => check($.html()), /identity/);
});
test("Avison official physical family binds another complete subject and offering without property exceptions", () => {
  const $ = cheerio.load(html);
  const member = { ...base, name: "Known Office Property", headline: "Office Campaign", street: "42 E Main Rd", city: "Other City", state: "TX", postalCode: "75001" };
  $("title").text(member.headline); $('meta[property="og:title"]').attr("content", member.headline);
  $('meta[property="og:description"]').attr("content", "Office building with a flexible layout.");
  $("h1").text("Retail for Sale"); $(".hero__sub-title").text("42 E Main Road, Other City, TX 75001");
  const node = $('script[type="application/ld+json"]'); const record = JSON.parse(node.text());
  record.name = member.name;
  for (const address of [record.address, record.propertyListing.address]) Object.assign(address, { streetAddress: member.street, addressLocality: member.city, addressRegion: member.state, postalCode: member.postalCode });
  node.text(JSON.stringify(record));
  assert.doesNotThrow(() => check($.html(), member));
});
for (const mutation of ["street", "city", "state", "zip", "missing-visible-zip", "structured-street", "structured-zip", "missing-structured-zip", "structured-subject", "duplicate-listing", "feed-subject", "title", "og-title", "generic-h1", "unit-h1", "duplicate-h1", "duplicate-hero", "description-zip", "description-street", "bare-description-zip", "bare-description-subject", "duplicate-description", "canonical", "og-url", "price-hero", "empty"]) {
  test(`Avison official complete physical witness refuses ${mutation}`, () => {
    const $ = cheerio.load(html); const member = { ...base };
    if (mutation === "street") $(".hero__sub-title").text("999 Other Drive, Henderson, NV 89015");
    if (mutation === "city") $(".hero__sub-title").text("160 E. Horizon Drive, Other City, NV 89015");
    if (mutation === "state") $(".hero__sub-title").text("160 E. Horizon Drive, Henderson, TX 89015");
    if (mutation === "zip") $(".hero__sub-title").text("160 E. Horizon Drive, Henderson, NV 89014");
    if (mutation === "missing-visible-zip") $(".hero__sub-title").text("160 E. Horizon Drive, Henderson, NV");
    const node = $('script[type="application/ld+json"]'); const record = JSON.parse(node.text());
    if (mutation === "structured-street") record.propertyListing.address.streetAddress = "999 Other Drive";
    if (mutation === "structured-zip") record.propertyListing.address.postalCode = "89014";
    if (mutation === "missing-structured-zip") { delete record.address.postalCode; delete record.propertyListing.address.postalCode; }
    if (mutation === "structured-subject") record.name = "Other Property";
    node.text(JSON.stringify(record));
    if (mutation === "duplicate-listing") $("head").append(node.clone());
    if (mutation === "feed-subject") { member.name = "Other Property"; member.headline = "Other Property"; }
    if (mutation === "title") $("title").text("Other Property");
    if (mutation === "og-title") $('meta[property="og:title"]').attr("content", "Other Property");
    if (mutation === "generic-h1") $("h1").text("Welcome");
    if (mutation === "unit-h1") $("h1").text("SuiteBC");
    if (mutation === "duplicate-h1") $("main").append($("h1").clone());
    if (mutation === "duplicate-hero") $("main").append($(".hero__sub-title").clone());
    if (mutation === "description-zip") $('meta[property="og:description"]').attr("content", "For lease - 160 E. Horizon Drive, 160 E. Horizon Drive, Henderson, NV 89014");
    if (mutation === "description-street") $('meta[property="og:description"]').attr("content", "For lease - 160 E. Horizon Drive, 999 Other Drive, Henderson, NV 89015");
    if (mutation === "bare-description-zip") $('meta[property="og:description"]').attr("content", "160 E. Horizon Drive, 160 E. Horizon Drive, Henderson, NV 89014");
    if (mutation === "bare-description-subject") $('meta[property="og:description"]').attr("content", "Other Property, 999 Other Drive, Henderson, NV 89015");
    if (mutation === "duplicate-description") $("head").append($('meta[property="og:description"]').clone());
    if (mutation === "canonical") $("head").append('<link rel="canonical" href="https://www.avisonyoung.us/properties/other">');
    if (mutation === "og-url") $('meta[property="og:url"]').attr("content", "https://www.avisonyoung.us/properties/other");
    if (mutation === "price-hero") $(".hero__sub-title").text("Priced at $1,000,000");
    if (mutation === "empty") { $("head").empty(); $("body").empty(); }
    assert.throws(() => check($.html(), member), /identity|structure/);
  });
}
