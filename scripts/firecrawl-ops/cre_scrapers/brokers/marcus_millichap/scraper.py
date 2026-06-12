#!/usr/bin/env python3
"""
marcus_millichap.py -- Marcus & Millichap CRE listing scraper for EQUIRE.

Marcus & Millichap (M&M) is THE specialist in investment-property sales:
multifamily, net-lease retail, self-storage, office, and hospitality. Nearly
every listing surfaces an offering price and most include cap rate, NOI, GRM,
price/unit, and occupancy. This makes it among the highest-value targets for
EQUIRE's investment underwriting pipeline.

Accessibility note (config.py)
-------------------------------
M&M's search page has historically timed out even at 90s with stealth. The
scraper is marked active=False in config.py. Use timeout=120000 and waitFor=10000
as a first attempt; if still timing out, the detail pages (individual listing
URLs obtained via sitemap or third-party source) may be more accessible than the
search page.

Discovery strategy
------------------
Scrape /properties with stealth + waitFor=10000. Filter returned links for the
/properties/<listing-id> pattern (typically a short numeric or slug ID). If the
search page is blocked, the scraper logs a warning and returns an empty list
rather than raising -- callers can feed detail URLs directly.

Parse strategy (investment-focused)
-------------------------------------
M&M listing pages typically include structured tables or sections with:
  - PRICE (or ASKING PRICE)
  - CAP RATE (going-in)
  - NOI (trailing-12 or pro-forma)
  - NUMBER OF UNITS (multifamily)
  - GRM (gross rent multiplier)
  - PRICE/UNIT
  - OCCUPANCY RATE
  - YEAR BUILT / TYPE
  - LOCATION details

The parser scans markdown for these labeled fields using targeted regex. Auction
listings are flagged in raw_data["is_auction"] = True.

Config alignment: config.BROKERS["marcus-millichap"]
  proxy=stealth, wait_for_ms=10000, timeout_ms=120000, active=False (pending fix)
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
# Patterns -- M&M uses structured label: value blocks in its markdown
# ---------------------------------------------------------------------------

# Price labels M&M uses
_PRICE_LABELS = (
    "price", "asking price", "list price", "sale price",
    "offering price", "current price", "minimum bid",
)

# Investment metric patterns
_CAP_RATE_RE = re.compile(r"cap\s*rate[:\s]+(\d+\.?\d*)\s*%", re.IGNORECASE)
_NOI_RE = re.compile(
    r"(?:noi|net\s+operating\s+income)[:\s]+\$?([\d,]+(?:\.\d+)?)\s*([Mm](?:illion)?)?",
    re.IGNORECASE,
)
_GRM_RE = re.compile(r"grm[:\s]+([\d.]+)", re.IGNORECASE)
_PRICE_UNIT_RE = re.compile(r"price\s*/\s*unit[:\s]+\$?([\d,]+)", re.IGNORECASE)
_PRICE_SF_RE = re.compile(r"price\s*/\s*(?:sf|sq\.?\s*ft\.?)[:\s]+\$?([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_UNITS_RE = re.compile(r"(?:number\s+of\s+)?(?:total\s+)?units?[:\s]+(\d+)", re.IGNORECASE)
_OCCUPANCY_RE = re.compile(
    r"(?:occupancy|occupied|leased)[:\s]+(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
_YEAR_BUILT_RE = re.compile(r"(?:year\s+built|built)[:\s]*(\d{4})", re.IGNORECASE)
_SIZE_RE = re.compile(
    r"(?:total\s+)?(?:building\s+)?(?:size|area|sf|sq\.?\s*ft\.?)[:\s]+([\d,]+(?:\.\d+)?)\s*(?:SF|sq\.?\s*ft\.?)?",
    re.IGNORECASE,
)
_FLOORS_RE = re.compile(r"(?:stories|floors?)[:\s]+(\d+)", re.IGNORECASE)
_CITY_STATE_ZIP_RE = re.compile(
    r"([A-Za-z\s\-'.]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)"
)

# Auction keywords
_AUCTION_RE = re.compile(r"\bauction\b|\bbid\b|\bminimum\s+bid\b", re.IGNORECASE)

# Detail URL: /properties/<id-or-slug> with no extra path segments
_DETAIL_URL_RE = re.compile(
    r"marcusmillichap\.com/properties/[^/\s?#]+$",
    re.IGNORECASE,
)

# Email and phone
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.+-]+\.\w+")
_PHONE_RE = re.compile(r"\+?1?[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_noi(text: str) -> Optional[float]:
    m = _NOI_RE.search(text)
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    suffix = (m.group(2) or "").lower()
    if suffix.startswith("m"):
        val *= 1_000_000
    return val


def _parse_occupancy(text: str) -> Optional[float]:
    m = _OCCUPANCY_RE.search(text)
    return round(float(m.group(1)) / 100, 6) if m else None


def _parse_labeled_price(text: str, label: str) -> Optional[float]:
    """Extract a dollar amount immediately following `label:` in text."""
    pattern = re.compile(
        re.escape(label) + r"[:\s]+\$?([\d,]+(?:\.\d+)?)\s*([Mm](?:illion)?)?",
        re.IGNORECASE,
    )
    m = pattern.search(text)
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    if (m.group(2) or "").lower().startswith("m"):
        val *= 1_000_000
    return val


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
        if any(kw in lower for kw in ("contact", "broker", "agent", "investment", "associate")):
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
        c.setdefault("brokerage_name", "Marcus & Millichap")
        c["is_primary"] = i == 0

    return contacts[:4]


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class MarcusMillichapScraper(BaseScraper):
    """
    Scraper for Marcus & Millichap (marcusmillichap.com/properties).

    M&M specializes in investment-property sales across all asset classes.
    Key financial fields: cap_rate, noi, price_per_unit, grm, occupancy_rate.

    Site accessibility note: the search page has been observed to SCRAPE_TIMEOUT
    even at 90s. FIRECRAWL_OPTIONS uses timeout=120000 and waitFor=10000 as a
    best-effort attempt. If the search page is blocked, feed individual listing
    URLs (obtained externally or via sitemap) directly to parse_listing() via
    batch_scrape(). The broker is marked active=False in config.py pending a
    reliable discovery path.

    Auction listings are detected from keywords ("auction", "minimum bid",
    "bid") and flagged in raw_data["is_auction"] = True.
    """

    BROKER_SLUG = "marcus-millichap"
    SEARCH_URL = "https://www.marcusmillichap.com/properties"
    FIRECRAWL_OPTIONS = {
        "proxy": "stealth",
        "waitFor": 10000,
        "timeout": 120000,
        "onlyMainContent": False,
    }

    def discover_listings(self, search_url: Optional[str] = None) -> list[str]:
        """
        Scrape the M&M properties search page and return detail-page URLs.

        M&M's search page may time out (see class docstring). If the scrape
        fails, logs a warning and returns an empty list rather than raising.

        Filter: /properties/<segment> URLs where <segment> contains no
        further slashes (i.e. actual detail pages, not category pages).
        """
        target = search_url or self.SEARCH_URL
        result = self.scrape_url(target, options={"formats": ["links", "markdown"]})
        if not result.get("success"):
            print(
                f"[{self.BROKER_SLUG}] discover failed (site may require manual URL seeding): "
                f"{result.get('error', '?')}",
                flush=True,
            )
            return []

        all_links = self._extract_links(result)
        detail_urls = [
            lnk for lnk in all_links
            if _DETAIL_URL_RE.search(lnk)
        ]

        return self._dedup(detail_urls)

    def parse_listing(self, url: str, scraped_dict: dict) -> Optional[ListingData]:
        """
        Parse a Marcus & Millichap property detail page into a ListingData.

        Investment-focused extraction:
          - sale_price_usd from PRICE / ASKING PRICE / OFFERING PRICE labels
          - cap_rate from CAP RATE label (converted to [0,1] fraction)
          - noi from NOI / NET OPERATING INCOME label
          - units from UNITS / NUMBER OF UNITS label
          - grm and price_per_unit stored in raw_data
          - occupancy_rate from OCCUPANCY label
          - is_auction flag in raw_data when auction language detected

        M&M listings are almost exclusively for-sale investment properties;
        transaction_type defaults to "sale" if not otherwise determinable.
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

        # External ID: last path segment of the URL
        path = url.rstrip("/").split("?")[0]
        tail = path.split("/")[-1]
        if tail and tail != "properties":
            listing.external_id = tail

        # Title
        raw_title = metadata.get("title") or metadata.get("ogTitle") or ""
        listing.title = re.sub(
            r"\s*[-|]\s*Marcus\s*&?\s*Millichap.*$", "", raw_title, flags=re.IGNORECASE
        ).strip() or None

        # Transaction type -- M&M is almost exclusively investment sales
        listing.transaction_type = extract_transaction_type(markdown) or "sale"

        # Property type
        listing.property_type = extract_property_type(markdown)

        # Location
        cz = _CITY_STATE_ZIP_RE.search(markdown)
        if cz:
            listing.city = cz.group(1).strip().title()
            listing.state = normalize_state(cz.group(2))
            listing.zip = cz.group(3)

        # Size
        sz_m = _SIZE_RE.search(markdown)
        if sz_m:
            listing.size_sf = normalize_sqft(sz_m.group(1) + " SF")

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

        # Price: try each label in decreasing specificity
        for label in _PRICE_LABELS:
            price = _parse_labeled_price(markdown, label)
            if price:
                listing.sale_price_usd = price
                break

        # Price per SF
        psf_m = _PRICE_SF_RE.search(markdown)
        if psf_m:
            listing.sale_price_per_sf = float(psf_m.group(1).replace(",", ""))

        # Cap rate
        cap_m = _CAP_RATE_RE.search(markdown)
        if cap_m:
            listing.cap_rate = normalize_cap_rate(cap_m.group(1) + "%")

        # NOI
        listing.noi = _parse_noi(markdown)

        # Occupancy
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
            if lnk.lower().endswith(".pdf") or "brochure" in lnk.lower() or "/om/" in lnk.lower()
        ]
        listing.images = [
            {"url": lnk, "is_primary": i == 0}
            for i, lnk in enumerate(
                lnk for lnk in links
                if any(lnk.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp"))
            )
        ]

        # Auction flag
        is_auction = bool(_AUCTION_RE.search(markdown))

        # GRM and price/unit (store in raw_data; no dedicated column)
        grm_m = _GRM_RE.search(markdown)
        price_unit_m = _PRICE_UNIT_RE.search(markdown)

        listing.raw_data = {
            "source": "marcus-millichap",
            "is_auction": is_auction,
            "grm": float(grm_m.group(1)) if grm_m else None,
            "price_per_unit": float(price_unit_m.group(1).replace(",", "")) if price_unit_m else None,
            "meta_title": metadata.get("title"),
            "meta_description": metadata.get("description"),
        }

        return listing
