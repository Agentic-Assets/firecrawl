import assert from "node:assert/strict";
import test from "node:test";
import { buildoutFieldObservations } from "../../../sources/buildout-field-observations.js";
import { buildoutCache, buildoutQueryFingerprint, srcBuildout } from "../../../sources/buildout.js";
import { prune } from "../../../lib/util.js";

test("captures explicit source count without canonical promotion", () => {
  const result = buildoutFieldObservations([["# of Spaces", "0 Spaces"], ["Cap Rate", "0.75%"]])!;
  assert.deepEqual(result.availableSpaceCount, { state: "observed", literals: [{sourceOrdinal: 0, literal: "0 Spaces"}], omittedCount: 0, value: 0 });
  assert.deepEqual(result.capRate, { state: "omitted_visibility_unverified", omittedCount: 1 });
  assert.equal(result.promotion, "not_authorized");
  assert.equal(prune(result).availableSpaceCount.value, 0);
  assert.equal(JSON.stringify(result).includes("0.75%"), false);
});

test("conflicting and invalid literals are retained without producing a count", () => {
  const result = buildoutFieldObservations([["# of Spaces", "3 Spaces"], ["# of Spaces", "unknown"]])!;
  assert.deepEqual(result.availableSpaceCount, { state: "invalid", literals: [{sourceOrdinal: 0, literal: "3 Spaces"}, {sourceOrdinal: 1, literal: "unknown"}], omittedCount: 0 });
  const conflict = buildoutFieldObservations([["# of Spaces", "2 Spaces"], ["# of Spaces", "3 Spaces"]])!;
  assert.equal(conflict.availableSpaceCount.state, "ambiguous");
  assert.equal(conflict.availableSpaceCount.literals?.length, 2);
});

test("unsafe values and malformed pairs are accounted for without a typed candidate", () => {
  const result = buildoutFieldObservations([["# of Spaces", "3 Spaces"], ["# of Spaces", "Bearer secret"], null])!;
  assert.equal(result.availableSpaceCount.state, "invalid");
  assert.equal(result.availableSpaceCount.omittedCount, 1);
  assert.equal(JSON.stringify(result).includes("Bearer"), false);
  assert.equal(buildoutFieldObservations([["Other", "value"]]), undefined);
});

test("large irrelevant nested arrays cannot cause traversal failure", () => {
  const huge = new Array(150_000).fill(0);
  assert.equal(buildoutFieldObservations([["# of Spaces", "1 Space"], ["Other", huge]])?.availableSpaceCount.value, 1);
});

test("collector handles malformed pairs and never persists withheld cap rate", async () => {
  const key = "fixture-field-observations";
  const memoryKey = `${key}\u0000${buildoutQueryFingerprint({})}`;
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error("unexpected network request"); };
  buildoutCache.set(memoryKey, { total: 1, items: [{ id: 1, sale: true, priceWithheld: true, show_link: "https://example.test/?propertyId=1", index_attributes: [null, ["Price", "$1,000,000"], ["# of Spaces", "2 Spaces"], ["# of Spaces", "unknown"], ["Cap Rate", "6.25%"]] }] });
  try {
    const result = await srcBuildout("Fixture", key, "https://example.test", "sale", 10, false);
    const listing = prune(result.listings[0]);
    assert.equal(listing.buildoutFieldObservations.availableSpaceCount.state, "invalid");
    assert.equal(listing.buildoutFieldObservations.availableSpaceCount.literals.length, 2);
    assert.equal(listing.buildoutFieldObservations.capRate.state, "omitted_visibility_unverified");
    // Synthetic flag semantics are unproven; the legacy sale-price path is unchanged.
    assert.equal(listing.salePriceUsd, 1_000_000);
    assert.equal(JSON.stringify(listing).includes("6.25%"), false);
    assert.equal(listing.capRatePct, undefined);
    assert.equal(listing.available_space_count, undefined);
  } finally { buildoutCache.delete(memoryKey); globalThis.fetch = oldFetch; }
});

test("malformed target-only pair is retained as an explicit omission", () => {
  const result = buildoutFieldObservations([["# of Spaces"]])!;
  assert.equal(result.availableSpaceCount.state, "invalid");
  assert.equal(result.availableSpaceCount.omittedCount, 1);
  assert.equal(result.malformedAttributeCount, 1);
});


test("collector suppresses unsafe space text from both emitted copies", async () => {
  const key = "fixture-unsafe-space";
  const memoryKey = `${key}\u0000${buildoutQueryFingerprint({})}`;
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error("unexpected network request"); };
  buildoutCache.set(memoryKey, { total: 2, items: [
    { id: 1, sale: true, show_link: "https://example.test/?propertyId=1", index_attributes: [["# of Spaces", "Bearer secret"]] },
    { id: 2, sale: true, show_link: "https://example.test/?propertyId=2", index_attributes: [["# of Spaces", "3 Spaces"]] },
  ] });
  try {
    const result = await srcBuildout("Fixture", key, "https://example.test", "sale", 10, false);
    assert.equal(JSON.stringify(result.listings[0]).includes("Bearer secret"), false);
    assert.equal(result.listings[0].buildoutFieldObservations.availableSpaceCount.omittedCount, 1);
    assert.equal(result.listings[0].extraFacts, undefined);
    assert.equal(result.listings[1].extraFacts.number_of_spaces, "3 Spaces");
    assert.equal(result.listings[1].buildoutFieldObservations.availableSpaceCount.value, 3);
  } finally { buildoutCache.delete(memoryKey); globalThis.fetch = oldFetch; }
});


test("collector keeps malformed row without promoting malformed price pair", async () => {
  const key = "fixture-malformed-price";
  const memoryKey = `${key}\u0000${buildoutQueryFingerprint({})}`;
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error("unexpected network request"); };
  buildoutCache.set(memoryKey, { total: 1, items: [{ id: 3, sale: true, show_link: "https://example.test/?propertyId=3", index_attributes: [null, ["Price", "$1,000,000", "unexpected"], ["# of Spaces", "3 Spaces"]] }] });
  try {
    const result = await srcBuildout("Fixture", key, "https://example.test", "sale", 10, false);
    assert.equal(result.listings.length, 1);
    const listing = result.listings[0];
    assert.equal(listing.salePriceUsd, null);
    assert.equal(listing.salePricePerSf, null);
    assert.equal(listing.salePriceText, null);
    assert.equal(listing.buildoutFieldObservations.malformedAttributeCount, 2);
    assert.equal(listing.buildoutFieldObservations.availableSpaceCount.state, "invalid");
    assert.equal(listing.buildoutFieldObservations.availableSpaceCount.literals[0].literal, "3 Spaces");
  } finally { buildoutCache.delete(memoryKey); globalThis.fetch = oldFetch; }
});


test("over-limit attributes report omissions without traversing contents", () => {
  const attrs = new Array(1001);
  Object.defineProperty(attrs, 0, { get() { throw new Error("must not inspect"); } });
  const result = buildoutFieldObservations(attrs)!;
  assert.equal(result.omittedAttributeCount, 1001);
  assert.equal(result.availableSpaceCount.state, "not_evaluated_attribute_limit");
  assert.equal(result.capRate?.state, "not_evaluated_attribute_limit");
});

test("collector refuses duplicate canonical labels and keeps record with conflict count", async () => {
  const key = "fixture-duplicate-canonical";
  const memoryKey = `${key}\u0000${buildoutQueryFingerprint({})}`;
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error("unexpected network request"); };
  buildoutCache.set(memoryKey, { total: 1, items: [{ id: 4, sale: true, show_link: "https://example.test/?propertyId=4", index_attributes: [null, ["Price", "$1,000,000"], ["Price", "$2,000,000"], ["Building Size", "100 SF"], ["Building Size", "200 SF"], ["# of Spaces", "2 Spaces"]] }] });
  try {
    const result = await srcBuildout("Fixture", key, "https://example.test", "sale", 10, false);
    assert.equal(result.listings.length, 1);
    const listing = result.listings[0];
    assert.equal(listing.salePriceUsd, null);
    assert.equal(listing.salePriceText, null);
    assert.equal(listing.sizeText, null);
    assert.equal(listing.buildoutAttributeMapping.duplicateAttributeCount, 2);
    assert.equal(listing.buildoutAttributeMapping.malformedAttributeCount, 1);
    assert.equal(listing.buildoutFieldObservations.availableSpaceCount.state, "invalid");
    assert.equal(listing.buildoutFieldObservations.availableSpaceCount.literals[0].literal, "2 Spaces");
  } finally { buildoutCache.delete(memoryKey); globalThis.fetch = oldFetch; }
});

test("collector skips full canonical map at input limit and reports total omissions", async () => {
  const key = "fixture-over-limit";
  const memoryKey = `${key}\u0000${buildoutQueryFingerprint({})}`;
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error("unexpected network request"); };
  const attrs: unknown[] = [["Price", "$1,000,000"], ["# of Spaces", "3 Spaces"]];
  while (attrs.length < 1001) attrs.push(["Other", "value"]);
  buildoutCache.set(memoryKey, { total: 1, items: [{ id: 5, sale: true, size_summary: "999 SF", show_link: "https://example.test/?propertyId=5", index_attributes: attrs }] });
  try {
    const result = await srcBuildout("Fixture", key, "https://example.test", "sale", 10, false);
    const listing = result.listings[0];
    assert.equal(listing.salePriceUsd, null);
    assert.equal(listing.sizeText, null);
    assert.equal(listing.extraFacts, undefined);
    assert.equal(listing.buildoutFieldObservations.omittedAttributeCount, 1001);
    assert.equal(listing.buildoutAttributeMapping.omittedAttributeCount, 1001);
  } finally { buildoutCache.delete(memoryKey); globalThis.fetch = oldFetch; }
});
