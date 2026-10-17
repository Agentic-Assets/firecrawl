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
