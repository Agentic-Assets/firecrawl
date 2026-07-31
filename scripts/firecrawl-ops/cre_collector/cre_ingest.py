#!/usr/bin/env python3
"""
cre_ingest.py: load collect.ts output JSON into the Supabase `credeals` schema.

Python stdlib only. Connects with psql (Homebrew libpq or PATH) using
POSTGRES_URL_NON_POOLING / POSTGRES_URL read from an env file at runtime.
Credential values are never printed and never written into artifacts.

Pipeline per run:
  1. Read one or more collector JSON files (collect.ts --out artifacts).
  2. Normalize each listing to cre_listings columns; merge duplicate
     (brokerage, external_id) rows within the batch (a property collected in
     both the sale and the lease pass becomes transaction_type=sale_or_lease).
  3. Emit a single SQL script: temp staging tables loaded via inline COPY,
     then an upsert merge into credeals.cre_listings keyed on
     (brokerage_id, external_id), child-table refresh (contacts, documents,
     images), and a cre_scrape_jobs row per brokerage.
  4. Run it with psql -v ON_ERROR_STOP=1 inside one transaction.

Usage:
  python3 cre_ingest.py --in ./out/run.json
  python3 cre_ingest.py --in run1.json --in run2.json --dry-run
  python3 cre_ingest.py --in ./out/run.json --mark-missing   # full runs only

Source-to-brokerage mapping: sub-sources fold into their parent brokerage with
a prefixed external_id (cbre-dealflow -> cbre, "dealflow:<id>"; jll-investor ->
jll, "investor:<id>"; colliers-main -> colliers, "main:<id>"). `savills`
requires the savills seed row in credeals.cre_brokerages
(sql/001_cre_brokerages.sql).
"""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Optional  # used in the "Optional[str]" string return annotations
from urllib.parse import parse_qsl, quote, unquote_to_bytes, urlsplit, urlunsplit

# Phase-2 data-lift: the shared text parsers live in cre_parse.py (the Python
# mirror of lib/parse.ts), so the ingest, the monitor, and the WS2 backfill all
# share ONE implementation proven identical to TS via the golden test vectors.
# cre_parse is import-safe (stdlib only, no I/O). The existing ingest helpers
# below (parse_lease_rates, parse_money, parse_size_text, is_sale_psf_text)
# delegate to it without changing their observable to_row()-level behavior.
import cre_parse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default search paths for the EQUIRE env file that holds POSTGRES_URL*.
# These assume the standard ~/Documents layout. On any other machine (or when
# the EQUIRE repo lives elsewhere), set CRE_ENV_FILE or pass --env-file instead
# of editing this list; both are tried before these defaults (see load_db_url).
ENV_FILE_CANDIDATES = [
    "~/Documents/GitHub/agentic-assets/dynamically-display-cre-listing-data/.env.local",
    "~/Documents/GitHub/agentic-assets/CRE_EQUIRE/.env.local",
]

PSQL_CANDIDATES = [
    os.environ.get("PSQL_BIN") or "",
    "/opt/homebrew/opt/libpq/bin/psql",
    "/usr/local/opt/libpq/bin/psql",
]

# sourceKey -> (brokerage slug, external_id prefix)
SOURCE_TO_BROKERAGE = {
    "cbre": ("cbre", ""),
    "cbre-dealflow": ("cbre", "dealflow:"),
    "jll": ("jll", ""),
    "jll-investor": ("jll", "investor:"),
    "cushman-wakefield": ("cushman-wakefield", ""),
    "colliers": ("colliers", ""),
    "colliers-main": ("colliers", "main:"),
    "newmark": ("newmark", ""),
    "marcus-millichap": ("marcus-millichap", ""),
    "avison-young": ("avison-young", ""),
    "savills": ("savills", ""),
    "svn": ("svn", ""),
    "nai-global": ("nai-global", ""),
    "lee-associates": ("lee-associates", ""),
    "transwestern": ("transwestern", ""),
    "matthews": ("matthews", ""),
    "franklin-street": ("franklin-street", ""),
    "srs": ("srs", ""),
    "hanley": ("hanley", ""),
    "kidder-mathews": ("kidder-mathews", ""),
    "interra-realty": ("interra-realty", ""),
    "essex-realty": ("essex-realty", ""),
    "pyramid-brokerage": ("pyramid-brokerage", ""),
    "daum-commercial": ("daum-commercial", ""),
    "foundry-commercial": ("foundry-commercial", ""),
    "lyon-stahl": ("lyon-stahl", ""),
    "faris-lee": ("faris-lee", ""),
    "fortis-net-lease": ("fortis-net-lease", ""),
    "unique-properties": ("unique-properties", ""),
    "kiser-group": ("kiser-group", ""),
    "pinnacle-rea": ("pinnacle-rea", ""),
    "cawley-chicago": ("cawley-chicago", ""),
    "bradford-allen": ("bradford-allen", ""),
    "hudson-peters": ("hudson-peters", ""),
    "gibson-commercial": ("gibson-commercial", ""),
    "leibsohn": ("leibsohn", ""),
    "nai-hiffman": ("nai-hiffman", ""),
    "nai-martens": ("nai-martens", ""),
    "bull-realty": ("bull-realty", ""),
    "tri-commercial": ("tri-commercial", ""),
    "berger-commercial": ("berger-commercial", ""),
    "nai-bergman": ("nai-bergman", ""),
    "nai-isaac": ("nai-isaac", ""),
    "trinity-partners": ("trinity-partners", ""),
    "metro-commercial": ("metro-commercial", ""),
    "33-realty": ("33-realty", ""),
    "nai-hallmark": ("nai-hallmark", ""),
    "nai-plotkin": ("nai-plotkin", ""),
    "greysteel": ("greysteel", ""),
    "nai-talcor": ("nai-talcor", ""),
    "nai-dominion": ("nai-dominion", ""),
}

# Provider cards that cannot yet prove a canonical listing identity are kept in
# cre_source_index under an explicitly provisional namespace.  Reconciliation
# is source-specific: each namespace has its own full-scope watermark so a
# newer empty snapshot for one provider cannot block or authorize another.
INVENTORY_ONLY_SOURCE_DEFINITIONS = {
    "cbre-dealflow": {
        "slug": "cbre",
        "external_id_like": "dealflow:card:%",
        "watermark_external_id": "dealflow:scope:inventory-only-watermark",
        "watermark_url": "https://www.cbredealflow.com/",
        "watermark_fingerprint": "inventory-only-scope-watermark-v1",
    },
    "colliers": {
        "slug": "colliers",
        "external_id_like": "salestracker:card:%",
        "watermark_external_id": "salestracker:scope:inventory-only-watermark",
        "watermark_url": "https://sales.colliers.com/",
        "watermark_fingerprint": (
            "inventory-only-scope-watermark-v1:colliers-salestracker"
        ),
    },
}

BUILDOUT_SOURCE_KEYS = {
    "svn",
    "lee-associates",
    "franklin-street",
    "faris-lee",
    "fortis-net-lease",
    "unique-properties",
    "kiser-group",
    "pinnacle-rea",
    "cawley-chicago",
    "bradford-allen",
    "hudson-peters",
    "gibson-commercial",
    "leibsohn",
    "nai-hiffman",
    "nai-martens",
    "bull-realty",
    "tri-commercial",
    "berger-commercial",
    "nai-bergman",
    "nai-isaac",
    "trinity-partners",
    "metro-commercial",
    "33-realty",
    "nai-hallmark",
    "nai-plotkin",
    "greysteel",
    "nai-talcor",
    "nai-dominion",
}
CUSHMAN_SOURCE_KEY = "cushman-wakefield"
CUSHMAN_CANONICAL_HOST = "www.cushmanwakefield.com"
CUSHMAN_ONECAP_HOST = "onecap.cushmanwakefield.com"
CUSHMAN_AZURE_HOST = "cw-prod-gblgws-a-cm.azurewebsites.net"
CHILD_PRESERVING_AUTHORITATIVE_FEED_SOURCE_KEYS = BUILDOUT_SOURCE_KEYS | {
    "cushman-wakefield",
    "srs",
    "hanley",
    "kidder-mathews",
    "newmark",
    "interra-realty",
}
AUTHORITATIVE_INVENTORY_FEED_SOURCE_KEYS = (
    BUILDOUT_SOURCE_KEYS
    | CHILD_PRESERVING_AUTHORITATIVE_FEED_SOURCE_KEYS
    | {"cbre"}
)
STRICT_FRESHNESS_SOURCE_KEYS = {
    "cbre",
    "jll",
    "jll-investor",
    "colliers-main",
    "cushman-wakefield",
    "svn",
    "lee-associates",
    "franklin-street",
    "newmark",
    "savills",
    "transwestern",
    "marcus-millichap",
    "nai-global",
    "matthews",
    "srs",
    "hanley",
    "kidder-mathews",
    "interra-realty",
    "essex-realty",
    "pyramid-brokerage",
    "daum-commercial",
    "foundry-commercial",
    "lyon-stahl",
    "faris-lee",
    "fortis-net-lease",
    "unique-properties",
    "kiser-group",
    "pinnacle-rea",
    "cawley-chicago",
    "bradford-allen",
    "hudson-peters",
    "gibson-commercial",
    "leibsohn",
    "nai-hiffman",
    "nai-martens",
    "bull-realty",
    "tri-commercial",
    "berger-commercial",
    "nai-bergman",
    "nai-isaac",
    "trinity-partners",
    "metro-commercial",
    "33-realty",
    "nai-hallmark",
    "nai-plotkin",
    "greysteel",
    "nai-talcor",
    "nai-dominion",
}
MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=5)
CHILD_COUNT_MIN_BASE = 10
CHILD_COUNT_RETAIN_NUMERATOR = 7
CHILD_COUNT_RETAIN_DENOMINATOR = 10

SOURCE_KEYS_BY_SLUG = {}
for _source_key, (_slug, _prefix) in SOURCE_TO_BROKERAGE.items():
    SOURCE_KEYS_BY_SLUG.setdefault(_slug, set()).add(_source_key)


def source_key_sql(listing_alias="l", brokerage_alias="b"):
    """Return the canonical source-key expression shared by ingest validation."""
    return f"""
CASE
  WHEN {brokerage_alias}.slug = 'cbre'
    AND {listing_alias}.external_id LIKE 'dealflow:%' THEN 'cbre-dealflow'
  WHEN {brokerage_alias}.slug = 'jll'
    AND {listing_alias}.external_id LIKE 'investor:%' THEN 'jll-investor'
  WHEN {brokerage_alias}.slug = 'colliers'
    AND {listing_alias}.external_id LIKE 'main:%' THEN 'colliers-main'
  ELSE COALESCE(
    NULLIF(
      {listing_alias}.raw_data #>> '{{latestInventoryObservation,sourceKey}}',
      ''
    ),
    NULLIF(
      {listing_alias}.raw_data
        #>> '{{latestInventoryObservation,primary,sourceKey}}',
      ''
    ),
    NULLIF(
      {listing_alias}.raw_data
        #>> '{{latestInventoryObservation,secondary_pass,sourceKey}}',
      ''
    ),
    NULLIF({listing_alias}.raw_data->>'sourceKey', ''),
    NULLIF({listing_alias}.raw_data #>> '{{primary,sourceKey}}', ''),
    NULLIF({listing_alias}.raw_data #>> '{{secondary_pass,sourceKey}}', ''),
    {brokerage_alias}.slug
  )
END
"""


def child_count_regressed(before, after):
    """Match the checkpoint quality gate for severe aggregate child loss."""
    retained_threshold = (
        before * CHILD_COUNT_RETAIN_NUMERATOR // CHILD_COUNT_RETAIN_DENOMINATOR
    )
    return (
        before >= CHILD_COUNT_MIN_BASE
        and after < retained_threshold
    )


# Ordered keyword -> property_type enum. First match wins.
PROPERTY_TYPE_RULES = [
    ("mixed", "mixed_use"),
    ("multifamily", "multifamily"),
    ("multi-family", "multifamily"),
    ("apartment", "multifamily"),
    ("residential income", "multifamily"),
    ("senior", "multifamily"),
    ("student housing", "multifamily"),
    ("hotel", "hospitality"),
    ("hospitality", "hospitality"),
    ("motel", "hospitality"),
    ("resort", "hospitality"),
    ("medical", "office"),
    ("office", "office"),
    ("coworking", "office"),
    ("retail", "retail"),
    ("restaurant", "retail"),
    ("storefront", "retail"),
    ("shopping", "retail"),
    ("strip center", "retail"),
    ("industrial", "industrial"),
    ("warehouse", "industrial"),
    ("flex", "industrial"),
    ("manufacturing", "industrial"),
    ("distribution", "industrial"),
    ("logistics", "industrial"),
    ("r&d", "industrial"),
    ("land", "land"),
    ("development site", "land"),
    ("lot", "land"),
    ("self storage", "special_purpose"),
    ("self-storage", "special_purpose"),
    ("church", "special_purpose"),
    ("school", "special_purpose"),
    ("data center", "special_purpose"),
    ("special", "special_purpose"),
    ("healthcare", "special_purpose"),
    ("automotive", "special_purpose"),
    ("car wash", "special_purpose"),
    ("gas station", "special_purpose"),
]

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "puerto rico": "PR",
}
STATE_CODES = set(US_STATES.values())

SQFT_PER_ACRE = 43560

# ---------------------------------------------------------------------------
# Field normalizers
# ---------------------------------------------------------------------------


def norm_state(v):
    if not v or not isinstance(v, str):
        return None
    s = v.strip()
    if len(s) == 2 and s.upper() in STATE_CODES:
        return s.upper()
    return US_STATES.get(s.lower())


def http_url_or_none(v):
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not re.match(r"^https?://", s, re.I):
        return None
    return s


def norm_property_type(asset_type):
    if not asset_type or not isinstance(asset_type, str):
        return None
    low = asset_type.lower()
    for kw, enum in PROPERTY_TYPE_RULES:
        if kw in low:
            return enum
    return "other"


def norm_cap_rate(v):
    """capRatePct is usually a percent (6.5); occasionally already a fraction."""
    if not isinstance(v, (int, float)) or v <= 0:
        return None
    if v <= 1:
        frac = float(v)
    elif v < 30:
        frac = v / 100.0
    else:
        return None
    return round(frac, 6) if 0 < frac < 0.5 else None


def parse_lease_rates(text):
    """Conservative $/SF/year (min, max) from free-text lease rates.

    Thin shim over cre_parse.parse_lease_rate (the golden-vector source of
    truth). Drops the basis-type third element so the existing two-tuple call
    sites (to_row, merge_rows neighbors) are unchanged. cre_parse.parse_lease_rate
    SUPERSEDES the prior inline logic (contract C.1): it additionally trusts a
    bare "$N" per-SF field (M&M / Cushman "Rent Per SF") and keeps the first
    per-SF value of a dual "$19 ($10/SF NNN)" rate.
    """
    lo, hi, _type = cre_parse.parse_lease_rate(text)
    return lo, hi


def parse_money(text):
    return cre_parse.parse_money(text)


def is_sale_psf_text(text):
    return cre_parse.is_per_sf_text(text)


def parse_size_text(text):
    """(size_sf, lot_size_sf) parsed from a free-text size summary.

    Delegates to cre_parse.parse_size_text (identical to lib/parse.ts)."""
    return cre_parse.parse_size_text(text)


def num_or_none(v, lo=0, hi=None):
    if not isinstance(v, (int, float)):
        return None
    if v <= lo:
        return None
    if hi is not None and v > hi:
        return None
    return float(v)


def iso_date_or_none(v):
    if not v or not isinstance(v, str):
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", v.strip())
    return m.group(0) if m else None


def clean_text(v, limit=None):
    """Trimmed non-empty string, or None. Optional length cap."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    return s[:limit] if limit else s


def str_array_or_none(v, limit=200):
    """List of trimmed non-empty strings (deduped, order-preserving), or None.

    Used for highlights[] / amenities[] lift into cre_listings text[] columns. A
    non-list, or a list with no usable strings, yields None so COALESCE-keep in
    the upsert never blanks a prior good array.
    """
    if not isinstance(v, list):
        return None
    out = []
    seen = set()
    for item in v:
        s = item.strip() if isinstance(item, str) else None
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s[:limit])
    return out or None


def norm_occupancy_rate(v):
    """Occupancy as a fraction in (0, 1], or None. Accepts 0-100 percent or a
    fraction; mirrors norm_cap_rate's percent-or-fraction tolerance."""
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
        return None
    frac = float(v) if v <= 1 else v / 100.0
    return round(frac, 6) if 0 < frac <= 1 else None


def norm_lease_rate_type(v):
    """Map a free-text lease-rate type to one of the four enum tokens allowed by
    the cre_listings.lease_rate_type CHECK ('nnn','modified_gross','gross',
    'full_service'), or None for anything unrecognized.

    Clamping unknowns to None (not 'other', which is NOT in this CHECK) keeps the
    COALESCE-keep upsert from clobbering a prior good value and guarantees the
    CHECK never trips. Mirrors norm_property_type / the doc_type CASE-map clamp."""
    if not isinstance(v, str):
        return None
    low = v.strip().lower()
    if not low:
        return None
    # Order matters: check the more specific variants before the bare "gross".
    if "modified gross" in low or "mod gross" in low or "modified_gross" in low:
        return "modified_gross"
    if "full service" in low or "full-service" in low or "fsg" in low or "full_service" in low:
        return "full_service"
    if "nnn" in low or "triple net" in low or "triple-net" in low:
        return "nnn"
    if "gross" in low:
        return "gross"
    return None


# Phase-2 data-lift institutional-field normalizers (contract Section B). Each
# clamps to None on anything unparseable so the COALESCE-keep upsert never
# clobbers a prior good value and the DB CHECKs (sql/012) never trip.


def norm_building_class(v):
    """Map a source class/subtype string to 'A'|'B'|'C'|'D' or None. Delegates to
    cre_parse.norm_building_class (identical to lib/parse.ts). Never infers a
    class from a property subtype with no class token."""
    return cre_parse.norm_building_class(v)


def int_or_none(v, lo=0, hi=None):
    """num_or_none then truncated to int. For dock_doors / drive_in_doors /
    num_rooms, which are integer columns (sql/012). None passes through."""
    n = num_or_none(v, lo=lo, hi=hi)
    return int(n) if n is not None else None


def bool_or_none(v):
    """True/False passthrough; anything else (including a truthy/falsy non-bool)
    is None so a missing flag never writes a definitive False (rail_served)."""
    return v if isinstance(v, bool) else None


def extra_facts_or_none(v):
    """A dict of snake_case-keyed long-tail facts, or None (contract A.1 / B).

    Drops null/empty values and non-string keys; coerces keys to stable
    snake_case strings. Returns None for a non-dict or an all-empty dict so the
    jsonb `||` merge in the upsert keeps the prior blob on an empty pass
    (COALESCE/merge-keep, invariant H). Never raises on odd input.
    """
    if not isinstance(v, dict):
        return None
    out = {}
    for k, val in v.items():
        if not isinstance(k, str):
            continue
        key = k.strip()
        if not key:
            continue
        if val is None:
            continue
        if isinstance(val, str):
            sval = val.strip()
            if not sval:
                continue
            out[key] = sval
        elif isinstance(val, bool):
            out[key] = val
        elif isinstance(val, (int, float)):
            out[key] = val
        elif isinstance(val, (list, dict)):
            if val:
                out[key] = val
        # other types are skipped
    return out or None


_OM_FACT_GROUPS = {"scalar", "unit_mix", "rent_roll"}

# The retired Firecrawl OM parser writes review-only artifacts.  They must never
# be treated as collector input: GetCREdata is the sole production OM writer.
# Keep this marker at the envelope level so the CLI can reject the whole file,
# and retain the row-shape guard below for pre-marker artifacts that may already
# exist on an operator machine.
RETIRED_OM_PARSE_ARTIFACT_KIND = "retired_om_parse_dry_run"


def om_facts_rows(v):
    """Legacy OM-facts serializer retained only for regression contracts.

    GetCREdata is the sole production OM writer. Firecrawl ingestion discards
    this payload in both to_row() and build_sql(). This pure helper remains so
    migration and parser-regression tests can validate the historical
    provenance shape without restoring a writer path.
    """
    if not isinstance(v, list):
        return []
    out = []
    for f in v:
        if not isinstance(f, dict):
            continue
        fact_key = clean_text(f.get("factKey") or f.get("fact_key"), 128)
        source_doc_url = http_url_or_none(f.get("sourceDocUrl") or f.get("source_doc_url"))
        parser_version = clean_text(f.get("parserVersion") or f.get("parser_version"), 64)
        if not fact_key or not source_doc_url or not parser_version:
            continue  # provenance is required on every OM-derived row
        group = (f.get("factGroup") or f.get("fact_group") or "scalar")
        group = group if group in _OM_FACT_GROUPS else "scalar"
        conf = num_or_none(f.get("confidence"), lo=-0.0001, hi=1)
        if conf is not None and not (0 <= conf <= 1):
            conf = None
        out.append(
            {
                "factGroup": group,
                "factKey": fact_key,
                "factValueText": clean_text(f.get("factValueText") or f.get("fact_value_text"), 2000),
                "factValueNum": num_or_none(f.get("factValueNum") or f.get("fact_value_num"),
                                            lo=-1e15, hi=1e15),
                "unitCount": int_or_none(f.get("unitCount") or f.get("unit_count"), lo=-1, hi=1e6),
                "sourceDocUrl": source_doc_url,
                "parserVersion": parser_version,
                "confidence": conf,
            }
        )
    return out


def is_retired_om_parse_listing(listing):
    """Return true for the legacy OM-parser artifact row shape.

    Normal collector listings carry a native ``id``.  The retired parser emits
    ``externalId`` and an ``omFacts`` payload instead.  Rejecting that exact
    shape prevents both its OM-promoted parent scalars and the URL-hash fallback
    identity from reaching the staging table.  Enrichment queue items may carry
    ``externalId`` before TypeScript turns them into normal listings, but they
    never carry ``omFacts`` and therefore remain unaffected.
    """
    return (
        isinstance(listing, dict)
        and "externalId" in listing
        and "omFacts" in listing
    )


# ---------------------------------------------------------------------------
# Listing transformation
# ---------------------------------------------------------------------------

def to_inventory_only_row(listing, observed_at):
    """Map a current provider card that lacks a canonical listing URL.

    These rows belong in ``cre_source_index`` as enumeration evidence, not in
    ``cre_listings``. Their external IDs are explicitly provisional and never
    support automatic history continuity.
    """
    marker = listing.get("inventoryOnly")
    if not isinstance(marker, dict):
        return None
    source_key = listing.get("sourceKey")
    definition = INVENTORY_ONLY_SOURCE_DEFINITIONS.get(source_key)
    mapping = SOURCE_TO_BROKERAGE.get(source_key)
    raw_id = listing.get("id")
    index_url = marker.get("indexUrl")
    if (
        not definition
        or not mapping
        or raw_id is None
        or not str(raw_id).strip()
        or not isinstance(index_url, str)
        or index_url != definition["watermark_url"]
    ):
        return None
    slug, prefix = mapping
    external_id = prefix + str(raw_id).strip()
    expected_prefix = definition["external_id_like"].removesuffix("%")
    if slug != definition["slug"] or not external_id.startswith(expected_prefix):
        return None
    evidence = {
        "title": listing.get("name") or listing.get("title"),
        "city": listing.get("city"),
        "state": listing.get("state"),
        "assetType": listing.get("assetType"),
        "sizeText": listing.get("sizeText"),
        "status": listing.get("status") or listing.get("statusBadge"),
        "provisionalIdentity": listing.get("provisionalIdentity"),
    }
    fingerprint = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return {
        "slug": slug,
        "external_id": external_id,
        "source_key": source_key,
        "url": index_url,
        "fingerprint": fingerprint,
        "observed_status": clean_text(
            listing.get("status") or listing.get("statusBadge"), 128
        ),
        "observed_at": observed_at,
    }


def inventory_only_full_scopes(data):
    """Return strictly complete inventory-only namespaces safe to reconcile.

    A namespace is included only when the artifact proves an unlimited,
    error-free sale+lease full enumeration.  This makes absence meaningful:
    provisional rows missing from that exact run may be retired from
    ``cre_source_index`` without making any claim about canonical listings.
    """
    run_meta = data.get("runMeta")
    if not isinstance(run_meta, dict):
        return []
    started_raw = run_meta.get("startedAt")
    finished_raw = run_meta.get("finishedAt")
    try:
        started = datetime.fromisoformat(
            str(started_raw).replace("Z", "+00:00")
        )
        finished = datetime.fromisoformat(
            str(finished_raw).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return []
    if (
        started.tzinfo is None
        or finished.tzinfo is None
        or finished < started
    ):
        return []
    if (
        run_meta.get("mode") != "full"
        or run_meta.get("transactions") != ["sale", "lease"]
        or run_meta.get("maxItemsPerSource") is not None
    ):
        return []
    entries = data.get("sources")
    listings = data.get("listings")
    if (
        not isinstance(entries, list)
        or not isinstance(listings, list)
        or data.get("totalListings") != len(listings)
    ):
        return []

    scopes = []
    for source_key, definition in INVENTORY_ONLY_SOURCE_DEFINITIONS.items():
        matching = [
            entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("sourceKey") == source_key
        ]
        source_listings = [
            listing
            for listing in listings
            if isinstance(listing, dict)
            and listing.get("sourceKey") == source_key
        ]
        transactions = {entry.get("transaction") for entry in matching}
        if (
            len(matching) != 2
            or transactions != {"sale", "lease"}
            or any(entry.get("supported") is not True for entry in matching)
            or any(entry.get("error") for entry in matching)
            or any(entry.get("truncated") is not False for entry in matching)
            or any(
                not isinstance(entry.get("listingsCollected"), int)
                or entry["listingsCollected"] < 0
                for entry in matching
            )
            or any(
                listing.get("transactionMode") not in {"sale", "lease"}
                for listing in source_listings
            )
            or any(
                sum(
                    listing.get("transactionMode") == entry["transaction"]
                    for listing in source_listings
                )
                != entry["listingsCollected"]
                for entry in matching
            )
            or any(
                listing.get("inventoryOnly") is not None
                and to_inventory_only_row(listing, finished.isoformat()) is None
                for listing in source_listings
            )
        ):
            continue
        scopes.append(
            {
                **definition,
                "source_key": source_key,
                "observed_at": finished.isoformat(),
            }
        )
    return scopes


def transaction_type_of(listing):
    tt = (listing.get("transactionType") or "").lower()
    if "sale" in tt and ("lease" in tt or "let" in tt):
        return "sale_or_lease"
    mode = listing.get("transactionMode")
    return mode if mode in ("sale", "lease") else None


# ---------------------------------------------------------------------------
# Status normalization (change-tracking core; PURE-ADDITIVE)
#
# These symbols are imported by the change-tracking / monitor layer AND, since
# Phase-2 status activation (2026-06-13), by the production upsert path itself:
# to_row()/build_sql() now stage status, source_lastmod, and canonical_key.
# Design: cre-intelligence-system-design.md sections 6 and 12.5; board impact:
# cre-phase2-board-impact-2026-06-13.md.
#
# Two-tier read, modeled on transaction_type_of (read-explicit-then-fallback):
#   1. STATUS_SOURCE_PATHS gives an ordered list of dotted paths to each
#      source's native status signal, covering BOTH the flat listing dict and
#      the {primary, secondary_pass} dual shape produced by merge_rows().
#   2. STATUS_RULES is a conservative word-boundary TEXT fallback over only
#      title/name/headline/url-slug.
#
# Invariant: never default to "active". collect.ts prune() drops false/null
# (e.g. underContract:false, absent marcusFlags), so absence is NOT a status;
# norm_status() returns None when there is no signal. The INSERT path defaults
# to 'active' (COALESCE(status,'active')); the upsert UPDATE keeps existing
# status sticky (resetting only resurrected rows to 'active'); and a separate
# targeted UPDATE upgrades a row to its real signal when this run carries one.
# So a None here reads as "no opinion", never a downgrade to active (Choice a).
# ---------------------------------------------------------------------------

# Ordered word-boundary text -> canonical status. First match wins. Never
# produces "active" (active is the default elsewhere, never inferred here).
# Canonical statuses match the widened DB CHECK:
#   sold, under_contract, pending, leased, off_market.
STATUS_RULES = [
    (re.compile(r"\b(?:sold|closed)\b", re.I), "sold"),
    (re.compile(r"\b(?:under\s+contract|in\s+contract|under\s+offer)\b", re.I), "under_contract"),
    (re.compile(r"\b(?:sale\s+pending|pending)\b", re.I), "pending"),
    (re.compile(r"\bleased\b", re.I), "leased"),
    (re.compile(r"\b(?:withdrawn|off\s+market)\b", re.I), "off_market"),
]

# Terminal statuses (a value here from EITHER dual pass wins over a
# non-terminal / None signal from the other pass).
_TERMINAL_STATUSES = {"sold", "under_contract", "pending", "leased", "off_market"}

# Explicit per-(sourceKey x raw_data-shape) status signal map (design 12.5).
# Each value is an ordered list of dotted paths into a single (flat) listing
# dict. norm_status() applies these paths to the flat dict AND to each of the
# {primary, secondary_pass} sub-passes. A path may resolve to a string (mapped
# through STATUS_RULES) or to a boolean signal (handled by _STATUS_BOOL_PATHS).
# Sources with NO native status get an empty list -> disappearance-only tier
# (norm_status returns None) so their rows are never mislabeled (notably CBRE's
# ~19k rows, which emit no status field).
STATUS_SOURCE_PATHS = {
    # Status-transition tier (native signal present in raw_data).
    "jll-investor": ["status", "jllInvestorSearchRow.status", "jllInvestorDetail.stageName"],
    "nai-global": ["listingStatus"],
    "svn": ["closed", "underContract"],
    "lee-associates": ["closed", "underContract"],
    "faris-lee": ["closed", "underContract"],
    "fortis-net-lease": ["closed", "underContract"],
    "unique-properties": ["closed", "underContract"],
    "kiser-group": ["closed", "underContract"],
    "pinnacle-rea": ["closed", "underContract"],
    "cawley-chicago": ["closed", "underContract"],
    "bradford-allen": ["closed", "underContract"],
    "hudson-peters": ["closed", "underContract"],
    "gibson-commercial": ["closed", "underContract"],
    "leibsohn": ["closed", "underContract"],
    "nai-hiffman": ["closed", "underContract"],
    "nai-martens": ["closed", "underContract"],
    "bull-realty": ["closed", "underContract"],
    "tri-commercial": ["closed", "underContract"],
    "berger-commercial": ["closed", "underContract"],
    "nai-bergman": ["closed", "underContract"],
    "nai-isaac": ["closed", "underContract"],
    "trinity-partners": ["closed", "underContract"],
    "metro-commercial": ["closed", "underContract"],
    "33-realty": ["closed", "underContract"],
    "nai-hallmark": ["closed", "underContract"],
    "nai-plotkin": ["closed", "underContract"],
    "greysteel": ["closed", "underContract"],
    "nai-talcor": ["closed", "underContract"],
    "nai-dominion": ["closed", "underContract"],
    "colliers": ["status"],
    "cbre-dealflow": ["status", "cbreDealflowCard.status", "cbreDealflowDetail.status"],
    "cushman-wakefield": ["listingStatus", "rawCushmanApi.listing_status"],
    "colliers-main": ["status", "colliersMain.propertyStatus"],
    "matthews": ["statusBadge"],
    "franklin-street": ["rawBuildout.closed", "rawBuildout.under_contract"],
    "srs": ["statusBadge"],
    "hanley": ["statusBadge"],
    # Freshness-only flags, NOT terminal -> explicitly map to no signal.
    "marcus-millichap": [],
    # Disappearance-only tier (no native status field; lifecycle = vanishing).
    "cbre": [],
    "jll": [],
    "newmark": [],
    "avison-young": [],
    "savills": [],
    "transwestern": [],
    "kidder-mathews": [],
    "interra-realty": ["statusBadge"],
    "essex-realty": ["statusBadge"],
    "pyramid-brokerage": [],
    "daum-commercial": [],
    "foundry-commercial": ["statusBadge"],
    "lyon-stahl": ["statusBadge"],
}

# Phase-2 data-lift: a UNIVERSAL leading status path read for EVERY source
# before its per-source STATUS_SOURCE_PATHS list. Adapters surface a source
# status badge as `statusBadge`. Monitor enumeration stays byte-identical for a
# per-source reason, NOT a single blanket gate: newmark emits statusBadge only on
# the full path (gated behind `!monitor`, like its media/links promotion);
# buildout's badge is derived from the native `underContract` field that already
# drove norm_status pre-diff, so the monitor artifact's status value is unchanged;
# colliers-main monitor emits a sparse {id,url,lastmod} object that never carries
# the badge; jll / jll-investor / colliers-ST monitor return []. norm_status maps
# the badge through STATUS_RULES; the value is STILL subject to the OPT-IN,
# default-OFF activation gate (apply_status_activation_gate), so a badge NEVER
# auto-activates and is NEVER written directly to cre_listings.status. Kept
# SEPARATE from STATUS_SOURCE_PATHS (rather than prepended to every list) so the
# disappearance-only sources keep their literal [] classification (a source with
# no badge contributes no signal, exactly as before).
_STATUS_UNIVERSAL_PATHS = ["statusBadge"]

# Boolean status signals: a path that resolves to True maps to this canonical
# token (then run through STATUS_RULES); False/None contributes no signal
# (prune() has already stripped False, so absence is normal).
_STATUS_BOOL_PATHS = {
    "closed": "closed",
    "underContract": "under contract",
}

# Text fields scanned by the conservative fallback. Never the whole blob.
_STATUS_TEXT_FIELDS = ("title", "name", "headline")


def _dig(obj, dotted_path):
    """Resolve a dotted path against a dict, returning None if any hop misses."""
    cur = obj
    for part in dotted_path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _status_from_signal(value):
    """Map one resolved string signal to a canonical status, or None.

    Boolean source signals (closed/underContract) are coerced to their
    canonical token by the caller before reaching here.
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, str):
        value = str(value)
    s = value.strip()
    if not s:
        return None
    for rx, canonical in STATUS_RULES:
        if rx.search(s):
            return canonical
    return None


def _explicit_status_from_pass(sub, paths):
    """Best canonical status from one flat sub-pass given its source paths.

    A terminal status wins immediately; otherwise keep scanning the ordered
    paths. Boolean paths (closed/underContract) are coerced to their canonical
    token before STATUS_RULES.
    """
    if not isinstance(sub, dict):
        return None
    best = None
    for path in paths:
        raw = _dig(sub, path)
        if raw is None:
            continue
        if isinstance(raw, bool):
            if not raw:
                continue
            raw = _STATUS_BOOL_PATHS.get(path.split(".")[-1])
            if raw is None:
                continue
        status = _status_from_signal(raw)
        if status in _TERMINAL_STATUSES:
            return status
        if status and best is None:
            best = status
    return best


def _text_status_from_pass(sub):
    """Conservative word-boundary status scan of only title/name/headline/slug."""
    if not isinstance(sub, dict):
        return None
    for field in _STATUS_TEXT_FIELDS:
        v = sub.get(field)
        if isinstance(v, str) and v.strip():
            status = _status_from_signal(v)
            if status:
                return status
    url = sub.get("url")
    if isinstance(url, str) and url:
        slug = url.rsplit("/", 1)[-1].split("?")[0].replace("-", " ").replace("_", " ")
        status = _status_from_signal(slug)
        if status:
            return status
    return None


def norm_status(listing) -> "Optional[str]":
    """Canonical listing status, or None when there is no signal.

    Mirrors transaction_type_of's read-explicit-then-fallback shape:
      (a) read the source's native status via STATUS_SOURCE_PATHS, handling
          both the flat dict and the {primary, secondary_pass} dual shape; a
          terminal status from EITHER pass wins;
      (b) otherwise a conservative word-boundary scan of title/name/headline/
          url-slug only (never the whole blob);
      (c) otherwise None.

    Never returns "active": absence is no opinion, not a downgrade. prune()
    drops false/null upstream, so a missing flag must not be read as a status.
    """
    if not isinstance(listing, dict):
        return None
    source_key = listing.get("sourceKey")
    # The universal `statusBadge` path is read first for EVERY source (Phase-2
    # data-lift), then the source's native paths. A disappearance-only source
    # with no badge yields the same result as before (paths effectively empty).
    paths = _STATUS_UNIVERSAL_PATHS + STATUS_SOURCE_PATHS.get(source_key, [])

    # Both raw_data shapes: flat listing, plus dual {primary, secondary_pass}.
    subs = [listing]
    for nested_key in ("primary", "secondary_pass"):
        nested = listing.get(nested_key)
        if isinstance(nested, dict):
            subs.append(nested)

    # (a) Explicit native status. Terminal from any pass wins immediately.
    best_explicit = None
    if paths:
        for sub in subs:
            status = _explicit_status_from_pass(sub, paths)
            if status in _TERMINAL_STATUSES:
                return status
            if status and best_explicit is None:
                best_explicit = status
        if best_explicit is not None:
            return best_explicit

    # (b) Conservative text fallback over scoped fields only.
    for sub in subs:
        status = _text_status_from_pass(sub)
        if status:
            return status

    # (c) No signal: None, never "active".
    return None


def _canonical_key(listing) -> "Optional[str]":
    """Advisory re-listing key: lower(address)|state[|round(lat,4)].

    Geo-bearing rows use lower(trimmed address)|state|round(lat,4); geoless
    rows (e.g. all jll-investor) degrade to lower(address)|state only. Returns
    None when there is no address. Advisory only (design section 6, item 5).
    """
    if not isinstance(listing, dict):
        return None
    address = listing.get("address") or listing.get("street")
    if not isinstance(address, str) or not address.strip():
        return None
    addr = address.strip().lower()
    state = listing.get("state")
    state_part = state.strip().lower() if isinstance(state, str) else ""
    key = f"{addr}|{state_part}"
    lat = listing.get("lat")
    if lat is None:
        lat = listing.get("latitude")
    if isinstance(lat, (int, float)) and not isinstance(lat, bool):
        key = f"{key}|{round(float(lat), 4)}"
    return key


def parse_source_lastmod(value):
    """Parse a source lastmod / dateModified string to a full-precision ISO-8601
    string (timestamptz-castable), or None. Never day-truncates: whatever
    precision the source carries is preserved. Returns None when unparseable.

    Single source of truth for source_lastmod across the ingest upsert and the
    observe-only monitor (cre_monitor imports this so the two agree exactly).
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    candidate = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        return datetime.fromisoformat(candidate).isoformat()
    except ValueError:
        pass
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?", s)
    if m:
        cand = m.group(0).replace(" ", "T")
        try:
            datetime.fromisoformat(cand)  # reject out-of-range month/day/hour (e.g. 2024-13-45)
            return cand
        except ValueError:
            return None
    return None


def parse_strict_freshness_timestamp(value, *, field):
    """Parse a strict freshness timestamp as an absolute UTC instant."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty ISO-8601 timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} is not valid ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_strict_artifact_freshness(
    data,
    *,
    require_strict_freshness=False,
    now=None,
):
    """Reject a strict refresh artifact whose rows do not prove observation time.

    Artifact completion time is intentionally not evidence that a detail page
    was observed. Buildout is the one explicit exception because its paginated
    inventory feed is the authoritative listing payload.
    """
    run_meta = data.get("runMeta")
    freshness = run_meta.get("freshness") if isinstance(run_meta, dict) else None
    artifact_is_strict = (
        isinstance(freshness, dict)
        and freshness.get("requireFreshDetails") is True
    )
    if require_strict_freshness and not artifact_is_strict:
        raise ValueError(
            "strict freshness is explicitly required but the artifact "
            "does not assert requireFreshDetails=true"
        )
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    latest_allowed = current + MAX_FUTURE_CLOCK_SKEW
    for field in ("startedAt", "finishedAt"):
        value = run_meta.get(field) if isinstance(run_meta, dict) else None
        if value is None:
            continue
        artifact_timestamp = parse_strict_freshness_timestamp(
            value,
            field=f"runMeta.{field}",
        )
        if artifact_timestamp > latest_allowed:
            raise ValueError(
                f"runMeta.{field} exceeds the 5-minute clock-skew allowance"
            )
    for index, listing in enumerate(data.get("listings") or []):
        if not isinstance(listing, dict):
            continue
        observation_fields = [
            ("inventoryObservedAt", listing.get("inventoryObservedAt")),
            ("detailObservedAt", listing.get("detailObservedAt")),
        ]
        provenance = listing.get("freshnessProvenance")
        if isinstance(provenance, dict):
            observation_fields.append(
                (
                    "freshnessProvenance.validatedAt",
                    provenance.get("validatedAt"),
                )
            )
        for field, value in observation_fields:
            if value is None:
                continue
            observed = parse_strict_freshness_timestamp(
                value,
                field=f"listings[{index}].{field}",
            )
            if observed > latest_allowed:
                raise ValueError(
                    f"listings[{index}].{field} exceeds "
                    "the 5-minute clock-skew allowance"
                )
    if not artifact_is_strict:
        return
    generation_id = freshness.get("generationId")
    if not isinstance(generation_id, str) or not generation_id.strip():
        raise ValueError("strict freshness artifact lacks a valid generation id/start")
    boundary = parse_strict_freshness_timestamp(
        freshness.get("generationStartedAt"),
        field="runMeta.freshness.generationStartedAt",
    )
    if boundary > latest_allowed:
        raise ValueError(
            "runMeta.freshness.generationStartedAt exceeds "
            "the 5-minute clock-skew allowance"
        )
    for index, listing in enumerate(data.get("listings") or []):
        if not isinstance(listing, dict):
            raise ValueError(f"strict freshness listings[{index}] is not an object")
        provenance = listing.get("freshnessProvenance")
        if not isinstance(provenance, dict):
            raise ValueError(f"strict freshness listings[{index}] lacks provenance")
        if provenance.get("generationId") != generation_id:
            raise ValueError(f"strict freshness listings[{index}] has the wrong generation")
        if listing.get("detailError"):
            raise ValueError(f"strict freshness listings[{index}] has incomplete detail")
        inventory_observed = parse_strict_freshness_timestamp(
            listing.get("inventoryObservedAt"),
            field=f"listings[{index}].inventoryObservedAt",
        )
        if inventory_observed < boundary:
            raise ValueError(f"strict freshness listings[{index}] has stale inventory")
        if inventory_observed > latest_allowed:
            raise ValueError(
                f"strict freshness listings[{index}] inventory observation "
                "exceeds the 5-minute clock-skew allowance"
            )
        source_key = listing.get("sourceKey")
        preserves_children = listing.get("preserveChildCollections") is True
        if source_key in AUTHORITATIVE_INVENTORY_FEED_SOURCE_KEYS:
            if provenance.get("detailScope") != "authoritative_inventory_feed":
                raise ValueError(
                    f"strict freshness listings[{index}] lacks authoritative feed provenance"
                )
            if source_key in CHILD_PRESERVING_AUTHORITATIVE_FEED_SOURCE_KEYS:
                if not preserves_children:
                    raise ValueError(
                        f"strict freshness listings[{index}] must preserve child collections"
                    )
            elif preserves_children:
                raise ValueError(
                    f"strict freshness listings[{index}] must not preserve child collections"
                )
            continue
        if preserves_children:
            raise ValueError(
                f"strict freshness listings[{index}] must not preserve child collections"
            )
        detail_value = listing.get("detailObservedAt")
        if provenance.get("cacheDisposition") == "source_revision_cache":
            detail_value = provenance.get("validatedAt")
        if not detail_value:
            raise ValueError(f"strict freshness listings[{index}] has stale detail")
        detail_observed = parse_strict_freshness_timestamp(
            detail_value,
            field=(
                f"listings[{index}].freshnessProvenance.validatedAt"
                if provenance.get("cacheDisposition") == "source_revision_cache"
                else f"listings[{index}].detailObservedAt"
            ),
        )
        if detail_observed < boundary:
            raise ValueError(f"strict freshness listings[{index}] has stale detail")
        if detail_observed > latest_allowed:
            raise ValueError(
                f"strict freshness listings[{index}] detail observation "
                "exceeds the 5-minute clock-skew allowance"
            )


def group_source_lastmod(flat_listings):
    """First non-None parsed lastmod across a group's FLAT listings, preferring
    lastUpdated then dateModified. Mirrors the first-non-None rule used for
    canonical_key (within a sale+lease group these values agree)."""
    for listing in flat_listings:
        for field in ("lastUpdated", "dateModified"):
            parsed = parse_source_lastmod(listing.get(field))
            if parsed:
                return parsed
    return None


def canonical_cushman_identity_url(url):
    """Return the URL-v1 identity input for one Cushman property.

    Cushman's public search API GUID rotates independently of the public
    property URL. Identity therefore uses the normalized HTTPS property URL,
    while the GUID remains in raw_data as provider provenance.
    """
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url.strip())
        host = (parsed.hostname or "").lower()
        if host in {"sitecore-www.cushmanwakefield.com", CUSHMAN_AZURE_HOST}:
            host = CUSHMAN_CANONICAL_HOST
        if (
            parsed.scheme.lower() != "https"
            or host not in {CUSHMAN_CANONICAL_HOST, CUSHMAN_ONECAP_HOST}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
        ):
            return None
    except ValueError:
        return None
    path = parsed.path.rstrip("/") or "/"
    query = ""
    if host == CUSHMAN_ONECAP_HOST:
        record_ids = [
            re.sub(r"\s+", " ", value).strip()
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key == "recordId"
        ]
        if len(record_ids) != 1:
            return None
        record_id = record_ids[0]
        if not record_id:
            return None
        # Use an explicit byte-level encoding shared with TypeScript. Python's
        # urllib and WHATWG URLSearchParams disagree about whether "~" is
        # escaped, which would otherwise produce different identity hashes.
        encoded_record_id = "".join(
            chr(byte)
            if (
                ord("A") <= byte <= ord("Z")
                or ord("a") <= byte <= ord("z")
                or ord("0") <= byte <= ord("9")
                or byte in {ord("-"), ord("_"), ord(".")}
            )
            else f"%{byte:02X}"
            for byte in record_id.encode("utf-8")
        )
        query = f"recordId={encoded_record_id}"
    return urlunsplit(("https", host, path, query, ""))


def cushman_canonical_external_id(url):
    identity_url = canonical_cushman_identity_url(url)
    if identity_url is None:
        return None
    digest = hashlib.sha256(identity_url.encode("utf-8")).hexdigest()[:32]
    return f"url:v1:{digest}"


def to_row(listing, brokers_by_idx, scraped_at):
    """Map one collector listing to a staging row dict, or None to skip."""
    if is_retired_om_parse_listing(listing):
        return None
    # Inventory-only cards intentionally have no canonical listing identity or
    # URL. Keep this guard ahead of URL/id derivation so a future adapter change
    # cannot accidentally promote the shared provider index URL into
    # cre_listings. main() routes the row to cre_source_index instead.
    if listing.get("inventoryOnly") is not None:
        return None
    source_key = listing.get("sourceKey")
    mapping = SOURCE_TO_BROKERAGE.get(source_key)
    if not mapping:
        return None
    provenance = listing.get("freshnessProvenance")
    observation_scraped_at = parse_source_lastmod(listing.get("detailObservedAt"))
    if (
        isinstance(provenance, dict)
        and provenance.get("cacheDisposition") == "source_revision_cache"
    ):
        observation_scraped_at = (
            parse_source_lastmod(provenance.get("validatedAt"))
            or observation_scraped_at
        )
    if (
        observation_scraped_at is None
        and isinstance(provenance, dict)
        and provenance.get("detailScope") == "authoritative_inventory_feed"
    ):
        observation_scraped_at = parse_source_lastmod(
            listing.get("inventoryObservedAt")
        )
    slug, prefix = mapping

    url = listing.get("url")
    if not url or not isinstance(url, str) or not url.startswith("http"):
        return None  # source_url is NOT NULL; un-linked rows aren't actionable

    raw_id = listing.get("id")
    # Buildout sources list a dual-mode property twice
    # with distinct inventory ids; the URL propertyId base ("1614726-sale" /
    # "1614726-lease") is the stable per-property key, so the pair merges.
    buildout_pid = None
    if source_key in BUILDOUT_SOURCE_KEYS:
        m = re.search(r"[?&]propertyId=([^&#]+)", url)
        if m:
            buildout_pid = re.sub(r"-(sale|lease)$", "", m.group(1))
    if source_key == CUSHMAN_SOURCE_KEY:
        external_id = cushman_canonical_external_id(url)
        if external_id is None:
            return None
    elif buildout_pid:
        external_id = prefix + buildout_pid
    elif raw_id is not None and str(raw_id).strip():
        external_id = prefix + str(raw_id).strip()
    else:
        external_id = prefix + "url:" + hashlib.sha1(url.encode()).hexdigest()[:16]

    size_sf = num_or_none(listing.get("buildingSizeSqft"), hi=1e9)
    lot_acres = num_or_none(listing.get("lotSizeAcres"), hi=1e6)
    lot_size_sf = lot_acres * SQFT_PER_ACRE if lot_acres else None
    if size_sf is None or lot_size_sf is None:
        t_size, t_lot = parse_size_text(listing.get("sizeText"))
        size_sf = size_sf if size_sf is not None else t_size
        lot_size_sf = lot_size_sf if lot_size_sf is not None else t_lot

    sale_price_text = listing.get("salePriceText")
    sale_price = num_or_none(listing.get("salePriceUsd"), lo=100, hi=1e11)
    price_per_sf = num_or_none(listing.get("salePricePerSf"), lo=0, hi=10000)
    # (DQ guard 1) NAI 'POUND '-labeled price: the value is really USD with a wrong
    # currency LABEL (RAW_DATA_GAP doc). When salePriceUsd is absent/zero but the
    # text carries a stripped currency label, recover the numeric as USD. Scoped
    # to nai-global and only used as a fallback so a clean numeric is never altered.
    if sale_price is None and source_key == "nai-global" and isinstance(sale_price_text, str):
        sale_price = num_or_none(
            cre_parse.parse_amount_ignoring_currency_label(sale_price_text), lo=100, hi=1e11
        )
    # (DQ guard 6) Newmark 'Subject to Offer' / non-numeric price: num_or_none
    # already drops a non-numeric salePriceUsd, and parse_money only matches a real
    # "$N" token, so a phrase like "Subject to Offer" never promotes to a number.
    # No extra branch needed; the existing numeric clamps cover it.
    if is_sale_psf_text(sale_price_text):
        # (DQ guard 2) Lee salePriceUsd per-SF conflation: a per-SF sale text means
        # the absolute sale price must NOT be read; route it to sale_price_per_sf.
        price_per_sf = price_per_sf or num_or_none(parse_money(sale_price_text), lo=0, hi=10000)
        sale_price = None
    if sale_price and size_sf and size_sf > 100:
        price_per_sf = round(sale_price / size_sf, 2)

    # (DQ guard 3) AY $5000/SF/YR anomaly + the >500 $/SF/yr cap live in
    # cre_parse.parse_lease_rate, so parse_lease_rates returns (None, None) for them.
    lease_min, lease_max = parse_lease_rates(listing.get("leaseRateText"))
    # An adapter may pre-parse a cleaner lease rate than leaseRateText; prefer the
    # explicit leaseRateMin/Max when present (contract B), COALESCE-style.
    lease_min = lease_min if lease_min is not None else num_or_none(
        listing.get("leaseRateMin"), lo=0, hi=500
    )
    lease_max = lease_max if lease_max is not None else num_or_none(
        listing.get("leaseRateMax"), lo=0, hi=500
    )

    contacts = []
    source_contacts = listing.get("contactsDetailed") or []
    if source_contacts:
        for i, c in enumerate(source_contacts):
            if not isinstance(c, dict) or not (
                c.get("name")
                or c.get("email")
                or c.get("phone")
                or c.get("profileUrl")
                or c.get("avatarUrl")
                or c.get("vcardUrl")
            ):
                continue
            contacts.append(
                {
                    "name": c.get("name"),
                    "title": c.get("title"),
                    # Broker real-estate license string as printed (sql/012
                    # cre_listing_contacts.license). M&M emits it; others may not.
                    "license": clean_text(c.get("license"), 64),
                    "email": c.get("email"),
                    "phone": c.get("phone"),
                    "company": c.get("company") or listing.get("sourceCompany"),
                    "profileUrl": http_url_or_none(c.get("profileUrl")),
                    "avatarUrl": http_url_or_none(c.get("avatarUrl")),
                    "vcardUrl": http_url_or_none(c.get("vcardUrl")),
                    "isPrimary": i == 0,
                }
            )
        if not contacts:
            source_contacts = []
    if not source_contacts:
        for i, bid in enumerate(listing.get("brokerIds") or []):
            b = brokers_by_idx.get(bid)
            if not b or not (b.get("name") or b.get("email")):
                continue
            contacts.append(
                {
                    "name": b.get("name"),
                    "title": b.get("title"),
                    "license": clean_text(b.get("license"), 64),
                    "email": b.get("email"),
                    "phone": b.get("phone"),
                    "company": b.get("company") or listing.get("sourceCompany"),
                    "avatarUrl": http_url_or_none(b.get("avatarUrl")),
                    "isPrimary": i == 0,
                }
            )

    documents = []
    for d in listing.get("brochures") or []:
        if isinstance(d, dict):
            doc_url = http_url_or_none(d.get("url"))
            if doc_url:
                documents.append(
                    {"title": d.get("name"), "url": doc_url, "docType": d.get("docType") or "brochure"}
                )
    # Harvested DocItems (lib/harvest.ts classified docs) ride the same documents
    # channel; honor the per-doc docType (default 'brochure'), http-url filtered.
    # doc_type classification on the forward path is the adapter's job (lib/
    # harvest.classifyDoc, already shipped); to_row honors the source docType
    # verbatim. The Python classify_doc mirror (cre_parse) is for the WS2
    # backfill / doc-reclassification scripts (contract Section D), not re-run here.
    for d in listing.get("documents") or []:
        if isinstance(d, dict):
            doc_url = http_url_or_none(d.get("url"))
            if doc_url:
                documents.append(
                    {"title": d.get("title"), "url": doc_url, "docType": d.get("docType") or "brochure"}
                )

    images = []
    for i, p in enumerate(listing.get("photos") or []):
        if isinstance(p, str) and p.startswith("http"):
            images.append({"url": p, "isPrimary": i == 0, "order": i})

    # Media (video / virtual-tour / matterport / 360) and outbound links harvested
    # from detail pages (lib/harvest.ts). Bare strings normalize to the default
    # 'other' type; everything is http-url filtered so non-URL noise never stages.
    media = []
    for m in listing.get("media") or []:
        if isinstance(m, str):
            mu = http_url_or_none(m)
            if mu:
                media.append(
                    {"mediaType": "other", "provider": None, "url": mu,
                     "embedUrl": None, "title": None}
                )
        elif isinstance(m, dict):
            mu = http_url_or_none(m.get("url"))
            if mu:
                media.append(
                    {"mediaType": m.get("mediaType") or "other",
                     "provider": m.get("provider"), "url": mu,
                     "embedUrl": http_url_or_none(m.get("embedUrl")),
                     "title": m.get("title")}
                )

    links = []
    for ln in listing.get("links") or []:
        if isinstance(ln, str):
            lu = http_url_or_none(ln)
            if lu:
                links.append({"url": lu, "rel": None, "linkType": "other"})
        elif isinstance(ln, dict):
            lu = http_url_or_none(ln.get("url"))
            if lu:
                links.append({"url": lu, "rel": ln.get("rel"),
                              "linkType": ln.get("linkType") or "other"})

    # GetCREdata is the sole production OM writer. Ignore an `omFacts` payload
    # in every Firecrawl collector artifact so a legacy or manually supplied
    # artifact cannot write cre_listing_om_facts through this ingestor.
    om_facts = []

    title = listing.get("name") or listing.get("headline") or listing.get("street")
    desc = listing.get("description")

    return {
        "slug": slug,
        "external_id": external_id,
        "source_url": url,
        "canonical_url": http_url_or_none(listing.get("canonicalUrl")),
        "transaction_type": transaction_type_of(listing),
        "property_type": norm_property_type(listing.get("assetType")),
        "title": title[:500] if isinstance(title, str) else None,
        "address": listing.get("street"),
        "city": listing.get("city"),
        "state": norm_state(listing.get("state")),
        "zip": str(listing.get("postalCode"))[:12] if listing.get("postalCode") else None,
        "lat": num_or_none(listing.get("latitude"), lo=-90, hi=90),
        "lng": num_or_none(listing.get("longitude"), lo=-180, hi=180),
        "size_sf": size_sf,
        "lot_size_sf": lot_size_sf,
        "year_built": (
            int(listing["yearBuilt"])
            if isinstance(listing.get("yearBuilt"), (int, float)) and 1700 < listing["yearBuilt"] < 2100
            else None
        ),
        "sale_price_usd": sale_price,
        "sale_price_per_sf": price_per_sf,
        "cap_rate": norm_cap_rate(listing.get("capRatePct")),
        "noi": num_or_none(listing.get("noi"), lo=0, hi=1e12),
        "gross_revenue": num_or_none(listing.get("grossRevenue"), lo=0, hi=1e12),
        "occupancy_rate": norm_occupancy_rate(listing.get("occupancyRate")),
        "units": num_or_none(listing.get("units"), lo=0, hi=1e6),
        "floors": num_or_none(listing.get("floors"), lo=0, hi=1e4),
        "parking_spaces": num_or_none(listing.get("parkingSpaces"), lo=0, hi=1e6),
        "parking_ratio": num_or_none(listing.get("parkingRatio"), lo=0, hi=1e4),
        "available_sf": num_or_none(listing.get("availableSf"), lo=0, hi=1e9),
        "min_divisible_sf": num_or_none(listing.get("minDivisibleSf"), lo=0, hi=1e9),
        "max_divisible_sf": num_or_none(listing.get("maxDivisibleSf"), lo=0, hi=1e9),
        "term_min_months": num_or_none(listing.get("termMinMonths"), lo=0, hi=1e4),
        "term_max_months": num_or_none(listing.get("termMaxMonths"), lo=0, hi=1e4),
        "lease_rate_min": lease_min,
        "lease_rate_max": lease_max,
        "lease_rate_type": norm_lease_rate_type(listing.get("leaseRateType")),
        "zoning": clean_text(listing.get("zoning"), 128),
        "market": clean_text(listing.get("market"), 128),
        "submarket": clean_text(listing.get("submarket"), 128),
        # Phase-2 data-lift institutional columns (sql/012). Each clamps to None
        # so the COALESCE-keep upsert never blanks a fuller prior capture.
        "building_class": norm_building_class(listing.get("buildingClass")),
        "property_subtype": clean_text(listing.get("propertySubtype"), 96),
        "apn": clean_text(listing.get("apn"), 64),
        "tenant_name": clean_text(listing.get("tenantName"), 256),
        "guarantor": clean_text(listing.get("guarantor"), 256),
        "lease_years_remaining": num_or_none(listing.get("leaseYearsRemaining"), lo=0, hi=99),
        "price_per_unit": num_or_none(listing.get("pricePerUnit"), lo=0, hi=1e9),
        "grm": num_or_none(listing.get("grm"), lo=0, hi=100),
        "price_per_acre": num_or_none(listing.get("pricePerAcre"), lo=0, hi=1e9),
        "num_rooms": int_or_none(listing.get("numRooms"), lo=0, hi=1e5),
        "revpar": num_or_none(listing.get("revpar"), lo=0, hi=1e5),
        "clear_height_ft": num_or_none(listing.get("clearHeightFt"), lo=0, hi=200),
        "dock_doors": int_or_none(listing.get("dockDoors"), lo=-1, hi=1e4),
        "drive_in_doors": int_or_none(listing.get("driveInDoors"), lo=-1, hi=1e4),
        "power_service": clean_text(listing.get("powerService"), 128),
        "rail_served": bool_or_none(listing.get("railServed")),
        "extra_facts": extra_facts_or_none(listing.get("extraFacts")),
        "highlights": str_array_or_none(listing.get("highlights")),
        "amenities": str_array_or_none(listing.get("amenities")),
        "description": desc[:20000] if isinstance(desc, str) else None,
        "markdown": clean_text(listing.get("markdown")),
        "updated_date": iso_date_or_none(listing.get("lastUpdated")),
        "status": norm_status(listing),
        "source_lastmod": group_source_lastmod([listing]),
        "canonical_key": _canonical_key(listing),
        # Prefer the source observation timestamp. runMeta.finishedAt is only a
        # compatibility fallback for legacy/non-strict artifacts and must not
        # manufacture current detail freshness.
        "scraped_at": observation_scraped_at or scraped_at,
        "raw_data": listing,
        "contacts": contacts,
        "documents": documents,
        "images": images,
        "media": media,
        "links": links,
        "om_facts": om_facts,
        "_modes": {listing.get("transactionMode")},
    }


def _contact_aliases(item):
    """Return every SQL-equivalent identity for one staged contact."""
    aliases = set()
    email = clean_text(item.get("email"))
    if email:
        aliases.add(("email", email.lower()))
    name = (clean_text(item.get("name")) or "").lower()
    if name:
        aliases.add(("name_phone", name, clean_text(item.get("phone")) or ""))
    return aliases


def _child_identity(collection, item):
    """Return the database-aligned identity for one non-contact child row."""
    if not isinstance(item, dict):
        return ("value", json.dumps(item, sort_keys=True, default=str))
    if collection in {"documents", "images"}:
        url = clean_text(item.get("url"))
        if url:
            return ("url", url)
    elif collection == "media":
        url = clean_text(item.get("url"))
        if url:
            return ("media", item.get("mediaType") or "other", url)
    elif collection == "links":
        url = clean_text(item.get("url"))
        if url:
            return ("link", item.get("linkType") or "other", url)
    elif collection == "om_facts":
        return (
            "om_fact",
            item.get("factGroup") or "scalar",
            item.get("factKey"),
            item.get("sourceDocUrl"),
            item.get("parserVersion"),
        )
    return ("row", json.dumps(item, sort_keys=True, default=str))


def _merge_child_values(prior, item):
    if not isinstance(prior, dict) or not isinstance(item, dict):
        return
    for key, value in item.items():
        if key == "isPrimary":
            prior[key] = bool(prior.get(key)) or bool(value)
        elif (
            (prior.get(key) is None or prior.get(key) == "")
            and value is not None
            and value != ""
        ):
            prior[key] = value


def _merge_contact_collections(primary, secondary):
    components = []
    for item in [*(primary or []), *(secondary or [])]:
        candidate = dict(item) if isinstance(item, dict) else item
        aliases = _contact_aliases(candidate) if isinstance(candidate, dict) else set()
        matches = [
            index
            for index, component in enumerate(components)
            if aliases and aliases.intersection(component["aliases"])
        ]
        if not matches:
            components.append({"row": candidate, "aliases": set(aliases)})
            continue
        target = components[matches[0]]
        _merge_child_values(target["row"], candidate)
        target["aliases"].update(aliases)
        target["aliases"].update(_contact_aliases(target["row"]))
        while True:
            absorbed_components = [
                component
                for component in components
                if component is not target
                and target["aliases"].intersection(component["aliases"])
            ]
            if not absorbed_components:
                break
            for absorbed in absorbed_components:
                _merge_child_values(target["row"], absorbed["row"])
                target["aliases"].update(absorbed["aliases"])
                target["aliases"].update(_contact_aliases(target["row"]))
                components.remove(absorbed)
    return [component["row"] for component in components]


def _merge_child_collections(collection, primary, secondary):
    """Union staged child rows without losing sale/lease-only observations."""
    if collection == "contacts":
        merged = _merge_contact_collections(primary, secondary)
    else:
        merged = []
        positions = {}
        for item in [*(primary or []), *(secondary or [])]:
            identity = _child_identity(collection, item)
            prior_index = positions.get(identity)
            if prior_index is None:
                positions[identity] = len(merged)
                merged.append(dict(item) if isinstance(item, dict) else item)
                continue
            _merge_child_values(merged[prior_index], item)
    if collection in {"contacts", "images"} and merged:
        primary_index = next(
            (
                index
                for index, item in enumerate(merged)
                if isinstance(item, dict) and bool(item.get("isPrimary"))
            ),
            0,
        )
        for index, item in enumerate(merged):
            if isinstance(item, dict):
                item["isPrimary"] = index == primary_index
    return merged


def merge_rows(a, b):
    """Merge two staged rows for the same (slug, external_id) within a batch."""
    a["_modes"] |= b["_modes"]
    if a["_modes"] >= {"sale", "lease"}:
        a["transaction_type"] = "sale_or_lease"
    elif b["transaction_type"] == "sale_or_lease":
        a["transaction_type"] = "sale_or_lease"
    for k in (
        "property_type", "title", "address", "city", "state", "zip", "lat", "lng",
        "size_sf", "lot_size_sf", "year_built", "sale_price_usd", "sale_price_per_sf",
        "cap_rate", "noi", "gross_revenue", "occupancy_rate", "units", "floors",
        "parking_spaces", "parking_ratio", "available_sf", "min_divisible_sf",
        "max_divisible_sf", "term_min_months", "term_max_months",
        "lease_rate_min", "lease_rate_max", "lease_rate_type", "zoning",
        "market", "submarket", "highlights", "amenities", "description",
        "updated_date", "source_lastmod", "canonical_key", "canonical_url",
        # Phase-2 data-lift institutional fields: first non-None wins across the
        # sale+lease passes (a sparse pass never blanks a fuller capture).
        "building_class", "property_subtype", "apn", "tenant_name", "guarantor",
        "lease_years_remaining", "price_per_unit", "grm", "price_per_acre",
        "num_rooms", "revpar", "clear_height_ft", "dock_doors", "drive_in_doors",
        "power_service", "rail_served",
    ):
        if a[k] is None and b[k] is not None:
            a[k] = b[k]
    for k in ("contacts", "documents", "images", "media", "links", "om_facts"):
        a[k] = _merge_child_collections(k, a[k], b[k])
    # extra_facts: merge the two long-tail blobs (union; the first pass wins a key
    # collision) so neither pass's facts are lost. Mirrors the jsonb `||` merge in
    # the upsert (a missing/empty pass keeps the other's blob).
    ea, eb = a.get("extra_facts"), b.get("extra_facts")
    if eb:
        merged_extra = dict(eb)
        if ea:
            merged_extra.update(ea)  # a (primary) overrides b on key collision
        a["extra_facts"] = merged_extra or None
    # markdown: prefer the longer non-empty capture across the sale+lease passes
    # (a detail pass with a fuller page wins over a sparse one); never blanks a
    # prior good capture.
    am, bm = a.get("markdown") or "", b.get("markdown") or ""
    a["markdown"] = (am if len(am) >= len(bm) else bm) or None
    # status: a real DROP signal (sold/leased/off_market) from EITHER pass wins
    # over a transitional/None one; otherwise keep the first non-None signal
    # (mirrors norm_status terminal-wins across the sale+lease passes).
    _DROP = {"sold", "leased", "off_market"}
    sa, sb = a.get("status"), b.get("status")
    if sa not in _DROP and sb in _DROP:
        a["status"] = sb
    elif sa is None:
        a["status"] = sb
    # keep both raw payloads when the passes differ
    if b["raw_data"] is not a["raw_data"]:
        a["raw_data"] = {"primary": a["raw_data"], "secondary_pass": b["raw_data"]}
    return a


def validate_duplicate_identity_before_merge(a, b):
    """Fail closed when a source contract requires one row per identity.

    Colliers SalesTracker is sale-only and the collector proves a one-to-one
    card-to-ProjectId mapping. Any repeated canonical ID therefore violates the
    source contract. Newmark's NIM adapter likewise proves unique output slugs
    across its disjoint sale and lease feeds. Other providers retain the
    existing sale/lease merge behavior because their adapters intentionally
    normalize dual-mode rows to one external ID.
    """
    def contains_source(payload, source_key):
        if not isinstance(payload, dict):
            return False
        if payload.get("sourceKey") == source_key:
            return True
        return any(
            contains_source(payload.get(key), source_key)
            for key in ("primary", "secondary_pass")
        )

    source_name = next(
        (
            name
            for key, name in (
                ("colliers", "Colliers"),
                ("newmark", "Newmark"),
            )
            if contains_source(a.get("raw_data"), key)
            and contains_source(b.get("raw_data"), key)
        ),
        None,
    )
    if source_name is None:
        return

    conflicts = []
    for field in ("source_url", "canonical_url", "title"):
        value_a = a.get(field)
        value_b = b.get(field)
        if value_a and value_b and value_a != value_b:
            conflicts.append(field)
    detail = (
        f": incompatible {', '.join(conflicts)}"
        if conflicts
        else ""
    )
    identity_label = (
        "canonical ProjectId"
        if source_name == "Colliers"
        else "canonical identity"
    )
    raise ValueError(
        f"duplicate {source_name} {identity_label} "
        f"{a.get('external_id')!r}{detail}"
    )


# ---------------------------------------------------------------------------
# COPY encoding + SQL generation
# ---------------------------------------------------------------------------


def copy_field(v):
    """Encode one value for PostgreSQL COPY text format."""
    if v is None:
        return "\\N"
    if isinstance(v, bool):
        return "t" if v else "f"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    s = str(v)
    return (
        s.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


STAGE_COLS = [
    "slug", "external_id", "source_url", "transaction_type", "property_type",
    "title", "address", "city", "state", "zip", "lat", "lng", "size_sf",
    "lot_size_sf", "year_built", "sale_price_usd", "sale_price_per_sf",
    "cap_rate", "noi", "gross_revenue", "occupancy_rate", "units", "floors",
    "parking_spaces", "parking_ratio", "available_sf", "min_divisible_sf",
    "max_divisible_sf", "term_min_months", "term_max_months",
    "lease_rate_min", "lease_rate_max", "lease_rate_type", "zoning",
    "market", "submarket", "highlights", "amenities", "description", "markdown",
    "updated_date", "scraped_at", "raw_data", "contacts", "documents", "images",
    "media", "links",
    "status", "source_lastmod", "canonical_key",
    # Phase-2 data-lift (sql/012): canonical_url + discrete institutional columns
    # + extra_facts jsonb. om_facts is staged as a jsonb array (sql/013 child).
    "canonical_url",
    "building_class", "property_subtype", "apn", "tenant_name", "guarantor",
    "lease_years_remaining", "price_per_unit", "grm", "price_per_acre",
    "num_rooms", "revpar", "clear_height_ft", "dock_doors", "drive_in_doors",
    "power_service", "rail_served", "extra_facts", "om_facts",
]

INVENTORY_ONLY_COLS = [
    "slug",
    "external_id",
    "source_key",
    "url",
    "fingerprint",
    "observed_status",
    "observed_at",
]

INVENTORY_ONLY_SCOPE_COLS = [
    "slug",
    "source_key",
    "external_id_like",
    "watermark_external_id",
    "watermark_url",
    "watermark_fingerprint",
    "observed_at",
]


def _flip_circuit_breaker():
    """Optional Phase-2 status-flip guard (board-impact doc 2026-06-13, finding 4).

    Default OFF so the unattended daily ingest is never blocked by it. Set
    CRE_STATUS_FLIP_MAX_FRACTION to a fraction in (0, 1] to abort the whole ingest
    transaction when any one source would move more than that share of its
    currently-active rows to a non-active status in a single run (the signature of
    a source parsing regression). CRE_STATUS_FLIP_MIN_BASE (default 200) suppresses
    the check for small-inventory sources where the ratio is noisy. Returns
    (fraction, min_base) when enabled, else None.
    """
    raw = os.environ.get("CRE_STATUS_FLIP_MAX_FRACTION")
    if not raw:
        return None
    try:
        frac = float(raw)
    except ValueError:
        return None
    if not (0 < frac <= 1):
        return None
    try:
        min_base = int(os.environ.get("CRE_STATUS_FLIP_MIN_BASE", "200"))
    except ValueError:
        min_base = 200
    return frac, max(1, min_base)


def _status_activation_enabled(cli_flag=False):
    """Phase-2 source-derived status activation is OPT-IN (default OFF).

    Writing a real sold / under_contract / pending / leased / off_market signal
    onto cre_listings.status is the board-coupled Phase-2 feature. It must not
    run until the EQUIRE consumer board-gate (Option B) and the widened 005
    views are live, or non-active rows silently drop off an 'active'-only board
    (cre-phase2-board-impact-2026-06-13.md). Default OFF so routine and
    scheduled ingests refresh listing data (prices, children, dedup, resurrect,
    new inventory) without ever flipping board state. Enable deliberately with
    --activate-status or CRE_ACTIVATE_STATUS=1 once the consumer layer ships.
    """
    if cli_flag:
        return True
    return os.environ.get("CRE_ACTIVATE_STATUS", "").strip().lower() in {"1", "true", "yes", "on"}


def apply_status_activation_gate(rows, activate_status):
    """Suppress source-derived status unless activation is explicitly enabled.

    When OFF (default), strip the staged status so the upsert inserts
    COALESCE(NULL,'active') -> 'active' for new rows and the targeted activation
    UPDATE is a no-op (its ``s.status IS NOT NULL`` filter matches nothing), so
    no existing row is flipped to a non-active status. Returns the number of
    rows whose non-null status signal was suppressed (observability). When ON,
    rows are left untouched and normal Phase-2 activation runs.
    """
    if activate_status:
        return 0
    suppressed = 0
    for r in rows:
        if r.get("status") is not None:
            r["status"] = None
            suppressed += 1
    return suppressed


def build_sql(
    rows,
    job_meta,
    started_at,
    mark_missing_slugs,
    history_guard=True,
    inventory_only_rows=(),
    inventory_only_scopes=(),
):
    # Defense in depth for direct callers of this builder. Normal ingestion
    # reaches here through to_row(), which already drops `omFacts`, but this
    # prevents a manually constructed row from restoring the retired writer.
    rows = [{**row, "om_facts": []} for row in rows]
    lines = []
    w = lines.append
    staged_source_key_sql = source_key_sql("s", "b")
    live_source_key_sql = source_key_sql("l", "b")
    w("\\set ON_ERROR_STOP on")
    w("BEGIN;")
    # Large complete-source artifacts (CBRE is ~80 MB of inline COPY data) can
    # legitimately take more than ten minutes to traverse the Supavisor
    # connection. Bound the data load separately, then restore the tighter
    # mutation timeout after all four transaction-local COPY stages.
    w("SET LOCAL statement_timeout = '1800s';")
    # Pin standard_conforming_strings so sql_lit()'s quote-doubling is provably
    # sufficient regardless of the server/role default: with it ON, a backslash
    # inside a '...' literal is literal, so a doubled single quote is the only
    # escape a scraped value can need. Set before any literal- or COPY-bearing
    # statement below. (Never wrap an interpolated value in an E'...' literal:
    # E-strings reprocess backslashes and would defeat sql_lit.)
    w("SET LOCAL standard_conforming_strings = on;")
    _cb = _flip_circuit_breaker()
    if _cb is not None:
        w(f"SET LOCAL cre.flip_max_fraction = '{_cb[0]}';")
        w(f"SET LOCAL cre.flip_min_base = '{_cb[1]}';")
    w("""
CREATE TEMP TABLE _stage (
    slug text, external_id text, source_url text, transaction_type text,
    property_type text, title text, address text, city text, state text,
    zip text, lat double precision, lng double precision, size_sf numeric,
    lot_size_sf numeric, year_built integer, sale_price_usd numeric,
    sale_price_per_sf numeric, cap_rate numeric, noi numeric,
    gross_revenue numeric, occupancy_rate numeric, units integer, floors integer,
    parking_spaces integer, parking_ratio numeric, available_sf numeric,
    min_divisible_sf numeric, max_divisible_sf numeric, term_min_months integer,
    term_max_months integer, lease_rate_min numeric, lease_rate_max numeric,
    lease_rate_type text, zoning text, market text, submarket text,
    highlights jsonb, amenities jsonb, description text, markdown text,
    updated_date timestamptz,
    scraped_at timestamptz, raw_data jsonb, contacts jsonb, documents jsonb,
    images jsonb, media jsonb, links jsonb,
    status text, source_lastmod timestamptz, canonical_key text,
    canonical_url text,
    building_class text, property_subtype text, apn text, tenant_name text,
    guarantor text, lease_years_remaining numeric, price_per_unit numeric,
    grm numeric, price_per_acre numeric, num_rooms integer, revpar numeric,
    clear_height_ft numeric, dock_doors integer, drive_in_doors integer,
    power_service text, rail_served boolean, extra_facts jsonb, om_facts jsonb
) ON COMMIT DROP;""")
    w(f"COPY _stage ({', '.join(STAGE_COLS)}) FROM stdin;")
    for r in rows:
        w("\t".join(copy_field(r[c]) for c in STAGE_COLS))
    w("\\.")

    w("""
CREATE TEMP TABLE _inventory_only (
    slug text, external_id text, source_key text, url text,
    fingerprint text, observed_status text, observed_at timestamptz
) ON COMMIT DROP;""")
    w(f"COPY _inventory_only ({', '.join(INVENTORY_ONLY_COLS)}) FROM stdin;")
    for r in inventory_only_rows:
        w("\t".join(copy_field(r[c]) for c in INVENTORY_ONLY_COLS))
    w("\\.")

    w("""
CREATE TEMP TABLE _inventory_only_scope (
    slug text, source_key text, external_id_like text,
    watermark_external_id text, watermark_url text,
    watermark_fingerprint text,
    observed_at timestamptz
) ON COMMIT DROP;""")
    w(
        f"COPY _inventory_only_scope "
        f"({', '.join(INVENTORY_ONLY_SCOPE_COLS)}) FROM stdin;"
    )
    for r in inventory_only_scopes:
        w("\t".join(copy_field(r[c]) for c in INVENTORY_ONLY_SCOPE_COLS))
    w("\\.")

    w("""
CREATE TEMP TABLE _jobmeta (
    slug text, discovered integer, saved integer, errors integer, notes text
) ON COMMIT DROP;""")
    w("COPY _jobmeta (slug, discovered, saved, errors, notes) FROM stdin;")
    for jm in job_meta:
        w("\t".join(copy_field(v) for v in (jm["slug"], jm["discovered"], jm["saved"], jm["errors"], jm["notes"])))
    w("\\.")
    w("SET LOCAL statement_timeout = '600s';")

    w("""
-- Fail loudly if a sourceKey maps to a brokerage slug that is not seeded.
DO $$
DECLARE missing text;
BEGIN
    SELECT string_agg(DISTINCT s.slug, ', ') INTO missing
    FROM (
      SELECT slug FROM _stage
      UNION ALL
      SELECT slug FROM _inventory_only
      UNION ALL
      SELECT slug FROM _inventory_only_scope
    ) s
    LEFT JOIN credeals.cre_brokerages b ON b.slug = s.slug
    WHERE b.id IS NULL;
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'unseeded brokerage slug(s): % (run sql/001_cre_brokerages.sql)', missing;
    END IF;
END $$;

-- Provider cards without a stable listing URL are enumeration evidence only.
-- Keep them out of canonical cre_listings (no fake URL or stable-history
-- claim), but persist their provisional identity in cre_source_index so the
-- current inventory is not silently discarded.
-- This table is a required production migration. Fail the transaction loudly
-- if it is absent rather than silently dropping enumeration evidence.
DO $$
DECLARE stale_source text;
BEGIN
  SELECT scope.source_key INTO stale_source
  FROM _inventory_only_scope scope
  JOIN credeals.cre_brokerages b ON b.slug = scope.slug
  WHERE EXISTS (
    SELECT 1
    FROM credeals.cre_source_index prior
    WHERE prior.brokerage_id = b.id
      AND prior.source_key = scope.source_key
      AND (
        prior.external_id LIKE scope.external_id_like
        OR prior.external_id = scope.watermark_external_id
      )
      AND prior.last_enumerated_at > scope.observed_at
  )
  LIMIT 1;
  IF stale_source IS NOT NULL THEN
    RAISE EXCEPTION
      'refusing stale inventory-only replay for source %', stale_source;
  END IF;
END $$;

INSERT INTO credeals.cre_source_index AS si (
  brokerage_id, external_id, source_key, url, fingerprint,
  soft_deleted, observed_status, first_seen, last_seen, last_enumerated_at
)
SELECT b.id, i.external_id, i.source_key, i.url, i.fingerprint,
       false, i.observed_status, i.observed_at, i.observed_at, i.observed_at
FROM _inventory_only i
JOIN credeals.cre_brokerages b ON b.slug = i.slug
ON CONFLICT (brokerage_id, external_id) DO UPDATE SET
  source_key = EXCLUDED.source_key,
  url = EXCLUDED.url,
  fingerprint = EXCLUDED.fingerprint,
  soft_deleted = false,
  observed_status = EXCLUDED.observed_status,
  last_seen = EXCLUDED.last_seen,
  last_enumerated_at = EXCLUDED.last_enumerated_at
WHERE EXCLUDED.last_enumerated_at >= si.last_enumerated_at;

-- A strict full source enumeration makes absence meaningful for the
-- provisional namespace. Retire vanished cards in the monitor index only;
-- never infer a canonical listing disappearance from these mutable card IDs.
UPDATE credeals.cre_source_index si
SET soft_deleted = true,
    last_enumerated_at = scope.observed_at
FROM _inventory_only_scope scope
JOIN credeals.cre_brokerages b ON b.slug = scope.slug
WHERE si.brokerage_id = b.id
  AND si.source_key = scope.source_key
  AND si.external_id LIKE scope.external_id_like
  AND si.soft_deleted = false
  AND si.last_enumerated_at <= scope.observed_at
  AND NOT EXISTS (
    SELECT 1
    FROM _inventory_only current
    WHERE current.slug = scope.slug
      AND current.source_key = scope.source_key
      AND current.external_id = si.external_id
  );

-- Persist a namespace-level high-water mark even when a valid full run
-- contains zero provisional cards. Without this sentinel, replaying an older
-- nonempty artifact after a newer empty snapshot could resurrect stale cards.
-- The sentinel is always soft-deleted and uses a non-card external-id prefix,
-- so current inventory queries and monitor disappearance logic exclude it.
INSERT INTO credeals.cre_source_index AS si (
  brokerage_id, external_id, source_key, url, fingerprint,
  soft_deleted, observed_status, first_seen, last_seen, last_enumerated_at
)
SELECT b.id,
       scope.watermark_external_id,
       scope.source_key,
       scope.watermark_url,
       scope.watermark_fingerprint,
       true,
       NULL,
       scope.observed_at,
       scope.observed_at,
       scope.observed_at
FROM _inventory_only_scope scope
JOIN credeals.cre_brokerages b ON b.slug = scope.slug
ON CONFLICT (brokerage_id, external_id) DO UPDATE SET
  source_key = EXCLUDED.source_key,
  url = EXCLUDED.url,
  fingerprint = EXCLUDED.fingerprint,
  soft_deleted = true,
  observed_status = NULL,
  last_seen = EXCLUDED.last_seen,
  last_enumerated_at = EXCLUDED.last_enumerated_at
WHERE EXCLUDED.last_enumerated_at >= si.last_enumerated_at;

-- CBRE Deal Flow may expose the same provider `pv` token under agreement,
-- brochure, legacy landing, and modern landing paths. Reconcile by the
-- provider token rather than the full URL. Prefer one active identity; when no
-- active row exists, one unique historical identity is reused and resurrected.
-- Ambiguous relevant identities fail the transaction and must be explicitly
-- consolidated, never guessed.
CREATE TEMP TABLE _dealflow_pv_identity ON COMMIT DROP AS
SELECT b.slug,
       substring(t.source_url from '[?&]pv=([^&#]+)') AS provider_pv,
       COALESCE(
         min(t.external_id) FILTER (WHERE t.deleted_at IS NULL),
         min(t.external_id)
       ) AS external_id,
       count(*) FILTER (WHERE t.deleted_at IS NULL) AS active_count,
       count(*) AS total_count
  FROM credeals.cre_listings t
  JOIN credeals.cre_brokerages b ON b.id = t.brokerage_id
  WHERE b.slug = 'cbre'
    AND t.external_id LIKE 'dealflow:%'
    AND t.source_url IS NOT NULL
    AND substring(t.source_url from '[?&]pv=([^&#]+)') IS NOT NULL
  GROUP BY b.slug, substring(t.source_url from '[?&]pv=([^&#]+)');

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM _dealflow_pv_identity e
    JOIN _stage s
      ON s.slug = e.slug
     AND substring(s.source_url from '[?&]pv=([^&#]+)') = e.provider_pv
    WHERE (
        e.active_count > 1
        OR (e.active_count = 0 AND e.total_count > 1)
      )
      AND jsonb_path_exists(s.raw_data, '$.**.sourceKey ? (@ == "cbre-dealflow")')
  ) THEN
    RAISE EXCEPTION
      'ambiguous active CBRE Deal Flow provider pv identity; consolidate duplicates before ingest';
  END IF;
END $$;

UPDATE _stage s
SET external_id = e.external_id
FROM _dealflow_pv_identity e
WHERE s.slug = e.slug
  AND substring(s.source_url from '[?&]pv=([^&#]+)') = e.provider_pv
  AND (
    e.active_count = 1
    OR (e.active_count = 0 AND e.total_count = 1)
  )
  AND jsonb_path_exists(s.raw_data, '$.**.sourceKey ? (@ == "cbre-dealflow")')
  AND s.external_id IS DISTINCT FROM e.external_id;

CREATE TEMP TABLE _src ON COMMIT DROP AS
SELECT b.id AS brokerage_id, s.*
FROM _stage s JOIN credeals.cre_brokerages b ON b.slug = s.slug;""")

    w(f"""
-- Capture the checkpoint-quality baseline before any listing or child mutation.
-- Scope it to canonical source keys present in this ingest, not the folded
-- brokerage slug, so a child regression in one source cannot be hidden by a
-- sibling source sharing the brokerage.
CREATE TEMP TABLE _ingest_child_sources ON COMMIT DROP AS
SELECT DISTINCT source_key
FROM (
  SELECT {staged_source_key_sql} AS source_key
  FROM _src s
  JOIN credeals.cre_brokerages b ON b.id = s.brokerage_id
) staged_sources
WHERE source_key IS NOT NULL;

CREATE TEMP TABLE _active_child_scope_before ON COMMIT DROP AS
SELECT listing_id, source_key
FROM (
  SELECT l.id AS listing_id, {live_source_key_sql} AS source_key
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE l.deleted_at IS NULL
) active
JOIN _ingest_child_sources ingest_scope USING (source_key);

-- Snapshot per-listing counts before mutation. The final guard compares only
-- parents that remain active after reconciliation: a legitimately retired
-- parent must not look like destructive child loss, while a parser collapse on
-- a retained parent must still fail the whole transaction.
CREATE TEMP TABLE _child_counts_by_listing_before ON COMMIT DROP AS
SELECT a.listing_id, a.source_key, 'contacts'::text AS child_type,
       count(c.id)::bigint AS child_count
FROM _active_child_scope_before a
LEFT JOIN credeals.cre_listing_contacts c ON c.listing_id = a.listing_id
GROUP BY a.listing_id, a.source_key
UNION ALL
SELECT a.listing_id, a.source_key, 'documents'::text, count(d.id)::bigint
FROM _active_child_scope_before a
LEFT JOIN credeals.cre_listing_documents d ON d.listing_id = a.listing_id
GROUP BY a.listing_id, a.source_key
UNION ALL
SELECT a.listing_id, a.source_key, 'images'::text, count(i.id)::bigint
FROM _active_child_scope_before a
LEFT JOIN credeals.cre_listing_images i ON i.listing_id = a.listing_id
GROUP BY a.listing_id, a.source_key;

DO $$ BEGIN
  IF EXISTS (
    SELECT 1
    WHERE to_regclass('credeals.cre_listing_media') IS NOT NULL
  ) THEN
    INSERT INTO _child_counts_by_listing_before
    SELECT a.listing_id, a.source_key, 'media', count(m.id)
    FROM _active_child_scope_before a
    LEFT JOIN credeals.cre_listing_media m ON m.listing_id = a.listing_id
    GROUP BY a.listing_id, a.source_key;
  END IF;
  IF EXISTS (
    SELECT 1
    WHERE to_regclass('credeals.cre_listing_links') IS NOT NULL
  ) THEN
    INSERT INTO _child_counts_by_listing_before
    SELECT a.listing_id, a.source_key, 'links', count(k.id)
    FROM _active_child_scope_before a
    LEFT JOIN credeals.cre_listing_links k ON k.listing_id = a.listing_id
    GROUP BY a.listing_id, a.source_key;
  END IF;
END $$;""")

    w("""
-- Cushman's provider GUID is not a durable property identity. Fixed artifacts
-- stage URL-v1 identities, but inserting one beside an active legacy GUID row
-- would grow the exact duplicate-URL defect this repair is intended to stop.
-- Fail closed until the one-time identity migration has renamed/merged the
-- active legacy row while preserving its UUID, detail, and children.
DO $$
DECLARE
    legacy_rows bigint;
BEGIN
    SELECT count(*) INTO legacy_rows
    FROM credeals.cre_listings t
    WHERE t.deleted_at IS NULL
      AND (
        t.external_id IS NULL
        OR t.external_id !~ '^url:v1:[0-9a-f]{32}$'
      )
      AND EXISTS (
        SELECT 1
        FROM _src s
        WHERE s.brokerage_id = t.brokerage_id
          AND jsonb_path_exists(
            s.raw_data,
            '$.**.sourceKey ? (@ == "cushman-wakefield")'
          )
      );
    IF legacy_rows > 0 THEN
      RAISE EXCEPTION
        'Cushman URL-v1 identity repair required for % active legacy identity row(s); refusing duplicate growth',
        legacy_rows;
    END IF;
END $$;

-- (H4a) Capture prior watched values BEFORE the upsert mutates them, so the
-- append-only price history records a row only on a REAL change. This reads
-- only cre_listings (always present) and is not guarded itself. New listings
-- have no row here and get no history entry (history starts at the first
-- change, which avoids bloat on the initial insert path).
CREATE TEMP TABLE _prior_vals ON COMMIT DROP AS
SELECT t.id, t.brokerage_id, t.external_id,
       t.sale_price_usd, t.sale_price_per_sf, t.lease_rate_min, t.lease_rate_max,
       t.status, t.cap_rate
FROM credeals.cre_listings t
JOIN _src s ON s.brokerage_id = t.brokerage_id AND s.external_id = t.external_id;

CREATE TEMP TABLE _up ON COMMIT DROP AS
WITH ins AS (
    INSERT INTO credeals.cre_listings AS t (
        brokerage_id, external_id, source_url, status, transaction_type,
        property_type, title, address, city, state, zip, lat, lng, market, submarket,
        size_sf, lot_size_sf, available_sf, min_divisible_sf, max_divisible_sf,
        floors, year_built, units, parking_spaces, parking_ratio,
        sale_price_usd, sale_price_per_sf, cap_rate, noi, gross_revenue, occupancy_rate,
        lease_rate_min, lease_rate_max, lease_rate_type, term_min_months, term_max_months,
        description, highlights, amenities, zoning, markdown,
        updated_date, scraped_at, raw_data, source_lastmod, canonical_key
    )
    SELECT brokerage_id, external_id, source_url, COALESCE(status, 'active'), transaction_type,
           property_type, title, address, city, state, zip, lat, lng, market, submarket,
           size_sf, lot_size_sf, available_sf, min_divisible_sf, max_divisible_sf,
           floors, year_built, units, parking_spaces, parking_ratio,
           sale_price_usd, sale_price_per_sf, cap_rate, noi, gross_revenue, occupancy_rate,
           lease_rate_min, lease_rate_max, lease_rate_type, term_min_months, term_max_months,
           description,
           CASE WHEN jsonb_typeof(highlights) = 'array'
                THEN ARRAY(SELECT jsonb_array_elements_text(highlights)) END,
           CASE WHEN jsonb_typeof(amenities) = 'array'
                THEN ARRAY(SELECT jsonb_array_elements_text(amenities)) END,
           zoning, NULLIF(markdown, ''),
           updated_date,
           CASE
             WHEN jsonb_path_exists(raw_data, '$.**.detailError')
               OR (
                 jsonb_path_exists(
                   raw_data,
                   '$.**.preserveChildCollections ? (@ == true || @ == "true")'
                 )
                 AND NOT jsonb_path_exists(
                   raw_data,
                   '$.**.detailObservedWithChildPreservation ? (@ == true || @ == "true")'
                 )
               )
             THEN NULL
             ELSE scraped_at
           END,
           CASE
             WHEN jsonb_path_exists(raw_data, '$.**.detailError')
               OR (
                 jsonb_path_exists(
                   raw_data,
                   '$.**.preserveChildCollections ? (@ == true || @ == "true")'
                 )
                 AND NOT jsonb_path_exists(
                   raw_data,
                   '$.**.detailObservedWithChildPreservation ? (@ == true || @ == "true")'
                 )
               )
             THEN jsonb_build_object(
                    'sourceKey', COALESCE(
                      raw_data->'sourceKey',
                      raw_data#>'{primary,sourceKey}',
                      raw_data#>'{secondary_pass,sourceKey}'
                    ),
                    'latestInventoryObservation', raw_data,
                    'inventoryObservedAt', to_jsonb(scraped_at)
                  )
             ELSE raw_data
           END,
           source_lastmod, canonical_key
    FROM _src
    ON CONFLICT (brokerage_id, external_id) WHERE external_id IS NOT NULL
    DO UPDATE SET
        source_url        = EXCLUDED.source_url,
        -- (M5) Revival resets to 'active' only when the prior status was 'inactive'
        -- (a mark-missing soft-delete marker), not a real terminal (sold/leased/
        -- off_market) that flickered back into the feed. A terminal-bearing row that
        -- reappears stays with its terminal label; the board gate excludes it.
        status            = CASE WHEN t.deleted_at IS NOT NULL AND t.status = 'inactive'
                                 THEN 'active' ELSE t.status END,
        -- transaction_type NEVER narrows a known type. A partial/generic re-ingest
        -- (the generic enricher emits no transactionType, and the enrichment queue
        -- has no transaction column so cre_enrich tags every claimed row "sale")
        -- must not flip an existing 'lease'/'sale_or_lease' row to 'sale' and drop
        -- it off the for-lease board (v_cre_active_for_lease filters
        -- transaction_type IN ('lease','sale_or_lease')). Rules, mirroring
        -- merge_rows() across the sale+lease passes:
        --   existing 'sale_or_lease'                  -> keep (never narrow)
        --   incoming 'sale_or_lease'                  -> take (upgrade)
        --   existing NULL                             -> take incoming
        --   existing/incoming differ (sale vs lease)  -> 'sale_or_lease' (serves both)
        --   same single mode, or incoming NULL        -> keep existing
        transaction_type  = CASE
                              WHEN t.transaction_type = 'sale_or_lease' THEN 'sale_or_lease'
                              WHEN EXCLUDED.transaction_type = 'sale_or_lease' THEN 'sale_or_lease'
                              WHEN t.transaction_type IS NULL THEN EXCLUDED.transaction_type
                              WHEN EXCLUDED.transaction_type IS NOT NULL
                                   AND EXCLUDED.transaction_type IS DISTINCT FROM t.transaction_type
                                   THEN 'sale_or_lease'
                              ELSE t.transaction_type
                            END,
        property_type     = COALESCE(EXCLUDED.property_type, t.property_type),
        title             = COALESCE(EXCLUDED.title, t.title),
        address           = COALESCE(EXCLUDED.address, t.address),
        city              = COALESCE(EXCLUDED.city, t.city),
        state             = COALESCE(EXCLUDED.state, t.state),
        zip               = COALESCE(EXCLUDED.zip, t.zip),
        lat               = COALESCE(EXCLUDED.lat, t.lat),
        lng               = COALESCE(EXCLUDED.lng, t.lng),
        size_sf           = CASE
                              WHEN t.size_sf > 1000000000 THEN EXCLUDED.size_sf
                              ELSE COALESCE(EXCLUDED.size_sf, t.size_sf)
                            END,
        market            = COALESCE(EXCLUDED.market, t.market),
        submarket         = COALESCE(EXCLUDED.submarket, t.submarket),
        lot_size_sf       = COALESCE(EXCLUDED.lot_size_sf, t.lot_size_sf),
        available_sf      = COALESCE(EXCLUDED.available_sf, t.available_sf),
        min_divisible_sf  = COALESCE(EXCLUDED.min_divisible_sf, t.min_divisible_sf),
        max_divisible_sf  = COALESCE(EXCLUDED.max_divisible_sf, t.max_divisible_sf),
        floors            = COALESCE(EXCLUDED.floors, t.floors),
        year_built        = COALESCE(EXCLUDED.year_built, t.year_built),
        units             = COALESCE(EXCLUDED.units, t.units),
        parking_spaces    = COALESCE(EXCLUDED.parking_spaces, t.parking_spaces),
        parking_ratio     = COALESCE(EXCLUDED.parking_ratio, t.parking_ratio),
        -- (L1) COALESCE-keep: a transient parse miss (regex miss, "Call for offer")
        -- keeps the prior good numeric value rather than overwriting with NULL.
        -- A real new numeric value still overwrites because COALESCE picks the first
        -- non-NULL, which is EXCLUDED when present. Mirrors cap_rate, property_type,
        -- and other neighbors that already use COALESCE-keep. The lifted structured
        -- columns (noi/gross_revenue/occupancy_rate/divisible/term/parking/...) follow
        -- the same rule so a sparse detail pass never clobbers a fuller prior capture.
        sale_price_usd    = COALESCE(EXCLUDED.sale_price_usd, t.sale_price_usd),
        sale_price_per_sf = COALESCE(EXCLUDED.sale_price_per_sf, t.sale_price_per_sf),
        cap_rate          = COALESCE(EXCLUDED.cap_rate, t.cap_rate),
        noi               = COALESCE(EXCLUDED.noi, t.noi),
        gross_revenue     = COALESCE(EXCLUDED.gross_revenue, t.gross_revenue),
        occupancy_rate    = COALESCE(EXCLUDED.occupancy_rate, t.occupancy_rate),
        lease_rate_min    = COALESCE(EXCLUDED.lease_rate_min, t.lease_rate_min),
        lease_rate_max    = COALESCE(EXCLUDED.lease_rate_max, t.lease_rate_max),
        lease_rate_type   = COALESCE(EXCLUDED.lease_rate_type, t.lease_rate_type),
        term_min_months   = COALESCE(EXCLUDED.term_min_months, t.term_min_months),
        term_max_months   = COALESCE(EXCLUDED.term_max_months, t.term_max_months),
        zoning            = COALESCE(EXCLUDED.zoning, t.zoning),
        highlights        = COALESCE(EXCLUDED.highlights, t.highlights),
        amenities         = COALESCE(EXCLUDED.amenities, t.amenities),
        description       = COALESCE(EXCLUDED.description, t.description),
        -- markdown reuses the existing (currently-empty) column; NULLIF guards a
        -- sparse/empty pass from clobbering a fuller prior capture (COALESCE-keep).
        markdown          = CASE
                              WHEN jsonb_path_exists(
                                EXCLUDED.raw_data,
                                '$.**.preserveExistingMarkdown ? (@ == true || @ == "true")'
                              )
                                THEN COALESCE(
                                  NULLIF(t.markdown, ''),
                                  NULLIF(EXCLUDED.markdown, '')
                                )
                              ELSE COALESCE(NULLIF(EXCLUDED.markdown, ''), t.markdown)
                            END,
        updated_date      = COALESCE(EXCLUDED.updated_date, t.updated_date),
        -- `scraped_at` is detail-observation time. A detailError, or a
        -- preserveChildCollections row without an explicit
        -- detailObservedWithChildPreservation proof establishes inventory/card
        -- observation only. Avison can record a current property-detail page
        -- while preserving children when its supplemental team feed is degraded;
        -- child refresh remains additive below.
        scraped_at        = CASE
                              WHEN jsonb_path_exists(EXCLUDED.raw_data, '$.**.detailError')
                                OR (
                                  jsonb_path_exists(
                                    EXCLUDED.raw_data,
                                    '$.**.preserveChildCollections ? (@ == true || @ == "true")'
                                  )
                                  AND NOT jsonb_path_exists(
                                    EXCLUDED.raw_data,
                                    '$.**.detailObservedWithChildPreservation ? (@ == true || @ == "true")'
                                  )
                                )
                              THEN CASE
                                     WHEN jsonb_path_exists(t.raw_data, '$.detailError')
                                       OR jsonb_path_exists(t.raw_data, '$.detailUnavailable')
                                       OR jsonb_path_exists(t.raw_data, '$.primary.detailError')
                                       OR jsonb_path_exists(t.raw_data, '$.secondary_pass.detailError')
                                       OR (
                                         jsonb_path_exists(
                                           t.raw_data,
                                           '$.**.preserveChildCollections ? (@ == true || @ == "true")'
                                         )
                                         AND NOT jsonb_path_exists(
                                           t.raw_data,
                                           '$.**.detailObservedWithChildPreservation ? (@ == true || @ == "true")'
                                         )
                                       )
                                     THEN NULL
                                     ELSE t.scraped_at
                                   END
                              ELSE EXCLUDED.scraped_at
                            END,
        raw_data          = CASE
                              WHEN jsonb_path_exists(EXCLUDED.raw_data, '$.**.detailError')
                                OR (
                                  jsonb_path_exists(
                                    EXCLUDED.raw_data,
                                    '$.**.preserveChildCollections ? (@ == true || @ == "true")'
                                  )
                                  AND NOT jsonb_path_exists(
                                    EXCLUDED.raw_data,
                                    '$.**.detailObservedWithChildPreservation ? (@ == true || @ == "true")'
                                  )
                                )
                              THEN (
                                     CASE
                                       WHEN jsonb_path_exists(t.raw_data, '$.detailError')
                                         OR jsonb_path_exists(t.raw_data, '$.detailUnavailable')
                                         OR jsonb_path_exists(t.raw_data, '$.primary.detailError')
                                         OR jsonb_path_exists(t.raw_data, '$.secondary_pass.detailError')
                                         OR (
                                           jsonb_path_exists(
                                             t.raw_data,
                                             '$.**.preserveChildCollections ? (@ == true || @ == "true")'
                                           )
                                           AND NOT jsonb_path_exists(
                                             t.raw_data,
                                             '$.**.detailObservedWithChildPreservation ? (@ == true || @ == "true")'
                                           )
                                         )
                                       THEN '{}'::jsonb
                                       ELSE COALESCE(t.raw_data, '{}'::jsonb)
                                     END
                                   )
                                   || jsonb_build_object(
                                        'sourceKey', COALESCE(
                                          EXCLUDED.raw_data->'sourceKey',
                                          EXCLUDED.raw_data#>'{primary,sourceKey}',
                                          EXCLUDED.raw_data#>'{secondary_pass,sourceKey}',
                                          t.raw_data->'sourceKey',
                                          t.raw_data#>'{primary,sourceKey}',
                                          t.raw_data#>'{secondary_pass,sourceKey}'
                                        ),
                                        'latestInventoryObservation', COALESCE(
                                          EXCLUDED.raw_data->'latestInventoryObservation',
                                          EXCLUDED.raw_data
                                        ),
                                        'inventoryObservedAt', COALESCE(
                                          EXCLUDED.raw_data->'inventoryObservedAt',
                                          to_jsonb(EXCLUDED.scraped_at)
                                        )
                                      )
                              ELSE EXCLUDED.raw_data
                            END,
        source_lastmod    = COALESCE(EXCLUDED.source_lastmod, t.source_lastmod),
        canonical_key     = COALESCE(EXCLUDED.canonical_key, t.canonical_key),
        deleted_at        = NULL,
        updated_at        = now()
    RETURNING t.id, t.brokerage_id, t.external_id
)
SELECT * FROM ins;

-- Phase-2 status-flip pre-flight (board-impact doc 2026-06-13, finding 4):
-- per-source observability plus an optional circuit breaker. Reads the
-- cre.flip_max_fraction / cre.flip_min_base GUCs set above from
-- CRE_STATUS_FLIP_MAX_FRACTION (unset => breaker disabled, NOTICE-only). The
-- breaker raises (rolling back the whole transaction under ON_ERROR_STOP, so
-- nothing is written) when any one source would move more than that fraction of
-- its active inventory to a non-active status this run -- the signature of a
-- source parsing regression. NOTICE lines make the per-source flip counts of the
-- first monitored activation run inspectable. Default disabled so the unattended
-- daily ingest is never blocked.
-- (L4a) leaving_active counts ANY non-active reclassification this run, not only
-- departures from 'active'. This catches under_contract -> sold and other
-- non-active transitions that the prior active-only filter missed, giving a
-- better signal of a source regression. The trip denominator (active_base) is
-- unchanged; the terminal-guard clause preserves the intentional exemption for
-- normal sold/leased progression out of under_contract/pending.
DO $$
DECLARE
    rec record;
    v_total bigint := 0;
    v_cap numeric := NULLIF(current_setting('cre.flip_max_fraction', true), '')::numeric;
    v_min_base int := COALESCE(NULLIF(current_setting('cre.flip_min_base', true), '')::int, 200);
    v_tripped text := NULL;
BEGIN
    FOR rec IN
        SELECT b.slug AS slug,
               count(*) FILTER (
                   WHERE s.status IS NOT NULL
                     AND t.status IS DISTINCT FROM s.status
                     AND NOT (t.status IN ('sold','leased','off_market')
                              AND s.status IN ('under_contract','pending'))
               ) AS changes,
               count(*) FILTER (
                   WHERE s.status IS NOT NULL
                     AND t.status IS DISTINCT FROM s.status
                     AND s.status <> 'active'
                     AND NOT (t.status IN ('sold','leased','off_market')
                              AND s.status IN ('under_contract','pending'))
               ) AS leaving_active,
               count(*) FILTER (WHERE t.status = 'active') AS active_base
        FROM _src s
        JOIN credeals.cre_listings t
          ON t.brokerage_id = s.brokerage_id AND t.external_id = s.external_id
        JOIN credeals.cre_brokerages b ON b.id = s.brokerage_id
        GROUP BY b.slug
        HAVING count(*) FILTER (
                   WHERE s.status IS NOT NULL
                     AND t.status IS DISTINCT FROM s.status
                     AND NOT (t.status IN ('sold','leased','off_market')
                              AND s.status IN ('under_contract','pending'))
               ) > 0
    LOOP
        v_total := v_total + rec.changes;
        RAISE NOTICE 'status-flip %: % change(s), % leaving active of % active base',
            rec.slug, rec.changes, rec.leaving_active, rec.active_base;
        IF v_cap IS NOT NULL AND rec.active_base >= v_min_base
           AND rec.leaving_active::numeric / rec.active_base > v_cap THEN
            v_tripped := COALESCE(v_tripped || ', ', '') ||
                format('%s (%s/%s)', rec.slug, rec.leaving_active, rec.active_base);
        END IF;
    END LOOP;
    IF v_total > 0 THEN
        RAISE NOTICE 'status-flip TOTAL: % row(s) change status this run', v_total;
    END IF;
    IF v_tripped IS NOT NULL THEN
        RAISE EXCEPTION 'status-flip circuit breaker tripped (max fraction %): %', v_cap, v_tripped;
    END IF;
END $$;

-- Phase-2 status activation (design 12.5; Choice a COALESCE, board-impact doc
-- 2026-06-13): upgrade a row to the source's real terminal / under_contract /
-- pending signal when this run carries one. The upsert above keeps existing
-- status sticky (only resurrected rows reset to 'active'), and norm_status never
-- yields 'active', so a no-signal pass can never downgrade a prior real signal.
-- No-signal rows stay 'active'; their lifecycle stays governed by disappearance
-- / --mark-missing, not by a NULL status (no coverage cliff). _src is one row
-- per (brokerage_id, external_id), so this join is 1:1.
--
-- Terminal stickiness (guard clause below): a terminal label
-- (sold/leased/off_market) is never overwritten by a transitional
-- (under_contract/pending) re-signal across runs, matching the "only ever
-- upgraded" invariant (sold > under_contract). A truly re-listed terminal row
-- recovers via the disappear -> reappear resurrection path (the ON CONFLICT CASE
-- resets deleted_at-bearing rows to 'active'), not in place: norm_status never
-- emits 'active', so a continuously-present row keeps its terminal label. This is
-- the accepted recovery semantics (board-impact doc; not an oversight).
UPDATE credeals.cre_listings t
SET status = s.status
FROM _src s
WHERE t.brokerage_id = s.brokerage_id
  AND t.external_id = s.external_id
  AND s.status IS NOT NULL
  AND t.status IS DISTINCT FROM s.status
  AND NOT (t.status IN ('sold','leased','off_market')
           AND s.status IN ('under_contract','pending'));

-- Phase-2 data-lift: write the new institutional columns + canonical_url +
-- extra_facts with COALESCE-keep (a sparse/backfill pass NEVER clobbers a fuller
-- prior value). These columns ship in sql/012, so the whole UPDATE is guarded on
-- the presence of a representative new column (the 012 ALTER adds them together):
-- a pre-012 ingest is a clean no-op, exactly like the to_regclass guards for new
-- tables. extra_facts merges (jsonb ||) so neither pass's long-tail facts are
-- lost; a NULL/empty staged blob keeps the prior blob. _src is one row per
-- (brokerage_id, external_id), so the join is 1:1.
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'credeals' AND table_name = 'cre_listings'
      AND column_name = 'building_class'
  ) THEN
    UPDATE credeals.cre_listings t SET
        canonical_url         = COALESCE(s.canonical_url, t.canonical_url),
        building_class        = COALESCE(s.building_class, t.building_class),
        property_subtype      = COALESCE(s.property_subtype, t.property_subtype),
        apn                   = COALESCE(s.apn, t.apn),
        tenant_name           = COALESCE(s.tenant_name, t.tenant_name),
        guarantor             = COALESCE(s.guarantor, t.guarantor),
        lease_years_remaining = COALESCE(s.lease_years_remaining, t.lease_years_remaining),
        price_per_unit        = COALESCE(s.price_per_unit, t.price_per_unit),
        grm                   = COALESCE(s.grm, t.grm),
        price_per_acre        = COALESCE(s.price_per_acre, t.price_per_acre),
        num_rooms             = COALESCE(s.num_rooms, t.num_rooms),
        revpar                = COALESCE(s.revpar, t.revpar),
        clear_height_ft       = COALESCE(s.clear_height_ft, t.clear_height_ft),
        dock_doors            = COALESCE(s.dock_doors, t.dock_doors),
        drive_in_doors        = COALESCE(s.drive_in_doors, t.drive_in_doors),
        power_service         = COALESCE(s.power_service, t.power_service),
        rail_served           = COALESCE(s.rail_served, t.rail_served),
        -- extra_facts: jsonb merge, keeping prior keys; a NULL/empty staged blob
        -- (no new facts this pass) leaves the prior blob untouched.
        extra_facts           = CASE
                                  WHEN s.extra_facts IS NULL
                                       OR s.extra_facts = '{}'::jsonb THEN COALESCE(t.extra_facts, '{}'::jsonb)
                                  ELSE COALESCE(t.extra_facts, '{}'::jsonb) || s.extra_facts
                                END
    FROM _src s
    WHERE t.brokerage_id = s.brokerage_id
      AND t.external_id = s.external_id;
  END IF;
END $$;

-- Children: refresh wholesale only when the latest source row completed a
-- detail pass. This protects previously good documents/images/contacts from
-- transient errors and from deliberate API-only/base refresh modes.
CREATE TEMP TABLE _child_refresh ON COMMIT DROP AS
SELECT DISTINCT u.id
FROM _up u
JOIN _src s USING (brokerage_id, external_id)
WHERE NOT jsonb_path_exists(s.raw_data, '$.**.detailError')
  AND (
    NOT jsonb_path_exists(
      s.raw_data,
      '$.**.preserveChildCollections ? (@ == true || @ == "true")'
    )
    OR NOT EXISTS (
      SELECT 1
      FROM _prior_vals p
      WHERE p.brokerage_id = s.brokerage_id
        AND p.external_id = s.external_id
    )
  );

-- Existing sparse/detail-unavailable rows retain their last-good child
-- collections, but current public-card contacts/images/documents must still be
-- visible. Add those current card children idempotently below without deleting
-- older detail-derived rows whose continued availability cannot be rechecked.
CREATE TEMP TABLE _child_additive ON COMMIT DROP AS
SELECT DISTINCT u.id
FROM _up u
JOIN _src s USING (brokerage_id, external_id)
JOIN _prior_vals p
  ON p.brokerage_id = s.brokerage_id
 AND p.external_id = s.external_id
WHERE jsonb_path_exists(s.raw_data, '$.**.detailError')
   OR jsonb_path_exists(
        s.raw_data,
        '$.**.preserveChildCollections ? (@ == true || @ == "true")'
      );

DELETE FROM credeals.cre_listing_contacts  WHERE listing_id IN (SELECT id FROM _child_refresh);
DELETE FROM credeals.cre_listing_documents WHERE listing_id IN (SELECT id FROM _child_refresh);
DELETE FROM credeals.cre_listing_images    WHERE listing_id IN (SELECT id FROM _child_refresh);

-- Contacts refresh. The `license` column ships in sql/012, so the INSERT is
-- column-existence-guarded: when present, license rides along; when absent
-- (pre-012), the exact prior column list is used so the ingest is unchanged.
-- Both branches are otherwise byte-identical to the prior INSERT.
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'credeals' AND table_name = 'cre_listing_contacts'
      AND column_name = 'license'
  ) THEN
    INSERT INTO credeals.cre_listing_contacts (
        listing_id, name, title, license, email, phone, brokerage_name,
        profile_url, avatar_url, vcard_url, is_primary
    )
    SELECT u.id, x->>'name', x->>'title', x->>'license', x->>'email', x->>'phone', x->>'company',
           x->>'profileUrl', x->>'avatarUrl', x->>'vcardUrl',
           COALESCE((x->>'isPrimary')::boolean, false)
    FROM _up u
    JOIN _src s USING (brokerage_id, external_id)
    CROSS JOIN LATERAL jsonb_array_elements(s.contacts) x
    WHERE u.id IN (SELECT id FROM _child_refresh)
      AND jsonb_typeof(s.contacts) = 'array';
  ELSE
    INSERT INTO credeals.cre_listing_contacts (
        listing_id, name, title, email, phone, brokerage_name,
        profile_url, avatar_url, vcard_url, is_primary
    )
    SELECT u.id, x->>'name', x->>'title', x->>'email', x->>'phone', x->>'company',
           x->>'profileUrl', x->>'avatarUrl', x->>'vcardUrl',
           COALESCE((x->>'isPrimary')::boolean, false)
    FROM _up u
    JOIN _src s USING (brokerage_id, external_id)
    CROSS JOIN LATERAL jsonb_array_elements(s.contacts) x
    WHERE u.id IN (SELECT id FROM _child_refresh)
      AND jsonb_typeof(s.contacts) = 'array';
  END IF;
END $$;

INSERT INTO credeals.cre_listing_documents (listing_id, doc_type, title, url)
SELECT u.id,
       CASE WHEN x->>'docType' IN ('brochure','om','flyer','floor_plan','financials','rent_roll')
            THEN x->>'docType' ELSE 'other' END,
       x->>'title', x->>'url'
FROM _up u
JOIN _src s USING (brokerage_id, external_id)
CROSS JOIN LATERAL jsonb_array_elements(s.documents) x
WHERE u.id IN (SELECT id FROM _child_refresh)
  AND jsonb_typeof(s.documents) = 'array' AND x->>'url' IS NOT NULL;

INSERT INTO credeals.cre_listing_images (listing_id, url, is_primary, display_order)
SELECT u.id, x->>'url', COALESCE((x->>'isPrimary')::boolean, false),
       COALESCE((x->>'order')::integer, 0)
FROM _up u
JOIN _src s USING (brokerage_id, external_id)
CROSS JOIN LATERAL jsonb_array_elements(s.images) x
WHERE u.id IN (SELECT id FROM _child_refresh)
  AND jsonb_typeof(s.images) = 'array' AND x->>'url' IS NOT NULL;

-- Sparse existing rows: upsert the contacts and images that were observed on
-- the current public card, without deleting last-good detail children.
UPDATE credeals.cre_listing_contacts c
SET is_primary = false
FROM _up u
JOIN _src s USING (brokerage_id, external_id)
CROSS JOIN LATERAL jsonb_array_elements(s.contacts) x
WHERE u.id IN (SELECT id FROM _child_additive)
  AND c.listing_id = u.id
  AND jsonb_typeof(s.contacts) = 'array'
  AND COALESCE((x->>'isPrimary')::boolean, false);

DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'credeals' AND table_name = 'cre_listing_contacts'
      AND column_name = 'license'
  ) THEN
    WITH incoming AS (
      SELECT u.id AS listing_id, x
      FROM _up u
      JOIN _src s USING (brokerage_id, external_id)
      CROSS JOIN LATERAL jsonb_array_elements(s.contacts) x
      WHERE u.id IN (SELECT id FROM _child_additive)
        AND jsonb_typeof(s.contacts) = 'array'
    )
    UPDATE credeals.cre_listing_contacts c SET
      name = COALESCE(i.x->>'name', c.name),
      title = COALESCE(i.x->>'title', c.title),
      license = COALESCE(i.x->>'license', c.license),
      email = COALESCE(i.x->>'email', c.email),
      phone = COALESCE(i.x->>'phone', c.phone),
      brokerage_name = COALESCE(i.x->>'company', c.brokerage_name),
      profile_url = COALESCE(i.x->>'profileUrl', c.profile_url),
      avatar_url = COALESCE(i.x->>'avatarUrl', c.avatar_url),
      vcard_url = COALESCE(i.x->>'vcardUrl', c.vcard_url),
      is_primary = COALESCE((i.x->>'isPrimary')::boolean, c.is_primary)
    FROM incoming i
    WHERE c.listing_id = i.listing_id
      AND (
        (NULLIF(lower(i.x->>'email'), '') IS NOT NULL
         AND lower(c.email) = lower(i.x->>'email'))
        OR
        (NULLIF(lower(i.x->>'name'), '') IS NOT NULL
         AND lower(c.name) = lower(i.x->>'name')
         AND COALESCE(c.phone, '') = COALESCE(i.x->>'phone', ''))
      );

    INSERT INTO credeals.cre_listing_contacts (
      listing_id, name, title, license, email, phone, brokerage_name,
      profile_url, avatar_url, vcard_url, is_primary
    )
    SELECT u.id, x->>'name', x->>'title', x->>'license', x->>'email', x->>'phone',
           x->>'company', x->>'profileUrl', x->>'avatarUrl', x->>'vcardUrl',
           COALESCE((x->>'isPrimary')::boolean, false)
    FROM _up u
    JOIN _src s USING (brokerage_id, external_id)
    CROSS JOIN LATERAL jsonb_array_elements(s.contacts) x
    WHERE u.id IN (SELECT id FROM _child_additive)
      AND jsonb_typeof(s.contacts) = 'array'
      AND NOT EXISTS (
        SELECT 1 FROM credeals.cre_listing_contacts c
        WHERE c.listing_id = u.id
          AND (
            (NULLIF(lower(x->>'email'), '') IS NOT NULL
             AND lower(c.email) = lower(x->>'email'))
            OR
            (NULLIF(lower(x->>'name'), '') IS NOT NULL
             AND lower(c.name) = lower(x->>'name')
             AND COALESCE(c.phone, '') = COALESCE(x->>'phone', ''))
          )
      );
  ELSE
    WITH incoming AS (
      SELECT u.id AS listing_id, x
      FROM _up u
      JOIN _src s USING (brokerage_id, external_id)
      CROSS JOIN LATERAL jsonb_array_elements(s.contacts) x
      WHERE u.id IN (SELECT id FROM _child_additive)
        AND jsonb_typeof(s.contacts) = 'array'
    )
    UPDATE credeals.cre_listing_contacts c SET
      name = COALESCE(i.x->>'name', c.name),
      title = COALESCE(i.x->>'title', c.title),
      email = COALESCE(i.x->>'email', c.email),
      phone = COALESCE(i.x->>'phone', c.phone),
      brokerage_name = COALESCE(i.x->>'company', c.brokerage_name),
      profile_url = COALESCE(i.x->>'profileUrl', c.profile_url),
      avatar_url = COALESCE(i.x->>'avatarUrl', c.avatar_url),
      vcard_url = COALESCE(i.x->>'vcardUrl', c.vcard_url),
      is_primary = COALESCE((i.x->>'isPrimary')::boolean, c.is_primary)
    FROM incoming i
    WHERE c.listing_id = i.listing_id
      AND (
        (NULLIF(lower(i.x->>'email'), '') IS NOT NULL
         AND lower(c.email) = lower(i.x->>'email'))
        OR
        (NULLIF(lower(i.x->>'name'), '') IS NOT NULL
         AND lower(c.name) = lower(i.x->>'name')
         AND COALESCE(c.phone, '') = COALESCE(i.x->>'phone', ''))
      );

    INSERT INTO credeals.cre_listing_contacts (
      listing_id, name, title, email, phone, brokerage_name,
      profile_url, avatar_url, vcard_url, is_primary
    )
    SELECT u.id, x->>'name', x->>'title', x->>'email', x->>'phone',
           x->>'company', x->>'profileUrl', x->>'avatarUrl', x->>'vcardUrl',
           COALESCE((x->>'isPrimary')::boolean, false)
    FROM _up u
    JOIN _src s USING (brokerage_id, external_id)
    CROSS JOIN LATERAL jsonb_array_elements(s.contacts) x
    WHERE u.id IN (SELECT id FROM _child_additive)
      AND jsonb_typeof(s.contacts) = 'array'
      AND NOT EXISTS (
        SELECT 1 FROM credeals.cre_listing_contacts c
        WHERE c.listing_id = u.id
          AND (
            (NULLIF(lower(x->>'email'), '') IS NOT NULL
             AND lower(c.email) = lower(x->>'email'))
            OR
            (NULLIF(lower(x->>'name'), '') IS NOT NULL
             AND lower(c.name) = lower(x->>'name')
             AND COALESCE(c.phone, '') = COALESCE(x->>'phone', ''))
          )
      );
  END IF;
END $$;

-- `is_primary` is singular. Legacy duplicate match keys can cause an additive
-- UPDATE to touch more than one existing contact; deterministically retain one
-- primary on every listing touched by this ingest.
WITH ranked_primary_contacts AS (
  SELECT
    c.id,
    row_number() OVER (PARTITION BY c.listing_id ORDER BY c.id) AS ordinal
  FROM credeals.cre_listing_contacts c
  JOIN _up u ON u.id = c.listing_id
  WHERE c.is_primary
)
UPDATE credeals.cre_listing_contacts c
SET is_primary = false
FROM ranked_primary_contacts ranked
WHERE c.id = ranked.id
  AND ranked.ordinal > 1;

UPDATE credeals.cre_listing_images i
SET is_primary = false
FROM _up u
JOIN _src s USING (brokerage_id, external_id)
CROSS JOIN LATERAL jsonb_array_elements(s.images) x
WHERE u.id IN (SELECT id FROM _child_additive)
  AND i.listing_id = u.id
  AND jsonb_typeof(s.images) = 'array'
  AND COALESCE((x->>'isPrimary')::boolean, false);

WITH incoming AS (
  SELECT u.id AS listing_id, x
  FROM _up u
  JOIN _src s USING (brokerage_id, external_id)
  CROSS JOIN LATERAL jsonb_array_elements(s.images) x
  WHERE u.id IN (SELECT id FROM _child_additive)
    AND jsonb_typeof(s.images) = 'array'
    AND x->>'url' IS NOT NULL
)
UPDATE credeals.cre_listing_images i SET
  is_primary = COALESCE((incoming.x->>'isPrimary')::boolean, i.is_primary),
  display_order = COALESCE((incoming.x->>'order')::integer, i.display_order)
FROM incoming
WHERE i.listing_id = incoming.listing_id
  AND i.url = incoming.x->>'url';

INSERT INTO credeals.cre_listing_images (listing_id, url, is_primary, display_order)
SELECT u.id, x->>'url', COALESCE((x->>'isPrimary')::boolean, false),
       COALESCE((x->>'order')::integer, 0)
FROM _up u
JOIN _src s USING (brokerage_id, external_id)
CROSS JOIN LATERAL jsonb_array_elements(s.images) x
WHERE u.id IN (SELECT id FROM _child_additive)
  AND jsonb_typeof(s.images) = 'array'
  AND x->>'url' IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM credeals.cre_listing_images i
    WHERE i.listing_id = u.id AND i.url = x->>'url'
  );

WITH incoming AS (
  SELECT u.id AS listing_id, x
  FROM _up u
  JOIN _src s USING (brokerage_id, external_id)
  CROSS JOIN LATERAL jsonb_array_elements(s.documents) x
  WHERE u.id IN (SELECT id FROM _child_additive)
    AND jsonb_typeof(s.documents) = 'array'
    AND x->>'url' IS NOT NULL
)
UPDATE credeals.cre_listing_documents d SET
  doc_type = CASE
               WHEN incoming.x->>'docType' IN (
                 'brochure','om','flyer','floor_plan','financials','rent_roll'
               ) THEN incoming.x->>'docType'
               ELSE d.doc_type
             END,
  title = COALESCE(incoming.x->>'title', d.title),
  scraped_at = now()
FROM incoming
WHERE d.listing_id = incoming.listing_id
  AND d.url = incoming.x->>'url';

INSERT INTO credeals.cre_listing_documents (listing_id, doc_type, title, url)
SELECT u.id,
       CASE WHEN x->>'docType' IN ('brochure','om','flyer','floor_plan','financials','rent_roll')
            THEN x->>'docType' ELSE 'other' END,
       x->>'title', x->>'url'
FROM _up u
JOIN _src s USING (brokerage_id, external_id)
CROSS JOIN LATERAL jsonb_array_elements(s.documents) x
WHERE u.id IN (SELECT id FROM _child_additive)
  AND jsonb_typeof(s.documents) = 'array'
  AND x->>'url' IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM credeals.cre_listing_documents d
    WHERE d.listing_id = u.id AND d.url = x->>'url'
  );

-- Media + links (sql/011 tables). Existence-guarded so a pre-011 ingest is a
-- no-op (mirrors the 009 to_regclass guard pattern): the DELETE and re-INSERT
-- are wrapped together per table, since a DELETE with no table to refill would
-- be harmful. The _child_refresh set already excludes detailError rows, so the
-- wholesale-replace fires only on a CLEAN detail touch (mirrors images exactly).
-- media_type/link_type participate in the unique key because one url can
-- legitimately appear once per type; ON CONFLICT ... DO NOTHING dedups in-batch.
-- Existing child-preserving rows also add newly observed media/links without
-- deleting last-good rows. This rides the generic _child_additive contract, not
-- a source-specific branch, and uses the sql/011 unique keys for idempotence.
DO $$ BEGIN
  IF to_regclass('credeals.cre_listing_media') IS NOT NULL THEN
    DELETE FROM credeals.cre_listing_media WHERE listing_id IN (SELECT id FROM _child_refresh);
    INSERT INTO credeals.cre_listing_media (listing_id, media_type, provider, url, embed_url, title)
    SELECT u.id, COALESCE(x->>'mediaType','other'), x->>'provider', x->>'url',
           x->>'embedUrl', x->>'title'
    FROM _up u
    JOIN _src s USING (brokerage_id, external_id)
    CROSS JOIN LATERAL jsonb_array_elements(s.media) x
    WHERE u.id IN (SELECT id FROM _child_refresh)
      AND jsonb_typeof(s.media) = 'array' AND x->>'url' IS NOT NULL
    ON CONFLICT (listing_id, media_type, url) DO NOTHING;

    INSERT INTO credeals.cre_listing_media (listing_id, media_type, provider, url, embed_url, title)
    SELECT u.id, COALESCE(x->>'mediaType','other'), x->>'provider', x->>'url',
           x->>'embedUrl', x->>'title'
    FROM _up u
    JOIN _src s USING (brokerage_id, external_id)
    CROSS JOIN LATERAL jsonb_array_elements(s.media) x
    WHERE u.id IN (SELECT id FROM _child_additive)
      AND jsonb_typeof(s.media) = 'array' AND x->>'url' IS NOT NULL
    ON CONFLICT (listing_id, media_type, url) DO NOTHING;
  END IF;
END $$;

DO $$ BEGIN
  IF to_regclass('credeals.cre_listing_links') IS NOT NULL THEN
    DELETE FROM credeals.cre_listing_links WHERE listing_id IN (SELECT id FROM _child_refresh);
    INSERT INTO credeals.cre_listing_links (listing_id, link_type, url, rel)
    SELECT u.id, COALESCE(x->>'linkType','other'), x->>'url', x->>'rel'
    FROM _up u
    JOIN _src s USING (brokerage_id, external_id)
    CROSS JOIN LATERAL jsonb_array_elements(s.links) x
    WHERE u.id IN (SELECT id FROM _child_refresh)
      AND jsonb_typeof(s.links) = 'array' AND x->>'url' IS NOT NULL
    ON CONFLICT (listing_id, link_type, url) DO NOTHING;

    INSERT INTO credeals.cre_listing_links (listing_id, link_type, url, rel)
    SELECT u.id, COALESCE(x->>'linkType','other'), x->>'url', x->>'rel'
    FROM _up u
    JOIN _src s USING (brokerage_id, external_id)
    CROSS JOIN LATERAL jsonb_array_elements(s.links) x
    WHERE u.id IN (SELECT id FROM _child_additive)
      AND jsonb_typeof(s.links) = 'array' AND x->>'url' IS NOT NULL
    ON CONFLICT (listing_id, link_type, url) DO NOTHING;
  END IF;
END $$;

-- OM-parsed facts (sql/013 cre_listing_om_facts). Existence-guarded so a pre-013
-- ingest is a no-op. UNLIKE media/links, om_facts is NOT wholesale-deleted on
-- refresh: it is a provenance-bearing audit trail, and a normal (non-OM) detail
-- pass carries no om_facts, so a DELETE would wipe a prior OM parse. Insert-only
-- with ON CONFLICT DO UPDATE on the (listing_id, fact_group, fact_key,
-- source_doc_url, parser_version) unique key, so a re-parse of the SAME parser
-- release and doc is idempotent (refreshes value/confidence/parsed_at), while a
-- new parser release or doc adds an auditable row. Rides the same _child_refresh
-- set, so detailError rows never write (invariant). The OM tier sets the matching
-- cre_listings scalar via the institutional UPDATE / COALESCE-keep path; this
-- table is the audit home. parser_version/source_doc_url are NOT NULL in the
-- table. Firecrawl ingestion stages an empty om_facts array by policy; this
-- retained upsert shape protects migration compatibility and archive handling.
DO $$ BEGIN
  IF to_regclass('credeals.cre_listing_om_facts') IS NOT NULL THEN
    INSERT INTO credeals.cre_listing_om_facts (
        listing_id, fact_group, fact_key, fact_value_text, fact_value_num,
        unit_count, source_doc_url, parsed_at, parser_version, confidence)
    SELECT u.id, COALESCE(x->>'factGroup','scalar'), x->>'factKey',
           x->>'factValueText', NULLIF(x->>'factValueNum','')::numeric,
           NULLIF(x->>'unitCount','')::integer, x->>'sourceDocUrl', now(),
           x->>'parserVersion', NULLIF(x->>'confidence','')::numeric
    FROM _up u
    JOIN _src s USING (brokerage_id, external_id)
    CROSS JOIN LATERAL jsonb_array_elements(s.om_facts) x
    WHERE u.id IN (SELECT id FROM _child_refresh)
      AND jsonb_typeof(s.om_facts) = 'array'
      AND x->>'factKey' IS NOT NULL
      AND x->>'sourceDocUrl' IS NOT NULL
      AND x->>'parserVersion' IS NOT NULL
    ON CONFLICT (listing_id, fact_group, fact_key, source_doc_url, parser_version) DO UPDATE SET
        fact_value_text = EXCLUDED.fact_value_text,
        fact_value_num  = EXCLUDED.fact_value_num,
        unit_count      = EXCLUDED.unit_count,
        parsed_at       = EXCLUDED.parsed_at,
        parser_version  = EXCLUDED.parser_version,
        confidence      = EXCLUDED.confidence;
  END IF;
END $$;
""")

    # (H4a) Append-only price/status history: one row per listing whose watched
    # value actually changed this run. The diff is computed from the new effective
    # values (post-upsert, post-activation) vs _prior_vals (pre-upsert snapshot).
    # When history_guard=True (real apply), the INSERT is wrapped in a DO $$ IF
    # to_regclass(...) IS NOT NULL $$ block so a pre-apply prod ingest is a no-op.
    # When history_guard=False (--dry-run path), the INSERT is emitted as a plain
    # top-level statement so offline tests can assert on it directly.
    _history_col_list = (
        "(listing_id, observed_at, sale_price_usd, sale_price_per_sf,\n"
        "     lease_rate_min, lease_rate_max, status, cap_rate, source_lastmod, transaction_type)"
    )
    _history_insert_body = f"""INSERT INTO credeals.cre_listing_price_history
    {_history_col_list}
SELECT t.id, now(), t.sale_price_usd, t.sale_price_per_sf,
       t.lease_rate_min, t.lease_rate_max, t.status, t.cap_rate, t.source_lastmod, t.transaction_type
FROM credeals.cre_listings t
JOIN _prior_vals p ON p.id = t.id
WHERE t.sale_price_usd    IS DISTINCT FROM p.sale_price_usd
   OR t.sale_price_per_sf IS DISTINCT FROM p.sale_price_per_sf
   OR t.lease_rate_min    IS DISTINCT FROM p.lease_rate_min
   OR t.lease_rate_max    IS DISTINCT FROM p.lease_rate_max
   OR t.status            IS DISTINCT FROM p.status
   OR t.cap_rate          IS DISTINCT FROM p.cap_rate;"""
    if history_guard:
        w(f"""
-- (H4a) Append-only price/status history. Existence-guarded: no-op when the
-- table is not yet applied to prod. Only fires when a watched field changed.
DO $$ BEGIN
  IF to_regclass('credeals.cre_listing_price_history') IS NOT NULL THEN
    {_history_insert_body}
  END IF;
END $$;""")
    else:
        w(f"""
-- (H4a) Append-only price/status history (unguarded for dry-run/offline tests).
{_history_insert_body}""")

    if mark_missing_slugs:
        slug_list = ", ".join("'" + s.replace("'", "''") + "'" for s in sorted(mark_missing_slugs))
        # (M3) Capture the soon-to-be-retired listings BEFORE the UPDATE overwrites
        # status, so the disappeared event can record the prior status as old_value.
        # The _retired temp table also drives the M2 archive INSERTs below.
        w(f"""
-- Full-run reconciliation: soft-delete listings this clean full run no longer
-- sees. Capture the retired set FIRST (with prior status) so the disappeared
-- event and the contact/document archive snapshot reference the same rows in
-- this one transaction.
CREATE TEMP TABLE _retired ON COMMIT DROP AS
SELECT l.id, l.brokerage_id, l.status AS prior_status
FROM credeals.cre_listings l
JOIN credeals.cre_brokerages b ON l.brokerage_id = b.id
WHERE b.slug IN ({slug_list})
  AND l.deleted_at IS NULL
  AND l.external_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM _up u WHERE u.id = l.id);

UPDATE credeals.cre_listings l
SET deleted_at = now(), status = 'inactive', updated_at = now()
FROM _retired r
JOIN credeals.cre_brokerages b ON b.id = r.brokerage_id
WHERE l.id = r.id
  AND b.slug IN ({slug_list});

INSERT INTO credeals.cre_listing_events
    (listing_id, brokerage_id, event_type, field, old_value, new_value,
     source_value, detected_at)
SELECT r.id, r.brokerage_id, 'disappeared', 'status', r.prior_status, 'inactive',
       'mark_missing', now()
FROM _retired r
ON CONFLICT (listing_id, event_type, COALESCE(field, ''), COALESCE(new_value, ''), scrape_job_id)
DO NOTHING;""")
        # (M2) Snapshot contacts and documents of the retired listings into the
        # append-only archives. Images are excluded (high volume, low historical
        # value). Guarded so a pre-apply ingest is a no-op. Column lists are
        # verbatim from spec section 2.5 / 3.4 so both sides of the contract agree.
        _contacts_insert = """INSERT INTO credeals.cre_listing_contacts_archive
    (source_listing_id, name, title, email, phone, brokerage_name,
     profile_url, avatar_url, vcard_url, is_primary)
SELECT c.listing_id, c.name, c.title, c.email, c.phone, c.brokerage_name,
       c.profile_url, c.avatar_url, c.vcard_url, c.is_primary
FROM credeals.cre_listing_contacts c
JOIN _retired r ON r.id = c.listing_id;"""
        _documents_insert = """INSERT INTO credeals.cre_listing_documents_archive
    (source_listing_id, doc_type, title, url)
SELECT d.listing_id, d.doc_type, d.title, d.url
FROM credeals.cre_listing_documents d
JOIN _retired r ON r.id = d.listing_id;"""
        # (011) Snapshot media + links of the retired listings into their archive
        # mirrors. These tables ship in sql/011, so the INSERTs are ALWAYS
        # to_regclass-guarded (even in the dry-run path) -- 011 may not be applied
        # yet, unlike the 009 contacts/documents archives above.
        _media_archive_insert = """
-- (011) Archive final media of retired listings. Existence-guarded.
DO $$ BEGIN
  IF to_regclass('credeals.cre_listing_media_archive') IS NOT NULL
     AND to_regclass('credeals.cre_listing_media') IS NOT NULL THEN
    INSERT INTO credeals.cre_listing_media_archive
        (source_listing_id, media_type, provider, url, embed_url, title)
    SELECT m.listing_id, m.media_type, m.provider, m.url, m.embed_url, m.title
    FROM credeals.cre_listing_media m
    JOIN _retired r ON r.id = m.listing_id;
  END IF;
END $$;

-- (011) Archive final links of retired listings. Existence-guarded.
DO $$ BEGIN
  IF to_regclass('credeals.cre_listing_links_archive') IS NOT NULL
     AND to_regclass('credeals.cre_listing_links') IS NOT NULL THEN
    INSERT INTO credeals.cre_listing_links_archive
        (source_listing_id, link_type, url, rel)
    SELECT lk.listing_id, lk.link_type, lk.url, lk.rel
    FROM credeals.cre_listing_links lk
    JOIN _retired r ON r.id = lk.listing_id;
  END IF;
END $$;

-- (013) Archive final OM facts of retired listings. Existence-guarded (013 may
-- not be applied yet), mirroring the 011 media/links archive snapshot.
DO $$ BEGIN
  IF to_regclass('credeals.cre_listing_om_facts_archive') IS NOT NULL
     AND to_regclass('credeals.cre_listing_om_facts') IS NOT NULL THEN
    INSERT INTO credeals.cre_listing_om_facts_archive
        (source_listing_id, fact_group, fact_key, fact_value_text, fact_value_num,
         unit_count, source_doc_url, parsed_at, parser_version, confidence)
    SELECT f.listing_id, f.fact_group, f.fact_key, f.fact_value_text, f.fact_value_num,
           f.unit_count, f.source_doc_url, f.parsed_at, f.parser_version, f.confidence
    FROM credeals.cre_listing_om_facts f
    JOIN _retired r ON r.id = f.listing_id;
  END IF;
END $$;"""
        if history_guard:
            w(f"""
-- (M2) Archive final contacts of retired listings. Existence-guarded.
DO $$ BEGIN
  IF to_regclass('credeals.cre_listing_contacts_archive') IS NOT NULL THEN
    {_contacts_insert}
  END IF;
END $$;

-- (M2) Archive final documents of retired listings. Existence-guarded.
DO $$ BEGIN
  IF to_regclass('credeals.cre_listing_documents_archive') IS NOT NULL THEN
    {_documents_insert}
  END IF;
END $$;
{_media_archive_insert}""")
        else:
            w(f"""
-- (M2) Archive final contacts of retired listings (unguarded for dry-run).
{_contacts_insert}

-- (M2) Archive final documents of retired listings (unguarded for dry-run).
{_documents_insert}
{_media_archive_insert}""")

    w(f"""
INSERT INTO credeals.cre_scrape_jobs
    (brokerage_id, status, started_at, completed_at,
     listings_discovered, listings_scraped, listings_saved, errors_count, notes)
SELECT b.id,
       CASE WHEN jm.errors > 0 THEN 'partial' ELSE 'completed' END,
       {sql_lit(started_at)}::timestamptz, now(),
       jm.discovered, jm.saved, jm.saved, jm.errors, jm.notes
FROM _jobmeta jm JOIN credeals.cre_brokerages b ON b.slug = jm.slug;

-- Enforce the severe child-loss predicate before COMMIT so a parser collapse
-- rolls back listings, children, history, and the scrape-job row atomically.
-- Compare the same pre-existing parents on both sides. Mark-missing may retire
-- parents and archive their children without constituting a child parser
-- regression; removing children from parents that remain active is guarded.
CREATE TEMP TABLE _retained_child_scope_after ON COMMIT DROP AS
SELECT before.listing_id, before.source_key
FROM _active_child_scope_before before
JOIN credeals.cre_listings l ON l.id = before.listing_id
WHERE l.deleted_at IS NULL;

CREATE TEMP TABLE _child_counts_before (
  source_key text NOT NULL,
  child_type text NOT NULL,
  child_count bigint NOT NULL,
  PRIMARY KEY (source_key, child_type)
) ON COMMIT DROP;

INSERT INTO _child_counts_before
SELECT snapshot.source_key, snapshot.child_type, sum(snapshot.child_count)
FROM _child_counts_by_listing_before snapshot
JOIN _retained_child_scope_after retained
  USING (listing_id, source_key)
GROUP BY snapshot.source_key, snapshot.child_type;

CREATE TEMP TABLE _child_counts_after (
  source_key text NOT NULL,
  child_type text NOT NULL,
  child_count bigint NOT NULL,
  PRIMARY KEY (source_key, child_type)
) ON COMMIT DROP;

INSERT INTO _child_counts_after
SELECT a.source_key, 'contacts', count(c.id)
FROM _retained_child_scope_after a
LEFT JOIN credeals.cre_listing_contacts c ON c.listing_id = a.listing_id
GROUP BY a.source_key
UNION ALL
SELECT a.source_key, 'documents', count(d.id)
FROM _retained_child_scope_after a
LEFT JOIN credeals.cre_listing_documents d ON d.listing_id = a.listing_id
GROUP BY a.source_key
UNION ALL
SELECT a.source_key, 'images', count(i.id)
FROM _retained_child_scope_after a
LEFT JOIN credeals.cre_listing_images i ON i.listing_id = a.listing_id
GROUP BY a.source_key;

DO $$ BEGIN
  IF EXISTS (
    SELECT 1
    WHERE to_regclass('credeals.cre_listing_media') IS NOT NULL
  ) THEN
    INSERT INTO _child_counts_after
    SELECT a.source_key, 'media', count(m.id)
    FROM _retained_child_scope_after a
    LEFT JOIN credeals.cre_listing_media m ON m.listing_id = a.listing_id
    GROUP BY a.source_key;
  END IF;
  IF EXISTS (
    SELECT 1
    WHERE to_regclass('credeals.cre_listing_links') IS NOT NULL
  ) THEN
    INSERT INTO _child_counts_after
    SELECT a.source_key, 'links', count(k.id)
    FROM _retained_child_scope_after a
    LEFT JOIN credeals.cre_listing_links k ON k.listing_id = a.listing_id
    GROUP BY a.source_key;
  END IF;
END $$;

DO $$
DECLARE regressions text;
BEGIN
  SELECT string_agg(
    format(
      '%s/%s fell more than 30%%: %s->%s',
      prior_counts.source_key,
      prior_counts.child_type,
      prior_counts.child_count,
      COALESCE(current_counts.child_count, 0)
    ),
    '; ' ORDER BY prior_counts.source_key, prior_counts.child_type
  )
  INTO regressions
  FROM _child_counts_before prior_counts
  LEFT JOIN _child_counts_after current_counts
    USING (source_key, child_type)
  WHERE prior_counts.child_count >= {CHILD_COUNT_MIN_BASE}
    AND COALESCE(current_counts.child_count, 0)
      < (
        prior_counts.child_count * {CHILD_COUNT_RETAIN_NUMERATOR}
        / {CHILD_COUNT_RETAIN_DENOMINATOR}
      );

  IF regressions IS NOT NULL THEN
    RAISE EXCEPTION
      'checkpoint child quality regression before commit: %',
      regressions;
  END IF;
END $$;

COMMIT;

\\echo ''
\\echo '=== credeals.cre_listings after ingest ==='
SELECT b.slug,
       count(*) FILTER (WHERE l.deleted_at IS NULL)                                   AS active,
       count(*) FILTER (WHERE l.deleted_at IS NULL AND l.transaction_type = 'sale')   AS sale,
       count(*) FILTER (WHERE l.deleted_at IS NULL AND l.transaction_type = 'lease')  AS lease,
       count(*) FILTER (WHERE l.deleted_at IS NULL AND l.transaction_type = 'sale_or_lease') AS both,
       count(*) FILTER (WHERE l.deleted_at IS NOT NULL)                               AS soft_deleted
FROM credeals.cre_listings l
JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
GROUP BY 1 ORDER BY 1;
""")
    return "\n".join(lines)


def sql_lit(s):
    return "'" + str(s).replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# Environment / psql plumbing
# ---------------------------------------------------------------------------


def load_db_url(env_file):
    using_legacy_fallback = False
    if env_file:
        # Expand ~ for parity with the CRE_ENV_FILE / defaults branches below,
        # so a --env-file value that was not shell-expanded still resolves.
        candidates = [os.path.expanduser(env_file)]
    else:
        # CRE_ENV_FILE (if set) takes precedence over the hardcoded ~/Documents
        # defaults so the pipeline is portable to any clone location / machine.
        candidates = []
        env_override = os.environ.get("CRE_ENV_FILE")
        if env_override:
            candidates.append(os.path.expanduser(env_override))
        else:
            using_legacy_fallback = True
        candidates.extend(os.path.expanduser(p) for p in ENV_FILE_CANDIDATES)
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        env = {}
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
        url = env.get("POSTGRES_URL_NON_POOLING") or env.get("POSTGRES_URL")
        if url:
            if using_legacy_fallback:
                print(
                    "WARNING: using legacy CRE env-file fallback. Set CRE_ENV_FILE "
                    "or pass --env-file before a real ingest; the selected path is "
                    f"{path} (database URL omitted).",
                    file=sys.stderr,
                )
            return url, path
    sys.exit(
        "No POSTGRES_URL_NON_POOLING/POSTGRES_URL found. Set CRE_ENV_FILE or pass "
        "--env-file pointing at an env file that has one (values are never printed)."
    )


DATABASE_TARGET_OVERRIDE_QUERY_KEYS = {
    "dbname",
    "host",
    "hostaddr",
    "port",
    "service",
    "servicefile",
}


def strict_uri_unquote(value, *, label):
    """Decode one URI component as strict UTF-8 after validating every escape."""
    invalid_escape = re.search(r"%(?![0-9A-Fa-f]{2})", value)
    if invalid_escape is not None:
        raise ValueError(f"{label} contains an invalid percent escape")
    try:
        return unquote_to_bytes(value).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not valid UTF-8") from exc


def parse_postgres_uri_query(query):
    """Strictly decode a PostgreSQL URI query without form-style ``+`` folding."""
    if not query:
        return []
    pairs = []
    for component in query.split("&"):
        raw_key, separator, raw_value = component.partition("=")
        if not separator:
            raw_value = ""
        pairs.append(
            (
                strict_uri_unquote(raw_key, label="PostgreSQL URL query key"),
                strict_uri_unquote(raw_value, label="PostgreSQL URL query value"),
            )
        )
    return pairs


def database_target_fingerprint_from_url(db_url):
    """Return a credential-free hash for one unambiguous PostgreSQL URI target."""
    parsed = urlsplit(db_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("selected database URL has no usable PostgreSQL host")
    authority = parsed.netloc.rsplit("@", 1)[-1]
    decoded_hostname = strict_uri_unquote(
        parsed.hostname, label="PostgreSQL URL host"
    )
    if "," in authority or "," in decoded_hostname:
        raise ValueError("multi-host PostgreSQL URLs are not supported")
    override_keys = {
        key.lower()
        for key, _value in parse_postgres_uri_query(parsed.query)
        if key.lower() in DATABASE_TARGET_OVERRIDE_QUERY_KEYS
    }
    if override_keys:
        raise ValueError(
            "database URL query parameters may override the target: "
            + ", ".join(sorted(override_keys))
        )
    try:
        port = parsed.port or 5432
    except ValueError as exc:
        raise ValueError("selected database URL has an invalid port") from exc
    database = strict_uri_unquote(
        parsed.path.lstrip("/"), label="PostgreSQL URL database"
    )
    if not database or "/" in database:
        raise ValueError("selected database URL has no usable database name")
    if decoded_hostname.startswith("/") or ":" in decoded_hostname:
        fingerprint_host = decoded_hostname
    else:
        fingerprint_host = decoded_hostname.lower().rstrip(".")
    target = f"{fingerprint_host}:{port}/{database}"
    return {
        "algorithm": "sha256",
        "value": hashlib.sha256(target.encode("utf-8")).hexdigest(),
    }


def assert_expected_database_target(db_url, expected_sha256):
    """Fail before DB access when an operational parent bound a different target."""
    if expected_sha256 is None:
        return
    if not isinstance(expected_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_sha256
    ):
        raise SystemExit("expected database target fingerprint must be 64 lowercase hex")
    try:
        actual = database_target_fingerprint_from_url(db_url)["value"]
    except ValueError as exc:
        raise SystemExit(f"refusing ambiguous database target: {exc}") from exc
    if actual != expected_sha256:
        raise SystemExit(
            "refusing database access: selected target does not match "
            "the checkpoint fingerprint"
        )


def find_psql():
    for p in PSQL_CANDIDATES:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    p = shutil.which("psql")
    if p:
        return p
    sys.exit("psql not found. brew install libpq, or set PSQL_BIN.")


PSQL_QUERY_ENV = {
    "application_name": "PGAPPNAME",
    "channel_binding": "PGCHANNELBINDING",
    "client_encoding": "PGCLIENTENCODING",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "gssencmode": "PGGSSENCMODE",
    "gsslib": "PGGSSLIB",
    "gssdelegation": "PGGSSDELEGATION",
    "krbsrvname": "PGKRBSRVNAME",
    "load_balance_hosts": "PGLOADBALANCEHOSTS",
    "max_protocol_version": "PGMAXPROTOCOLVERSION",
    "min_protocol_version": "PGMINPROTOCOLVERSION",
    "options": "PGOPTIONS",
    "passfile": "PGPASSFILE",
    "password": "PGPASSWORD",
    "require_auth": "PGREQUIREAUTH",
    "requirepeer": "PGREQUIREPEER",
    "requiressl": "PGREQUIRESSL",
    "sslcert": "PGSSLCERT",
    "sslcertmode": "PGSSLCERTMODE",
    "sslcompression": "PGSSLCOMPRESSION",
    "sslcrl": "PGSSLCRL",
    "sslcrldir": "PGSSLCRLDIR",
    "sslkey": "PGSSLKEY",
    "ssl_max_protocol_version": "PGSSLMAXPROTOCOLVERSION",
    "ssl_min_protocol_version": "PGSSLMINPROTOCOLVERSION",
    "sslmode": "PGSSLMODE",
    "sslnegotiation": "PGSSLNEGOTIATION",
    "sslrootcert": "PGSSLROOTCERT",
    "sslsni": "PGSSLSNI",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
    "user": "PGUSER",
}

PSQL_URI_ONLY_QUERY_KEYS = {
    "fallback_application_name",
    "keepalives",
    "keepalives_count",
    "keepalives_idle",
    "keepalives_interval",
    "oauth_client_id",
    "oauth_issuer",
    "oauth_scope",
    "replication",
    "sslkeylogfile",
    "tcp_user_timeout",
}

PSQL_UNSUPPORTED_SECRET_QUERY_KEYS = {
    "oauth_client_secret",
    "scram_client_key",
    "scram_server_key",
    "sslpassword",
}

PSQL_TARGET_ENV_KEYS = {
    "PGDATABASE",
    "PGHOST",
    "PGHOSTADDR",
    "PGPORT",
    "PGSERVICE",
    "PGSERVICEFILE",
    *PSQL_QUERY_ENV.values(),
}


def psql_connection_env(db_url):
    """Bind libpq through the child environment, never the process argv.

    Clear inherited target/credential variables, then decompose the already
    fingerprinted URI into libpq's dedicated environment variables. This keeps
    credentials out of ``ps`` output without losing SSL/query settings.
    """
    parsed = urlsplit(db_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("selected database URL has no usable PostgreSQL host")
    env = os.environ.copy()
    for key in PSQL_TARGET_ENV_KEYS:
        env.pop(key, None)
    env["PGHOST"] = strict_uri_unquote(
        parsed.hostname, label="PostgreSQL URL host"
    )
    try:
        env["PGPORT"] = str(parsed.port or 5432)
    except ValueError as exc:
        raise ValueError("selected database URL has an invalid port") from exc
    database = strict_uri_unquote(
        parsed.path.lstrip("/"), label="PostgreSQL URL database"
    )
    if database:
        env["PGDATABASE"] = database
    if parsed.username is not None:
        env["PGUSER"] = strict_uri_unquote(
            parsed.username, label="PostgreSQL URL username"
        )
    if parsed.password is not None:
        env["PGPASSWORD"] = strict_uri_unquote(
            parsed.password, label="PostgreSQL URL password"
        )
    for key, value in parse_postgres_uri_query(parsed.query):
        normalized = key.lower()
        target = PSQL_QUERY_ENV.get(normalized)
        if target is not None:
            env[target] = value
        elif normalized in PSQL_URI_ONLY_QUERY_KEYS:
            continue
        elif normalized in PSQL_UNSUPPORTED_SECRET_QUERY_KEYS:
            raise ValueError(
                f"PostgreSQL URL query parameter cannot be moved out of process argv safely: {key}"
            )
        else:
            raise ValueError(
                f"unsupported PostgreSQL URL query parameter for psql: {key}"
            )
    return env


def psql_connection_args(db_url):
    """Return a credential-free ``--dbname`` URI only when URI-only options exist."""
    parsed = urlsplit(db_url)
    uri_only = [
        (key, value)
        for key, value in parse_postgres_uri_query(parsed.query)
        if key.lower() in PSQL_URI_ONLY_QUERY_KEYS
    ]
    if not uri_only:
        return []
    hostname = strict_uri_unquote(
        parsed.hostname or "", label="PostgreSQL URL host"
    )
    if ":" in hostname:
        rendered_host = f"[{quote(hostname, safe=':')}]"
    else:
        rendered_host = quote(hostname, safe="-._~")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("selected database URL has an invalid port") from exc
    authority = rendered_host + (f":{port}" if port is not None else "")
    database = strict_uri_unquote(
        parsed.path.lstrip("/"), label="PostgreSQL URL database"
    )
    rendered_path = "/" + quote(database, safe="-._~")
    query = "&".join(
        f"{quote(key, safe='-._~')}={quote(value, safe='-._~')}"
        for key, value in uri_only
    )
    safe_uri = urlunsplit((parsed.scheme, authority, rendered_path, query, ""))
    return ["--dbname", safe_uri]


# ---------------------------------------------------------------------------
# Read-back: stream rows out of Postgres as JSON, robustly.
#
# The backfill / classify scripts read existing rows via
#   COPY (SELECT jsonb_build_object(...) ) TO STDOUT
# The DEFAULT (text) COPY format doubles every backslash, which corrupts any
# JSON string carrying an escape (HTML with \" , a Windows path, a regex). The
# round-trip then fails json.loads and a naive `except: continue` would SILENTLY
# DROP the row -- e.g. 100% of Marcus & Millichap rows (their raw_data embeds
# escaped-quote HTML) vanished with no error. CSV COPY format does not
# backslash-escape, so it round-trips JSON intact; csv.reader unquotes it.
# A decode failure here ABORTS (never a silent skip) so a malformed row can
# never disappear unnoticed.
# ---------------------------------------------------------------------------


def _raise_csv_field_limit():
    """csv.reader rejects a field over 131072 bytes by default; a large raw_data
    JSON object easily exceeds that. Raise the limit as high as the platform's C
    long allows (halving down from sys.maxsize on a narrow-long platform)."""
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit = int(limit // 10)


def _csv_cells(stdout_text):
    """Yield each CSV row's first (JSON) cell from COPY-CSV output, stripped and
    non-empty. Raises the csv field-size limit first so a large raw_data object
    is not rejected."""
    _raise_csv_field_limit()
    for row in csv.reader(io.StringIO(stdout_text)):
        if not row:
            continue
        cell = row[0].strip()
        if cell:
            yield cell


def parse_copy_csv_json(stdout_text, *, label="read"):
    """STRICT decoder for `COPY (...) TO STDOUT WITH (FORMAT csv)` output where
    each CSV row's first field holds one JSON object. Yields decoded objects and
    RAISES ValueError on the first undecodable row (never silently drops it).

    Pure (no DB): the unit tests feed synthetic CSV (including a backslash-bearing
    JSON value and an oversized field) to lock the round-trip.
    """
    for cell in _csv_cells(stdout_text):
        try:
            yield json.loads(cell)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"[{label}] undecodable COPY row (first 120 chars: {cell[:120]!r}): {exc}"
            ) from exc


def iter_copy_json_rows(psql, db_url, inner_select, *, label="read"):
    """Run `COPY (<inner_select>) TO STDOUT WITH (FORMAT csv)` and yield decoded
    dicts. `inner_select` must return exactly ONE column per row holding a JSON
    object (jsonb or its ::text). CSV format (see module note above) round-trips
    JSON containing backslash escapes intact.

    A row that fails to decode is SKIPPED but COUNTED and reported LOUDLY at the
    end (never silently): a non-silent skip keeps one pathological row from
    blocking a large additive backfill while still surfacing any loss for
    investigation before an --apply. A psql error still aborts.
    """
    sql = f"COPY ({inner_select}) TO STDOUT WITH (FORMAT csv)"
    proc = subprocess.run(
        [
            psql,
            *psql_connection_args(db_url),
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ],
        env=psql_connection_env(db_url),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        sys.exit(f"[{label}] psql read exited {proc.returncode}")
    bad = 0
    seen = 0
    for cell in _csv_cells(proc.stdout):
        seen += 1
        try:
            yield json.loads(cell)
        except json.JSONDecodeError as exc:
            bad += 1
            if bad <= 5:
                sys.stderr.write(
                    f"[{label}] WARN skipped undecodable row "
                    f"(first 120 chars: {cell[:120]!r}): {exc}\n"
                )
    if bad:
        sys.stderr.write(
            f"[{label}] WARNING: {bad}/{seen} row(s) FAILED JSON decode and were "
            f"SKIPPED (not silently). Investigate before --apply.\n"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inputs", action="append", required=True,
                    help="collect.ts output JSON (repeatable)")
    ap.add_argument("--env-file", default=None, help="env file holding POSTGRES_URL*")
    ap.add_argument(
        "--expected-db-target-sha256",
        default=None,
        help=argparse.SUPPRESS,
    )
    ap.add_argument("--dry-run", action="store_true", help="build SQL, print stats, don't connect")
    ap.add_argument(
        "--require-strict-freshness",
        action="store_true",
        help=(
            "require every input artifact to carry and satisfy the strict "
            "freshness contract; checkpoint refresh passes this for strict sources"
        ),
    )
    ap.add_argument("--mark-missing", action="store_true",
                    help="soft-delete listings not present in this run (full runs only); "
                         "applies only to brokerages whose every source pass ran error-free "
                         "and staged >= --mark-missing-floor rows")
    ap.add_argument("--mark-missing-floor", type=int, default=100)
    ap.add_argument("--activate-status", action="store_true",
                    help="enable Phase-2 source-derived status activation "
                         "(default OFF; also via CRE_ACTIVATE_STATUS=1). Only use once "
                         "the EQUIRE consumer board-gate is deployed, or non-active rows "
                         "silently drop off the 'active'-only board")
    ap.add_argument("--keep-artifacts", default=None, help="dir to keep the generated SQL in")
    args = ap.parse_args()

    merged = {}          # (slug, external_id) -> row
    inventory_only_merged = {}  # (slug, external_id) -> source-index row
    inventory_only_scopes_merged = {}  # source_key -> proven full scope
    skipped_no_url = 0
    per_source_counts = {}
    source_entries = []  # all sources[] entries across files
    started_at = None

    for path in args.inputs:
        with open(path) as f:
            data = json.load(f)
        if data.get("artifactKind") == RETIRED_OM_PARSE_ARTIFACT_KIND:
            sys.exit(
                "refusing retired om_parse dry-run artifact: GetCREdata is the "
                "sole production OM extraction writer"
            )
        run_meta = data.get("runMeta") or {}
        try:
            validate_strict_artifact_freshness(
                data,
                require_strict_freshness=args.require_strict_freshness,
            )
        except ValueError as exc:
            sys.exit(f"refusing strict freshness artifact: {exc}")
        mode = run_meta.get("mode")
        if not args.dry_run and mode not in {"full", "enrich"}:
            sys.exit(
                f"refusing live ingest for artifact mode {mode!r}: "
                "expected runMeta.mode to be 'full' or 'enrich'"
            )
        started_at = started_at or run_meta.get("startedAt")
        scraped_at = run_meta.get("finishedAt") or datetime.now(timezone.utc).isoformat()
        brokers_by_idx = {i: b for i, b in enumerate(data.get("brokers") or [])}
        source_entries.extend(data.get("sources") or [])
        artifact_scopes = inventory_only_full_scopes(data)
        artifact_scope_sources = {scope["source_key"] for scope in artifact_scopes}
        artifact_inventory_sources = {
            listing.get("sourceKey")
            for listing in (data.get("listings") or [])
            if isinstance(listing, dict) and listing.get("inventoryOnly") is not None
        }
        artifact_unscoped_inventory = (
            artifact_inventory_sources - artifact_scope_sources
        )
        if artifact_unscoped_inventory:
            sys.exit(
                "refusing inventory-only rows without a strict complete full "
                "scope in the same artifact: "
                f"{sorted(artifact_unscoped_inventory)}"
            )
        for scope in artifact_scopes:
            if scope["source_key"] in inventory_only_scopes_merged:
                sys.exit(
                    "refusing duplicate complete inventory-only scopes for "
                    f"{scope['source_key']!r}; provide exactly one current "
                    "full snapshot for that source"
                )
            inventory_only_scopes_merged[scope["source_key"]] = scope
        for listing in data.get("listings") or []:
            inventory_row = to_inventory_only_row(listing, scraped_at)
            if inventory_row is not None:
                inventory_key = (
                    inventory_row["slug"],
                    inventory_row["external_id"],
                )
                if (
                    listing.get("sourceKey") == "colliers"
                    and inventory_key in inventory_only_merged
                ):
                    sys.exit(
                        "refusing duplicate Colliers provisional inventory "
                        f"identity {inventory_key[1]!r}"
                    )
                inventory_only_merged[inventory_key] = inventory_row
                continue
            row = to_row(listing, brokers_by_idx, scraped_at)
            if row is None:
                skipped_no_url += 1
                continue
            key = (row["slug"], row["external_id"])
            if key in merged:
                try:
                    validate_duplicate_identity_before_merge(merged[key], row)
                except ValueError as exc:
                    sys.exit(f"refusing duplicate canonical identity: {exc}")
                merged[key] = merge_rows(merged[key], row)
            else:
                merged[key] = row
            sk = listing.get("sourceKey")
            per_source_counts[sk] = per_source_counts.get(sk, 0) + 1

    rows = list(merged.values())
    inventory_only_rows = list(inventory_only_merged.values())
    inventory_only_scopes = list(inventory_only_scopes_merged.values())
    unscoped_inventory_sources = {
        row["source_key"] for row in inventory_only_rows
    } - set(inventory_only_scopes_merged)
    if unscoped_inventory_sources:
        sys.exit(
            "refusing inventory-only ingest without a strict complete full "
            f"enumeration scope: {sorted(unscoped_inventory_sources)}"
        )
    if not rows and not inventory_only_rows and not inventory_only_scopes:
        sys.exit("nothing to ingest (0 usable listings or inventory-only rows)")
    for r in rows:
        r.pop("_modes", None)
    started_at = started_at or datetime.now(timezone.utc).isoformat()

    # Phase-2 status activation is opt-in (default OFF). With it off, suppress
    # source-derived statuses so this ingest refreshes listing data without
    # flipping board state (no non-active row reaches the 'active'-only board).
    activate_status = _status_activation_enabled(args.activate_status)
    suppressed = apply_status_activation_gate(rows, activate_status)
    if activate_status:
        print("status activation: ENABLED (Phase-2 source statuses will be written)", file=sys.stderr)
    else:
        print(
            f"status activation: OFF (default) -- suppressed {suppressed} source status "
            "signal(s); rows stay 'active'. Enable with --activate-status / CRE_ACTIVATE_STATUS=1.",
            file=sys.stderr,
        )

    # Per-brokerage job stats + mark-missing eligibility.
    # (M1) Build a per-source-key discovered count so the folded-coverage check
    # can require a nonzero contribution from EVERY folded key, not just presence.
    # A folded source (e.g. colliers-main) that returned zero rows without an error
    # would otherwise satisfy the key-presence check while contributing nothing,
    # which could cause the whole brokerage's rows to be soft-deleted.
    slug_stats = {}
    source_keys_by_slug_seen = {}
    discovered_by_source_key = {}
    for e in source_entries:
        source_key = e.get("sourceKey")
        mapping = SOURCE_TO_BROKERAGE.get(source_key)
        if not mapping:
            continue
        slug = mapping[0]
        source_keys_by_slug_seen.setdefault(slug, set()).add(source_key)
        st = slug_stats.setdefault(slug, {"discovered": 0, "errors": 0, "notes": []})
        collected = e.get("listingsCollected") or 0
        st["discovered"] += collected
        discovered_by_source_key[source_key] = discovered_by_source_key.get(source_key, 0) + collected
        if e.get("error"):
            st["errors"] += 1
            st["notes"].append(f"{e.get('sourceKey')}/{e.get('transaction')}: {e['error'][:160]}")
        if e.get("truncated"):
            st["errors"] += 1
            st["notes"].append(
                f"{e.get('sourceKey')}/{e.get('transaction')}: source pass truncated"
            )
    slug_saved = {}
    for r in rows:
        slug_saved[r["slug"]] = slug_saved.get(r["slug"], 0) + 1
    mark_missing_slugs = set()
    if args.mark_missing:
        for slug, st in slug_stats.items():
            known_keys = SOURCE_KEYS_BY_SLUG.get(slug, {slug})
            seen_keys = source_keys_by_slug_seen.get(slug, set())
            # (M1) Count-aware folded coverage: every folded key must have a nonzero
            # discovered count this run. Singletons (len == 1) skip the count check
            # because the --mark-missing-floor staged-count check already covers them.
            has_complete_folded_coverage = (
                len(known_keys) == 1
                or (known_keys <= seen_keys
                    and all(discovered_by_source_key.get(k, 0) > 0 for k in known_keys))
            )
            if (
                st["errors"] == 0
                and slug_saved.get(slug, 0) >= args.mark_missing_floor
                and has_complete_folded_coverage
            ):
                mark_missing_slugs.add(slug)
            elif len(known_keys) > 1 and not has_complete_folded_coverage:
                st["notes"].append(
                    "mark-missing skipped: folded source coverage incomplete "
                    f"(saw {sorted(seen_keys)}, need {sorted(known_keys)}; "
                    f"zero-count keys: {sorted(k for k in known_keys if discovered_by_source_key.get(k, 0) == 0)})"
                )
        ineligible = set(slug_stats) - mark_missing_slugs
        if ineligible:
            print(
                "mark-missing skipped for (errors, below floor, or incomplete folded source coverage): "
                f"{sorted(ineligible)}",
                file=sys.stderr,
            )

    job_meta = [
        {
            "slug": slug,
            "discovered": st["discovered"],
            "saved": slug_saved.get(slug, 0),
            "errors": st["errors"],
            "notes": "; ".join(st["notes"]) or None,
        }
        for slug, st in sorted(slug_stats.items())
        if (
            slug_saved.get(slug, 0) > 0
            or st["discovered"] > 0
            or st["errors"] > 0
            or slug in {scope["slug"] for scope in inventory_only_scopes}
        )
    ]

    sql = build_sql(
        rows,
        job_meta,
        started_at,
        mark_missing_slugs,
        history_guard=not args.dry_run,
        inventory_only_rows=inventory_only_rows,
        inventory_only_scopes=inventory_only_scopes,
    )

    print(
        f"staged listings: {len(rows)}; inventory-only index rows: "
        f"{len(inventory_only_rows)} (skipped, unusable: {skipped_no_url})",
        file=sys.stderr,
    )
    for sk in sorted(per_source_counts):
        print(f"  {sk}: {per_source_counts[sk]}", file=sys.stderr)
    if mark_missing_slugs:
        print(f"mark-missing active for: {sorted(mark_missing_slugs)}", file=sys.stderr)

    out_dir = args.keep_artifacts or tempfile.mkdtemp(prefix="cre_ingest_")
    os.makedirs(out_dir, exist_ok=True)
    sql_path = os.path.join(out_dir, "ingest.sql")
    with open(sql_path, "w") as f:
        f.write(sql)
    print(f"sql: {sql_path} ({os.path.getsize(sql_path) / 1e6:.1f} MB)", file=sys.stderr)

    if args.dry_run:
        print("dry run: not connecting", file=sys.stderr)
        return

    db_url, env_path = load_db_url(args.env_file)
    assert_expected_database_target(db_url, args.expected_db_target_sha256)
    print(f"credentials: {env_path}", file=sys.stderr)
    psql = find_psql()
    proc = subprocess.run(
        [
            psql,
            *psql_connection_args(db_url),
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            sql_path,
        ],
        env=psql_connection_env(db_url),
        stdout=sys.stdout, stderr=sys.stderr,
    )
    if not args.keep_artifacts:
        shutil.rmtree(out_dir, ignore_errors=True)
    if proc.returncode != 0:
        sys.exit(f"psql exited {proc.returncode}")
    print("ingest complete", file=sys.stderr)


if __name__ == "__main__":
    main()
