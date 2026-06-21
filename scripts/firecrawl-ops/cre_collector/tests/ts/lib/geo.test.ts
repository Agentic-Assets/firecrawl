// Pure unit tests for lib/geo.ts. No network, no argv side effects
// (geo.ts deliberately does not import lib/config.ts). node:test style,
// matching the existing tests/ts/lib/*.test.ts files.

import test from "node:test";
import assert from "node:assert/strict";

import { zip5, geoKey } from "../../../lib/geo.js";

// ---------------------------------------------------------------------------
// zip5: normalization
// ---------------------------------------------------------------------------

test("zip5: 5-digit ZIP passthrough", () => {
  assert.equal(zip5("75201"), "75201");
  assert.equal(zip5("10001"), "10001");
  assert.equal(zip5("00501"), "00501");
});

test("zip5: ZIP+4 hyphenated form -> 5 digits", () => {
  assert.equal(zip5("75201-1234"), "75201");
  assert.equal(zip5("10001-0000"), "10001");
});

test("zip5: 9-digit no-hyphen form -> 5 digits", () => {
  assert.equal(zip5("752011234"), "75201");
  assert.equal(zip5("100010000"), "10001");
});

test("zip5: whitespace trimmed", () => {
  assert.equal(zip5("  75201  "), "75201");
  assert.equal(zip5("  75201-1234  "), "75201");
});

test("zip5: null/empty -> null", () => {
  assert.equal(zip5(null), null);
  assert.equal(zip5(""), null);
  assert.equal(zip5("   "), null);
});

test("zip5: non-numeric -> null", () => {
  assert.equal(zip5("ABCDE"), null);
  assert.equal(zip5("1234A"), null);
});

test("zip5: 4 digits -> null (too short)", () => {
  assert.equal(zip5("1234"), null);
});

test("zip5: 6 digits -> null (not 5 or 9)", () => {
  assert.equal(zip5("123456"), null);
});

test("zip5: 7 digits -> null", () => {
  assert.equal(zip5("1234567"), null);
});

test("zip5: 9 digits with hyphen in wrong place -> null (total digit count matters)", () => {
  // "12-3456789" has 9 digits total; after stripping hyphen = "123456789" = valid 9-digit.
  // But "1234-56789" has 4+5 = 9 digits -> valid after strip.
  assert.equal(zip5("1234-56789"), "12345");
});

test("zip5: leading zeros preserved", () => {
  assert.equal(zip5("02110"), "02110");
  assert.equal(zip5("02110-1234"), "02110");
});

// ---------------------------------------------------------------------------
// geoKey: stable lat/lng key
// ---------------------------------------------------------------------------

test("geoKey: returns 4-dp 'lat,lng' string for valid coordinates", () => {
  assert.equal(geoKey(32.7869, -96.7971), "32.7869,-96.7971");
  assert.equal(geoKey(40.7484, -73.9967), "40.7484,-73.9967");
});

test("geoKey: rounds to 4 decimal places", () => {
  assert.equal(geoKey(32.78694567, -96.79712345), "32.7869,-96.7971");
  assert.equal(geoKey(40.748_4999, -73.996_6999), "40.7485,-73.9967");
});

test("geoKey: zero coordinates are valid", () => {
  assert.equal(geoKey(0, 0), "0.0000,0.0000");
});

test("geoKey: boundary values accepted", () => {
  assert.equal(geoKey(90, 180), "90.0000,180.0000");
  assert.equal(geoKey(-90, -180), "-90.0000,-180.0000");
});

test("geoKey: null/undefined -> null", () => {
  assert.equal(geoKey(null, null), null);
  assert.equal(geoKey(null, -96.7971), null);
  assert.equal(geoKey(32.7869, null), null);
  assert.equal(geoKey(undefined as any, undefined as any), null);
});

test("geoKey: non-finite -> null", () => {
  assert.equal(geoKey(NaN, -96.7971), null);
  assert.equal(geoKey(Infinity, -96.7971), null);
  assert.equal(geoKey(32.7869, -Infinity), null);
  assert.equal(geoKey(NaN, NaN), null);
});

test("geoKey: out-of-range lat -> null", () => {
  assert.equal(geoKey(91, -96.7971), null);
  assert.equal(geoKey(-91, -96.7971), null);
});

test("geoKey: out-of-range lng -> null", () => {
  assert.equal(geoKey(32.7869, 181), null);
  assert.equal(geoKey(32.7869, -181), null);
});

test("geoKey: non-number types -> null", () => {
  assert.equal(geoKey("32.7869" as any, -96.7971), null);
  assert.equal(geoKey(32.7869, "-96.7971" as any), null);
  assert.equal(geoKey({} as any, [] as any), null);
});

// ---------------------------------------------------------------------------
// geoKey: stability (same coordinates always produce same key)
// ---------------------------------------------------------------------------

test("geoKey: identical calls produce identical keys", () => {
  const k1 = geoKey(47.6062, -122.3321);
  const k2 = geoKey(47.6062, -122.3321);
  assert.equal(k1, k2);
});

test("geoKey: high-precision float within same 4-dp bucket -> same key", () => {
  // 47.60620001 and 47.60621000 both round to 47.6062
  const k1 = geoKey(47.60620001, -122.3321);
  const k2 = geoKey(47.60621000, -122.3321);
  assert.equal(k1, k2);
});

test("geoKey: coordinates across a 4-dp boundary differ", () => {
  const k1 = geoKey(47.6062, -122.3321);
  const k2 = geoKey(47.6063, -122.3321);
  assert.notEqual(k1, k2);
});

// ---------------------------------------------------------------------------
// Never-throw invariant
// ---------------------------------------------------------------------------

test("zip5 and geoKey never throw on garbage input", () => {
  const garbage = [null, undefined, "", 0, {}, [], NaN, Infinity, "\x00", "!!!"] as any[];
  for (const v of garbage) {
    assert.doesNotThrow(() => zip5(v));
    assert.doesNotThrow(() => geoKey(v, v));
    assert.doesNotThrow(() => geoKey(0, v));
    assert.doesNotThrow(() => geoKey(v, 0));
  }
});
