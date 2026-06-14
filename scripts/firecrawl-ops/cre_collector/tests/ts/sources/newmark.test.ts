import test from "node:test";
import assert from "node:assert/strict";
import { normalizePersonName, newmarkState } from "../../../sources/newmark.js";

test("normalizePersonName lowercases and collapses whitespace", () => {
  assert.equal(normalizePersonName("  Jane   Q.  Public "), "jane q. public");
  assert.equal(normalizePersonName(null), null);
  assert.equal(normalizePersonName(""), null);
});

test("newmarkState prefers explicit state fields", () => {
  assert.equal(newmarkState({ state: "Texas" }), "Texas");
  assert.equal(newmarkState({ state_code: "tx" }), "TX");
});

test("newmarkState infers DC from Washington zip", () => {
  assert.equal(newmarkState({ city: "Washington", zip: "20005" }), "DC");
});

test("newmarkState returns null when no signal", () => {
  assert.equal(newmarkState({ city: "Chicago" }), null);
});
