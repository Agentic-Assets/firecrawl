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

// ---------------------------------------------------------------------------
// parseLeaseRate
// ---------------------------------------------------------------------------

// Detect a per-SF signal in lease rate text. Broader than isPerSfText to also
// catch "USD/SF/MO", "SF/yr", bare "PSF", etc.
function hasPerSfSignal(s: string): boolean {
  return /(?:\/|\bper\s+)\s*(?:s\.?f\.?|sq\.?\s*ft|square\s*f(?:eet|oot))|\bpsf\b|\bsf\/(?:yr|mo|month|year)|\busd\/sf\b|\bper\s+square\s+f/i.test(s);
}

// Detect a negative signal: text claims to be annual/total but NOT explicitly per-SF.
// "(Annual)" or "USD" alone (without a /SF qualifier) indicates an ambiguous or
// absolute price rather than a per-SF rate.
function hasNegativeSignal(s: string): boolean {
  // "(Annual)" without an SF qualifier indicates an absolute annual total, not per-SF.
  return /\(\s*annual\s*\)/i.test(s);
}

// Detect a per-month signal (annualize x12).
function hasPerMonthSignal(s: string): boolean {
  return /\/(?:mo|month)\b|\bsf\/mo\b|\busd\/sf\/mo\b/i.test(s);
}

// Derive the lease basis type from trailing keywords. Order: most-specific first.
function deriveLeaseBasisType(s: string): LeaseRate["type"] {
  const l = s.toLowerCase();
  if (/modified\s+gross|mod\s+gross/.test(l)) return "modified_gross";
  if (/full\s+service|\bfsg\b/.test(l)) return "full_service";
  if (/nnn|triple\s+net/.test(l)) return "nnn";
  // "IG" (industrial gross) or bare "Gross" but NOT "Modified Gross" (already caught).
  if (/\bgross\b|\big\b/.test(l)) return "gross";
  return null;
}

/**
 * Parse a range of up to two lease-rate numbers from the cleaned (no-comma) text.
 * Handles mixed $-prefixed and bare forms: "$1.59 - 1.70", "$35 - 45", "$22 - $26".
 * Returns [min, max?] where max is present only when the text contains a clear range
 * and max > min (so "$19 SF/yr ($10.00/SF NNN)" does not produce a range since
 * 10.00 < 19).
 */
function extractLeaseAmounts(s: string): [number, number | null] | null {
  const cleaned = s.replace(/,/g, "");

  // Strategy 1: Range with explicit "$" on first and optional "$" on second.
  // Pattern: $N(.N)? (separator) N(.N)? where separator is " - " or " to ".
  const rangeWithDollar = cleaned.match(
    /\$\s*([0-9]+(?:\.[0-9]+)?)\s*[-–to]+\s*\$?\s*([0-9]+(?:\.[0-9]+)?)/
  );
  if (rangeWithDollar) {
    const lo = Number(rangeWithDollar[1]);
    const hi = Number(rangeWithDollar[2]);
    if (isFinite(lo) && isFinite(hi) && lo >= 0 && hi >= 0) {
      // Only treat as a range when hi > lo (avoids the dual-value "19 vs 10" case).
      if (hi > lo) return [lo, hi];
      // hi <= lo: take only lo as the min, discard hi.
      return [lo, null];
    }
  }

  // Strategy 2: Range with no "$": "35 - 45 SF/yr", "3.59 USD/SF/MO".
  // First look for a bare range before the SF/type tokens.
  const bareRange = cleaned.match(/([0-9]+(?:\.[0-9]+)?)\s*[-–]+\s*([0-9]+(?:\.[0-9]+)?)/);
  if (bareRange) {
    const lo = Number(bareRange[1]);
    const hi = Number(bareRange[2]);
    if (isFinite(lo) && isFinite(hi) && lo >= 0 && hi >= 0) {
      if (hi > lo) return [lo, hi];
      return [lo, null];
    }
  }

  // Strategy 3: Single value with "$".
  const singleDollar = cleaned.match(/\$\s*([0-9]+(?:\.[0-9]+)?)/);
  if (singleDollar) {
    const v = Number(singleDollar[1]);
    if (isFinite(v) && v >= 0) return [v, null];
  }

  // Strategy 4: Single bare number (for "3.59 USD/SF/MO" with no $).
  const singleBare = cleaned.match(/([0-9]+(?:\.[0-9]+)?)/);
  if (singleBare) {
    const v = Number(singleBare[1]);
    if (isFinite(v) && v >= 0) return [v, null];
  }

  return null;
}

/**
 * Lease-rate parse: returns annualized $/SF/yr min/max + normalized basis type.
 *
 * Semantics (locked, per the Phase-2 Data-Lift Contract):
 * - A bare "$N.NN" with no other tokens is trusted as-is (caller labeled it lease rate).
 * - Trust a value when the text has a positive per-SF signal (PSF, /SF, SF/yr, etc.).
 * - Reject when the text has a negative signal ("(Annual)" without SF qualifier).
 * - Annualize a per-month value (/mo, month) by x12.
 * - Reject a value > 500 $/SF/yr (implausible; catches the AY $5000/SF/YR anomaly).
 * - For a money RANGE, reject when max > 100 && min < 100 (suite-size mis-range,
 *   the Buildout case). A range is only recognized when max > min.
 * - type from the trailing token (modified_gross, full_service, nnn, gross, null).
 */
export function parseLeaseRate(text: string | null): LeaseRate {
  const NULL_RESULT: LeaseRate = { min: null, max: null, type: null };

  const s = toStr(text);
  if (!s) return NULL_RESULT;

  const type = deriveLeaseBasisType(s);

  // Check for negative signal (absolute/total, not per-SF).
  if (hasNegativeSignal(s)) return { ...NULL_RESULT, type };

  const isPerSf = hasPerSfSignal(s);
  const isMonthly = hasPerMonthSignal(s);

  // A bare "$N.NN" with no other qualifiers (besides a possible type-word like "NNN")
  // is trusted as-is: the adapter already labeled this field as a lease rate.
  // We detect a bare amount by checking no explicit non-SF qualifier token is present
  // and the string is a very short "$N.NN" form.
  const isBareAmount = !isPerSf && !hasNegativeSignal(s) && /^\$\s*[0-9]+(?:\.[0-9]+)?(?:\s+(?:nnn|gross|modified\s+gross|full\s+service|fsg|triple\s+net))?\s*$/i.test(s.trim());

  if (!isPerSf && !isBareAmount) {
    // No per-SF signal and not a bare amount: not trustable.
    return { ...NULL_RESULT, type };
  }

  const extracted = extractLeaseAmounts(s);
  if (!extracted) return { ...NULL_RESULT, type };

  const [rawMin, rawMax] = extracted;

  // Annualize if monthly.
  const annMin = isMonthly ? round(rawMin * 12, 2) : rawMin;
  const annMax = rawMax !== null ? (isMonthly ? round(rawMax * 12, 2) : rawMax) : null;

  // Reject suite-size mis-range FIRST (before the 500 cap), using raw annualized values.
  // When max > 100 && min < 100, the range is a suite-size masquerading as a money range
  // (the Buildout case: "$2.50 - 250 SF/month" -> annMin=30, annMax=3000).
  if (annMax !== null && annMax > 100 && annMin < 100) {
    return { ...NULL_RESULT, type };
  }

  // Reject implausible values > 500 $/SF/yr (AY anomaly guard).
  if (annMin > 500) return { ...NULL_RESULT, type };

  // Cap max at 500 (drop a high range high-end, keep min).
  const finalMax = annMax !== null && annMax > 500 ? null : annMax;

  return { min: annMin, max: finalMax, type };
}
