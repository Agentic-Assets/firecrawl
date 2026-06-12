#!/usr/bin/env python3
"""
cushman.py -- Cushman & Wakefield commercial real estate scraper.

Cushman & Wakefield (C&W) is a global commercial real estate services firm.
This scraper targets the US investment-sale listings portal at:
  https://www.cushmanwakefield.com/en/united-states/properties/invest/search

Site characteristics (verified 2026-06-11 against config.py):
  - Coveo faceted search engine. No Cloudflare challenge; geo-redirect only --
    use the direct US URL to skip the redirect.
  - Search page renders full property cards including prices in the initial HTML
    after JavaScript hydration (waitFor=6000 sufficient).
  - Pagination via Coveo fragment facets: ``#first=N`` in the URL hash.
  - Detail-page URL pattern:
      /en/united-states/properties/for-sale/{type}/{state}/{city}/{slug}/{slug}-s
    where the trailing ``-s`` distinguishes detail from list pages.
  - C&W uses ``proxy=stealth`` as a general defense measure; no CF challenge
    seen but stealth avoids bot fingerprinting on the Coveo layer.

Key Cushman & Wakefield fields:
  - **Market / submarket**: C&W prominently classifies properties by CBRE/JLL
    equivalent market taxonomy (e.g., "Houston Suburbs", "Atlanta Midtown").
  - **Asset class**: Explicit asset-class label (office, industrial, retail,
    multifamily) on every listing page.
  - **NOI / cap rate**: Investment sales pages typically show going-in cap rate
    and trailing-12 NOI for marketed deals.
  - **Broker team**: C&W often names both the originating broker and the
    capital markets deal team (different from leasing contacts).
  - **Listing ID**: Appears as a numeric suffix in the detail URL slug or in a
    dedicated "Property ID" field in the page metadata.

Discovery strategy:
  Scrape the Coveo invest/search page for ``links`` and filter for detail URLs
  matching the ``/properties/for-sale/`` or ``/properties/for-lease/`` path
  prefix with a trailing ``-s`` slug.  Coveo loads up to ~20 cards per page.
  For large markets, use the ``#first=N`` hash parameter to page through
  additional results (not implemented in the initial discovery pass -- the
  first page yields enough seed URLs for a targeted test run).

Usage:
    python3 -c "
    from cre_scrapers.cushman import CushmanScraper
    from pathlib import Path
    s = CushmanScraper()
    result = s.run(max_listings=10, output_dir=Path('./out/cushman'))
    print(result['parsed'], 'listings parsed')
    "
"""

from __future__ import annotations

import re
from typing import Optional

from ...base import BaseScraper
from ...normalizer import (
    ListingData,
    normalize_price,
    normalize_sqft,
    normalize_cap_rate,
    normalize_state,
    extract_property_type,
    extract_transaction_type,
    clean_phone,
)

# ---------------------------------------------------------------------------
# Cushman & Wakefield search pages
# ---------------------------------------------------------------------------

# Primary US investment-sale search (Coveo-powered, no geo-redirect needed)
_INVEST_SEARCH_URL = (
    "https://www.cushmanwakefield.com/en/united-states/properties/invest/search"
)

# Additional search pages for leasing (office, industrial)
_LEASE_SEARCH_URLS: list[str] = [
    "https://www.cushmanwakefield.com/en/united-states/properties/office-space-for-lease",
    "https://www.cushmanwakefield.com/en/united-states/properties/industrial-for-lease",
]

# All default discovery pages (sale first, then lease)
SEARCH_PAGES: list[str] = [_INVEST_SEARCH_URL] + _LEASE_SEARCH_URLS

_BASE_URL = "https://www.cushmanwakefield.com"

# C&W detail URL must contain one of these path segments
_DETAIL_PATH_MARKERS = (
    "/properties/for-sale/",
    "/properties/for-lease/",
    "/properties/invest/",
    # Occasionally C&W uses /properties/ + property-type
    "/properties/office/",
    "/properties/industrial/",
    "/properties/retail/",
    "/properties/multifamily/",
    "/properties/land/",
)

# ---------------------------------------------------------------------------
# Regex patterns for parsing C&W markdown
# ---------------------------------------------------------------------------

_PROP_ID_RE = re.compile(
    r"(?:Property\s+ID|Listing\s+ID|Reference\s+(?:ID|#))[:\s#]*([A-Z0-9\-]{3,20})",
    re.IGNORECASE,
)
# C&W detail URLs end in a numeric slug segment (the -s suffix)
_URL_ID_RE = re.compile(r"/([A-Za-z0-9\-]+(?:-s)?)/?$")

_SIZE_RE = re.compile(r"([\d,]+)\s*(?:SF|sq\.?\s*ft\.?)", re.IGNORECASE)
_AVAIL_SF_RE = re.compile(
    r"(?:Available|Available\s+Space|For\s+Lease)[:\s]+([\d,]+)\s*(?:SF|sq\.?\s*ft\.?)",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(r"\$([\d,]+(?:\.\d+)?)\s*([KkMmBb])?")
_PRICE_PSF_RE = re.compile(r"\$([\d,.]+)\s*/\s*(?:SF|PSF)", re.IGNORECASE)
_CAP_RATE_RE = re.compile(r"cap\s+rate[:\s]+([\d.]+)\s*%", re.IGNORECASE)
_NOI_RE = re.compile(r"NOI[:\s]+\$?([\d,]+(?:\.\d+)?)\s*([KkMmBb])?", re.IGNORECASE)
_OCCUPANCY_RE = re.compile(r"([\d.]+)\s*%\s*(?:occupi|leased|occupied)", re.IGNORECASE)
_YEAR_BUILT_RE = re.compile(r"(?:Year\s+Built|Built|Constructed)[:\s]+((?:19|20)\d{2})", re.IGNORECASE)
_FLOORS_RE = re.compile(r"(\d+)\s*(?:Stor(?:y|ies)|Floors?|Levels?)", re.IGNORECASE)
_MARKET_RE = re.compile(r"(?:Market|Submarket)[:\s]+([^\n]+)", re.IGNORECASE)
_ZONING_RE = re.compile(r"(?:Zoning|Zone)[:\s]+([A-Z0-9\-]{1,20})", re.IGNORECASE)
_PHONE_RE = re.compile(r"(\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4})")
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_CITY_STATE_ZIP_RE = re.compile(
    r"([A-Za-z\s.'\-]+),\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)"
)
_LEASE_RATE_RE = re.compile(
    r"\$([\d,.]+)\s*/\s*(?:SF|PSF)\s*/?\s*(?:YR|Year|Yr)?", re.IGNORECASE
)


class CushmanScraper(BaseScraper):
    """Scraper for Cushman & Wakefield's US property portal.

    Focuses on US investment-sale listings (going-in cap rate, NOI, broker
    team) and for-lease office/industrial properties.  C&W is a global firm;
    all discovery pages are pre-filtered to the /en/united-states/ subtree.
    """

    BROKER_SLUG = "cushman-wakefield"
    SEARCH_URL = _INVEST_SEARCH_URL
    FIRECRAWL_OPTIONS = {
        "proxy": "stealth",
        "waitFor": 6000,    # Coveo search hydration; no Cloudflare challenge
        "timeout": 60000,
    }

    # ---------------------------------------------------------------------------
    # Discovery
    # ---------------------------------------------------------------------------

    def discover_listings(self, search_url: Optional[str] = None) -> list[str]:
        """Scrape Cushman & Wakefield search pages and return detail-page URLs.

        Uses the Coveo invest/search page plus lease search pages as seeds.
        Filters extracted links to those whose path contains a C&W detail-page
        marker (``/properties/for-sale/`` etc.) and ends with a ``-s`` slug
        or otherwise matches the detail-URL pattern.

        Returns a deduplicated list of absolute URLs.
        """
        pages_to_scrape = [search_url] if search_url else SEARCH_PAGES
        detail_urls: list[str] = []

        for page_url in pages_to_scrape:
            print(f"[cushman-wakefield] discovering from: {page_url}")
            result = self.scrape_url(
                page_url,
                options={
                    "formats": ["links"],
                    "onlyMainContent": False,
                    "waitFor": 8000,   # Coveo may need extra time to render cards
                },
            )
            if not result.get("success"):
                print(
                    f"[cushman-wakefield] discovery failed for {page_url}: "
                    f"{result.get('error', result)}"
                )
                continue

            links = self._extract_links(result)
            for href in links:
                url = _make_absolute(href)
                if url and _is_detail_url(url):
                    detail_urls.append(url)

        unique = self._dedup(detail_urls)
        print(
            f"[cushman-wakefield] discovered {len(unique)} unique listing URLs "
            f"across {len(pages_to_scrape)} search pages"
        )
        return unique

    # ---------------------------------------------------------------------------
    # Parsing
    # ---------------------------------------------------------------------------

    def parse_listing(self, url: str, scraped_dict: dict) -> Optional[ListingData]:
        """Extract structured fields from a scraped Cushman & Wakefield detail page.

        C&W investment-sale pages typically include:
          - Property title (H1)
          - Address, city, state, ZIP
          - Market / submarket classification
          - Asset class label (office, industrial, retail, etc.)
          - Size (total and available SF)
          - Asking price (sale) or lease rate ($/SF/yr)
          - Going-in cap rate and trailing-12 NOI
          - Occupancy rate
          - Year built, floors
          - Description and key investment highlights
          - Broker team: originating broker + capital markets team
            (name, title, phone, email for each)
          - Property documents (offering memorandum, brochure)

        Returns None if the markdown is empty or lacks sufficient location data.
        """
        md = self._get_markdown(scraped_dict)
        if not md or len(md) < 100:
            return None

        listing = ListingData(
            brokerage_slug=self.BROKER_SLUG,
            source_url=url,
            markdown=md,
        )

        # --- External ID ---
        # Try the in-page field first, then fall back to URL slug
        m = _PROP_ID_RE.search(md)
        if m:
            listing.external_id = m.group(1).strip()
        else:
            m = _URL_ID_RE.search(url.rstrip("/"))
            if m:
                listing.external_id = m.group(1)

        # --- Title ---
        listing.title = _extract_title(md)

        # --- Transaction type ---
        listing.transaction_type = _infer_transaction_type(url, md)

        # --- Property type ---
        listing.property_type = extract_property_type(md) or _infer_type_from_url(url)

        # --- Location ---
        addr = _extract_address_block(md)
        if addr:
            listing.address = addr.get("address")
            listing.city = addr.get("city")
            listing.state = addr.get("state")
            listing.zip = addr.get("zip")

        # --- Market / submarket ---
        m = _MARKET_RE.search(md[:3000])
        if m:
            market_text = m.group(1).strip()
            # C&W sometimes has "Market: Dallas / Submarket: Las Colinas"
            if "/" in market_text:
                parts = [p.strip() for p in market_text.split("/")]
                listing.market = parts[0]
                listing.submarket = parts[1] if len(parts) > 1 else None
            else:
                listing.market = market_text[:100]

        # Bail if we have neither location nor ID
        if not listing.address and not listing.external_id:
            return None

        # --- Size ---
        m = _SIZE_RE.search(md)
        if m:
            listing.size_sf = normalize_sqft(m.group(0))

        # --- Available SF (leasing) ---
        m = _AVAIL_SF_RE.search(md)
        if m:
            listing.available_sf = normalize_sqft(m.group(1) + " SF")

        # --- Asking price (sale) ---
        price_raw = _extract_price_line(md)
        if price_raw:
            listing.sale_price_usd = normalize_price(price_raw)
            # Derive price/SF if size is known
            if listing.sale_price_usd and listing.size_sf and listing.size_sf > 0:
                listing.sale_price_per_sf = listing.sale_price_usd / listing.size_sf
        # Check for explicit price/SF field
        m = _PRICE_PSF_RE.search(md)
        if m:
            try:
                listing.sale_price_per_sf = float(m.group(1).replace(",", ""))
            except ValueError:
                pass

        # --- Lease rate ---
        m = _LEASE_RATE_RE.search(md)
        if m:
            listing.lease_rate_min = normalize_price(m.group(0))

        # --- Cap rate ---
        m = _CAP_RATE_RE.search(md)
        if m:
            listing.cap_rate = normalize_cap_rate(m.group(1) + "%")

        # --- NOI ---
        m = _NOI_RE.search(md)
        if m:
            noi_raw = m.group(1).replace(",", "")
            suffix = (m.group(2) or "").upper()
            mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get(suffix, 1.0)
            try:
                listing.noi = float(noi_raw) * mult
            except ValueError:
                pass

        # --- Occupancy ---
        m = _OCCUPANCY_RE.search(md)
        if m:
            try:
                listing.occupancy_rate = float(m.group(1)) / 100.0
            except ValueError:
                pass

        # --- Year built ---
        m = _YEAR_BUILT_RE.search(md)
        if m:
            try:
                listing.year_built = int(m.group(1))
            except ValueError:
                pass

        # --- Floors ---
        m = _FLOORS_RE.search(md)
        if m:
            try:
                listing.floors = int(m.group(1))
            except ValueError:
                pass

        # --- Zoning ---
        m = _ZONING_RE.search(md)
        if m:
            listing.zoning = m.group(1).strip()

        # --- Description and highlights ---
        listing.description = _extract_description(md)
        listing.highlights = _extract_highlights(md)

        # --- Broker team contacts ---
        listing.contacts = _extract_contacts(md)

        # --- Documents (OM, brochure) ---
        links = self._extract_links(scraped_dict)
        listing.documents = _classify_documents(links)

        # --- Raw data ---
        listing.raw_data = {
            "source": "cushman-wakefield",
            "url": url,
            "markdown_length": len(md),
            "link_count": len(links),
        }

        return listing


# ---------------------------------------------------------------------------
# Cushman & Wakefield helper functions
# ---------------------------------------------------------------------------

def _make_absolute(href: str) -> str:
    """Convert a relative C&W path to an absolute URL."""
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return _BASE_URL + href
    return ""


def _is_detail_url(url: str) -> bool:
    """Return True if the URL looks like a C&W property detail page.

    Checks for known detail-path markers and excludes obvious non-listing
    pages (search, category, language, account pages).
    """
    url_lower = url.lower()
    if not any(marker in url_lower for marker in _DETAIL_PATH_MARKERS):
        return False
    # Exclude the search/category pages themselves
    exclude = ("/search", "/invest/search", "/for-lease/search", "/for-sale/search")
    if any(url_lower.endswith(e) or (e + "?") in url_lower for e in exclude):
        return False
    return True


def _infer_transaction_type(url: str, md: str) -> Optional[str]:
    """Infer transaction type from URL path and markdown."""
    url_lower = url.lower()
    if "for-sale" in url_lower or "/invest/" in url_lower:
        return "sale"
    if "for-lease" in url_lower or "for-rent" in url_lower:
        return "lease"
    return extract_transaction_type(md[:500])


def _infer_type_from_url(url: str) -> Optional[str]:
    """Derive property type from C&W URL path segments."""
    url_lower = url.lower()
    for keyword, ptype in [
        ("multifamily", "multifamily"),
        ("multi-family", "multifamily"),
        ("industrial", "industrial"),
        ("office", "office"),
        ("retail", "retail"),
        ("flex", "flex"),
        ("land", "land"),
        ("hotel", "hospitality"),
        ("self-storage", "self_storage"),
    ]:
        if keyword in url_lower:
            return ptype
    return None


def _extract_title(md: str) -> Optional[str]:
    """Return the first meaningful heading or first line."""
    for line in md.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped and len(stripped) > 4 and not stripped.startswith("!["):
            return stripped[:200]
    return None


def _extract_address_block(md: str) -> dict:
    """Parse street address, city, state, and ZIP from C&W markdown.

    C&W typically renders the address near the top as:
        "1234 Commerce Drive\\nDallas, TX 75201"
    or within a structured table.
    """
    result: dict = {}

    m = _CITY_STATE_ZIP_RE.search(md[:2000])
    if m:
        result["city"] = m.group(1).strip()
        result["state"] = normalize_state(m.group(2))
        result["zip"] = m.group(3)

        text_before = md[: m.start()].strip()
        lines_before = [l.strip() for l in text_before.splitlines() if l.strip()]
        if lines_before:
            candidate = lines_before[-1].lstrip("#").strip()
            if re.search(r"\d", candidate) and len(candidate) < 120:
                result["address"] = candidate

    return result


def _extract_price_line(md: str) -> Optional[str]:
    """Return the first line containing a dollar amount, excluding withheld prices."""
    skip_re = re.compile(
        r"contact|upon\s+request|negotiable|pricing\s+available|call\s+for",
        re.IGNORECASE,
    )
    for line in md.splitlines()[:100]:
        if skip_re.search(line):
            return None
        m = _PRICE_RE.search(line)
        if m:
            return line.strip()
    return None


def _extract_description(md: str) -> Optional[str]:
    """Return the largest prose block from the markdown (up to 2000 chars)."""
    sections = re.split(r"\n#{1,3}\s+", md)
    best: Optional[str] = None
    best_len = 0
    for section in sections:
        for para in section.split("\n\n"):
            para = para.strip()
            if len(para) > best_len and not para.startswith("#") and not para.startswith("!"):
                best_len = len(para)
                best = para
    return best[:2000] if best else None


def _extract_highlights(md: str) -> list[str]:
    """Extract bullet-point investment highlights from C&W markdown.

    C&W investment-sale pages commonly have a "Key Highlights" or
    "Investment Highlights" section with a bulleted list of deal points.
    """
    highlights: list[str] = []
    in_section = False

    header_re = re.compile(
        r"(?:Investment\s+Highlights?|Key\s+Highlights?|Property\s+Highlights?|"
        r"Highlights?|Key\s+Features?)\s*$",
        re.IGNORECASE,
    )
    bullet_re = re.compile(r"^[\*\-•]\s+(.+)")

    for line in md.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if header_re.match(stripped):
            in_section = True
            continue
        if in_section:
            if stripped.startswith("#"):
                in_section = False
                continue
            bm = bullet_re.match(stripped)
            if bm:
                highlights.append(bm.group(1).strip())

    return highlights[:20]


def _extract_contacts(md: str) -> list[dict]:
    """Parse broker/deal-team contact information from C&W markdown.

    C&W pages often list two groups:
      1. Capital markets / investment sales team (for investment properties)
      2. Leasing broker contacts (for leasing listings)

    Extracts names, titles, phone numbers, and email addresses.
    """
    contacts: list[dict] = []

    # Find the agent/broker section
    contact_section_re = re.compile(
        r"(?:Contact|Broker(?:s)?|Deal\s+Team|Capital\s+Markets\s+Team|"
        r"Investment\s+(?:Sales\s+)?Team|Advisors?)[:\s]*\n+(.*?)(?=\n#{1,3}|\Z)",
        re.DOTALL | re.IGNORECASE,
    )
    m = contact_section_re.search(md)
    block = m.group(1) if m else md

    emails = _EMAIL_RE.findall(block)
    phones_raw = _PHONE_RE.findall(block)
    phones = [clean_phone(p) for p in phones_raw]

    # Heuristic name detection: capitalized "First Last" lines
    name_re = re.compile(r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s*$")
    # C&W pages may also include a title on the next line
    title_re = re.compile(
        r"^(Executive\s+Director|Director|Managing\s+Director|Senior\s+Director|"
        r"Vice\s+President|SVP|EVP|Associate|Analyst|Capital\s+Markets|"
        r"Broker|Senior\s+Associate|Principal|Partner)\b",
        re.IGNORECASE,
    )

    lines = block.splitlines()
    names: list[str] = []
    titles: list[Optional[str]] = []

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if name_re.match(line) and len(line.split()) <= 5:
            name = line
            title = None
            # Peek ahead for a title
            if i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if title_re.match(next_line):
                    title = next_line
                    i += 1
            names.append(name)
            titles.append(title)
        i += 1

    if names:
        for idx, name in enumerate(names[:5]):
            contacts.append({
                "name": name,
                "title": titles[idx] if idx < len(titles) else None,
                "email": emails[idx] if idx < len(emails) else None,
                "phone": phones[idx] if idx < len(phones) else None,
                "brokerage_name": "Cushman & Wakefield",
                "is_primary": idx == 0,
            })
    elif emails or phones:
        contacts.append({
            "name": None,
            "title": None,
            "email": emails[0] if emails else None,
            "phone": phones[0] if phones else None,
            "brokerage_name": "Cushman & Wakefield",
            "is_primary": True,
        })

    return contacts


def _classify_documents(links: list[str]) -> list[dict]:
    """Classify C&W PDF links as OM, brochure, or floor plan.

    C&W investment sales typically include an Offering Memorandum (OM) link
    and sometimes a property brochure or executive summary.
    """
    docs: list[dict] = []
    for url in links:
        url_lower = url.lower()
        if ".pdf" not in url_lower and "pdf" not in url_lower:
            continue
        doc_type = "brochure"
        if any(kw in url_lower for kw in ("offering", "memorandum", "/om", "-om", "_om")):
            doc_type = "om"
        elif any(kw in url_lower for kw in ("floor", "floorplan", "floor-plan")):
            doc_type = "floor_plan"
        elif any(kw in url_lower for kw in ("executive", "summary", "exec")):
            doc_type = "executive_summary"
        docs.append({"doc_type": doc_type, "title": None, "url": url})
    return docs
