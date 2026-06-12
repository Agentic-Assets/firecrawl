#!/usr/bin/env python3
"""
colliers.py -- Colliers International CRE listing scraper for EQUIRE.

Colliers is a legacy probe target. Earlier page probes found stable
usa{7-digit} listing IDs, but the production collector does not include it
until a usable public GET path, Firecrawl action path, or authorized
integration is available.

Discovery strategy
------------------
Scrape /en/properties with stealth + waitFor=5000 to let the Coveo SPA hydrate.
Filter returned links by the usa<7-digit> ID pattern. Additional property types
and markets can be explored by appending Coveo facet hash fragments or query
parameters (e.g. ?types=industrial).

Parse strategy
--------------
Colliers emphasizes investment and industrial properties. The markdown will
typically contain:

  - Property name / address / market
  - Sale price or lease rate (NNN common for retail/industrial)
  - Property type, size (SF), lot SF for industrial
  - Year built, floors, parking ratio
  - Investment metrics: cap rate, NOI, occupancy
  - Description and bullet highlights
  - Broker name, phone, email

external_id is extracted from the usa<7-digit> tail of the URL, which is a
stable and parseable identifier for Supabase dedup.

Config alignment: config.BROKERS["colliers"]
  proxy=stealth, wait_for_ms=5000, timeout_ms=60000, pagination=coveo_hash_facets
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
# URL / ID patterns
# ---------------------------------------------------------------------------

# Colliers listing ID embedded in detail URLs: "usa" + exactly 6-8 digits
_COLLIERS_ID_RE = re.compile(r"(usa[0-9]{6,8})", re.IGNORECASE)

# Lease rate: "$12.50 /SF/YR" or "12.50 PSF"
_LEASE_RATE_RE = re.compile(
    r"\$?([\d,.]+)\s*/?\s*(?:SF|sq\.?\s*ft\.?|PSF)\s*/?\s*(?:yr|year|ann)?",
    re.IGNORECASE,
)

# City, state, zip: "Austin, TX 78701"
_CITY_STATE_ZIP_RE = re.compile(
    r"([A-Za-z\s\-'.]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)"
)

# Year built
_YEAR_BUILT_RE = re.compile(
    r"(?:year\s+built|built\s+in|year\s+of\s+construction)[:\s]*(\d{4})",
    re.IGNORECASE,
)

# Floors
_FLOORS_RE = re.compile(r"(\d+)\s*(?:floors?|stories|stor(?:ey|ies))", re.IGNORECASE)

# Parking ratio
_PARKING_RATIO_RE = re.compile(
    r"parking\s+ratio[:\s]+([\d.]+)\s*(?:spaces?\s+per\s+1[,.]?000\s*SF|/\s*1[,.]?000)?",
    re.IGNORECASE,
)

# NOI
_NOI_RE = re.compile(
    r"noi[:\s]+\$?([\d,]+(?:\.\d+)?)\s*([Mm]illion|[Mm])?",
    re.IGNORECASE,
)

# Occupancy
_OCCUPANCY_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*%\s*(?:occupied|occupancy|leased)",
    re.IGNORECASE,
)

# Lot size (industrial)
_LOT_SIZE_RE = re.compile(
    r"lot\s+(?:size|area)[:\s]+([\d,]+(?:\.\d+)?)\s*(SF|sq\.?\s*ft\.?|acres?)",
    re.IGNORECASE,
)

# Email and phone (contact extraction)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.+-]+\.\w+")
_PHONE_RE = re.compile(r"\+?1?[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}")

# Lease type keywords (ordered most-to-least specific)
_LEASE_TYPE_PATTERNS = [
    (re.compile(r"\bfull\s+service\b", re.IGNORECASE), "full_service"),
    (re.compile(r"\bmodified\s+gross\b|\bmg\b", re.IGNORECASE), "modified_gross"),
    (re.compile(r"\bnnn\b|\btriple\s+net\b", re.IGNORECASE), "nnn"),
    (re.compile(r"\bgross\b", re.IGNORECASE), "gross"),
]

# Colliers category search URLs surfacing more listings per type
CATEGORY_URLS = [
    "https://www.colliers.com/en/properties?types=industrial",
    "https://www.colliers.com/en/properties?types=office",
    "https://www.colliers.com/en/properties?types=retail",
    "https://www.colliers.com/en/properties?types=multifamily",
    "https://www.colliers.com/en/properties?types=land",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_external_id(url: str) -> Optional[str]:
    m = _COLLIERS_ID_RE.search(url)
    return m.group(1).lower() if m else None


def _parse_lease_type(text: str) -> Optional[str]:
    for pattern, lease_type in _LEASE_TYPE_PATTERNS:
        if pattern.search(text):
            return lease_type
    return None


def _parse_noi(text: str) -> Optional[float]:
    m = _NOI_RE.search(text)
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    suffix = (m.group(2) or "").lower()
    if suffix in ("million", "m"):
        val *= 1_000_000
    return val


def _parse_occupancy(text: str) -> Optional[float]:
    m = _OCCUPANCY_RE.search(text)
    return round(float(m.group(1)) / 100, 6) if m else None


def _extract_contacts(lines: list[str]) -> list[dict]:
    """
    Walk markdown lines looking for broker contact blocks.
    Returns a list of {name, email, phone, brokerage_name, is_primary} dicts.
    """
    contacts: list[dict] = []
    current: dict = {}
    in_contact = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current and current.get("name"):
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
            # Name candidate: short, starts with capital, no leading digits
            if (not em and not ph and stripped and stripped[0].isupper()
                    and len(stripped) < 60 and not any(c.isdigit() for c in stripped[:2])):
                current.setdefault("name", stripped)

    if current and current.get("name"):
        contacts.append(current)

    for i, c in enumerate(contacts):
        c["brokerage_name"] = "Colliers"
        c["is_primary"] = i == 0

    return contacts[:3]


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class ColliersScraper(BaseScraper):
    """
    Scraper for Colliers International (colliers.com/en/properties).

    Colliers is the easiest target in the brokerage set. No Cloudflare.
    Listing IDs are stable 6-8 digit numbers appended to detail-page URLs
    as 'usa<id>' -- extract to populate external_id for Supabase dedup.

    Emphasis: investment properties (multifamily, industrial, NNN retail).
    Cap rate, NOI, lease rate (NNN), and size are primary value fields.

    Discovery uses the Coveo-powered /en/properties page; raise waitFor to
    7000 if render is slow on first run.
    """

    BROKER_SLUG = "colliers"
    SEARCH_URL = "https://www.colliers.com/en/properties"
    FIRECRAWL_OPTIONS = {
        "proxy": "stealth",
        "waitFor": 5000,
        "timeout": 60000,
        "onlyMainContent": False,
    }

    def discover_listings(self, search_url: Optional[str] = None) -> list[str]:
        """
        Scrape the Colliers properties search page and return detail-page URLs.

        Primary filter: links containing the usa<6-8-digit-id> pattern.
        Fallback: any colliers.com/en/properties/ sub-path that is not a bare
        category index.

        Returns a deduplicated list of absolute URL strings.
        """
        target = search_url or self.SEARCH_URL
        result = self.scrape_url(target, options={"formats": ["links", "markdown"]})
        if not result.get("success"):
            return []

        all_links = self._extract_links(result)

        # Primary: detail pages with embedded usa<id>
        detail_urls = [
            lnk for lnk in all_links
            if _COLLIERS_ID_RE.search(lnk) and "colliers.com" in lnk
        ]

        # Fallback: any /en/properties/ sub-path that is not a category root
        if not detail_urls:
            detail_urls = [
                lnk for lnk in all_links
                if "colliers.com/en/properties/" in lnk
                and not lnk.rstrip("/").endswith("/en/properties")
            ]

        return self._dedup(detail_urls)

    def parse_listing(self, url: str, scraped_dict: dict) -> Optional[ListingData]:
        """
        Parse a Colliers property detail page markdown into a ListingData instance.

        Extracts:
          - external_id from the usa<id> in the URL
          - transaction_type from "For Sale" / "For Lease" language
          - property_type from keyword matching
          - size_sf, lot_size_sf (industrial sites)
          - sale_price_usd or lease_rate_min/max + lease_rate_type
          - cap_rate, noi, occupancy_rate (investment listings)
          - year_built, floors, parking_ratio
          - city, state, zip from "City, ST ZIPCODE" pattern
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

        # External ID from URL
        listing.external_id = _extract_external_id(url)

        # Title from meta (strip "| Colliers" suffix)
        raw_title = metadata.get("title") or metadata.get("ogTitle") or ""
        listing.title = re.sub(r"\s*\|\s*Colliers.*$", "", raw_title).strip() or None

        # Transaction type
        listing.transaction_type = extract_transaction_type(markdown)

        # Property type
        listing.property_type = extract_property_type(markdown)

        # Location
        cz = _CITY_STATE_ZIP_RE.search(markdown)
        if cz:
            listing.city = cz.group(1).strip().title()
            listing.state = normalize_state(cz.group(2))
            listing.zip = cz.group(3)

        # Size
        listing.size_sf = normalize_sqft(markdown[:2000])  # scan early content

        # Industrial lot SF
        lot_m = _LOT_SIZE_RE.search(markdown)
        if lot_m:
            lot_val = float(lot_m.group(1).replace(",", ""))
            if "acre" in lot_m.group(2).lower():
                lot_val *= 43560.0  # acres -> SF
            listing.lot_size_sf = lot_val

        # Building details
        yr_m = _YEAR_BUILT_RE.search(markdown)
        if yr_m:
            listing.year_built = int(yr_m.group(1))

        fl_m = _FLOORS_RE.search(markdown)
        if fl_m:
            listing.floors = int(fl_m.group(1))

        pr_m = _PARKING_RATIO_RE.search(markdown)
        if pr_m:
            listing.parking_ratio = float(pr_m.group(1))

        # Price / rate
        if listing.transaction_type in ("sale", "sale_or_lease"):
            # Look for labeled price first; fall back to first dollar amount
            price_m = re.search(
                r"(?:sale\s+price|asking\s+price|list\s+price)[:\s]+\$?([\d,]+(?:\.\d+)?)\s*([Mm]illion|[Mm])?",
                markdown,
                re.IGNORECASE,
            )
            if price_m:
                val = float(price_m.group(1).replace(",", ""))
                if (price_m.group(2) or "").lower() in ("million", "m"):
                    val *= 1_000_000
                listing.sale_price_usd = val
            else:
                listing.sale_price_usd = normalize_price(markdown[:500])

        if listing.transaction_type in ("lease", "sale_or_lease"):
            lr_m = _LEASE_RATE_RE.search(markdown)
            if lr_m:
                rate = float(lr_m.group(1).replace(",", ""))
                listing.lease_rate_min = rate
                listing.lease_rate_max = rate
            listing.lease_rate_type = _parse_lease_type(markdown)

        # Investment metrics
        cap_m = re.search(
            r"cap\s*rate[:\s]+(\d+\.?\d*)\s*%",
            markdown,
            re.IGNORECASE,
        )
        if cap_m:
            listing.cap_rate = normalize_cap_rate(cap_m.group(1) + "%")

        listing.noi = _parse_noi(markdown)
        listing.occupancy_rate = _parse_occupancy(markdown)

        # Description: first substantive paragraph
        paras = [p.strip() for p in markdown.split("\n\n") if len(p.strip()) >= 80]
        if paras:
            listing.description = paras[0]

        # Highlights from bullet points
        bullets = re.findall(r"^[-*]\s+(.+)$", markdown, re.MULTILINE)
        listing.highlights = [b.strip() for b in bullets[:10] if len(b.strip()) > 5]

        # Contacts
        listing.contacts = _extract_contacts(markdown.split("\n"))

        # Document and image URLs
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
            "source": "colliers",
            "meta_title": metadata.get("title"),
            "meta_description": metadata.get("description"),
        }

        return listing
