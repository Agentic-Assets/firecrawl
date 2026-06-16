#!/usr/bin/env python3
"""cre_geo.py: offline ZIP->county+CBSA crosswalk resolver (Phase-2 data-lift, Section C.4/E).

Single source of truth in Python for geo-derivation. Used by cre_geo_backfill.py
(the 87k-row backfill pass) and optionally by cre_ingest.py on the forward path.
The TS side (lib/geo.ts) exposes only pure normalizers (zip5, geoKey); the full
crosswalk lookup runs here because the backfill and ingest both run in Python.

Public surface (contract C.4):
    class ZipCbsaCrosswalk:
        __init__(csv_path=None)   # default: data/zip_cbsa_crosswalk.csv beside this file
        by_zip(zip5)              # -> {county, cbsa_code, cbsa_name} | None
        by_latlng(lat, lng)       # -> nearest-centroid {county, cbsa_code, cbsa_name} | None

    def derive_geo(listing_or_row, crosswalk):
        # -> (county, cbsa_code, cbsa_name, submarket, geo_source) | all-None tuple

Precedence in derive_geo:
    1. Source-verbatim county / market / submarket (Newmark gives all three).
       geo_source = 'source'. COALESCE-keep: if the source gave a value, keep it.
    2. ZIP crosswalk  (postalCode present) -> geo_source = 'crosswalk_zip'.
    3. Lat/lng nearest-centroid (lat + lng present) -> geo_source = 'crosswalk_latlng'.
    Submarket is source-only; we never fabricate a submarket from CBSA data.
    Market column: set to cbsa_name when the source gave no market value (COALESCE-keep).

Python stdlib only. Pure functions (ZipCbsaCrosswalk is load-once, O(1) lookups).
No network. Import-safe (no side effects at import time beyond the class definition).
"""

import csv
import math
import os
import re

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default crosswalk CSV: beside this file in data/
_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CSV = os.path.join(_HERE, "data", "zip_cbsa_crosswalk.csv")

# Maximum great-circle distance (km) for a lat/lng centroid match.  A listing
# whose nearest ZIP centroid is farther than this is left unmatched rather than
# receiving a stale / wrong county.  The value (50 km ~ 31 miles) covers
# suburban areas; a rural property in a very sparse ZIP grid might miss, which
# is the safer failure mode.
_MAX_LATLNG_KM = 50.0

# Earth radius in km (WGS-84 mean).
_EARTH_KM = 6371.0

# ZIP-5 normalizer: strip leading/trailing whitespace, zero-pad to 5 digits.
_ZIP_CLEAN_RE = re.compile(r"[^0-9]")


def _normalize_zip(raw):
    """Normalize a 9-digit, ZIP+4, or already-5-digit ZIP to a bare 5-char
    string (left-zero-padded), or None when the input is not zip-like."""
    if not isinstance(raw, str):
        return None
    digits = _ZIP_CLEAN_RE.sub("", raw)
    if len(digits) < 5:
        return None
    return digits[:5].zfill(5)


def _haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance in km between two WGS-84 coordinate pairs."""
    r = _EARTH_KM
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# ZipCbsaCrosswalk
# ---------------------------------------------------------------------------


class ZipCbsaCrosswalk:
    """Loads the committed offline ZIP->county+CBSA CSV once into memory.

    Memory footprint for the full ~41k-row file is ~15 MB; acceptable as a
    module-level singleton or a script-scoped instance.

    Columns expected in the CSV (see data/README.md):
        zip5, county_fips, county_name, state, cbsa_code, cbsa_name,
        centroid_lat, centroid_lng
    """

    def __init__(self, csv_path=None):
        path = csv_path or _DEFAULT_CSV
        # _by_zip: zip5 str -> record dict
        # _centroids: list of (lat, lng, record) for nearest-centroid search
        self._by_zip = {}
        self._centroids = []
        self._load(path)

    def _load(self, path):
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    z = (row.get("zip5") or "").strip()
                    if not z:
                        continue
                    rec = {
                        "county": _county_label(row),
                        "cbsa_code": (row.get("cbsa_code") or "").strip() or None,
                        "cbsa_name": (row.get("cbsa_name") or "").strip() or None,
                    }
                    # Primary index by zip5
                    self._by_zip[z] = rec
                    # Centroid index (only when lat/lng are valid floats)
                    try:
                        lat = float(row.get("centroid_lat") or "")
                        lng = float(row.get("centroid_lng") or "")
                        self._centroids.append((lat, lng, rec))
                    except (TypeError, ValueError):
                        pass
        except FileNotFoundError:
            # Crosswalk CSV absent (full dataset not yet built).
            # ZipCbsaCrosswalk still loads; all lookups return None.
            pass

    def by_zip(self, zip5_raw):
        """Look up by ZIP code.  Returns {county, cbsa_code, cbsa_name} or None."""
        z = _normalize_zip(zip5_raw) if isinstance(zip5_raw, str) else None
        if not z:
            return None
        return self._by_zip.get(z)

    def by_latlng(self, lat, lng, max_km=_MAX_LATLNG_KM):
        """Return the nearest-centroid record when within max_km, else None.

        Scans the centroid list linearly (O(n) on ~41k rows).  On a modern CPU
        this is <10 ms per lookup; the backfill calls it only when by_zip misses,
        so total overhead is acceptable.  A spatial index would be needed only
        if the call rate approaches thousands per second.
        """
        if lat is None or lng is None:
            return None
        try:
            lat_f, lng_f = float(lat), float(lng)
        except (TypeError, ValueError):
            return None

        best_km = max_km
        best_rec = None
        for c_lat, c_lng, rec in self._centroids:
            km = _haversine_km(lat_f, lng_f, c_lat, c_lng)
            if km < best_km:
                best_km = km
                best_rec = rec
        return best_rec

    def __len__(self):
        return len(self._by_zip)


def _county_label(row):
    """Build a human-readable county label: 'Cook County, IL' (or bare name)."""
    name = (row.get("county_name") or "").strip()
    state = (row.get("state") or "").strip()
    if name and state:
        return f"{name}, {state}"
    return name or None


# ---------------------------------------------------------------------------
# derive_geo
# ---------------------------------------------------------------------------


def derive_geo(listing_or_row, crosswalk):
    """Derive (county, cbsa_code, cbsa_name, submarket, geo_source) for a row.

    listing_or_row: a dict representing either:
      - a raw cre_listings DB row with snake_case keys
        (county, market, submarket, postal_code / zip5, latitude, longitude)
      - a collector listing object with camelCase keys
        (county, market, submarket, postalCode, latitude, longitude)
    Both key styles are accepted; snake_case wins on collision.

    Precedence (contract Section C.4 / Section E submarket fallback rule):
      1. Source-verbatim: if the row already has a non-null county, treat the
         whole geo bundle as source-provided (geo_source='source').
         - county: kept as-is.
         - cbsa_code/cbsa_name: kept as-is when present; derived from the
           crosswalk when absent (source gave county but not CBSA).
         - submarket: kept as-is (source-only; never fabricated).
         - market: kept as-is when present; set to cbsa_name otherwise
           (COALESCE-keep).
      2. ZIP crosswalk: when source county is absent but postalCode is present.
         geo_source='crosswalk_zip'. submarket stays None.
      3. Lat/lng: when source county and postalCode are both absent but lat+lng
         are present. geo_source='crosswalk_latlng'. submarket stays None.

    Returns:
        (county, cbsa_code, cbsa_name, submarket, geo_source)
    All elements may be None when geo derivation is not possible.
    """
    row = listing_or_row if isinstance(listing_or_row, dict) else {}

    # Normalised field accessor: accepts snake_case then camelCase.
    def _get(snake, camel=None):
        v = row.get(snake)
        if v is None and camel:
            v = row.get(camel)
        if isinstance(v, str):
            v = v.strip() or None
        return v

    src_county = _get("county")
    src_submarket = _get("submarket")

    # ---- 1. Source-verbatim ----
    if src_county:
        # Source gave a county; this is the authoritative geo bundle.
        cbsa_code = _get("cbsa_code")
        cbsa_name = _get("cbsa_name")
        # Fill in CBSA from the crosswalk when source omitted it but gave a ZIP.
        if (not cbsa_code) and crosswalk is not None:
            postal = _get("postal_code", "postalCode")
            cw_rec = crosswalk.by_zip(postal) if postal else None
            if cw_rec is None:
                lat = _get("latitude")
                lng = _get("longitude")
                cw_rec = crosswalk.by_latlng(lat, lng) if (lat and lng) else None
            if cw_rec:
                cbsa_code = cw_rec.get("cbsa_code")
                cbsa_name = cw_rec.get("cbsa_name")
        # market/submarket are source-authoritative (not geo-derived here); the
        # backfill SQL writes only the county/CBSA/geo_source columns.
        return src_county, cbsa_code, cbsa_name, src_submarket, "source"

    # ---- 2. ZIP crosswalk ----
    postal = _get("postal_code", "postalCode")
    if postal and crosswalk is not None:
        rec = crosswalk.by_zip(postal)
        if rec:
            cbsa_name = rec.get("cbsa_name")
            return (
                rec.get("county"),
                rec.get("cbsa_code"),
                cbsa_name,
                None,  # submarket never fabricated
                "crosswalk_zip",
            )

    # ---- 3. Lat/lng centroid ----
    lat = _get("latitude")
    lng = _get("longitude")
    if (lat is not None) and (lng is not None) and crosswalk is not None:
        rec = crosswalk.by_latlng(lat, lng)
        if rec:
            cbsa_name = rec.get("cbsa_name")
            return (
                rec.get("county"),
                rec.get("cbsa_code"),
                cbsa_name,
                None,  # submarket never fabricated
                "crosswalk_latlng",
            )

    # No geo info available.
    return None, None, None, None, None
