// Isolate argv before savills.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  savillsSaleCardIsCommercial,
  mapSavillsRow,
  mapSavillsLeaseRow,
  savillsPageInfo,
  savillsTotalItems,
} from "../../../sources/savills.js";

// ---------------------------------------------------------------------------
// Fixture helpers
// ---------------------------------------------------------------------------

const __filename = fileURLToPath(import.meta.url);
const __dir = dirname(__filename);
const FIXTURE_PATH = join(__dir, "../../fixtures/raw_data/savills.json");

/** Load the savills.json fixture (array of {external_id, rawSavillsProperty} blobs). */
function loadFixture(): Array<{ _comment?: string; external_id: string; rawSavillsProperty: any }> {
  return JSON.parse(readFileSync(FIXTURE_PATH, "utf8"));
}

const SOURCE_URL = "https://search.savills.com/com/en/list/commercial/property-to-let/united-states-of-america";

// --- savillsSaleCardIsCommercial ---

test("savillsSaleCardIsCommercial returns true for commercial keyword in propertyType", () => {
  assert.equal(savillsSaleCardIsCommercial({ propertyType: "Office Space", href: null, cardText: null }), true);
  assert.equal(savillsSaleCardIsCommercial({ propertyType: "Retail Unit", href: null, cardText: null }), true);
  assert.equal(savillsSaleCardIsCommercial({ propertyType: "Industrial Warehouse", href: null, cardText: null }), true);
  assert.equal(savillsSaleCardIsCommercial({ propertyType: "Hotel", href: null, cardText: null }), true);
  assert.equal(savillsSaleCardIsCommercial({ propertyType: "Development Site", href: null, cardText: null }), true);
  assert.equal(savillsSaleCardIsCommercial({ propertyType: "Mixed Use", href: null, cardText: null }), true);
  assert.equal(savillsSaleCardIsCommercial({ propertyType: "Land", href: null, cardText: null }), true);
  assert.equal(savillsSaleCardIsCommercial({ propertyType: "Leisure Property", href: null, cardText: null }), true);
  assert.equal(savillsSaleCardIsCommercial({ propertyType: "Commercial", href: null, cardText: null }), true);
  assert.equal(savillsSaleCardIsCommercial({ propertyType: "Hospitality", href: null, cardText: null }), true);
});

test("savillsSaleCardIsCommercial returns true for /commercial/ segment in href", () => {
  assert.equal(savillsSaleCardIsCommercial({
    propertyType: null,
    href: "https://search.savills.com/com/en/list/commercial/property-for-sale/us",
    cardText: null,
  }), true);
  assert.equal(savillsSaleCardIsCommercial({
    propertyType: "unknown",
    href: "/commercial/property-detail/abc123",
    cardText: null,
  }), true);
});

test("savillsSaleCardIsCommercial returns true for commercial keyword in cardText", () => {
  assert.equal(savillsSaleCardIsCommercial({
    propertyType: null,
    href: null,
    cardText: "12,500 sq ft warehouse for sale in Chicago",
  }), true);
  assert.equal(savillsSaleCardIsCommercial({
    propertyType: null,
    href: null,
    cardText: "Prime office location downtown",
  }), true);
});

test("savillsSaleCardIsCommercial returns false for residential keywords", () => {
  assert.equal(savillsSaleCardIsCommercial({ propertyType: "House", href: null, cardText: null }), false);
  assert.equal(savillsSaleCardIsCommercial({ propertyType: "Apartment", href: null, cardText: null }), false);
  assert.equal(savillsSaleCardIsCommercial({ propertyType: "Flat", href: null, cardText: null }), false);
  assert.equal(savillsSaleCardIsCommercial({ propertyType: null, href: null, cardText: "4 bedroom villa" }), false);
  assert.equal(savillsSaleCardIsCommercial({ propertyType: "Residential", href: null, cardText: null }), false);
  assert.equal(savillsSaleCardIsCommercial({ propertyType: "Cottage", href: null, cardText: null }), false);
});

test("savillsSaleCardIsCommercial residential marker overrides commercial keyword (force-false)", () => {
  // A card with both signals: residential wins to stay safe.
  assert.equal(savillsSaleCardIsCommercial({
    propertyType: "Residential Development",
    href: null,
    cardText: "office conversion potential",
  }), false);
});

test("savillsSaleCardIsCommercial returns false when no commercial signal (default-deny)", () => {
  // Completely neutral card with no commercial or residential keywords.
  assert.equal(savillsSaleCardIsCommercial({ propertyType: null, href: null, cardText: null }), false);
  assert.equal(savillsSaleCardIsCommercial({ propertyType: "", href: "", cardText: "" }), false);
  assert.equal(savillsSaleCardIsCommercial({
    propertyType: "Property",
    href: "https://search.savills.com/property-detail/us12345",
    cardText: "Beautiful property in Dallas",
  }), false);
});

// --- mapSavillsLeaseRow ---

test("mapSavillsLeaseRow maps a synthetic __NEXT_DATA__ row to expected lease listing fields", () => {
  const row = {
    IsCommercial: true,
    ExternalPropertyID: "US-LEASE-001",
    ExternalPropertyIDFormatted: "us-lease-001",
    AddressLine1: "100 Commerce Drive",
    AddressLine2: "Chicago, IL 60601",
    PropertyTypes: [{ Caption: "Office" }],
    GuidePriceText: "$25/SF/yr",
    DisplayPriceText: null,
    SizeFormatted: "(12,500 sq ft)",
    FooterSizeFormatted: null,
    LongDescription: [{ Head: "Overview", Body: "Prime loop office." }],
    Latitude: 41.8827,
    Longitude: -87.6233,
    PrimaryAgent: {
      AgentName: "Jane Smith",
      EmailAddress: "jane@savills.com",
      AgentPhoneNumber: "312-555-0100",
    },
    SecondaryAgent: null,
    ImagesGallery: [],
    PropertyCardImagesGallery: [],
    BrochureGallery: [],
    FloorplanPDFUrl: null,
  };

  const listing = mapSavillsLeaseRow(row, "https://search.savills.com/source-url");

  assert.ok(listing !== null);
  assert.equal(listing.transactionType, "Lease");
  assert.equal(listing.id, "US-LEASE-001");
  assert.equal(listing.name, "100 Commerce Drive");
  assert.equal(listing.city, "Chicago");
  assert.equal(listing.state, "IL");
  assert.equal(listing.postalCode, "60601");
  assert.equal(listing.country, "US");
  assert.equal(listing.leaseRateText, "$25/SF/yr");
  assert.equal(listing.buildingSizeSqft, 12500);
  assert.equal(listing.url, "https://search.savills.com/com/en/property-detail/us-lease-001");
  assert.equal(listing.latitude, 41.8827);
  assert.equal(listing.longitude, -87.6233);
  assert.equal(listing.contactsDetailed.length, 1);
  assert.equal(listing.contactsDetailed[0].name, "Jane Smith");
  // clean() collapses all whitespace (including newlines) to single spaces.
  assert.equal(listing.description, "Overview Prime loop office.");
});

test("mapSavillsLeaseRow returns null for a non-US row", () => {
  const row = {
    IsCommercial: true,
    ExternalPropertyID: "CA-001",
    AddressLine1: "200 Bay Street",
    AddressLine2: "Toronto, Canada",  // Not a US location
    PropertyTypes: [{ Caption: "Office" }],
  };

  const result = mapSavillsLeaseRow(row, "https://search.savills.com/source-url");
  assert.equal(result, null);
});

test("mapSavillsLeaseRow falls back to sourceUrl when no detailId", () => {
  const row = {
    IsCommercial: true,
    ExternalPropertyID: null,
    ExternalPropertyIDFormatted: null,
    AddressLine1: "500 Market St",
    AddressLine2: "San Francisco, CA 94105",
    PropertyTypes: [],
    GuidePriceText: null,
    DisplayPriceText: null,
    SizeFormatted: null,
    FooterSizeFormatted: null,
    LongDescription: [],
    Latitude: null,
    Longitude: null,
    PrimaryAgent: null,
    SecondaryAgent: null,
    ImagesGallery: [],
    PropertyCardImagesGallery: [],
    BrochureGallery: [],
    FloorplanPDFUrl: null,
  };

  const sourceUrl = "https://search.savills.com/source-url";
  const listing = mapSavillsLeaseRow(row, sourceUrl);

  assert.ok(listing !== null);
  assert.equal(listing.url, sourceUrl);
  // When ExternalPropertyID is null and ExternalPropertyIDFormatted is null,
  // detailId resolves to undefined and id is also undefined.
  assert.equal(listing.id, undefined);
});

// --- savillsTotalItems (light assertion; pure function) ---

test("savillsTotalItems extracts totalItems from __NEXT_DATA__", () => {
  const html = `
    <script id="__NEXT_DATA__" type="application/json">
      {"props":{"initialReduxState":{"listPage":{"totalItems":42},"properties":{}}}}
    </script>
  `;
  const result = savillsTotalItems(html, 0);
  assert.equal(result, 42);
});

test("savillsTotalItems falls back to heading match when __NEXT_DATA__ totalItems is absent", () => {
  const html = `<h1>35 Properties for let in US</h1>`;
  const result = savillsTotalItems(html, 0);
  assert.equal(result, 35);
});

test("savillsTotalItems returns fallback when no signal is found", () => {
  const html = `<p>No listings</p>`;
  const result = savillsTotalItems(html, 5);
  assert.equal(result, 5);
});

test("savillsPageInfo reads provider pagination rather than top-level page count", () => {
  const html = `
    <script id="__NEXT_DATA__" type="application/json">
      {"props":{"initialReduxState":{"listPage":{"totalItems":2,"currentPage":1,"pageMap":{"1":{"paging":{"current":1,"total":2,"totalItems":36},"metaData":{"NextUrl":"/com/en/list?cursor=next"}}}},"properties":{}}}}
    </script>
  `;
  assert.deepEqual(savillsPageInfo(html, 0), {
    currentPage: 1,
    totalPages: 2,
    totalItems: 36,
    nextUrl: "/com/en/list?cursor=next",
  });
});

test("savillsPageInfo normalizes a reconciled provider zero-page singleton to one page", () => {
  const html = `
    <script id="__NEXT_DATA__" type="application/json">
      {"props":{"initialReduxState":{"listPage":{"totalItems":0,"currentPage":1,"pageMap":{"1":{"paging":{"current":1,"total":0,"totalItems":2},"metaData":{"NextUrl":""}}}},"properties":{}}}}
    </script>
  `;
  assert.deepEqual(savillsPageInfo(html, 2, true), {
    currentPage: 1,
    totalPages: 1,
    totalItems: 2,
    nextUrl: null,
  });
});

test("savillsPageInfo rejects a zero-page shape when the page is incomplete", () => {
  const html = `
    <script id="__NEXT_DATA__" type="application/json">
      {"props":{"initialReduxState":{"listPage":{"currentPage":1,"pageMap":{"1":{"paging":{"current":1,"total":0,"totalItems":2},"metaData":{"NextUrl":""}}}},"properties":{}}}}
    </script>
  `;
  assert.deepEqual(savillsPageInfo(html, 1, true), {
    currentPage: 1,
    totalPages: null,
    totalItems: 2,
    nextUrl: null,
  });
});

test("mapSavillsRow maps a U.S. sale row with display price", () => {
  const row = {
    ExternalPropertyID: "sale-us-1",
    ExternalPropertyIDFormatted: "sale-us-1",
    AddressLine1: "100 Main Street",
    AddressLine2: "Dallas, TX 75201",
    PropertyTypes: [{ Caption: "Office" }],
    GuidePriceText: "Asking price",
    DisplayPriceText: "US$ 1,250,000",
    SizeFormatted: "25,000 sq ft",
  };
  const listing = mapSavillsRow(row, "sale", "https://search.savills.com/source");
  assert.ok(listing !== null);
  assert.equal(listing.transactionType, "Sale");
  assert.equal(listing.salePriceText, "US$ 1,250,000");
  assert.equal(listing.salePriceUsd, 1250000);
  assert.equal(listing.leaseRateText, null);
});

// ---------------------------------------------------------------------------
// Phase-2 scalar lift: fixture-driven tests for canonicalUrl / highlights / availableSf
// ---------------------------------------------------------------------------

test("mapSavillsLeaseRow emits canonicalUrl as the absolute property-detail URL (fixture row 1)", () => {
  const fixtures = loadFixture();
  const row = fixtures.find((f) => f.external_id === "5025923B-1E46-42E5-81C4-6F4316A8B02D")!.rawSavillsProperty;
  const listing = mapSavillsLeaseRow(row, SOURCE_URL);
  assert.ok(listing !== null);
  // ExternalPropertyIDFormatted is present, so url is built from it and used as canonicalUrl.
  assert.equal(
    listing.canonicalUrl,
    "https://search.savills.com/com/en/property-detail/5025923b-1e46-42e5-81c4-6f4316a8b02d"
  );
});

test("mapSavillsLeaseRow emits highlights as newline-joined WebFeatureList strings (fixture row 1)", () => {
  const fixtures = loadFixture();
  const row = fixtures.find((f) => f.external_id === "5025923B-1E46-42E5-81C4-6F4316A8B02D")!.rawSavillsProperty;
  const listing = mapSavillsLeaseRow(row, SOURCE_URL);
  assert.ok(listing !== null);
  assert.ok(typeof listing.highlights === "string");
  // All four WebFeatureList items are joined.
  assert.ok(listing.highlights.includes("Bucktown / Wicker Park"));
  assert.ok(listing.highlights.includes("Damen Blue Line station"));
  assert.ok(listing.highlights.includes("12,700 vehicles"));
  assert.equal(listing.highlights.split("\n").length, 4);
});

test("mapSavillsLeaseRow emits availableSf from AvailableSize.SqFt when non-zero (fixture row 1)", () => {
  const fixtures = loadFixture();
  const row = fixtures.find((f) => f.external_id === "5025923B-1E46-42E5-81C4-6F4316A8B02D")!.rawSavillsProperty;
  const listing = mapSavillsLeaseRow(row, SOURCE_URL);
  assert.ok(listing !== null);
  assert.equal(listing.availableSf, 5139);
});

test("mapSavillsLeaseRow suppresses availableSf when AvailableSize.SqFt is zero (fixture row 2)", () => {
  const fixtures = loadFixture();
  // Row 2: AvailableSize.SqFt = 0 (not stated); must NOT emit a zero value.
  const row = fixtures.find((f) => f.external_id === "E4014DBE-336F-4AC8-BA9B-6ED1B48F73FE")!.rawSavillsProperty;
  const listing = mapSavillsLeaseRow(row, SOURCE_URL);
  assert.ok(listing !== null);
  assert.equal(listing.availableSf, null, "zero SqFt must be suppressed to null");
});

test("mapSavillsLeaseRow still emits highlights for row 2 (WebFeatureList present)", () => {
  const fixtures = loadFixture();
  const row = fixtures.find((f) => f.external_id === "E4014DBE-336F-4AC8-BA9B-6ED1B48F73FE")!.rawSavillsProperty;
  const listing = mapSavillsLeaseRow(row, SOURCE_URL);
  assert.ok(listing !== null);
  assert.ok(typeof listing.highlights === "string");
  assert.ok(listing.highlights.includes("B3-2 zoning"));
  assert.equal(listing.highlights.split("\n").length, 4);
});

test("mapSavillsLeaseRow emits null highlights when WebFeatureList is absent", () => {
  // Synthetic row with no WebFeatureList.
  const row = {
    ExternalPropertyID: "SYNTH-001",
    ExternalPropertyIDFormatted: "synth-001",
    AddressLine1: "100 Test St",
    AddressLine2: "Chicago, IL 60601",
    PropertyTypes: [{ Caption: "Office" }],
    Latitude: 41.88,
    Longitude: -87.63,
    AvailableSize: { SqFt: 0 },
    // No WebFeatureList key at all.
    LongDescription: [],
    ImagesGallery: [],
    PropertyCardImagesGallery: [],
    BrochureGallery: [],
    FloorplanPDFUrl: null,
  };
  const listing = mapSavillsLeaseRow(row, SOURCE_URL);
  assert.ok(listing !== null);
  assert.equal(listing.highlights, null);
});

test("mapSavillsLeaseRow emits null availableSf when AvailableSize is absent", () => {
  // Synthetic row with no AvailableSize key.
  const row = {
    ExternalPropertyID: "SYNTH-002",
    ExternalPropertyIDFormatted: "synth-002",
    AddressLine1: "200 Test St",
    AddressLine2: "Chicago, IL 60601",
    PropertyTypes: [{ Caption: "Office" }],
    Latitude: 41.88,
    Longitude: -87.63,
    WebFeatureList: [],
    LongDescription: [],
    ImagesGallery: [],
    PropertyCardImagesGallery: [],
    BrochureGallery: [],
    FloorplanPDFUrl: null,
  };
  const listing = mapSavillsLeaseRow(row, SOURCE_URL);
  assert.ok(listing !== null);
  assert.equal(listing.availableSf, null);
});

test("mapSavillsLeaseRow does not throw on empty/null WebFeatureList entries", () => {
  const row = {
    ExternalPropertyID: "SYNTH-003",
    ExternalPropertyIDFormatted: "synth-003",
    AddressLine1: "300 Test St",
    AddressLine2: "Chicago, IL 60601",
    PropertyTypes: [],
    Latitude: 41.88,
    Longitude: -87.63,
    AvailableSize: { SqFt: 2000 },
    WebFeatureList: [null, "", "  ", "Valid feature"],
    LongDescription: [],
    ImagesGallery: [],
    PropertyCardImagesGallery: [],
    BrochureGallery: [],
    FloorplanPDFUrl: null,
  };
  let listing: any;
  assert.doesNotThrow(() => { listing = mapSavillsLeaseRow(row, SOURCE_URL); });
  assert.ok(listing !== null);
  // Only the valid string survives the clean() filter.
  assert.equal(listing.highlights, "Valid feature");
  // availableSf is emitted for the 2000 value.
  assert.equal(listing.availableSf, 2000);
});
