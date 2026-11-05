process.argv = [process.argv[0]!, process.argv[1]!, "--monitor"];
import assert from "node:assert/strict";
import test from "node:test";

const { PAGE_CAP } = await import("../../../lib/config.js");

test("monitor retains its bounded default", () => {
  assert.equal(PAGE_CAP, 60);
});
