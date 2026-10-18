import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as cheerio from "cheerio";
import { assertAvisonYoungDetailDoc, avisonYoungBaseListing } from "../../../sources/avison-young.js";
const html = readFileSync(new URL("../../fixtures/raw_data/avison-young-official-range-scope.html", import.meta.url), "utf8");
const feed = JSON.parse(readFileSync(new URL("../../fixtures/raw_data/avison-young-official-range-scope-feed.json", import.meta.url), "utf8"));
const base = avisonYoungBaseListing(feed, new Map());
function check(body = html, member = base, url = base.externalUrl) {
  return assertAvisonYoungDetailDoc({ rawHtml: body, markdown: "", links: [], metadata: { statusCode: 200, sourceURL: url } }, url, member);
}
test("Avison official complete physical anchor can describe its exact starting-address range scope", () => {
  const before = structuredClone(base);
  assert.doesNotThrow(() => check());
  assert.deepEqual(base, before);
});
for (const marker of ["&", "/", "#", "and", "at"]) for (const position of ["feed", "description"]) {
  test(`Avison official range witness refuses raw ${position} ${marker} role ambiguity`, () => {
    const $ = cheerio.load(html);
    const normalizedSuffix = ["and", "at"].includes(marker) ? `E Raines ${marker} Other Rd` : "E Raines Other Rd";
    const rawSuffix = `E Raines ${marker} Other Rd`;
    const member = { ...base, street: "5146 " + (position === "feed" ? rawSuffix : normalizedSuffix) };
    $(".hero__sub-title, h2").filter((_i, e) => $(e).text().startsWith("5146")).text(`5146 ${normalizedSuffix}, Memphis, TN 38118`);
    const node = $('script[type="application/ld+json"]'); const record = JSON.parse(node.text());
    for (const address of [record.address, record.propertyListing.address]) address.streetAddress = "5146 " + normalizedSuffix;
    node.text(JSON.stringify(record));
    $('meta[property="og:description"]').attr("content", "For lease - Willow Lake Industrial Park - Building 7, 5146-5202 " + (position === "description" ? rawSuffix : normalizedSuffix) + ", Memphis, TN 38118");
    assert.throws(() => check($.html(), member), /identity/);
  });
}
for (const mutation of ["low", "high", "descending", "equal", "suffix", "direction", "unit", "intersection", "zip", "city", "state", "subject", "missing-description", "empty-description", "oversized-description", "duplicate-description", "conflicting-description", "generic-h1", "wrong-h1", "title", "og-title", "canonical", "og-url", "hero-zip", "hero-street", "hero-unit", "missing-visible-zip", "structured-street", "structured-zip", "missing-structured-zip", "structured-subject", "duplicate-listing", "address-h2", "range-h2", "missing-structure", "native", "reverse-feed-range"]) {
  test(`Avison official one-way range scope enforces ${mutation}`, () => {
    const $ = cheerio.load(html); const member = { ...base }; let url = base.externalUrl;
    const description = $('meta[property="og:description"]');
    let text = description.attr("content")!;
    const rangeChanges: Record<string, string> = { low: "5147-5202", high: "5146-5203", descending: "5146-5000", equal: "5146-5146" };
    if (rangeChanges[mutation]) text = text.replace("5146-5202", rangeChanges[mutation]!);
    if (mutation === "high") $("head").append('<meta name="description" content="' + description.attr("content") + '">');
    if (mutation === "suffix") text = text.replace("Raines", "Other");
    if (mutation === "direction") text = text.replace(" E Raines", " W Raines");
    if (mutation === "unit") text = text.replace("Raines Rd", "Raines Rd Suite A");
    if (mutation === "intersection") text = text.replace("Raines Rd", "Raines Rd & Other Road");
    if (mutation === "zip") text = text.replace("38118", "38119");
    if (mutation === "city") text = text.replace("Memphis", "Other City");
    if (mutation === "state") text = text.replace("TN", "TX");
    if (mutation === "subject") text = text.replace("Willow Lake Industrial Park - Building 7", "Other Property");
    description.attr("content", text);
    if (mutation === "missing-description") description.remove();
    if (mutation === "empty-description") description.attr("content", "");
    if (mutation === "oversized-description") description.attr("content", text + "x".repeat(1001));
    if (mutation === "duplicate-description") $("head").append(description.clone());
    if (mutation === "conflicting-description") $("head").append('<meta name="description" content="For lease - Other Property, 999 Other Road, Memphis, TN 38118">');
    if (mutation === "generic-h1") $("h1").text("Industrial for Lease");
    if (mutation === "wrong-h1") $("h1").text("Other Property");
    if (mutation === "title") $("title").text("Other Property");
    if (mutation === "og-title") $('meta[property="og:title"]').attr("content", "Other Property");
    if (mutation === "canonical") $("head").append('<link rel="canonical" href="https://www.avisonyoung.us/properties/other">');
    if (mutation === "og-url") $('meta[property="og:url"]').attr("content", "https://www.avisonyoung.us/properties/other");
    if (mutation === "hero-zip") $(".hero__sub-title").text("5146 E Raines Road, Memphis, TN 38119");
    if (mutation === "hero-street") $(".hero__sub-title").text("5147 E Raines Road, Memphis, TN 38118");
    if (mutation === "hero-unit") $(".hero__sub-title").text("5146 E Raines Road Suite A, Memphis, TN 38118");
    if (mutation === "missing-visible-zip") $(".hero__sub-title").text("5146 E Raines Road, Memphis, TN");
    const node = $('script[type="application/ld+json"]'); const record = JSON.parse(node.text());
    if (mutation === "structured-street") record.propertyListing.address.streetAddress = "5147 E Raines Road";
    if (mutation === "structured-zip") record.propertyListing.address.postalCode = "38119";
    if (mutation === "missing-structured-zip") delete record.address.postalCode;
    if (mutation === "structured-subject") record.name = "Other Property";
    node.text(JSON.stringify(record));
    if (mutation === "duplicate-listing") $("head").append(node.clone());
    if (mutation === "range-h2") $("main").append("<h2>5146-5202 E Raines Road, Memphis, TN 38118</h2>");
    if (mutation === "address-h2") $("main").append("<h2>999 Other Road, Memphis, TN 38118</h2>");
    if (mutation === "missing-structure") $(".page-content").remove();
    if (mutation === "native") { url = base.sharpLaunchUrl; record.url = url; node.text(JSON.stringify(record)); $('meta[property="og:url"]').attr("content", url); }
    if (mutation === "reverse-feed-range") { member.street = "5146-5202 E Raines Rd"; description.attr("content", text.replace("5146-5202 E Raines Rd", "5146 E Raines Rd")); }
    if (mutation === "missing-description" || mutation === "empty-description") assert.doesNotThrow(() => check($.html(), member, url)); // Existing complete-visible family has optional metadata.
    else assert.throws(() => check($.html(), member, url), /identity|structure/);
  });
}
