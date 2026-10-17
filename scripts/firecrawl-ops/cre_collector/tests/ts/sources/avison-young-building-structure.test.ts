import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as cheerio from "cheerio";
import { assertAvisonYoungDetailDoc, enrichAvisonYoungListing } from "../../../sources/avison-young.js";
import type { ScrapedDoc } from "../../../types.js";

// Reduced observed native DOM: identity, Building overview/facts and availability
// remain. Scripts, contacts, assets and unrelated sections are omitted.
const cases = [
  {
    "ordinal": 170,
    "name": "160 State Street",
    "street": "160 State Street",
    "city": "Boston",
    "state": "MA",
    "postalCode": "02109",
    "sharpLaunchUrl": "https://ayus160statestreet.sharplaunch.com",
    "expected": true
  },
  {
    "ordinal": 226,
    "name": "620 Parkway Drive",
    "street": "620 Parkway Drive",
    "city": "Broomall",
    "state": "PA",
    "postalCode": "19008",
    "sharpLaunchUrl": "https://ayus620parkwaydrive.sharplaunch.com",
    "expected": true
  },
  {
    "ordinal": 256,
    "name": "421 Chestnut Street",
    "street": "421 Chestnut Street",
    "city": "Philadelphia",
    "state": "PA",
    "postalCode": "19106",
    "sharpLaunchUrl": "https://ayus421chestnutstreet.sharplaunch.com",
    "expected": true
  },
  {
    "ordinal": 324,
    "name": "43 Leopard Road",
    "street": "43 Leopard Road",
    "city": "Paoli",
    "state": "PA",
    "postalCode": "19301",
    "sharpLaunchUrl": "https://ayus43leopardroad.sharplaunch.com",
    "expected": true
  },
  {
    "ordinal": 130,
    "name": "Clark Tower Sublease",
    "street": "5100 Poplar Ave",
    "city": "Memphis",
    "state": "TN",
    "postalCode": "38137",
    "sharpLaunchUrl": "https://ayusclarktowersublease.sharplaunch.com",
    "expected": false
  },
  {
    "ordinal": 177,
    "name": "Delp Distribution Center",
    "street": "4500 Delp St",
    "city": "Memphis",
    "state": "TN",
    "postalCode": "38118",
    "sharpLaunchUrl": "https://ayusdelpdistributioncenter.sharplaunch.com",
    "expected": false
  },
  {
    "ordinal": 62972,
    "name": "2.0 Acres of Land for Sale",
    "street": "451 West Mesquite Boulevard",
    "city": "Mesquite",
    "state": "NV",
    "postalCode": "89027",
    "sharpLaunchUrl": "https://20acresoflandforsale.sharplaunch.com",
    "expected": true
  },
  {
    "ordinal": 62973,
    "name": "2.17 Acres of Land for Sale",
    "street": "454 West Mesquite Boulevard",
    "city": "Mesquite",
    "state": "NV",
    "postalCode": "89027",
    "sharpLaunchUrl": "https://217acresoflandforsale.sharplaunch.com",
    "expected": true
  },
  {
    "ordinal": 62971,
    "name": "0.97 Acres of Land for Sale",
    "street": "421 West Mesquite Boulevard",
    "city": "Mesquite",
    "state": "NV",
    "postalCode": "89027",
    "sharpLaunchUrl": "https://097acresforsale.sharplaunch.com",
    "expected": false
  }
].map(row => ({
  ...row,
  html: readFileSync(new URL(`../../fixtures/raw_data/avison-young-building-${row.ordinal}.html`, import.meta.url), "utf8"),
}));
function doc(row: typeof cases[number], html = row.html): ScrapedDoc {
  return { rawHtml: html, markdown: "", links: [], metadata: { statusCode: 200, sourceURL: row.sharpLaunchUrl } };
}
function check(row: typeof cases[number], html = row.html) {
  return assertAvisonYoungDetailDoc(doc(row, html), row.sharpLaunchUrl, row);
}
for (const row of cases) {
  if (!row.expected) {
    test(`Avison Building actual conflicting ${row.ordinal} stays refused`, () => {
      assert.throws(() => check(row), /identity|structure/);
    });
    continue;
  }
  test(`Avison Building observed ${row.ordinal} accepts unchanged identity and facts`, () => {
    const before = structuredClone(row);
    assert.doesNotThrow(() => check(row));
    assert.deepEqual(row, before);
  });
  test(`Avison Building ${row.ordinal} actual enrichment retains base fields and detail text`, async () => {
    const previous = process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
    process.env.AVISON_YOUNG_DETAIL_TRANSPORT = "direct";
    try {
      let calls = 0;
      const base = { ...row, id: `building-${row.ordinal}`, salePriceUsd: 2300000, leaseRate: 20,
        photos: ["https://images.example.test/target.jpg"], rawSharpLaunch: { retained: "source facts" } };
      const before = structuredClone(base);
      const result = await enrichAvisonYoungListing(base, false, async (url) => {
        calls++;
        assert.equal(url, row.sharpLaunchUrl);
        return { ...doc(row), markdown: "Complete observed property text", metadata: { ...doc(row).metadata, transport: "direct_http" } };
      });
      assert.equal(calls, 1);
      assert.equal(result.detailError, undefined);
      assert.equal(result.markdown, "Complete observed property text");
      for (const key of ["id", "name", "street", "city", "state", "postalCode", "salePriceUsd", "leaseRate", "rawSharpLaunch"])
        assert.deepEqual(result[key], base[key as keyof typeof base]);
      assert.ok(result.photos.some((photo: any) => (typeof photo === "string" ? photo : photo.url) === base.photos[0]));
      assert.deepEqual(base, before);
    } finally {
      if (previous === undefined) delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
      else process.env.AVISON_YOUNG_DETAIL_TRANSPORT = previous;
    }
  });
  for (const mutation of [
    "empty-overview", "whitespace-overview", "heading-only-overview", "empty-facts",
    "overview-only", "outside-building", "number", "range", "unit", "city", "state",
    "zip", "h1", "title", "og-url", "canonical", "structured", "challenge",
  ]) {
    test(`Avison Building ${row.ordinal} refuses ${mutation}`, () => {
      const base = { ...row };
      const $ = cheerio.load(row.html);
      const facts = ".building__property-features-boxes .property-features-box > .px-body-2";
      const fields = ".availability-card-info-item-title, .availability-card-info-item-value";
      if (mutation === "empty-overview") $(".building__overview-text").empty();
      if (mutation === "whitespace-overview") $(".building__overview-text").text("   ");
      if (mutation === "heading-only-overview") $(".building__overview-text").html("<h2>Overview</h2>");
      if (mutation === "empty-facts") { $(facts).text(" "); $(fields).text(" "); }
      if (mutation === "overview-only") { $(".building__property-features-boxes, .availability-card-v2").remove(); }
      if (mutation === "outside-building") $("section#building").attr("id", "unrelated");
      if (mutation === "number") base.street = "99999 Other Street";
      if (mutation === "range") base.street += "-99999";
      if (mutation === "unit") base.street += " Suite 99";
      if (mutation === "city") base.city = "Other City";
      if (mutation === "state") base.state = "AK";
      if (mutation === "zip") base.postalCode = "00000";
      if (mutation === "h1") $("h1").text("Other Property");
      if (mutation === "title") $("title").text("Other Property");
      if (mutation === "og-url") $('meta[property="og:url"]').attr("content", "https://other.sharplaunch.com");
      if (mutation === "canonical") $("head").append('<link rel="canonical" href="https://other.sharplaunch.com">');
      if (mutation === "structured") {
        $("head").append('<script type="application/ld+json">' + JSON.stringify({
          "@type": "RealEstateListing", name: "Other Property", url: row.sharpLaunchUrl,
        }) + '</script>');
      }
      if (mutation === "challenge") $("body").append("Verify you are human");
      assert.throws(() => check(base, $.html()), /identity|structure|challenge/);
    });
  }
}

// Existing complete-visible identity keeps unknown ZIPs distinct from a known
// contradiction. These observed Building pages use marketing SEO descriptions.
for (const row of cases.filter(row => row.ordinal === 62972 || row.ordinal === 62973)) {
  const legacyInfo = (html: string) => {
    const $ = cheerio.load(html);
    const fact = $(".property-features-box > .px-body-2").first().text();
    assert.ok(fact.trim());
    const box = $('<div class="building__property-info-box"><li><span>Property feature</span><span class="actual-fact"></span></li></div>');
    box.find(".actual-fact").text(fact);
    $("section#building .building").append(box);
    return $.html();
  };
  for (const missing of ["feed-zip", "hero-zip"]) {
    test(`Avison visible Building ${row.ordinal} retains existing ${missing} semantics`, () => {
      const base = { ...row };
      const $ = cheerio.load(row.html);
      if (missing === "feed-zip") base.postalCode = "";
      else $(".hero__sub-title").text($(".hero__sub-title").text().replace(row.postalCode, ""));
      assert.doesNotThrow(() => check(base, legacyInfo($.html())));
      assert.doesNotThrow(() => check(base, $.html()));
    });
  }
  test(`Avison visible Building ${row.ordinal} city-only hero needs the full description witness`, () => {
    const $ = cheerio.load(row.html);
    $(".hero__sub-title").text(`${row.city}, ${row.state}`);
    assert.throws(() => check(row, $.html()), /identity|structure/);
    const full = `${row.name}, ${row.street}, ${row.city}, ${row.state} ${row.postalCode}`;
    $('meta[name="description"], meta[name="twitter:description"], meta[property="og:description"]').attr("content", full);
    assert.doesNotThrow(() => check(row, $.html()));
    assert.throws(() => check({ ...row, postalCode: "" }, $.html()), /identity|structure/);
    assert.throws(() => check({ ...row, postalCode: "00000" }, $.html()), /identity|structure/);
  });
  for (const mutation of ["og-title", "canonical-query", "duplicate-structured", "structured-address"]) {
    test(`Avison visible Building ${row.ordinal} refuses ${mutation}`, () => {
      const $ = cheerio.load(row.html);
      if (mutation === "og-title") $('meta[property="og:title"]').attr("content", "Unrelated Property");
      if (mutation === "canonical-query") $("head").append(`<link rel="canonical" href="${row.sharpLaunchUrl}?foreign=1">`);
      if (mutation === "duplicate-structured") {
        const record = { "@type": "RealEstateListing", name: row.name, url: row.sharpLaunchUrl };
        $("head").append('<script type="application/ld+json">' + JSON.stringify([record, record]) + '</script>');
      }
      if (mutation === "structured-address") $("head").append('<script type="application/ld+json">' + JSON.stringify({
        "@type": "RealEstateListing", name: row.name, url: row.sharpLaunchUrl,
        address: { streetAddress: "999 Other Street", addressLocality: row.city, addressRegion: row.state, postalCode: row.postalCode },
      }) + '</script>');
      assert.throws(() => check(row, $.html()), /identity|structure/);
    });
  }
}

// Diagnostic categories distinguish populated native property content without a
// complete identity witness from an empty or unrecognized template.
test("Avison visible Building actual 62971 reports identity refusal", () => {
  const row = cases.find(row => row.ordinal === 62971)!;
  assert.throws(() => check(row), {
    name: "Error", message: "Avison Young detail identity does not match the requested property",
  });
});
for (const row of cases.filter(row => row.ordinal === 62972 || row.ordinal === 62973)) {
  for (const text of ["", "   "]) {
    test(`Avison visible Building ${row.ordinal} ${JSON.stringify(text)} overview reports structure refusal`, () => {
      const $ = cheerio.load(row.html);
      $(".building__overview-text p, .building__overview-text li").text(text);
      assert.throws(() => check(row, $.html()), {
        name: "Error", message: "Avison Young detail structure was not recognized",
      });
    });
  }
}
