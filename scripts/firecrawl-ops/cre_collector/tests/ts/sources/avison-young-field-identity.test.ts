import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as cheerio from "cheerio";
import { assertAvisonYoungDetailDoc } from "../../../sources/avison-young.js";
import type { ScrapedDoc } from "../../../types.js";

// Reduced captured DOM: identity containers and property sections remain;
// scripts, contacts, assets, and unrelated sections are omitted.
const cases = [
  { key: "discovery", name: "Discovery Point", street: "2300 Discovery Drive", city: "Orlando", state: "FL", postalCode: "32826", sharpLaunchUrl: "https://ayusdiscoverypoint.sharplaunch.com" },
  { key: "bellevue", name: "Bellevue West", street: "7648 Highway 70 S", city: "Nashville", state: "TN", postalCode: "37221", sharpLaunchUrl: "https://ayusbellevuewest.sharplaunch.com" },
  { key: "highway", name: "3572 Winder Hwy", street: "3572 Winder Hwy", city: "Flowery Branch", state: "GA", postalCode: "30542", sharpLaunchUrl: "https://ayus3572winderhwy.sharplaunch.com" },
  { key: "saint-city", name: "401 N. Lindbergh Boulevard", street: "401 N. Lindbergh Boulevard", city: "Saint Louis", state: "MO", postalCode: "63141", sharpLaunchUrl: "https://ayus401nlindberghboulevard.sharplaunch.com" },
  { key: "property-sections", name: "3635 Knight Road", street: "3635 Knight Rd", city: "Memphis", state: "TN", postalCode: "38118", sharpLaunchUrl: "https://ayus3635knightrd.sharplaunch.com" },
].map(row => ({ ...row, html: readFileSync(new URL(`../../fixtures/raw_data/avison-young-field-${row.key}.html`, import.meta.url), "utf8") }));
function check(html: string, base: typeof cases[number]) {
  const doc: ScrapedDoc = { rawHtml: html, markdown: "", links: [], metadata: { statusCode: 200, sourceURL: base.sharpLaunchUrl } };
  return assertAvisonYoungDetailDoc(doc, base.sharpLaunchUrl, base);
}
for (const row of cases) {
  test(`Avison field-aware captured ${row.key} accepts without changing source values`, () => {
    const before = structuredClone(row);
    assert.doesNotThrow(() => check(row.html, row));
    assert.deepEqual(row, before);
  });
  for (const mutation of ["number", "range", "unit", "city", "state", "zip", "wrong-h1", "title", "og-title", "og-url", "canonical", "missing-hero", "duplicate-hero", "duplicate-h1", "conflicting-h2", "structured-name", "structured-street", "structured-city", "structured-duplicate", "challenge"]) {
    test(`Avison field-aware ${row.key} refuses ${mutation}`, () => {
      const base = { ...row };
      const $ = cheerio.load(row.html);
      if (mutation === "number") base.street = "99999 Other Highway";
      if (mutation === "range") base.street = row.street.replace(/^\d+/, "$&-99999");
      if (mutation === "unit") base.street += " Suite 99";
      if (mutation === "city") base.city = "Street Louis";
      if (mutation === "state") base.state = "AK";
      if (mutation === "zip") $(".hero__sub-title").append(" 00000");
      if (mutation === "wrong-h1") $("h1").text("99999 Other Property");
      if (mutation === "title") $("title").text("Other Property");
      if (mutation === "og-title") $('meta[property="og:title"]').attr("content", "Other Property");
      if (mutation === "og-url") $('meta[property="og:url"]').attr("content", "https://other.sharplaunch.com");
      if (mutation === "canonical") $("head").append('<link rel="canonical" href="https://other.sharplaunch.com">');
      if (mutation === "missing-hero") $(".hero__sub-title").remove();
      if (mutation === "duplicate-hero") $("body").append($(".hero__sub-title").clone());
      if (mutation === "duplicate-h1") $("body").append($("h1").clone());
      if (mutation === "conflicting-h2") $("body").append(`<h2>${row.street}, Other City, AK</h2>`);
      if (mutation.startsWith("structured")) {
        const record = { "@type": "RealEstateListing", name: mutation === "structured-name" ? "Other Property" : row.name, url: row.sharpLaunchUrl, address: { streetAddress: mutation === "structured-street" ? "99999 Other Highway" : row.street, addressLocality: mutation === "structured-city" ? "Other City" : row.city, addressRegion: row.state } };
        $("head").append(`<script type="application/ld+json">${JSON.stringify(record)}</script>`);
        if (mutation === "structured-duplicate") $("head").append(`<script type="application/ld+json">${JSON.stringify(record)}</script>`);
      }
      if (mutation === "challenge") $("body").append("Checking your browser");
      assert.throws(() => check($.html(), base), /identity|structure|challenge/);
    });
  }
}
test("Avison a page-content shell is not substantive property structure", () => {
  const row = cases.find(row => row.key === "property-sections")!;
  const $ = cheerio.load(row.html);
  $(".page-content").empty();
  assert.throws(() => check($.html(), row), /structure|identity/);
});
test("Avison Highway is not a city alias", () => {
  const row = cases.find(row => row.key === "highway")!;
  assert.throws(() => check(row.html, { ...row, city: "Flowery Highway" }), /identity/);
});

for (const row of cases.filter(row => ["discovery", "bellevue"].includes(row.key))) {
  for (const mutation of ["missing-description", "wrong-street", "wrong-name", "wrong-city", "wrong-state", "wrong-zip", "no-known-zip", "duplicate-description", "conflicting-description", "no-property-structure"]) {
    test(`Avison named description ${row.key} refuses ${mutation}`, () => {
      const $ = cheerio.load(row.html); const base = { ...row };
      const selectors = 'meta[name="description"], meta[name="twitter:description"], meta[property="og:description"]';
      if (mutation === "missing-description") $(selectors).remove();
      if (mutation === "wrong-street") $(selectors).attr("content", `For lease - ${row.name}, 999 Other Road, ${row.city}, ${row.state} ${row.postalCode}`);
      if (mutation === "wrong-name") $(selectors).attr("content", `For lease - Other Property, ${row.street}, ${row.city}, ${row.state} ${row.postalCode}`);
      if (mutation === "wrong-city") $(selectors).attr("content", `For lease - ${row.name}, ${row.street}, Other City, ${row.state} ${row.postalCode}`);
      if (mutation === "wrong-state") $(selectors).attr("content", `For lease - ${row.name}, ${row.street}, ${row.city}, AK ${row.postalCode}`);
      if (mutation === "wrong-zip") $(selectors).attr("content", `For lease - ${row.name}, ${row.street}, ${row.city}, ${row.state} 00000`);
      if (mutation === "no-known-zip") base.postalCode = "";
      if (mutation === "duplicate-description") $("head").append($('meta[name="description"]').clone());
      if (mutation === "conflicting-description") $("head").append('<meta property="og:description" content="Other Property, 999 Other Road, Other City, AK 00000">');
      if (mutation === "no-property-structure") $(".page-content").empty();
      assert.throws(() => check($.html(), base), /identity|structure/);
    });
  }
}
for (const city of ["St Louis", "St. Louis", "Saint Louis"]) {
  test(`Avison city role also binds title-location tail ${city}`, () => {
    const row = cases.find(row => row.key === "saint-city")!;
    const $ = cheerio.load(row.html);
    const title = `${row.name} - ${city}, MO`;
    $("title").text(title); $('meta[property="og:title"]').attr("content", title);
    assert.doesNotThrow(() => check($.html(), { ...row, city }));
  });
}
test("Avison redundant street-only heading retains Drive/Dr with independent complete location", () => {
  const original = cases.find(row => row.key === "property-sections")!;
  const row = { ...original, name: "3635 Knight Dr", street: "3635 Knight Dr" };
  const $ = cheerio.load(original.html.replace(/Knight (?:Road|Rd)/g, "Knight Drive"));
  $(".page-content").prepend('<h2 class="section-title__text">3635 Knight Drive</h2>');
  assert.doesNotThrow(() => check($.html(), row));
  for (const street of ["3636 Knight Dr", "3635-3636 Knight Dr", "3635 Knight Dr Suite 9"]) {
    assert.throws(() => check($.html(), { ...row, street }), /identity|structure/);
  }
});
test("Avison empty availability cards do not establish substantive property structure", () => {
 const row = cases.find(row => row.key === "property-sections")!; const $ = cheerio.load(row.html);
 $(".availability-card-v2").empty();
 assert.throws(() => check($.html(), row), /structure|identity/);
});
for (const suffix of ["Highway", "Drive"]) {
 test(`Avison registered official wrapper compares coherent structured street-name ${suffix}`, () => {
  const original = cases.find(row => row.key === "highway")!;
  const externalUrl = "https://www.avisonyoung.us/properties/field-role-fixture";
  const base = { ...original, name: `3572 Winder ${suffix}`, street: `3572 Winder ${suffix}`, externalUrl };
  const $ = cheerio.load(original.html.replace(/Winder (?:Highway|Hwy)/g, `Winder ${suffix}`));
  $('meta[property="og:url"]').attr("content", externalUrl);
  const record = { "@type": "RealEstateListing", name: base.name, url: externalUrl, address: { streetAddress: base.street, addressLocality: base.city, addressRegion: base.state } };
  function official(value: typeof record) {
   const page = cheerio.load($.html()); page("head").append(`<script type="application/ld+json">${JSON.stringify(value)}</script>`);
   return assertAvisonYoungDetailDoc({ rawHtml: page.html(), markdown: "", links: [], metadata: { statusCode: 200, sourceURL: externalUrl } }, externalUrl, base);
  }
  assert.doesNotThrow(() => official(record));
  assert.throws(() => official({ ...record, name: "Different Property" }), /identity/);
  assert.throws(() => official({ ...record, address: { ...record.address, streetAddress: "999 Other Highway" } }), /identity/);
  assert.throws(() => official({ ...record, address: { ...record.address, addressLocality: "Other City" } }), /identity/);
 });
}
const directionalCases = { North: "N", Northeast: "NE", East: "E", Southeast: "SE", South: "S", Southwest: "SW", West: "W", Northwest: "NW" };
for (const [word, abbreviation] of Object.entries(directionalCases)) {
 test(`Avison only terminal street postdirection ${word}/${abbreviation} compares equally`, () => {
  const original = cases.find(row => row.key === "property-sections")!;
  const row = { ...original, name: `3635 Knight Rd ${abbreviation}`, street: `3635 Knight Rd ${abbreviation}` };
  const $ = cheerio.load(original.html.replace(/Knight (?:Road|Rd)/g, `Knight Road ${word}`));
  assert.doesNotThrow(() => check($.html(), row));
  for (const street of [`3636 Knight Rd ${abbreviation}`, `3635-3636 Knight Rd ${abbreviation}`, `3635 Knight Rd ${abbreviation} Suite 9`, `3635 Knight Rd ${abbreviation === "N" ? "S" : "N"}`]) {
   assert.throws(() => check($.html(), { ...row, street }), /identity|structure/);
  }
  $("body").append(`<h2>3635 Knight Road ${word}, Other City, AK</h2>`);
  assert.throws(() => check($.html(), row), /identity|structure/);
 });
}
test("Avison interior direction words and opposing pairs are not rewritten", () => {
 const original = cases.find(row => row.key === "property-sections")!;
 for (const [observed, expected] of [["West Knight Road", "W Knight Rd"], ["Knight Road North-South", "Knight Rd North S"], ["Knight Road East-West", "Knight Rd East W"]]) {
  const html = original.html.replace(/Knight (?:Road|Rd)/g, observed!);
  assert.throws(() => check(html, { ...original, name: `3635 ${expected}`, street: `3635 ${expected}` }), /identity|structure/);
 }
});
test("Avison named property complete visible address does not need name equal street", () => {
 const original = cases.find(row => row.key === "property-sections")!; const $ = cheerio.load(original.html);
 const row = { ...original, name: "Knight Business Park" };
 $("h1").text(row.name); $("title").text(row.name); $('meta[property="og:title"]').attr("content", row.name);
 $(".hero__sub-title").text(`${row.street}, ${row.city}, ${row.state} ${row.postalCode}`);
 $('meta[name="description"],meta[name="twitter:description"],meta[property="og:description"]').remove();
 assert.doesNotThrow(() => check($.html(), row));
 for (const hero of [`999 Knight Rd, ${row.city}, ${row.state}`, `${row.street}, Other City, ${row.state}`, `${row.street}, ${row.city}, AK`, `${row.street}, ${row.city}, ${row.state} 00000`, `${row.city}, ${row.state}`]) {
  const page = cheerio.load($.html()); page(".hero__sub-title").text(hero);
  assert.throws(() => check(page.html(), row), /identity|structure/);
 }
});
test("Avison expected name/street tuple preserves embedded commas and rejects ambiguous boundaries", () => {
 const original = cases.find(row => row.key === "discovery")!; const $ = cheerio.load(original.html);
 const row = { ...original, name: "710 Beaver Valley Mall, Lot 8", street: "710, Lot 8 Beaver Valley Mall" };
 $("h1").text(row.name); $("title").text(row.name); $('meta[property="og:title"]').attr("content", row.name);
 const selectors = 'meta[name="description"],meta[name="twitter:description"],meta[property="og:description"]';
 const description = `For lease - ${row.name}, ${row.street}, ${row.city}, ${row.state} ${row.postalCode}`;
 $(selectors).attr("content", description);
 assert.doesNotThrow(() => check($.html(), row));
 for (const wrong of [description.replace("710, Lot", "711, Lot"), description.replace("710, Lot", "710-711, Lot"), description.replace("710, Lot", "710, Unit 9, Lot"), description.replace("Lot 8", "Lot 9"), description.replace(`, ${row.street}`, `,, ${row.street}`)]) {
  const page = cheerio.load($.html()); page(selectors).attr("content", wrong);
  assert.throws(() => check(page.html(), row), /identity|structure/);
 }
});
test("Avison a directional unit value is not a street postdirection", () => {
 const original = cases.find(row => row.key === "property-sections")!;
 const html = original.html.replace(/Knight (?:Road|Rd)/g, "Knight Road Suite West");
 assert.throws(() => check(html, { ...original, name: "3635 Knight Rd Suite W", street: "3635 Knight Rd Suite W" }), /identity|structure/);
});
for (const [observed, expected] of [["Suite 200 West", "Suite 200 W"], ["#200 West", "#200 W"]]) {
 test(`Avison preserves the complete multi-token unit identifier ${observed}`, () => {
  const original = cases.find(row => row.key === "property-sections")!;
  const html = original.html.replace(/Knight (?:Road|Rd)/g, `Knight Road ${observed}`);
  assert.throws(() => check(html, { ...original, name: `3635 Knight Rd ${expected}`, street: `3635 Knight Rd ${expected}` }), /identity|structure/);
 });
}
test("Avison postdirection street-name comparison also applies before title city/state", () => {
 const original = cases.find(row => row.key === "property-sections")!;
 const row = { ...original, name: "3635 Knight Road West", street: "3635 Knight Road West" };
 const $ = cheerio.load(original.html.replace(/Knight (?:Road|Rd)/g, "Knight Road West"));
 $("title").text(`${row.name}, ${row.city}, ${row.state}`); $('meta[property="og:title"]').attr("content", `${row.name}, ${row.city}, ${row.state}`);
 assert.doesNotThrow(() => check($.html(), row));
 for (const title of [`3635 Knight Road East, ${row.city}, ${row.state}`, `${row.name}, Other City, ${row.state}`, `${row.name}, ${row.city}, AK`]) {
  const page = cheerio.load($.html()); page("title").text(title); page('meta[property="og:title"]').attr("content", title);
  assert.throws(() => check(page.html(), row), /identity|structure/);
 }
});
