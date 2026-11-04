import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as cheerio from "cheerio";
import { assertAvisonYoungDetailDoc } from "../../../sources/avison-young.js";
import type { ScrapedDoc } from "../../../types.js";

// Sanitized observed hero/native-info structures; private primary hashes and
// extraction/replacement provenance are retained with the review proof.
const cases = [
  { key: "land", base: { name: "Land at SEC Market Rd & River Rd", street: "SEC Market Road & River Road", city: "Example City", state: "AZ", postalCode: "85335", sharpLaunchUrl: "https://ayusfixtureland.sharplaunch.com" } },
  { key: "medical", base: { name: "Example Medical Plaza", street: "19000 Commerce Boulevard", city: "Example City", state: "CA", postalCode: "90503", sharpLaunchUrl: "https://ayusfixturemedical.sharplaunch.com" } },
].map(row => ({ ...row, html: readFileSync(new URL(`../../fixtures/raw_data/avison-young-visible-${row.key}.html`, import.meta.url), "utf8") }));
function doc(html: string, url: string, statusCode = 200): ScrapedDoc {
  return { rawHtml: html, markdown: "", links: [], metadata: { sourceURL: url, statusCode } };
}
for (const row of cases) {
  const url = row.base.sharpLaunchUrl;
  test(`Avison observed ${row.key} hero binds complete visible identity without rewriting feed fields`, () => {
    const before = structuredClone(row.base);
    assert.doesNotThrow(() => assertAvisonYoungDetailDoc(doc(row.html, url), url, row.base));
    assert.deepEqual(row.base, before);
  });
  for (const field of ["name", "street", "city", "state"] as const) {
    test(`Avison observed ${row.key} rejects wrong ${field}`, () => {
      const value = field === "state" ? "TX" : field === "street" && row.key === "medical" ? "19001 Commerce Boulevard" : "Unrelated property";
      assert.throws(() => assertAvisonYoungDetailDoc(doc(row.html, url), url, { ...row.base, [field]: value }), /identity/);
    });
  }
  for (const mutation of ["title", "og-title", "og-url", "canonical", "duplicate-title", "duplicate-og-title", "duplicate-og-url", "duplicate-h1", "duplicate-hero", "conflicting-hero", "missing-hero", "wrong-hero-city", "wrong-hero-state", "wrong-hero-zip", "challenge", "source-url"]) {
    test(`Avison observed ${row.key} rejects ${mutation}`, () => {
      const $ = cheerio.load(row.html);
      if (mutation === "title") $("title").text("Different property");
      if (mutation === "og-title") $('meta[property="og:title"]').attr("content", "Different property");
      if (mutation === "og-url") $('meta[property="og:url"]').attr("content", "https://other.sharplaunch.com/");
      if (mutation === "canonical") $("head").append('<link rel="canonical" href="https://other.sharplaunch.com/">');
      if (mutation === "duplicate-title") $("head").append($("title").clone());
      if (mutation === "duplicate-og-title") $("head").append($('meta[property="og:title"]').clone());
      if (mutation === "duplicate-og-url") $("head").append($('meta[property="og:url"]').clone());
      if (mutation === "duplicate-h1") $("body").append($("h1").clone());
      if (mutation === "duplicate-hero") $("body").append($(".hero__sub-title").clone());
      if (mutation === "conflicting-hero") $("body").append('<h2 class="hero__sub-title">Different property, Other City, TX</h2>');
      if (mutation === "missing-hero") $(".hero__sub-title").remove();
      if (mutation === "wrong-hero-city") $(".hero__sub-title").text($(".hero__sub-title").text().replace("Example City", "Other City"));
      if (mutation === "wrong-hero-state") $(".hero__sub-title").text($(".hero__sub-title").text().replace(row.base.state, "TX"));
      if (mutation === "wrong-hero-zip") $(".hero__sub-title").append(" 99999");
      if (mutation === "challenge") $("body").append("Checking your browser");
      const d = doc($.html(), mutation === "source-url" ? "https://other.sharplaunch.com/" : url);
      assert.throws(() => assertAvisonYoungDetailDoc(d, url, row.base), /identity|challenge|structure/);
    });
  }
  test(`Avison observed ${row.key} rejects HTTP 404`, () => {
    assert.throws(() => assertAvisonYoungDetailDoc(doc(row.html, url, 404), url, row.base), /HTTP 404/);
  });
  test(`Avison observed ${row.key} accepts matching ZIP and canonical`, () => {
    const $ = cheerio.load(row.html);
    $(".hero__sub-title").append(` ${row.base.postalCode}`);
    $("head").append(`<link rel="canonical" href="${url}/">`);
    assert.doesNotThrow(() => assertAvisonYoungDetailDoc(doc($.html(), url), url, row.base));
  });
}
const land = cases[0]!;
test("Avison short name/location hero cannot replace the full street H2 witness", () => {
  const $ = cheerio.load(land.html);
  $(".hero__sub-title").text("Land at SEC Market Rd & River Rd | Example City, AZ");
  const base = { ...land.base, street: "123 Market Road" };
  $("body").append("<script>123 Market Road, Example City, AZ</script>");
  assert.throws(() => assertAvisonYoungDetailDoc(doc($.html(), base.sharpLaunchUrl), base.sharpLaunchUrl, base), /identity/);
  $("body").append("<h2>Land at SEC Market Rd &amp; River Rd | 123 Market Rd, Example City, AZ</h2>");
  assert.doesNotThrow(() => assertAvisonYoungDetailDoc(doc($.html(), base.sharpLaunchUrl), base.sharpLaunchUrl, base));
  $("body").append("<h2>Other property | 123 Market Rd, Example City, AZ</h2>");
  assert.throws(() => assertAvisonYoungDetailDoc(doc($.html(), base.sharpLaunchUrl), base.sharpLaunchUrl, base), /identity/);
});
test("Avison named layout permits repeated coherent street witnesses but refuses conflicting ones", () => {
  const $ = cheerio.load(land.html);
  $("body").append($(".hero__sub-title").clone().removeClass("hero__sub-title"));
  assert.doesNotThrow(() => assertAvisonYoungDetailDoc(doc($.html(), land.base.sharpLaunchUrl), land.base.sharpLaunchUrl, land.base));
  $("body").append(`<h2>Other property | SEC Market Rd &amp; River Rd, Example City, AZ</h2>`);
  assert.throws(() => assertAvisonYoungDetailDoc(doc($.html(), land.base.sharpLaunchUrl), land.base.sharpLaunchUrl, land.base), /identity/);
});
