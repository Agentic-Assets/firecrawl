import assert from "node:assert/strict";
import test from "node:test";
import {
  extractRethinkProperties,
  hanleyFallbackOptions,
  hanleyIsLease,
  mapHanleyListing,
  parseHanleyInventory,
} from "../../../sources/hanley.js";

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

test("strict Hanley parsing proves one structurally valid embedded public inventory", () => {
  const html =
    `<script>var rethink_properties = [` +
    `{"id":"1","name":"Public","visibility":"Public"},` +
    `{"id":"2","name":"Private","visibility":"Private"}` +
    `];</script>`;
  const parsed = parseHanleyInventory(html, true);
  assert.deepEqual(parsed.rows.map((row) => row.id), ["1", "2"]);
  assert.deepEqual(parsed.publicRows.map((row) => row.id), ["1"]);

  assert.throws(
    () => parseHanleyInventory("<html>verify you are human</html>", true),
    /challenge/
  );
  assert.throws(
    () => parseHanleyInventory(`<script>var rethink_properties = [{"id":"1"}];</script>`, true),
    /visibility/
  );
  assert.throws(
    () =>
      parseHanleyInventory(
        `<script>var rethink_properties = [` +
          `{"id":"1","visibility":"Public"},{"id":"1","visibility":"Public"}];</script>`,
        true
      ),
    /duplicate provider identity/
  );
  assert.throws(
    () =>
      parseHanleyInventory(
        `<script>var rethink_properties = [];</script>` +
          `<script>var rethink_properties = [];</script>`,
        true
      ),
    /exactly one/
  );
  assert.throws(
    () => parseHanleyInventory(`<script>var rethink_properties = [{"id":`, true),
    /complete JSON array/
  );
});

test("strict Hanley Firecrawl fallback explicitly bypasses cached responses", () => {
  assert.deepEqual(hanleyFallbackOptions(true), {
    proxy: "stealth",
    waitFor: 3000,
    maxAge: 0,
  });
  assert.equal(hanleyFallbackOptions(false).maxAge, undefined);
});

test("strict Hanley mappings require provider identity and preserve source-native children", () => {
  assert.throws(
    () =>
      mapHanleyListing(
        { name: "Missing identity", visibility: "Public" },
        "sale",
        { strict: true, inventoryObservedAt: "2026-07-29T12:00:00.000Z" }
      ),
    /provider id/
  );
  const listing = mapHanleyListing(
    { id: "H-2", name: "Current", visibility: "Public" },
    "sale",
    {
      strict: true,
      inventoryObservedAt: "2026-07-29T12:00:00.000Z",
      generationId: "hanley-generation",
    }
  );
  assert.equal(listing.id, "H-2");
  assert.equal(listing.inventoryObservedAt, "2026-07-29T12:00:00.000Z");
  assert.equal(listing.freshnessProvenance.generationId, "hanley-generation");
  assert.equal(listing.freshnessProvenance.detailScope, "authoritative_inventory_feed");
  assert.equal(listing.preserveChildCollections, true);
});
