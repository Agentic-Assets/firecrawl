// lib/parse.ts - pure lease-rate / money / acres / building-class parsers.
//
// Design contract (locked):
//   * Pure: no network, no import-time side effects. Do NOT import ./config.js
//     (it parses argv at import time; tests must stay no-argv).
//   * NEVER throws: every input is guarded; null/garbage -> null/empty result.
//   * Semantics are IDENTICAL to the Python mirror in cre_parse.py; both are
//     verified against the shared golden test-vector table
//     (tests/fixtures/golden_parse_vectors.json).
//
// Exported API (signatures are frozen per the Phase-2 Data-Lift Contract):
//
//   parseLeaseRate(text)         -> LeaseRate
//   parseMoney(text)             -> number | null
//   acresToSf(text)              -> number | null
//   parseAmountIgnoringCurrencyLabel(text) -> number | null
//   parsePercentToFraction(text) -> number | null
//   normBuildingClass(text)      -> "A"|"B"|"C"|"D"|null
//   parseSizeText(text)          -> { sizeSf, lotSf }
//   isPerSfText(text)            -> boolean

// ---------------------------------------------------------------------------
// LeaseRate interface
// ---------------------------------------------------------------------------

export interface LeaseRate {
  /** $/SF/yr, annualized; null when not per-SF-trustable */
  min: number | null;
  /** Range high, else null */
  max: number | null;
  /** Lease basis type: "nnn" | "modified_gross" | "gross" | "full_service" | null */
  type: "nnn" | "modified_gross" | "gross" | "full_service" | null;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/** Coerce to string or return null. Never throws. */
function toStr(text: unknown): string | null {
  if (typeof text !== "string") return null;
  const t = text.trim();
  return t || null;
}

/** Round to a given number of decimal places to suppress floating-point drift. */
function round(v: number, dp = 2): number {
  const f = Math.pow(10, dp);
  return Math.round(v * f) / f;
}

// ---------------------------------------------------------------------------
// isPerSfText
// ---------------------------------------------------------------------------

/**
 * Guard: returns true when the free text is a PER-SF price.
 * Mirrors util.isPerSfPriceText; re-exported here for the Lee sale-price guard.
 */
export function isPerSfText(text: string | null): boolean {
  const s = toStr(text);
  if (!s) return false;
  // /SF, /SqFt, per square foot, PSF, per sq ft; also bare SF/yr, SF/MO, psf tokens.
  return /(?:\/|\bper\s+)\s*(?:s\.?f\.?|sq\.?\s*ft|square\s*feet)|\bpsf\b|\/sf\b|\bsf\/(?:yr|mo|month|year)|\bper\s+square\s+f/i.test(s);
}

// ---------------------------------------------------------------------------
// parseMoney
// ---------------------------------------------------------------------------

/**
 * Extract first "$N[,N][.N]" from text, strip commas, return as number.
 * Also handles bare numeric strings that start with a digit (no $ required)
 * when called internally from parseAmountIgnoringCurrencyLabel.
 * Returns null when no dollar amount is found.
 */
export function parseMoney(text: string | null): number | null {
  const s = toStr(text);
  if (!s) return null;
  // Match a $ followed by a number (commas as thousands separators OK).
  const m = s.replace(/,/g, "").match(/\$\s*([0-9]+(?:\.[0-9]+)?)/);
  if (!m) return null;
  const v = Number(m[1]);
  return isFinite(v) && v > 0 ? v : null;
}

/** Internal: parse a bare numeric string (no leading $). */
function parseNumericString(s: string): number | null {
  const clean = s.replace(/,/g, "");
  const m = clean.match(/([0-9]+(?:\.[0-9]+)?)/);
  if (!m) return null;
  const v = Number(m[1]);
  return isFinite(v) && v > 0 ? v : null;
}

// ---------------------------------------------------------------------------
// parseAmountIgnoringCurrencyLabel
// ---------------------------------------------------------------------------

/**
 * Amount where a non-USD currency LABEL is present but the value is really USD
 * (NAI 'POUND ' prefix). Strips any leading currency word/symbol, returns the
 * numeric. Used ONLY where the gap doc proves the label is wrong.
 */
export function parseAmountIgnoringCurrencyLabel(text: string | null): number | null {
  const s = toStr(text);
  if (!s) return null;
  // Strip a leading currency word/symbol (POUND, GBP, USD, EUR, $, £, €).
  const stripped = s.replace(/^\s*(?:POUND|GBP|USD|EUR|\$|£|€)\s*/i, "").trim();
  // Try parseMoney on the stripped remainder (handles the $-prefixed USD case).
  const fromMoney = parseMoney(stripped.startsWith("$") ? stripped : `$${stripped}`);
  if (fromMoney !== null) return fromMoney;
  // Fall back to parsing a bare numeric (the POUND case has no $ after strip).
  return parseNumericString(stripped);
}

// ---------------------------------------------------------------------------
// acresToSf
// ---------------------------------------------------------------------------

const ACRES_RE = /([0-9][0-9,]*(?:\.[0-9]+)?)\s*ac(?:res?)?\b/i;

/**
 * Acres -> SF (x 43560). Accepts "3.83 acres" / "3.83 ac" / bare number+unit.
 * Returns null when no acre measurement is found.
 */
export function acresToSf(text: string | null): number | null {
  const s = toStr(text);
  if (!s) return null;
  const m = s.replace(/,/g, "").match(/([0-9]+(?:\.[0-9]+)?)\s*ac(?:res?)?\b/i);
  if (!m) return null;
  const acres = Number(m[1]);
  if (!isFinite(acres) || acres <= 0) return null;
  return round(acres * 43560, 4);
}

// ---------------------------------------------------------------------------
// parsePercentToFraction
// ---------------------------------------------------------------------------

/**
 * Percent string -> fraction in (0, 1].
 * "87.5%" -> 0.875; "0.875" -> 0.875 (already a fraction).
 * Returns null for non-numeric / zero / out-of-range.
 */
export function parsePercentToFraction(text: string | null): number | null {
  const s = toStr(text);
  if (!s) return null;
  const m = s.replace(/,/g, "").match(/([0-9]+(?:\.[0-9]+)?)\s*(%?)/);
  if (!m) return null;
  const v = Number(m[1]);
  if (!isFinite(v) || v <= 0) return null;
  // If a % sign is present, divide by 100.
  if (m[2] === "%") return round(v / 100, 6);
  // No % sign: if value is > 1, treat as a percentage already (e.g. "87.5" -> 0.875).
  if (v > 1) return round(v / 100, 6);
  // Value in (0, 1]: already a fraction.
  return round(v, 6);
}

// ---------------------------------------------------------------------------
// normBuildingClass
// ---------------------------------------------------------------------------

/**
 * Normalize any cased "Class A" / "A" / "office.medical (B)" to 'A'|'B'|'C'|'D'|null.
 * Match order:
 *   1. Explicit "Class X" pattern (JLL buildingClass = "Class A").
 *   2. Bare trailing \b([A-D])\b ONLY when input is <= 2 tokens (avoid stray letters in prose).
 * Returns uppercase A/B/C/D or null.
 */
export function normBuildingClass(text: string | null): "A" | "B" | "C" | "D" | null {
  const s = toStr(text);
  if (!s) return null;
  // 1. Explicit "Class X" (case-insensitive).
  const classMatch = s.match(/\bclass\s+([A-Da-d])\b/i);
  if (classMatch) {
    return classMatch[1]!.toUpperCase() as "A" | "B" | "C" | "D";
  }
  // 2. Bare letter ONLY when the token count is <= 2 (e.g. "A" or "Class A").
  const tokens = s.trim().split(/\s+/);
  if (tokens.length <= 2) {
    const bareMatch = s.match(/\b([A-Da-d])\b/);
    if (bareMatch) {
      return bareMatch[1]!.toUpperCase() as "A" | "B" | "C" | "D";
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// parseSizeText
// ---------------------------------------------------------------------------

/**
 * Size text -> { sizeSf, lotSf }. Routes an "Acres" token to lotSf (x43560).
 * Examples:
 *   "12,500 SF on 2.0 Acres" -> { sizeSf: 12500, lotSf: 87120 }
 *   "5,000 SF"               -> { sizeSf: 5000, lotSf: null }
 *   "3.0 Acres"              -> { sizeSf: null, lotSf: 130680 }
 */
export function parseSizeText(text: string | null): { sizeSf: number | null; lotSf: number | null } {
  const s = toStr(text);
  if (!s) return { sizeSf: null, lotSf: null };

  // Extract lot size from an "acres" token first.
  const lotSf = acresToSf(s);

  // Extract SF value: look for a number followed by SF/SqFt/sq ft tokens,
  // OR a bare number that is NOT followed by an acre token.
  let sizeSf: number | null = null;

  // Remove commas for numeric matching.
  const cleaned = s.replace(/,/g, "");

  // Match an explicit SF/sq.ft token.
  const sfMatch = cleaned.match(/([0-9]+(?:\.[0-9]+)?)\s*(?:sf|sq\.?\s*ft|square\s*feet)\b/i);
  if (sfMatch) {
    const v = Number(sfMatch[1]);
    sizeSf = isFinite(v) && v > 0 ? v : null;
  } else if (!ACRES_RE.test(s)) {
    // No explicit unit: treat the first number as SF (only when no acres token).
    const numMatch = cleaned.match(/([0-9]+(?:\.[0-9]+)?)/);
    if (numMatch) {
      const v = Number(numMatch[1]);
      sizeSf = isFinite(v) && v > 0 ? v : null;
    }
  }

  return { sizeSf, lotSf };
}

import { rentEvidence } from "./rent-evidence.js";

/** USD/SF/year only when currency, denominator and period are explicit. */
export function parseLeaseRate(text: string | null): LeaseRate {
  const evidence = rentEvidence(text);
  return {min: evidence.annual_psf_min, max: evidence.annual_psf_max, type: evidence.lease_basis};
}
