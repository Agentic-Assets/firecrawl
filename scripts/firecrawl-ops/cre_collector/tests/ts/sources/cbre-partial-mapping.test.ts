import assert from "node:assert/strict";
import { test } from "node:test";
import { brokers, resetBrokerStateForTests } from "../../../lib/broker.js";
import { mapCbreDocument } from "../../../sources/cbre.js";

const observed = { observedAt: "2026-01-01T00:00:00Z", generationId: "run-1",
  method: "cbre_listings_api", cacheDisposition: "live" as const };
const base = { "Common.PrimaryKey": "native-1", "Common.Aspects": [],
  "Common.ActualAddress": { "Common.Line1": "One Main", "Common.Region": "FL" } };

test("full mapper keeps established sale fallback and freshness", () => {
  const row = mapCbreDocument(base, "sale", observed);
  assert.equal(row.id, "native-1");
  assert.equal(row.transactionType, "Sale");
  assert.equal(row.url, "https://www.cbre.com/properties/properties-for-lease/commercial-space/details/native-1/one-main-fl");
  assert.equal(row.freshnessProvenance?.detailScope, "authoritative_inventory_feed");
  assert.equal(row.inventoryObservedAt, observed.observedAt);
});

test("partial mapper preserves unknown tenure and does not claim full inventory freshness", () => {
  const row = mapCbreDocument(base, "sale", observed, true);
  assert.equal(row.transactionType, null);
  assert.equal(row.salePriceText, null);
  assert.equal(row.inventoryObservedAt, undefined);
  assert.equal(row.detailObservedAt, undefined);
  assert.equal(row.freshnessProvenance, undefined);
  assert.equal(mapCbreDocument({ ...base, "Common.Aspects": ["isLetting"] }, "sale", observed, true).transactionType, "Lease");
  assert.equal(mapCbreDocument({ ...base, "Common.Aspects": ["isSale", "isLetting"] }, "lease", observed, true).transactionType, "Sale/Lease");
});

test("partial mapper rejects only rows lacking usable native identity", () => {
  for (const value of [null, {}, { "Common.PrimaryKey": "  " }, { "Common.PrimaryKey": 7 }]) {
    assert.throws(() => mapCbreDocument(value, "sale", observed, true), /no usable native identity/);
  }
});


test("partial mapper does not invent USD for an unlabelled price", () => {
  const charges = [{ "Common.ChargeKind": "SalePrice", "Common.Amount": 42 },
    { "Common.ChargeKind": "Rent", "Common.Amount": 5 }];
  const row = mapCbreDocument({ ...base, "Common.Charges": charges }, "sale", observed, true);
  assert.equal(row.salePriceUsd, null);
  assert.equal(row.leaseRateText, null);
  assert.equal(mapCbreDocument({ ...base, "Common.Charges": charges }, "sale", observed).salePriceUsd, 42);
});


test("partial mapping retains good optional values beside malformed elements", () => {
  const row = mapCbreDocument({ ...base,
    "Common.Charges": [null, { "Common.ChargeKind": "SalePrice", "Common.Amount": 42, "Common.ChargeCurrency": "USD" }],
    "Common.Agents": [null], "Common.Brochures": [null], "Common.Photos": [null, { "Common.ImageResources": null }],
    "Common.Strapline": [null, { "Common.Text": "Available" }],
  }, "sale", observed, true);
  assert.equal(row.salePriceUsd, 42);
  assert.equal(row.headline, "Available");
  assert.equal(row.transactionType, null);
});


test("partial mapping does not mutate shared broker state", () => {
  resetBrokerStateForTests();
  const doc = { ...base, "Common.Agents": [{ "Common.AgentName": "Example Broker" }] };
  const row = mapCbreDocument(doc, "sale", observed, true);
  assert.deepEqual(row.brokerIds, []);
  assert.deepEqual(brokers, []);
});


test("partial mapping omits invalid brochures and keeps an explicit valid URI", () => {
  const row = mapCbreDocument({ ...base, "Common.Brochures": [
    {}, { "Common.Uri": 123 }, { "Common.Uri": "" }, { "Common.Uri": "   " },
    { "Common.Uri": "javascript:alert(1)" }, { "Common.Uri": "//evil.test/doc" },
    { "Common.Uri": "/files/real.pdf", "Common.BrochureName": "Flyer" },
    { "Common.Uri": "HTTPS://example.test/real.pdf" },
  ] }, "sale", observed, true);
  assert.deepEqual(row.documents.map((item) => item.url), ["https://www.cbre.com/files/real.pdf", "https://example.test/real.pdf"]);
});


test("partial mapping does not invent photo URLs from malformed resources", () => {
  const row = mapCbreDocument({ ...base, "Common.Photos": [
    { "Common.ImageResources": [{ "Common.Resource.Uri": "javascript:alert(1)" }] },
    { "Common.ImageResources": [{ "Common.Resource.Uri": "/photos/one.jpg" }] },
    { "Common.ImageResources": [{ "Common.Resource.Uri": "HTTPS://example.test/photo.jpg" }] },
  ] }, "sale", observed, true);
  assert.deepEqual(row.photos, ["https://www.cbre.com/photos/one.jpg", "https://example.test/photo.jpg"]);
});
