import { performance } from "node:perf_hooks";

// Additional outer refusal ceiling, aligned with Python MAX_FUTURE_CLOCK_SKEW.
// Existing stricter source/generation/future checks remain authoritative.
export const MAX_OBSERVATION_CLOCK_DRIFT_MS = 5 * 60 * 1000;

type ClockDependencies = { wallNowMs?: () => number; monotonicMs?: () => number };

/** UTC observation stamps for this process only; never restamp provider/cache data. */
export function createObservationClock(
  dependencies: ClockDependencies = {},
): () => string {
  const wallNow = dependencies.wallNowMs ?? Date.now;
  const monotonicNow = dependencies.monotonicMs ?? performance.now.bind(performance);
  const anchorWall = wallNow();
  const anchorMonotonic = monotonicNow();
  if (
    !Number.isFinite(anchorWall) ||
    !Number.isFinite(new Date(anchorWall).getTime()) ||
    !Number.isFinite(anchorMonotonic) ||
    anchorMonotonic < 0
  ) {
    throw new Error("Observation clock anchor is invalid");
  }
  let previousMonotonic = anchorMonotonic;
  return () => {
    const monotonic = monotonicNow();
    const wall = wallNow();
    if (
      !Number.isFinite(monotonic) ||
      monotonic < previousMonotonic ||
      !Number.isFinite(wall)
    ) {
      throw new Error("Observation clock sample is invalid or regressed");
    }
    const observed = anchorWall + (monotonic - anchorMonotonic);
    const date = new Date(observed);
    if (!Number.isFinite(date.getTime()))
      throw new Error("Observation clock UTC value is invalid");
    const drift = wall - observed;
    if (Math.abs(drift) > MAX_OBSERVATION_CLOCK_DRIFT_MS) {
      throw new Error(
        `Observation clock wall drift exceeds ${MAX_OBSERVATION_CLOCK_DRIFT_MS}ms: drift_ms=${Math.round(drift)}`,
      );
    }
    previousMonotonic = monotonic;
    return date.toISOString();
  };
}

// Shared by full/targeted envelopes and every new source-owned observation.
// A step before this anchor or between processes remains subject to validators.
export const observationNow = createObservationClock();
