// lib/geo.ts - offline ZIP+lat/lng normalizers for the forward adapter path.
//
// Design contract (locked):
//   * Pure: no network, no file I/O at import time, no import-time side effects.
//     Do NOT import ./config.js (it parses argv at import time).
//   * NEVER throws: every input is guarded; null/garbage -> null.
//   * The authoritative ZIP->county/CBSA crosswalk LOOKUP runs in Python
//     (cre_geo.py) for scale. Adapters emit postalCode/latitude/longitude and
//     the ingest/backfill derives geo. This module exposes only pure normalizers
//     so lib/geo.ts stays import-safe and does not bundle the 3.5 MB CSV.
//
// Exported API (signatures are frozen per the Phase-2 Data-Lift Contract):
//
//   zip5(raw)            -> string | null  (5-digit normalized ZIP)
//   geoKey(lat, lng)     -> string | null  (stable 4-dp "lat,lng" key)

// ---------------------------------------------------------------------------
// zip5
// ---------------------------------------------------------------------------

/**
 * Normalize a 9-digit ZIP+4 or a 5-digit ZIP to a 5-digit string.
 * Returns null for any input that is not a recognizable US ZIP format.
 *
 * Accepted forms:
 *   "75201"       -> "75201"
 *   "75201-1234"  -> "75201"
 *   "752011234"   -> "75201"   (9-digit without hyphen)
 *   "  75201 "    -> "75201"   (whitespace trimmed)
 *
 * Rejected:
 *   null, "", "ABC", "1234", "123456" (6 digits, not 9 or 5)
 */
export function zip5(raw: string | null): string | null {
  if (typeof raw !== "string") return null;
  const s = raw.trim();
  if (!s) return null;
  // Strip hyphen if present (ZIP+4 form "NNNNN-NNNN").
  const digits = s.replace(/-/g, "");
  // Accept exactly 5 or 9 digits.
  if (!/^[0-9]{5}(?:[0-9]{4})?$/.test(digits)) return null;
  return digits.slice(0, 5);
}

// ---------------------------------------------------------------------------
// geoKey
// ---------------------------------------------------------------------------

/**
 * Round lat/lng to a stable key precision (4 decimal places) for crosswalk
 * matching. Returns a "lat,lng" string or null when either coordinate is
 * missing or out of range.
 *
 * 4 decimal places = ~11 m precision, sufficient for ZIP-centroid matching.
 * Values out of WGS-84 bounds (lat [-90,90], lng [-180,180]) are rejected.
 */
export function geoKey(lat: number | null, lng: number | null): string | null {
  if (lat === null || lat === undefined || lng === null || lng === undefined) return null;
  if (typeof lat !== "number" || typeof lng !== "number") return null;
  if (!isFinite(lat) || !isFinite(lng)) return null;
  if (lat < -90 || lat > 90) return null;
  if (lng < -180 || lng > 180) return null;
  return `${lat.toFixed(4)},${lng.toFixed(4)}`;
}
