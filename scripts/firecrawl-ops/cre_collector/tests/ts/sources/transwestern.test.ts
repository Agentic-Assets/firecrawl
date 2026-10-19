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
  transwesternDetailPageIsUsable,
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

// Minimized public property template observed at
// https://transwestern.com/property/hollowell-english on 2026-10-11.
// This template has no availability table; feed identity text is HTML encoded.
const currentPropertyHtml = `
  <main class="property-dtl">
    <h1>Hollowell &amp; English</h1>
    <section class="property-dscptn">Property overview</section>
    <table id="tblAttachments"><tr><td>Property documents</td></tr></table>
  </main>
`;
const currentPropertyRow = { BuildingName: "Hollowell &amp; English" };

test("Transwestern recognizes the public property template without availability", () => {
  const variants = [
    currentPropertyHtml,
    currentPropertyHtml.replace(/<table[\s\S]*?<\/table>/, ""),
    currentPropertyHtml.replace(/<section[\s\S]*?<\/section>/, ""),
  ];
  for (const rawHtml of variants) {
    assert.equal(transwesternDetailPageIsUsable({
      rawHtml: rawHtml.replace(/Hollowell &amp; English/g, "Sample Property"),
      markdown: "Sample Property",
      links: [],
    }, { BuildingName: "Sample Property" }), true);
  }
});

test("Transwestern compares HTML encoded feed identities with decoded headings", () => {
  assert.equal(transwesternDetailPageIsUsable({
    rawHtml: currentPropertyHtml,
    markdown: "Hollowell & English",
    links: [],
  }, currentPropertyRow), true);
});

test("Transwestern rejects wrong identity, challenges, and incomplete property pages", () => {
  const rejected = [
    ["wrong identity", currentPropertyHtml.replace(/Hollowell &amp; English/g, "Other Property")],
    ["challenge", `${currentPropertyHtml}<p>Verify you are human</p>`],
    ["not found", `${currentPropertyHtml}<p>Page not found</p>`],
    ["empty", ""],
    ["heading only", "<h1>Hollowell &amp; English</h1>"],
    ["property shell only", '<main class="property-dtl"><h1>Hollowell &amp; English</h1></main>'],
    ["attachment table outside property template", '<h1>Hollowell &amp; English</h1><table id="tblAttachments"></table>'],
    ["body outside property shell", '<main class="property-dtl"><h1>Hollowell &amp; English</h1></main><section class="property-dscptn">Overview</section><table id="tblAttachments"></table>'],
    ["missing identity", '<main class="property-dtl"><section class="property-dscptn">Overview</section></main>'],
  ];
  for (const [reason, rawHtml] of rejected) {
    assert.equal(transwesternDetailPageIsUsable({
      rawHtml,
      markdown: "",
      links: [],
    }, currentPropertyRow), false, reason);
  }
});

test("Transwestern lease feed remains fail-closed when a row has no provider identity", async () => {
  const original = firecrawl.scrape;
  (firecrawl as any).scrape = async () => ({
    rawHtml: JSON.stringify([
      { PageUrl: "sample-property", BuildingName: "Sample Property" },
      { PageUrl: "-", BuildingName: "Unlinked Property" },
    ]),
  });
  try {
    await assert.rejects(
      srcTranswestern("lease", 1, false),
      /Transwestern lease\/Lease feed row lacks a valid PageUrl identity/
    );
  } finally {
    (firecrawl as any).scrape = original;
  }
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


test("Transwestern real source mapper excludes basemap tiles while preserving property richness and freshness", async () => {
  const original = firecrawl.scrape;
  const tile = "https://mapsresources-pa.googleapis.com/v1/tiles?layer=basemap";
  const gallery = "https://images.transwestern.com/property/gallery.jpg";
  const feedImage = "https://images.transwestern.com/property/feed.jpg";
  const flyer = "https://transwestern.com/property/flyer.pdf";
  const feedRow = {
    PageUrl: "sample-property", BuildingName: "Sample Property", PropertyTypeName: "Office",
    FullAddress: "100 Main Street", City: "Dallas", State: "TX", ZipCode: "75201",
    PropertySize: 10000, Price: 2500000, PropertyImage: feedImage,
  };
  const rawHtml = `<main class="property-dtl"><h1>Sample Property</h1>
    <section class="property-dscptn">Current property overview</section>
    <ul class="property-facts"><li><b>Year Built:</b> 1987</li></ul>
    <div class="photos-list"><a class="chocolat-image" href="${gallery}">Gallery</a></div>
    <table id="tblAttachments"><tr><td><a href="${flyer}">Property flyer</a></td></tr></table>
    <div class="PropertyVcard"><div class="v-card"><h3>Sample Broker</h3>
      <a href="/people/sample-broker">Sample Broker</a><a href="mailto:sample@example.test">Email</a>
      <a href="tel:2145550100">214-555-0100</a></div></div>
    <iframe src="https://player.vimeo.com/video/824804225"></iframe></main>`;
  const markdown = "Sample Property\nCurrent property overview";
  const calls: string[] = [];
  (firecrawl as any).scrape = async (url: string) => {
    calls.push(url);
    if (url.includes("/properties?")) return { rawHtml: JSON.stringify([feedRow]) };
    return { rawHtml, markdown, links: [flyer], images: [tile, gallery] };
  };
  try {
    const result = await srcTranswestern("sale", 1, false);
    assert.equal(calls.filter((url) => url === "https://transwestern.com/property/sample-property").length, 1);
    assert.equal(result.listings.length, 1);
    const listing = result.listings[0]!;
    assert.equal(listing.detailError, undefined);
    assert.deepEqual([...listing.photos].sort(), [gallery, feedImage].sort());
    assert.equal(listing.id, "sample-property");
    assert.equal(listing.canonicalUrl, "https://transwestern.com/property/sample-property");
    assert.equal(listing.transactionType, "Sale");
    assert.equal(listing.buildingSizeSqft, 10000);
    assert.equal(listing.salePriceUsd, 2500000);
    assert.equal(listing.yearBuilt, 1987);
    assert.deepEqual(listing.rawTranswesternFeed, feedRow);
    assert.equal(listing.markdown, markdown);
    assert.ok(listing.documents.some((item: { url: string }) => item.url === flyer));
    assert.ok(listing.media.some((item: { provider: string }) => item.provider === "vimeo"));
    assert.equal(listing.contactsDetailed.length, 1);
    assert.equal(listing.contactsDetailed[0].name, "Sample Broker");
    assert.equal(listing.contactsDetailed[0].email, "sample@example.test");
    assert.equal(listing.brokerIds.length, 1);
    assert.equal(listing.freshnessProvenance.method, "transwestern_detail_page");
    assert.equal(listing.freshnessProvenance.cacheDisposition, "live");
    assert.ok(Number.isFinite(Date.parse(listing.inventoryObservedAt)));
    assert.ok(Date.parse(listing.detailObservedAt) >= Date.parse(listing.inventoryObservedAt));
    assert.deepEqual(listing.detailScrape, {
      url: listing.url, markdownLength: markdown.length, rawHtmlLength: rawHtml.length, linkCount: 1,
    });
  } finally {
    (firecrawl as any).scrape = original;
  }
});


test("Transwestern tile-only native gallery and feed image cannot bypass the shared image filter", async () => {
  const original = firecrawl.scrape;
  const tile = "https://mapsresources-pa.googleapis.com/v1/tiles?layer=basemap";
  const flyer = "https://transwestern.com/property/flyer.pdf";
  const feedRow = {
    PageUrl: "sample-property", BuildingName: "Sample Property", PropertyTypeName: "Office",
    PropertyImage: tile, PropertySize: 10000,
  };
  (firecrawl as any).scrape = async (url: string) => {
    if (url.includes("/properties?")) return { rawHtml: JSON.stringify([feedRow]) };
    return {
      rawHtml: `<main class="property-dtl"><h1>Sample Property</h1>
        <section class="property-dscptn">Current detail without property photos</section>
        <div class="photos-list"><a class="chocolat-image" href="${tile}">Map</a></div>
        <table id="tblAttachments"><tr><td><a href="${flyer}">Flyer</a></td></tr></table></main>`,
      markdown: "Current detail without property photos", links: [flyer], images: [],
    };
  };
  try {
    const result = await srcTranswestern("sale", 1, false);
    assert.equal(result.listings.length, 1);
    const listing = result.listings[0]!;
    assert.equal(listing.photos, undefined, "prune omits an empty photos array; no native tile may reappear");
    assert.equal(listing.detailError, undefined);
    assert.equal(listing.id, "sample-property");
    assert.equal(listing.name, "Sample Property");
    assert.equal(listing.buildingSizeSqft, 10000);
    assert.deepEqual(listing.rawTranswesternFeed, feedRow);
    assert.equal(listing.markdown, "Current detail without property photos");
    assert.ok(listing.documents.some((item: { url: string }) => item.url === flyer));
    assert.equal(listing.freshnessProvenance.cacheDisposition, "live");
    assert.ok(Number.isFinite(Date.parse(listing.detailObservedAt)));
  } finally {
    (firecrawl as any).scrape = original;
  }
});
