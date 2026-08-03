import test from "node:test";
import assert from "node:assert/strict";
import * as cheerio from "cheerio";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  canonicalTranswesternUrl,
  transwesternDetailUrl,
  transwesternTransactionType,
  transwesternSizeText,
  transwesternPriceText,
  parseTranswesternFacts,
  parseTranswesternAvailability,
  transwesternStructured,
  liftTranswesternScalars,
  transwesternCapTruncated,
  srcTranswestern,
} from "../../../sources/transwestern.js";
import { firecrawl } from "../../../lib/scrape.js";

// ---------------------------------------------------------------------------
// Fixture loader (tests/fixtures/raw_data/transwestern.json)
// ---------------------------------------------------------------------------
const __dir = dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = join(__dir, "../../fixtures/raw_data/transwestern.json");
const fixtures = JSON.parse(readFileSync(FIXTURE_PATH, "utf-8")) as Array<{
  external_id: string;
  transaction_type: string;
  raw_data: Record<string, unknown>;
}>;

// Helper: get a fixture by external_id
function fx(id: string) {
  const f = fixtures.find((r) => r.external_id === id);
  if (!f) throw new Error(`fixture not found: ${id}`);
  return f.raw_data;
}

test("Transwestern finite caps report truncation after bucket de-duplication", () => {
  assert.equal(transwesternCapTruncated(1, 1, 2), true);
  assert.equal(transwesternCapTruncated(2, 2, 2), false);
  assert.equal(
    transwesternCapTruncated(Number.POSITIVE_INFINITY, 2, 3),
    false
  );
});

test("Transwestern full refresh bypasses Firecrawl cache while monitor keeps defaults", async () => {
  const original = firecrawl.scrape;
  const calls: Array<{ url: string; options: any }> = [];
  const feedRow = {
    PageUrl: "sample-property",
    BuildingName: "Sample Property",
    PropertyTypeName: "Office",
    FullAddress: "100 Main Street",
    City: "Dallas",
    State: "TX",
    ZipCode: "75201",
    PropertySize: 10000,
  };
  (firecrawl as any).scrape = async (url: string, options: any) => {
    calls.push({ url, options });
    if (url.includes("/properties?")) return { rawHtml: JSON.stringify([feedRow]) };
    return {
      rawHtml: "<html><h1>Sample Property</h1><div class='property-description'>Current detail</div></html>",
      markdown: "Sample Property\nCurrent detail",
      links: [],
    };
  };
  try {
    await srcTranswestern("sale", 1, false);
    assert.ok(calls.length >= 3);
    assert.ok(calls.every((call) => call.options.maxAge === 0));

    calls.length = 0;
    await srcTranswestern("sale", 1, true);
    assert.equal(calls.length, 2);
    assert.ok(calls.every((call) => !("maxAge" in call.options)));
  } finally {
    (firecrawl as any).scrape = original;
  }
});

test("canonicalTranswesternUrl resolves relative paths and rejects junk", () => {
  assert.equal(
    canonicalTranswesternUrl("/property/dallas-office"),
    "https://transwestern.com/property/dallas-office"
  );
  assert.equal(canonicalTranswesternUrl("javascript:void(0)"), null);
  assert.equal(canonicalTranswesternUrl("-"), null);
  assert.equal(canonicalTranswesternUrl(null), null);
});

test("transwesternDetailUrl builds property slug URLs", () => {
  assert.equal(
    transwesternDetailUrl("dallas-midtown-tower"),
    "https://transwestern.com/property/dallas-midtown-tower"
  );
  assert.equal(transwesternDetailUrl("-"), null);
  assert.equal(transwesternDetailUrl(null), null);
});

test("transwesternTransactionType maps bucket labels", () => {
  assert.equal(transwesternTransactionType("Sale"), "Sale");
  assert.equal(transwesternTransactionType("Lease"), "Lease");
  assert.equal(transwesternTransactionType("Sublease"), "Sublease");
  assert.equal(transwesternTransactionType("Sale or Lease"), "Sale/Lease");
});

test("transwesternSizeText formats square footage", () => {
  assert.equal(transwesternSizeText({ PropertySize: 12500 }), "12,500 SF");
  assert.equal(transwesternSizeText({ PropertySize: 0 }), null);
  assert.equal(transwesternSizeText({}), null);
});

test("transwesternPriceText formats sale price or lease fallback", () => {
  assert.equal(transwesternPriceText({ Price: 2500000 }, "sale"), "$2,500,000");
  assert.equal(transwesternPriceText({ Price: 0 }, "sale"), "Contact broker for pricing");
  assert.equal(transwesternPriceText({ Price: 0 }, "lease"), null);
});

test("parseTranswesternFacts extracts label/value pairs", () => {
  const html = `
    <ul class="property-facts">
      <li><b>Year Built:</b> 1987</li>
      <li><strong>Class:</strong> A</li>
    </ul>
  `;
  const $ = cheerio.load(html);
  assert.deepEqual(parseTranswesternFacts($), {
    "Year Built": "1987",
    Class: "A",
  });
});

test("transwesternStructured lifts year_built/units/floors/parking/zoning from facts", () => {
  const out = transwesternStructured({
    "Year Built": "1987",
    "No. of Units": "120",
    Floors: "8 stories",
    "Parking Spaces": "350",
    Zoning: "C-2 Commercial",
    Class: "A", // unmapped -> ignored
  });
  assert.equal(out.yearBuilt, 1987);
  assert.equal(out.units, 120);
  assert.equal(out.floors, 8);
  assert.equal(out.parkingSpaces, 350);
  assert.equal(out.zoning, "C-2 Commercial");
});

test("transwesternStructured emits nothing for a sparse/empty facts block", () => {
  assert.deepEqual(transwesternStructured({}), {});
  // An out-of-range year is rejected (guards a misparsed token).
  assert.deepEqual(transwesternStructured({ "Year Built": "12" }), {});
});

test("parseTranswesternAvailability parses suite rows", () => {
  const html = `
    <table id="tblAvailability">
      <tr><th>Suite</th><th>Size</th><th>Rate</th><th>Type</th></tr>
      <tr><td>1200</td><td>4,500 SF</td><td>$32/SF</td><td>Office</td></tr>
      <tr><td>1400</td><td>2,100 SF</td><td>$28/SF</td><td>Office</td></tr>
    </table>
  `;
  const $ = cheerio.load(html);
  const rows = parseTranswesternAvailability($);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].suite, "1200");
  assert.equal(rows[0].size, "4,500 SF");
  assert.equal(rows[0].rate, "$32/SF");
  assert.equal(rows[0].type, "Office");
});

// ---------------------------------------------------------------------------
// liftTranswesternScalars — Phase-2 data-lift tests using real fixture blobs
// ---------------------------------------------------------------------------

test("liftTranswesternScalars: industrial sale (1025-w-national-avenue) lifts class/clearHeight/docks/doors/power/rail", () => {
  const raw = fx("1025-w-national-avenue");
  const facts = raw.transwesternFacts as Record<string, string>;
  const avail = raw.availability as Array<{ raw?: string[]; rate?: string | null; size?: string | null; type?: string | null }>;
  const url = raw.url as string;

  const out = liftTranswesternScalars(facts, avail, url);

  // buildingClass: facts['Class'] = 'B' -> normBuildingClass -> 'B'
  assert.equal(out.buildingClass, "B");

  // clearHeightFt: facts['Clear Height(max)'] = '16' -> 16
  assert.equal(out.clearHeightFt, 16);

  // dockDoors: facts['Docks'] = '1'
  assert.equal(out.dockDoors, 1);

  // driveInDoors: facts['Grade Level Doors'] = '1'
  assert.equal(out.driveInDoors, 1);

  // powerService: facts['Power'] = '1200a'
  assert.equal(out.powerService, "1200a");

  // railServed: facts['Rail'] = 'No' -> false
  assert.equal(out.railServed, false);

  // canonicalUrl from url
  assert.equal(out.canonicalUrl, "https://transwestern.com/property/1025-w-national-avenue");

  // Availability row is type='Sale' -> not counted toward availableSf or lease rates
  assert.equal(out.availableSf, undefined);
  assert.equal(out.leaseRateMin, undefined);
  assert.equal(out.leaseRateMax, undefined);
  assert.equal(out.leaseRateType, undefined);

  // minDivisibleSf/maxDivisibleSf from size '24,990'
  assert.equal(out.minDivisibleSf, 24990);
  assert.equal(out.maxDivisibleSf, 24990);

  // extraFacts: Typical Floor Size is long-tail (no Yard/Crane in this fixture)
  assert.ok(out.extraFacts && "typical_floor_size" in out.extraFacts, "extraFacts should contain typical_floor_size");
  assert.equal(out.extraFacts?.["typical_floor_size"], "24,990");
});

test("liftTranswesternScalars: lease listing (455-kehoe-boulevard) lifts lease rates, Absolute Net -> nnn, availableSf, extraFacts", () => {
  const raw = fx("455-kehoe-boulevard");
  const facts = raw.transwesternFacts as Record<string, string>;
  const avail = raw.availability as Array<{ raw?: string[]; rate?: string | null; size?: string | null; type?: string | null }>;
  const url = raw.url as string;

  const out = liftTranswesternScalars(facts, avail, url);

  // buildingClass: facts['Class'] = 'C'
  assert.equal(out.buildingClass, "C");

  // No clear height or docks/doors/power/rail in this fixture
  assert.equal(out.clearHeightFt, undefined);
  assert.equal(out.dockDoors, undefined);
  assert.equal(out.driveInDoors, undefined);
  assert.equal(out.powerService, undefined);
  assert.equal(out.railServed, undefined);

  // availableSf: sum of non-sale sizes: 3036 + 3122 = 6158
  assert.equal(out.availableSf, 6158);

  // minDivisibleSf / maxDivisibleSf
  assert.equal(out.minDivisibleSf, 3036);
  assert.equal(out.maxDivisibleSf, 3122);

  // leaseRateMin / leaseRateMax: both rows have $11.75/SF -> min=11.75, max=null (single rate)
  // Note: parseLeaseRate on '$11.75' returns {min:11.75,...} (bare amount, trusted as-is).
  assert.equal(out.leaseRateMin, 11.75);
  assert.equal(out.leaseRateMax, null);

  // leaseRateType: 'Absolute Net' in raw[] -> maps to 'nnn'
  assert.equal(out.leaseRateType, "nnn");

  // extraFacts: Year Renovated, Typical Floor Size, Elevators
  assert.ok(out.extraFacts, "extraFacts should be present");
  assert.equal(out.extraFacts?.["year_renovated"], "1998");
  assert.equal(out.extraFacts?.["typical_floor_size"], "34,567");
  assert.equal(out.extraFacts?.["elevators"], "0");
});

test("liftTranswesternScalars: office sale (reserve-1) emits extraFacts for Year Renovated/Elevators/Typical Floor Size", () => {
  const raw = fx("reserve-1");
  const facts = raw.transwesternFacts as Record<string, string>;
  const avail = raw.availability as Array<{ raw?: string[]; rate?: string | null; size?: string | null; type?: string | null }>;
  const url = raw.url as string;

  const out = liftTranswesternScalars(facts, avail, url);

  // buildingClass: 'B'
  assert.equal(out.buildingClass, "B");

  // No availableSf (only sale rows)
  assert.equal(out.availableSf, undefined);

  // extraFacts
  assert.ok(out.extraFacts, "extraFacts should be present");
  assert.equal(out.extraFacts?.["year_renovated"], "2021");
  assert.equal(out.extraFacts?.["elevators"], "1");
  assert.equal(out.extraFacts?.["typical_floor_size"], "5,374");

  // canonicalUrl
  assert.equal(out.canonicalUrl, "https://transwestern.com/property/reserve-1");
});

test("liftTranswesternScalars: land sale with real acreage (seq-us-67-fm-2280) converts Land Area (ac) to lotSf", () => {
  const raw = fx("seq-us-67-fm-2280");
  const facts = raw.transwesternFacts as Record<string, string>;
  const avail = raw.availability as Array<{ raw?: string[]; rate?: string | null; size?: string | null; type?: string | null }>;
  const url = raw.url as string;

  const out = liftTranswesternScalars(facts, avail, url);

  // Land Area (ac) = '29.2' -> 29.2 * 43560 = 1,271,952 SF
  assert.ok(out.lotSf !== undefined && out.lotSf !== null, "lotSf should be set");
  assert.ok(Math.abs((out.lotSf as number) - 29.2 * 43560) < 1, `lotSf should be ~${29.2 * 43560}, got ${out.lotSf}`);

  // apn: Parcel = '1'
  assert.equal(out.apn, "1");

  // No building class (no Class fact)
  assert.equal(out.buildingClass, undefined);
});

test("liftTranswesternScalars: DQ guard suppresses lotSf when Land Area (ac) is suspiciously large (29,185 looks like SF)", () => {
  const raw = fx("2390-n-druid-hills-rd-ne");
  const facts = raw.transwesternFacts as Record<string, string>;
  const avail = raw.availability as Array<{ raw?: string[]; rate?: string | null; size?: string | null; type?: string | null }>;
  const url = raw.url as string;

  const out = liftTranswesternScalars(facts, avail, url);

  // Land Area (ac) = '29,185' -> 29185 >= 10000 threshold -> do NOT convert -> lotSf undefined
  assert.equal(out.lotSf, undefined);

  // No Class, no industrial fields
  assert.equal(out.buildingClass, undefined);
  assert.equal(out.dockDoors, undefined);
});

test("liftTranswesternScalars: empty facts / empty availability never throws and emits no fields", () => {
  const out = liftTranswesternScalars({}, [], null);
  assert.equal(out.buildingClass, undefined);
  assert.equal(out.clearHeightFt, undefined);
  assert.equal(out.dockDoors, undefined);
  assert.equal(out.driveInDoors, undefined);
  assert.equal(out.powerService, undefined);
  assert.equal(out.railServed, undefined);
  assert.equal(out.apn, undefined);
  assert.equal(out.lotSf, undefined);
  assert.equal(out.minDivisibleSf, undefined);
  assert.equal(out.maxDivisibleSf, undefined);
  assert.equal(out.availableSf, undefined);
  assert.equal(out.leaseRateMin, undefined);
  assert.equal(out.leaseRateMax, undefined);
  assert.equal(out.leaseRateType, undefined);
  assert.equal(out.canonicalUrl, undefined);
  assert.equal(out.extraFacts, undefined);
});

test("liftTranswesternScalars: lease type vocabulary matches NNN, FSG, MG without hardcoded index", () => {
  // NNN token in a raw[] cell at various positions
  const availNnn = [
    { raw: ["109", "1,500", "$10.00", "Direct-New", "1,500", "NNN"], rate: "$10.00", size: "1,500", type: "Direct-New" },
  ];
  const outNnn = liftTranswesternScalars({}, availNnn, null);
  assert.equal(outNnn.leaseRateType, "nnn");

  // FSG token
  const availFsg = [
    { raw: ["Suite A", "5,000", "$24.00/SF/YR", "Direct-New", "5,000", "FSG"], rate: "$24.00/SF/YR", size: "5,000", type: "Direct-New" },
  ];
  const outFsg = liftTranswesternScalars({}, availFsg, null);
  assert.equal(outFsg.leaseRateType, "full_service");

  // Modified Gross in the middle
  const availMg = [
    { raw: ["100", "Modified Gross", "2,000", "$18.50/SF", "2,000"], rate: "$18.50/SF", size: "2,000", type: "Direct-New" },
  ];
  const outMg = liftTranswesternScalars({}, availMg, null);
  assert.equal(outMg.leaseRateType, "modified_gross");

  // No matching type -> undefined
  const availNoType = [
    { raw: ["200", "1,000", "$15.00/SF", "Direct-New"], rate: "$15.00/SF", size: "1,000", type: "Direct-New" },
  ];
  const outNoType = liftTranswesternScalars({}, availNoType, null);
  assert.equal(outNoType.leaseRateType, undefined);
});

test("liftTranswesternScalars: lease rates > 1000 psf are excluded (implausible guard)", () => {
  const avail = [
    { raw: ["A1", "2,000", "$5000/SF/YR", "Direct-New"], rate: "$5000/SF/YR", size: "2,000", type: "Direct-New" },
  ];
  const out = liftTranswesternScalars({}, avail, null);
  // parseLeaseRate caps at 500 -> returns min=null; leaseRateMin should be absent
  assert.equal(out.leaseRateMin, undefined);
});

test("liftTranswesternScalars: railServed parses Yes/No/absent correctly", () => {
  assert.equal(liftTranswesternScalars({ Rail: "Yes" }, [], null).railServed, true);
  assert.equal(liftTranswesternScalars({ Rail: "No" }, [], null).railServed, false);
  // 'no' lowercase
  assert.equal(liftTranswesternScalars({ Rail: "no" }, [], null).railServed, false);
  // Absent key -> undefined
  assert.equal(liftTranswesternScalars({}, [], null).railServed, undefined);
});
