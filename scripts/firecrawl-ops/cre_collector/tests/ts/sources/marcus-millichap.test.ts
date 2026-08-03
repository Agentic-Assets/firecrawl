import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  marcusUrl,
  parseMarcusLocation,
  parseMarcusAddress,
  extractCssUrl,
  parseMarcusTileHtml,
  parseMarcusScalars,
  parseMarcusContactLicense,
  appendMarcusDetailCache,
  assertMarcusInventoryCount,
  assertMarcusMapDetails,
  marcusCapTruncated,
  parseMarcusPropertiesResponse,
  parseMarcusMapRowsResponse,
  marcusDetailHtmlIsUsable,
  prepareMarcusDetailCache,
  readMarcusDetailCache,
} from "../../../sources/marcus-millichap.js";

// ---------------------------------------------------------------------------
// Fixture loader
// ---------------------------------------------------------------------------
const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = join(__dirname, "../../fixtures/raw_data/marcus-millichap.json");

function loadFixture(): any[] {
  return JSON.parse(readFileSync(FIXTURE_PATH, "utf8"));
}

// ---------------------------------------------------------------------------
// Existing parser tests (unchanged)
// ---------------------------------------------------------------------------

test("marcusUrl resolves relative property links", () => {
  assert.equal(
    marcusUrl("/properties/dallas-retail-123"),
    "https://www.marcusmillichap.com/properties/dallas-retail-123"
  );
  assert.equal(marcusUrl(""), null);
});

test("Marcus finite caps report truncation against map inventory", () => {
  assert.equal(marcusCapTruncated(1, 1, 2), true);
  assert.equal(marcusCapTruncated(2, 2, 2), false);
  assert.equal(marcusCapTruncated(Number.POSITIVE_INFINITY, 2, 3), false);
});

test("parseMarcusLocation splits city, state, and zip", () => {
  assert.deepEqual(parseMarcusLocation("Dallas, TX 75201"), {
    city: "Dallas",
    state: "TX",
    postalCode: "75201",
  });
  assert.deepEqual(parseMarcusLocation("Unparsed"), {
    city: "Unparsed",
    state: null,
    postalCode: null,
  });
});

test("parseMarcusAddress splits full street address", () => {
  assert.deepEqual(parseMarcusAddress("123 Main St, Dallas, TX 75201"), {
    street: "123 Main St",
    city: "Dallas",
    state: "TX",
    postalCode: "75201",
  });
  assert.deepEqual(parseMarcusAddress("bad"), {
    street: null,
    city: null,
    state: null,
    postalCode: null,
  });
});

test("extractCssUrl pulls URL from inline style", () => {
  assert.equal(extractCssUrl('background-image: url("https://cdn.example/img.jpg")'), "https://cdn.example/img.jpg");
  assert.equal(extractCssUrl("no url here"), null);
});

test("parseMarcusTileHtml maps tile markup and row fields", () => {
  const tileHtml = `
    <div class="mm-tile" data-dealid="D-99" data-activityid="ACT-1">
      <a href="/properties/sample-deal">
        <h2>Sample Retail Center</h2>
        <h3>Retail</h3>
        <div class="mm-location">Austin, TX 78701</div>
        <div class="mm-listing-price">Listing Price: $3,500,000</div>
        <div class="mm-cap-rate">6.25%</div>
        <img src="https://mmimageservice.azurewebsites.net/api/image/property/1.jpg" />
      </a>
    </div>
  `;
  const listing = parseMarcusTileHtml(tileHtml, {
    DealId: "D-99",
    PropertyName: "Row Name Override",
    ListingPrice: "$3,400,000",
    PropertyType: "Retail",
    City: "Austin",
    StateProvince: "TX",
    PostalCode: "78701",
    Latitude: 30.27,
    Longitude: -97.74,
    PropertyUrl: "/properties/sample-deal",
  });
  assert.equal(listing.id, "D-99");
  assert.equal(listing.activityId, "ACT-1");
  assert.equal(listing.name, "Row Name Override");
  assert.equal(listing.city, "Austin");
  assert.equal(listing.state, "TX");
  assert.equal(listing.salePriceUsd, 3500000);
  assert.equal(listing.capRatePct, 6.25);
  assert.ok(listing.photos?.[0]?.includes("mmimageservice"));
  assert.equal(listing.url, "https://www.marcusmillichap.com/properties/sample-deal");
});

test("marcusDetailHtmlIsUsable rejects challenge shells and requires property identity", () => {
  assert.equal(
    marcusDetailHtmlIsUsable("<html><h1>Sample Property</h1><div class='score-hero-body'>Dallas, TX</div></html>"),
    true
  );
  assert.equal(marcusDetailHtmlIsUsable("<html><h1>Access denied</h1><div>captcha</div></html>"), false);
  assert.equal(marcusDetailHtmlIsUsable("<html><h1>Generic page</h1></html>"), false);
});

test("Marcus detail cache accepts only the same attempt and unchanged provider identity", () => {
  const dir = mkdtempSync(join(tmpdir(), "marcus-cache-test-"));
  const cachePath = join(dir, "detail-cache.jsonl");
  const base = {
    id: "deal-1",
    activityId: "activity-1",
    url: "https://www.marcusmillichap.com/properties/deal-1",
    salePriceUsd: 1000000,
    rawMarcusSearchRow: { ActivityId: "activity-1", NewlyListed: true },
  };
  const enriched = { ...base, description: "Current detail" };
  try {
    prepareMarcusDetailCache(cachePath, "attempt-a");
    appendMarcusDetailCache(cachePath, "attempt-a", base, enriched);
    assert.equal(readMarcusDetailCache(cachePath, "attempt-a", [base]).get("deal-1")?.description, "Current detail");
    assert.equal(
      readMarcusDetailCache(cachePath, "attempt-a", [{ ...base, salePriceUsd: 1100000 }]).size,
      0
    );

    prepareMarcusDetailCache(cachePath, "attempt-b");
    assert.equal(readMarcusDetailCache(cachePath, "attempt-b", [base]).size, 0);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("Marcus inventory reconciliation rejects provider-total and map-detail gaps", () => {
  assert.doesNotThrow(() =>
    assertMarcusInventoryCount(
      2,
      [{ ActivityId: "a" }, { ActivityId: "b" }],
      true
    )
  );
  assert.throws(
    () =>
      assertMarcusInventoryCount(
        3,
        [{ ActivityId: "a" }, { ActivityId: "b" }],
        true
      ),
    /properties API reported 3, map API returned 2/
  );
  assert.throws(
    () =>
      assertMarcusMapDetails(
        [{ ActivityId: "a" }, { ActivityId: "b" }],
        [
          { activityId: "a", url: "https://www.marcusmillichap.com/properties/a" },
          { activityId: "b", detailError: "HTTP 503" },
        ]
      ),
    /map detail failed for 1\/2 ActivityIds \(b\)/
  );
});

test("strict Marcus inventory requires coherent finite TotalCount and response shape", () => {
  assert.deepEqual(
    parseMarcusPropertiesResponse(
      { Results: { Properties: [{ ActivityId: "a" }], TotalCount: 1 } },
      true
    ),
    { rows: [{ ActivityId: "a" }], total: 1 }
  );
  for (const total of [null, Number.NaN, Number.POSITIVE_INFINITY, -1, 1.5]) {
    assert.throws(
      () =>
        parseMarcusPropertiesResponse(
          { Results: { Properties: [{ ActivityId: "a" }], TotalCount: total } },
          true
        ),
      /finite nonnegative integer TotalCount/
    );
  }
  assert.throws(
    () => parseMarcusPropertiesResponse({ Results: { TotalCount: 1 } }, true),
    /Properties array/
  );
});

test("strict Marcus map inventory requires a Properties array and exact total", () => {
  assert.throws(
    () => parseMarcusMapRowsResponse({ Results: {} }, true),
    /mapproperties response has no Properties array/
  );
  const rows = parseMarcusMapRowsResponse(
    {
      Results: {
        Properties: [
          { ActivityId: "a" },
          { ActivityId: "a" },
          { ActivityId: "b" },
        ],
      },
    },
    true
  );
  assert.deepEqual(rows.map((row: any) => row.ActivityId), ["a", "b"]);
  assert.throws(
    () => assertMarcusInventoryCount(null, rows, true),
    /finite nonnegative integer TotalCount/
  );
  assert.doesNotThrow(() => assertMarcusInventoryCount(null, rows, false));
  assert.throws(
    () => assertMarcusInventoryCount(3, rows, true),
    /properties API reported 3, map API returned 2/
  );
  assert.doesNotThrow(() => assertMarcusInventoryCount(3, rows, false));
  assert.deepEqual(
    parseMarcusPropertiesResponse(
      { Results: { Properties: [{ ActivityId: "legacy" }] } },
      false
    ),
    { rows: [{ ActivityId: "legacy" }], total: null }
  );
  assert.deepEqual(
    parseMarcusMapRowsResponse([{ ActivityId: "legacy" }], false),
    [{ ActivityId: "legacy" }]
  );
});

// ---------------------------------------------------------------------------
// parseMarcusContactLicense tests
// ---------------------------------------------------------------------------

test("parseMarcusContactLicense extracts license string after 'License(s):'", () => {
  assert.equal(parseMarcusContactLicense("License(s): IL: 475.188007"), "IL: 475.188007");
  assert.equal(
    parseMarcusContactLicense("License(s): IL: 475.205207, MI: 6501455825, IA: S73371000"),
    "IL: 475.205207, MI: 6501455825, IA: S73371000"
  );
  assert.equal(parseMarcusContactLicense("License(s): CA: 02086137"), "CA: 02086137");
});

test("parseMarcusContactLicense returns null for absent or non-license strings", () => {
  assert.equal(parseMarcusContactLicense(null), null);
  assert.equal(parseMarcusContactLicense(undefined), null);
  assert.equal(parseMarcusContactLicense(""), null);
  assert.equal(parseMarcusContactLicense("no license here"), null);
});

// ---------------------------------------------------------------------------
// parseMarcusScalars: null/empty input
// ---------------------------------------------------------------------------

test("parseMarcusScalars returns empty object for null/missing specs", () => {
  const result = parseMarcusScalars(null);
  // All contract fields should be null; none should throw.
  assert.equal(result.capRatePct, null);
  assert.equal(result.occupancyRate, null);
  assert.equal(result.sizeSf, null);
  assert.equal(result.salePricePerSf, null);
  assert.equal(result.lotSizeSf, null);
  assert.equal(result.units, null);
  assert.equal(result.leaseRateType, null);
  assert.equal(result.leaseRateMin, null);
  assert.equal(result.leaseRateMax, null);
  assert.equal(result.tenantName, null);
  assert.equal(result.guarantor, null);
  assert.equal(result.leaseYearsRemaining, null);
  assert.equal(result.grm, null);
  assert.equal(result.pricePerUnit, null);
  assert.equal(result.pricePerAcre, null);
  assert.equal(result.numRooms, null);
  assert.equal(result.revpar, null);
  assert.equal(result.extraFacts, null);
});

test("parseMarcusScalars does not throw on empty specs object", () => {
  assert.doesNotThrow(() => parseMarcusScalars({}));
  const result = parseMarcusScalars({});
  assert.equal(result.capRatePct, null);
});

// ---------------------------------------------------------------------------
// parseMarcusScalars: fixture blob 1 - net-lease medical (163445)
// tenant, guarantor, lease type, rent/SF, years remaining, cap rate, size, price/SF
// ---------------------------------------------------------------------------

test("parseMarcusScalars fixture 163445: net-lease medical - tenant/guarantor/lease fields", () => {
  const fixtures = loadFixture();
  const blob = fixtures.find((f: any) => f.external_id === "163445");
  assert.ok(blob, "fixture 163445 must exist");
  const result = parseMarcusScalars(blob.marcusSpecifications, blob.capRatePct);

  // Tenant credit fields
  assert.equal(result.tenantName, "JenCare Senior Medical Center");
  assert.equal(result.guarantor, "Subsidiary of a Corporation");

  // Lease years remaining: "1.3" -> 1.3
  assert.equal(result.leaseYearsRemaining, 1.3);

  // Lease type from 'Lease Type': "Triple Net (NNN)" -> "nnn"
  assert.equal(result.leaseRateType, "nnn");

  // Rent per SF: "$23.40" as bare amount -> 23.40
  assert.equal(result.leaseRateMin, 23.40);
  assert.equal(result.leaseRateMax, null);

  // Cap rate from specs: "8.60%" -> 8.6
  assert.equal(result.capRatePct, 8.6);

  // Size from 'Rentable SF': "10,815" -> 10815
  assert.equal(result.sizeSf, 10815);

  // salePricePerSf from 'Price/Gross SF': "$272.07" -> 272.07
  assert.equal(result.salePricePerSf, 272.07);

  // No lot size in this fixture
  assert.equal(result.lotSizeSf, null);

  // No units in this fixture
  assert.equal(result.units, null);
});

test("parseMarcusScalars fixture 163445: no GRM/revpar/numRooms/pricePerUnit/pricePerAcre", () => {
  const fixtures = loadFixture();
  const blob = fixtures.find((f: any) => f.external_id === "163445");
  const result = parseMarcusScalars(blob.marcusSpecifications, blob.capRatePct);
  assert.equal(result.grm, null);
  assert.equal(result.revpar, null);
  assert.equal(result.numRooms, null);
  assert.equal(result.pricePerUnit, null);
  assert.equal(result.pricePerAcre, null);
});

// ---------------------------------------------------------------------------
// parseMarcusScalars: fixture blob 2 - multifamily (177871)
// GRM, occupancy, price/unit, units, cap rate, size, price/SF
// ---------------------------------------------------------------------------

test("parseMarcusScalars fixture 177871: multifamily - GRM, occupancy, price/unit, units", () => {
  const fixtures = loadFixture();
  const blob = fixtures.find((f: any) => f.external_id === "177871");
  assert.ok(blob, "fixture 177871 must exist");
  const result = parseMarcusScalars(blob.marcusSpecifications, blob.capRatePct);

  // GRM: "6.06" -> 6.06
  assert.equal(result.grm, 6.06);

  // Occupancy: "87.5%" -> 0.875
  assert.equal(result.occupancyRate, 0.875);

  // pricePerUnit: "$56,964" -> 56964
  assert.equal(result.pricePerUnit, 56964);

  // units: "28" -> 28
  assert.equal(result.units, 28);

  // cap rate: "6.28%" -> 6.28
  assert.equal(result.capRatePct, 6.28);

  // sizeSf from 'Gross SF' (no 'Rentable SF'): "17,400" -> 17400
  assert.equal(result.sizeSf, 17400);

  // salePricePerSf: "$91.67" -> 91.67
  assert.equal(result.salePricePerSf, 91.67);

  // No tenant credit fields on multifamily
  assert.equal(result.tenantName, null);
  assert.equal(result.guarantor, null);
  assert.equal(result.leaseYearsRemaining, null);
  assert.equal(result.leaseRateType, null);
  assert.equal(result.leaseRateMin, null);

  // No lot size on this fixture
  assert.equal(result.lotSizeSf, null);
});

// ---------------------------------------------------------------------------
// parseMarcusScalars: fixture blob 3 - lot size (128758)
// Lot Size in acres -> lotSizeSf, no tenant/lease fields
// ---------------------------------------------------------------------------

test("parseMarcusScalars fixture 128758: lot-size property - acres to SF conversion", () => {
  const fixtures = loadFixture();
  const blob = fixtures.find((f: any) => f.external_id === "128758");
  assert.ok(blob, "fixture 128758 must exist");
  const result = parseMarcusScalars(blob.marcusSpecifications, blob.capRatePct);

  // 'Lot Size': "3.83 acres" -> 3.83 * 43560 = 166834.8
  assert.ok(result.lotSizeSf !== null, "lotSizeSf should not be null");
  assert.ok(Math.abs(result.lotSizeSf - 166834.8) < 1, `lotSizeSf ${result.lotSizeSf} should be ~166834.8`);

  // sizeSf from 'Rentable SF': "38,000" -> 38000
  assert.equal(result.sizeSf, 38000);

  // salePricePerSf: "$144.74" -> 144.74
  assert.equal(result.salePricePerSf, 144.74);

  // cap rate: "7.80%" -> 7.8
  assert.equal(result.capRatePct, 7.8);

  // No tenant/lease fields
  assert.equal(result.tenantName, null);
  assert.equal(result.guarantor, null);
  assert.equal(result.leaseYearsRemaining, null);
  assert.equal(result.leaseRateType, null);
  assert.equal(result.leaseRateMin, null);

  // extraFacts: Year Built should be in extraFacts (it is a long-tail field)
  assert.ok(result.extraFacts !== null, "extraFacts should contain year_built_raw");
  assert.equal(result.extraFacts?.["year_built_raw"], "1979");
});

// ---------------------------------------------------------------------------
// parseMarcusScalars: top-level capRatePct fallback
// ---------------------------------------------------------------------------

test("parseMarcusScalars falls back to top-level capRatePct when specs lack 'Cap Rate'", () => {
  const result = parseMarcusScalars({ "Tenant Name": "Acme Corp" }, 5.75);
  assert.equal(result.capRatePct, 5.75);
  assert.equal(result.tenantName, "Acme Corp");
});

test("parseMarcusScalars specs Cap Rate takes precedence over top-level capRatePct", () => {
  const result = parseMarcusScalars({ "Cap Rate": "8.00%" }, 5.75);
  assert.equal(result.capRatePct, 8.0);
});

// ---------------------------------------------------------------------------
// parseMarcusScalars: individual field edge cases
// ---------------------------------------------------------------------------

test("parseMarcusScalars: GRM out-of-range (>= 100) yields null", () => {
  const result = parseMarcusScalars({ "GRM": "150" });
  assert.equal(result.grm, null);
});

test("parseMarcusScalars: leaseYearsRemaining out-of-range (> 99) yields null", () => {
  const result = parseMarcusScalars({ "Years Remaining On Lease": "105" });
  assert.equal(result.leaseYearsRemaining, null);
});

test("parseMarcusScalars: hotel RevPAR and Number of Rooms", () => {
  const result = parseMarcusScalars({
    "RevPAR": "$85.00",
    "Number of Rooms": "120",
  });
  assert.equal(result.revpar, 85.0);
  assert.equal(result.numRooms, 120);
});

test("parseMarcusScalars: pricePerAcre from Price/Acre", () => {
  const result = parseMarcusScalars({ "Price/Acre": "$125,000" });
  assert.equal(result.pricePerAcre, 125000);
});

test("parseMarcusScalars: extraFacts captures Buildable Square Feet", () => {
  const result = parseMarcusScalars({ "Buildable Square Feet": "25,000" });
  assert.ok(result.extraFacts !== null);
  assert.ok(result.extraFacts?.["buildable_sf"] > 0, "buildable_sf should be positive");
});

test("parseMarcusScalars: extraFacts is null when no long-tail fields present", () => {
  const result = parseMarcusScalars({ "Tenant Name": "Acme", "GRM": "8.5" });
  // Only contract fields populated; no long-tail
  assert.equal(result.extraFacts, null);
});

// ---------------------------------------------------------------------------
// parseMarcusContactLicense: fixture contacts
// ---------------------------------------------------------------------------

test("parseMarcusContactLicense parses real fixture contacts correctly", () => {
  const fixtures = loadFixture();
  const blob = fixtures.find((f: any) => f.external_id === "163445");
  const contacts = blob.contactsDetailed as any[];

  // First contact: "License(s): IL: 475.188007"
  assert.equal(parseMarcusContactLicense(contacts[0].license), "IL: 475.188007");
  // Second contact: "License(s): IL: 471.010712"
  assert.equal(parseMarcusContactLicense(contacts[1].license), "IL: 471.010712");
});

test("parseMarcusContactLicense handles multi-state license strings", () => {
  const fixtures = loadFixture();
  const blob = fixtures.find((f: any) => f.external_id === "177871");
  const contact = (blob.contactsDetailed as any[])[0];
  // "License(s): IL: 475.205207, MI: 6501455825, IA: S73371000"
  const parsed = parseMarcusContactLicense(contact.license);
  assert.ok(parsed?.includes("IL: 475.205207"), "should include IL license");
  assert.ok(parsed?.includes("MI: 6501455825"), "should include MI license");
  assert.ok(parsed?.includes("IA: S73371000"), "should include IA license");
});

test("parseMarcusContactLicense returns null when contact has no license field", () => {
  const fixtures = loadFixture();
  const blob = fixtures.find((f: any) => f.external_id === "128758");
  const contacts = blob.contactsDetailed as any[];
  // Luke Lamoreaux has no license field
  const lamoreaux = contacts.find((c: any) => c.name === "Luke D. Lamoreaux");
  assert.ok(lamoreaux, "Lamoreaux contact must exist in fixture");
  assert.equal(parseMarcusContactLicense(lamoreaux.license), null);
});
