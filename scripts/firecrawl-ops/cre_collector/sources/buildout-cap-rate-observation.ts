/**
 * Parse a narrowly scoped Buildout inventory observation. This helper is not
 * wired to collection, persistence, or promotion. It does not decide whether
 * an observation is fresh, visible, or authoritative for a canonical listing.
 */
export type BuildoutCapRateObservation =
  | { kind: "missing" }
  | { kind: "invalid"; reason: "malformed_attributes" | "invalid_value"; literals: unknown[] }
  | { kind: "ambiguous"; literals: unknown[] }
  | { kind: "observed"; literal: string; fraction: number };

export function parseBuildoutCapRateObservation(
  attributes: unknown,
): BuildoutCapRateObservation {
  if (!Array.isArray(attributes)) {
    return { kind: "invalid", reason: "malformed_attributes", literals: [] };
  }

  const literals: unknown[] = [];
  for (const pair of attributes) {
    if (!Array.isArray(pair) || pair.length !== 2 || typeof pair[0] !== "string" || !pair[0].trim()) {
      return { kind: "invalid", reason: "malformed_attributes", literals };
    }
    // The observed feed label is exact. Similar labels may describe a
    // different cap-rate concept and must not be silently combined.
    if (pair[0] === "Cap Rate") literals.push(pair[1]);
  }

  if (literals.length === 0) return { kind: "missing" };
  if (literals.length > 1) return { kind: "ambiguous", literals };

  const literal = literals[0];
  if (typeof literal !== "string") {
    return { kind: "invalid", reason: "invalid_value", literals };
  }
  const match = /^\s*(\d+)(?:\.(\d{1,4}))?%\s*$/.exec(literal);
  if (!match) return { kind: "invalid", reason: "invalid_value", literals };
  const percent = Number(`${match[1]}.${match[2] ?? "0"}`);
  if (!Number.isFinite(percent) || percent <= 0 || percent >= 30) {
    return { kind: "invalid", reason: "invalid_value", literals };
  }
  // Four decimal places in percent translate exactly to at most six decimal
  // places in the normalized fraction. No percent-or-fraction heuristic.
  const fraction = Number((percent / 100).toFixed(6));
  if (fraction <= 0) return { kind: "invalid", reason: "invalid_value", literals };
  return { kind: "observed", literal, fraction };
}
