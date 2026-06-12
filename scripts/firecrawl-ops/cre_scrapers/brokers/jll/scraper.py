#!/usr/bin/env python3
"""
jll.py -- JLL commercial real estate scraper.

JLL (Jones Lang LaSalle) is one of the largest US commercial real estate
services firms, covering office, industrial, retail, multifamily, and
mixed-use properties across all major US markets.  Their public property
portal lives at ``property.jll.com``.

Site characteristics (verified 2026-06-11 against config.py):
  - Own Next.js SPA, no Cloudflare.  ``proxy=stealth`` still recommended
    as general defense against bot detection.
  - Search pages show ~20 property cards per load, client-side paginated.
  - Category/search URLs carry query params: /sale-office, /rent-industrial,
    /sale-multifamily, etc.
  - Detail-page URL pattern:
      https://property.jll.com/listings/{slug-address-market}
  - Listing ID (JLL internal): appears in page as "Listing ID: XXXXXX"
    and sometimes in the URL path after the slug.
  - Broker name, phone, and email appear in the markdown under "Connect with
    our team" or "Broker" sections.
  - Asking price shown for investment sales; lease rates shown for for-lease
    listings.  Both may read "Contact Broker" for off-market/withheld pricing.

Discovery strategy:
  Scrape the category search pages (/sale-office, /sale-industrial, etc.)
  for ``links`` and filter to paths starting with /listings/.  Each page
  yields ~20 cards.  Extend search_pages in SEARCH_PAGES to cover more
  asset classes.

Usage:
    python3 -c "
    from cre_scrapers.jll import JLLScraper
    from pathlib import Path
    s = JLLScraper()
    result = s.run(max_listings=10, output_dir=Path('./out/jll'))
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
# JLL search pages for discovery (asset class coverage)
# ---------------------------------------------------------------------------

# Discovery covers for-sale and for-lease across the four major asset classes
# JLL exposes on property.jll.com.  Add more paths as needed.
SEARCH_PAGES: list[str] = [
    "https://property.jll.com/sale-office",
    "https://property.jll.com/sale-industrial",
    "https://property.jll.com/sale-retail",
    "https://property.jll.com/sale-multifamily",
    "https://property.jll.com/rent-office",
    "https://property.jll.com/rent-industrial",
    "https://property.jll.com/rent-retail",
]

# Base URL for constructing absolute URLs from relative paths
_BASE_URL = "https://property.jll.com"

# Regex patterns for JLL markdown parsing
_LISTING_ID_RE = re.compile(r"(?:Listing\s+ID|Property\s+ID)[:\s#]+([A-Z0-9-]{4,20})", re.IGNORECASE)
_SIZE_RE = re.compile(r"([\d,]+)\s*(?:SF|sq\.?\s*ft\.?)", re.IGNORECASE)
_PRICE_INLINE_RE = re.compile(r"\$([\d,]+(?:\.\d+)?)\s*(?:Million|M\b)?", re.IGNORECASE)
_LEASE_RATE_RE = re.compile(
    r"\$([\d,.]+)\s*(?:/\s*SF\s*/\s*(?:YR|Year|Yr)|\s*PSF\s*/\s*YR)", re.IGNORECASE
)
_CAP_RATE_LINE_RE = re.compile(r"cap\s+rate[:\s]+([\d.]+)\s*%", re.IGNORECASE)
_NOI_RE = re.compile(r"NOI[:\s]+\$?([\d,]+(?:\.\d+)?)\s*([KkMmBb])?", re.IGNORECASE)
_OCCUPANCY_RE = re.compile(r"([\d.]+)\s*%\s*(?:occupi|leased|occupied)", re.IGNORECASE)
_YEAR_BUILT_RE = re.compile(r"(?:Year\s+Built|Built)[:\s]+((?:19|20)\d{2})", re.IGNORECASE)
_FLOORS_RE = re.compile(r"([\d]+)\s+(?:Stor(?:y|ies)|Floors?)", re.IGNORECASE)
_PHONE_RE = re.compile(r"(\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4})")
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_ZIP_RE = re.compile(r"\b(\d{5})\b")
_AGENT_BLOCK_RE = re.compile(
    r"(?:Connect with our team|Broker|Agent|Contact)[:\s]*\n+(.*?)(?=\n#{1,3}|\Z)",
    re.DOTALL | re.IGNORECASE,
)
_ADDRESS_SPLIT_RE = re.compile(r",\s*")


class JLLScraper(BaseScraper):
    """Scraper for JLL's property portal (property.jll.com).

    Covers US commercial listings: office, industrial, retail, and
    multifamily -- both for-sale and for-lease.  Broker contact
    information (name, phone, email) is extracted from the "Connect
    with our team" section common to JLL detail pages.
    """

    BROKER_SLUG = "jll"
    SEARCH_URL = "https://property.jll.com/sale-office"
    FIRECRAWL_OPTIONS = {
        "proxy": "stealth",
        "waitFor": 5000,     # JLL SPA hydration; no Cloudflare, so 5s is sufficient
        "timeout": 60000,
    }

    # ---------------------------------------------------------------------------
    # Discovery
    # ---------------------------------------------------------------------------

    def discover_listings(self, search_url: Optional[str] = None) -> list[str]:
        """Scrape JLL search/category pages and return detail-page URLs.

        Iterates over SEARCH_PAGES (all asset classes) unless a specific
        ``search_url`` is provided.  Filters links to paths that begin with
        /listings/ -- the JLL detail-page prefix.

        Returns a deduplicated list of absolute URLs.
        """
        pages_to_scrape = [search_url] if search_url else SEARCH_PAGES
        detail_urls: list[str] = []

        for page_url in pages_to_scrape:
            print(f"[jll] discovering from: {page_url}")
            result = self.scrape_url(
                page_url,
                options={
                    "formats": ["links"],
                    "onlyMainContent": False,
                    "waitFor": 7000,   # Extra wait for card hydration on search pages
                },
            )
            if not result.get("success"):
                print(f"[jll] discovery failed for {page_url}: {result.get('error', result)}")
                continue

            links = self._extract_links(result)
            for href in links:
                url = _make_absolute(href)
                if url and _is_listing_url(url):
                    detail_urls.append(url)

        unique = self._dedup(detail_urls)
        print(f"[jll] discovered {len(unique)} unique listing URLs across {len(pages_to_scrape)} search pages")
        return unique

    # ---------------------------------------------------------------------------
    # Parsing
    # ---------------------------------------------------------------------------

    def parse_listing(self, url: str, scraped_dict: dict) -> Optional[ListingData]:
        """Extract structured fields from a scraped JLL detail page.

        JLL detail pages in markdown typically include:
          - Property title (H1 or first heading)
          - Address, city, state, ZIP
          - Listing ID line
          - Size (SF) and available space
          - Asking price or lease rate
          - Cap rate, NOI, occupancy (investment sales)
          - Property description and highlights
          - "Connect with our team" section with agent name/phone/email
          - Floor plan PDF links

        Returns None if the markdown is empty or the page looks like an
        error/redirect page (no address found and no listing ID).
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
        m = _LISTING_ID_RE.search(md)
        if m:
            listing.external_id = m.group(1).strip()

        # --- Title (first non-empty heading or first line) ---
        listing.title = _extract_title(md)

        # --- Transaction type ---
        listing.transaction_type = _infer_transaction_type(url, md)

        # --- Property type ---
        listing.property_type = extract_property_type(md) or _infer_type_from_url(url)

        # --- Location ---
        address_block = _extract_address_block(md)
        if address_block:
            listing.address = address_block.get("address")
            listing.city = address_block.get("city")
            listing.state = address_block.get("state")
            listing.zip = address_block.get("zip")

        # Bail if we have no location signal -- page is likely empty/gated
        if not listing.address and not listing.external_id:
            return None

        # --- Size ---
        m = _SIZE_RE.search(md)
        if m:
            listing.size_sf = normalize_sqft(m.group(0))

        # --- Asking price (sale) ---
        price_raw = _extract_price_raw(md, "sale")
        if price_raw:
            listing.sale_price_usd = normalize_price(price_raw)

        # --- Lease rate ---
        m = _LEASE_RATE_RE.search(md)
        if m:
            listing.lease_rate_min = normalize_price(m.group(0))

        # --- Cap rate ---
        m = _CAP_RATE_LINE_RE.search(md)
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

        # --- Description and highlights ---
        listing.description = _extract_description(md)
        listing.highlights = _extract_highlights(md)

        # --- Contacts ---
        listing.contacts = _extract_contacts(md)

        # --- Document URLs (floor plans, brochures) ---
        links = self._extract_links(scraped_dict)
        listing.documents = _classify_documents(links)

        # --- Raw data ---
        listing.raw_data = {
            "source": "jll",
            "url": url,
            "markdown_length": len(md),
            "link_count": len(links),
        }

        return listing


# ---------------------------------------------------------------------------
# JLL-specific helper functions
# ---------------------------------------------------------------------------

def _make_absolute(href: str) -> str:
    """Convert a relative JLL path to an absolute URL."""
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return _BASE_URL + href
    return ""


def _is_listing_url(url: str) -> bool:
    """Return True if the URL looks like a JLL property detail page."""
    return "property.jll.com/listings/" in url


def _infer_transaction_type(url: str, md: str) -> Optional[str]:
    """Infer transaction type from URL path and markdown text."""
    url_lower = url.lower()
    if "/sale" in url_lower or "for-sale" in url_lower:
        tx = "sale"
    elif "/rent" in url_lower or "for-lease" in url_lower:
        tx = "lease"
    else:
        tx = extract_transaction_type(md[:500])
    return tx


def _infer_type_from_url(url: str) -> Optional[str]:
    """Derive property type from JLL URL path segments."""
    url_lower = url.lower()
    for keyword, ptype in [
        ("office", "office"),
        ("industrial", "industrial"),
        ("retail", "retail"),
        ("multifamily", "multifamily"),
        ("multi-family", "multifamily"),
        ("flex", "flex"),
        ("land", "land"),
    ]:
        if keyword in url_lower:
            return ptype
    return None


def _extract_title(md: str) -> Optional[str]:
    """Return the first H1 or H2 heading, or the first non-empty line."""
    for line in md.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped and len(stripped) > 4:
            return stripped[:200]
    return None


def _extract_address_block(md: str) -> dict:
    """Parse city, state, zip, and street address from JLL markdown.

    JLL typically renders the address near the top of the page as:
        "123 Main Street\\nDallas, TX 75201"
    or inline in the first few paragraphs.
    """
    result: dict = {}

    # Look for a "City, ST ZIP" pattern
    city_state_zip_re = re.compile(
        r"([A-Za-z\s.'-]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)"
    )
    m = city_state_zip_re.search(md[:2000])
    if m:
        result["city"] = m.group(1).strip()
        result["state"] = normalize_state(m.group(2))
        result["zip"] = m.group(3)

    # Pull a street address from the lines immediately before city/state
    if m:
        text_before = md[: m.start()].strip()
        # The last non-empty line before the city block is usually the street address
        lines_before = [l.strip() for l in text_before.splitlines() if l.strip()]
        if lines_before:
            candidate = lines_before[-1].lstrip("#").strip()
            # Rough sanity: street address usually has a digit
            if re.search(r"\d", candidate) and len(candidate) < 120:
                result["address"] = candidate

    return result


def _extract_price_raw(md: str, mode: str = "sale") -> Optional[str]:
    """Extract a raw price string from markdown.

    ``mode="sale"`` looks for a standalone dollar amount.
    Returns None if the price is withheld ("Contact Broker").
    """
    skip_re = re.compile(r"contact\s+broker|upon\s+request|negotiable|pricing\s+available", re.IGNORECASE)
    for line in md.splitlines()[:80]:   # Check the first 80 lines
        if skip_re.search(line):
            return None
        m = _PRICE_INLINE_RE.search(line)
        if m:
            return line.strip()
    return None


def _extract_description(md: str) -> Optional[str]:
    """Return the largest prose paragraph from the markdown."""
    # Split on section headings; take the longest body paragraph
    sections = re.split(r"\n#{1,3}\s+", md)
    best: Optional[str] = None
    best_len = 0
    for section in sections:
        paras = [p.strip() for p in section.split("\n\n") if p.strip()]
        for para in paras:
            if len(para) > best_len and not para.startswith("#"):
                best_len = len(para)
                best = para
    return best[:2000] if best else None


def _extract_highlights(md: str) -> list[str]:
    """Extract bullet-point highlights from JLL markdown.

    JLL often groups key features as a bulleted list under a heading like
    "Property Highlights" or "Key Features".
    """
    highlights: list[str] = []
    in_section = False

    highlight_header_re = re.compile(
        r"(?:Property\s+Highlights?|Key\s+Features?|Features?|Highlights?)\s*$",
        re.IGNORECASE,
    )
    bullet_re = re.compile(r"^[\*\-•]\s+(.+)")

    for line in md.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if highlight_header_re.match(stripped):
            in_section = True
            continue
        if in_section:
            if stripped.startswith("#"):   # New heading ends the section
                in_section = False
                continue
            bm = bullet_re.match(stripped)
            if bm:
                highlights.append(bm.group(1).strip())

    return highlights[:20]


def _extract_contacts(md: str) -> list[dict]:
    """Parse agent/broker contact information from JLL markdown.

    Looks for the "Connect with our team" block and extracts name, phone,
    email, and title for each agent listed.
    """
    contacts: list[dict] = []

    # Find the team/contact section
    contact_section_re = re.compile(
        r"(?:Connect\s+with\s+our\s+team|Contact\s+(?:Us|Broker|Agent|Team)|"
        r"Team\s+Contact|Broker\s+Contact)[:\s]*\n+(.*?)(?=\n#{1,3}|\Z)",
        re.DOTALL | re.IGNORECASE,
    )
    m = contact_section_re.search(md)
    block = m.group(1) if m else md  # Fall back to full markdown

    # Collect emails and phones from the block
    emails = _EMAIL_RE.findall(block)
    phones_raw = _PHONE_RE.findall(block)
    phones = [clean_phone(p) for p in phones_raw]

    # Heuristic: each name-ish line followed by title/phone/email
    # builds one contact dict.
    name_re = re.compile(r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s*$")
    names: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if name_re.match(stripped) and len(stripped.split()) <= 4:
            names.append(stripped)

    if names:
        for i, name in enumerate(names[:5]):
            contacts.append({
                "name": name,
                "email": emails[i] if i < len(emails) else None,
                "phone": phones[i] if i < len(phones) else None,
                "brokerage_name": "JLL",
                "is_primary": i == 0,
            })
    elif emails or phones:
        # Couldn't parse names; at least capture contact info
        contacts.append({
            "name": None,
            "email": emails[0] if emails else None,
            "phone": phones[0] if phones else None,
            "brokerage_name": "JLL",
            "is_primary": True,
        })

    return contacts


def _classify_documents(links: list[str]) -> list[dict]:
    """Classify link URLs as brochures, floor plans, or OM documents.

    Returns a list of {doc_type, title, url} dicts for JLL PDF links.
    JLL typically hosts docs on property.jll.com or jll.com with
    PDF/brochure/floor-plan keywords in the path.
    """
    docs: list[dict] = []
    for url in links:
        url_lower = url.lower()
        if not url_lower.endswith(".pdf") and "pdf" not in url_lower:
            continue
        doc_type = "brochure"
        if "floor" in url_lower or "floorplan" in url_lower:
            doc_type = "floor_plan"
        elif "om" in url_lower or "offering" in url_lower:
            doc_type = "om"
        docs.append({"doc_type": doc_type, "title": None, "url": url})
    return docs
