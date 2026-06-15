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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

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
}

SOURCE_KEYS_BY_SLUG = {}
for _source_key, (_slug, _prefix) in SOURCE_TO_BROKERAGE.items():
    SOURCE_KEYS_BY_SLUG.setdefault(_slug, set()).add(_source_key)

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


_MONEY_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)")
_SALE_PSF_TEXT_RE = re.compile(
    r"(?:/|\bper\s+)\s*(?:s\.?f\.?|sq\.?\s*ft|square\s*feet)|\bpsf\b",
    re.I,
)
# "$10 - 16", "$1.50 to 2.25": the upper bound often has no $ of its own
_MONEY_RANGE_RE = re.compile(
    r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:-|–|to)\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
)


def parse_lease_rates(text):
    """Conservative $/SF/year (min, max) from free-text lease rates.

    Only trusts values that are explicitly per square foot; monthly per-SF
    rates are annualized. Anything else (gross monthly rent, 'Negotiable')
    stays in raw_data only.
    """
    if not text or not isinstance(text, str):
        return None, None
    low = text.lower()
    if not re.search(r"(/|per\s|\s)s\.?f|psf|square\s*f", low):
        return None, None
    m = _MONEY_RANGE_RE.search(text)
    if m:
        nums = [float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))]
        # Buildout sometimes formats a suite-size range as a money range, e.g.
        # "$2.50 - 250 SF/month". Treat large upper bounds as unsafe rather
        # than promoting suite size into an annual PSF lease rate.
        if max(nums) > 100 and min(nums) < 100:
            return None, None
    else:
        nums = [float(x.replace(",", "")) for x in _MONEY_RE.findall(text)]
    nums = [n for n in nums if 0 < n <= 500]
    if not nums:
        return None, None
    monthly = bool(re.search(r"/\s*mo|month", low))
    annual = bool(re.search(r"/\s*yr|year|annual|/\s*a\b", low))
    if monthly and not annual:
        nums = [n * 12 for n in nums]
    elif not annual and min(nums) > 100:
        return None, None  # per-SF but implausible as annual; don't guess
    nums = [n for n in nums if 0 < n <= 500]
    if not nums:
        return None, None
    lo, hi = min(nums), max(nums)
    return round(lo, 2), (round(hi, 2) if hi > lo else None)


def parse_money(text):
    if not text or not isinstance(text, str):
        return None
    m = _MONEY_RE.search(text)
    return float(m.group(1).replace(",", "")) if m else None


def is_sale_psf_text(text):
    return bool(text and isinstance(text, str) and _SALE_PSF_TEXT_RE.search(text))


_SF_RE = re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:sf\b|sq\.?\s*ft|square\s*feet)", re.I)
_ACRE_RE = re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*acres?\b", re.I)


def parse_size_text(text):
    """(size_sf, lot_size_sf) parsed from a free-text size summary."""
    if not text or not isinstance(text, str):
        return None, None
    size_sf = None
    lot_sf = None
    m = _SF_RE.search(text)
    if m:
        size_sf = float(m.group(1).replace(",", ""))
        if size_sf > 1_000_000_000:
            size_sf = None
    m = _ACRE_RE.search(text)
    if m:
        lot_sf = float(m.group(1).replace(",", "")) * SQFT_PER_ACRE
    return size_sf, lot_sf


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


# ---------------------------------------------------------------------------
# Listing transformation
# ---------------------------------------------------------------------------


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
    "colliers": ["status"],
    "cbre-dealflow": ["status", "cbreDealflowCard.status", "cbreDealflowDetail.status"],
    "cushman-wakefield": ["listingStatus", "rawCushmanApi.listing_status"],
    "colliers-main": ["status", "colliersMain.propertyStatus"],
    # Freshness-only flags, NOT terminal -> explicitly map to no signal.
    "marcus-millichap": [],
    # Disappearance-only tier (no native status field; lifecycle = vanishing).
    "cbre": [],
    "jll": [],
    "newmark": [],
    "avison-young": [],
    "savills": [],
    "transwestern": [],
}

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
    paths = STATUS_SOURCE_PATHS.get(source_key, [])

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


def to_row(listing, brokers_by_idx, scraped_at):
    """Map one collector listing to a staging row dict, or None to skip."""
    source_key = listing.get("sourceKey")
    mapping = SOURCE_TO_BROKERAGE.get(source_key)
    if not mapping:
        return None
    slug, prefix = mapping

    url = listing.get("url")
    if not url or not isinstance(url, str) or not url.startswith("http"):
        return None  # source_url is NOT NULL; un-linked rows aren't actionable

    raw_id = listing.get("id")
    # Buildout sources (svn, lee-associates) list a dual-mode property twice
    # with distinct inventory ids; the URL propertyId base ("1614726-sale" /
    # "1614726-lease") is the stable per-property key, so the pair merges.
    buildout_pid = None
    if source_key in ("svn", "lee-associates"):
        m = re.search(r"[?&]propertyId=([^&#]+)", url)
        if m:
            buildout_pid = re.sub(r"-(sale|lease)$", "", m.group(1))
    if buildout_pid:
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
    if is_sale_psf_text(sale_price_text):
        price_per_sf = price_per_sf or num_or_none(parse_money(sale_price_text), lo=0, hi=10000)
        sale_price = None
    if sale_price and size_sf and size_sf > 100:
        price_per_sf = round(sale_price / size_sf, 2)

    lease_min, lease_max = parse_lease_rates(listing.get("leaseRateText"))

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
                documents.append({"title": d.get("name"), "url": doc_url, "docType": "brochure"})

    images = []
    for i, p in enumerate(listing.get("photos") or []):
        if isinstance(p, str) and p.startswith("http"):
            images.append({"url": p, "isPrimary": i == 0, "order": i})

    title = listing.get("name") or listing.get("headline") or listing.get("street")
    desc = listing.get("description")

    return {
        "slug": slug,
        "external_id": external_id,
        "source_url": url,
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
        "lease_rate_min": lease_min,
        "lease_rate_max": lease_max,
        "description": desc[:20000] if isinstance(desc, str) else None,
        "updated_date": iso_date_or_none(listing.get("lastUpdated")),
        "status": norm_status(listing),
        "source_lastmod": group_source_lastmod([listing]),
        "canonical_key": _canonical_key(listing),
        "scraped_at": scraped_at,
        "raw_data": listing,
        "contacts": contacts,
        "documents": documents,
        "images": images,
        "_modes": {listing.get("transactionMode")},
    }


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
        "cap_rate", "lease_rate_min", "lease_rate_max", "description", "updated_date",
        "source_lastmod", "canonical_key",
    ):
        if a[k] is None and b[k] is not None:
            a[k] = b[k]
    for k in ("contacts", "documents", "images"):
        if not a[k] and b[k]:
            a[k] = b[k]
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
    "cap_rate", "lease_rate_min", "lease_rate_max", "description",
    "updated_date", "scraped_at", "raw_data", "contacts", "documents", "images",
    "status", "source_lastmod", "canonical_key",
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


def build_sql(rows, job_meta, started_at, mark_missing_slugs, history_guard=True):
    lines = []
    w = lines.append
    w("\\set ON_ERROR_STOP on")
    w("BEGIN;")
    w("SET LOCAL statement_timeout = '600s';")
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
    sale_price_per_sf numeric, cap_rate numeric, lease_rate_min numeric,
    lease_rate_max numeric, description text, updated_date timestamptz,
    scraped_at timestamptz, raw_data jsonb, contacts jsonb, documents jsonb,
    images jsonb, status text, source_lastmod timestamptz, canonical_key text
) ON COMMIT DROP;""")
    w(f"COPY _stage ({', '.join(STAGE_COLS)}) FROM stdin;")
    for r in rows:
        w("\t".join(copy_field(r[c]) for c in STAGE_COLS))
    w("\\.")

    w("""
CREATE TEMP TABLE _jobmeta (
    slug text, discovered integer, saved integer, errors integer, notes text
) ON COMMIT DROP;""")
    w("COPY _jobmeta (slug, discovered, saved, errors, notes) FROM stdin;")
    for jm in job_meta:
        w("\t".join(copy_field(v) for v in (jm["slug"], jm["discovered"], jm["saved"], jm["errors"], jm["notes"])))
    w("\\.")

    w("""
-- Fail loudly if a sourceKey maps to a brokerage slug that is not seeded.
DO $$
DECLARE missing text;
BEGIN
    SELECT string_agg(DISTINCT s.slug, ', ') INTO missing
    FROM _stage s LEFT JOIN credeals.cre_brokerages b ON b.slug = s.slug
    WHERE b.id IS NULL;
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'unseeded brokerage slug(s): % (run sql/001_cre_brokerages.sql)', missing;
    END IF;
END $$;

CREATE TEMP TABLE _src ON COMMIT DROP AS
SELECT b.id AS brokerage_id, s.*
FROM _stage s JOIN credeals.cre_brokerages b ON b.slug = s.slug;

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
        property_type, title, address, city, state, zip, lat, lng, size_sf,
        lot_size_sf, year_built, sale_price_usd, sale_price_per_sf, cap_rate,
        lease_rate_min, lease_rate_max, description, updated_date, scraped_at,
        raw_data, source_lastmod, canonical_key
    )
    SELECT brokerage_id, external_id, source_url, COALESCE(status, 'active'), transaction_type,
           property_type, title, address, city, state, zip, lat, lng, size_sf,
           lot_size_sf, year_built, sale_price_usd, sale_price_per_sf, cap_rate,
           lease_rate_min, lease_rate_max, description, updated_date, scraped_at,
           raw_data, source_lastmod, canonical_key
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
        transaction_type  = EXCLUDED.transaction_type,
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
        lot_size_sf       = COALESCE(EXCLUDED.lot_size_sf, t.lot_size_sf),
        year_built        = COALESCE(EXCLUDED.year_built, t.year_built),
        -- (L1) COALESCE-keep: a transient parse miss (regex miss, "Call for offer")
        -- keeps the prior good numeric value rather than overwriting with NULL.
        -- A real new numeric value still overwrites because COALESCE picks the first
        -- non-NULL, which is EXCLUDED when present. Mirrors cap_rate, property_type,
        -- and other neighbors that already use COALESCE-keep.
        sale_price_usd    = COALESCE(EXCLUDED.sale_price_usd, t.sale_price_usd),
        sale_price_per_sf = COALESCE(EXCLUDED.sale_price_per_sf, t.sale_price_per_sf),
        cap_rate          = COALESCE(EXCLUDED.cap_rate, t.cap_rate),
        lease_rate_min    = COALESCE(EXCLUDED.lease_rate_min, t.lease_rate_min),
        lease_rate_max    = COALESCE(EXCLUDED.lease_rate_max, t.lease_rate_max),
        description       = COALESCE(EXCLUDED.description, t.description),
        updated_date      = COALESCE(EXCLUDED.updated_date, t.updated_date),
        scraped_at        = EXCLUDED.scraped_at,
        raw_data          = EXCLUDED.raw_data,
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

-- Children: refresh wholesale only when the latest source row did not hit a
-- detail-page error. This protects previously good documents/images/contacts
-- from transient detail-scrape failures while still refreshing normal rows.
CREATE TEMP TABLE _child_refresh ON COMMIT DROP AS
SELECT DISTINCT u.id
FROM _up u
JOIN _src s USING (brokerage_id, external_id)
WHERE NOT jsonb_path_exists(s.raw_data, '$.**.detailError');

DELETE FROM credeals.cre_listing_contacts  WHERE listing_id IN (SELECT id FROM _child_refresh);
DELETE FROM credeals.cre_listing_documents WHERE listing_id IN (SELECT id FROM _child_refresh);
DELETE FROM credeals.cre_listing_images    WHERE listing_id IN (SELECT id FROM _child_refresh);

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

INSERT INTO credeals.cre_listing_documents (listing_id, doc_type, title, url)
SELECT u.id,
       CASE WHEN x->>'docType' IN ('brochure','om','flyer','floor_plan') THEN x->>'docType' ELSE 'other' END,
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
END $$;""")
        else:
            w(f"""
-- (M2) Archive final contacts of retired listings (unguarded for dry-run).
{_contacts_insert}

-- (M2) Archive final documents of retired listings (unguarded for dry-run).
{_documents_insert}""")

    w(f"""
INSERT INTO credeals.cre_scrape_jobs
    (brokerage_id, status, started_at, completed_at,
     listings_discovered, listings_scraped, listings_saved, errors_count, notes)
SELECT b.id,
       CASE WHEN jm.errors > 0 THEN 'partial' ELSE 'completed' END,
       {sql_lit(started_at)}::timestamptz, now(),
       jm.discovered, jm.saved, jm.saved, jm.errors, jm.notes
FROM _jobmeta jm JOIN credeals.cre_brokerages b ON b.slug = jm.slug;

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
            return url, path
    sys.exit(
        "No POSTGRES_URL_NON_POOLING/POSTGRES_URL found. Set CRE_ENV_FILE or pass "
        "--env-file pointing at an env file that has one (values are never printed)."
    )


def find_psql():
    for p in PSQL_CANDIDATES:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    p = shutil.which("psql")
    if p:
        return p
    sys.exit("psql not found. brew install libpq, or set PSQL_BIN.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inputs", action="append", required=True,
                    help="collect.ts output JSON (repeatable)")
    ap.add_argument("--env-file", default=None, help="env file holding POSTGRES_URL*")
    ap.add_argument("--dry-run", action="store_true", help="build SQL, print stats, don't connect")
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
    skipped_no_url = 0
    per_source_counts = {}
    source_entries = []  # all sources[] entries across files
    started_at = None

    for path in args.inputs:
        with open(path) as f:
            data = json.load(f)
        run_meta = data.get("runMeta") or {}
        started_at = started_at or run_meta.get("startedAt")
        scraped_at = run_meta.get("finishedAt") or datetime.now(timezone.utc).isoformat()
        brokers_by_idx = {i: b for i, b in enumerate(data.get("brokers") or [])}
        source_entries.extend(data.get("sources") or [])
        for listing in data.get("listings") or []:
            row = to_row(listing, brokers_by_idx, scraped_at)
            if row is None:
                skipped_no_url += 1
                continue
            key = (row["slug"], row["external_id"])
            if key in merged:
                merged[key] = merge_rows(merged[key], row)
            else:
                merged[key] = row
            sk = listing.get("sourceKey")
            per_source_counts[sk] = per_source_counts.get(sk, 0) + 1

    rows = list(merged.values())
    if not rows:
        sys.exit("nothing to ingest (0 usable listings)")
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
        if slug_saved.get(slug, 0) > 0 or st["discovered"] > 0 or st["errors"] > 0
    ]

    sql = build_sql(rows, job_meta, started_at, mark_missing_slugs,
                    history_guard=not args.dry_run)

    print(f"staged listings: {len(rows)} (skipped, no URL: {skipped_no_url})", file=sys.stderr)
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
    print(f"credentials: {env_path}", file=sys.stderr)
    psql = find_psql()
    proc = subprocess.run(
        [psql, db_url, "-q", "-v", "ON_ERROR_STOP=1", "-f", sql_path],
        stdout=sys.stdout, stderr=sys.stderr,
    )
    if not args.keep_artifacts:
        shutil.rmtree(out_dir, ignore_errors=True)
    if proc.returncode != 0:
        sys.exit(f"psql exited {proc.returncode}")
    print("ingest complete", file=sys.stderr)


if __name__ == "__main__":
    main()
