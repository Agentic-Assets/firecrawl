import test from "node:test";
import assert from "node:assert/strict";
import {
  clean,
  num,
  boundedInt,
  moneyToNumber,
  isPerSfPriceText,
  prune,
  pmap,
} from "../../../lib/util.js";

test("clean collapses whitespace and trims", () => {
  assert.equal(clean("  hello   world  "), "hello world");
  assert.equal(clean("no-extra"), "no-extra");
});

test("clean returns null for non-strings and empty results", () => {
  assert.equal(clean(null), null);
  assert.equal(clean(undefined), null);
  assert.equal(clean(42), null);
  assert.equal(clean(""), null);
  assert.equal(clean("   "), null);
});

test("num keeps finite non-zero numbers", () => {
  assert.equal(num(42), 42);
  assert.equal(num(-3.5), -3.5);
});

test("num drops zero, non-finite, and non-numbers", () => {
  assert.equal(num(0), null);
  assert.equal(num(NaN), null);
  assert.equal(num(Infinity), null);
  assert.equal(num(-Infinity), null);
  assert.equal(num("5"), null);
  assert.equal(num(null), null);
});

test("boundedInt clamps and truncates", () => {
  assert.equal(boundedInt("12.9", 3, 1, 10), 10);
  assert.equal(boundedInt("-5", 3, 0, 6), 0);
  assert.equal(boundedInt("4.2", 3, 1, 10), 4);
});

test("boundedInt uses fallback for undefined and non-finite input", () => {
  assert.equal(boundedInt(undefined, 5, 1, 10), 5);
  assert.equal(boundedInt("not-a-number", 7, 1, 10), 7);
});

test("moneyToNumber parses dollar amounts", () => {
  assert.equal(moneyToNumber("$1,250,000"), 1250000);
  assert.equal(moneyToNumber("asking $ 42.50 today"), 42.5);
});

test("moneyToNumber returns null when no dollar amount", () => {
  assert.equal(moneyToNumber(null), null);
  assert.equal(moneyToNumber(""), null);
  assert.equal(moneyToNumber("price on application"), null);
});

test("isPerSfPriceText detects per-SF lease phrasing", () => {
  assert.equal(isPerSfPriceText("$32 / SF"), true);
  assert.equal(isPerSfPriceText("12 per sq ft"), true);
  assert.equal(isPerSfPriceText("18 PSF"), true);
  assert.equal(isPerSfPriceText("$5,000,000"), false);
  assert.equal(isPerSfPriceText(null), false);
});

test("prune removes empty nested values", () => {
  const input = {
    keep: "yes",
    dropEmpty: "",
    dropFalse: false,
    dropNull: null,
    nested: { a: 1, b: "", c: { d: undefined, e: "ok" } },
    list: ["x", "", null, { z: false }],
  };
  assert.deepEqual(prune(input), {
    keep: "yes",
    nested: { a: 1, c: { e: "ok" } },
    list: ["x"],
  });
});

test("prune returns undefined for wholly empty structures", () => {
  assert.equal(prune({ a: "", b: null }), undefined);
  assert.equal(prune(["", null, false]), undefined);
});

test("pmap preserves input order under concurrency", async () => {
  const items = [0, 1, 2, 3, 4];
  const delays = [30, 10, 50, 5, 20];
  const out = await pmap(items, 3, async (value, index) => {
    await new Promise((resolve) => setTimeout(resolve, delays[index]));
    return value * 10;
  });
  assert.deepEqual(out, [0, 10, 20, 30, 40]);
});

test("pmap handles empty input", async () => {
  assert.deepEqual(await pmap([], 2, async () => 1), []);
});

test("pmap stops scheduling new work after the first rejection", async () => {
  const started: number[] = [];
  await assert.rejects(
    () =>
      pmap([0, 1, 2, 3, 4, 5], 3, async (value) => {
        started.push(value);
        if (value === 0) throw new Error("terminal");
        await new Promise((resolve) => setTimeout(resolve, 20));
        return value;
      }),
    /terminal/
  );
  await new Promise((resolve) => setTimeout(resolve, 40));
  assert.deepEqual(started, [0, 1, 2]);
});
