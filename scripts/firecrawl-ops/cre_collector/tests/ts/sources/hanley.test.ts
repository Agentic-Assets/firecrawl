import assert from "node:assert/strict";
import test from "node:test";
import { extractRethinkProperties, hanleyIsLease, mapHanleyListing } from "../../../sources/hanley.js";

test("extractRethinkProperties reads embedded JSON array", () => {
  const html = `<script>var rethink_properties = [{"id":"1","name":"A"},{"id":"2","name":"B"}];</script>`;
  assert.deepEqual(extractRethinkProperties(html).map((row) => row.id), ["1", "2"]);
});

test("hanleyIsLease detects landlord and tenant records", () => {
  assert.equal(hanleyIsLease({ dealRecordType: "Seller_Rep" }), false);
  assert.equal(hanleyIsLease({ dealRecordType: "Landlord_Rep" }), true);
  assert.equal(hanleyIsLease({ dealPipelineTypes: ["Tenant Rep"] }), true);
});

test("mapHanleyListing maps sale fields and units", () => {
  const listing = mapHanleyListing(
    {
      id: "H-1",
      name: "Net Lease Retail",
      dealRecordType: "Seller_Rep",
      address: "10 Main St",
      city: "Irvine",
      state: "CA",
      zipCode: "92614",
      salesPrice: 2500000,
      capRate: 5.75,
      propertySquareFootage: 12000,
      numberOfUnits: 3,
    },
    "sale"
  );
  assert.equal(listing.id, "H-1");
  assert.equal(listing.salePriceUsd, 2500000);
  assert.equal(listing.units, 3);
  assert.equal(listing.url, "https://hanleyinvestmentgroup.com/listings/?id=H-1");
});
