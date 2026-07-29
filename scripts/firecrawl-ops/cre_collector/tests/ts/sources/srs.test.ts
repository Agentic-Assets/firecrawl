import assert from "node:assert/strict";
import test from "node:test";
import {
  assertSrsInventoryPage,
  assertSrsInventoryReconciled,
  mapSrsListing,
  srsTenure,
} from "../../../sources/srs.js";

function srsRow(id: string): any {
  return {
    id: `fallback-${id}`,
    permalink: `/properties/sale/${id}`,
    apto_data: {
      SRS_Listings_ID__c: id,
      Name: `Listing ${id}`,
      Availability__c: "Sale",
    },
  };
}

test("srsTenure classifies sale and lease rows", () => {
  assert.deepEqual(srsTenure({ apto_data: { Availability__c: "Investment Sale" } }), {
    isSale: true,
    isLease: false,
  });
  assert.deepEqual(srsTenure({ apto_data: { Availability__c: "Lease" }, permalink: "/properties/lease/foo" }), {
    isSale: false,
    isLease: true,
  });
});

test("mapSrsListing maps Salesforce-backed fields", () => {
  const row = {
    id: "row-1",
    permalink: "/properties/sale/family-dollar",
    square_feet_data: "9,180 SF",
    location: { lat: 35.1, lon: -90.2 },
    apto_data: {
      SRS_Listings_ID__c: "SRS-42",
      Name: "Family Dollar",
      Availability__c: "Sale",
      Primary_Property_Type__c: "Retail",
      Property_Address__c: "123 Main St",
      Property_City__c: "Tulsa",
      Property_State__c: "OK",
      Property_Zip__c: "74103",
      Sale_Price__c: 1550000,
      Cap_Rate__c: 6.25,
      Total_Property_SF_GLA__c: 9180,
      related_brokers: [{ name: "Jane Broker", email: "jane@example.com" }],
    },
  };
  const listing = mapSrsListing(row, "sale");
  assert.equal(listing.id, "SRS-42");
  assert.equal(listing.name, "Family Dollar");
  assert.equal(listing.salePriceUsd, 1550000);
  assert.equal(listing.state, "OK");
  assert.equal(listing.url, "https://www.srsre.com/properties/sale/family-dollar");
  assert.equal(listing.brokerIds.length, 1);
});

test("strict SRS pages require coherent totals and exact page shape", () => {
  const first = assertSrsInventoryPage(
    { total: 13, properties: Array.from({ length: 12 }, (_, index) => srsRow(`S-${index}`)) },
    0,
    null,
    true
  );
  assert.deepEqual(first, { total: 13, pageSize: 12 });
  assert.deepEqual(
    assertSrsInventoryPage({ total: 13, properties: [srsRow("S-12")] }, 1, first, true),
    first
  );

  assert.throws(
    () => assertSrsInventoryPage({ properties: [] }, 0, null, true),
    /valid integer total/
  );
  assert.throws(
    () => assertSrsInventoryPage({ total: 1.5, properties: [] }, 0, null, true),
    /valid integer total/
  );
  assert.throws(
    () => assertSrsInventoryPage({ total: 1, properties: null }, 0, null, true),
    /properties array/
  );
  assert.throws(
    () => assertSrsInventoryPage({ total: 14, properties: [srsRow("S-12")] }, 1, first, true),
    /total changed/
  );
  assert.throws(
    () => assertSrsInventoryPage({ total: 13, properties: [] }, 1, first, true),
    /expected 1 rows/
  );
});

test("strict SRS reconciliation rejects ambiguous identities and aggregate gaps", () => {
  assert.throws(
    () => assertSrsInventoryReconciled([srsRow("S-1"), srsRow("S-1")], 2, true),
    /duplicate provider identity/
  );
  assert.throws(
    () => assertSrsInventoryReconciled([{ id: "fallback-only", apto_data: {} }], 1, true),
    /SRS_Listings_ID__c/
  );
  assert.throws(
    () => assertSrsInventoryReconciled([srsRow("S-1")], 2, true),
    /expected 2 unique rows/
  );
  assert.doesNotThrow(() =>
    assertSrsInventoryReconciled([srsRow("S-1"), srsRow("S-2")], 2, true)
  );
});

test("strict SRS mappings use only provider identity and emit authoritative inventory provenance", () => {
  assert.throws(
    () =>
      mapSrsListing(
        { id: "fallback-only", apto_data: { Name: "Ambiguous" } },
        "sale",
        { strict: true, inventoryObservedAt: "2026-07-29T12:00:00.000Z" }
      ),
    /SRS_Listings_ID__c/
  );

  const listing = mapSrsListing(srsRow("SRS-authoritative"), "sale", {
    strict: true,
    inventoryObservedAt: "2026-07-29T12:00:00.000Z",
    generationId: "srs-generation",
  });
  assert.equal(listing.id, "SRS-authoritative");
  assert.equal(listing.inventoryObservedAt, "2026-07-29T12:00:00.000Z");
  assert.equal(listing.freshnessProvenance.generationId, "srs-generation");
  assert.equal(listing.freshnessProvenance.detailScope, "authoritative_inventory_feed");
  assert.equal(listing.freshnessProvenance.cacheDisposition, "live");
  assert.equal(listing.preserveChildCollections, true);
});
