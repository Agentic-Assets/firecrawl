#!/usr/bin/env python3
"""
newmark.py -- Newmark CRE listing scraper for EQUIRE.

Newmark (nmrk.com) is a major institutional CRE brokerage covering capital
markets, leasing, debt and equity, valuation, and property management across
all asset classes.

Accessibility note (config.py)
-------------------------------
Rated HARD (gated). The public /properties page is an investor-portal signup
funnel with no public listings. Real listings are behind nim.nmrk.com, which
requires authentication. The broker is marked active=False in config.py.

This scraper is a stub implementation. The discover_listings() method attempts
the public URL and logs a warning when it is gated. If Newmark listings become
accessible (e.g. via a public API, sitemap, or third-party aggregator), update
SEARCH_URL and the link filter in discover_listings accordingly.

NOTE: Stub implementation -- validate discover_listings output before production
use. The public /properties page is gated behind an investor portal signup;
real listings require nim.nmrk.com authentication. This scraper will return
empty discovery results until a public access path is identified.

Detail parse strategy (for use with externally sourced URLs)
------------------------------------------------------------
If Newmark detail URLs are obtained via a non-scrape path (sitemap, partner
feed, manual entry), parse_listing() can process their markdown. Typical fields:
  - Property name and address
  - Transaction type (For Sale / For Lease)
  - Property type (office, industrial, retail, multifamily)
  - Size (SF), available SF
  - Sale price or lease rate
  - Cap rate, NOI for investment listings
  - Year built, floors
  - Broker contacts

Config alignment: config.BROKERS["newmark"]
  proxy=stealth, wait_for_ms=5000, timeout_ms=60000, active=False (gated)
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
# Patterns
# ---------------------------------------------------------------------------

# Newmark detail URL patterns (nim.nmrk.com is the authenticated portal;
# nmrk.com/properties?... is the public gated page)
_DETAIL_URL_PATTERNS = [
    re.compile(r"nmrk\.com/properties/[^/?#\s]+", re.IGNORECASE),
    re.compile(r"nim\.nmrk\.com/listings/[^/?#\s]+", re.IGNORECASE),
    re.compile(r"newmark\.com/properties/[^/?#\s]+", re.IGNORECASE),
]

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
_AVAILABLE_SF_RE = re.compile(
    r"available[:\s]+([\d,]+(?:\.\d+)?)\s*(?:SF|sq\.?\s*ft\.?)",
    re.IGNORECASE,
)
_LEASE_RATE_RE = re.compile(
    r"\$?([\d,.]+)\s*/?\s*(?:SF|PSF)\s*/?\s*(?:yr|year|ann(?:ually?)?)?",
    re.IGNORECASE,
)
_UNITS_RE = re.compile(
    r"(?:number\s+of\s+)?(?:total\s+)?units?[:\s]+(\d+)",
    re.IGNORECASE,
)
_CITY_STATE_ZIP_RE = re.compile(
    r"([A-Za-z\s\-'.]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)"
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.+-]+\.\w+")
_PHONE_RE = re.compile(r"\+?1?[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}")

# Gated-page indicators
_GATED_SIGNALS = re.compile(
    r"\b(?:sign\s*in|log\s*in|create\s*account|register|sign\s*up|investor\s*portal)\b",
    re.IGNORECASE,
)

_LEASE_TYPE_PATTERNS = [
    (re.compile(r"\bfull\s+service\b", re.IGNORECASE), "full_service"),
    (re.compile(r"\bmodified\s+gross\b|\bmg\b", re.IGNORECASE), "modified_gross"),
    (re.compile(r"\bnnn\b|\btriple\s+net\b", re.IGNORECASE), "nnn"),
    (re.compile(r"\bgross\b", re.IGNORECASE), "gross"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_detail_url(url: str) -> bool:
    return any(p.search(url) for p in _DETAIL_URL_PATTERNS)


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
        if any(kw in lower for kw in ("contact", "broker", "agent", "advisor", "newmark")):
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
        c.setdefault("brokerage_name", "Newmark")
        c["is_primary"] = i == 0

    return contacts[:3]


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class NewmarkScraper(BaseScraper):
    """
    Scraper for Newmark (nmrk.com/properties).

    NOTE: Stub implementation -- validate discover_listings output before
    production use. The public /properties page is gated behind an investor
    portal signup; real listings require nim.nmrk.com authentication. This
    scraper will return empty discovery results until a public access path
    is identified.

    Newmark covers institutional-grade properties across all asset classes
    including office, industrial, retail, multifamily, hospitality, and land.
    It is a major capital-markets and leasing advisor for institutional owners
    and occupiers.

    If Newmark listing URLs are obtained via a non-scrape path (sitemap,
    partner data feed, or manual entry), they can be passed directly to
    batch_scrape() and parse_listing() will process the resulting markdown.
    """

    BROKER_SLUG = "newmark"
    SEARCH_URL = "https://www.nmrk.com/properties?saleOrLease=sale"
    FIRECRAWL_OPTIONS = {
        "proxy": "stealth",
        "waitFor": 5000,
        "timeout": 60000,
        "onlyMainContent": False,
    }

    def discover_listings(self, search_url: Optional[str] = None) -> list[str]:
        """
        Attempt to scrape the Newmark properties page for listing URLs.

        This legacy page-scraper path logs a warning when rendered pages gate
        behind nim.nmrk.com and returns an empty list. The production collector
        uses the public Algolia API in ../cre_collector/collect.ts.

        Returns an empty list (rather than raising) on failure or gating.
        """
        target = search_url or self.SEARCH_URL
        result = self.scrape_url(target, options={"formats": ["links", "markdown"]})
        if not result.get("success"):
            return []

        markdown = self._get_markdown(result)
        # Detect gated page
        if _GATED_SIGNALS.search(markdown or ""):
            print(
                f"[{self.BROKER_SLUG}] discover: gated page detected (portal/sign-in). "
                "No public listings available. Provide URLs directly to batch_scrape() "
                "or update SEARCH_URL when a public path becomes accessible.",
                flush=True,
            )
            return []

        all_links = self._extract_links(result)
        detail_urls = [lnk for lnk in all_links if _is_detail_url(lnk)]

        return self._dedup(detail_urls)

    def parse_listing(self, url: str, scraped_dict: dict) -> Optional[ListingData]:
        """
        Parse a Newmark property detail page into a ListingData.

        Designed for when Newmark listing URLs are sourced externally.
        Returns None if the page appears gated or empty.

        Extracts:
          - transaction_type from For Sale / For Lease language
          - property_type from keyword matching
          - size_sf, available_sf
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

        # Reject gated pages
        if _GATED_SIGNALS.search(markdown) and len(markdown) < 2000:
            return None

        metadata = scraped_dict.get("data", scraped_dict).get("metadata", {})

        listing = ListingData(
            brokerage_slug=self.BROKER_SLUG,
            source_url=url,
            markdown=markdown,
        )

        # External ID: last non-empty path segment
        path = url.rstrip("/").split("?")[0]
        segments = [s for s in path.split("/") if s]
        listing.external_id = segments[-1] if segments else None

        # Title
        raw_title = metadata.get("title") or metadata.get("ogTitle") or ""
        listing.title = re.sub(
            r"\s*[-|]\s*Newmark.*$", "", raw_title, flags=re.IGNORECASE
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
            "source": "newmark",
            "meta_title": metadata.get("title"),
            "meta_description": metadata.get("description"),
        }

        return listing
