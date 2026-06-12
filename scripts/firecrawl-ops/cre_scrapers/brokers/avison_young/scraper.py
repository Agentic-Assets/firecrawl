#!/usr/bin/env python3
"""
avison_young.py -- Avison Young CRE listing scraper for EQUIRE.

Avison Young (avisonyoung.us) uses a Liferay hash-fragment SPA for its
property search. Listings hydrate client-side after the page loads, so they
are NOT present in the initial HTML response. The scraper uses waitFor=9000
to give the SPA time to render before Firecrawl captures the DOM.

Accessibility note (config.py)
-------------------------------
Rated HARD due to hash SPA. Listings are rendered client-side; the discovery
page may return empty or near-empty markdown even with waitFor=9000. If
discovery returns fewer listings than expected, increase waitFor toward 12000.
The internal listing API (if discoverable via network tab) may be a better
long-term approach for production volume.

URL structure
-------------
Search:  https://www.avisonyoung.us/property-search
         Also supports market sub-pages: /web/<market>/...

Detail:  https://www.avisonyoung.us/web/<market>/properties/<listing-id>
         Example: /web/us-dallas/properties/12345-property-name

Discovery strategy
------------------
Scrape /property-search (or the US sale sub-path) with stealth + waitFor=9000.
Filter returned links for the /web/<market>/properties/<id> pattern. Because
the SPA may not fully hydrate, the scraper also looks for any avisonyoung.us
link that contains "/properties/" and appears to be a detail page.

Parse strategy
--------------
Avison Young covers all property types including office, industrial, retail,
multifamily, and land. Typical markdown fields:
  - Property name and address
  - Transaction type (For Sale / For Lease)
  - Property type (Office, Industrial, Retail, Multi-Family, Land)
  - Size (SF), available SF
  - Sale price or lease rate
  - Year built, floors
  - Description and highlights
  - Broker name, phone, email

Config alignment: config.BROKERS["avison-young"]
  proxy=stealth, wait_for_ms=9000, timeout_ms=90000, pagination=hash_spa_or_internal_api
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

# Avison Young detail URL: /web/<market>/properties/<id> or legacy formats
_DETAIL_URL_RE = re.compile(
    r"avisonyoung\.us/(?:web/[^/]+/properties/|property-detail/)[^/\s?#]+",
    re.IGNORECASE,
)
_PROPERTIES_PATH_RE = re.compile(r"avisonyoung\.us/.+/properties/.+", re.IGNORECASE)

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
        if any(kw in lower for kw in ("contact", "broker", "agent", "advisor", "representative")):
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
        c.setdefault("brokerage_name", "Avison Young")
        c["is_primary"] = i == 0

    return contacts[:3]


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class AvisonYoungScraper(BaseScraper):
    """
    Scraper for Avison Young (avisonyoung.us/property-search).

    Avison Young covers a broad range of commercial property types including
    office, industrial, retail, multifamily, and land across the US.

    Discovery is HARD due to the hash-fragment Liferay SPA: listings hydrate
    client-side after page load. waitFor=9000 is the minimum viable render
    time. If the search page returns fewer than ~5 listing URLs, increase
    waitFor to 12000 or seed URLs from an alternative source (sitemap, search
    partner). The internal listing API (if discoverable) is the recommended
    long-term alternative.
    """

    BROKER_SLUG = "avison-young"
    SEARCH_URL = "https://www.avisonyoung.us/properties-for-sale"
    FIRECRAWL_OPTIONS = {
        "proxy": "stealth",
        "waitFor": 9000,
        "timeout": 90000,
        "onlyMainContent": False,
    }

    # Alternative search entry points to try if primary is thin
    FALLBACK_URLS = [
        "https://www.avisonyoung.us/property-search",
        "https://www.avisonyoung.us/commercial-real-estate/sale",
    ]

    def discover_listings(self, search_url: Optional[str] = None) -> list[str]:
        """
        Scrape the Avison Young property-search page and return detail URLs.

        Due to the hash-fragment SPA, this may return few or no results on
        the first attempt. Returns an empty list (rather than raising) if the
        page fails or is empty; callers should check the count.

        Filter: any avisonyoung.us URL containing /properties/ with an
        additional path segment (detail pages).
        """
        target = search_url or self.SEARCH_URL
        result = self.scrape_url(target, options={"formats": ["links", "markdown"]})
        if not result.get("success"):
            return []

        all_links = self._extract_links(result)
        detail_urls = [
            lnk for lnk in all_links
            if (_DETAIL_URL_RE.search(lnk) or _PROPERTIES_PATH_RE.search(lnk))
            and "avisonyoung.us" in lnk
        ]

        # Filter out bare category/search pages
        detail_urls = [
            lnk for lnk in detail_urls
            if not lnk.rstrip("/").endswith("/property-search")
            and not lnk.rstrip("/").endswith("/properties")
        ]

        return self._dedup(detail_urls)

    def parse_listing(self, url: str, scraped_dict: dict) -> Optional[ListingData]:
        """
        Parse an Avison Young property detail page into a ListingData.

        Extracts:
          - transaction_type from For Sale / For Lease language
          - property_type from keyword matching (office, industrial, retail, etc.)
          - size_sf and available_sf
          - sale_price_usd or lease_rate_min + lease_rate_type
          - cap_rate, noi, occupancy_rate for investment listings
          - year_built, floors
          - city, state, zip from address block
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
            r"\s*[-|]\s*Avison\s*Young.*$", "", raw_title, flags=re.IGNORECASE
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
            "source": "avison-young",
            "meta_title": metadata.get("title"),
            "meta_description": metadata.get("description"),
        }

        return listing
