import assert from "node:assert/strict";
import test from "node:test";
import { mapSrsListing, srsTenure } from "../../../sources/srs.js";

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
