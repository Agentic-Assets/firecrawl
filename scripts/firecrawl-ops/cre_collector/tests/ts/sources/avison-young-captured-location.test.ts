import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as cheerio from "cheerio";
import { assertAvisonYoungDetailDoc } from "../../../sources/avison-young.js";
import type { ScrapedDoc } from "../../../types.js";

const kings = {
  id: "17353", name: "Kings Plaza Land",
  street: "S/SWC Bell Road & 59th Avenue", city: "Glendale", state: "AZ",
  postalCode: "85308", sharpLaunchUrl: "https://ayuskingsplazaland.sharplaunch.com",
  externalUrl: "https://www.avisonyoung.us/properties/kings-plaza-land",
};
const liberty = {
  id: "17336", name: "20 Liberty Lane", street: "20 Liberty Lane",
  city: "Greenville", state: "SC", postalCode: "29607",
  sharpLaunchUrl: "https://ayus20libertylane.sharplaunch.com",
  externalUrl: "https://www.avisonyoung.us/properties/20-liberty-lane-greenville-lease",
};
const cases = [
  { file: "002", base: kings, url: kings.sharpLaunchUrl },
  { file: "004", base: kings, url: kings.externalUrl },
  { file: "006", base: liberty, url: liberty.sharpLaunchUrl },
  { file: "007", base: liberty, url: liberty.externalUrl },
].map(row => ({ ...row, html: readFileSync(new URL(
  `../../fixtures/raw_data/avison-young-layout-${row.file}.html`, import.meta.url
), "utf8") }));

function doc(html: string, url: string): ScrapedDoc {
  return { rawHtml: html, markdown: "", links: [], metadata: { sourceURL: url, statusCode: 200 } };
}
for (const row of cases) {
  test(`Avison captured layout ${row.file} binds complete visible property identity`, () => {
    assert.doesNotThrow(() => assertAvisonYoungDetailDoc(doc(row.html, row.url), row.url, row.base));
  });
  for (const field of ["name", "street", "city", "state"] as const) {
    test(`Avison captured layout ${row.file} rejects wrong feed ${field}`, () => {
      assert.throws(() => assertAvisonYoungDetailDoc(doc(row.html, row.url), row.url, {
        ...row.base, [field]: field === "state" ? "TX" : "Unrelated property",
      }), /identity/);
    });
  }
  for (const mutation of ["title", "og-title", "og-url", "canonical", "duplicate-hero", "duplicate-h1", "challenge"]) {
    if (row.file === "002" && mutation === "duplicate-h1") continue;
    test(`Avison captured layout ${row.file} rejects ${mutation}`, () => {
      const $ = cheerio.load(row.html);
      if (mutation === "title") $("title").text("Unrelated property");
      if (mutation === "og-title") $("meta[property='og:title']").attr("content", "Unrelated property");
      if (mutation === "og-url") $("meta[property='og:url']").attr("content", "https://other.sharplaunch.com/");
      if (mutation === "canonical") $("head").append('<link rel="canonical" href="https://other.sharplaunch.com/">');
      if (mutation === "duplicate-hero") $("body").append($(".hero__sub-title").clone());
      if (mutation === "duplicate-h1") $("body").append("<h1>Another property</h1>");
      if (mutation === "challenge") $("body").append("Checking your browser");
      assert.throws(() => assertAvisonYoungDetailDoc(doc($.html(), row.url), row.url, row.base), /identity|challenge/);
    });
  }
}
for (const row of cases.filter(row => row.base === liberty)) {
  for (const location of ["Greenville, NC 29607", "Greenville, SC 29608", "Greenville, SC29607", "Greenville Heights, SC 29607"]) {
    test(`Avison ZIP location ${row.file} rejects ${location}`, () => {
      const $ = cheerio.load(row.html);
      $(".hero__sub-title").text(location);
      assert.throws(() => assertAvisonYoungDetailDoc(doc($.html(), row.url), row.url, row.base), /identity/);
    });
  }
  test(`Avison ZIP layout ${row.file} rejects street-number substring`, () => {
    assert.throws(() => assertAvisonYoungDetailDoc(doc(row.html, row.url), row.url, {
      ...row.base, street: "120 Liberty Lane",
    }), /identity/);
  });
}
for (const row of cases.filter(row => row.base === kings)) {
  for (const replacement of ["S/SWC Bell Rd & 159th Ave", "S/SWC Bell Rd & 59th AvenueExtra", "S/SWC Bell Rd & 59th Ave Unit 2", "S/SWC Bell Rd & 59th Ave, Tempe, AZ"]) {
    test(`Avison combined hero ${row.file} rejects altered intersection ${replacement}`, () => {
      const $ = cheerio.load(row.html);
      $(".hero__sub-title").text(`Kings Plaza Land | ${replacement}, Glendale, AZ`);
      assert.throws(() => assertAvisonYoungDetailDoc(doc($.html(), row.url), row.url, row.base), /identity/);
    });
  }
}

for (const row of cases.filter(row => row.base === liberty)) {
  test(`Avison ZIP layout ${row.file} retains unknown feed postal code`, () => {
    assert.doesNotThrow(() => assertAvisonYoungDetailDoc(doc(row.html, row.url), row.url, {
      ...row.base, postalCode: undefined,
    }));
  });
}
for (const row of cases.filter(row => row.url === row.base.externalUrl)) {
  test(`Avison wrapper ${row.file} rejects wrong or ambiguous structured identity`, () => {
    for (const field of ["name", "streetAddress", "addressLocality", "addressRegion", "url"]) {
      const $ = cheerio.load(row.html);
      const script = $("script[type='application/ld+json']");
      const record = JSON.parse(script.text());
      if (field === "name" || field === "url") record[field] = "Unrelated property";
      else record.address[field] = "Unrelated property";
      script.text(JSON.stringify(record));
      assert.throws(() => assertAvisonYoungDetailDoc(doc($.html(), row.url), row.url, row.base), /identity/);
    }
    const $ = cheerio.load(row.html);
    $("head").append($("script[type='application/ld+json']").clone());
    assert.throws(() => assertAvisonYoungDetailDoc(doc($.html(), row.url), row.url, row.base), /identity/);
  });
}

for (const row of cases.filter(row => row.base === kings)) {
  for (const location of ["Glendale, AZ 8530", "Glendale, AZ 85309", "Glendale, CA 85308"]) {
    test(`Avison combined ZIP ${row.file} rejects ${location}`, () => {
      const $ = cheerio.load(row.html);
      $(".hero__sub-title").text(`Kings Plaza Land | S/SWC Bell Rd & 59th Ave, ${location}`);
      assert.throws(() => assertAvisonYoungDetailDoc(doc($.html(), row.url), row.url, row.base), /identity/);
    });
  }
}
test("Avison legacy full-identity heading does not require a marketing H1", () => {
  const html = readFileSync(new URL(
    "../../fixtures/raw_data/avison-young-sharplaunch-building-detail.html", import.meta.url
  ), "utf8");
  const $ = cheerio.load(html);
  $("h1").remove();
  const url = "https://ayusalmaschoolcorporatecenteriii.sharplaunch.com/";
  assert.doesNotThrow(() => assertAvisonYoungDetailDoc(doc($.html(), url), url, {
    name: "Alma School Corporate Center III", street: "1839 S Alma School Road",
    city: "Mesa", state: "AZ", postalCode: "85210", sharpLaunchUrl: url,
  }));
});
