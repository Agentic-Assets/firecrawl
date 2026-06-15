// Isolate argv before savills.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import {
  savillsSaleCardIsCommercial,
  mapSavillsLeaseRow,
  savillsTotalItems,
} from "../../../sources/savills.js";

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
