"""
normalizer.py -- Canonical ListingData dataclass and field-level normalizers.

All broker scrapers produce a ListingData. The listing_to_supabase_dict()
function converts it to the dict shape expected by the cre_listings REST upsert
(on_conflict=brokerage_id,external_id).

State abbreviation map covers all 50 states + DC + territories.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional


# ---------------------------------------------------------------------------
# Canonical listing dataclass
# ---------------------------------------------------------------------------

@dataclass
class ListingData:
    """Canonical representation of a CRE listing.

    Field names match cre_listings column names exactly so
    listing_to_supabase_dict() can pass through most fields unchanged.
    """

    # Identity
    brokerage_slug: str = ""
    external_id: Optional[str] = None
    source_url: str = ""
    status: str = "active"

    # Classification
    transaction_type: Optional[str] = None  # sale | lease | sale_or_lease
    property_type: Optional[str] = None

    # Descriptive
    title: Optional[str] = None
    description: Optional[str] = None
    highlights: list = field(default_factory=list)
    zoning: Optional[str] = None
    markdown: Optional[str] = None

    # Location
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    country: str = "US"
    lat: Optional[float] = None
    lng: Optional[float] = None
    market: Optional[str] = None
    submarket: Optional[str] = None

    # Physical attributes
    size_sf: Optional[float] = None
    available_sf: Optional[float] = None
    min_divisible_sf: Optional[float] = None
    floors: Optional[int] = None
    year_built: Optional[int] = None
    units: Optional[int] = None

    # Financials -- sale
    sale_price_usd: Optional[float] = None
    sale_price_per_sf: Optional[float] = None
    cap_rate: Optional[float] = None          # stored as decimal: 0.065 = 6.5%
    noi: Optional[float] = None
    occupancy_rate: Optional[float] = None    # stored as decimal: 0.95 = 95%

    # Financials -- lease
    lease_rate_min: Optional[float] = None    # $/SF/year
    lease_rate_max: Optional[float] = None
    lease_rate_type: Optional[str] = None     # NNN | MG | FS | etc.

    # Related objects (written to child tables)
    contacts: list = field(default_factory=list)   # [{name, title, email, phone}]
    documents: list = field(default_factory=list)  # [{doc_type, title, url}]
    images: list = field(default_factory=list)     # [{url, is_primary}]

    # Opaque broker payload for debugging / future field extraction
    raw_data: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# State name / abbreviation map
# ---------------------------------------------------------------------------

_STATE_NAMES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "washington dc": "DC", "washington d.c.": "DC",
    "puerto rico": "PR", "guam": "GU", "virgin islands": "VI",
}

_STATE_ABBREVS: set[str] = set(_STATE_NAMES.values())


def normalize_state(s: str) -> Optional[str]:
    """Return a 2-letter state abbreviation, or None if unrecognized.

    Handles both full names ("Texas") and abbreviations ("TX").
    Case-insensitive. Strips trailing/leading whitespace and periods.
    """
    if not s:
        return None
    cleaned = s.strip().rstrip(".").strip()
    upper = cleaned.upper()
    if upper in _STATE_ABBREVS:
        return upper
    lower = cleaned.lower()
    return _STATE_NAMES.get(lower)


# ---------------------------------------------------------------------------
# Price / size normalizers
# ---------------------------------------------------------------------------

def normalize_price(s: str) -> Optional[float]:
    """Convert a price string to a float dollar amount.

    Examples:
      "$1.2M"          -> 1_200_000.0
      "$25,000,000"    -> 25_000_000.0
      "$25/PSF"        -> 25.0          (per-SF rate, caller decides field)
      "25.00 /SF/yr"   -> 25.0
      "Contact Broker" -> None
      "Negotiable"     -> None
      ""               -> None
    """
    if not s:
        return None
    raw = s.strip()
    # Skip non-numeric sentinel strings
    lower = raw.lower()
    skip_phrases = (
        "contact", "call", "negotiate", "negotiable", "upon request",
        "tbd", "n/a", "see agent", "market",
    )
    for phrase in skip_phrases:
        if phrase in lower:
            return None

    # Strip currency symbol and whitespace
    cleaned = re.sub(r"[$,\s]", "", raw)

    # Handle multiplier suffixes: M/B/K (case-insensitive)
    multiplier = 1.0
    m = re.match(r"^([\d.]+)([KkMmBb])", cleaned)
    if m:
        num_str, suffix = m.group(1), m.group(2).upper()
        multiplier = {"K": 1e3, "M": 1e6, "B": 1e9}[suffix]
        try:
            return float(num_str) * multiplier
        except ValueError:
            return None

    # Strip trailing units like /PSF /SF/yr /SF/Yr /yr
    cleaned = re.sub(r"/[A-Za-z/]+$", "", cleaned).strip()

    try:
        return float(cleaned) * multiplier
    except ValueError:
        return None


def normalize_sqft(s: str) -> Optional[float]:
    """Convert a square footage string to a float.

    Examples:
      "12,500 SF"  -> 12_500.0
      "12.5K SF"   -> 12_500.0
      "1.2 Acres"  -> None  (caller handles unit conversion if needed)
      ""           -> None
    """
    if not s:
        return None
    raw = s.strip()
    lower = raw.lower()

    # Reject acres and other non-SF units for now
    if "acre" in lower:
        return None

    # Strip commas, SF/sq ft labels
    cleaned = re.sub(r"[,\s]*(sq\.?\s*ft\.?|sf|square\s*feet?)", "", raw, flags=re.IGNORECASE).strip()
    cleaned = cleaned.replace(",", "")

    # Handle K suffix
    m = re.match(r"^([\d.]+)[Kk]", cleaned)
    if m:
        try:
            return float(m.group(1)) * 1000.0
        except ValueError:
            return None

    try:
        return float(cleaned)
    except ValueError:
        return None


def normalize_cap_rate(s: str) -> Optional[float]:
    """Convert a cap rate string to a decimal fraction.

    "6.5%"  -> 0.065
    "6.50%" -> 0.065
    ".065"  -> 0.065  (already decimal)
    ""      -> None
    """
    if not s:
        return None
    cleaned = s.strip().replace("%", "").replace(",", "").strip()
    try:
        val = float(cleaned)
    except ValueError:
        return None
    # If value > 1, treat as percentage
    if val > 1:
        return val / 100.0
    return val


def clean_phone(s: str) -> str:
    """Normalize a phone number to digits-and-dashes: '(555) 123-4567' -> '555-123-4567'."""
    if not s:
        return ""
    digits = re.sub(r"\D", "", s)
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits[0] == "1":
        d = digits[1:]
        return f"{d[:3]}-{d[3:6]}-{d[6:]}"
    # Return cleaned digits if pattern unrecognized
    return digits


# ---------------------------------------------------------------------------
# Property type / transaction type extraction
# ---------------------------------------------------------------------------

_PROPERTY_TYPE_MAP: list[tuple[str, str]] = [
    # (lowercase keyword, canonical enum value)
    ("multifamily", "multifamily"),
    ("multi-family", "multifamily"),
    ("multi family", "multifamily"),
    ("apartment", "multifamily"),
    ("industrial", "industrial"),
    ("warehouse", "industrial"),
    ("distribution", "industrial"),
    ("flex", "flex"),
    ("r&d", "flex"),
    ("research and development", "flex"),
    ("retail", "retail"),
    ("shopping center", "retail"),
    ("strip center", "retail"),
    ("hotel", "hospitality"),
    ("motel", "hospitality"),
    ("hospitality", "hospitality"),
    ("land", "land"),
    ("ground lease", "land"),
    ("medical", "medical_office"),
    ("healthcare", "medical_office"),
    ("life science", "life_science"),
    ("lab", "life_science"),
    ("data center", "data_center"),
    ("self-storage", "self_storage"),
    ("storage", "self_storage"),
    ("senior", "senior_housing"),
    ("assisted living", "senior_housing"),
    ("office", "office"),
    ("mixed use", "mixed_use"),
    ("mixed-use", "mixed_use"),
]


def extract_property_type(text: str) -> Optional[str]:
    """Map free-text property description to a canonical property_type enum value.

    Checks keywords in order from most-specific to least-specific.
    Returns None if no match found.
    """
    if not text:
        return None
    lower = text.lower()
    for keyword, ptype in _PROPERTY_TYPE_MAP:
        if keyword in lower:
            return ptype
    return None


def extract_transaction_type(text: str) -> Optional[str]:
    """Detect sale/lease/sale_or_lease from text.

    Returns "sale", "lease", "sale_or_lease", or None.
    """
    if not text:
        return None
    lower = text.lower()
    has_sale = bool(re.search(r"\bfor\s+sale\b|\bsale\b", lower))
    has_lease = bool(re.search(r"\bfor\s+lease\b|\bfor\s+rent\b|\blease\b", lower))
    if has_sale and has_lease:
        return "sale_or_lease"
    if has_sale:
        return "sale"
    if has_lease:
        return "lease"
    return None


# ---------------------------------------------------------------------------
# Supabase REST serializer
# ---------------------------------------------------------------------------

def listing_to_supabase_dict(listing: ListingData, brokerage_id: str) -> dict:
    """Convert a ListingData to a flat dict ready for Supabase REST upsert.

    - brokerage_id: the UUID from the cre_brokerages table for this broker.
    - Drops None values so Supabase does not overwrite existing fields with null.
    - Serializes highlights/raw_data as JSON-compatible types (already are).
    - contacts/documents/images are NOT included -- caller writes those
      to child tables via save_contacts() / save_documents().
    """
    d = asdict(listing)

    # Remove child-table fields (written separately)
    for key in ("contacts", "documents", "images"):
        d.pop(key, None)

    # Inject the FK
    d["brokerage_id"] = brokerage_id

    # Drop None values to avoid stomping existing DB data on upsert
    return {k: v for k, v in d.items() if v is not None}
