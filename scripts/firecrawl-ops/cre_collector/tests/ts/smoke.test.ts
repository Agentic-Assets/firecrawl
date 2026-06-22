import assert from "node:assert/strict";
import { test } from "node:test";

test("smoke: harness is wired", () => {
  assert.equal(1 + 1, 2);
});
