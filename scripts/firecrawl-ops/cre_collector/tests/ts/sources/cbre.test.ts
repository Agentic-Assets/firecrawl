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
  assertCbrePage,
  assertCbreAggregate,
  cbreResultTruncated,
  srcCbre,
} from "../../../sources/cbre.js";
import { firecrawl } from "../../../lib/scrape.js";

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

test("CBRE finite caps report truncation against DocumentCount", () => {
  assert.equal(cbreResultTruncated(1, 2, 2), true);
  assert.equal(cbreResultTruncated(2, 2, 2), false);
  assert.equal(cbreResultTruncated(Number.POSITIVE_INFINITY, 2, 2), false);
  assert.equal(cbreResultTruncated(Number.POSITIVE_INFINITY, 2, 1), true);
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

test("strict CBRE pages require stable totals, exact cardinality, and unique identities", () => {
  const page = assertCbrePage(
    {
      DocumentCount: 3,
      Documents: [
        { "Common.PrimaryKey": "CBRE-1" },
        { "Common.PrimaryKey": "CBRE-2" },
      ],
    },
    1,
    2,
    null,
    true
  );
  assert.equal(page.total, 3);
  assert.deepEqual(page.documents.map((row: any) => row["Common.PrimaryKey"]), [
    "CBRE-1",
    "CBRE-2",
  ]);
  assert.throws(
    () =>
      assertCbrePage(
        { DocumentCount: 3, Documents: [{ "Common.PrimaryKey": "CBRE-3" }] },
        2,
        2,
        4,
        true
      ),
    /changed from 4 to 3/
  );
  assert.throws(
    () => assertCbrePage({ DocumentCount: 2.5, Documents: [] }, 1, 2, null, true),
    /nonnegative integer DocumentCount/
  );
  assert.throws(
    () =>
      assertCbrePage(
        { DocumentCount: 2, Page: 2, PageSize: 2, Documents: [] },
        1,
        2,
        null,
        true
      ),
    /page metadata expected page 1/
  );
  assert.throws(
    () =>
      assertCbrePage(
        { DocumentCount: 2, Documents: [{ "Common.PrimaryKey": "CBRE-1" }] },
        1,
        2,
        null,
        true
      ),
    /expected 2 documents, received 1/
  );
  assert.throws(
    () =>
      assertCbrePage(
        {
          DocumentCount: 2,
          Documents: [
            { "Common.PrimaryKey": "CBRE-1" },
            { "Common.PrimaryKey": "CBRE-1" },
          ],
        },
        1,
        2,
        null,
        true
      ),
    /duplicate Common.PrimaryKey/
  );
  assert.throws(
    () =>
      assertCbrePage(
        {
          DocumentCount: 1,
          Documents: [{ "Common.PrimaryKey": " " }],
        },
        1,
        2,
        null,
        true
      ),
    /nonempty Common.PrimaryKey/
  );
});

test("strict CBRE aggregate reconciliation rejects duplicate or missing provider rows", () => {
  const rows = [
    { "Common.PrimaryKey": "CBRE-1" },
    { "Common.PrimaryKey": "CBRE-2" },
    { "Common.PrimaryKey": "CBRE-3" },
  ];
  assert.doesNotThrow(() => assertCbreAggregate(rows, 3, 2, true));
  assert.throws(
    () => assertCbreAggregate(rows.slice(0, 2), 3, 2, true),
    /expected 3 unique documents, received 2/
  );
  assert.throws(
    () => assertCbreAggregate([rows[0], rows[0], rows[2]], 3, 2, true),
    /duplicate Common.PrimaryKey/
  );
  assert.doesNotThrow(() => assertCbreAggregate(rows.slice(0, 2), 3, 2, false));
});

test("strict CBRE requires an explicit refresh generation", async (t) => {
  const originalStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const originalGeneration = process.env.CRE_REFRESH_GENERATION;
  process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
  delete process.env.CRE_REFRESH_GENERATION;
  t.after(() => {
    if (originalStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = originalStrict;
    if (originalGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = originalGeneration;
  });
  await assert.rejects(
    () => srcCbre("sale", 1, false),
    /requires CRE_REFRESH_GENERATION/
  );
});

test("strict CBRE fetches every page uncached and stamps authoritative provenance", async (t) => {
  const originalScrape = firecrawl.scrape;
  const originalStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const originalGeneration = process.env.CRE_REFRESH_GENERATION;
  const calls: any[] = [];
  const rows = Array.from({ length: 201 }, (_, index) => ({
    "Common.PrimaryKey": `CBRE-${index + 1}`,
    "Common.ActualAddress": {
      "Common.Line1": `Property ${index + 1}`,
      "Common.Line2": `${index + 1} Main Street`,
      "Common.Locallity": "Dallas",
      "Common.Region": "TX",
      "Common.PostCode": "75201",
      "Common.Country": "US",
    },
    "Common.Aspects": ["isSale"],
  }));
  (firecrawl as any).scrape = async (url: string, options: any) => {
    calls.push({ url, options });
    const page = Number(new URL(url).searchParams.get("Page"));
    return {
      rawHtml: JSON.stringify({
        DocumentCount: rows.length,
        Documents: page === 1 ? rows.slice(0, 200) : rows.slice(200),
      }),
    };
  };
  process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
  process.env.CRE_REFRESH_GENERATION = "cbre-strict-test";
  t.after(() => {
    (firecrawl as any).scrape = originalScrape;
    if (originalStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = originalStrict;
    if (originalGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = originalGeneration;
  });

  const result = await srcCbre("sale", Number.POSITIVE_INFINITY, false);
  assert.equal(result.listings.length, 201);
  assert.equal(calls.length, 2);
  assert.ok(calls.every(({ options }) => options.maxAge === 0));
  for (const listing of result.listings) {
    assert.equal(listing.detailObservedAt, listing.inventoryObservedAt);
    assert.equal(listing.freshnessProvenance.generationId, "cbre-strict-test");
    assert.equal(
      listing.freshnessProvenance.detailScope,
      "authoritative_inventory_feed"
    );
    assert.equal(listing.freshnessProvenance.cacheDisposition, "live");
  }
});
