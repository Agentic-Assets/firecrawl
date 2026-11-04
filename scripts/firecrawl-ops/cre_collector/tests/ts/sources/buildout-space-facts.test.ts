import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// Deny transport before importing the real producer; every test seeds its cache.
let networkAttempts = 0;
globalThis.fetch = async () => {
  networkAttempts++;
  throw new Error("unexpected Buildout test transport");
};
const { srcBuildout, buildoutCache, buildoutQueryFingerprint } = await import("../../../sources/buildout.js");
const fixture = JSON.parse(readFileSync(new URL("../../fixtures/raw_data/buildout-space-facts.json", import.meta.url), "utf8"));

async function mapped(attributes = fixture.index_attributes, monitor = false) {
  const row = { ...structuredClone(fixture), index_attributes: structuredClone(attributes) };
  const before = structuredClone(row);
  const key = "offline-space-facts";
  buildoutCache.set(`${key}\u0000${buildoutQueryFingerprint({})}`, { items: [row], total: 1 });
  try {
    const result = await srcBuildout("Cawley Chicago", key, "https://cawleycre.com/", "lease", Infinity, monitor, {});
    assert.deepEqual(row, before, "the native observation remains unchanged");
    assert.equal(networkAttempts, 0);
    assert.equal(result.listings.length, 1);
    return result.listings[0];
  } finally {
    buildoutCache.clear();
  }
}

test("observed Cawley availability-space text survives without units or parking inference", async () => {
  const listing = await mapped();
  assert.deepEqual(listing.extraFacts, { number_of_spaces: "4 Spaces" });
  assert.equal(listing.id, "586374");
  assert.equal(listing.assetType, "Office Building");
  assert.equal(listing.availableSf, 970);
  assert.equal(listing.maxDivisibleSf, 2480);
  assert.equal(listing.units, undefined);
  assert.equal(listing.parkingSpaces, undefined);
  assert.equal(listing.preserveChildCollections, true);
});

test("monitor preserves the same literal availability fact", async () => {
  assert.deepEqual((await mapped(fixture.index_attributes, true)).extraFacts, { number_of_spaces: "4 Spaces" });
});

test("equivalent repeated labels agree after existing whitespace cleaning", async () => {
  assert.deepEqual((await mapped([["# of Spaces", "4 Spaces"], ["# of Spaces", " 4   Spaces "]])).extraFacts,
    { number_of_spaces: "4 Spaces" });
});

for (const [name, attributes] of [
  ["absent", [["Space Type", "Office Building"]]],
  ["blank", [["# of Spaces", "  "]]],
  ["malformed numeric", [["# of Spaces", 4]]],
  ["malformed shape", [["# of Spaces", "4 Spaces", "unowned third cell"]]],
  ["conflicting", [["# of Spaces", "4 Spaces"], ["# of Spaces", "5 Spaces"]]],
  ["valid plus malformed", [["# of Spaces", "4 Spaces"], ["# of Spaces", null]]],
  ["valid plus blank", [["# of Spaces", "4 Spaces"], ["# of Spaces", ""]]],
  ["different label", [["Parking Spaces", "4 Spaces"]]],
] as const) {
  test(`${name} space facts do not choose a value`, async () => {
    assert.equal((await mapped(attributes)).extraFacts, undefined);
  });
}

test("space-fact lift leaves all unrelated mapped output unchanged", async () => {
  const withFact = await mapped();
  const withoutFact = await mapped(fixture.index_attributes.filter(([label]: string[]) => label !== "# of Spaces"));
  const { extraFacts, buildoutFieldObservations, rawBuildout: withRaw, buildoutRawRetention: withReceipt, ...rest } = withFact;
  const { rawBuildout: withoutRaw, buildoutRawRetention: withoutReceipt, ...withoutRest } = withoutFact;
  assert.deepEqual(rest, withoutRest);
  assert.deepEqual(withReceipt, withoutReceipt);
  assert.deepEqual(withRaw.index_attributes, fixture.index_attributes);
  assert.deepEqual(withoutRaw.index_attributes, fixture.index_attributes.filter(([label]: string[]) => label !== "# of Spaces"));
  const { index_attributes: _withAttributes, ...withRawRest } = withRaw;
  const { index_attributes: _withoutAttributes, ...withoutRawRest } = withoutRaw;
  assert.deepEqual(withRawRest, withoutRawRest);
  assert.deepEqual(extraFacts, { number_of_spaces: "4 Spaces" });
  assert.deepEqual(buildoutFieldObservations, {
    schema: "buildout-field-observations-v1",
    sourceIdentity: "unverified",
    freshness: "unverified",
    promotion: "not_authorized",
    malformedAttributeCount: 0,
    availableSpaceCount: {
      state: "observed",
      literals: [{ sourceOrdinal: 2, literal: "4 Spaces" }],
      omittedCount: 0,
      value: 4,
    },
  });
});
