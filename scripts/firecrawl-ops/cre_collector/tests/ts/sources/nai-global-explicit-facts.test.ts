// Reduced observed Infabode1666069 table; synthetic mutations are named below.
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { naiListingFromFeed } from "../../../sources/nai-global.js";

const observed = readFileSync(
  new URL("../../fixtures/raw_data/nai-global-explicit-facts.html", import.meta.url),
  "utf8"
);
const cell = (label: string, value: string) =>
  `<tr><td><strong>${label}</strong></td><td>${value}</td></tr>`;
const table = (...rows: string[]) => `<table><tbody>${rows.join("")}</tbody></table>`;
const post = (content: unknown) => ({
  id: 1666069,
  title: "Observed property",
  content,
  contentType: { id: 4, name: "Sale Listings" },
  price: 600000,
  currency: "POUND",
  sizeTotal: 3800,
  tags: ["AutomotiveRepair"],
  urlOriginal: "https://www.naiglobal.com/listings/?propertyId=observed-sale",
  source: { name: "Observed NAI office" },
});
function map(content: unknown) {
  const raw = post(content);
  return naiListingFromFeed(raw, "sale", raw, null);
}
const absent = (value: unknown) => assert.ok(value == null);

test("NAI observed labeled table promotes numeric yearBuilt", () => {
  assert.equal(map(observed).yearBuilt, 1972);
});

test("NAI observed physical type is Retail, not publication Sale Listings", () => {
  assert.equal(map(observed).assetType, "Retail");
});

test("NAI unknown explicit physical type stays literal for the existing normalizer", () => {
  // Canonical norm_property_type intentionally maps this unknown literal to other.
  const listing = map(table(cell("Property Type:", "Unrecognized campus form")));
  assert.equal(listing.assetType, "Unrecognized campus form");
});

test("NAI mapping preserves raw publication/category and unrelated mapped facts", () => {
  const raw = post(observed);
  const snapshot = structuredClone(raw);
  const listing = naiListingFromFeed(raw, "sale", raw, null);
  assert.deepEqual(raw, snapshot);
  assert.deepEqual(listing.feedRow, snapshot);
  assert.deepEqual(listing.publicPost, snapshot);
  assert.deepEqual(listing.feedRow.contentType, { id: 4, name: "Sale Listings" });
  assert.equal(listing.salePriceUsd, 600000);
  assert.equal(listing.buildingSizeSqft, 3800);
  assert.equal(listing.canonicalUrl, raw.urlOriginal);
  assert.deepEqual(listing.highlights, ["AutomotiveRepair"]);
});

const missingFacts: Array<[string, unknown]> = [
  ["absent", undefined],
  ["null", null],
  ["empty", ""],
  ["nonstring", 42],
  ["narrative", "<p>Retail opportunity built in 1972.</p>"],
  ["unrelated labels", table(cell("Renovated:", "1972"), cell("Business Type:", "Retail"))],
];
for (const [name, content] of missingFacts) {
  test(`NAI ${name} supplies no explicit physical/year facts`, () => {
    const listing = map(content);
    absent(listing.yearBuilt);
    absent(listing.assetType);
  });
}

for (const value of [
  "0", "1700", "2100", "1972.5", "1972-1980", "built in 1972",
  "1972 (renovated 2020)", "", "01972",
]) {
  test(`NAI does not guess a year from ${JSON.stringify(value)}`, () => {
    absent(map(table(cell("Year Built:", value))).yearBuilt);
  });
}

test("NAI exact year bounds match canonical staging", () => {
  assert.equal(map(table(cell("Year Built:", "1701"))).yearBuilt, 1701);
  assert.equal(map(table(cell("Year Built:", "2099"))).yearBuilt, 2099);
});

test("NAI equivalent duplicate labels retain one fact across whitespace/case", () => {
  const listing = map(table(
    cell(" Year  Built : ", " 1972 "),
    cell("YEAR BUILT:", "1972"),
    cell("Property Type:", "Retail"),
    cell("property type:", " retail ")
  ));
  assert.equal(listing.yearBuilt, 1972);
  assert.equal(listing.assetType, "Retail");
});

for (const competingYear of ["1980", "unknown", "1972.5"]) {
  test(`NAI valid year plus ${competingYear} does not discard a conflict`, () => {
    const listing = map(table(
      cell("Year Built:", "1972"), cell("Year Built:", competingYear)
    ));
    absent(listing.yearBuilt);
  });
}

for (const competingType of ["Office", "Unrecognized campus form"]) {
  test(`NAI Retail plus ${competingType} does not pick a physical type`, () => {
    const listing = map(table(
      cell("Property Type:", "Retail"), cell("Property Type:", competingType)
    ));
    absent(listing.assetType);
  });
}

test("NAI direct-cell grammar ignores nested tables and unrelated extra cells", () => {
  const nested = table(cell("wrapper", table(
    cell("Year Built:", "1972"), cell("Property Type:", "Retail")
  )));
  const extra = "<table><tr><td>Year Built:</td><td>1972</td><td>1980</td></tr>"
    + "<tr><td></td><td>Retail</td></tr></table>";
  for (const content of [nested, extra]) {
    const listing = map(content);
    absent(listing.yearBuilt);
    absent(listing.assetType);
  }
});

test("NAI scalarSource/detail-error boundary is preserved", () => {
  const raw = post(observed);
  const noDetail = naiListingFromFeed(raw, "sale", null, null, false);
  absent(noDetail.yearBuilt);
  absent(noDetail.assetType);
  const failed = naiListingFromFeed(raw, "sale", raw, "detail failed", false);
  absent(failed.yearBuilt);
  absent(failed.assetType);
  const fallback = naiListingFromFeed(raw, "sale", null, null, true);
  assert.equal(fallback.yearBuilt, 1972);
  assert.equal(fallback.assetType, "Retail");
});

test("NAI bulk-detail monitor shape and lease retain the same explicit facts", () => {
  const raw = post(observed);
  raw.contentType = { id: 10, name: "Lease Listings" };
  const listing = naiListingFromFeed(raw, "lease", raw, null, false);
  assert.equal(listing.yearBuilt, 1972);
  assert.equal(listing.assetType, "Retail");
  assert.equal(listing.leaseRateMin, 600000);
  assert.equal(listing.salePriceUsd, undefined);
});
