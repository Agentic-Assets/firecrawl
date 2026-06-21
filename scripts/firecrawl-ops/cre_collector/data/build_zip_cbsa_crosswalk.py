#!/usr/bin/env python3
"""build_zip_cbsa_crosswalk.py: offline builder for data/zip_cbsa_crosswalk.csv.

Builds a one-row-per-ZIP crosswalk from THREE US Census Bureau public-domain
datasets (no API token, no registration, no license/attribution required) and
emits a deterministic CSV consumed by cre_geo.py and sql/014_cre_geo_crosswalk.sql.

WHY ALL-CENSUS (and not HUD)
----------------------------
The original design fetched the HUD USPS ZIP-COUNTY crosswalk for the
ZIP -> county FIPS mapping.  HUD's direct portal download is now behind an
Akamai bot-management gate (returns HTTP 202 with an empty body to scripted
clients) and its hudapi path requires a per-user access token.  Neither is
reproducible in an unattended build.  The US Census Bureau publishes an
equivalent, token-free, fully public-domain ZIP(ZCTA)->county relationship file
on www2.census.gov, so this builder uses an all-Census pipeline instead.  ZCTA5
("ZIP Code Tabulation Area") is the Census analogue of a USPS ZIP code; it is
the standard public substitute and matches >99% of mailable ZIPs.

DATASETS (all public domain, no attribution required)
-----------------------------------------------------
  1. ZCTA5 <-> County relationship file (2020 geographies)
     tab20_zcta520_county20_natl.txt  (pipe-delimited)
     -> ZIP(ZCTA5) -> county FIPS, county name, and AREALAND_PART (the land
        area of the ZCTA that falls inside that county).  Multi-county ZIPs are
        deduped by MAX(AREALAND_PART): the county holding the largest share of
        the ZCTA's land wins.  (This replaces HUD's RES_RATIO dedup key with a
        defensible land-area dominance rule.)
  2. CBSA Delineation File, September 2023 (OMB Bulletin 23-01)
     list1_2023.xlsx
     -> county FIPS -> CBSA code + CBSA title (metro/micro areas).  Counties
        not in any CBSA simply have no entry; those ZIPs get empty cbsa fields.
  3. National ZCTA Gazetteer, 2023
     2023_Gaz_zcta_national.zip  (tab-delimited .txt inside)
     -> ZIP(ZCTA5) -> centroid lat/lng (INTPTLAT / INTPTLONG).

VINTAGES TARGETED (update the URLs below + this header when re-running):
  ZCTA<->County relationship:  2020 geographies (rel2020)
  CBSA delineation:            September 2023 (list1_2023)
  ZCTA gazetteer centroids:    2023

OUTPUT COLUMNS (data/zip_cbsa_crosswalk.csv) -- consumed verbatim by cre_geo.py:
  zip5         TEXT    5-digit ZIP/ZCTA (left-zero-padded)
  county_fips  TEXT    5-digit county FIPS (state FIPS 2-char + county 3-char)
  county_name  TEXT    bare county name, e.g. 'Cook County' (cre_geo joins with state)
  state        TEXT    2-char USPS state abbreviation (derived from FIPS prefix)
  cbsa_code    TEXT    5-digit CBSA code, or '' for non-metro counties
  cbsa_name    TEXT    CBSA title, e.g. 'Chicago-Naperville-Elgin, IL-IN-WI', or ''
  centroid_lat FLOAT   ZCTA centroid latitude (4 dp), or '' when unknown
  centroid_lng FLOAT   ZCTA centroid longitude (4 dp), or '' when unknown

DESIGN NOTES:
  - One row per ZIP.  Multi-county ZIPs keep the max-AREALAND_PART county.
  - Non-metro ZIPs keep cbsa_code='' and cbsa_name='' (empty string, not NULL;
    the CSV format has no NULL concept and '' is unambiguous to the loader).
  - state is derived from the county FIPS prefix via a static FIPS->USPS table
    (stable; avoids a fourth download).
  - Output is sorted by zip5 for byte-deterministic diffs.

NETWORK REQUIREMENT:
  Three HTTPS GETs against www2.census.gov.  If run without network access the
  script exits early (rc 0), prints a clear notice, and leaves the existing
  committed seed file intact, so cre_geo.py keeps working on the seed subset.

USAGE:
  cd scripts/firecrawl-ops/cre_collector
  python3 data/build_zip_cbsa_crosswalk.py            # writes data/zip_cbsa_crosswalk.csv
  python3 data/build_zip_cbsa_crosswalk.py --dry-run  # fetch+parse, print counts only
  python3 data/build_zip_cbsa_crosswalk.py --skip-gazetteer  # no centroids (faster)

  Idempotent: same vintages -> byte-identical output (sorted, header included).

SEE ALSO:
  data/README.md                 -- dataset provenance and usage
  cre_geo.py                     -- Python crosswalk loader (reads this CSV)
  sql/014_cre_geo_crosswalk.sql  -- DB loader (\\copy from this file)
"""

import argparse
import csv
import io
import os
import sys
import urllib.request
import zipfile

# ---------------------------------------------------------------------------
# Dataset URLs (public-domain US Census Bureau files; no token required)
# ---------------------------------------------------------------------------

# 1. ZCTA5 <-> County relationship file (2020 geographies). Pipe-delimited.
#    Columns include GEOID_ZCTA5_20, GEOID_COUNTY_20, NAMELSAD_COUNTY_20,
#    AREALAND_PART (land area of the ZCTA part within that county).
CENSUS_ZCTA_COUNTY_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/"
    "tab20_zcta520_county20_natl.txt"
)

# 2. CBSA Delineation File, September 2023 (OMB Bulletin 23-01). XLSX.
#    Header is on the 3rd row; FIPS State Code (col 9) + FIPS County Code
#    (col 10) -> CBSA Code (col 0) + CBSA Title (col 3).
CENSUS_CBSA_URL = (
    "https://www2.census.gov/programs-surveys/metro-micro/geographies/"
    "reference-files/2023/delineation-files/list1_2023.xlsx"
)

# 3. National ZCTA Gazetteer, 2023 (centroids). ZIP containing a tab-delimited
#    .txt with GEOID, INTPTLAT, INTPTLONG.
CENSUS_ZCTA_GAZ_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/"
    "2023_Gaz_zcta_national.zip"
)

# ---------------------------------------------------------------------------
# Static FIPS state code -> USPS abbreviation (50 states + DC + territories).
# Stable reference data; avoids a fourth download just for the state column.
# ---------------------------------------------------------------------------

_STATE_FIPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO",
    "09": "CT", "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI",
    "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY", "60": "AS", "66": "GU", "69": "MP",
    "72": "PR", "74": "UM", "78": "VI",
}

# ---------------------------------------------------------------------------
# Output column order (consumed verbatim by cre_geo.ZipCbsaCrosswalk._load).
# ---------------------------------------------------------------------------

_OUT_COLS = [
    "zip5",
    "county_fips",
    "county_name",
    "state",
    "cbsa_code",
    "cbsa_name",
    "centroid_lat",
    "centroid_lng",
]


# ---------------------------------------------------------------------------
# Network fetch helpers
# ---------------------------------------------------------------------------


def _fetch_bytes(url, timeout=120):
    """Download url -> bytes.  Returns (bytes, None) or (None, error_str)."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "cre_geo_builder/2.0 (public-domain census data fetch)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), None
    except Exception as exc:
        return None, str(exc)


def _check_network():
    """Quick connectivity check against the Census host used for all fetches."""
    data, err = _fetch_bytes(
        "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/", timeout=15
    )
    return err is None


# ---------------------------------------------------------------------------
# 1. ZCTA <-> County relationship parser
# ---------------------------------------------------------------------------


def parse_zcta_county(raw_bytes):
    """Parse the pipe-delimited ZCTA<->county relationship file.

    Returns (records, None) or (None, error). Each record:
      {zip5, county_fips, county_name, arealand_part}
    Rows with a blank ZCTA (county-only area rows) are skipped.
    """
    try:
        text = raw_bytes.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text), delimiter="|")
        records = []
        for row in reader:
            zip5 = (row.get("GEOID_ZCTA5_20") or "").strip()
            if len(zip5) != 5 or not zip5.isdigit():
                continue
            county_fips = (row.get("GEOID_COUNTY_20") or "").strip().zfill(5)
            if len(county_fips) != 5 or not county_fips.isdigit():
                continue
            county_name = (row.get("NAMELSAD_COUNTY_20") or "").strip()
            try:
                arealand_part = int(row.get("AREALAND_PART") or 0)
            except (TypeError, ValueError):
                arealand_part = 0
            records.append({
                "zip5": zip5,
                "county_fips": county_fips,
                "county_name": county_name,
                "arealand_part": arealand_part,
            })
        return records, None
    except Exception as exc:
        return None, f"ZCTA-county parse failed: {exc}"


# ---------------------------------------------------------------------------
# 2. Census CBSA delineation parser (XLSX)
# ---------------------------------------------------------------------------


def parse_cbsa_delineation(raw_bytes):
    """Parse list1_<year>.xlsx -> {county_fips5: (cbsa_code, cbsa_name)}.

    The Census file has two title rows before the header row; we locate the
    header by the 'FIPS State Code' / 'FIPS County Code' / 'CBSA Code' labels
    so the parser survives minor row-offset changes between vintages.
    """
    try:
        import openpyxl  # type: ignore
    except ImportError:
        return None, "openpyxl not installed; run: pip install openpyxl"
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        # Find the header row.
        col = {}
        for row in rows:
            labels = [str(c or "").strip() for c in row]
            if "FIPS State Code" in labels and "FIPS County Code" in labels:
                col = {labels[i]: i for i in range(len(labels))}
                break
        if not col:
            wb.close()
            return None, "CBSA delineation: header row not found"
        c_cbsa = col.get("CBSA Code")
        c_title = col.get("CBSA Title")
        c_st = col.get("FIPS State Code")
        c_cty = col.get("FIPS County Code")
        mapping = {}
        for row in rows:  # continues after the header row
            if c_st >= len(row) or c_cty >= len(row):
                continue
            st = str(row[c_st] or "").strip()
            cty = str(row[c_cty] or "").strip()
            if not st or not cty:
                continue
            fips5 = st.zfill(2) + cty.zfill(3)
            cbsa_code = str(row[c_cbsa] or "").strip() if c_cbsa is not None else ""
            cbsa_name = str(row[c_title] or "").strip() if c_title is not None else ""
            if cbsa_code:
                mapping[fips5] = (cbsa_code, cbsa_name)
        wb.close()
        return mapping, None
    except Exception as exc:
        return None, f"CBSA delineation parse failed: {exc}"


# ---------------------------------------------------------------------------
# 3. Census ZCTA gazetteer centroid parser
# ---------------------------------------------------------------------------


def parse_gazetteer_zip(raw_bytes):
    """Parse the ZCTA gazetteer ZIP -> {zip5: (lat, lng)} (4 dp)."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            name = next(n for n in zf.namelist() if n.endswith(".txt"))
            text = zf.read(name).decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        centroids = {}
        for row in reader:
            lower = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            zcta = (lower.get("geoid") or "").strip().zfill(5)
            if len(zcta) != 5 or not zcta.isdigit():
                continue
            try:
                lat = round(float(lower.get("intptlat") or 0), 4) or None
                lng = round(float(lower.get("intptlong") or 0), 4) or None
            except (TypeError, ValueError):
                lat = lng = None
            if lat and lng:
                centroids[zcta] = (lat, lng)
        return centroids, None
    except Exception as exc:
        return None, f"gazetteer parse failed: {exc}"


# ---------------------------------------------------------------------------
# Build logic: dedup + join
# ---------------------------------------------------------------------------


def build_crosswalk(zcta_records, cbsa_map, centroids=None):
    """Dedup ZCTA->county by max AREALAND_PART, join CBSA + centroid, sort by zip.

    zcta_records: list of {zip5, county_fips, county_name, arealand_part}
    cbsa_map:     {county_fips5: (cbsa_code, cbsa_name)}
    centroids:    {zip5: (lat, lng)} or None

    Returns a list of row dicts ordered by _OUT_COLS, sorted by zip5.
    """
    centroids = centroids or {}
    # Dedup: keep the county with the largest land-area part per ZIP.
    best = {}
    for rec in zcta_records:
        z = rec["zip5"]
        if z not in best or rec["arealand_part"] > best[z]["arealand_part"]:
            best[z] = rec

    out_rows = []
    for z in sorted(best.keys()):
        rec = best[z]
        fips5 = rec["county_fips"]
        cbsa_code, cbsa_name = cbsa_map.get(fips5, ("", ""))
        lat, lng = centroids.get(z, (None, None))
        out_rows.append({
            "zip5": z,
            "county_fips": fips5,
            "county_name": rec["county_name"],
            "state": _STATE_FIPS.get(fips5[:2], ""),
            "cbsa_code": cbsa_code or "",
            "cbsa_name": cbsa_name or "",
            "centroid_lat": lat if lat is not None else "",
            "centroid_lng": lng if lng is not None else "",
        })
    return out_rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    here = os.path.dirname(os.path.abspath(__file__))
    default_out = os.path.join(here, "zip_cbsa_crosswalk.csv")
    ap.add_argument("--out", default=default_out, help=f"output CSV path (default: {default_out})")
    ap.add_argument("--dry-run", action="store_true", help="fetch+parse but do not write the CSV")
    ap.add_argument(
        "--skip-gazetteer", action="store_true",
        help="skip the centroid download (faster; centroid_lat/lng left blank)",
    )
    args = ap.parse_args()

    print("[build_zip_cbsa_crosswalk] checking network connectivity ...")
    if not _check_network():
        print(
            "[build_zip_cbsa_crosswalk] WARNING: network unavailable.\n"
            "  Cannot download Census sources.  Existing committed seed\n"
            "  (data/zip_cbsa_crosswalk.csv) is left intact.  Run this script on a\n"
            "  machine with internet access to build the full dataset before the\n"
            "  live geo backfill.",
            file=sys.stderr,
        )
        sys.exit(0)

    # ---- 1. ZCTA <-> county relationship ----
    print(f"[build_zip_cbsa_crosswalk] fetching ZCTA<->county: {CENSUS_ZCTA_COUNTY_URL}")
    rel_bytes, err = _fetch_bytes(CENSUS_ZCTA_COUNTY_URL)
    if not rel_bytes:
        sys.exit(f"[build_zip_cbsa_crosswalk] ERROR: ZCTA-county fetch failed: {err}")
    zcta_records, err = parse_zcta_county(rel_bytes)
    if not zcta_records:
        sys.exit(f"[build_zip_cbsa_crosswalk] ERROR: ZCTA-county parse failed: {err}")
    print(f"  Loaded {len(zcta_records):,} ZCTA-county relationship rows.")

    # ---- 2. CBSA delineation ----
    print(f"[build_zip_cbsa_crosswalk] fetching CBSA delineation: {CENSUS_CBSA_URL}")
    cbsa_bytes, err = _fetch_bytes(CENSUS_CBSA_URL)
    cbsa_map = {}
    if cbsa_bytes:
        cbsa_map, err = parse_cbsa_delineation(cbsa_bytes)
        if cbsa_map is None:
            cbsa_map = {}
            print(f"  CBSA delineation parse warning: {err}; cbsa fields will be empty.")
    else:
        print(f"  CBSA delineation download failed: {err}; cbsa fields will be empty.")
    print(f"  Loaded {len(cbsa_map):,} county->CBSA mappings.")

    # ---- 3. Optional ZCTA centroids ----
    centroids = {}
    if not args.skip_gazetteer:
        print(f"[build_zip_cbsa_crosswalk] fetching ZCTA gazetteer: {CENSUS_ZCTA_GAZ_URL}")
        gaz_bytes, err = _fetch_bytes(CENSUS_ZCTA_GAZ_URL)
        if gaz_bytes:
            centroids, err = parse_gazetteer_zip(gaz_bytes)
            if centroids is None:
                centroids = {}
                print(f"  gazetteer parse warning: {err}")
            else:
                print(f"  Loaded {len(centroids):,} ZCTA centroid(s).")
        else:
            print(f"  gazetteer download failed: {err}; centroid columns left blank.")

    # ---- 4. Build + dedup + join ----
    print("[build_zip_cbsa_crosswalk] deduplicating + joining ...")
    out_rows = build_crosswalk(zcta_records, cbsa_map, centroids)
    with_cbsa = sum(1 for r in out_rows if r["cbsa_code"])
    with_centroid = sum(1 for r in out_rows if r["centroid_lat"] != "")
    print(
        f"  Output: {len(out_rows):,} rows (one per ZIP after dedup); "
        f"{with_cbsa:,} with a CBSA, {with_centroid:,} with a centroid."
    )

    if args.dry_run:
        print("[build_zip_cbsa_crosswalk] --dry-run: not writing output.")
        return

    # ---- 5. Write CSV ----
    out_path = os.path.abspath(args.out)
    print(f"[build_zip_cbsa_crosswalk] writing {out_path} ...")
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_OUT_COLS)
        writer.writeheader()
        writer.writerows(out_rows)
    print(f"[build_zip_cbsa_crosswalk] done. {len(out_rows):,} rows written to {out_path}")
    print(
        "\nNext steps:\n"
        "  1. git add data/zip_cbsa_crosswalk.csv && git commit\n"
        "  2. Apply sql/014_cre_geo_crosswalk.sql to the Supabase project\n"
        "     (\\copy loads this file into credeals.cre_zip_cbsa_crosswalk)\n"
        "  3. python3 cre_geo_backfill.py --dry-run  (review counts)\n"
        "  4. python3 cre_geo_backfill.py --apply    (explicit go-ahead only)"
    )


if __name__ == "__main__":
    main()
