// Pure unit tests for lib/parse.ts. No network, no argv side effects
// (parse.ts deliberately does not import lib/config.ts). node:test style,
// matching the existing tests/ts/lib/*.test.ts files.
//
// Loads the shared golden test-vector fixture (tests/fixtures/golden_parse_vectors.json)
// and asserts every vector for the TS side. Also asserts null/garbage inputs
// return null/empty, never throw.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import {
  parseLeaseRate,
  parseMoney,
  acresToSf,
  parseAmountIgnoringCurrencyLabel,
  parsePercentToFraction,
  normBuildingClass,
  parseSizeText,
  isPerSfText,
  type LeaseRate,
} from "../../../lib/parse.js";

// ---------------------------------------------------------------------------
// Load shared golden test-vector fixture
// ---------------------------------------------------------------------------

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = resolve(__dirname, "../../fixtures/golden_parse_vectors.json");

interface GoldenVector {
  fn: string;
  input: string;
  expected: any;
  note?: string;
}

const vectors: GoldenVector[] = JSON.parse(readFileSync(FIXTURE_PATH, "utf8"));

// ---------------------------------------------------------------------------
// Helper: dispatch by function name
// ---------------------------------------------------------------------------

function dispatch(fn: string, input: string): any {
  switch (fn) {
    case "parseLeaseRate":
      return parseLeaseRate(input);
    case "parseMoney":
      return parseMoney(input);
    case "acresToSf":
      return acresToSf(input);
    case "parseAmountIgnoringCurrencyLabel":
      return parseAmountIgnoringCurrencyLabel(input);
    case "parsePercentToFraction":
      return parsePercentToFraction(input);
    case "normBuildingClass":
      return normBuildingClass(input);
    case "parseSizeText":
      return parseSizeText(input);
    case "isPerSfText":
      return isPerSfText(input);
    default:
      throw new Error(`Unknown fn: ${fn}`);
  }
}

// ---------------------------------------------------------------------------
// Golden vector tests (parametrized)
// ---------------------------------------------------------------------------

for (const vec of vectors) {
  const label = `golden[${vec.fn}] input="${vec.input}"${vec.note ? " | " + vec.note : ""}`;
  test(label, () => {
    const actual = dispatch(vec.fn, vec.input);
    assert.deepEqual(
      actual,
      vec.expected,
      `${vec.fn}("${vec.input}") expected ${JSON.stringify(vec.expected)} got ${JSON.stringify(actual)}`
    );
  });
}

// ---------------------------------------------------------------------------
// parseLeaseRate: additional edge cases
// ---------------------------------------------------------------------------

test("parseLeaseRate returns {min:null,max:null,type:null} for null input", () => {
  assert.deepEqual(parseLeaseRate(null), { min: null, max: null, type: null });
});

test("parseLeaseRate returns null result for empty string", () => {
  assert.deepEqual(parseLeaseRate(""), { min: null, max: null, type: null });
});

test("parseLeaseRate: bare $N.NN with no qualifiers is trusted as-is (like golden vector 1)", () => {
  // A bare "$N.NN" with no other tokens is returned as min (the adapter labeled this as a rate).
  // This matches the golden vector row 1: "$23.40" -> {min:23.40, max:null, type:null}.
  assert.deepEqual(parseLeaseRate("$12.50"), { min: 12.50, max: null, type: null });
});

test("parseLeaseRate: large bare dollar amount that exceeds 500 is rejected by the AY cap", () => {
  // "$1,200,000" is a bare amount but min > 500, so rejected by the implausible-value guard.
  assert.deepEqual(parseLeaseRate("$1,200,000"), { min: null, max: null, type: null });
});

test("parseLeaseRate: NNN variant 'Triple Net' recognized", () => {
  const r = parseLeaseRate("$25.00/SF/YR Triple Net");
  assert.equal(r.type, "nnn");
  assert.equal(r.min, 25);
});

test("parseLeaseRate: Full Service Gross alias FSG", () => {
  const r = parseLeaseRate("$24.00/SF/YR, FSG");
  assert.equal(r.type, "full_service");
  assert.equal(r.min, 24);
  assert.equal(r.max, null);
});

test("parseLeaseRate: modified_gross beats gross when both present", () => {
  const r = parseLeaseRate("$20.00 PSF Modified Gross");
  assert.equal(r.type, "modified_gross");
});

test("parseLeaseRate: range where both values are above 100 is kept (institutional high-value markets)", () => {
  // Both annualized: $120 and $150 per SF/yr are high but not a mis-range.
  const r = parseLeaseRate("$120 - $150 PSF");
  // Neither value is < 100 while the other is > 100, so it is NOT rejected.
  assert.equal(r.min, 120);
  assert.equal(r.max, 150);
});

test("parseLeaseRate: 'per square foot' long form recognized as per-SF signal", () => {
  const r = parseLeaseRate("$18.00 per square foot NNN");
  assert.equal(r.min, 18);
  assert.equal(r.type, "nnn");
});

// ---------------------------------------------------------------------------
// parseMoney: edge cases
// ---------------------------------------------------------------------------

test("parseMoney returns null for null/empty", () => {
  assert.equal(parseMoney(null), null);
  assert.equal(parseMoney(""), null);
  assert.equal(parseMoney("price on application"), null);
});

test("parseMoney extracts first $ amount, ignores rest", () => {
  assert.equal(parseMoney("asking $42.50 today or $50.00 later"), 42.50);
});

test("parseMoney handles large amounts with commas", () => {
  assert.equal(parseMoney("$1,250,000"), 1250000);
  assert.equal(parseMoney("$12,500,000.00"), 12500000);
});

test("parseMoney: no $ sign -> null", () => {
  assert.equal(parseMoney("272.07"), null);
  assert.equal(parseMoney("1,250,000"), null);
});

// ---------------------------------------------------------------------------
// acresToSf: edge cases
// ---------------------------------------------------------------------------

test("acresToSf returns null for null/empty/no acres token", () => {
  assert.equal(acresToSf(null), null);
  assert.equal(acresToSf(""), null);
  assert.equal(acresToSf("12,500 SF"), null);
});

test("acresToSf handles commas in acres value", () => {
  const r = acresToSf("1,000 acres");
  assert.ok(r !== null);
  assert.equal(r, 1000 * 43560);
});

test("acresToSf: 'ac' short form", () => {
  const r = acresToSf("2.5 ac");
  assert.ok(r !== null);
  assert.ok(Math.abs(r! - 2.5 * 43560) < 0.01);
});

// ---------------------------------------------------------------------------
// parseAmountIgnoringCurrencyLabel: edge cases
// ---------------------------------------------------------------------------

test("parseAmountIgnoringCurrencyLabel: null/empty -> null", () => {
  assert.equal(parseAmountIgnoringCurrencyLabel(null), null);
  assert.equal(parseAmountIgnoringCurrencyLabel(""), null);
});

test("parseAmountIgnoringCurrencyLabel: GBP prefix stripped", () => {
  assert.equal(parseAmountIgnoringCurrencyLabel("GBP 1,234,567.00"), 1234567);
});

test("parseAmountIgnoringCurrencyLabel: EUR prefix stripped", () => {
  assert.equal(parseAmountIgnoringCurrencyLabel("EUR 500,000"), 500000);
});

// ---------------------------------------------------------------------------
// parsePercentToFraction: edge cases
// ---------------------------------------------------------------------------

test("parsePercentToFraction: null/empty -> null", () => {
  assert.equal(parsePercentToFraction(null), null);
  assert.equal(parsePercentToFraction(""), null);
});

test("parsePercentToFraction: 100% -> 1.0", () => {
  const r = parsePercentToFraction("100%");
  assert.ok(r !== null);
  assert.ok(Math.abs(r! - 1.0) < 0.0001);
});

test("parsePercentToFraction: 0% / zero -> null", () => {
  assert.equal(parsePercentToFraction("0%"), null);
  assert.equal(parsePercentToFraction("0.0"), null);
});

test("parsePercentToFraction: value in (0,1] without % sign treated as fraction", () => {
  assert.equal(parsePercentToFraction("0.5"), 0.5);
  assert.equal(parsePercentToFraction("1.0"), 1.0);
});

test("parsePercentToFraction: value > 1 without % sign treated as percent", () => {
  // "95.5" without % -> treated as 95.5% -> 0.955
  const r = parsePercentToFraction("95.5");
  assert.ok(r !== null);
  assert.ok(Math.abs(r! - 0.955) < 0.0001);
});

// ---------------------------------------------------------------------------
// normBuildingClass: edge cases
// ---------------------------------------------------------------------------

test("normBuildingClass: null/empty -> null", () => {
  assert.equal(normBuildingClass(null), null);
  assert.equal(normBuildingClass(""), null);
});

test("normBuildingClass: case-insensitive 'class a' -> 'A'", () => {
  assert.equal(normBuildingClass("class a"), "A");
  assert.equal(normBuildingClass("CLASS B"), "B");
  assert.equal(normBuildingClass("Class C"), "C");
  assert.equal(normBuildingClass("Class D"), "D");
});

test("normBuildingClass: bare single letter -> class", () => {
  assert.equal(normBuildingClass("A"), "A");
  assert.equal(normBuildingClass("B"), "B");
});

test("normBuildingClass: multi-token prose with stray letter -> null (avoids false match)", () => {
  // A long description that contains 'A' somewhere should not be matched.
  assert.equal(normBuildingClass("Prime office in the heart of Dallas"), null);
});

test("normBuildingClass: 'Class A+' -> 'A' (ignores modifier)", () => {
  // Class A+ is treated as Class A for the \bClass\s+([A-D])\b match.
  assert.equal(normBuildingClass("Class A+"), "A");
});

// ---------------------------------------------------------------------------
// parseSizeText: edge cases
// ---------------------------------------------------------------------------

test("parseSizeText: null/empty -> {sizeSf: null, lotSf: null}", () => {
  assert.deepEqual(parseSizeText(null), { sizeSf: null, lotSf: null });
  assert.deepEqual(parseSizeText(""), { sizeSf: null, lotSf: null });
});

test("parseSizeText: pure SF value -> {sizeSf, lotSf: null}", () => {
  assert.deepEqual(parseSizeText("5,000 SF"), { sizeSf: 5000, lotSf: null });
});

test("parseSizeText: pure acres value -> {sizeSf: null, lotSf}", () => {
  const r = parseSizeText("3.0 Acres");
  assert.equal(r.sizeSf, null);
  assert.ok(r.lotSf !== null);
  assert.ok(Math.abs(r.lotSf! - 3.0 * 43560) < 0.01);
});

test("parseSizeText: 'sq ft' long form recognized", () => {
  const r = parseSizeText("10,000 sq ft");
  assert.equal(r.sizeSf, 10000);
  assert.equal(r.lotSf, null);
});

// ---------------------------------------------------------------------------
// isPerSfText: edge cases
// ---------------------------------------------------------------------------

test("isPerSfText: null/empty -> false", () => {
  assert.equal(isPerSfText(null), false);
  assert.equal(isPerSfText(""), false);
});

test("isPerSfText: PSF recognized", () => {
  assert.equal(isPerSfText("$32 PSF"), true);
});

test("isPerSfText: '/sf' token recognized", () => {
  assert.equal(isPerSfText("$6.00/SF"), true);
});

test("isPerSfText: 'per sq ft' recognized", () => {
  assert.equal(isPerSfText("$18 per sq ft"), true);
});

test("isPerSfText: absolute sale price not flagged", () => {
  assert.equal(isPerSfText("$5,000,000"), false);
  assert.equal(isPerSfText("Sale Price $2.1M"), false);
});

// ---------------------------------------------------------------------------
// Never-throw invariant
// ---------------------------------------------------------------------------

test("no parser function throws on garbage/null input", () => {
  const garbage = [null, undefined, "", 0, {}, [], "!!!$$$", "NaN", "\x00\xFF"] as any[];
  for (const v of garbage) {
    assert.doesNotThrow(() => parseLeaseRate(v));
    assert.doesNotThrow(() => parseMoney(v));
    assert.doesNotThrow(() => acresToSf(v));
    assert.doesNotThrow(() => parseAmountIgnoringCurrencyLabel(v));
    assert.doesNotThrow(() => parsePercentToFraction(v));
    assert.doesNotThrow(() => normBuildingClass(v));
    assert.doesNotThrow(() => parseSizeText(v));
    assert.doesNotThrow(() => isPerSfText(v));
  }
});
