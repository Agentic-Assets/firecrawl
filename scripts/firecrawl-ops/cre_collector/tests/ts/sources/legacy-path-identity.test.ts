import assert from "node:assert/strict";
import test from "node:test";
import { legacyIdentityListings } from "../../fixtures/legacy-path-identity.js";
import * as daum from "../../../sources/daum-commercial.js";
import * as foundry from "../../../sources/foundry-commercial.js";
import * as lyon from "../../../sources/lyon-stahl.js";

test("legacy producers preserve literal source-specific keys and separate native provenance", () => {
  for (const slug of ["MiXeD%2fCase", "MiXeD%2FCase", "50388"]) {
    const rows = legacyIdentityListings(slug);
    assert.deepEqual(rows.map(row => row.id), [`property/${slug}`, `property/${slug}`, slug]);
    assert.deepEqual(rows.map(row => row.daumPostId ?? row.foundryPostId ?? row.lyonStahlPostId), ["101", "101", "101"]);
  }
});

test("legacy source uniqueness refuses native or output-key collisions independently", () => {
  const checks = [
    { check: daum.assertDaumUniqueProviderIds, field: "daumPostId" },
    { check: foundry.assertFoundryUniqueProviderIds, field: "foundryPostId" },
    { check: lyon.assertLyonStahlUniqueProviderIds, field: "lyonStahlPostId" },
  ];
  for (const { check, field } of checks) {
    assert.doesNotThrow(() => check([{ id: "first", [field]: "1" }, { id: "second", [field]: "2" }]));
    assert.throws(() => check([{ id: "first", [field]: "1" }, { id: "second", [field]: "1" }]), /duplicate provider ID/);
    assert.throws(() => check([{ id: "same", [field]: "1" }, { id: "same", [field]: "2" }]), /duplicate listing key/);
    assert.throws(() => check([{ id: "first", [field]: "0" }]), /provider ID/);
  }
});

test("legacy key builders reject unsafe or non-property routes before deriving an ID", () => {
  for (const { build, host, route } of [
    { build: daum.daumListingId, host: "daumcommercial.com", route: "property" },
    { build: foundry.foundryListingId, host: "www.foundrycommercial.com", route: "property" },
    { build: lyon.lyonStahlListingId, host: "lyonstahl.com", route: "properties" },
  ]) {
    for (const unsafe of [
      `http://${host}/${route}/one/`, `https://evil.test/${route}/one/`,
      `https://user@${host}/${route}/one/`, `https://${host}:8443/${route}/one/`,
      `https://${host}/${route}/one/?preview=1`, `https://${host}/${route}/one/#preview`,
      `https://${host}/${route}/`, `https://${host}/${route}/one/two/`,
      `https://${host}/${route}/%2e%2e/`, `https://${host}/unrelated/one/`,
    ]) assert.throws(() => build(unsafe), /owned property URL/);
  }
});

// Exact literal spelling from the saved historical namespace, not a new provider request.
test("Foundry retains the observed historical percent-encoded path", () => {
  assert.equal(foundry.foundryListingId("https://www.foundrycommercial.com/property/41-87%c2%b1-acres-for-sale-in-homosassa-fl/"), "property/41-87%c2%b1-acres-for-sale-in-homosassa-fl");
});
