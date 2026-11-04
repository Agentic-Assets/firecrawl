import test from "node:test";
import assert from "node:assert/strict";

import { parseBuildoutCapRateObservation } from "../../../sources/buildout-cap-rate-observation.js";

test("explicit source percentages retain literals and normalize to fractions", () => {
  for (const [literal, fraction] of [["6.5%", 0.065], ["0.5%", 0.005], ["29.9%", 0.299], [" 5.2500% ", 0.0525]] as const) {
    const pairs = [["Price", "$2,699,085"], ["Cap Rate", literal], ["Other", ""]];
    const before = structuredClone(pairs);
    assert.deepEqual(parseBuildoutCapRateObservation(pairs), { kind: "observed", literal, fraction });
    assert.deepEqual(pairs, before);
  }
});

test("missing and malformed attribute sets remain distinct", () => {
  assert.deepEqual(parseBuildoutCapRateObservation([["Price", "$1"]]), { kind: "missing" });
  for (const input of [null, {}, [["Cap Rate"]], [["Other", "ok"], null], [["", "x"]]]) {
    assert.equal(parseBuildoutCapRateObservation(input).kind, "invalid");
  }
  assert.equal(parseBuildoutCapRateObservation([["CAP RATE", "6%"]]).kind, "missing");
});

test("duplicate cap-rate labels are ambiguous even if their values agree", () => {
  for (const pairs of [
    [["Cap Rate", "6%"], ["Cap Rate", "6%"]],
    [["Cap Rate", "6%"], ["Other", "x"], ["Cap Rate", "7%"]],
  ]) {
    assert.deepEqual(parseBuildoutCapRateObservation(pairs), {
      kind: "ambiguous", literals: [pairs[0][1], pairs[pairs.length - 1][1]],
    });
  }
});

test("refuses ambiguous units, ranges, unsupported values and lossy precision", () => {
  for (const value of [6.5, true, null, NaN, Infinity, "6.5", "6.5%-7%", "~6.5%", "6.5% Cap Rate", "0%", "30%", "-1%", "0.00001%", "1e2%", "Infinity%", "", "x".repeat(1024)]) {
    assert.deepEqual(parseBuildoutCapRateObservation([["Cap Rate", value]]), {
      kind: "invalid", reason: "invalid_value", literals: [value],
    });
  }
});
