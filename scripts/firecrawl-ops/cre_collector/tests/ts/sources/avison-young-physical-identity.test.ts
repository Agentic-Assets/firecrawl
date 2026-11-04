import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as cheerio from "cheerio";
import { assertAvisonYoungDetailDoc } from "../../../sources/avison-young.js";
import type { ScrapedDoc } from "../../../types.js";

// Reduced original identity nodes and a substantive fact row; hashes in fixtures.
const cases = [
  { id: "17939", name: "140 S Dearborn", street: "140 S Dearborn", city: "Chicago", state: "IL", postalCode: "60603" },
  { id: "17981", name: "LogistiCenter at Nellis Blvd", street: "5005 E Cartier Ave", city: "Las Vegas", state: "NV", postalCode: "89115" },
  { id: "18225", name: "Glendale Palms", street: "SEC 67th Avenue & Bell Road", city: "Glendale", state: "AZ", postalCode: "85308" },
  { id: "18660", name: "3838 Charles Raper Jonas Hwy", street: "3838 Charles Raper Jonas Hwy", city: "Stanley", state: "NC", postalCode: "28164" },
].map((row) => {
  const html = readFileSync(new URL(`../../fixtures/raw_data/avison-young-physical-${row.id}.html`, import.meta.url), "utf8");
  return { ...row, html, sharpLaunchUrl: cheerio.load(html)('meta[property="og:url"]').attr("content")! };
});
function check(row: typeof cases[number], html = row.html, base = row) {
  const doc: ScrapedDoc = { rawHtml: html, markdown: "", links: [], metadata: { statusCode: 200, sourceURL: row.sharpLaunchUrl } };
  return assertAvisonYoungDetailDoc(doc, row.sharpLaunchUrl, base);
}
for (const row of cases) {
  test(`Avison physical witness accepts primary ${row.id} without renaming source fields`, () => {
    const before = structuredClone(row);
    assert.doesNotThrow(() => check(row));
    assert.deepEqual(row, before);
  });
  for (const mutation of ["missing-description", "blank-description", "conflicting-description", "duplicate-description", "unknown-zip", "wrong-zip", "wrong-street", "wrong-city", "wrong-state", "missing-hero", "duplicate-hero", "duplicate-h1", "title", "og-title", "og-url", "canonical", "empty-structure", "structured-address", "structured-name", "duplicate-structured", "contradictory-h2", "challenge"]) {
    test(`Avison physical primary ${row.id} refuses ${mutation}`, () => {
      const $ = cheerio.load(row.html); const base = { ...row };
      if (mutation === "missing-description") $('meta[name="description"],meta[name="twitter:description"],meta[property="og:description"]').remove();
      if (mutation === "blank-description") $('meta[name="description"]').attr("content", "");
      if (mutation === "conflicting-description") $('meta[name="description"]').attr("content", "Other Property, 999 Other Road, Other City, TX 99999");
      if (mutation === "duplicate-description") $("head").append($('meta[name="description"]').clone());
      if (mutation === "unknown-zip") base.postalCode = "";
      if (mutation === "wrong-zip") $(".hero__sub-title").append(" 99999");
      if (mutation === "wrong-street") base.street = "999 Other Road";
      if (mutation === "wrong-city") base.city = "Other City";
      if (mutation === "wrong-state") base.state = "TX";
      if (mutation === "missing-hero") $(".hero__sub-title").remove();
      if (mutation === "duplicate-hero") $("body").append($(".hero__sub-title").clone());
      if (mutation === "duplicate-h1") $("body").append($("h1").clone());
      if (mutation === "title") $("title").text("Different Property");
      if (mutation === "og-title") $('meta[property="og:title"]').attr("content", "Different Property");
      if (mutation === "og-url") $('meta[property="og:url"]').attr("content", "https://other.sharplaunch.com/");
      if (mutation === "canonical") $("head").append('<link rel="canonical" href="https://other.sharplaunch.com/">');
      if (mutation === "empty-structure") $(".building__property-info-box").remove();
      if (mutation.startsWith("structured") || mutation === "duplicate-structured") {
        const record = { "@type": "RealEstateListing", name: mutation === "structured-name" ? "Other Property" : row.name, url: row.sharpLaunchUrl, address: { streetAddress: mutation === "structured-address" ? "999 Other Road" : row.street, addressLocality: row.city, addressRegion: row.state } };
        const node = `<script type="application/ld+json">${JSON.stringify(record)}</script>`;
        $("head").append(node); if (mutation === "duplicate-structured") $("head").append(node);
      }
      if (mutation === "contradictory-h2") $("body").append(`<h2>${row.street}, Other City, TX</h2>`);
      if (mutation === "challenge") $("body").append("Checking your browser");
      assert.throws(() => check(row, $.html(), base), /identity|structure|challenge/);
    });
  }
}
const row = cases[0]!;
for (const heading of ["Medical Office Space in Chicago IL 60603", "Medical Office Space in Chicago Illinois", "United Building", "Office, Medical and Retail Space", "Different Property", "Unrecognized Property for Sale", "5.22 Acre Fully Secured Yard", "0.45 Acres for Sale", "Prime Retail Space for Lease in Chicago, IL", "Medical Office Space in Chicago, IL 60603"]) {
  test(`Avison advisory H1 ${heading} requires both complete physical witnesses`, () => {
    const $ = cheerio.load(row.html); $("h1").text(heading);
    assert.doesNotThrow(() => check(row, $.html()));
    $('meta[name="description"],meta[name="twitter:description"],meta[property="og:description"]').remove();
    assert.throws(() => check(row, $.html()), /identity|structure/);
  });
}
for (const heading of ["Medical Office Space in Chicago IL 99999", "Medical Office Space in Chicago Texas", "Medical Office Space in Chicago UnknownState", "Medical Office Space in Other City IL", "Medical Office Space in Chicago TX", "Main & Elm", "Main and Elm", "Main at Elm", "NWC Main at Elm", "Main / Elm", "Medical Office Space in Chicago, Illinoiszzz", "140 S Dearborn Main & Elm", "140 S Dearborn Suite A", "140 S Dearborn SuiteB", "140 S Dearborn SteA", "140 S Dearborn UnitBC", "SuiteB", "SuiteBC", "Glendale Palms UnitBC", "999 Other Road", "140-141 S Dearborn", "140 S Dearborn Suite 9", "140 S Dearborn #9", "Other Road & Bell Road", "Suite 2", "Building 2", "999 Unrecognized", "Property - 999 Other Road", "140 S Dearborn, Other City, IL", "Medical Office Space in Other City, IL", "Medical Office Space in Chicago, TX", "Medical Office Space in Chicago, IL 99999", "Medical Office Space in Chicago, ZZ"]) {
  test(`Avison stronger physical witness still refuses H1 address assertion ${heading}`, () => {
    const $ = cheerio.load(row.html); $("h1").text(heading);
    assert.throws(() => check(row, $.html()), /identity|structure/);
  });
}
for (const street of ["140-144 S Dearborn", "140 S Dearborn Suite 2", "140 S Dearborn & Bell Road"]) {
  test(`Avison physical witness retains complete synthetic range/unit/intersection ${street}`, () => {
    const html = row.html.replaceAll(row.street, street); const base = { ...row, street, name: street };
    assert.doesNotThrow(() => check(row, html, base));
    const $ = cheerio.load(html); $("h1").text(street + " Suite 9");
    assert.throws(() => check(row, $.html(), base), /identity|structure/);
  });
}