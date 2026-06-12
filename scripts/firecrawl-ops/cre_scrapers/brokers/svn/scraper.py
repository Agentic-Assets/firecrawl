#!/usr/bin/env python3
"""
svn.py -- SVN International CRE listing scraper for EQUIRE.

SVN (Shared Value Network) is a nationwide commercial real estate brokerage
operating through a franchise network. It covers retail, industrial, office,
multifamily, and land across the US, with a focus on smaller and mid-market
transactions.

Accessibility note (config.py)
-------------------------------
Rated MEDIUM. SVN runs a WordPress site with no Cloudflare. The bare browse
page (/properties/) contains navigation only, not listing cards. Listing cards
appear only when a search filter is applied via ?propertyTypes=<id>. Standard
WordPress archive pagination applies (&page=N).

Property type IDs (WordPress taxonomy IDs):
  1  = Office
  2  = Retail
  3  = Industrial
  4  = Land
  5  = Multifamily
  6  = Hospitality
  7  = Net Lease
  8  = Self Storage
  9  = Senior Housing
  10 = Mixed Use

Discovery strategy
------------------
Scrape /properties/?propertyTypes=<id> for each property type, then combine
and deduplicate. Each page returns ~12 listing cards with links. Paginate with
&page=N until a page returns no new listing links.

Parse strategy
--------------
SVN listing pages are plain WordPress single pages. Markdown
typically includes:
  - Property name and address
  - Transaction type (For Sale / For Lease)
  - Property type
  - Size (SF), available SF, lot SF
  - Sale price or lease rate
  - Year built, floors, parking
  - Description
  - Broker name, phone, email

Config alignment: config.BROKERS["svn"]
  proxy=stealth, wait_for_ms=5000, timeout_ms=60000, pagination=wordpress_query_params
"""

from __future__ import annotations

import re
from typing import Optional

from ...base import BaseScraper
from ...normalizer import (
    ListingData,
    normalize_cap_rate,
    normalize_price,
    normalize_sqft,
    normalize_state,
    extract_property_type,
    extract_transaction_type,
)

# ---------------------------------------------------------------------------
# SVN WordPress property type IDs for search URL construction
# ---------------------------------------------------------------------------

# Covering the highest-volume types first
PROPERTY_TYPE_IDS = [2, 3, 1, 5, 4, 7]  # retail, industrial, office, multifamily, land, net lease

# Base search URL patterns
SEARCH_URL_TEMPLATE = "https://svn.com/properties/?propertyTypes={type_id}"
PAGINATION_URL_TEMPLATE = "https://svn.com/properties/?propertyTypes={type_id}&page={page}"

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# SVN detail URL: /properties/<listing-slug>/ (no type_id in path)
_DETAIL_URL_RE = re.compile(
    r"svn\.com/properties/[^/?#\s]+/?$",
    re.IGNORECASE,
)

_YEAR_BUILT_RE = re.compile(
    r"(?:year\s+built|built\s+in|year\s+of\s+construction)[:\s]*(\d{4})",
    re.IGNORECASE,
)
_FLOORS_RE = re.compile(r"(\d+)\s*(?:floors?|stories|stor(?:ey|ies))", re.IGNORECASE)
_CAP_RATE_RE = re.compile(r"cap\s*rate[:\s]+(\d+\.?\d*)\s*%", re.IGNORECASE)
_NOI_RE = re.compile(
    r"(?:noi|net\s+operating\s+income)[:\s]+\$?([\d,]+(?:\.\d+)?)\s*([Mm](?:illion)?)?",
    re.IGNORECASE,
)
_OCCUPANCY_RE = re.compile(
    r"(?:occupancy|occupied|leased)[:\s]+(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
_LOT_SIZE_RE = re.compile(
    r"lot\s+(?:size|area)[:\s]+([\d,]+(?:\.\d+)?)\s*(SF|sq\.?\s*ft\.?|acres?)",
    re.IGNORECASE,
)
_AVAILABLE_SF_RE = re.compile(
    r"available[:\s]+([\d,]+(?:\.\d+)?)\s*(?:SF|sq\.?\s*ft\.?)",
    re.IGNORECASE,
)
_LEASE_RATE_RE = re.compile(
    r"\$?([\d,.]+)\s*/?\s*(?:SF|PSF)\s*/?\s*(?:yr|year|ann(?:ually?)?)?",
    re.IGNORECASE,
)
_CITY_STATE_ZIP_RE = re.compile(
    r"([A-Za-z\s\-'.]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)"
)
_UNITS_RE = re.compile(
    r"(?:number\s+of\s+)?(?:total\s+)?units?[:\s]+(\d+)",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.+-]+\.\w+")
_PHONE_RE = re.compile(r"\+?1?[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}")

_LEASE_TYPE_PATTERNS = [
    (re.compile(r"\bfull\s+service\b", re.IGNORECASE), "full_service"),
    (re.compile(r"\bmodified\s+gross\b|\bmg\b", re.IGNORECASE), "modified_gross"),
    (re.compile(r"\bnnn\b|\btriple\s+net\b|\bnet\s+lease\b", re.IGNORECASE), "nnn"),
    (re.compile(r"\bgross\b", re.IGNORECASE), "gross"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_noi(text: str) -> Optional[float]:
    m = _NOI_RE.search(text)
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    if (m.group(2) or "").lower().startswith("m"):
        val *= 1_000_000
    return val


def _parse_occupancy(text: str) -> Optional[float]:
    m = _OCCUPANCY_RE.search(text)
    return round(float(m.group(1)) / 100, 6) if m else None


def _parse_lease_type(text: str) -> Optional[str]:
    for pattern, lt in _LEASE_TYPE_PATTERNS:
        if pattern.search(text):
            return lt
    return None


def _extract_contacts(lines: list[str]) -> list[dict]:
    contacts: list[dict] = []
    current: dict = {}
    in_contact = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current.get("name"):
                contacts.append(current)
                current = {}
                in_contact = False
            continue
        lower = stripped.lower()
        if any(kw in lower for kw in ("contact", "broker", "agent", "advisor", "svn")):
            in_contact = True
        if in_contact:
            em = _EMAIL_RE.search(stripped)
            ph = _PHONE_RE.search(stripped)
            if em:
                current.setdefault("email", em.group(0))
            if ph:
                current.setdefault("phone", ph.group(0).strip())
            if (not em and not ph and stripped and stripped[0].isupper()
                    and len(stripped) < 60 and not any(c.isdigit() for c in stripped[:2])):
                current.setdefault("name", stripped)

    if current.get("name"):
        contacts.append(current)

    for i, c in enumerate(contacts):
        c.setdefault("brokerage_name", "SVN")
        c["is_primary"] = i == 0

    return contacts[:3]


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class SVNScraper(BaseScraper):
    """
    Scraper for SVN International (svn.com/properties/).

    SVN covers nationwide commercial real estate including retail, industrial,
    office, multifamily, land, net-lease, and self-storage through a franchise
    network. Listings tend toward small-to-mid market transactions.

    Discovery uses WordPress ?propertyTypes=<id> query parameters. The bare
    /properties/ URL contains no listing cards; at least one propertyTypes
    filter must be applied. The scraper iterates over PROPERTY_TYPE_IDS and
    combines results. Standard WP pagination (&page=N) is used for multi-page
    result sets.
    """

    BROKER_SLUG = "svn"
    SEARCH_URL = "https://svn.com/properties/?propertyTypes=3"  # industrial as default
    FIRECRAWL_OPTIONS = {
        "proxy": "stealth",
        "waitFor": 5000,
        "timeout": 60000,
        "onlyMainContent": False,
    }

    def discover_listings(self, search_url: Optional[str] = None) -> list[str]:
        """
        Scrape SVN property listings across multiple property type filters.

        Iterates PROPERTY_TYPE_IDS, scrapes page 1 of each, and collects
        all /properties/<slug> detail URLs. Does not paginate further on a
        single run; for production volume, extend with &page=N iteration.

        Returns a deduplicated list of absolute detail-page URLs.
        """
        if search_url:
            # Single custom URL -- just scrape it
            targets = [search_url]
        else:
            targets = [
                SEARCH_URL_TEMPLATE.format(type_id=tid)
                for tid in PROPERTY_TYPE_IDS
            ]

        all_detail_urls: list[str] = []

        for target in targets:
            result = self.scrape_url(target, options={"formats": ["links", "markdown"]})
            if not result.get("success"):
                continue
            links = self._extract_links(result)
            detail_urls = [
                lnk for lnk in links
                if _DETAIL_URL_RE.search(lnk)
                # Exclude the base search URL and paginated search URLs
                and "propertyTypes" not in lnk
                and "&page=" not in lnk
            ]
            all_detail_urls.extend(detail_urls)

        return self._dedup(all_detail_urls)

    def parse_listing(self, url: str, scraped_dict: dict) -> Optional[ListingData]:
        """
        Parse an SVN property detail page into a ListingData.

        SVN is WordPress-based: plain markdown with labeled fields.
        Extracts:
          - transaction_type from For Sale / For Lease language
          - property_type from keyword matching (retail, industrial, etc.)
          - size_sf, available_sf, lot_size_sf
          - sale_price_usd or lease_rate_min + lease_rate_type
          - cap_rate, noi, occupancy_rate for investment listings
          - units for multifamily
          - year_built, floors
          - city, state, zip
          - broker contacts
        """
        markdown = self._get_markdown(scraped_dict)
        if not markdown or len(markdown) < 100:
            return None

        metadata = scraped_dict.get("data", scraped_dict).get("metadata", {})

        listing = ListingData(
            brokerage_slug=self.BROKER_SLUG,
            source_url=url,
            markdown=markdown,
        )

        # External ID: the listing slug from the URL
        path = url.rstrip("/").split("?")[0]
        listing.external_id = path.split("/")[-1] or None

        # Title
        raw_title = metadata.get("title") or metadata.get("ogTitle") or ""
        listing.title = re.sub(
            r"\s*[-|]\s*SVN.*$", "", raw_title, flags=re.IGNORECASE
        ).strip() or None

        # Transaction type and property type
        listing.transaction_type = extract_transaction_type(markdown)
        listing.property_type = extract_property_type(markdown)

        # Location
        cz = _CITY_STATE_ZIP_RE.search(markdown)
        if cz:
            listing.city = cz.group(1).strip().title()
            listing.state = normalize_state(cz.group(2))
            listing.zip = cz.group(3)

        # Size
        listing.size_sf = normalize_sqft(markdown[:2000])

        # Available SF
        av_m = _AVAILABLE_SF_RE.search(markdown)
        if av_m:
            listing.available_sf = float(av_m.group(1).replace(",", ""))

        # Lot size (land and industrial)
        lot_m = _LOT_SIZE_RE.search(markdown)
        if lot_m:
            lot_val = float(lot_m.group(1).replace(",", ""))
            if "acre" in lot_m.group(2).lower():
                lot_val *= 43560.0
            listing.lot_size_sf = lot_val

        # Units (multifamily)
        u_m = _UNITS_RE.search(markdown)
        if u_m:
            listing.units = int(u_m.group(1))

        # Year built / floors
        yr_m = _YEAR_BUILT_RE.search(markdown)
        if yr_m:
            listing.year_built = int(yr_m.group(1))

        fl_m = _FLOORS_RE.search(markdown)
        if fl_m:
            listing.floors = int(fl_m.group(1))

        # Price / rate
        if listing.transaction_type in ("sale", "sale_or_lease", None):
            price_m = re.search(
                r"(?:sale\s+price|asking\s+price|list\s+price|offering\s+price)[:\s]+\$?([\d,]+(?:\.\d+)?)\s*([Mm](?:illion)?)?",
                markdown,
                re.IGNORECASE,
            )
            if price_m:
                val = float(price_m.group(1).replace(",", ""))
                if (price_m.group(2) or "").lower().startswith("m"):
                    val *= 1_000_000
                listing.sale_price_usd = val
            elif listing.transaction_type == "sale":
                listing.sale_price_usd = normalize_price(markdown[:500])

        if listing.transaction_type in ("lease", "sale_or_lease"):
            lr_m = _LEASE_RATE_RE.search(markdown)
            if lr_m:
                rate = float(lr_m.group(1).replace(",", ""))
                listing.lease_rate_min = rate
                listing.lease_rate_max = rate
            listing.lease_rate_type = _parse_lease_type(markdown)

        # Investment metrics
        cap_m = _CAP_RATE_RE.search(markdown)
        if cap_m:
            listing.cap_rate = normalize_cap_rate(cap_m.group(1) + "%")

        listing.noi = _parse_noi(markdown)
        listing.occupancy_rate = _parse_occupancy(markdown)

        # Description
        paras = [p.strip() for p in markdown.split("\n\n") if len(p.strip()) >= 80]
        if paras:
            listing.description = paras[0]

        # Highlights
        bullets = re.findall(r"^[-*]\s+(.+)$", markdown, re.MULTILINE)
        listing.highlights = [b.strip() for b in bullets[:10] if len(b.strip()) > 5]

        # Contacts
        listing.contacts = _extract_contacts(markdown.split("\n"))

        # Documents and images
        links = self._extract_links(scraped_dict)
        listing.documents = [
            {"doc_type": "brochure", "url": lnk}
            for lnk in links
            if lnk.lower().endswith(".pdf") or "brochure" in lnk.lower()
        ]
        listing.images = [
            {"url": lnk, "is_primary": i == 0}
            for i, lnk in enumerate(
                lnk for lnk in links
                if any(lnk.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp"))
            )
        ]

        listing.raw_data = {
            "source": "svn",
            "meta_title": metadata.get("title"),
            "meta_description": metadata.get("description"),
        }

        return listing
