import test from "node:test";
import assert from "node:assert/strict";

const { buildoutInventoryObservation, preserveBuildoutInventoryObservation } = await import("../../../sources/buildout-observation.js");
const { prune } = await import("../../../lib/util.js");

test("offline prototype retains literals for 60 distinct synthetic rows", () => {
  for (let i = 0; i < 60; i++) {
    const attributes = [["Price", `$${i + 1}`], ["Lot", `${i + 1} Acres`],
      ["Unknown", false], ["Unknown", null], ["Empty", ""]];
    const original = structuredClone(attributes);
    const observation = buildoutInventoryObservation(attributes, {
      observedAt: "2026-10-28T12:00:00Z", generationId: "test-generation" });
    const listing = prune({ id: i + 1 });
    preserveBuildoutInventoryObservation(listing, observation);
    assert.deepEqual(listing.buildoutInventoryObservation.attributes, original);
    assert.deepEqual(attributes, original);
  }
});

test("unsafe, malformed and excessive entries are counted rather than persisted", () => {
  const input: unknown[] = [["Rate", "$2.50/SF/month"], ["API key", "abc"],
    ["Description", "Authorization: Bearer abc123"], ["Link", "https://example.org/?token=abc"],
    ["Nested", { private: "x" }], ["Broken"], ["Control", "a\u0001b"], ["Long", "x".repeat(2049)],
    ...Array.from({ length: 105 }, (_, i) => [`Value ${i}`, i])];
  const result = buildoutInventoryObservation(input, { observedAt: "time", generationId: "generation" });
  assert.equal(result.attributes.length, 100);
  assert.equal(result.omittedCount, input.length - 100);
  assert.deepEqual(result.omissions, { malformed: 3, sensitive: 3, limit: 7 });
  assert.deepEqual(result.attributes[0], ["Rate", "$2.50/SF/month"]);
  assert.equal(JSON.stringify(result).includes("Bearer"), false);
  assert.deepEqual(buildoutInventoryObservation([], {}).attributes, []);
  assert.equal(buildoutInventoryObservation(undefined, {}), undefined);
  assert.deepEqual(buildoutInventoryObservation([["Password", "secret"]], {}).omissions,
    { malformed: 0, sensitive: 1, limit: 0 });
});


test("preserves multiline values and rejects credentials and signed query parameters", () => {
  const entries = [["Notes", "First line\nSecond line\tindented\r\nThird"],
    ["Authorization", "Basic dXNlcjpwYXNz"], ["Notes", "Authorization: Basic dXNlcjpwYXNz"],
    ["Notes", "Cookie: session=abc"], ["Notes", "Set-Cookie: sid=abc"],
    ["Link", "https://example.org/?X-Amz-Signature=abc"],
    ["Link", "https://example.org/?X-Amz-Credential=abc"],
    ["Link", "https://example.org/?page=1"]];
  const result = buildoutInventoryObservation(entries);
  assert.deepEqual(result.attributes, [entries[0], entries[7]]);
  assert.deepEqual(result.omissions, { malformed: 0, sensitive: 6, limit: 0 });
});

test("malformed pairs are counted by the offline prototype", () => {
  const result = buildoutInventoryObservation([null, ["Price", "$100"], ["Broken"], ["Notes", "line one\nline two"]]);
  assert.deepEqual(result.attributes, [["Price", "$100"], ["Notes", "line one\nline two"]]);
  assert.equal(result.omissions.malformed, 2);
});

test("UTF-8 byte budget counts oversized entries without truncation", () => {
  const result = buildoutInventoryObservation(Array.from({ length: 100 }, (_, i) => [`Note ${i}`, "界".repeat(2048)]));
  assert.ok(result.omissions.limit > 0);
  assert.equal(result.attributes.length + result.omittedCount, 100);
  assert.ok(result.attributes.every(pair => typeof pair[1] === "string" && pair[1].length === 2048));
  assert.ok(Buffer.byteLength(JSON.stringify(result), "utf8") < 250_000);
});


test("provenance distinguishes missing and invalid values without retaining them", () => {
  assert.deepEqual(buildoutInventoryObservation([]).provenance, { observedAt: "missing", generationId: "missing" });
  for (const time of ["2026-02-30T12:00:00Z", "2026-10-28T12:00:00+14:01", "2026-10-28", "Bearer dummy"]) {
    const result = buildoutInventoryObservation([], { observedAt: time, generationId: "Bearer dummy" });
    assert.deepEqual(result.provenance, { observedAt: "invalid", generationId: "invalid" });
    assert.equal("observedAt" in result, false);
    assert.equal("generationId" in result, false);
  }
  const valid = buildoutInventoryObservation([], { observedAt: "2024-02-29T23:59:59.123456+14:00", generationId: "test-123" });
  assert.deepEqual(valid.provenance, { observedAt: "valid", generationId: "valid" });
  assert.equal(valid.observedAt, "2024-02-29T23:59:59.123456+14:00");
});

test("credential detection preserves ordinary words and checks decoded signed URL keys", () => {
  const benign = [["Notes", "Basic rent includes utilities"], ["Secretary", "Jane Smith"], ["Notes", "Basic infrastructure is included"], ["Notes", "Bearer walls support roof"], ["Notes", "Bearer walls"], ["Notes", "Bearer 100000 SF"], ["Notes", "Use Bearer abcdefgh to access"],
    ["Notes", "View https://example.org/?page=1&label=rent%20included for details"]];
  const unsafe = [["Notes", "Visit https://example.org/?X-Goog-Signature=abc for details"],
    ["Notes", "See https://example.org/?%74oken=abc"],
    ["Notes", "https://example.org/?%2574oken=abc"],
    ["Notes", "https://example.org/?X-Amz-Security-Token=abc"],
    ["Notes", "https://example.org/#access_token=abc"],
    ["Notes", "https://example.org/#section?%61ccess_token=abc"],
    ["Notes", "https://example.org/?X-Goog-Credential=abc"],
    ["Notes", "https://example.org/?bad=%ZZ"],
    ["Notes", "https://example.org/?bad=%FF"],
    ["Notes", "https://user:pass@example.org/path"],
    ["Notes", "Basic dXNlcjpwYXNz"], ["Authorization", "Basic abc"],
    ["Notes", "Authorization: Bearer abc"]];
  const result = buildoutInventoryObservation([...benign, ...unsafe]);
  assert.deepEqual(result.attributes, benign);
  assert.equal(result.omissions.sensitive, unsafe.length);
});
