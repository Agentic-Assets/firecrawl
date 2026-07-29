import assert from "node:assert/strict";
import test from "node:test";
import {
  assertKidderInventoryPage,
  assertKidderInventoryReconciled,
  kidderTenure,
  mapKidderListing,
} from "../../../sources/kidder-mathews.js";

function kidderRow(id: string): any {
  return {
    listing_key: id,
    property_key: `property-${id}`,
    property_name: `Listing ${id}`,
    list_price: 1_000_000,
  };
}

test("kidderTenure classifies sale, lease, and default sale rows", () => {
  assert.deepEqual(kidderTenure({ list_price: 1000000 }), { isSale: true, isLease: false });
  assert.deepEqual(kidderTenure({ asking_rent_max: 32 }), { isSale: false, isLease: true });
  assert.deepEqual(kidderTenure({}), { isSale: true, isLease: false });
});

test("mapKidderListing maps API fields", () => {
  const listing = mapKidderListing(
    {
      listing_key: 12345,
      property_name: "West Coast Industrial",
      use_type: "Industrial",
      property_address: "800 Port Way",
      city: "Seattle",
      state_code: "WA",
      zip_postal_code: "98101",
      list_price: 7100000,
      sf_avail: 45000,
      brokers: ["Jane Broker"],
      listing_photo: "https://images.example.com/hero.jpg",
    },
    "sale"
  );
  assert.equal(listing.id, "12345");
  assert.equal(listing.salePriceUsd, 7100000);
  assert.equal(listing.state, "WA");
  assert.equal(listing.url, "https://www.kidder.com/listings/12345");
  assert.equal(listing.brokerIds.length, 1);
});

test("strict Kidder pages require coherent totals and exact page shape", () => {
  const first = assertKidderInventoryPage(
    {
      totalResultCount: 51,
      results: Array.from({ length: 50 }, (_, index) => kidderRow(`K-${index}`)),
    },
    0,
    null,
    true
  );
  assert.deepEqual(first, { total: 51, pageSize: 50 });
  assert.deepEqual(
    assertKidderInventoryPage(
      { totalResultCount: 51, results: [kidderRow("K-50")] },
      1,
      first,
      true
    ),
    first
  );

  assert.throws(
    () => assertKidderInventoryPage({ results: [] }, 0, null, true),
    /valid integer totalResultCount/
  );
  assert.throws(
    () => assertKidderInventoryPage({ totalResultCount: 2, results: null }, 0, null, true),
    /results array/
  );
  assert.throws(
    () =>
      assertKidderInventoryPage(
        { totalResultCount: 52, results: [kidderRow("K-50")] },
        1,
        first,
        true
      ),
    /total changed/
  );
  assert.throws(
    () => assertKidderInventoryPage({ totalResultCount: 51, results: [] }, 1, first, true),
    /expected 1 rows/
  );
});

test("strict Kidder reconciliation requires unique listing_key identities", () => {
  assert.throws(
    () => assertKidderInventoryReconciled([kidderRow("K-1"), kidderRow("K-1")], 2, true),
    /duplicate provider identity/
  );
  assert.throws(
    () => assertKidderInventoryReconciled([{ property_key: "fallback-only" }], 1, true),
    /listing_key/
  );
  assert.throws(
    () => assertKidderInventoryReconciled([kidderRow("K-1")], 2, true),
    /expected 2 unique rows/
  );
  assert.doesNotThrow(() =>
    assertKidderInventoryReconciled([kidderRow("K-1"), kidderRow("K-2")], 2, true)
  );
});

test("strict Kidder mapping rejects property_key fallback and retains all feed photos", () => {
  assert.throws(
    () =>
      mapKidderListing(
        { property_key: "fallback-only", property_name: "Ambiguous" },
        "sale",
        { strict: true, inventoryObservedAt: "2026-07-29T12:00:00.000Z" }
      ),
    /listing_key/
  );

  const listing = mapKidderListing(
    {
      ...kidderRow("K-current"),
      listing_photo: [
        "https://images.example.com/1.jpg",
        "https://images.example.com/2.jpg",
      ],
      property_photo: "https://images.example.com/3.jpg",
      photos: ["https://images.example.com/4.jpg"],
    },
    "sale",
    {
      strict: true,
      inventoryObservedAt: "2026-07-29T12:00:00.000Z",
      generationId: "kidder-generation",
    }
  );
  assert.equal(listing.id, "K-current");
  assert.deepEqual(listing.photos, [
    "https://images.example.com/1.jpg",
    "https://images.example.com/2.jpg",
    "https://images.example.com/3.jpg",
    "https://images.example.com/4.jpg",
  ]);
  assert.equal(listing.inventoryObservedAt, "2026-07-29T12:00:00.000Z");
  assert.equal(listing.freshnessProvenance.generationId, "kidder-generation");
  assert.equal(listing.freshnessProvenance.detailScope, "authoritative_inventory_feed");
  assert.equal(listing.preserveChildCollections, true);
});
