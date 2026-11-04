import test from "node:test";
import assert from "node:assert/strict";
import { createObservationClock, MAX_OBSERVATION_CLOCK_DRIFT_MS } from "../../../lib/observation-clock.js";

function fixture() {
  let wall = Date.parse("2026-10-16T00:00:00Z");
  let mono = 100;
  const now = createObservationClock({ wallNowMs: () => wall, monotonicMs: () => mono });
  return { now, advance(ms: number, wallStep = 0) { wall += ms + wallStep; mono += ms; }, setWall(value: number) { wall = value; }, setMono(value: number) { mono = value; } };
}

test("one anchor orders observations through small backward and forward steps", () => {
  const f = fixture();
  assert.equal(f.now(), "2026-10-16T00:00:00.000Z");
  f.advance(100, -60);
  assert.equal(f.now(), "2026-10-16T00:00:00.100Z");
  f.advance(100, 120);
  assert.equal(f.now(), "2026-10-16T00:00:00.200Z");
});

test("long elapsed operation does not reanchor to wall time", () => {
  const f = fixture(); f.advance(6 * 60 * 60 * 1000, -60);
  assert.equal(f.now(), "2026-10-16T06:00:00.000Z");
});

test("large wall drift refuses both directions at the existing five-minute ceiling", () => {
  assert.equal(MAX_OBSERVATION_CLOCK_DRIFT_MS, 5 * 60 * 1000);
  for (const direction of [-1, 1]) {
    const f = fixture(); f.advance(10, direction * (MAX_OBSERVATION_CLOCK_DRIFT_MS + 1));
    assert.throws(f.now, /observation clock.*drift/i);
  }
});

test("invalid or regressing monotonic readings and invalid wall samples refuse", () => {
  for (const value of [NaN, Infinity, 99]) {
    const f = fixture(); f.setMono(value); assert.throws(f.now, /observation clock/i);
  }
  const f = fixture(); f.advance(10); f.now(); f.setMono(105);
  assert.throws(f.now, /observation clock/i);
  f.setWall(NaN); assert.throws(f.now, /observation clock/i);
});

test("unrepresentable UTC anchors refuse rather than emitting invalid dates", () => {
  for (const wall of [NaN, Infinity, 1e16]) {
    assert.throws(() => createObservationClock({ wallNowMs: () => wall, monotonicMs: () => 0 }), /observation clock/i);
  }
});


test("explicit cached observations and provider dates are never restamped", async () => {
  const { detailObservation } = await import("../../../lib/freshness.js");
  const observedAt = "2000-01-01T00:00:00Z";
  const receipt = detailObservation("cached", "source_revision_cache", observedAt, { validatedAt: "2001-01-01T00:00:00Z", sourceRevision: "1999-12-31" });
  assert.equal(receipt.observedAt, observedAt);
  assert.equal(receipt.validatedAt, "2001-01-01T00:00:00Z");
  assert.equal(receipt.sourceRevision, "1999-12-31");
});
