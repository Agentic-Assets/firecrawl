#!/usr/bin/env python3
"""
nai_global.py -- NAI Global CRE listing scraper for EQUIRE.

NAI Global is one of the largest commercial real estate networks in the
world, operating through independently owned member offices (franchisees).
Its North American listing search at /north-american-listings/ aggregates
properties across all member offices and covers all property types.

Accessibility note (config.py)
-------------------------------
Rated MEDIUM. The primary barrier is a CookieYes consent wall (not Cloudflare).
The ``actions`` config click on the accept button clears the consent wall
before Firecrawl captures the page content. Once cleared, listing cards render
normally. Franchise listings may also live on NAI member subdomain sites
(e.g. naiaustintexas.com) rather than naiglobal.com -- those sub-sites are
outside the scope of this scraper.

NOTE: NAI's site structure may require testing -- stub implementation.
Validate discover_listings output before production use. The consent wall
interaction depends on the CookieYes selector matching; verify with a manual
Firecrawl scrape of the search page if listings are not returned.

Discovery strategy
------------------
Scrape /north-american-listings/ with the CookieYes accept action. Filter
returned links for the /north-american-listings/<id>/ or /listing/<id>/
pattern. NAI listing IDs are typically numeric or short alphanumeric slugs.

Parse strategy
--------------
NAI Global covers all commercial property types: office, retail, industrial,
land, multifamily, and hospitality. Listing pages follow a typical brokerage
template with labeled fields. The markdown typically includes:
  - Property name and full address
  - Transaction type (For Sale / For Lease)
  - Property type
  - Size (SF), available SF
  - Sale price or lease rate
  - Cap rate and NOI for investment listings
  - Year built
  - Description
  - Contact broker(s)

Config alignment: config.BROKERS["nai-global"]
  proxy=stealth, wait_for_ms=5000, timeout_ms=60000, actions=[click accept cookie]
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

# NAI Global detail URL patterns
_DETAIL_URL_PATTERNS = [
    re.compile(r"naiglobal\.com/north-american-listings/[^/?#\s]+", re.IGNORECASE),
    re.compile(r"naiglobal\.com/listing/[^/?#\s]+", re.IGNORECASE),
    re.compile(r"naiglobal\.com/properties/[^/?#\s]+", re.IGNORECASE),
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
_LOT_SIZE_RE = re.compile(
    r"lot\s+(?:size|area)[:\s]+([\d,]+(?:\.\d+)?)\s*(SF|sq\.?\s*ft\.?|acres?)",
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

_LEASE_TYPE_PATTERNS = [
    (re.compile(r"\bfull\s+service\b", re.IGNORECASE), "full_service"),
    (re.compile(r"\bmodified\s+gross\b|\bmg\b", re.IGNORECASE), "modified_gross"),
    (re.compile(r"\bnnn\b|\btriple\s+net\b", re.IGNORECASE), "nnn"),
    (re.compile(r"\bgross\b", re.IGNORECASE), "gross"),
]

# CookieYes accept action (matches config.py)
_COOKIEYES_ACTION = {
    "type": "click",
    "selector": "[data-cky-tag=accept-button], .cky-btn-accept",
}


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
        if any(kw in lower for kw in ("contact", "broker", "agent", "advisor", "nai")):
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
        c.setdefault("brokerage_name", "NAI Global")
        c["is_primary"] = i == 0

    return contacts[:3]


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class NAIGlobalScraper(BaseScraper):
    """
    Scraper for NAI Global (naiglobal.com/north-american-listings/).

    NAI Global aggregates listings from independently owned member offices
    across North America, covering all commercial property types.

    NOTE: NAI's site structure may require testing -- stub implementation.
    Validate discover_listings output before production use. The CookieYes
    consent-wall click (data-cky-tag=accept-button) must succeed for listings
    to render. Test manually if discovery returns empty results:

        python3 -c "
        from cre_scrapers.nai_global import NAIGlobalScraper
        s = NAIGlobalScraper()
        urls = s.discover_listings()
        print(len(urls), urls[:3])
        "

    Franchise sub-site listings (e.g. on naiaustintexas.com) are outside the
    scope of this scraper; only naiglobal.com URLs are returned.
    """

    BROKER_SLUG = "nai-global"
    SEARCH_URL = "https://www.naiglobal.com/north-american-listings/"
    FIRECRAWL_OPTIONS = {
        "proxy": "stealth",
        "waitFor": 5000,
        "timeout": 60000,
        "onlyMainContent": False,
        # CookieYes consent-wall click -- clear before content capture
        "actions": [_COOKIEYES_ACTION],
    }

    def discover_listings(self, search_url: Optional[str] = None) -> list[str]:
        """
        Scrape the NAI Global North American listings page and return detail URLs.

        Includes the CookieYes accept action via FIRECRAWL_OPTIONS. If the
        consent wall is not cleared, the page may return empty or minimal
        markdown.

        Filter: links matching known NAI Global listing URL patterns.
        Returns an empty list on failure rather than raising.
        """
        target = search_url or self.SEARCH_URL
        result = self.scrape_url(target, options={"formats": ["links", "markdown"]})
        if not result.get("success"):
            return []

        all_links = self._extract_links(result)
        detail_urls = [
            lnk for lnk in all_links
            if _is_detail_url(lnk) and "naiglobal.com" in lnk
        ]

        # Fallback: any naiglobal.com link with 3+ path segments that looks like a detail
        if not detail_urls:
            detail_urls = [
                lnk for lnk in all_links
                if "naiglobal.com" in lnk
                and len([s for s in lnk.split("/") if s]) >= 5
                and not any(kw in lnk.lower() for kw in ("search", "filter", "category", "page"))
            ]

        return self._dedup(detail_urls)

    def parse_listing(self, url: str, scraped_dict: dict) -> Optional[ListingData]:
        """
        Parse a NAI Global property detail page into a ListingData.

        Covers all commercial property types. Extracts:
          - transaction_type from For Sale / For Lease language
          - property_type from keyword matching
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

        # External ID: last non-empty path segment
        path = url.rstrip("/").split("?")[0]
        segments = [s for s in path.split("/") if s]
        listing.external_id = segments[-1] if segments else None

        # Title
        raw_title = metadata.get("title") or metadata.get("ogTitle") or ""
        listing.title = re.sub(
            r"\s*[-|]\s*NAI.*$", "", raw_title, flags=re.IGNORECASE
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

        # Lot size
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
            "source": "nai-global",
            "meta_title": metadata.get("title"),
            "meta_description": metadata.get("description"),
        }

        return listing
