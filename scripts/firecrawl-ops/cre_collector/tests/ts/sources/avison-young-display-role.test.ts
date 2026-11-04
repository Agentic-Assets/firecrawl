import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as cheerio from "cheerio";
import { assertAvisonYoungDetailDoc, avisonYoungBaseListing } from "../../../sources/avison-young.js";
import type { ScrapedDoc } from "../../../types.js";
const load = (name: string) => readFileSync(new URL(`../../fixtures/raw_data/${name}`, import.meta.url), "utf8");
const base17418 = avisonYoungBaseListing(JSON.parse(load("avison-17418-native-role-feed.json")), new Map());
const base18031 = avisonYoungBaseListing(JSON.parse(load("avison-18031-official-role-feed.json")), new Map());
const native17418 = load("avison-17418-native-role.html");
const official17418 = load("avison-17418-official-role.html");
const native18031 = load("avison-18031-native-role.html");
const official18031 = load("avison-18031-official-role.html");
function check(html: string, base: typeof base17418, url: string): void {
  const doc: ScrapedDoc = { rawHtml: html, markdown: "", links: [], metadata: { statusCode: 200, sourceURL: url } };
  assertAvisonYoungDetailDoc(doc, url, base);
}
function listing($: cheerio.CheerioAPI): any {
  return JSON.parse($('script[type="application/ld+json"]').first().text());
}

test("Avison native descriptor omission does not remove a descriptor already present in the feed stem", () => {
  const feed = JSON.parse(load("avison-17418-native-role-feed.json"));
  feed.address = "100 - 200 Indian Head Boulevard Boulevard";
  feed.name = feed.address;
  const base = avisonYoungBaseListing(feed, new Map());
  const $ = cheerio.load(native17418);
  $(".hero__sub-title").text("100-200 Indian Head, St. Louis, Missouri");
  $("title").text(base.name);
  $('meta[property="og:title"]').attr("content", base.name);
  $('meta[name="description"], meta[name="twitter:description"], meta[property="og:description"]')
    .attr("content", "For lease - 100 - 200 Indian Head Boulevard Boulevard, Saint Louis, MO 63132");
  assert.throws(() => check($.html(), base, base.sharpLaunchUrl), /identity|structure/);
});
test("Avison native full-range description and owned metadata support one omitted terminal street descriptor", () => {
  assert.doesNotThrow(() => check(native17418, base17418, base17418.sharpLaunchUrl));
});
for (const mutation of ["wrong-street", "wrong-endpoint", "wrong-direction", "unit", "intersection", "wrong-city", "wrong-state", "wrong-zip", "wrong-hero-zip", "hash-separated-range", "slash-separated-range", "ampersand-separated-range", "plus-separated-range", "at-sign-separated-range", "unicode-slash-separated-range", "pipe-separated-range", "repeated-range", "and-claim", "at-claim", "quadrant-claim", "description-street", "description-city", "missing-description", "wrong-title", "wrong-url", "generic-h1", "duplicate-hero"]) {
  test(`Avison native terminal descriptor omission refuses ${mutation}`, () => {
    const $ = cheerio.load(native17418);
    const descriptions = $('meta[name="description"], meta[name="twitter:description"], meta[property="og:description"]');
    if (mutation === "wrong-street") $(".hero__sub-title").text("10745-10767 Indian Head Industrial Drive, St. Louis, Missouri");
    if (mutation === "wrong-endpoint") $(".hero__sub-title").text("10745-10768 Indian Head Industrial, St. Louis, Missouri");
    if (mutation === "wrong-direction") $(".hero__sub-title").text("10745-10767 N Indian Head Industrial, St. Louis, Missouri");
    if (mutation === "unit") $(".hero__sub-title").text("10745-10767 Indian Head Industrial Suite A, St. Louis, Missouri");
    if (mutation === "intersection") $(".hero__sub-title").text("10745-10767 Indian Head Industrial & Other Road, St. Louis, Missouri");
    if (mutation === "wrong-city") $(".hero__sub-title").text("10745-10767 Indian Head Industrial, Other City, Missouri");
    if (mutation === "wrong-state") $(".hero__sub-title").text("10745-10767 Indian Head Industrial, St. Louis, IL");
    if (mutation === "wrong-zip") descriptions.attr("content", "For lease - 10745 - 10767 Indian Head Industrial Boulevard, Saint Louis, MO 63130");
    if (mutation === "wrong-hero-zip") $(".hero__sub-title").text("10745-10767 Indian Head Industrial, St. Louis, Missouri 63130");
    if (mutation === "hash-separated-range") $(".hero__sub-title").text("10745 #10767 Indian Head Industrial, St. Louis, Missouri");
    if (mutation === "slash-separated-range") $(".hero__sub-title").text("10745/10767 Indian Head Industrial, St. Louis, Missouri");
    if (mutation === "ampersand-separated-range") $(".hero__sub-title").text("10745&10767 Indian Head Industrial, St. Louis, Missouri");
    if (mutation === "plus-separated-range") $(".hero__sub-title").text("10745+10767 Indian Head Industrial, St. Louis, Missouri");
    if (mutation === "at-sign-separated-range") $(".hero__sub-title").text("10745@10767 Indian Head Industrial, St. Louis, Missouri");
    if (mutation === "unicode-slash-separated-range") $(".hero__sub-title").text("10745∕10767 Indian Head Industrial, St. Louis, Missouri");
    if (mutation === "pipe-separated-range") $(".hero__sub-title").text("10745|10767 Indian Head Industrial, St. Louis, Missouri");
    if (mutation === "repeated-range") $(".hero__sub-title").text("10745-10767 10745-10767 Indian Head Industrial, St. Louis, Missouri");
    if (mutation === "and-claim") $(".hero__sub-title").text("10745-10767 and Indian Head Industrial, St. Louis, Missouri");
    if (mutation === "at-claim") $(".hero__sub-title").text("10745-10767 at Indian Head Industrial, St. Louis, Missouri");
    if (mutation === "quadrant-claim") $(".hero__sub-title").text("10745-10767 NEC Indian Head Industrial, St. Louis, Missouri");
    if (mutation === "description-street") descriptions.attr("content", "For lease - 10745 - 10767 Other Street Boulevard, Saint Louis, MO 63132");
    if (mutation === "description-city") descriptions.attr("content", "For lease - 10745 - 10767 Indian Head Industrial Boulevard, Other City, MO 63132");
    if (mutation === "missing-description") descriptions.remove();
    if (mutation === "wrong-title") $("title").text("Other Property");
    if (mutation === "wrong-url") $('meta[property="og:url"]').attr("content", "https://other.sharplaunch.com/property");
    if (mutation === "generic-h1") $("h1").text("Industrial space available");
    if (mutation === "duplicate-hero") $("main").append($(".hero__sub-title").clone());
    assert.throws(() => check($.html(), base17418, base17418.sharpLaunchUrl), /identity|structure/);
  });
}

test("Avison official exact structured address and street-plus-acreage H1 bind matching visible locality", () => {
  assert.doesNotThrow(() => check(official18031, base18031, base18031.externalUrl));
});
for (const mutation of ["wrong-street", "wrong-route", "wrong-direction", "wrong-range", "hash-before-acreage", "slash-before-acreage", "ampersand-before-acreage", "plus-before-acreage", "at-sign-before-acreage", "unicode-slash-before-acreage", "pipe-before-acreage", "and-claim", "at-claim", "quadrant-claim", "repeated-range", "unit", "intersection", "wrong-city", "wrong-state", "wrong-zip", "structured-street", "structured-name", "structured-url", "structured-zip", "duplicate-listing", "wrong-name", "wrong-og-url", "wrong-canonical", "generic-h1", "no-acres", "zero-acreage", "extra-claim", "description-claim"]) {
  test(`Avison official street-acreage witness refuses ${mutation}`, () => {
    const $ = cheerio.load(official18031);
    const node = $('script[type="application/ld+json"]'); const record = listing($);
    if (mutation === "wrong-street") $("h1").text("Hwy 222 - 117 acres");
    if (mutation === "wrong-route") $("h1").text("US Highway 221 - 117 acres");
    if (mutation === "wrong-direction") $("h1").text("N Hwy 221 - 117 acres");
    if (mutation === "wrong-range") $("h1").text("Hwy 221-223 - 117 acres");
    if (mutation === "hash-before-acreage") $("h1").text("Hwy 221 # - 117 acres");
    if (mutation === "slash-before-acreage") $("h1").text("Hwy 221 / - 117 acres");
    if (mutation === "ampersand-before-acreage") $("h1").text("Hwy 221 & - 117 acres");
    if (mutation === "plus-before-acreage") $("h1").text("Hwy 221 + - 117 acres");
    if (mutation === "at-sign-before-acreage") $("h1").text("Hwy 221 @ - 117 acres");
    if (mutation === "unicode-slash-before-acreage") $("h1").text("Hwy 221 ∕ - 117 acres");
    if (mutation === "pipe-before-acreage") $("h1").text("Hwy 221 | - 117 acres");
    if (mutation === "and-claim") $("h1").text("Hwy 221 and Other Road - 117 acres");
    if (mutation === "at-claim") $("h1").text("Hwy 221 at Other Road - 117 acres");
    if (mutation === "quadrant-claim") $("h1").text("Hwy 221 SWC - 117 acres");
    if (mutation === "repeated-range") $("h1").text("Hwy 221-223 - 221 - 117 acres");
    if (mutation === "unit") $("h1").text("Hwy 221 Suite A - 117 acres");
    if (mutation === "intersection") $("h1").text("Hwy 221 & Other Road - 117 acres");
    if (mutation === "wrong-city") $(".hero__sub-title").text("Other City, South Carolina");
    if (mutation === "wrong-state") $(".hero__sub-title").text("Laurens, North Carolina");
    if (mutation === "wrong-zip") $(".hero__sub-title").text("Laurens, South Carolina 00000");
    if (mutation === "structured-street") record.address.streetAddress = record.propertyListing.address.streetAddress = "Hwy 222";
    if (mutation === "structured-name") record.name = "Other Property";
    if (mutation === "structured-url") record.url = "https://www.avisonyoung.us/properties/other";
    if (mutation === "structured-zip") record.address.postalCode = record.propertyListing.address.postalCode = "00000";
    if (mutation === "wrong-name") { $("title").text("Other Property"); $('meta[property="og:title"]').attr("content", "Other Property"); }
    if (mutation === "wrong-og-url") $('meta[property="og:url"]').attr("content", "https://www.avisonyoung.us/properties/other");
    if (mutation === "wrong-canonical") $('link[rel="canonical"]').attr("href", "https://www.avisonyoung.us/properties/other");
    if (mutation === "generic-h1") $("h1").text("Land for Sale");
    if (mutation === "no-acres") $("h1").text("Hwy 221 - 117 sf");
    if (mutation === "zero-acreage") $("h1").text("Hwy 221 - 0 acres");
    if (mutation === "extra-claim") $("h1").text("Hwy 221 - 117 acres - 200,000 sf");
    if (mutation === "description-claim") $('meta[property="og:description"]').attr("content", "Hwy 222, Laurens, SC 29360");
    node.text(JSON.stringify(record));
    if (mutation === "duplicate-listing") $("head").append(node.clone());
    assert.throws(() => check($.html(), base18031, base18031.externalUrl), /identity|structure/);
  });
}

test("Avison native 18031 remains insufficient without full physical identity", () => {
  assert.throws(() => check(native18031, base18031, base18031.sharpLaunchUrl), /identity|structure/);
});
test("Avison official 17418 remains refused with its observed missing visible ZIP anchor", () => {
  assert.throws(() => check(official17418, base17418, base17418.externalUrl), /identity|structure/);
});
