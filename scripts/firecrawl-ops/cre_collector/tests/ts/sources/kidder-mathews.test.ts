import assert from "node:assert/strict";
import test from "node:test";
import { kidderTenure, mapKidderListing } from "../../../sources/kidder-mathews.js";

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
