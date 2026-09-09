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
  cbreInventoryUrl,
  cbreIdentityFingerprint,
  cbreSnapshotDifference,
  cbreSnapshotFingerprint,
  assertCbrePage,
  assertCbreAggregate,
  fetchCbreSnapshot,
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

function loadFixture(): Array<{
  _comment?: string;
  external_id: string;
  sourceKey: string;
  raw_data: any;
}> {
  return JSON.parse(readFileSync(FIXTURE_PATH, "utf8"));
}

const cbreRow = (id: string, extra: Record<string, any> = {}) => ({
  "Common.PrimaryKey": id,
  ...extra,
});

type CbreMockPage = { DocumentCount: number; Documents: any[] };

function cbrePassFetcher(
  passes: Array<Record<number, CbreMockPage | Record<string, any>>>,
  calls: Array<{ page: number; pass: number }> = [],
) {
  return async (page: number, pass: number) => {
    calls.push({ page, pass });
    return structuredClone(passes[pass - 1]?.[page]);
  };
}

test("cbreAspect maps transaction to API aspect", () => {
  assert.equal(cbreAspect("sale"), "isSale");
  assert.equal(cbreAspect("lease"), "isLetting");
});

test("CBRE pass cache keys are collision-free while decoded semantics remain exact", () => {
  for (let generation = 0; generation < 100; generation++) {
    const urls = Array.from({ length: 5 }, (_, index) =>
      cbreInventoryUrl(
        "isSale",
        38,
        200,
        `generation-${generation}`,
        index + 1,
      ),
    );
    assert.equal(new Set(urls).size, urls.length);
    for (const value of urls) {
      const url = new URL(value);
      assert.equal(url.searchParams.get("site"), "us-comm");
      assert.equal(url.searchParams.get("Common.Aspects"), "isSale");
      assert.equal(url.searchParams.get("PageSize"), "200");
      assert.equal(url.searchParams.get("Page"), "38");
      assert.deepEqual(
        [...url.searchParams.keys()].sort(),
        ["Common.Aspects", "Page", "PageSize", "site"].sort(),
      );
    }
  }
});

test("CBRE snapshot fingerprint ignores object-key ordering but detects field changes", () => {
  const original = [cbreRow("CBRE-1", { beta: 2, alpha: { two: 2, one: 1 } })];
  const reordered = [cbreRow("CBRE-1", { alpha: { one: 1, two: 2 }, beta: 2 })];
  const changed = [cbreRow("CBRE-1", { alpha: { one: 1, two: 3 }, beta: 2 })];
  assert.equal(
    cbreSnapshotFingerprint(original),
    cbreSnapshotFingerprint(reordered),
  );
  assert.notEqual(
    cbreSnapshotFingerprint(original),
    cbreSnapshotFingerprint(changed),
  );
});

test("CBRE identity fingerprint ignores order and content but detects membership", () => {
  assert.equal(
    cbreIdentityFingerprint([cbreRow("A", { value: 1 }), cbreRow("B")]),
    cbreIdentityFingerprint([cbreRow("B"), cbreRow("A", { value: 2 })]),
  );
  assert.notEqual(
    cbreIdentityFingerprint([cbreRow("A"), cbreRow("B")]),
    cbreIdentityFingerprint([cbreRow("A"), cbreRow("C")]),
  );
});

test("CBRE snapshot differences distinguish identity, field, and order churn", () => {
  const difference = cbreSnapshotDifference(
    [cbreRow("A", { stable: 1 }), cbreRow("B", { changing: "old" })],
    [cbreRow("B", { changing: "new" }), cbreRow("C", { stable: 1 })],
  );
  assert.match(difference, /added=1,removed=1,changed_rows=1,same_order=false/);
  assert.match(difference, /changed_fields=changing:1/);
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
    "midtown-tower-123-main-st-dallas-tx-75201",
  );
  assert.equal(
    cbreListingSlug({
      name: "Retail Pad",
      street: null,
      city: "Austin",
      state: "TX",
      zip: null,
    }),
    "retail-pad-austin-tx",
  );
  assert.equal(
    cbreListingSlug({
      name: null,
      street: null,
      city: null,
      state: null,
      zip: null,
    }),
    "",
  );
});

test("cbreListingUrl builds CBRE detail path", () => {
  assert.equal(
    cbreListingUrl("US-SMPL-160329", "midtown-tower-dallas-tx"),
    "https://www.cbre.com/properties/properties-for-lease/commercial-space/details/US-SMPL-160329/midtown-tower-dallas-tx",
  );
});

test("cbreBrochureUrl resolves absolute and relative URIs", () => {
  assert.equal(
    cbreBrochureUrl("https://cdn.cbre.com/brochure.pdf"),
    "https://cdn.cbre.com/brochure.pdf",
  );
  assert.equal(
    cbreBrochureUrl("/resources/fileassets/US-SMPL/brochure.pdf"),
    "https://www.cbre.com/resources/fileassets/US-SMPL/brochure.pdf",
  );
  assert.equal(cbreBrochureUrl(null), "https://www.cbre.com");
});

test("cbrePhotoUrl resolves absolute and relative resource URIs", () => {
  assert.equal(
    cbrePhotoUrl("https://images.cbre.com/photo.jpg"),
    "https://images.cbre.com/photo.jpg",
  );
  assert.equal(
    cbrePhotoUrl("/resources/photos/abc.jpg"),
    "https://www.cbre.com/resources/photos/abc.jpg",
  );
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
    "https://www.cbre.com/properties/properties-for-lease/commercial-space/details/US-SMPL-196821/5-medical-plaza-drive-5-medical-plaza-drive-suite-200-roseville-ca-95661",
  );
  // highlights from headline
  assert.equal(out.highlights, "±2,259 SF Sublease Available | Medical Space");
  // 3.59 USD/SF/MO -> annualized = 3.59 * 12 = 43.08 $/SF/yr
  assert.ok(
    out.leaseRateMin !== null,
    "leaseRateMin must be non-null for SF/MO text",
  );
  assert.ok(
    Math.abs((out.leaseRateMin ?? 0) - 43.08) < 0.01,
    `expected ~43.08, got ${out.leaseRateMin}`,
  );
  assert.equal(out.leaseRateMax, null);
  assert.equal(out.leaseRateType, null);
});

test("cbreNewFieldsFromRawData: second lease row with 4.5 USD/SF/MO", () => {
  const fixture = loadFixture();
  const row = fixture.find((r) => r.external_id === "US-SMPL-198939")!;
  assert.ok(row, "fixture row US-SMPL-198939 must exist");
  const out = cbreNewFieldsFromRawData(row.raw_data);
  // canonicalUrl
  assert.ok(
    out.canonicalUrl?.includes("US-SMPL-198939"),
    "canonicalUrl must contain external_id",
  );
  // highlights
  assert.equal(out.highlights, "±50,271 SF INDUSTRIAL BUILDING | FOR LEASE");
  // 4.5 USD/SF/MO -> 4.5 * 12 = 54.0 $/SF/yr
  assert.ok(out.leaseRateMin !== null, "leaseRateMin must be non-null");
  assert.ok(
    Math.abs((out.leaseRateMin ?? 0) - 54.0) < 0.01,
    `expected ~54.0, got ${out.leaseRateMin}`,
  );
  assert.equal(out.leaseRateMax, null);
  assert.equal(out.leaseRateType, null);
});

test("cbreNewFieldsFromRawData: null/absent leaseRateText yields null lease rate fields", () => {
  const out = cbreNewFieldsFromRawData({
    url: "https://www.cbre.com/properties/x",
    headline: null,
  });
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
  const out = cbreNewFieldsFromRawData({
    leaseRateText: "$22 - $26 PSF NNN",
    url: null,
    headline: null,
  });
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
    true,
  );
  assert.equal(page.total, 3);
  assert.deepEqual(
    page.documents.map((row: any) => row["Common.PrimaryKey"]),
    ["CBRE-1", "CBRE-2"],
  );
  assert.throws(
    () =>
      assertCbrePage(
        { DocumentCount: 3, Documents: [{ "Common.PrimaryKey": "CBRE-3" }] },
        2,
        2,
        4,
        true,
      ),
    /changed from 4 to 3/,
  );
  assert.throws(
    () =>
      assertCbrePage({ DocumentCount: 2.5, Documents: [] }, 1, 2, null, true),
    /nonnegative integer DocumentCount/,
  );
  assert.throws(
    () =>
      assertCbrePage(
        { DocumentCount: 2, Page: 2, PageSize: 2, Documents: [] },
        1,
        2,
        null,
        true,
      ),
    /page metadata expected page 1/,
  );
  assert.throws(
    () =>
      assertCbrePage(
        { DocumentCount: 2, Documents: [{ "Common.PrimaryKey": "CBRE-1" }] },
        1,
        2,
        null,
        true,
      ),
    /expected 2 documents, received 1/,
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
        true,
      ),
    /duplicate Common.PrimaryKey/,
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
        true,
      ),
    /nonempty Common.PrimaryKey/,
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
    /expected 3 unique documents, received 2/,
  );
  assert.throws(
    () => assertCbreAggregate([rows[0], rows[0], rows[2]], 3, 2, true),
    /duplicate Common.PrimaryKey/,
  );
  assert.doesNotThrow(() => assertCbreAggregate(rows.slice(0, 2), 3, 2, false));
});

test("CBRE full snapshots reject internally drifting DocumentCount passes", async () => {
  const drifted = {
    1: {
      DocumentCount: 5,
      Documents: [cbreRow("A"), cbreRow("B"), cbreRow("C")],
    },
    2: { DocumentCount: 4, Documents: [cbreRow("D")] },
    3: { DocumentCount: 4, Documents: [] },
  };
  const calls: Array<{ page: number; pass: number }> = [];
  await assert.rejects(
    () =>
      fetchCbreSnapshot(cbrePassFetcher([drifted, drifted], calls), {
        pageSize: 3,
        maxPasses: 2,
        maxPages: 5,
        concurrency: 1,
      }),
    /did not converge[\s\S]*pages did not all report that total/,
  );
  assert.deepEqual(calls, [
    { page: 1, pass: 1 },
    { page: 2, pass: 1 },
    { page: 3, pass: 1 },
    { page: 1, pass: 2 },
    { page: 2, pass: 2 },
    { page: 3, pass: 2 },
  ]);
});

test("CBRE membership convergence emits the later pass during content and order churn", async () => {
  const earlier = {
    1: {
      DocumentCount: 3,
      Documents: [cbreRow("A"), cbreRow("B", { "Common.Brochures": ["old"] })],
    },
    2: { DocumentCount: 3, Documents: [cbreRow("C")] },
    3: { DocumentCount: 3, Documents: [] },
  };
  const later = {
    1: {
      DocumentCount: 3,
      Documents: [cbreRow("B", { "Common.Brochures": ["new"] }), cbreRow("A")],
    },
    2: { DocumentCount: 3, Documents: [cbreRow("C")] },
    3: { DocumentCount: 3, Documents: [] },
  };
  const snapshot = await fetchCbreSnapshot(cbrePassFetcher([earlier, later]), {
    pageSize: 2,
    maxPasses: 2,
    maxPages: 5,
    concurrency: 1,
  });
  assert.deepEqual(
    snapshot.documents.map((row) => row["Common.PrimaryKey"]),
    ["B", "A", "C"],
  );
  assert.match(
    snapshot.contentDifference ?? "",
    /added=0,removed=0,changed_rows=1,same_order=false/,
  );
  assert.match(snapshot.contentDifference ?? "", /Common\.Brochures:1/);
});

test("CBRE convergence replaces a deletion-shifted pass instead of accepting its count", async () => {
  const shifted = {
    1: { DocumentCount: 6, Documents: [cbreRow("A"), cbreRow("B")] },
    2: { DocumentCount: 5, Documents: [cbreRow("D"), cbreRow("E")] },
    3: { DocumentCount: 5, Documents: [cbreRow("F")] },
    4: { DocumentCount: 5, Documents: [] },
  };
  const current = {
    1: { DocumentCount: 5, Documents: [cbreRow("B"), cbreRow("C")] },
    2: { DocumentCount: 5, Documents: [cbreRow("D"), cbreRow("E")] },
    3: { DocumentCount: 5, Documents: [cbreRow("F")] },
    4: { DocumentCount: 5, Documents: [] },
  };
  const calls: Array<{ page: number; pass: number }> = [];
  const snapshot = await fetchCbreSnapshot(
    cbrePassFetcher([shifted, current, current], calls),
    { pageSize: 2, maxPasses: 3, maxPages: 5, concurrency: 1 },
  );
  assert.equal(calls.filter(({ page }) => page === 1).length, 3);
  assert.deepEqual(
    snapshot.documents.map((row) => row["Common.PrimaryKey"]),
    ["B", "C", "D", "E", "F"],
  );
});

test("CBRE snapshot extends pagination when growth reaches the planned sentinel", async () => {
  const grown = {
    1: { DocumentCount: 4, Documents: [cbreRow("A"), cbreRow("B")] },
    2: { DocumentCount: 5, Documents: [cbreRow("C"), cbreRow("D")] },
    3: { DocumentCount: 5, Documents: [cbreRow("E")] },
    4: { DocumentCount: 5, Documents: [] },
  };
  const calls: Array<{ page: number; pass: number }> = [];
  const stable = {
    1: { DocumentCount: 5, Documents: [cbreRow("A"), cbreRow("B")] },
    2: { DocumentCount: 5, Documents: [cbreRow("C"), cbreRow("D")] },
    3: { DocumentCount: 5, Documents: [cbreRow("E")] },
    4: { DocumentCount: 5, Documents: [] },
  };
  const snapshot = await fetchCbreSnapshot(
    cbrePassFetcher([grown, stable, stable], calls),
    { pageSize: 2, maxPasses: 3, maxPages: 5, concurrency: 1 },
  );
  assert.equal(snapshot.total, 5);
  assert.equal(calls.filter(({ page }) => page === 4).length, 3);
  assert.deepEqual(snapshot.reportedTotals, [5, 5, 5, 5]);
});

test("CBRE snapshot never deduplicates cross-page duplicate identities", async () => {
  const duplicate = {
    1: { DocumentCount: 3, Documents: [cbreRow("A"), cbreRow("B")] },
    2: { DocumentCount: 3, Documents: [cbreRow("B")] },
    3: { DocumentCount: 3, Documents: [] },
  };
  await assert.rejects(
    () =>
      fetchCbreSnapshot(cbrePassFetcher([duplicate, duplicate]), {
        pageSize: 2,
        maxPasses: 2,
        maxPages: 5,
        concurrency: 1,
      }),
    /did not converge[\s\S]*duplicate Common\.PrimaryKey B/,
  );
});

test("CBRE snapshot rejects malformed pages and nonempty pages after a terminal gap", async () => {
  const malformed = {
    1: { DocumentCount: 3, Documents: [cbreRow("A"), cbreRow("B")] },
    2: { DocumentCount: 3 },
  };
  await assert.rejects(
    () =>
      fetchCbreSnapshot(cbrePassFetcher([malformed, malformed]), {
        pageSize: 2,
        maxPasses: 2,
        maxPages: 5,
        concurrency: 1,
      }),
    /did not converge[\s\S]*missing a Documents array/,
  );

  const gap = {
    1: { DocumentCount: 5, Documents: [cbreRow("A"), cbreRow("B")] },
    2: { DocumentCount: 2, Documents: [] },
    3: { DocumentCount: 5, Documents: [cbreRow("E")] },
    4: { DocumentCount: 5, Documents: [] },
  };
  await assert.rejects(
    () =>
      fetchCbreSnapshot(cbrePassFetcher([gap, gap]), {
        pageSize: 2,
        maxPasses: 2,
        maxPages: 5,
        concurrency: 1,
      }),
    /did not converge[\s\S]*nonempty page 3 after a terminal empty page/,
  );
});

test("CBRE identity oscillation fails closed after the bounded pass budget", async () => {
  const inventory = (left: string, right: string) => ({
    1: { DocumentCount: 2, Documents: [cbreRow(left), cbreRow(right)] },
    2: { DocumentCount: 2, Documents: [] },
  });
  await assert.rejects(
    () =>
      fetchCbreSnapshot(
        cbrePassFetcher([
          inventory("A", "B"),
          inventory("B", "C"),
          inventory("A", "B"),
        ]),
        { pageSize: 2, maxPasses: 3, maxPages: 5, concurrency: 1 },
      ),
    /did not converge across 3 complete cache-bypassed passes/,
  );
});

test("CBRE snapshot rejects provider declarations beyond its hard page bound", async () => {
  const oversized = {
    1: { DocumentCount: 7, Documents: [cbreRow("A"), cbreRow("B")] },
  };
  await assert.rejects(
    () =>
      fetchCbreSnapshot(cbrePassFetcher([oversized, oversized]), {
        pageSize: 2,
        maxPasses: 2,
        maxPages: 3,
        concurrency: 1,
      }),
    /exceeding the 3-page safety cap/,
  );
});

test("strict CBRE requires an explicit refresh generation", async (t) => {
  const originalStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const originalGeneration = process.env.CRE_REFRESH_GENERATION;
  process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
  delete process.env.CRE_REFRESH_GENERATION;
  t.after(() => {
    if (originalStrict === undefined)
      delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = originalStrict;
    if (originalGeneration === undefined)
      delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = originalGeneration;
  });
  await assert.rejects(
    () => srcCbre("sale", 1, false),
    /requires CRE_REFRESH_GENERATION/,
  );
});

test("strict finite CBRE caps retain the one-pass truncated probe behavior", async (t) => {
  const originalScrape = firecrawl.scrape;
  const originalStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const originalGeneration = process.env.CRE_REFRESH_GENERATION;
  const calls: string[] = [];
  (firecrawl as any).scrape = async (url: string) => {
    calls.push(url);
    return {
      rawHtml: JSON.stringify({
        DocumentCount: 2,
        Documents: [cbreRow("CBRE-1"), cbreRow("CBRE-2")],
      }),
    };
  };
  process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
  process.env.CRE_REFRESH_GENERATION = "cbre-finite-test";
  t.after(() => {
    (firecrawl as any).scrape = originalScrape;
    if (originalStrict === undefined)
      delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = originalStrict;
    if (originalGeneration === undefined)
      delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = originalGeneration;
  });

  const result = await srcCbre("sale", 1, false);
  assert.equal(calls.length, 1);
  assert.equal(result.totalAvailable, 2);
  assert.equal(result.listings.length, 1);
  assert.equal(result.truncated, true);
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
        Documents:
          page === 1 ? rows.slice(0, 200) : page === 2 ? rows.slice(200) : [],
      }),
    };
  };
  process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
  process.env.CRE_REFRESH_GENERATION = "cbre-strict-test";
  t.after(() => {
    (firecrawl as any).scrape = originalScrape;
    if (originalStrict === undefined)
      delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = originalStrict;
    if (originalGeneration === undefined)
      delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = originalGeneration;
  });

  const result = await srcCbre("sale", Number.POSITIVE_INFINITY, false);
  assert.equal(result.listings.length, 201);
  assert.equal(result.totalAvailable, 201);
  assert.equal(calls.length, 6);
  assert.ok(calls.every(({ options }) => options.maxAge === 0));
  const pageOneCalls = calls.filter(
    ({ url }) => new URL(url).searchParams.get("Page") === "1",
  );
  assert.equal(pageOneCalls.length, 2);
  assert.notEqual(
    new URL(pageOneCalls[0].url).search,
    new URL(pageOneCalls[1].url).search,
  );
  for (const { url } of calls) {
    const parsed = new URL(url);
    assert.equal(parsed.searchParams.get("site"), "us-comm");
    assert.equal(parsed.searchParams.get("Common.Aspects"), "isSale");
    assert.equal(parsed.searchParams.get("PageSize"), "200");
    assert.deepEqual(
      [...parsed.searchParams.keys()].sort(),
      ["Common.Aspects", "Page", "PageSize", "site"].sort(),
    );
  }
  for (const listing of result.listings) {
    assert.equal(listing.detailObservedAt, listing.inventoryObservedAt);
    assert.equal(listing.freshnessProvenance.generationId, "cbre-strict-test");
    assert.equal(
      listing.freshnessProvenance.detailScope,
      "authoritative_inventory_feed",
    );
    assert.equal(listing.freshnessProvenance.cacheDisposition, "live");
  }
});
