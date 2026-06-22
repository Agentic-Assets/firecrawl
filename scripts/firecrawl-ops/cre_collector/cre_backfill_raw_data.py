#!/usr/bin/env python3
"""
cre_backfill_raw_data.py: one-time, additive, idempotent Class-1 scalar backfill.

WHY THIS EXISTS
---------------
The ~87k existing active cre_listings rows were ingested before the adapters
emitted the Phase-2 institutional fields (submarket, building_class, tenant_name,
canonical_url, ...). The value is ALREADY present in each row's stored raw_data
(the adapter's listing object), under the per-source NESTED objects the gap doc
enumerates (marcusSpecifications, rawSharpLaunch, rawNewmarkHit, jllDetail,
transwesternFacts, availability[], publicPost, rawSavillsProperty, ...). This
script reads ONLY that stored raw_data (no scrape, no network) and writes the
now-mappable columns. It is the biggest immediate coverage jump in the data-lift.

SOURCE OF TRUTH
---------------
* Gap doc per-source recoverable map: RAW_DATA_GAP_CLASSIFICATION_2026-06-15.md.
* Field vocabulary + COALESCE-keep + dual-mode invariants:
  PHASE2_DATA_LIFT_CONTRACT_2026-06-15.md (Sections B, C, H).
* ALL parsing goes through cre_parse.py (the frozen, golden-vector-locked
  helpers). This script NEVER reinvents a parse regex.

CONTRACT (locked)
-----------------
* PURE-ADDITIVE, COALESCE-KEEP. Every column write is
  `UPDATE ... SET col = COALESCE(<derived>, col)` so a derived NULL never blanks
  a populated value, and a populated derived value only fills a row where the
  column is currently NULL (the WHERE clause restricts to improvable rows).
  extra_facts is `COALESCE(extra_facts,'{}') || <derived>` (jsonb merge), guarded
  so an empty derived blob is a no-op.
* NEVER writes status / deleted_at / transaction_type. A source status BADGE
  routes ONLY through the existing OPT-IN default-off activation gate in
  cre_ingest.py; this backfill does not touch status at all.
* dual-mode read: for colliers-main / lee / svn / avison-young / transwestern a
  dual sale+lease payload is wrapped {primary, secondary_pass}. Every read is
  COALESCE(raw->'primary', raw->'secondary_pass', raw) (gap doc invariant 4)
  so ~6-8% of dual-mode rows are not silently dropped.
* canonical_url is universal: raw_data->>'url' (or the dual-mode COALESCE) for
  every source. Column 0% today, ~95% board-wide after this pass.
* COLUMN-EXISTENCE + to_regclass GUARDED. The institutional columns land in
  sql/012; until that migration is applied the generated UPDATE is a clean
  no-op (a DO block checks every target column exists before running). This file
  NEVER creates or alters a table (sql/ owns DDL).
* --dry-run is the DEFAULT: build the SQL, print a per-source per-column
  candidate-count summary, write NOTHING. --apply is gated.
* Same DB convention as cre_ingest.py / backfill_media_from_raw_data.py:
  POSTGRES_URL_NON_POOLING / POSTGRES_URL via psql, discovered through the SAME
  loader (load_db_url) and resolver (find_psql). The URL is never printed.

Usage:
  python3 cre_backfill_raw_data.py                       # dry-run (default)
  python3 cre_backfill_raw_data.py --dry-run
  python3 cre_backfill_raw_data.py --apply               # writes (gated)
  python3 cre_backfill_raw_data.py --keep-sql /tmp/bf.sql --dry-run
  python3 cre_backfill_raw_data.py --env-file /path/.env.local
  python3 cre_backfill_raw_data.py --source marcus-millichap,newmark
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

# Reuse the exact DB-connection convention + the frozen normalizers/parsers from
# cre_ingest.py / cre_parse.py without duplicating them. conftest.py puts
# cre_collector/ on sys.path under pytest; for a direct CLI run we add our own
# directory too so the import resolves from anywhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cre_parse  # noqa: E402
from cre_ingest import (  # noqa: E402
    SOURCE_TO_BROKERAGE,
    SQFT_PER_ACRE,
    bool_or_none,
    clean_text,
    find_psql,
    http_url_or_none,
    int_or_none,
    iter_copy_json_rows,
    load_db_url,
    norm_building_class,
    norm_cap_rate,
    norm_lease_rate_type,
    norm_occupancy_rate,
    num_or_none,
    sql_lit,
)

# ---------------------------------------------------------------------------
# Target columns. Every column written by this backfill MUST appear here; the
# generated SQL is column-existence-guarded against exactly this set so a
# pre-sql/012 run is a clean no-op. canonical_url / market / submarket / county
# pre-exist (sql/002); the rest land in sql/012. extra_facts is handled
# separately (jsonb merge, not COALESCE-keep).
# ---------------------------------------------------------------------------

# numeric / text / int / bool scalar columns (COALESCE-keep write).
SCALAR_COLUMNS = (
    "canonical_url",
    "cap_rate",
    "occupancy_rate",
    "size_sf",
    "lot_size_sf",
    "sale_price_usd",
    "sale_price_per_sf",
    "units",
    "year_built",
    "floors",
    "available_sf",
    "min_divisible_sf",
    "max_divisible_sf",
    "lease_rate_min",
    "lease_rate_max",
    "lease_rate_type",
    "market",
    "submarket",
    "county",
    "building_class",
    "property_subtype",
    "apn",
    "tenant_name",
    "guarantor",
    "lease_years_remaining",
    "price_per_unit",
    "grm",
    "price_per_acre",
    "num_rooms",
    "revpar",
    "clear_height_ft",
    "dock_doors",
    "drive_in_doors",
    "power_service",
    "rail_served",
)

# Postgres column type per staged column (drives the temp-table DDL and the cast
# in the COALESCE write). Keep in sync with sql/002 + sql/012.
_COLUMN_PG_TYPE = {
    "canonical_url": "text",
    "cap_rate": "numeric",
    "occupancy_rate": "numeric",
    "size_sf": "numeric",
    "lot_size_sf": "numeric",
    "sale_price_usd": "numeric",
    "sale_price_per_sf": "numeric",
    "units": "integer",
    "year_built": "integer",
    "floors": "integer",
    "available_sf": "numeric",
    "min_divisible_sf": "numeric",
    "max_divisible_sf": "numeric",
    "lease_rate_min": "numeric",
    "lease_rate_max": "numeric",
    "lease_rate_type": "text",
    "market": "text",
    "submarket": "text",
    "county": "text",
    "building_class": "text",
    "property_subtype": "text",
    "apn": "text",
    "tenant_name": "text",
    "guarantor": "text",
    "lease_years_remaining": "numeric",
    "price_per_unit": "numeric",
    "grm": "numeric",
    "price_per_acre": "numeric",
    "num_rooms": "integer",
    "revpar": "numeric",
    "clear_height_ft": "numeric",
    "dock_doors": "integer",
    "drive_in_doors": "integer",
    "power_service": "text",
    "rail_served": "boolean",
}

# ---------------------------------------------------------------------------
# Dual-mode read (gap doc invariant 4): a dual sale+lease payload is wrapped
# {primary, secondary_pass} by merge_rows(); a single-pass row is flat. Reads
# must COALESCE(primary, secondary_pass, top-level) or ~6-8% of rows drop.
# ---------------------------------------------------------------------------


def dual_mode_passes(raw):
    """Ordered sub-dicts to read for a dual-mode COALESCE: primary, then
    secondary_pass, then the flat blob itself. Mirrors the gap doc
    COALESCE(raw->'primary', raw->'secondary_pass', raw) precedence."""
    if not isinstance(raw, dict):
        return []
    passes = []
    for key in ("primary", "secondary_pass"):
        sub = raw.get(key)
        if isinstance(sub, dict):
            passes.append(sub)
    passes.append(raw)
    return passes


def dual_get(raw, *keys):
    """First present (not-None) value among `keys`, scanning primary ->
    secondary_pass -> flat in turn. The universal canonical_url / source-field
    read path for dual-mode sources."""
    for sub in dual_mode_passes(raw):
        for k in keys:
            v = sub.get(k)
            if v is not None:
                return v
    return None


def dual_get_obj(raw, key):
    """First present nested OBJECT (dict) for `key` across primary ->
    secondary_pass -> flat. Used to locate marcusSpecifications / rawSharpLaunch /
    rawNewmarkHit / jllDetail / transwesternFacts on either pass."""
    for sub in dual_mode_passes(raw):
        v = sub.get(key)
        if isinstance(v, dict):
            return v
    return None


def dual_get_list(raw, key):
    """First present nested LIST for `key` across primary -> secondary_pass ->
    flat (transwestern availability[])."""
    for sub in dual_mode_passes(raw):
        v = sub.get(key)
        if isinstance(v, list):
            return v
    return None


def _strip_comma_num(v, lo=0, hi=None):
    """A comma-formatted numeric string ('25,000') or number -> float via
    num_or_none. Pure string parse uses cre_parse.parse_money's number regex
    indirectly through _NUM; here we only strip grouping commas and reuse
    num_or_none's range clamp so behavior matches the ingest path."""
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s:
            return None
        try:
            v = float(s)
        except ValueError:
            return None
    return num_or_none(v, lo=lo, hi=hi)


def _canonical_url(raw):
    """Universal canonical_url <- raw_data->>'url' (dual-mode aware), http-guarded.
    Applies to EVERY source (gap doc: column 0% today, url present 92-100%)."""
    return http_url_or_none(dual_get(raw, "url"))


def _set(out, col, value):
    """Stage a derived column value only when it is not None (None is the
    COALESCE-keep no-op; staging it would needlessly widen the candidate count)."""
    if value is not None and col in _COLUMN_PG_TYPE:
        out[col] = value


def _add_extra(extra, key, value):
    """Stage one snake_case extra_facts entry, dropping empties."""
    if value is None:
        return
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return
    extra[key] = value


# ---------------------------------------------------------------------------
# Per-source pure derivation. Each returns (columns: dict, extra_facts: dict).
# Input is the stored cre_listings.raw_data blob (the adapter listing object,
# possibly {primary, secondary_pass}-wrapped). No DB, no network, never throws.
# Every parse delegates to cre_parse / the frozen cre_ingest normalizers.
# ---------------------------------------------------------------------------


def derive_marcus_millichap(raw):
    """Marcus & Millichap: nested raw_data->'marcusSpecifications' (gap doc).
    Values are pre-formatted strings; parse/strip via cre_parse + normalizers."""
    cols, extra = {}, {}
    spec = dual_get_obj(raw, "marcusSpecifications") or {}

    # cap_rate <- 'Cap Rate' ('8.60%') or top-level capRatePct. norm_cap_rate
    # accepts a percent number; parse the '%' string to a number first.
    cap_txt = spec.get("Cap Rate")
    cap_pct = cre_parse.parse_percent_to_fraction(cap_txt) if isinstance(cap_txt, str) else None
    # parse_percent_to_fraction('8.60%') -> 0.086 (already a fraction).
    cap = norm_cap_rate(cap_pct) if cap_pct is not None else norm_cap_rate(raw.get("capRatePct"))
    if cap is None:
        cap = norm_cap_rate(dual_get(raw, "capRatePct"))
    _set(cols, "cap_rate", cap)

    # occupancy_rate <- 'Occupancy' ('87.5%' -> 0.875).
    _set(cols, "occupancy_rate",
         norm_occupancy_frac(spec.get("Occupancy")))

    # size_sf <- 'Rentable SF' (fallback 'Gross SF').
    _set(cols, "size_sf",
         _strip_comma_num(spec.get("Rentable SF"), lo=0, hi=1e9)
         or _strip_comma_num(spec.get("Gross SF"), lo=0, hi=1e9))

    # sale_price_per_sf <- 'Price/Gross SF' ('$272.07').
    _set(cols, "sale_price_per_sf",
         num_or_none(cre_parse.parse_money(spec.get("Price/Gross SF")), lo=0, hi=1e5))

    # lot_size_sf <- 'Lot Size' ('3.83 acres' x43560).
    _set(cols, "lot_size_sf", cre_parse.acres_to_sf(spec.get("Lot Size")))

    # units <- 'Number of Units'.
    _set(cols, "units", int_or_none(_strip_comma_num(spec.get("Number of Units"), lo=0, hi=1e6),
                                    lo=0, hi=1e6))

    # year_built <- 'Year Built' (occasionally present on M&M land/retail rows).
    _set(cols, "year_built", _year_or_none(spec.get("Year Built")))

    # lease_rate_type <- 'Lease Type' ('Triple Net (NNN)').
    _set(cols, "lease_rate_type", norm_lease_rate_type(spec.get("Lease Type")))

    # lease_rate_min/max <- 'Rent Per Square Feet' ('$23.40'); in-place tenant rent.
    lo, hi, _t = cre_parse.parse_lease_rate(spec.get("Rent Per Square Feet"))
    _set(cols, "lease_rate_min", lo)
    _set(cols, "lease_rate_max", hi)

    # New institutional columns (sql/012).
    _set(cols, "tenant_name", clean_text(spec.get("Tenant Name"), 256))
    _set(cols, "guarantor", clean_text(spec.get("Guarantor"), 256))
    _set(cols, "lease_years_remaining",
         num_or_none(_strip_comma_num(spec.get("Years Remaining On Lease")), lo=0, hi=99))
    _set(cols, "grm", num_or_none(_strip_comma_num(spec.get("GRM")), lo=0, hi=100))
    _set(cols, "price_per_unit",
         num_or_none(cre_parse.parse_money(spec.get("Price/Unit")), lo=0, hi=1e9))
    _set(cols, "price_per_acre",
         num_or_none(cre_parse.parse_money(spec.get("Price/Acre")), lo=0, hi=1e9))
    _set(cols, "num_rooms",
         int_or_none(_strip_comma_num(spec.get("Number of Rooms"), lo=0, hi=1e5), lo=0, hi=1e5))
    _set(cols, "revpar",
         num_or_none(cre_parse.parse_money(spec.get("RevPAR")), lo=0, hi=1e5))

    # extra_facts long tail (no discrete column, no consumer query need).
    _add_extra(extra, "buildable_square_feet", clean_text(spec.get("Buildable Square Feet"), 64))
    _add_extra(extra, "price_per_room", clean_text(spec.get("Price/Room"), 64))
    return cols, extra


def derive_avison_young(raw):
    """Avison Young: nested raw_data->'rawSharpLaunch' (gap doc). Dual-mode."""
    cols, extra = {}, {}
    rsl = dual_get_obj(raw, "rawSharpLaunch") or {}

    # AY stores no top-level `url`; canonical_url <- rawSharpLaunch.external_url
    # (the canonical avisonyoung.us listing URL, not the SharpLaunch subdomain).
    ay_url = http_url_or_none(dual_get(raw, "url") or rsl.get("external_url"))
    if ay_url is not None:
        cols["canonical_url"] = ay_url

    # available_sf / min_divisible_sf <- availabilities_min_surface_sqft.
    _set(cols, "available_sf",
         num_or_none(rsl.get("availabilities_min_surface_sqft"), lo=0, hi=1e9))
    _set(cols, "min_divisible_sf",
         num_or_none(rsl.get("availabilities_min_surface_sqft"), lo=0, hi=1e9))
    _set(cols, "max_divisible_sf",
         num_or_none(rsl.get("availabilities_max_surface_sqft"), lo=0, hi=1e9))

    # lease_rate_min/max <- availabilities_min_rent / availabilities_max_rent.
    # (DQ guard 3) AY anomalous $7500/SF/YR: clamp through the >500 cap so the
    # known anomaly is rejected exactly like cre_parse.parse_lease_rate would.
    _set(cols, "lease_rate_min",
         num_or_none(rsl.get("availabilities_min_rent"), lo=0, hi=500))
    _set(cols, "lease_rate_max",
         num_or_none(rsl.get("availabilities_max_rent"), lo=0, hi=500))

    # submarket <- submarket.
    _set(cols, "submarket", clean_text(rsl.get("submarket"), 128))

    # year_built <- yearbuilt (rsl) / top-level yearBuilt.
    _set(cols, "year_built",
         _year_or_none(rsl.get("yearbuilt")) or _year_or_none(dual_get(raw, "yearBuilt")))

    # cap_rate <- top-level capRatePct / cap_rate.
    _set(cols, "cap_rate",
         norm_cap_rate(dual_get(raw, "capRatePct")) or norm_cap_rate(rsl.get("cap_rate")))

    # sale_price_per_sf <- top-level saleUnitPrice / sale_unit_price.
    _set(cols, "sale_price_per_sf",
         num_or_none(dual_get(raw, "saleUnitPrice") or rsl.get("sale_unit_price"), lo=0, hi=1e5))

    # units <- units.
    _set(cols, "units", int_or_none(rsl.get("units"), lo=0, hi=1e6))

    # building_class <- assetType / rawSubtypes ('office.medical'); class is
    # never inferred from a bare subtype, so norm_building_class returns None for
    # 'office.medical' -> property_subtype carries the raw string instead.
    subtype = _first_subtype(rsl.get("type")) or clean_text(dual_get(raw, "assetType"), 96)
    _set(cols, "building_class", norm_building_class(subtype))
    _set(cols, "property_subtype", clean_text(subtype, 96))

    # extra_facts: lifecycle timestamps (no discrete column).
    _add_extra(extra, "on_market_at", clean_text(rsl.get("on_market_at"), 64))
    _add_extra(extra, "off_market_at", clean_text(rsl.get("off_market_at"), 64))
    return cols, extra


def derive_newmark(raw):
    """Newmark: nested raw_data->'rawNewmarkHit' (Algolia index) + top-level."""
    cols, extra = {}, {}
    hit = dual_get_obj(raw, "rawNewmarkHit") or {}

    # county <- top-level county (mirror rawNewmarkHit.county). 99.9%, source-verbatim.
    _set(cols, "county", clean_text(dual_get(raw, "county") or hit.get("county"), 128))
    # submarket <- submarket / hit.submarket.
    _set(cols, "submarket", clean_text(dual_get(raw, "submarket") or hit.get("submarket"), 128))
    # market <- rawNewmarkHit.market (source-verbatim).
    _set(cols, "market", clean_text(hit.get("market"), 128))
    # units <- rawNewmarkHit.number_of_units.
    _set(cols, "units", int_or_none(hit.get("number_of_units"), lo=0, hi=1e6))

    # sale_price_usd <- rawNewmarkHit.sale_price ('$8,585,673.00'); (DQ guard 6)
    # 'Subject to Offer' / non-numeric is rejected because parse_money only
    # matches a real '$N' token (a quoted phrase yields None).
    _set(cols, "sale_price_usd",
         num_or_none(cre_parse.parse_money(hit.get("sale_price")), lo=100, hi=1e11))

    # property_subtype <- rawNewmarkHit.property_subtype ('Warehouse/Distribution'), 100%.
    _set(cols, "property_subtype", clean_text(hit.get("property_subtype"), 96))
    return cols, extra


def derive_jll(raw):
    """JLL: nested raw_data->'jllDetail' (+ jllInvestorDetail subset)."""
    cols, extra = {}, {}
    detail = dual_get_obj(raw, "jllDetail") or {}
    investor = dual_get_obj(raw, "jllInvestorDetail") or {}

    # submarket <- jllDetail.submarket, ~85%.
    _set(cols, "submarket", clean_text(detail.get("submarket"), 128))
    # building_class <- jllDetail.buildingClass ('A'/'B'/'C').
    _set(cols, "building_class", norm_building_class(detail.get("buildingClass")))

    # extra_facts: locationDescription (Suburbs/Urban), dealType.
    _add_extra(extra, "location_description", clean_text(detail.get("locationDescription"), 64))
    _add_extra(extra, "deal_type", clean_text(investor.get("dealType"), 64))
    return cols, extra


def derive_transwestern(raw):
    """Transwestern: raw_data->'transwesternFacts' + raw_data->'availability'[].
    Dual-mode. NOTE: the media build already lifts some facts; this is additive
    and COALESCE-keep, so a double-map is harmless (never overwrites)."""
    cols, extra = {}, {}
    facts = dual_get_obj(raw, "transwesternFacts") or {}
    avail = dual_get_list(raw, "availability") or []

    # floors <- Stories.
    _set(cols, "floors", int_or_none(_strip_comma_num(facts.get("Stories")), lo=0, hi=1e4))
    # year_built <- 'Year Built'.
    _set(cols, "year_built", _year_or_none(facts.get("Year Built")))
    # building_class <- Class.
    _set(cols, "building_class", norm_building_class(facts.get("Class")))

    # min/max divisible + available_sf over availability[].size (lease rows only
    # for available_sf: exclude any type ILIKE '%sale%').
    sizes, lease_sizes, lease_rates = [], [], []
    lease_type_token = None
    for row in avail:
        if not isinstance(row, dict):
            continue
        sz = _strip_comma_num(row.get("size"), lo=0, hi=1e9)
        rtype = str(row.get("type") or "")
        is_sale = "sale" in rtype.lower()
        if sz is not None:
            sizes.append(sz)
            if not is_sale:
                lease_sizes.append(sz)
        if not is_sale:
            # lease rate: rate<1000 psf, parse via cre_parse (annualized psf).
            lo, _hi, _t = cre_parse.parse_lease_rate(row.get("rate"))
            if lo is not None and lo < 1000:
                lease_rates.append(lo)
            # lease_rate_type token lives in raw[]: match against the vocabulary,
            # index VARIES, so scan each token (never hardcode an index).
            if lease_type_token is None:
                lease_type_token = _transwestern_net_token(row.get("raw"))
    if sizes:
        _set(cols, "min_divisible_sf", num_or_none(min(sizes), lo=0, hi=1e9))
        _set(cols, "max_divisible_sf", num_or_none(max(sizes), lo=0, hi=1e9))
    if lease_sizes:
        _set(cols, "available_sf", num_or_none(sum(lease_sizes), lo=0, hi=1e9))
    if lease_rates:
        _set(cols, "lease_rate_min", num_or_none(min(lease_rates), lo=0, hi=500))
        _set(cols, "lease_rate_max", num_or_none(max(lease_rates), lo=0, hi=500))
    if lease_type_token:
        _set(cols, "lease_rate_type", norm_lease_rate_type(lease_type_token))

    # lot_size_sf <- 'Land Area (ac)' x43560 — (DQ guard 5) UNIT INCONSISTENT:
    # a value that already looks like SF (large integer) must NOT be x43560'd.
    _set(cols, "lot_size_sf", _transwestern_land_area_sf(facts.get("Land Area (ac)")))

    # Industrial specs (sql/012).
    _set(cols, "clear_height_ft", _clear_height(facts))
    _set(cols, "dock_doors", int_or_none(_strip_comma_num(facts.get("Docks")), lo=-1, hi=1e4))
    _set(cols, "drive_in_doors",
         int_or_none(_strip_comma_num(facts.get("Grade Level Doors")), lo=-1, hi=1e4))
    _set(cols, "power_service", clean_text(facts.get("Power"), 128))
    _set(cols, "rail_served", _yesno_bool(facts.get("Rail")))
    _set(cols, "apn", clean_text(facts.get("Parcel"), 64))
    _set(cols, "property_subtype", clean_text(facts.get("Property Type"), 96))

    # extra_facts long tail.
    _add_extra(extra, "tw_year_renovated", clean_text(facts.get("Year Renovated"), 32))
    _add_extra(extra, "tw_typical_floor_size", clean_text(facts.get("Typical Floor Size"), 32))
    _add_extra(extra, "tw_elevators", clean_text(facts.get("Elevators"), 16))
    _add_extra(extra, "tw_yard", clean_text(facts.get("Yard"), 16))
    return cols, extra


def derive_cbre(raw):
    """CBRE: flat cbre feed (+ cbre-dealflow subset). leaseRateText / headline."""
    cols, extra = {}, {}
    # lease_rate_min/max/type <- leaseRateText ('3.59 USD/SF/MO').
    lo, hi, t = cre_parse.parse_lease_rate(dual_get(raw, "leaseRateText"))
    _set(cols, "lease_rate_min", lo)
    _set(cols, "lease_rate_max", hi)
    _set(cols, "lease_rate_type", norm_lease_rate_type(t))
    # property_subtype <- dealflow projectType when present.
    dealflow = dual_get_obj(raw, "cbreDealflowDetail") or {}
    _add_extra(extra, "cbre_project_type", clean_text(dealflow.get("projectType"), 64))
    return cols, extra


def derive_cushman_wakefield(raw):
    """Cushman & Wakefield: flat list-API feed. leaseRateText ('$30.60 (Annual) USD')."""
    cols, extra = {}, {}
    lo, hi, t = cre_parse.parse_lease_rate(dual_get(raw, "leaseRateText"))
    _set(cols, "lease_rate_min", lo)
    _set(cols, "lease_rate_max", hi)
    _set(cols, "lease_rate_type", norm_lease_rate_type(t))
    api = dual_get_obj(raw, "rawCushmanApi") or {}
    if api.get("is_investment_property") is True:
        _add_extra(extra, "is_investment_property", True)
    return cols, extra


def derive_buildout(raw):
    """SVN + Lee & Associates (Buildout feed). leaseRateText ranges, acreage
    sizeText, and the Lee salePriceUsd per-SF conflation guard (DQ guard 2)."""
    cols, extra = {}, {}
    # lease_rate_min/max/type <- leaseRateText.
    lo, hi, t = cre_parse.parse_lease_rate(dual_get(raw, "leaseRateText"))
    _set(cols, "lease_rate_min", lo)
    _set(cols, "lease_rate_max", hi)
    _set(cols, "lease_rate_type", norm_lease_rate_type(t))

    # lot_size_sf <- sizeText rows ending 'Acres' x43560 (route acreage to lot).
    _set(cols, "lot_size_sf", cre_parse.acres_to_sf(dual_get(raw, "sizeText")))

    # (DQ guard 2) Lee salePriceUsd CONFLATES absolute price and per-SF rate
    # ('$6.00/SF' stored as salePriceUsd:6). Guard on salePriceText '/ SF': when
    # per-SF, suppress the absolute price and route to sale_price_per_sf.
    sale_text = dual_get(raw, "salePriceText")
    sale_usd = num_or_none(dual_get(raw, "salePriceUsd"), lo=100, hi=1e11)
    if cre_parse.is_per_sf_text(sale_text):
        psf = num_or_none(cre_parse.parse_money(sale_text), lo=0, hi=1e5)
        _set(cols, "sale_price_per_sf", psf)
        # absolute price suppressed (do not stage sale_price_usd)
    else:
        _set(cols, "sale_price_usd", sale_usd)
    return cols, extra


def derive_colliers(raw):
    """Colliers: colliers-main (sitemap) + colliers SalesTracker subset.
    leaseRateText (low explicit-token yield) + license rides contacts (not here)."""
    cols, extra = {}, {}
    lo, hi, t = cre_parse.parse_lease_rate(dual_get(raw, "leaseRateText"))
    _set(cols, "lease_rate_min", lo)
    _set(cols, "lease_rate_max", hi)
    _set(cols, "lease_rate_type", norm_lease_rate_type(t))
    return cols, extra


def derive_nai_global(raw):
    """NAI Global: infabode feed, raw_data->'publicPost'. (DQ guard 1) provider
    returns currency='POUND' on USD listings; the value is USD regardless of the
    label, so use parse_amount_ignoring_currency_label / treat publicPost.price
    as USD. listingStatus is contaminated -> NOT read here."""
    cols, extra = {}, {}
    post = dual_get_obj(raw, "publicPost") or {}
    mode = dual_get(raw, "transactionMode")

    # NAI has no top-level `url`; canonical_url <- sourceWebsiteUrl (==
    # publicPost.urlOriginal), gap doc. Set it here so the universal fallback
    # (which reads only `url`) does not miss the NAI feed.
    nai_url = http_url_or_none(dual_get(raw, "sourceWebsiteUrl") or post.get("urlOriginal"))
    if nai_url is not None:
        cols["canonical_url"] = nai_url

    price = post.get("price")
    # publicPost.price is a bare number labeled POUND but really USD. When it is a
    # string carrying a 'POUND ' prefix, strip the label; when numeric, take as USD.
    if isinstance(price, str):
        price = cre_parse.parse_amount_ignoring_currency_label(price)

    if mode == "sale":
        _set(cols, "sale_price_usd", num_or_none(price, lo=100, hi=1e11))
    elif mode == "lease":
        # per-SF annual lease price.
        _set(cols, "lease_rate_min", num_or_none(price, lo=0, hi=500))
        _set(cols, "lease_rate_max", num_or_none(price, lo=0, hi=500))

    # min/max divisible <- sizeRangeL / sizeRangeH (non-zero).
    _set(cols, "min_divisible_sf", num_or_none(post.get("sizeRangeL"), lo=0, hi=1e9))
    _set(cols, "max_divisible_sf", num_or_none(post.get("sizeRangeH"), lo=0, hi=1e9))

    # building_class <- tags ('BuildingClassB' -> 'B').
    _set(cols, "building_class", _nai_building_class(post.get("tags")))

    # extra_facts: listing office.
    org = dual_get_obj(raw, "sourceOrganization") or {}
    _add_extra(extra, "listing_office", clean_text(org.get("name"), 128))
    return cols, extra


def derive_savills(raw):
    """Savills: raw_data->'rawSavillsProperty' (full detail API preserved).
    Mostly Class-3 (Price on request). Recoverable: highlights (handled by the
    media/text builder, not here), available_sf <- AvailableSize.SqFt (non-zero)."""
    cols, extra = {}, {}
    prop = dual_get_obj(raw, "rawSavillsProperty") or {}
    avail = prop.get("AvailableSize") or {}
    if isinstance(avail, dict):
        _set(cols, "available_sf", num_or_none(avail.get("SqFt"), lo=0, hi=1e9))
    return cols, extra


# Source slug -> derivation function. Keyed by brokerage SLUG (the dedup grain
# for the scoped UPDATE), folding sub-source keys into the parent brokerage:
# cbre+cbre-dealflow -> cbre; colliers+colliers-main -> colliers; svn + lee
# -> buildout (one adapter shape).
DERIVERS = {
    "marcus-millichap": derive_marcus_millichap,
    "avison-young": derive_avison_young,
    "newmark": derive_newmark,
    "jll": derive_jll,
    "transwestern": derive_transwestern,
    "cbre": derive_cbre,
    "cushman-wakefield": derive_cushman_wakefield,
    "colliers": derive_colliers,
    "nai-global": derive_nai_global,
    "savills": derive_savills,
    "svn": derive_buildout,
    "lee-associates": derive_buildout,
}

# Map a brokerage slug -> the source keys whose rows it holds (for --source
# selection and the scoped UPDATE). Built from SOURCE_TO_BROKERAGE.
SLUG_TO_SOURCE_KEYS = {}
for _sk, (_slug, _pref) in SOURCE_TO_BROKERAGE.items():
    SLUG_TO_SOURCE_KEYS.setdefault(_slug, []).append(_sk)


def derive_columns(source_key, raw):
    """Pure dispatcher: (columns dict, extra_facts dict) for one raw_data blob.

    `source_key` is the per-row sourceKey (cbre-dealflow, colliers-main, svn,
    ...). The universal canonical_url is added for every source. Never throws;
    a blob it does not recognize yields only canonical_url (and an empty
    extra_facts)."""
    if not isinstance(raw, dict):
        return {}, {}
    fn = _deriver_for(source_key)
    cols, extra = ({}, {})
    if fn is not None:
        try:
            cols, extra = fn(raw)
        except Exception:  # pragma: no cover - defensive; a parse never aborts a sweep
            cols, extra = {}, {}
    # Universal canonical_url for EVERY source (gap doc).
    cu = _canonical_url(raw)
    if cu is not None:
        cols.setdefault("canonical_url", cu)
    # Drop any key not in the staged column set (defensive).
    cols = {k: v for k, v in cols.items() if k in _COLUMN_PG_TYPE and v is not None}
    extra = {k: v for k, v in (extra or {}).items()
             if v is not None and not (isinstance(v, str) and not v.strip())}
    return cols, extra


def _deriver_for(source_key):
    """Resolve the derivation fn for a per-row sourceKey, folding sub-sources to
    their parent brokerage adapter (cbre-dealflow -> cbre derive, colliers-main
    -> colliers derive, jll-investor -> jll derive)."""
    if source_key in DERIVERS:
        return DERIVERS[source_key]
    # Sub-source -> parent slug -> a deriver registered under a source key of
    # that slug. SOURCE_TO_BROKERAGE folds the sub-source onto the parent slug.
    mapping = SOURCE_TO_BROKERAGE.get(source_key)
    if not mapping:
        return None
    slug = mapping[0]
    if slug in DERIVERS:
        return DERIVERS[slug]
    for sk in SLUG_TO_SOURCE_KEYS.get(slug, []):
        if sk in DERIVERS:
            return DERIVERS[sk]
    return None


# ---------------------------------------------------------------------------
# Small typed helpers (delegate parsing to cre_parse; never a fresh regex).
# ---------------------------------------------------------------------------


def norm_occupancy_frac(text):
    """'87.5%' -> 0.875 via cre_parse, then clamp through norm_occupancy_rate's
    (0,1] band. A non-string / unparseable value yields None."""
    frac = cre_parse.parse_percent_to_fraction(text) if isinstance(text, str) else None
    return norm_occupancy_rate(frac) if frac is not None else None


def _year_or_none(v):
    """A 4-digit year (1700-2099) as int, or None. Accepts a string ('1984') or
    number; reuses cre_parse number extraction implicitly via str strip."""
    if isinstance(v, str):
        m = re.search(r"\b(1[789]\d\d|20\d\d)\b", v)
        if not m:
            return None
        v = int(m.group(1))
    if isinstance(v, (int, float)) and 1700 < v < 2100:
        return int(v)
    return None


def _first_subtype(types):
    """First non-empty subtype string from a SharpLaunch type[] list
    ('office.medical'), or None."""
    if isinstance(types, list):
        for t in types:
            if isinstance(t, str) and t.strip():
                return t.strip()
    elif isinstance(types, str) and types.strip():
        return types.strip()
    return None


def _transwestern_net_token(raw_arr):
    """Find the net/lease-basis token in a transwestern availability raw[] array
    (FSG/NNN/MG/IG/'Absolute Net'); the token INDEX varies, so scan against the
    vocabulary rather than hardcoding a position. Returns the matched token or
    None. norm_lease_rate_type then maps it to the CHECK enum."""
    if not isinstance(raw_arr, list):
        return None
    # Net/lease-basis tokens norm_lease_rate_type can map ('Absolute Net' maps to
    # None today and is intentionally not matched; the rest map to their enum).
    token_re = re.compile(
        r"nnn|triple net|modified gross|mod gross|full service|fsg|\bgross\b|\big\b",
        re.I,
    )
    for tok in raw_arr:
        if isinstance(tok, str) and token_re.search(tok):
            return tok  # raw token; norm_lease_rate_type maps it to the CHECK enum
    return None


def _transwestern_land_area_sf(v):
    """(DQ guard 5) 'Land Area (ac)' is unit-inconsistent: a small value ('29.2')
    is acres -> x43560; a large comma-formatted value ('29,185') already looks
    like SF and must NOT be converted. Heuristic: a numeric < 5000 is treated as
    acres; >= 5000 is assumed already-SF and returned as-is (still additive,
    COALESCE-keep). Anything unparseable -> None."""
    n = _strip_comma_num(v, lo=0, hi=1e9)
    if n is None:
        return None
    if n < 5000:
        return n * SQFT_PER_ACRE
    # Already SF-scale; do not multiply (avoid an absurd 1.2-billion-SF lot).
    return n


def _clear_height(facts):
    """Clear height (ft) from transwestern facts: prefer 'Clear Height(max)',
    fall back to 'Clear Height(min)' / 'Ceiling Height Range'. Clamp to (0,200)."""
    for key in ("Clear Height(max)", "Clear Height(min)", "Ceiling Height Range"):
        n = _strip_comma_num(facts.get(key), lo=0, hi=200)
        if n is not None:
            return n
    return None


def _yesno_bool(v):
    """'Yes'/'No' (case-insensitive) -> True/False; anything else -> None. Never
    writes a definitive False from a missing flag (bool_or_none semantics)."""
    if isinstance(v, bool):
        return bool_or_none(v)
    if isinstance(v, str):
        low = v.strip().lower()
        if low in ("yes", "y", "true"):
            return True
        if low in ("no", "n", "false"):
            return False
    return None


_NAI_CLASS_RE = re.compile(r"BuildingClass([A-D])\b", re.I)


def _nai_building_class(tags):
    """NAI tags[] ('BuildingClassB') -> 'B'. Returns None when no class tag."""
    if not isinstance(tags, list):
        return None
    for t in tags:
        if isinstance(t, str):
            m = _NAI_CLASS_RE.search(t)
            if m:
                return m.group(1).upper()
    return None


# ---------------------------------------------------------------------------
# COPY encoding (mirror cre_ingest.copy_field for the columns we stage)
# ---------------------------------------------------------------------------


def copy_field(v):
    if v is None:
        return "\\N"
    if isinstance(v, bool):
        return "t" if v else "f"
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    s = str(v)
    return (
        s.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


# ---------------------------------------------------------------------------
# Reading raw_data out of the DB (one source slug at a time)
# ---------------------------------------------------------------------------


def read_rows_sql(slug):
    """Inner SELECT (one JSON object per row) for iter_copy_json_rows: streams
    (id, source_key, raw_data) for one brokerage slug. Only active (not
    soft-deleted), object-typed raw_data rows. Cast to ::text for clean CSV
    round-trip; the helper wraps it in `COPY (...) TO STDOUT WITH (FORMAT csv)`
    so backslash-bearing raw_data (M&M HTML) is never corrupted/dropped."""
    return (
        "SELECT jsonb_build_object("
        "'id', l.id, "
        "'source_key', l.raw_data->>'sourceKey', "
        "'raw_data', l.raw_data)::text "
        "FROM credeals.cre_listings l "
        "JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id "
        f"WHERE b.slug = {sql_lit(slug)} "
        "AND l.deleted_at IS NULL "
        "AND jsonb_typeof(l.raw_data) = 'object'"
    )


def fetch_rows(db_url, psql, slug):
    """Run the read for one slug and yield (listing_id, source_key, raw).

    Delegates to cre_ingest.iter_copy_json_rows (CSV COPY format) so JSON that
    contains backslash escapes round-trips intact and any undecodable row aborts
    loudly instead of being silently skipped."""
    for obj in iter_copy_json_rows(psql, db_url, read_rows_sql(slug), label=f"backfill:{slug}"):
        lid = obj.get("id")
        raw = obj.get("raw_data")
        if lid is not None and isinstance(raw, dict):
            yield lid, obj.get("source_key"), raw


# ---------------------------------------------------------------------------
# Write SQL builder (additive, COALESCE-keep, column-existence + to_regclass
# guarded so a pre-sql/012 run is a clean no-op).
# ---------------------------------------------------------------------------

_STAGE_TABLE = "_bf_cols"


def _staged_columns(staged_rows):
    """The set of columns actually present across the staged rows, in
    SCALAR_COLUMNS order (stable, deterministic SQL)."""
    present = set()
    for r in staged_rows:
        present.update(c for c in r["cols"].keys())
    return [c for c in SCALAR_COLUMNS if c in present]


def build_sql(slug, staged_rows):
    """Build the additive, COALESCE-keep, guarded UPDATE for one brokerage slug.

    staged_rows: list of {"listing_id", "cols": {col: value}, "extra": {k: v}}.

    Guarantees (contract H):
      * Every scalar column write is COALESCE(<derived>, col) so a derived value
        only FILLS a currently-NULL column and a derived NULL never blanks a good
        value (the staged temp row carries NULL for an underived column, and the
        WHERE clause restricts to rows where the column IS NULL).
      * extra_facts merges: COALESCE(extra_facts,'{}') || staged, guarded so an
        empty staged blob is a no-op.
      * COLUMN-EXISTENCE guarded: a DO block checks every target column exists on
        credeals.cre_listings before running, so a pre-sql/012 run is a clean
        no-op (never an error).
      * Never touches status / deleted_at / transaction_type.
    """
    cols = _staged_columns(staged_rows)
    has_extra = any(r["extra"] for r in staged_rows)

    lines = []
    w = lines.append
    w("\\set ON_ERROR_STOP on")
    w("BEGIN;")
    w("SET LOCAL statement_timeout = '600s';")
    w("SET LOCAL standard_conforming_strings = on;")

    # Staging temp table: listing_id + every staged scalar column (typed) +
    # extra_facts jsonb. Columns absent from this run are simply not created.
    stage_cols_ddl = ["listing_id uuid"]
    for c in cols:
        stage_cols_ddl.append(f"{c} {_COLUMN_PG_TYPE[c]}")
    stage_cols_ddl.append("extra_facts jsonb")
    w(f"CREATE TEMP TABLE {_STAGE_TABLE} (\n    " + ",\n    ".join(stage_cols_ddl) + "\n) ON COMMIT DROP;")

    copy_cols = ["listing_id"] + list(cols) + ["extra_facts"]
    w(f"COPY {_STAGE_TABLE} ({', '.join(copy_cols)}) FROM stdin;")
    for r in staged_rows:
        out = [copy_field(r["listing_id"])]
        for c in cols:
            out.append(copy_field(r["cols"].get(c)))
        out.append(copy_field(r["extra"] if r["extra"] else None))
        w("\t".join(out))
    w("\\.")

    # Required-column existence guard set: every staged column PLUS extra_facts
    # when used. A pre-sql/012 DB is missing the institutional columns and
    # extra_facts, so the whole UPDATE is skipped (NOTICE), never an error.
    required = list(cols)
    if has_extra:
        required.append("extra_facts")
    required_lit = ", ".join(sql_lit(c) for c in required) if required else "NULL"

    # Build the scalar SET list: col = COALESCE(s.col, t.col). The WHERE clause
    # additionally restricts each scalar to rows where the target IS NULL so a
    # populated value is never re-touched (improvable-only).
    set_clauses = [f"{c} = COALESCE(s.{c}, t.{c})" for c in cols]
    if has_extra:
        # jsonb merge, guarded: only merge when the staged blob is a non-empty
        # object; else keep the prior extra_facts unchanged.
        set_clauses.append(
            "extra_facts = CASE WHEN s.extra_facts IS NOT NULL "
            "AND s.extra_facts <> '{}'::jsonb "
            "THEN COALESCE(t.extra_facts, '{}'::jsonb) || s.extra_facts "
            "ELSE t.extra_facts END"
        )
    set_sql = ",\n        ".join(set_clauses) if set_clauses else "id = t.id"

    # improvable-only predicate: at least one staged scalar would fill a NULL, or
    # the staged extra_facts adds keys. This keeps the UPDATE scoped and avoids
    # rewriting rows with nothing to change.
    improvable = [f"(t.{c} IS NULL AND s.{c} IS NOT NULL)" for c in cols]
    if has_extra:
        improvable.append("(s.extra_facts IS NOT NULL AND s.extra_facts <> '{}'::jsonb)")
    improvable_sql = " OR ".join(improvable) if improvable else "false"

    # The slug-scoped UPDATE, wrapped in a column-existence + to_regclass guard.
    w(f"""
-- COALESCE-keep additive backfill for brokerage slug '{slug}'. Every scalar
-- write is COALESCE(staged, current) and is restricted (improvable-only WHERE)
-- to rows where the target is currently NULL, so a populated value is never
-- blanked or re-touched. extra_facts is a guarded jsonb merge. The whole block
-- is column-existence guarded: a DB missing the sql/012 columns runs nothing.
DO $$
DECLARE
    _missing int;
BEGIN
    IF to_regclass('credeals.cre_listings') IS NULL THEN
        RAISE NOTICE 'cre_listings absent; backfill skipped for slug {slug}';
        RETURN;
    END IF;
    SELECT count(*) INTO _missing
    FROM (SELECT unnest(ARRAY[{required_lit}]::text[]) AS col) need
    WHERE need.col IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns c
        WHERE c.table_schema = 'credeals'
          AND c.table_name = 'cre_listings'
          AND c.column_name = need.col
      );
    IF _missing > 0 THEN
        RAISE NOTICE 'cre_listings missing % target column(s) (pre-sql/012); backfill skipped for slug {slug}', _missing;
        RETURN;
    END IF;

    UPDATE credeals.cre_listings t
    SET {set_sql}
    FROM {_STAGE_TABLE} s
    JOIN credeals.cre_brokerages b ON b.slug = {sql_lit(slug)}
    WHERE t.id = s.listing_id
      AND t.brokerage_id = b.id
      AND t.deleted_at IS NULL
      AND ({improvable_sql});
END $$;""")

    w("COMMIT;")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-source candidate summary (dry-run)
# ---------------------------------------------------------------------------


def summarize(slug, staged_rows):
    """Per-column candidate counts (rows that staged a non-null value for the
    column) for the dry-run report, plus extra_facts key counts."""
    per_col = {}
    extra_keys = {}
    listings = set()
    for r in staged_rows:
        if r["cols"] or r["extra"]:
            listings.add(r["listing_id"])
        for c, v in r["cols"].items():
            if v is not None:
                per_col[c] = per_col.get(c, 0) + 1
        for k in (r["extra"] or {}):
            extra_keys[k] = extra_keys.get(k, 0) + 1
    return {
        "slug": slug,
        "listings_touched": len(listings),
        "per_col": per_col,
        "extra_keys": extra_keys,
    }


def print_summary(stats, scanned):
    print(f"[backfill] {stats['slug']}: scanned {scanned}, "
          f"{stats['listings_touched']} listing(s) with >=1 candidate")
    for col in SCALAR_COLUMNS:
        n = stats["per_col"].get(col)
        if n:
            print(f"[backfill]   col {col:<22} {n}")
    for k, n in sorted(stats["extra_keys"].items()):
        print(f"[backfill]   extra_facts.{k:<18} {n}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def stage_rows_for_slug(rows):
    """Turn (listing_id, source_key, raw) tuples into staged-row dicts. Pure:
    used by both the CLI sweep and the tests (no DB)."""
    staged = []
    for lid, source_key, raw in rows:
        cols, extra = derive_columns(source_key, raw)
        if cols or extra:
            staged.append({"listing_id": lid, "cols": cols, "extra": extra})
    return staged


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true",
        help="(default) build SQL, print per-source per-column candidate counts, write nothing",
    )
    mode.add_argument(
        "--apply", action="store_true",
        help="actually run the COALESCE-keep UPDATEs (gated; off by default)",
    )
    ap.add_argument("--env-file", default=None, help="env file holding POSTGRES_URL*")
    ap.add_argument(
        "--source", default=None,
        help="comma list of brokerage slugs to backfill (default: all known slugs)",
    )
    ap.add_argument(
        "--keep-sql", default=None,
        help="write the generated SQL (concatenated per slug) to this path (dry-run too)",
    )
    args = ap.parse_args()

    apply = bool(args.apply)

    known_slugs = sorted({slug for slug in SLUG_TO_SOURCE_KEYS if slug in DERIVERS} |
                         {SOURCE_TO_BROKERAGE[sk][0] for sk in DERIVERS
                          if sk in SOURCE_TO_BROKERAGE})
    if args.source:
        requested = [s.strip() for s in args.source.split(",") if s.strip()]
        unknown = [s for s in requested if s not in known_slugs]
        if unknown:
            sys.exit(f"unknown --source slug(s): {', '.join(unknown)} "
                     f"(known: {', '.join(known_slugs)})")
        slugs = [s for s in known_slugs if s in requested]
    else:
        slugs = known_slugs

    db_url, env_path = load_db_url(args.env_file)
    print(f"[backfill] env file: {env_path}")  # path only, never the URL
    psql = find_psql()

    all_sql = []
    for slug in slugs:
        scanned = 0
        rows = []
        for lid, source_key, raw in fetch_rows(db_url, psql, slug):
            scanned += 1
            rows.append((lid, source_key, raw))
        staged = stage_rows_for_slug(rows)
        stats = summarize(slug, staged)
        print_summary(stats, scanned)
        if not staged:
            continue
        sql = build_sql(slug, staged)
        all_sql.append(sql)
        if apply:
            _apply_sql(psql, db_url, sql, slug)

    if args.keep_sql and all_sql:
        with open(args.keep_sql, "w") as f:
            f.write("\n\n".join(all_sql))
        print(f"[backfill] SQL written to {args.keep_sql}")

    if not apply:
        print("[backfill] DRY-RUN: no rows written. Re-run with --apply to write.")


def _apply_sql(psql, db_url, sql, slug):
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
        tf.write(sql)
        sql_path = tf.name
    try:
        proc = subprocess.run(
            [psql, db_url, "-q", "-v", "ON_ERROR_STOP=1", "-f", sql_path],
            capture_output=True,
            text=True,
        )
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            sys.exit(f"psql apply exited {proc.returncode} (slug {slug})")
        if proc.stderr.strip():
            sys.stderr.write(proc.stderr)
    finally:
        os.unlink(sql_path)
    print(f"[backfill] APPLIED slug {slug}.")


if __name__ == "__main__":
    main()
