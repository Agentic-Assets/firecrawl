# data/

Durable reference data for the CRE collector. Files here are committed (not gitignored).

## zip_cbsa_crosswalk.csv

**Status: FULL DATASET (33,791 rows, one per Census ZCTA). Built 2026-06-15 from
an all-Census, token-free pipeline (see Sources). Rebuild any time with
`build_zip_cbsa_crosswalk.py` on a networked host; commit the result before
running `cre_geo_backfill.py --apply`.**

33,791 is the count of Census ZCTAs (geographic ZIPs). The earlier "~41,000"
estimate counted HUD's USPS ZIP universe, which includes non-geographic
(PO-box / point) ZIPs that have no county or centroid; ZCTAs are the standard
public-domain substitute and cover >99% of mailable ZIPs.

To rebuild:

```bash
cd scripts/firecrawl-ops/cre_collector
python3 data/build_zip_cbsa_crosswalk.py          # writes data/zip_cbsa_crosswalk.csv
git add data/zip_cbsa_crosswalk.csv
git commit -m "chore: rebuild zip_cbsa_crosswalk.csv (Census rel2020 / CBSA 2023)"
```

Columns: `zip5, county_fips, county_name, state, cbsa_code, cbsa_name, centroid_lat, centroid_lng`

One row per ZIP (the county holding the largest land-area share of the ZCTA
wins when a ZIP spans multiple counties, per Census `AREALAND_PART`). 24,734 of
the 33,791 ZIPs fall inside a CBSA; the rest are non-metro and carry empty
`cbsa_code`/`cbsa_name`. All 33,791 have a centroid.

### Sources (all US Census Bureau, public domain, no token, no attribution)

HUD's USPS crosswalk was dropped: its direct portal download is now behind an
Akamai bot gate (HTTP 202, empty body) and its API requires a per-user token,
neither reproducible in an unattended build. The Census equivalents below need
no token and are fetched straight from `www2.census.gov`.

- **ZCTA5 <-> County relationship file** (2020 geographies)
  `.../rel2020/zcta520/tab20_zcta520_county20_natl.txt`
  Used for: ZIP(ZCTA5) -> county FIPS, county name, and `AREALAND_PART`
  (land-area dedup key for multi-county ZIPs).
- **CBSA Delineation File** (September 2023, OMB Bulletin 23-01)
  `.../metro-micro/.../delineation-files/2023/list1_2023.xlsx`
  Used for: county FIPS -> cbsa_code, cbsa_name.
- **National ZCTA Gazetteer** (2023)
  `.../gazetteer/2023_Gazetteer/2023_Gaz_zcta_national.zip`
  Used for: ZIP(ZCTA5) -> centroid lat/lng.

State abbreviation is derived from the county FIPS prefix via a static
FIPS->USPS table in the builder (avoids a fourth download).

### Reproducibility

`data/build_zip_cbsa_crosswalk.py` downloads the three Census sources, dedups
multi-county ZIPs by max `AREALAND_PART`, joins CBSA + centroid, and emits this
file sorted by `zip5` (byte-deterministic for the same vintages). The vintages
are recorded in the script header so the output is reviewable.

### Usage

- **Python path** (`cre_geo.py`): reads this CSV directly into `ZipCbsaCrosswalk`
  for in-memory O(1) lookup. No DB round-trip needed for the 87k backfill.
- **SQL path** (`sql/014_cre_geo_crosswalk.sql`): `\copy` loads this file into
  `credeals.cre_zip_cbsa_crosswalk` for consumer ad-hoc SQL joins.
- **TS path** (`lib/geo.ts`): does NOT load this file (keeps the Node bundle
  small). Adapters emit `postalCode`/`latitude`/`longitude`; ingest derives geo.

### geo_source values (written to cre_listings.geo_source)

| Value | Meaning |
|---|---|
| `source` | Broker provided county/market/submarket verbatim (e.g. Newmark) |
| `crosswalk_zip` | Derived from postalCode via this CSV |
| `crosswalk_latlng` | Derived from lat/lng nearest-centroid match via this CSV |
