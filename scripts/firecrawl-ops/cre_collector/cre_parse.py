#!/usr/bin/env python3
"""cre_parse.py: the Python mirror of lib/parse.ts (Phase-2 data-lift, Section C).

Single source of truth in Python for the shared CRE text parsers. Imported by
cre_ingest.py (the production upsert path) and, in Workflow 2, by the raw_data
backfill script. The TS side (lib/parse.ts) and this module are VERIFIABLY
IDENTICAL via the shared golden test-vector table
(tests/fixtures/golden_parse_vectors.json, contract Section C.5).

Public surface (contract C.3):
    parse_lease_rate(text)  -> (min, max, type)   $/SF/yr annualized + basis token
    parse_money(text)       -> float | None
    acres_to_sf(text)       -> float | None
    parse_amount_ignoring_currency_label(text) -> float | None
    parse_percent_to_fraction(text)            -> float | None  in (0, 1]
    norm_building_class(text) -> 'A'|'B'|'C'|'D'|None
    parse_size_text(text)   -> (size_sf, lot_sf)
    is_per_sf_text(text)    -> bool
    classify_doc(url, title) -> doc_type token (mirror of lib/harvest.classifyDoc)

Python stdlib only. Pure functions; no I/O, no network, import-safe.

Deviation note (golden vector row 14): the contract states acresToSf multiplies
by 43560 in three places (Section A SQFT_PER_ACRE, C.1, C.3) and rows 15/16
(`0.5 ac` -> 21780, `2.0 Acres` -> 87120) both confirm 43560. The single cell
`3.83 acres -> 166774.8` is arithmetically inconsistent with that factor
(3.83 * 43560 = 166834.8). This module implements the stated factor (43560);
the fixture owner should reconcile that one cell. No other deviation.
"""

import re

# ---------------------------------------------------------------------------
# Shared constants / regexes
# ---------------------------------------------------------------------------

SQFT_PER_ACRE = 43560

# First "$N[,N][.N]" -> the captured numeric (commas stripped by the caller).
_MONEY_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)")

# A bare numeric token (with optional grouping commas / decimals), no $ required.
_NUM_RE = re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)")

# Per-SF price text guard. Verbatim mirror of lib/util.isPerSfPriceText (and the
# existing cre_ingest._SALE_PSF_TEXT_RE) so the Lee salePriceUsd per-SF conflation
# guard stays byte-identical across TS and Python.
_PER_SF_TEXT_RE = re.compile(
    r"(?:/|\bper\s+)\s*(?:s\.?f\.?|sq\.?\s*ft|square\s*feet)|\bpsf\b",
    re.I,
)

# Size-in-SF and area-in-acres extractors (mirror cre_ingest._SF_RE / _ACRE_RE).
_SF_RE = re.compile(
    r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:sf\b|sq\.?\s*ft|square\s*feet)", re.I
)
_ACRE_RE = re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*ac(?:res?)?\b", re.I)

# Percent token and leading-currency-label strip.
_PCT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%")
_CURRENCY_PREFIX_RE = re.compile(r"^\s*(?:POUND|GBP|USD|EUR|£|€|\$)\s*", re.I)



def _to_num(s):
    """'8,585,673.00' -> 8585673.0. Caller guarantees a numeric-shaped match."""
    return float(s.replace(",", ""))


# ---------------------------------------------------------------------------
# Lease rate
# ---------------------------------------------------------------------------



def parse_lease_rate(text):
    """USD/SF/year only when currency, denominator and period are explicit."""
    from cre_rent_evidence import rent_evidence

    evidence = rent_evidence(text)
    return evidence["annual_psf_min"], evidence["annual_psf_max"], evidence["lease_basis"]

# ---------------------------------------------------------------------------
# Money / acres / size
# ---------------------------------------------------------------------------


def parse_money(text):
    """First "$N[,N][.N]" -> float, commas stripped, currency words ignored."""
    if not isinstance(text, str):
        return None
    m = _MONEY_RE.search(text)
    return _to_num(m.group(1)) if m else None


def acres_to_sf(text):
    """"3.83 acres" / "0.5 ac" -> SF (x 43560), or None."""
    if not isinstance(text, str):
        return None
    m = _ACRE_RE.search(text)
    return _to_num(m.group(1)) * SQFT_PER_ACRE if m else None


def parse_size_text(text):
    """(size_sf, lot_size_sf) from a free-text size summary. An "Acres" token
    routes to lot_size_sf (x 43560); an "SF" token to size_sf."""
    if not isinstance(text, str):
        return None, None
    size_sf = None
    lot_sf = None
    m = _SF_RE.search(text)
    if m:
        size_sf = _to_num(m.group(1))
        if size_sf > 1_000_000_000:
            size_sf = None
    m = _ACRE_RE.search(text)
    if m:
        lot_sf = _to_num(m.group(1)) * SQFT_PER_ACRE
    return size_sf, lot_sf


def parse_amount_ignoring_currency_label(text):
    """Strip a leading currency word/symbol (POUND|GBP|USD|EUR|$|£|€) then take
    the first numeric; the value is treated as USD regardless of the stripped
    label. Used only where the gap doc proves the label is wrong (NAI 'POUND ').
    """
    if not isinstance(text, str):
        return None
    stripped = _CURRENCY_PREFIX_RE.sub("", text, count=1)
    m = _NUM_RE.search(stripped)
    return _to_num(m.group(1)) if m else None


def is_per_sf_text(text):
    """True when the text is a per-SF price ("$6.00/SF", "psf", "per square
    foot"), so an absolute sale price must NOT be read from it. Mirrors
    lib/util.isPerSfPriceText / the existing cre_ingest.is_sale_psf_text guard."""
    return bool(text and isinstance(text, str) and _PER_SF_TEXT_RE.search(text))


# ---------------------------------------------------------------------------
# Percent / building class
# ---------------------------------------------------------------------------


def parse_percent_to_fraction(text):
    """Percent string -> fraction in (0, 1]. "87.5%" -> 0.875, "0.875" -> 0.875.

    Accepts an explicit percent token first; otherwise a bare number is read as a
    fraction when <= 1, else as a percent when in (1, 100]. Returns None for
    anything outside (0, 1] after conversion. Mirrors norm_occupancy_rate's
    percent-or-fraction tolerance for occupancy-style values.
    """
    if not isinstance(text, str):
        return None
    m = _PCT_RE.search(text)
    if m:
        v = float(m.group(1))
        return round(v / 100.0, 6) if 0 < v <= 100 else None
    m = re.search(r"([0-9]*\.?[0-9]+)", text)
    if not m:
        return None
    v = float(m.group(1))
    if 0 < v <= 1:
        return round(v, 6)
    if 1 < v <= 100:
        return round(v / 100.0, 6)
    return None


def norm_building_class(text):
    """Any cased "Class A" / a 1-2 token bare "A" -> 'A'|'B'|'C'|'D', else None.

    Order: an explicit "Class X" token wins; otherwise a bare trailing A-D is
    accepted ONLY when the whole input is <= 2 tokens (avoids matching a stray
    letter in prose). A subtype string with no class token (e.g. "office.medical")
    returns None: class is never inferred from a property subtype.
    """
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    m = re.search(r"\bClass\s+([A-D])\b", s, re.I)
    if m:
        return m.group(1).upper()
    if len(s.split()) <= 2:
        m = re.search(r"\b([A-D])\b", s)
        if m:
            return m.group(1).upper()
    return None


# ---------------------------------------------------------------------------
# Document classification (mirror of lib/harvest.classifyDoc, contract Section D)
# ---------------------------------------------------------------------------

# Allowed doc_type tokens (widened set from sql/011). 'other' is the safe default.
DOC_TYPES = ("om", "brochure", "flyer", "floor_plan", "financials", "rent_roll", "other")

# Recognized document file extension (anchored on a real extension token).
_DOC_EXT_RE = re.compile(r"\.(?:pdf|docx?|pptx?|xlsx?|csv)(?:[?#]|$)", re.I)

# Ordered keyword -> doc_type, most specific first (verbatim from classifyDoc).
_DOC_KEYWORD_RULES = [
    (re.compile(r"rent[-_ ]?roll", re.I), "rent_roll"),
    (re.compile(r"financ|pro[-_ ]?forma|proforma|\bt-?12\b", re.I), "financials"),
    (re.compile(r"floor[-_ ]?plan|site[-_ ]?plan|floorplan|siteplan", re.I), "floor_plan"),
    (
        re.compile(
            r"offering|memorandum|(?:^|[/_-])om(?:[/_.-]|$)|teaser|dataroom|data[-_ ]room|deal[-_ ]room",
            re.I,
        ),
        "om",
    ),
    (re.compile(r"flyer", re.I), "flyer"),
    (re.compile(r"brochure|marketing|\bpackage\b|\bdeck\b|\bpib\b", re.I), "brochure"),
]

# Buildout-hosted download shapes that qualify a keyword-less, extension-less URL.
_BUILDOUT_HOST_RE = re.compile(r"(?:^|\.)buildout\.com$", re.I)
_BUILDOUT_PATH_RE = re.compile(r"/(?:sharing|docs)/", re.I)
_BUILDOUT_FILE_RE = re.compile(r"[?&]file=\d+", re.I)


def _host_of(url):
    """Lowercased hostname of an http(s) url, or '' (no urllib import surprises)."""
    m = re.match(r"^https?://([^/?#]+)", url or "", re.I)
    if not m:
        return ""
    return m.group(1).split("@")[-1].split(":")[0].lower()


def classify_doc(url, title=None):
    """Classify a url+title into a doc_type token, or None when it is not a
    document. Python mirror of lib/harvest.classifyDoc (contract Section D),
    used by the OM-parse / doc-classification tiers (Workflow 2) and available to
    the ingest for parity. Decision order is most-specific first; a keyword hit
    qualifies a url even without a file extension; a bare document extension or a
    recognized Buildout hosted-download link classifies as 'other'.
    """
    if not isinstance(url, str) or not url:
        return None
    hay = f"{url} {title or ''}".lower()
    for rx, doc_type in _DOC_KEYWORD_RULES:
        if rx.search(hay):
            return doc_type
    has_ext = bool(_DOC_EXT_RE.search(url))
    host = _host_of(url)
    is_hosted_download = bool(_BUILDOUT_HOST_RE.search(host)) and (
        bool(_BUILDOUT_PATH_RE.search(url)) or bool(_BUILDOUT_FILE_RE.search(url))
    )
    if has_ext or is_hosted_download:
        return "other"
    return None
