// Isolate argv before cbre.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  cbreAspect,
  cbreListingSlug,
  cbreListingUrl,
  cbreBrochureUrl,
  cbrePhotoUrl,
  cbreTransactionType,
  cbreStrandedStructured,
  cbreDocTypeFromName,
  cbreNewFieldsFromRawData,
} from "../../../sources/cbre.js";

// ---------------------------------------------------------------------------
// Fixture helpers
// ---------------------------------------------------------------------------

const __filename = fileURLToPath(import.meta.url);
const __dir = dirname(__filename);
const FIXTURE_PATH = join(__dir, "../../fixtures/raw_data/cbre.json");

function loadFixture(): Array<{ _comment?: string; external_id: string; sourceKey: string; raw_data: any }> {
  return JSON.parse(readFileSync(FIXTURE_PATH, "utf8"));
}

test("cbreAspect maps transaction to API aspect", () => {
  assert.equal(cbreAspect("sale"), "isSale");
  assert.equal(cbreAspect("lease"), "isLetting");
});

test("cbreListingSlug lowercases and hyphenates address parts", () => {
  assert.equal(
    cbreListingSlug({
      name: "Midtown Tower",
      street: "123 Main St",
      city: "Dallas",
      state: "TX",
      zip: "75201",
    }),
    "midtown-tower-123-main-st-dallas-tx-75201"
  );
  assert.equal(
    cbreListingSlug({ name: "Retail Pad", street: null, city: "Austin", state: "TX", zip: null }),
    "retail-pad-austin-tx"
  );
  assert.equal(cbreListingSlug({ name: null, street: null, city: null, state: null, zip: null }), "");
});

test("cbreListingUrl builds CBRE detail path", () => {
  assert.equal(
    cbreListingUrl("US-SMPL-160329", "midtown-tower-dallas-tx"),
    "https://www.cbre.com/properties/properties-for-lease/commercial-space/details/US-SMPL-160329/midtown-tower-dallas-tx"
  );
});

test("cbreBrochureUrl resolves absolute and relative URIs", () => {
  assert.equal(cbreBrochureUrl("https://cdn.cbre.com/brochure.pdf"), "https://cdn.cbre.com/brochure.pdf");
  assert.equal(cbreBrochureUrl("/resources/fileassets/US-SMPL/brochure.pdf"), "https://www.cbre.com/resources/fileassets/US-SMPL/brochure.pdf");
  assert.equal(cbreBrochureUrl(null), "https://www.cbre.com");
});

test("cbrePhotoUrl resolves absolute and relative resource URIs", () => {
  assert.equal(cbrePhotoUrl("https://images.cbre.com/photo.jpg"), "https://images.cbre.com/photo.jpg");
  assert.equal(cbrePhotoUrl("/resources/photos/abc.jpg"), "https://www.cbre.com/resources/photos/abc.jpg");
  assert.equal(cbrePhotoUrl(null), null);
  assert.equal(cbrePhotoUrl(""), null);
});

test("cbreTransactionType maps aspect flags", () => {
  assert.equal(cbreTransactionType(["isSale"]), "Sale");
  assert.equal(cbreTransactionType(["isLetting"]), "Lease");
  assert.equal(cbreTransactionType(["isSale", "isLetting"]), "Sale/Lease");
  assert.equal(cbreTransactionType([]), "Sale");
});

test("cbreDocTypeFromName classifies brochure names into doc types", () => {
  assert.equal(cbreDocTypeFromName("Offering Memorandum"), "om");
  assert.equal(cbreDocTypeFromName("Rent Roll"), "rent_roll");
  assert.equal(cbreDocTypeFromName("Financial Summary"), "financials");
  assert.equal(cbreDocTypeFromName("Floor Plan - Level 2"), "floor_plan");
  assert.equal(cbreDocTypeFromName("Property Flyer"), "flyer");
  assert.equal(cbreDocTypeFromName("Marketing Package"), "brochure");
  assert.equal(cbreDocTypeFromName(null), "brochure");
});

test("cbreStrandedStructured lifts cap rate (Charges) + Dynamic fields; empty for sparse", () => {
  const out = cbreStrandedStructured({
    "Common.Charges": [
      { "Common.ChargeKind": "SalePrice", "Common.Amount": 12000000 },
      { "Common.ChargeKind": "CapRate", "Common.Amount": 6.5 },
    ],
    "Dynamic.YearBuilt": 2001,
    "Dynamic.NumberOfFloors": 8,
    "Dynamic.NumberOfUnits": 120,
    "Dynamic.Zoning": "C-2",
  });
  assert.equal(out.capRatePct, 6.5);
  assert.equal(out.yearBuilt, 2001);
  assert.equal(out.floors, 8);
  assert.equal(out.units, 120);
  assert.equal(out.zoning, "C-2");
  assert.deepEqual(cbreStrandedStructured({}), {});
});

// ---------------------------------------------------------------------------
// WS1: cbreNewFieldsFromRawData - fixture-driven tests
// ---------------------------------------------------------------------------

test("cbreNewFieldsFromRawData: lease row with USD/SF/MO rate annualizes to leaseRateMin", () => {
  const fixture = loadFixture();
  // First fixture: 3.59 USD/SF/MO lease row
  const row = fixture.find((r) => r.external_id === "US-SMPL-196821")!;
  assert.ok(row, "fixture row US-SMPL-196821 must exist");
  const out = cbreNewFieldsFromRawData(row.raw_data);
  // canonicalUrl from url
  assert.equal(
    out.canonicalUrl,
    "https://www.cbre.com/properties/properties-for-lease/commercial-space/details/US-SMPL-196821/5-medical-plaza-drive-5-medical-plaza-drive-suite-200-roseville-ca-95661"
  );
  // highlights from headline
  assert.equal(out.highlights, "±2,259 SF Sublease Available | Medical Space");
  // 3.59 USD/SF/MO -> annualized = 3.59 * 12 = 43.08 $/SF/yr
  assert.ok(out.leaseRateMin !== null, "leaseRateMin must be non-null for SF/MO text");
  assert.ok(Math.abs((out.leaseRateMin ?? 0) - 43.08) < 0.01, `expected ~43.08, got ${out.leaseRateMin}`);
  assert.equal(out.leaseRateMax, null);
  assert.equal(out.leaseRateType, null);
});

test("cbreNewFieldsFromRawData: second lease row with 4.5 USD/SF/MO", () => {
  const fixture = loadFixture();
  const row = fixture.find((r) => r.external_id === "US-SMPL-198939")!;
  assert.ok(row, "fixture row US-SMPL-198939 must exist");
  const out = cbreNewFieldsFromRawData(row.raw_data);
  // canonicalUrl
  assert.ok(out.canonicalUrl?.includes("US-SMPL-198939"), "canonicalUrl must contain external_id");
  // highlights
  assert.equal(out.highlights, "±50,271 SF INDUSTRIAL BUILDING | FOR LEASE");
  // 4.5 USD/SF/MO -> 4.5 * 12 = 54.0 $/SF/yr
  assert.ok(out.leaseRateMin !== null, "leaseRateMin must be non-null");
  assert.ok(Math.abs((out.leaseRateMin ?? 0) - 54.0) < 0.01, `expected ~54.0, got ${out.leaseRateMin}`);
  assert.equal(out.leaseRateMax, null);
  assert.equal(out.leaseRateType, null);
});

test("cbreNewFieldsFromRawData: null/absent leaseRateText yields null lease rate fields", () => {
  const out = cbreNewFieldsFromRawData({ url: "https://www.cbre.com/properties/x", headline: null });
  assert.equal(out.leaseRateMin, null);
  assert.equal(out.leaseRateMax, null);
  assert.equal(out.leaseRateType, null);
  assert.equal(out.highlights, null);
  assert.equal(out.canonicalUrl, "https://www.cbre.com/properties/x");
});

test("cbreNewFieldsFromRawData: null input does not throw", () => {
  assert.doesNotThrow(() => {
    const out = cbreNewFieldsFromRawData(null);
    assert.equal(out.canonicalUrl, null);
    assert.equal(out.highlights, null);
    assert.equal(out.leaseRateMin, null);
  });
});

test("cbreNewFieldsFromRawData: NNN lease rate type is parsed", () => {
  const out = cbreNewFieldsFromRawData({ leaseRateText: "$22 - $26 PSF NNN", url: null, headline: null });
  assert.equal(out.leaseRateType, "nnn");
  assert.equal(out.leaseRateMin, 22);
  assert.equal(out.leaseRateMax, 26);
});
