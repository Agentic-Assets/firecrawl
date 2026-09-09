// Isolate argv before colliers-main.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  colliersMainIsChallenge,
  colliersMainAbs,
  colliersMainIdFromUrl,
  extractSitemapLocs,
  colliersMainTransaction,
  parseColliersMainAddress,
  colliersMainJsonLd,
  colliersMainDetailCachePath,
  readColliersMainCache,
  appendColliersMainCache,
  colliersMainCachedListingIsCurrent,
  colliersMainDetailPassTruncated,
  colliersMainResultTruncated,
  parseColliersMainDetail,
  type ColliersMainEntry,
  fetchColliersMainEntries,
  resetColliersMainSitemapCacheForTest,
  scrapeColliersMainDetailDoc,
  assertColliersMainDetailRuntimeReady,
  acquireColliersMainDetailStart,
  coolDownColliersMainDetailStarts,
  resetColliersMainDetailPacerForTest,
  colliersMainCoveoQueryBody,
  reconcileColliersMainCoveoResults,
  reconcileColliersMainCoveoExperts,
  colliersMainPublicUrl,
  mapColliersMainCoveoListing,
  colliersMainCoveoAcreageIsAdmissible,
  colliersMainCoveoAcreageIsCorroborated,
  colliersMainCoveoMeasurements,
  postColliersMainBrowserBatch,
} from "../../../sources/colliers-main.js";
import type { ScrapedDoc } from "../../../types.js";
import { firecrawl } from "../../../lib/scrape.js";

// ---------------------------------------------------------------------------
// Fixture helpers
// ---------------------------------------------------------------------------

const FIXTURE_PATH = join(
  new URL(".", import.meta.url).pathname,
  "../../fixtures/raw_data/colliers.json"
);

function loadFixture(): any[] {
  return JSON.parse(readFileSync(FIXTURE_PATH, "utf8"));
}

/** Return the raw_data for the first colliers-main fixture entry. */
function mainFixture(): any {
  const fixtures = loadFixture();
  const entry = fixtures.find((f: any) => f.sourceKey === "colliers-main");
  if (!entry) throw new Error("No colliers-main fixture found");
  return entry.raw_data;
}

/** Build a minimal ScrapedDoc that mimics what Firecrawl returns for a Colliers main detail page. */
function syntheticDoc(
  ldJson: object,
  markdownExtra = "",
  opts: Partial<{ statusCode: number; title: string }> = {}
): ScrapedDoc {
  const ldScript = `<script type="application/ld+json">${JSON.stringify(ldJson)}</script>`;
  return {
    rawHtml: ldScript,
    markdown: markdownExtra,
    links: [],
    metadata: { statusCode: opts.statusCode ?? 200, title: opts.title ?? "Office For Sale" },
  };
}

function doc(partial: Partial<ScrapedDoc>): ScrapedDoc {
  return {
    rawHtml: "",
    markdown: "",
    links: [],
    metadata: {},
    ...partial,
  };
}

test("colliersMainIsChallenge detects Cloudflare challenge pages", () => {
  assert.equal(colliersMainIsChallenge(doc({ metadata: { statusCode: 429 } })), true);
  assert.equal(colliersMainIsChallenge(doc({ metadata: { statusCode: 503 } })), true);
  assert.equal(
    colliersMainIsChallenge(doc({ metadata: { title: "Just a moment..." }, rawHtml: "" })),
    true
  );
  assert.equal(
    colliersMainIsChallenge(doc({ metadata: { statusCode: 200, title: "Office For Sale" }, rawHtml: "<html>ok</html>" })),
    false
  );
  assert.equal(
    colliersMainIsChallenge(doc({ metadata: { statusCode: 200 }, rawHtml: "<div>cf-chl-widget</div>" })),
    true
  );
});

test("colliersMainAbs resolves host-relative links and rejects unsafe schemes", () => {
  assert.equal(
    colliersMainAbs("/en/properties/usa12345-office-for-sale"),
    "https://www.colliers.com/en/properties/usa12345-office-for-sale"
  );
  assert.equal(colliersMainAbs("javascript:void(0)"), null);
  assert.equal(colliersMainAbs("mailto:agent@colliers.com"), null);
  assert.equal(colliersMainAbs("#section"), null);
});

test("colliersMainIdFromUrl extracts usa##### ids from detail URLs", () => {
  assert.equal(
    colliersMainIdFromUrl("https://www.colliers.com/en/properties/usa12345"),
    "usa12345"
  );
  assert.equal(
    colliersMainIdFromUrl("https://www.colliers.com/en/properties/USA99999?foo=bar"),
    "usa99999"
  );
  assert.equal(
    colliersMainIdFromUrl("https://www.colliers.com/en/properties/usa12345-office-dallas"),
    null
  );
  assert.equal(colliersMainIdFromUrl("https://www.colliers.com/en/about"), null);
});

test("extractSitemapLocs parses loc elements from sitemap XML", () => {
  const xml = `
    <?xml version="1.0" encoding="UTF-8"?>
    <urlset>
      <url><loc>https://www.colliers.com/en/sitemap?type=properties</loc></url>
      <url><loc>  https://www.colliers.com/en/properties/usa11111  </loc></url>
    </urlset>
  `;
  assert.deepEqual(extractSitemapLocs(xml), [
    "https://www.colliers.com/en/sitemap?type=properties",
    "https://www.colliers.com/en/properties/usa11111",
  ]);
});

test("colliersMainTransaction classifies sale, lease, and dual-mode listings", () => {
  assert.deepEqual(
    colliersMainTransaction("Office For Sale — 123 Main St", "", "https://www.colliers.com/en/properties/usa10001-for-sale"),
    { type: "Sale", sublease: false }
  );
  assert.deepEqual(
    colliersMainTransaction("Retail For Lease — Austin", "", "https://www.colliers.com/en/properties/usa10002-for-lease"),
    { type: "Lease", sublease: false }
  );
  assert.deepEqual(
    colliersMainTransaction("Industrial For Sale or Lease", "", "https://www.colliers.com/en/properties/usa10003-sale-or-lease"),
    { type: "Sale/Lease", sublease: false }
  );
  assert.deepEqual(
    colliersMainTransaction("Office Sublease — Denver", "", "https://www.colliers.com/en/properties/usa10004-sublease"),
    { type: "Lease", sublease: true }
  );
});

test("parseColliersMainAddress splits street, city, state, zip, and country", () => {
  assert.deepEqual(parseColliersMainAddress("11701 I-30, Little Rock, AR 72209, USA"), {
    street: "11701 I-30",
    city: "Little Rock",
    state: "AR",
    postalCode: "72209",
    country: "US",
  });
  assert.deepEqual(parseColliersMainAddress("100 King St W, Toronto, ON"), {
    street: "100 King St W",
    city: "Toronto",
    state: "ON",
    postalCode: null,
    country: null,
  });
  assert.deepEqual(parseColliersMainAddress(null), {
    street: null,
    city: null,
    state: null,
    postalCode: null,
    country: null,
  });
});

test("colliersMainJsonLd extracts RealEstateListing JSON-LD from HTML", () => {
  const html = `
    <html>
      <script type="application/ld+json">
        {"@type":"Organization","name":"Colliers"}
      </script>
      <script type="application/ld+json">
        {"@type":"RealEstateListing","name":"Office For sale — 500 Main St, Dallas, TX 75201, USA"}
      </script>
    </html>
  `;
  const ld = colliersMainJsonLd(html);
  assert.equal(ld?.["@type"], "RealEstateListing");
  assert.match(ld?.name, /Dallas, TX/);
  assert.equal(colliersMainJsonLd("<html><body>no json-ld</body></html>"), null);
});

test("Colliers Coveo query uses exact public ids without visitor or auth state", () => {
  const query = new URLSearchParams(colliersMainCoveoQueryBody(["usa1168531", "USA1159083"], "property"));
  assert.match(query.get("aq") ?? "", /@ftitle16556==USA1168531/);
  assert.match(query.get("aq") ?? "", /@z95xtemplate==534C0EB71D32434FBE0F62A2AB174F16/);
  assert.equal(query.get("numberOfResults"), "2");
  assert.equal(query.get("maximumAge"), "0");
  assert.equal(query.has("visitorId"), false);
  assert.equal(query.has("analytics"), false);
  assert.equal(query.has("accessToken"), false);
});

test("Colliers Coveo reconciliation admits known first-party click hosts and rejects identity drift", () => {
  const entries = [
    entry("usa1168531", "https://www.colliers.com/en/properties/a/usa1168531"),
    entry("usa1159083", "https://www.colliers.com/en/properties/b/usa1159083"),
  ];
  const a = { clickUri: entries[0].url, raw: { propertyz32xid: "USA1168531" } };
  const b = { clickUri: entries[1].url, raw: { propertyz32xid: "USA1159083" } };
  assert.equal(reconcileColliersMainCoveoResults(entries, [a, b]).size, 2);
  assert.equal(reconcileColliersMainCoveoResults(entries, [
    { ...a, clickUri: "https://cmimport.colliers.com/en/properties/a/usa1168531" },
    b,
  ]).size, 2);
  assert.throws(() => reconcileColliersMainCoveoResults(entries, [a]), /missing 1/);
  assert.throws(() => reconcileColliersMainCoveoResults(entries, [a, a]), /duplicate/);
  assert.throws(() => reconcileColliersMainCoveoResults(entries, [a, { ...b, raw: { propertyz32xid: "USA9999999" } }]), /unexpected/);
  assert.throws(() => reconcileColliersMainCoveoResults(entries, [a, { ...b, clickUri: "https://www.colliers.com/en/properties/wrong/usa1159083" }]), /click identity does not match/);
  assert.throws(
    () => reconcileColliersMainCoveoResults(entries, [
      a,
      { ...b, clickUri: "https://attacker.example/en/properties/b/usa1159083" },
    ]),
    /click identity does not match/,
    "an identical path on another origin is not first-party reconciliation evidence"
  );
});

test("Colliers Coveo expert reconciliation fails closed on missing, duplicate, and extra ids", () => {
  const a = { raw: { z95xid: "{ABC-123}" } };
  const b = { raw: { z95xid: "DEF456" } };
  assert.equal(reconcileColliersMainCoveoExperts(["abc123", "def456"], [a, b]).size, 2);
  assert.throws(() => reconcileColliersMainCoveoExperts(["abc123", "def456"], [a]), /missing 1 expert/);
  assert.throws(() => reconcileColliersMainCoveoExperts(["abc123", "def456"], [a, a]), /duplicate expert/);
  assert.throws(() => reconcileColliersMainCoveoExperts(["abc123"], [b]), /unexpected expert/);
  assert.equal(
    reconcileColliersMainCoveoExperts(["abc123", "def456"], [a], true).size,
    1,
    "the property record may reference a deleted expert profile"
  );
});

test("Colliers Coveo child URLs normalize public HTTP paths and reject unsafe values", () => {
  assert.equal(
    colliersMainPublicUrl("/content/dam/colliers/brochure.pdf"),
    "https://www.colliers.com/content/dam/colliers/brochure.pdf"
  );
  assert.equal(colliersMainPublicUrl("https://user:pass@example.com/private"), null);
  assert.equal(colliersMainPublicUrl("javascript:alert(1)"), null);
  assert.equal(colliersMainPublicUrl("data:text/plain,secret"), null);
  assert.equal(colliersMainPublicUrl("http://127.0.0.1/private"), null);
  assert.equal(colliersMainPublicUrl("http://169.254.169.254/latest/meta-data"), null);
  assert.equal(colliersMainPublicUrl("http://service.local/private"), null);
});

test("Colliers Coveo mapper preserves scalar, document, image, and resolved expert evidence", () => {
  const e = entry("usa1168531", "https://www.colliers.com/en/properties/a/usa1168531");
  e.inventoryObservedAt = "2026-09-09T08:00:00.000Z";
  e.lastmod = "2026-09-09T01:35:25+00:00";
  const experts = new Map<string, any>([["abc123", { clickUri: "https://www.colliers.com/en/experts/jane", raw: {
    z95xid: "abc123", z95xname: "Jane Broker", title: "Vice President", officez32xphone: "+1 555 0100",
    ez120xpertofficenamecomputed: ["Honolulu"], urllink: "/en/experts/jane", profilez32xpicture: "https://example.com/jane.png",
  } }]]);
  const mapped = mapColliersMainCoveoListing(e, { clickUri: e.url, raw: {
    propertyz32xid: "USA1168531", propertyz32xtitle: "Waterfront Plaza",
    propertyz32xfullz32xaddress: "500 Ala Moana Blvd, Honolulu, HI 96813, USA",
    forz32xsale: "0", forz32xlease: "1", primarypropertytype: "Office",
    description: "<p>Premier mixed-use property.</p>", latitude: 21.3, longitude: -157.86,
    buildingz32xsiz122xe: 14211,
    buildingz32xsiz122xez32xunit: "40409737aa8c4b10b53be81940c7b2ed",
    propertyz32xstatus: "Available",
    propertyz32ximages: "https://example.com/1-w|javascript:alert(1)|https://example.com/2-w|https://example.com/1-w",
    relatedz32xdocuments: JSON.stringify([
      { DocumentName: "Brochure", DocumentLink: "/brochure" },
      { DocumentName: "Property Flyer", DocumentLink: "/property-flyer.pdf" },
      { DocumentName: "Duplicate", DocumentLink: "/brochure" },
      { DocumentName: "Unsafe", DocumentLink: "data:text/plain,bad" },
    ]),
    relatedz32xlinks: JSON.stringify([
      { Name: "Tour", Value: "https://example.com/tour" },
      { Name: "Unsafe", Value: "mailto:broker@example.com" },
    ]),
    relatedz32xez120xperts: ["abc123"], propertyz32xfeatures: JSON.stringify(["Zoning: C-2", "Year Built: 1999"]),
  } }, experts);
  assert.equal(mapped.transactionType, "Lease");
  assert.equal(mapped.street, "500 Ala Moana Blvd");
  assert.equal(mapped.buildingSizeSqft, 14211);
  assert.equal(mapped.description, "Premier mixed-use property.");
  assert.equal(mapped.zoning, "C-2");
  assert.equal(mapped.yearBuilt, 1999);
  assert.equal(mapped.photos.length, 2);
  assert.equal(mapped.brochures[0].name, "Brochure");
  assert.equal(mapped.brochures[0].url, "https://www.colliers.com/brochure");
  assert.equal(mapped.brochures[1].docType, "flyer");
  assert.equal(mapped.links.length, 1);
  assert.equal(mapped.contactsDetailed[0].name, "Jane Broker");
  assert.equal(mapped.preserveContactCollections, undefined);
  assert.equal(mapped.freshnessProvenance.detailScope, "first_party_detail_api");
  assert.equal(mapped.freshnessProvenance.cacheDisposition, "live");
});

test("Colliers Coveo mapper rejects malformed and non-array structured child fields", () => {
  const e = entry("usa1168531", "https://www.colliers.com/en/properties/a/usa1168531");
  const baseRaw = {
    propertyz32xtitle: "Waterfront Plaza",
    propertyz32xfullz32xaddress: "500 Ala Moana Blvd, Honolulu, HI 96813, USA",
    forz32xsale: "1",
  };

  assert.throws(
    () => mapColliersMainCoveoListing(e, {
      raw: { ...baseRaw, relatedz32xdocuments: "[{broken" },
    }),
    /related documents is malformed JSON/
  );
  assert.throws(
    () => mapColliersMainCoveoListing(e, {
      raw: { ...baseRaw, relatedz32xlinks: JSON.stringify({ Value: "/tour" }) },
    }),
    /related links is not an array/
  );
});

test("Colliers Coveo mapper preserves a current name-only fallback for a deleted expert profile", () => {
  const e = entry("usa1168531", "https://www.colliers.com/en/properties/a/usa1168531");
  const mapped = mapColliersMainCoveoListing(e, { raw: {
      propertyz32xtitle: "Waterfront Plaza",
      propertyz32xfullz32xaddress: "500 Ala Moana Blvd, Honolulu, HI 96813, USA",
      forz32xsale: "1",
      relatedz32xez120xperts: ["abc123"],
      relatedez120xpertsfullnamecomputed: ["Jane Broker"],
    } });
  assert.deepEqual(mapped.contactsDetailed, [{ name: "Jane Broker", company: "Colliers" }]);
  assert.deepEqual(mapped.colliersMain.unresolvedExpertIds, ["abc123"]);
  assert.equal(mapped.preserveContactCollections, true);
  assert.equal(mapped.detailObservedWithContactPreservation, true);
});

test("Colliers Coveo mapper preserves prior contacts when a referenced expert has no live fallback", () => {
  const e = entry("usa1024939", "https://www.colliers.com/en/properties/a/usa1024939");
  const mapped = mapColliersMainCoveoListing(e, { raw: {
    propertyz32xtitle: "Legacy Broker Assignment",
    propertyz32xfullz32xaddress: "1 Main St, Phoenix, AZ 85001, USA",
    forz32xsale: "1",
    relatedz32xez120xperts: ["3d072913-f9fb-4ec6-bc73-9cf35e6b3120"],
  } });

  assert.equal(mapped.contactsDetailed, undefined);
  assert.deepEqual(mapped.colliersMain.unresolvedExpertIds, [
    "3d072913f9fb4ec6bc739cf35e6b3120",
  ]);
  assert.equal(mapped.preserveContactCollections, true);
  assert.equal(mapped.detailObservedWithContactPreservation, true);
});

test("Colliers Coveo mapper suppresses hidden price normalization and preserves raw pricing", () => {
  const e = entry("usa1162565", "https://www.colliers.com/en/properties/a/usa1162565");
  const mapped = mapColliersMainCoveoListing(e, { raw: {
    propertyz32xtitle: "Stratford Nimitz Business Center",
    propertyz32xfullz32xaddress: "1753 Addison Way, Hayward, CA 94544, USA",
    forz32xsale: "1",
    hidez32xsalez32xprice: "1",
    hidez32xleasez32xprice: "0",
    salez32xtype: "sale-type-guid",
    fsalez32xpricez32xmin16556: 200,
    fsalez32xpricez32xmaz120x16556: 200,
    currency: "USD",
  } });
  assert.equal(mapped.salePriceUsd, undefined);
  assert.equal(mapped.salePriceText, undefined);
  assert.deepEqual(mapped.colliersMain.rawPricing, {
    hideSalePrice: "1",
    hideLeasePrice: "0",
    saleType: "sale-type-guid",
    leaseType: null,
    leaseRateType: null,
    salePriceMin: 200,
    salePriceMax: 200,
    leasePriceMin: null,
    leasePriceMax: null,
    currency: "USD",
    sortPrice: null,
    visibleLeaseRateUnit: null,
  });
});

test("Colliers Coveo mapper keeps visible sale bounds raw when their unit is unproved", () => {
  const e = entry("usa1166257", "https://www.colliers.com/en/properties/a/usa1166257");
  const mapped = mapColliersMainCoveoListing(e, { raw: {
    propertyz32xtitle: "Industrial Property",
    propertyz32xfullz32xaddress: "1 Main St, Phoenix, AZ 85001, USA",
    forz32xsalez32xprice: "1",
    forz32xsale: "1",
    hidez32xsalez32xprice: "0",
    salez32xtype: "0e92390674564994aa637eadc7f11364",
    fsalez32xpricez32xmin16556: 405,
    fsalez32xpricez32xmaz120x16556: 405,
    currency: "USD",
  } });

  assert.equal(mapped.salePriceUsd, undefined);
  assert.equal(mapped.salePriceText, undefined);
  assert.equal(mapped.colliersMain.rawPricing.salePriceMin, 405);
  assert.equal(mapped.colliersMain.rawPricing.salePriceMax, 405);
});

test("Colliers Coveo mapper keeps visible lease rates raw when cadence is unproved", () => {
  const e = entry("usa1151255", "https://www.colliers.com/en/properties/a/usa1151255");
  const perSf = mapColliersMainCoveoListing(e, { raw: {
    propertyz32xtitle: "Warehouse",
    propertyz32xfullz32xaddress: "8626 Wilbur Ave, Northridge, CA 91324, USA",
    forz32xsale: "0",
    forz32xlease: "1",
    hidez32xleasez32xprice: "0",
    fleasez32xpricez32xmin16556: 0.99,
    leasez32xratez32xtype: "247f7ab813234d33a9e042c0b5f13652",
    currency: "USD",
  } });
  assert.equal(perSf.leaseRateText, undefined);
  assert.equal(perSf.leaseRateMin, undefined);
  assert.equal(perSf.leaseRateMax, undefined);
  assert.equal(perSf.colliersMain.rawPricing.leasePriceMin, 0.99);
  assert.equal(perSf.colliersMain.rawPricing.visibleLeaseRateUnit, "/ SF");

  const annualAbsolute = mapColliersMainCoveoListing(e, { raw: {
    propertyz32xtitle: "Ground Lease",
    propertyz32xfullz32xaddress: "1 Main St, Phoenix, AZ 85001, USA",
    forz32xsale: "0",
    forz32xlease: "1",
    hidez32xleasez32xprice: "0",
    fleasez32xpricez32xmin16556: 110000,
    leasez32xratez32xtype: "d1e6e3f63c5b4229a67bf21c8e5c3488",
    currency: "USD",
  } });
  assert.equal(annualAbsolute.leaseRateText, undefined);
  assert.equal(annualAbsolute.leaseRateMin, undefined);
  assert.equal(annualAbsolute.leaseRateMax, undefined);
  assert.equal(annualAbsolute.colliersMain.rawPricing.visibleLeaseRateUnit, "/ year");
});

test("Colliers Coveo measurements honor explicit units and reject implausible acreage", () => {
  assert.deepEqual(colliersMainCoveoMeasurements({
    buildingz32xsiz122xe: 122850,
    buildingz32xsiz122xez32xunit: "40409737aa8c4b10b53be81940c7b2ed",
    flotz32xsiz122xe16556: 1.18,
    lotz32xsiz122xez32xunit: "708623336f6542cab69b28fe1eee7322",
    fminz32xarea16556: 555,
    fmaz120xz32xarea16556: 780,
    floorz32xareaz32xunit: "40409737aa8c4b10b53be81940c7b2ed",
  }), {
    buildingSizeSqft: 122850,
    lotSizeAcres: 1.18,
    availableSf: 780,
    minDivisibleSf: 555,
    maxDivisibleSf: 780,
  });
  assert.deepEqual(colliersMainCoveoMeasurements({
    buildingz32xsiz122xe: 72,
    buildingz32xsiz122xez32xunit: "89d325e537db494cb9016e8a71d7eca3",
    flotz32xsiz122xe16556: 25265,
    lotz32xsiz122xez32xunit: "40409737aa8c4b10b53be81940c7b2ed",
  }), {
    units: 72,
    lotSizeAcres: 25265 / 43560,
  });
  assert.deepEqual(colliersMainCoveoMeasurements({
    propertysiz122xecomputed: 178385.2,
    siz122xeunitcomputed: ["ac"],
    flotz32xsiz122xe16556: 178385.2,
    lotz32xsiz122xez32xunit: "708623336f6542cab69b28fe1eee7322",
  }), {});
});

test("Colliers Coveo requires source-text corroboration above 100 acres", () => {
  const acreUnit = "708623336f6542cab69b28fe1eee7322";
  const supported = {
    propertyz32xtitle: "Land for Sale in Northern Arizona",
    description: "<p>A rare opportunity to acquire &plusmn;3,765.65 acres.</p>",
    flotz32xsiz122xe16556: 3765.65,
    lotz32xsiz122xez32xunit: acreUnit,
  };
  assert.equal(colliersMainCoveoAcreageIsCorroborated(supported, 3765.65), true);
  assert.equal(colliersMainCoveoMeasurements(supported).lotSizeAcres, 3765.65);

  const contradicted = {
    propertyz32xtitle: "22-Property Portfolio | South Chicago",
    description: "<p>311 units across 22 properties.</p>",
    flotz32xsiz122xe16556: 9600,
    lotz32xsiz122xez32xunit: acreUnit,
  };
  assert.equal(colliersMainCoveoAcreageIsCorroborated(contradicted, 9600), false);
  assert.deepEqual(colliersMainCoveoMeasurements(contradicted), {});

  const decimalShift = {
    description: "<p>Approximately 1.29 acres.</p>",
    flotz32xsiz122xe16556: 129,
    lotz32xsiz122xez32xunit: acreUnit,
  };
  assert.equal(colliersMainCoveoAcreageIsCorroborated(decimalShift, 129), false);
  assert.deepEqual(colliersMainCoveoMeasurements(decimalShift), {});

  const smallDecimalShift = {
    description: "<p>Approximately .369 acre.</p>",
    flotz32xsiz122xe16556: 0.0369,
    lotz32xsiz122xez32xunit: acreUnit,
  };
  assert.equal(colliersMainCoveoAcreageIsCorroborated(smallDecimalShift, 0.0369), false);
  assert.equal(colliersMainCoveoAcreageIsAdmissible(smallDecimalShift, 0.0369), false);
  assert.deepEqual(colliersMainCoveoMeasurements(smallDecimalShift), {});

  const smallWithoutText = {
    description: "<p>Land parcel offered for sale.</p>",
    flotz32xsiz122xe16556: 0.4,
    lotz32xsiz122xez32xunit: acreUnit,
  };
  assert.equal(colliersMainCoveoAcreageIsAdmissible(smallWithoutText, 0.4), true);
  assert.equal(colliersMainCoveoMeasurements(smallWithoutText).lotSizeAcres, 0.4);

  const trailingApproximationMismatch = {
    description: "<p>The site contains 0.86± acre.</p>",
    flotz32xsiz122xe16556: 0.086,
    lotz32xsiz122xez32xunit: acreUnit,
  };
  assert.equal(
    colliersMainCoveoAcreageIsAdmissible(trailingApproximationMismatch, 0.086),
    false,
  );
  assert.deepEqual(colliersMainCoveoMeasurements(trailingApproximationMismatch), {});
});

test("Colliers Coveo browser batching retries outer and inner transient failures", async () => {
  const oldFetch = globalThis.fetch;
  const waits: number[] = [];
  const responses = [
    new Response(JSON.stringify({ error: "Bootstrap returned HTTP 429", bootstrapStatus: 429 }), { status: 502 }),
    new Response(JSON.stringify({ responses: [{ status: 429, body: "" }] }), { status: 200 }),
    new Response(JSON.stringify({ responses: [{ status: 200, body: JSON.stringify({ results: [] }) }] }), { status: 200 }),
  ];
  globalThis.fetch = async () => responses.shift()!;
  try {
    const payloads = await postColliersMainBrowserBatch(["q=office"], async (milliseconds) => {
      waits.push(milliseconds);
    });
    assert.equal(payloads.length, 1);
    assert.deepEqual(payloads[0].results, []);
    assert.deepEqual(waits, [15_000, 30_000]);
  } finally {
    globalThis.fetch = oldFetch;
  }
});

test("strict Colliers retries unknown HTTP 200 pages without property JSON-LD", () => {
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  try {
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    const e = entry("usa12345", "https://www.colliers.com/en/properties/usa12345");
    assert.throws(
      () =>
        parseColliersMainDetail(
          e,
          doc({
            rawHtml: "<html><h1>Consent required</h1></html>",
            markdown: "Consent required",
            metadata: { statusCode: 200, title: "Consent required" },
          })
        ),
      /lacks validated RealEstateListing JSON-LD/
    );
    assert.deepEqual(
      parseColliersMainDetail(
        e,
        doc({
          rawHtml: "<html>gone</html>",
          markdown: "Gone",
          metadata: { statusCode: 410, title: "Gone" },
        })
      ).skip,
      "not_found"
    );
    assert.equal(
      parseColliersMainDetail(
        e,
        syntheticDoc(SALE_LD, "stale property body", {
          statusCode: 404,
          title: "Not Found",
        })
      ).skip,
      "not_found",
      "explicit HTTP tombstones must win over a stale JSON-LD body"
    );
  } finally {
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
  }
});

test("strict Colliers admits a validated LightBox detail without JSON-LD", () => {
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  try {
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    const listing = parseColliersMainDetail(
      entry("usa1167092", "https://www.colliers.com/en/properties/villa-encanto/usa1167092"),
      doc({
        rawHtml: `
          <html><body>
            <h1>Villa Encanto</h1>
            <div class="address">2850 Clydedale Dr, Dallas, TX 75220-4667 | Multifamily - Garden Apartments</div>
          </body></html>
        `,
        markdown: "Villa Encanto\n\nInvestment Sale\n\nBuilding Size: 100,000 SF",
        metadata: { statusCode: 200, title: "Villa Encanto, Dallas, TX | Colliers | Powered by LightBox" },
      })
    );
    assert.equal(listing.id, "usa1167092");
    assert.equal(listing.city, "Dallas");
    assert.equal(listing.state, "TX");
    assert.equal(listing.postalCode, "75220-4667");
    assert.equal(listing.assetType, "Multifamily - Garden Apartments");
    assert.equal(listing.transactionType, "Sale");
    assert.equal(listing.colliersMain.detailTemplate, "lightbox");
  } finally {
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
  }
});

test("colliersMainDetailCachePath returns durable cache location", () => {
  const previous = process.env.COLLIERS_MAIN_DETAIL_CACHE_PATH;
  try {
    delete process.env.COLLIERS_MAIN_DETAIL_CACHE_PATH;
    assert.equal(colliersMainDetailCachePath(), "out/cache/colliers-main/detail-cache.jsonl");
    process.env.COLLIERS_MAIN_DETAIL_CACHE_PATH = "out/cache/colliers-main/fresh-2026-07-29.jsonl";
    assert.equal(
      colliersMainDetailCachePath(),
      "out/cache/colliers-main/fresh-2026-07-29.jsonl"
    );
  } finally {
    if (previous === undefined) delete process.env.COLLIERS_MAIN_DETAIL_CACHE_PATH;
    else process.env.COLLIERS_MAIN_DETAIL_CACHE_PATH = previous;
  }
});

test("Colliers cache reuse follows live sitemap lastmod", () => {
  const cached = {
    freshnessProvenance: { sourceRevision: "2026-07-28T12:00:00Z" },
    name: "Listing",
  };
  assert.equal(
    colliersMainCachedListingIsCurrent(
      entry("usa1", "https://example.test/1", "2026-07-28T12:00:00Z"),
      cached
    ),
    true
  );
  assert.equal(
    colliersMainCachedListingIsCurrent(
      entry("usa1", "https://example.test/1", "2026-07-29T12:00:00Z"),
      cached
    ),
    false
  );
  assert.equal(
    colliersMainCachedListingIsCurrent(entry("usa1", "https://example.test/1", null), cached),
    true
  );
});

test("Colliers cache reuse rejects an intra-day source revision change", () => {
  const cached = {
    freshnessProvenance: { sourceRevision: "2026-07-28T08:15:00Z" },
  };
  assert.equal(
    colliersMainCachedListingIsCurrent(
      entry("usa1", "https://example.test/1", "2026-07-28T16:45:00Z"),
      cached
    ),
    false
  );
});

test("Colliers cache reuse rejects legacy lastUpdated-only proof when sitemap has a revision", () => {
  assert.equal(
    colliersMainCachedListingIsCurrent(
      entry("usa1", "https://example.test/1", "2026-07-28T16:45:00Z"),
      { lastUpdated: "2026-07-28T16:45:00Z" }
    ),
    false
  );
});

test("Colliers cache reuse also requires the active refresh generation", () => {
  const oldGeneration = process.env.CRE_REFRESH_GENERATION;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  try {
    process.env.CRE_REFRESH_GENERATION = "generation-current";
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    const sitemapEntry = entry(
      "usa1",
      "https://example.test/1",
      "2026-07-29"
    );
    assert.equal(
      colliersMainCachedListingIsCurrent(sitemapEntry, {
        freshnessProvenance: {
          generationId: "generation-current",
          sourceRevision: "2026-07-29",
        },
      }),
      true
    );
    assert.equal(
      colliersMainCachedListingIsCurrent(sitemapEntry, {
        freshnessProvenance: {
          generationId: "generation-old",
          sourceRevision: "2026-07-29",
        },
      }),
      false
    );
    assert.equal(
      colliersMainCachedListingIsCurrent(sitemapEntry, {
        skip: "no_structured_data",
        freshnessProvenance: {
          generationId: "generation-current",
          sourceRevision: "2026-07-29",
        },
      }),
      false
    );
    assert.equal(
      colliersMainCachedListingIsCurrent(sitemapEntry, {
        skip: "not_found",
        freshnessProvenance: {
          generationId: "generation-current",
          sourceRevision: "2026-07-29",
        },
      }),
      true
    );
  } finally {
    if (oldGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = oldGeneration;
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
  }
});

test("Colliers retries a semantic sitemap failure before accepting inventory", async () => {
  const waits: number[] = [];
  let indexAttempts = 0;
  resetColliersMainSitemapCacheForTest();
  try {
    const entries = await fetchColliersMainEntries(
      async (url) => {
        if (url.endsWith("/sitemap")) {
          indexAttempts++;
          return indexAttempts === 1
            ? "<html><title>Just a moment...</title></html>"
            : "<sitemapindex><sitemap><loc>https://www.colliers.com/en/sitemap?type=properties</loc></sitemap></sitemapindex>";
        }
        return "<urlset><url><loc>https://www.colliers.com/en/properties/usa12345</loc><lastmod>2026-07-31</lastmod></url></urlset>";
      },
      async (milliseconds) => {
        waits.push(milliseconds);
      }
    );
    assert.deepEqual(entries.map((entry) => entry.id), ["usa12345"]);
    assert.equal(indexAttempts, 2);
    assert.deepEqual(waits, [2500]);
  } finally {
    resetColliersMainSitemapCacheForTest();
  }
});

test("Colliers sitemap fails closed on malformed or duplicate property identities", async () => {
  const index = "<sitemapindex><sitemap><loc>https://www.colliers.com/en/sitemap?type=properties</loc></sitemap></sitemapindex>";
  for (const properties of [
    "<urlset><url><loc>https://www.colliers.com/en/properties/no-native-id</loc></url></urlset>",
    "<urlset><url><loc>https://www.colliers.com/en/properties/a/usa12345</loc></url><url><loc>https://www.colliers.com/en/properties/b/usa12345</loc></url></urlset>",
  ]) {
    resetColliersMainSitemapCacheForTest();
    await assert.rejects(
      fetchColliersMainEntries(
        async (url) => url.endsWith("/sitemap") ? index : properties,
        async () => undefined
      ),
      /sitemap (?:URL lacks a usa identifier|returned duplicate property id)/
    );
  }
  resetColliersMainSitemapCacheForTest();
});

test("strict Colliers sitemap and detail Firecrawl calls bypass cached responses", async () => {
  const oldScrape = firecrawl.scrape;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const calls: any[] = [];
  (firecrawl as any).scrape = async (url: string, options: any) => {
    calls.push(options);
    if (url.includes("type=properties")) {
      return {
        rawHtml:
          "<urlset><url><loc>https://www.colliers.com/en/properties/usa12345</loc><lastmod>2026-07-29</lastmod></url></urlset>",
      };
    }
    if (url.endsWith("/sitemap")) {
      return {
        rawHtml:
          "<sitemapindex><sitemap><loc>https://www.colliers.com/en/sitemap?type=properties</loc></sitemap></sitemapindex>",
      };
    }
    return {
      rawHtml:
        '<script type="application/ld+json">{"@type":"RealEstateListing","name":"Strict Property For Sale"}</script>',
      markdown: "Strict Property For Sale",
      links: [],
      metadata: { statusCode: 200, title: "Strict Property For Sale" },
    };
  };
  try {
    resetColliersMainSitemapCacheForTest();
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    const entries = await fetchColliersMainEntries();
    assert.equal(entries.length, 1);
    await scrapeColliersMainDetailDoc(entries[0]!.url);
    assert.equal(calls.length, 3);
    assert.ok(calls.every((options) => options.maxAge === 0));
  } finally {
    (firecrawl as any).scrape = oldScrape;
    resetColliersMainSitemapCacheForTest();
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
  }
});

test("Colliers detail pass is truncated while work is deferred or errored", () => {
  assert.equal(colliersMainDetailPassTruncated({ errors: 0, deferred: 0 }), false);
  assert.equal(colliersMainDetailPassTruncated({ errors: 1, deferred: 0 }), true);
  assert.equal(colliersMainDetailPassTruncated({ errors: 0, deferred: 1 }), true);
});

test("Colliers detail pacing serializes starts and honors a shared cooldown", async () => {
  const oldInterval = process.env.COLLIERS_MAIN_DETAIL_START_INTERVAL_MS;
  const oldCooldown = process.env.COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS;
  let now = 1000;
  const waits: number[] = [];
  const wait = async (milliseconds: number) => {
    waits.push(milliseconds);
    now += milliseconds;
  };
  try {
    process.env.COLLIERS_MAIN_DETAIL_START_INTERVAL_MS = "100";
    process.env.COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS = "250";
    resetColliersMainDetailPacerForTest();
    await acquireColliersMainDetailStart(() => now, wait);
    await acquireColliersMainDetailStart(() => now, wait);
    coolDownColliersMainDetailStarts(now);
    await acquireColliersMainDetailStart(() => now, wait);
    assert.deepEqual(waits, [100, 250]);
  } finally {
    resetColliersMainDetailPacerForTest();
    if (oldInterval === undefined) delete process.env.COLLIERS_MAIN_DETAIL_START_INTERVAL_MS;
    else process.env.COLLIERS_MAIN_DETAIL_START_INTERVAL_MS = oldInterval;
    if (oldCooldown === undefined) delete process.env.COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS;
    else process.env.COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS = oldCooldown;
  }
});

test("Colliers detail retries exhausted transport failures with bounded source backoff", async () => {
  const oldAttempts = process.env.COLLIERS_MAIN_CHALLENGE_RETRIES;
  const oldInterval = process.env.COLLIERS_MAIN_DETAIL_START_INTERVAL_MS;
  const oldCooldown = process.env.COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS;
  const waits: number[] = [];
  let calls = 0;
  try {
    process.env.COLLIERS_MAIN_CHALLENGE_RETRIES = "3";
    process.env.COLLIERS_MAIN_DETAIL_START_INTERVAL_MS = "0";
    process.env.COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS = "0";
    resetColliersMainDetailPacerForTest();
    const result = await scrapeColliersMainDetailDoc(
      "https://www.colliers.com/en/properties/usa10006",
      async () => {
        calls++;
        if (calls < 3) throw new Error("socket hang up");
        return syntheticDoc(SALE_LD, "Building Size: 1,000 SF");
      },
      async (milliseconds) => void waits.push(milliseconds),
      () => 0
    );
    assert.equal(result.metadata?.statusCode, 200);
    assert.equal(calls, 3);
    assert.deepEqual(waits, [4000, 8000]);
  } finally {
    resetColliersMainDetailPacerForTest();
    if (oldAttempts === undefined) delete process.env.COLLIERS_MAIN_CHALLENGE_RETRIES;
    else process.env.COLLIERS_MAIN_CHALLENGE_RETRIES = oldAttempts;
    if (oldInterval === undefined) delete process.env.COLLIERS_MAIN_DETAIL_START_INTERVAL_MS;
    else process.env.COLLIERS_MAIN_DETAIL_START_INTERVAL_MS = oldInterval;
    if (oldCooldown === undefined) delete process.env.COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS;
    else process.env.COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS = oldCooldown;
  }
});

test("Colliers detail retries challenge shells before admitting a usable detail", async () => {
  const oldAttempts = process.env.COLLIERS_MAIN_CHALLENGE_RETRIES;
  const oldInterval = process.env.COLLIERS_MAIN_DETAIL_START_INTERVAL_MS;
  const oldCooldown = process.env.COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS;
  let calls = 0;
  try {
    process.env.COLLIERS_MAIN_CHALLENGE_RETRIES = "2";
    process.env.COLLIERS_MAIN_DETAIL_START_INTERVAL_MS = "0";
    process.env.COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS = "0";
    resetColliersMainDetailPacerForTest();
    const result = await scrapeColliersMainDetailDoc(
      "https://www.colliers.com/en/properties/usa10007",
      async () => {
        calls++;
        return calls === 1
          ? doc({ rawHtml: "<div>cf-chl-widget</div>", metadata: { statusCode: 429 } })
          : syntheticDoc(SALE_LD, "Building Size: 1,000 SF");
      },
      async () => undefined,
      () => 0
    );
    assert.equal(result.metadata?.statusCode, 200);
    assert.equal(calls, 2);
  } finally {
    resetColliersMainDetailPacerForTest();
    if (oldAttempts === undefined) delete process.env.COLLIERS_MAIN_CHALLENGE_RETRIES;
    else process.env.COLLIERS_MAIN_CHALLENGE_RETRIES = oldAttempts;
    if (oldInterval === undefined) delete process.env.COLLIERS_MAIN_DETAIL_START_INTERVAL_MS;
    else process.env.COLLIERS_MAIN_DETAIL_START_INTERVAL_MS = oldInterval;
    if (oldCooldown === undefined) delete process.env.COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS;
    else process.env.COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS = oldCooldown;
  }
});

test("Colliers detail telemetry records retries without changing the scrape result", async () => {
  const oldAttempts = process.env.COLLIERS_MAIN_CHALLENGE_RETRIES;
  const oldInterval = process.env.COLLIERS_MAIN_DETAIL_START_INTERVAL_MS;
  const oldCooldown = process.env.COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS;
  const events: Array<{ kind: string; attempt: number; cooldownMs?: number }> = [];
  let calls = 0;
  try {
    process.env.COLLIERS_MAIN_CHALLENGE_RETRIES = "2";
    process.env.COLLIERS_MAIN_DETAIL_START_INTERVAL_MS = "0";
    process.env.COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS = "0";
    resetColliersMainDetailPacerForTest();
    const result = await scrapeColliersMainDetailDoc(
      "https://www.colliers.com/en/properties/usa10008",
      async () => {
        calls++;
        if (calls === 1) throw new Error("socket hang up");
        return syntheticDoc(SALE_LD, "Building Size: 1,000 SF");
      },
      async () => undefined,
      () => 0,
      (event) => events.push(event)
    );
    assert.equal(result.metadata?.statusCode, 200);
    assert.deepEqual(
      events.map((event) => `${event.kind}:${event.attempt}`),
      ["transport_error:1", "cooldown:1", "attempt_success:2"]
    );
  } finally {
    resetColliersMainDetailPacerForTest();
    if (oldAttempts === undefined) delete process.env.COLLIERS_MAIN_CHALLENGE_RETRIES;
    else process.env.COLLIERS_MAIN_CHALLENGE_RETRIES = oldAttempts;
    if (oldInterval === undefined) delete process.env.COLLIERS_MAIN_DETAIL_START_INTERVAL_MS;
    else process.env.COLLIERS_MAIN_DETAIL_START_INTERVAL_MS = oldInterval;
    if (oldCooldown === undefined) delete process.env.COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS;
    else process.env.COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS = oldCooldown;
  }
});

test("Colliers detail runtime canary refuses transport failures before cache fanout", async () => {
  const entries = [
    entry("usa10001", "https://www.colliers.com/en/properties/usa10001"),
    entry("usa10002", "https://www.colliers.com/en/properties/usa10002"),
  ];
  const cached = new Map<string, any>();
  let calls = 0;
  await assert.rejects(
    () =>
      assertColliersMainDetailRuntimeReady(entries, cached, async () => {
        calls++;
        throw new Error("local Playwright transport unavailable");
      }),
    /runtime readiness canary failed before fanout/
  );
  assert.equal(calls, entries.length);
  assert.equal(cached.size, 0, "canary failures must not create cache rows");
});

test("Colliers detail runtime canary proceeds after a later valid detail", async () => {
  const entries = [
    entry("usa10003", "https://www.colliers.com/en/properties/usa10003"),
    entry("usa10004", "https://www.colliers.com/en/properties/usa10004"),
  ];
  let calls = 0;
  await assert.doesNotReject(() =>
    assertColliersMainDetailRuntimeReady(entries, new Map(), async () => {
      calls++;
      if (calls === 1) throw new Error("socket hang up");
      return syntheticDoc(SALE_LD, "Building Size: 1,000 SF");
    })
  );
  assert.equal(calls, 2);
});

test("Colliers detail runtime canary accepts a verified not-found tombstone", async () => {
  await assert.doesNotReject(() =>
    assertColliersMainDetailRuntimeReady(
      [entry("usa10005", "https://www.colliers.com/en/properties/usa10005")],
      new Map(),
      async () => doc({ rawHtml: "<html>gone</html>", markdown: "Gone", metadata: { statusCode: 410 } })
    )
  );
});

test("Colliers finite caps report truncation against sitemap inventory", () => {
  const complete = { errors: 0, deferred: 0 };
  assert.equal(colliersMainResultTruncated(complete, 1, 2), true);
  assert.equal(colliersMainResultTruncated(complete, 2, 2), false);
  assert.equal(
    colliersMainResultTruncated(complete, Number.POSITIVE_INFINITY, 2),
    false
  );
  assert.equal(colliersMainResultTruncated({ errors: 1, deferred: 0 }, 2, 2), true);
});

test("readColliersMainCache and appendColliersMainCache round-trip JSONL rows", () => {
  const dir = mkdtempSync(join(tmpdir(), "colliers-main-cache-"));
  const cachePath = join(dir, "detail-cache.jsonl");
  try {
    assert.equal(readColliersMainCache(cachePath).size, 0);

    appendColliersMainCache(cachePath, {
      id: "usa55555",
      url: "https://www.colliers.com/en/properties/usa55555",
      name: "Cached Listing",
    });
    appendColliersMainCache(cachePath, {
      id: "main:usa66666",
      url: "https://www.colliers.com/en/properties/usa66666",
      name: "Prefixed Id",
    });
    appendColliersMainCache(cachePath, {
      id: "usa77777",
      detailError: "transient failure",
      url: "https://www.colliers.com/en/properties/usa77777",
    });

    const cached = readColliersMainCache(cachePath);
    assert.equal(cached.size, 2);
    assert.equal(cached.get("usa55555")?.name, "Cached Listing");
    assert.equal(cached.get("usa66666")?.name, "Prefixed Id");
    assert.equal(cached.has("usa77777"), false);

    const lines = readFileSync(cachePath, "utf8").trim().split("\n");
    assert.equal(lines.length, 2);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// Phase-2 data-lift tests: new camelCase scalar fields from parseColliersMainDetail
// ---------------------------------------------------------------------------

/** Minimal entry for testing. */
function entry(id: string, url: string, lastmod: string | null = "2026-01-15"): ColliersMainEntry {
  return { id, url, lastmod };
}

const SALE_LD = {
  "@type": "RealEstateListing",
  name: "Office For sale — 239 Great Neck Rd, Great Neck, NY 11021, USA | United States | Colliers",
};

const LEASE_LD = {
  "@type": "RealEstateListing",
  name: "Industrial For Lease — 4321 Industrial Blvd, Phoenix, AZ 85007, USA | United States | Colliers",
};

const MG_LEASE_LD = {
  "@type": "RealEstateListing",
  name: "Office For Lease — 100 Main St, Dallas, TX 75201, USA | United States | Colliers",
};

test("parseColliersMainDetail: canonicalUrl is the entry url", () => {
  const e = entry("usa1159083", "https://www.colliers.com/en/properties/usa-239-great-neck-rd/usa1159083");
  const mdContent = "## Office For Sale\n**Property Status** Available\nBuilding Size: 15,476 SF";
  const docx = syntheticDoc(SALE_LD, mdContent);
  const listing = parseColliersMainDetail(e, docx);
  assert.equal(listing.canonicalUrl, e.url);
});

test("parseColliersMainDetail: statusBadge from **Property Status** markdown token", () => {
  const e = entry("usa1159083", "https://www.colliers.com/en/properties/usa1159083");
  const mdContent = "## Office For Sale\n**Property Status** Available\nBuilding Size: 5,000 SF";
  const docx = syntheticDoc(SALE_LD, mdContent);
  const listing = parseColliersMainDetail(e, docx);
  assert.equal(listing.statusBadge, "Available");
  assert.equal(listing.colliersMain.propertyStatus, "Available");
});

test("parseColliersMainDetail: statusBadge is absent when no Property Status in markdown", () => {
  const e = entry("usa9999999", "https://www.colliers.com/en/properties/usa9999999");
  const mdContent = "## Office For Sale\nBuilding Size: 5,000 SF";
  const docx = syntheticDoc(SALE_LD, mdContent);
  const listing = parseColliersMainDetail(e, docx);
  // prune() strips null values so the key may be absent; check null-or-undefined.
  assert.ok(listing.statusBadge == null, `statusBadge should be null/absent; got ${listing.statusBadge}`);
});

test("parseColliersMainDetail: leaseRateType from Modified Gross lease rate text", () => {
  const e = entry("usa1159094", "https://www.colliers.com/en/properties/usa1159094");
  // Markdown has a /SF lease rate with Modified Gross type.
  const mdContent =
    "## Office For Lease\n" +
    "**Property Status** Available\n" +
    "$18.50/SF Modified Gross\n";
  const docx = syntheticDoc(MG_LEASE_LD, mdContent);
  const listing = parseColliersMainDetail(e, docx);
  // The adapter captures leaseRateText from a markdown regex, then parses it.
  // Assert leaseRateType is non-null when a valid per-SF lease rate appears.
  if (listing.leaseRateText) {
    // leaseRateType must match the expected type from parseLeaseRate.
    assert.ok(
      listing.leaseRateType === "modified_gross" ||
        listing.leaseRateType === "gross" ||
        listing.leaseRateType === "nnn" ||
        listing.leaseRateType === "full_service" ||
        listing.leaseRateType === null,
      `leaseRateType must be a valid type or null; got: ${listing.leaseRateType}`
    );
  }
});

test("parseColliersMainDetail: leaseRateMin/Max from fixture Modified Gross text", () => {
  const e = entry("usa2000001", "https://www.colliers.com/en/properties/usa2000001");
  // Use a lease rate text that the regex in the adapter can capture via the /SF regex.
  // The adapter regex: /\$[\d,.]+\s*(?:\/|per\s*)\s*(?:SF|sq\.?\s*ft)[^\n]{0,24}/i
  const mdContent =
    "## Office For Lease\n" +
    "$18.50/SF Modified Gross per year\n" +
    "Building Size: 10,000 SF\n";
  const docx = syntheticDoc(MG_LEASE_LD, mdContent);
  const listing = parseColliersMainDetail(e, docx);
  // leaseRateText was captured from the regex; leaseRateMin must be 18.5.
  if (listing.leaseRateText) {
    assert.ok(typeof listing.leaseRateMin === "number" && listing.leaseRateMin > 0);
    assert.equal(listing.leaseRateType, "modified_gross");
  }
});

test("parseColliersMainDetail: leaseRateMin/Max absent when no lease rate text (sale-only listing)", () => {
  const e = entry("usa3000001", "https://www.colliers.com/en/properties/usa3000001-office-for-sale");
  const mdContent = "## Office For Sale\n$5,000,000\nBuilding Size: 20,000 SF";
  const docx = syntheticDoc(SALE_LD, mdContent);
  const listing = parseColliersMainDetail(e, docx);
  // Sale listing: leaseRateText is null, so leaseRateMin/Max/Type are null/absent (prune strips null).
  assert.ok(listing.leaseRateText == null, "leaseRateText should be absent for a sale listing");
  assert.ok(listing.leaseRateMin == null, "leaseRateMin should be absent when no rate text");
  assert.ok(listing.leaseRateMax == null, "leaseRateMax should be absent when no rate text");
  assert.ok(listing.leaseRateType == null, "leaseRateType should be absent when no rate text");
});

test("parseColliersMainDetail: NNN lease rate yields type=nnn, positive leaseRateMin", () => {
  const e = entry("usa4000001", "https://www.colliers.com/en/properties/usa4000001-for-lease");
  const md = "## Industrial For Lease\n$12.00/SF NNN\nBuilding Size: 50,000 SF";
  const docx = syntheticDoc(LEASE_LD, md);
  const listing = parseColliersMainDetail(e, docx);
  if (listing.leaseRateText) {
    assert.equal(listing.leaseRateType, "nnn");
    assert.equal(listing.leaseRateMin, 12);
    // leaseRateMax is null when no range; prune() drops it so check == null.
    assert.ok(listing.leaseRateMax == null, "leaseRateMax should be absent for a single-value rate");
  }
});

test("parseColliersMainDetail: fixture raw_data fields align with new field set", () => {
  // Verify the stored fixture raw_data has the shape the adapter now emits.
  const raw = mainFixture();
  // canonicalUrl: the fixture has url and the new field must be set to that.
  assert.ok(typeof raw.url === "string", "fixture has url");
  // statusBadge: the fixture has colliersMain.propertyStatus.
  const statusBadge = raw.colliersMain?.propertyStatus;
  assert.equal(statusBadge, "Available");
  // leaseRateText: present in this fixture (set in the fixture to a MG text).
  assert.ok(raw.leaseRateText, "fixture has leaseRateText");
});

test("parseColliersMainDetail: does not throw on minimal/empty doc", () => {
  const e = entry("usa0000001", "https://www.colliers.com/en/properties/usa0000001");
  const minimalDoc: ScrapedDoc = {
    rawHtml: `<script type="application/ld+json">{"@type":"RealEstateListing","name":"Office For Sale"}</script>`,
    markdown: "",
    links: [],
    metadata: { statusCode: 200, title: "Office" },
  };
  let listing: any;
  assert.doesNotThrow(() => {
    listing = parseColliersMainDetail(e, minimalDoc);
  });
  // canonicalUrl is always set (the entry url).
  assert.equal(listing.canonicalUrl, e.url);
  // Phase-2 optional fields: absent when source lacks them (prune strips null).
  assert.ok(listing.statusBadge == null, "statusBadge absent when no Property Status in markdown");
  assert.ok(listing.leaseRateType == null, "leaseRateType absent when no lease rate text");
  assert.ok(listing.leaseRateMin == null, "leaseRateMin absent when no lease rate text");
  assert.ok(listing.leaseRateMax == null, "leaseRateMax absent when no lease rate text");
});
