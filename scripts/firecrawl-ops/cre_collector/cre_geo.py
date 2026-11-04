#!/usr/bin/env python3
"""cre_geo.py: offline ZIP->county+CBSA crosswalk resolver (Phase-2 data-lift, Section C.4/E).

Single source of truth in Python for geo-derivation. Used by cre_geo_backfill.py
(the existing-row backfill pass) and by cre_ingest.py on the forward path, so
every new or updated listing row carries county/CBSA/geo_source.
The TS side (lib/geo.ts) exposes only pure normalizers (zip5, geoKey); the full
crosswalk lookup runs here because the backfill and ingest both run in Python.

Public surface (contract C.4):
    class ZipCbsaCrosswalk:
        __init__(csv_path=None)   # default: data/zip_cbsa_crosswalk.csv beside this file
        by_zip(zip5)              # -> {county, cbsa_code, cbsa_name} | None
        by_latlng(lat, lng)       # -> nearest-centroid {county, cbsa_code, cbsa_name} | None
        coordinate_state_conflict(lat, lng, state, postal=None)  # -> other state | None

    def derive_geo(listing_or_row, crosswalk):
        # -> (county, cbsa_code, cbsa_name, submarket, geo_source) | all-None tuple
    def is_placeholder_point(lat, lng)       # provider default "no location" pin
    def canonical_source_county(src_county, listing_state, zip_rec)

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

import bisect
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
MAX_LATLNG_KM = 50.0

# Provider default pins that stand for "no location", not a place. The RCM
# platform (Colliers SalesTracker, my.rcm1.com, CBRE Dealflow) publishes
# 40.054, -98.237 (rural Nebraska) for listings in many states.
PLACEHOLDER_POINTS = frozenset({(40.054, -98.237)})

# Earth radius in km (WGS-84 mean).
_EARTH_KM = 6371.0

# Kilometres per degree of latitude. Any point within max_km lies inside a
# latitude band of max_km / _KM_PER_LAT_DEG degrees, which bounds the scan.
_KM_PER_LAT_DEG = math.pi * _EARTH_KM / 180.0

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

    The committed file is 33,791 rows (2.3 MB) and loads in well under a
    second; use it as a module-level singleton or a script-scoped instance.

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
        self._states = set()
        self._load(path)
        # Stable sort by latitude so by_latlng can bisect to a latitude band
        # instead of scanning every centroid (forward ingest calls it per row).
        self._centroids.sort(key=lambda c: c[0])
        self._centroid_lats = [c[0] for c in self._centroids]

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
                        "county_name": (row.get("county_name") or "").strip() or None,
                        "state": (row.get("state") or "").strip().upper() or None,
                        "cbsa_code": (row.get("cbsa_code") or "").strip() or None,
                        "cbsa_name": (row.get("cbsa_name") or "").strip() or None,
                    }
                    # Primary index by zip5
                    self._by_zip[z] = rec
                    if rec["state"]:
                        self._states.add(rec["state"])
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

    def _centroids_near(self, lat, lng, max_km):
        """Yield (km, rec) for every centroid within max_km of a valid point.

        Only centroids inside the latitude band that can lie within max_km are
        measured (bisect on the latitude-sorted list), so a lookup touches a
        small slice of the ~33k centroids.
        """
        if lat is None or lng is None:
            return
        try:
            lat_f, lng_f = float(lat), float(lng)
        except (TypeError, ValueError):
            return
        if not (math.isfinite(lat_f) and math.isfinite(lng_f)):
            return
        band = max_km / _KM_PER_LAT_DEG
        lo = bisect.bisect_left(self._centroid_lats, lat_f - band)
        hi = bisect.bisect_right(self._centroid_lats, lat_f + band)
        for c_lat, c_lng, rec in self._centroids[lo:hi]:
            km = _haversine_km(lat_f, lng_f, c_lat, c_lng)
            if km < max_km:
                yield km, rec

    def by_latlng(self, lat, lng, max_km=MAX_LATLNG_KM):
        """Return the nearest-centroid record when within max_km, else None.

        Both the backfill and the forward ingest call it only when by_zip misses.
        """
        best_km = max_km
        best_rec = None
        for km, rec in self._centroids_near(lat, lng, max_km):
            if km < best_km:
                best_km = km
                best_rec = rec
        return best_rec

    def coordinate_state_conflict(self, lat, lng, state, postal=None, max_km=MAX_LATLNG_KM):
        """Return the state a point lies in when it contradicts `state`, else None.

        A source can publish coordinates that belong to another listing (the
        pre-July-2026 Colliers SalesTracker pairing put a Saint Paul, MN card in
        Miami). Rejection needs positive evidence: the nearest ZIP centroid
        within max_km is in another state AND no centroid of `state` lies within
        max_km, so a correct point near a state line is kept. A point far from
        every centroid, or a state the crosswalk does not cover (non-US), gives
        no evidence and returns None. When the row's ZIP resolves to the point's
        state, the listing's state field is the suspect value, not the
        coordinates, so they are kept as well.
        """
        st = state.strip().upper() if isinstance(state, str) else None
        if not st or st not in self._states:
            return None
        nearest_km = max_km
        nearest_state = None
        for km, rec in self._centroids_near(lat, lng, max_km):
            if rec.get("state") == st:
                return None
            if km < nearest_km:
                nearest_km = km
                nearest_state = rec.get("state")
        if not nearest_state:
            return None
        zip_rec = self.by_zip(postal) if isinstance(postal, str) else None
        if zip_rec and zip_rec.get("state") == nearest_state:
            return None
        return nearest_state

    def __len__(self):
        return len(self._by_zip)


def is_placeholder_point(lat, lng):
    """True when (lat, lng) is a known provider default pin (4-decimal match)."""
    try:
        point = (round(float(lat), 4), round(float(lng), 4))
    except (TypeError, ValueError):
        return False
    return point in PLACEHOLDER_POINTS


def _county_label(row):
    """Build a human-readable county label: 'Cook County, IL' (or bare name)."""
    name = (row.get("county_name") or "").strip()
    state = (row.get("state") or "").strip()
    if name and state:
        return f"{name}, {state}"
    return name or None


# Trailing county-equivalent words dropped before comparing a source county to
# the crosswalk name ("Los Angeles" vs "Los Angeles County").
_COUNTY_SUFFIX_RE = re.compile(r"\s+(county|parish|borough)$", re.IGNORECASE)


def _county_key(name):
    return _COUNTY_SUFFIX_RE.sub("", " ".join(name.split())).lower()


def canonical_source_county(src_county, listing_state, zip_rec):
    """Return the canonical "<county_name>, <ST>" for a source-verbatim county.

    Sources publish bare names ("Los Angeles", "King", "Polk") while the
    crosswalk and every EQUIRE county filter use "Los Angeles County, CA". The
    source value is canonicalized only when the row's ZIP resolves in the
    crosswalk to the listing's own state and the crosswalk county name starts
    with the source county as whole words, ignoring a trailing County, Parish or
    Borough and case. Anything else, including a ZIP in another county, keeps
    the source value verbatim.
    """
    if not isinstance(src_county, str):
        return src_county
    st = listing_state.strip().upper() if isinstance(listing_state, str) else None
    if not (st and zip_rec and zip_rec.get("state") == st and zip_rec.get("county_name")):
        return src_county
    source = " ".join(src_county.split())
    suffix = f", {st}"
    if source.upper().endswith(suffix):
        source = source[: -len(suffix)]
    source_key = _county_key(source)
    name = zip_rec["county_name"]
    name_key = _county_key(name)
    if source_key and (name_key == source_key or name.lower().startswith(source_key + " ")):
        return f"{name}, {st}"
    return src_county


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
         - county: canonical "<county_name>, <ST>" when the ZIP confirms it
           (canonical_source_county), else kept as-is.
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
    listing_state = _get("state")
    if isinstance(listing_state, str):
        listing_state = listing_state.upper()

    def _latlng_record(lat, lng):
        # A provider default pin is not a location; derive nothing from it.
        if is_placeholder_point(lat, lng):
            return None
        rec = crosswalk.by_latlng(lat, lng) if crosswalk is not None else None
        # A nearest ZIP centroid can cross a dense metro boundary. If the broker
        # supplied a state, fail closed rather than assigning another state's CBSA.
        if rec and listing_state and rec.get("state") and rec["state"] != listing_state:
            return None
        return rec

    # ---- 1. Source-verbatim ----
    if src_county:
        # Source gave a county; this is the authoritative geo bundle.
        postal = _get("postal_code", "postalCode")
        zip_rec = crosswalk.by_zip(postal) if (postal and crosswalk is not None) else None
        county = canonical_source_county(src_county, listing_state, zip_rec)
        cbsa_code = _get("cbsa_code")
        cbsa_name = _get("cbsa_name")
        # Fill in CBSA from the crosswalk when source omitted it but gave a ZIP.
        if (not cbsa_code) and crosswalk is not None:
            cw_rec = zip_rec
            if cw_rec is None:
                lat = _get("latitude")
                lng = _get("longitude")
                cw_rec = _latlng_record(lat, lng) if (lat and lng) else None
            if cw_rec:
                cbsa_code = cw_rec.get("cbsa_code")
                cbsa_name = cw_rec.get("cbsa_name")
        # market/submarket are source-authoritative (not geo-derived here); the
        # backfill SQL writes only the county/CBSA/geo_source columns.
        return county, cbsa_code, cbsa_name, src_submarket, "source"

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
        rec = _latlng_record(lat, lng)
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
