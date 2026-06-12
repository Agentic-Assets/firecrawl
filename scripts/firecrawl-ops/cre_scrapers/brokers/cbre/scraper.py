"""
cbre.py -- CBRE broker scraper.

Inherits from base.BaseScraper (the pipeline-oriented ABC) and implements
the two required methods:

  discover_listings(search_url=None) -> list[str]
    Scrapes the CBRE search/results page and returns deduplicated detail URLs.

  parse_listing(url, scraped) -> ListingData | None
    Parses a Firecrawl result dict for a CBRE detail page into a ListingData.

External ID format: US-SMPL-6130, US-NNNNN-NNNNNN (upper-case slug + digits).
PDF/document assets live at: resources/fileassets/ or fileassets/ path segments.

Reference: docs/firecrawl-ops/references/cbre-scraping.md
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
    clean_phone,
    extract_transaction_type,
    extract_property_type,
)

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# CBRE property ID in URL: /details/US-SMPL-6130/property or /details/US-SMPL-6130/
_ID_RE = re.compile(r"/details/(US-[A-Z]+-[0-9]+)", re.IGNORECASE)

# Contact block patterns (CBRE detail pages render broker cards in markdown)
_CONTACT_NAME_RE = re.compile(
    r"(?:^|\n)\*\*([A-Z][a-zA-Z .'\-]+)\*\*",
)
_CONTACT_TITLE_RE = re.compile(
    r"(?:Senior\s+|Executive\s+)?(?:Vice\s+President|Managing\s+Director|Director|"
    r"Associate|Agent|Advisor|Broker|Principal|Partner|Specialist)[^\n]*",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}")
_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+\.[a-zA-Z]{2,}")

# Lease rate patterns: "$25.00 /SF/Yr" or "$15 - $20 /SF/NNN"
_LEASE_RATE_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:-\s*\$\s*([\d,]+(?:\.\d+)?))?\s*/\s*(?:SF|sq\.?\s*ft\.?)\s*/\s*"
    r"(Yr|Year|NNN|MG|FS|Modified\s+Gross|Full\s+Service|Gross)?",
    re.IGNORECASE,
)

# Address line: "123 Main Street, Houston, TX 77002"
_ADDRESS_FULL_RE = re.compile(
    r"(\d+[^,\n]{2,60}),\s*([A-Za-z ]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)"
)

# Price line in markdown: "**Asking Price:** $5,250,000" or similar
_ASKING_PRICE_RE = re.compile(
    r"(?:asking\s+price|list\s+price|sale\s+price)[:\s]+(\$[\d,. MmBbKk]+)",
    re.IGNORECASE,
)

# Size line: "**Total Size:** 22,500 SF"
_TOTAL_SIZE_RE = re.compile(
    r"(?:total\s+(?:size|area|building)|building\s+(?:size|area|sf))[:\s]+([\d,. KkMm]+\s*(?:SF|sq\.?\s*ft\.?)?)",
    re.IGNORECASE,
)

# Available space line
_AVAIL_SIZE_RE = re.compile(
    r"(?:available|avail\.?|space\s+available)[:\s]+([\d,. KkMm]+\s*(?:SF|sq\.?\s*ft\.?)?)",
    re.IGNORECASE,
)

# Transaction type keywords embedded in title or property label
_SALE_OR_LEASE_RE = re.compile(r"\bfor\s+sale\s*or\s+lease\b", re.IGNORECASE)
_FOR_SALE_RE = re.compile(r"\bfor\s+sale\b", re.IGNORECASE)
_FOR_LEASE_RE = re.compile(r"\bfor\s+lease\b|\bfor\s+rent\b", re.IGNORECASE)

# Document/PDF links
_PDF_LINK_RE = re.compile(
    r"https?://(?:www\.)?cbre\.com[^\s\)\"']*(?:fileassets|resources)[^\s\)\"']*\.pdf",
    re.IGNORECASE,
)
# Image URLs (CBRE CDN)
_IMAGE_RE = re.compile(
    r"https?://[^\s\"']+cbre\.com[^\s\"']*\.(?:jpg|jpeg|png|webp)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Module-level parsing helpers (inline regex, no external deps)
# ---------------------------------------------------------------------------

_YEAR_BUILT_RE = re.compile(r"(?:year\s+built|built\s+in)[:\s]*(\d{4})", re.IGNORECASE)
_FLOORS_RE = re.compile(r"(\d+)\s*(?:floors?|stories|stor(?:ey|ies))", re.IGNORECASE)
_UNITS_RE = re.compile(r"(\d+)\s*(?:units?|apartments?|suites?)", re.IGNORECASE)
_CAP_RATE_RE = re.compile(r"cap\s*rate[:\s]+(\d+\.?\d*)\s*%", re.IGNORECASE)
_NOI_RE = re.compile(r"noi[:\s]+\$?([\d,]+(?:\.\d+)?)\s*([Mm]illion|[Mm])?", re.IGNORECASE)
_OCCUPANCY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:occupied|occupancy|leased)", re.IGNORECASE)
_PRICE_RE = re.compile(
    r"\$?([\d,]+(?:\.\d+)?)\s*([Mm]illion|[Mm]|[Bb]illion|[Bb]|[Kk])?",
    re.IGNORECASE,
)


def _parse_year_built(text: str) -> Optional[int]:
    m = _YEAR_BUILT_RE.search(text)
    return int(m.group(1)) if m else None


def _parse_floors(text: str) -> Optional[int]:
    m = _FLOORS_RE.search(text)
    return int(m.group(1)) if m else None


def _parse_units(text: str) -> Optional[int]:
    m = _UNITS_RE.search(text)
    return int(m.group(1)) if m else None


def _parse_cap_rate_text(text: str) -> Optional[float]:
    m = _CAP_RATE_RE.search(text)
    if not m:
        return None
    return round(float(m.group(1)) / 100, 6)


def _parse_noi_text(text: str) -> Optional[float]:
    m = _NOI_RE.search(text)
    if not m:
        return None
    raw = float(m.group(1).replace(",", ""))
    if m.group(2) and m.group(2).lower() in ("million", "m"):
        raw *= 1_000_000
    return raw


def _parse_occupancy_text(text: str) -> Optional[float]:
    m = _OCCUPANCY_RE.search(text)
    if not m:
        return None
    return round(float(m.group(1)) / 100, 6)


def _parse_price_generic(text: str) -> Optional[float]:
    """Extract the first USD dollar amount from text as a float."""
    m = _PRICE_RE.search(text)
    if not m:
        return None
    raw = float(m.group(1).replace(",", ""))
    suffix = (m.group(2) or "").lower()
    if suffix in ("million", "m"):
        raw *= 1_000_000
    elif suffix in ("billion", "b"):
        raw *= 1_000_000_000
    elif suffix == "k":
        raw *= 1_000
    return raw


# Lease rate type normalization map
_LEASE_TYPE_MAP = {
    "nnn": "NNN",
    "mg": "MG",
    "modified gross": "MG",
    "fs": "FS",
    "full service": "FS",
    "gross": "Gross",
    "yr": None,
    "year": None,
}


class CBREScraper(BaseScraper):
    """CBRE commercial real estate scraper.

    Handles Cloudflare Managed Challenge via stealth playwright proxy.
    Property detail URLs contain a stable external ID (e.g. US-SMPL-6130).
    """

    BROKER_SLUG = "cbre"
    SEARCH_URL = "https://www.cbre.com/properties/properties-for-sale/commercial-space"
    FIRECRAWL_OPTIONS = {
        "proxy": "stealth",
        "waitFor": 6000,
        "timeout": 60000,
    }

    def _log(self, msg: str) -> None:
        """Write a timestamped log line to stderr."""
        import sys as _sys
        print(f"[{self.BROKER_SLUG}] {msg}", file=_sys.stderr)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_listings(self, search_url: str = None) -> list:
        """Scrape the CBRE search page and return deduplicated property detail URLs.

        Filters links to those containing '/details/' on the cbre.com domain.
        Returns [] on scrape failure.
        """
        url = search_url or self.SEARCH_URL
        self._log(f"discover_listings: {url}")

        result = self.scrape_url(
            url,
            options={
                "formats": ["links", "markdown"],
                "onlyMainContent": False,
                "waitFor": 8000,  # extra wait for SPA hydration on search page
            },
        )
        if not result.get("success"):
            self._log(f"discover FAIL: {result.get('error', '?')}")
            return []

        all_links = self._extract_links(result)

        detail_urls = []
        for href in all_links:
            if "cbre.com" in href and "/details/" in href:
                detail_urls.append(href)

        unique = self._dedup(detail_urls)
        self._log(f"discover_listings: found {len(unique)} detail URLs")
        return unique

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse_listing(self, url: str, scraped: dict) -> Optional[ListingData]:
        """Parse a scraped CBRE detail page into a ListingData.

        scraped is the Firecrawl 'data' dict (keys: markdown, links, metadata).
        Returns None if the page appears empty or is not a valid listing.
        """
        # scraped may be the full response dict or just the data sub-dict
        if "data" in scraped and isinstance(scraped.get("data"), dict):
            data = scraped["data"]
        else:
            data = scraped

        markdown: str = self._get_markdown(scraped)
        data = scraped.get("data", scraped)
        metadata: dict = data.get("metadata", {}) or {}
        links: list = self._extract_links(scraped)

        if not markdown or len(markdown) < 100:
            self._log(f"parse_listing: skipping {url} (empty markdown)")
            return None

        listing = ListingData(brokerage_slug=self.BROKER_SLUG, source_url=url)

        # --- External ID ---
        listing.external_id = self._extract_id(url)

        # --- Title ---
        title_candidates = [
            metadata.get("ogTitle"),
            metadata.get("title"),
            self._extract_h1(markdown),
        ]
        for candidate in title_candidates:
            if candidate and len(candidate.strip()) > 3:
                listing.title = candidate.strip()
                break

        # --- Transaction type ---
        listing.transaction_type = self._extract_transaction_type(url, markdown, listing.title or "")

        # --- Property type ---
        listing.property_type = extract_property_type(markdown[:2000])

        # --- Address components ---
        self._extract_address(markdown, listing)

        # --- Size ---
        size_match = _TOTAL_SIZE_RE.search(markdown)
        if size_match:
            listing.size_sf = normalize_sqft(size_match.group(1))
        avail_match = _AVAIL_SIZE_RE.search(markdown)
        if avail_match:
            listing.available_sf = normalize_sqft(avail_match.group(1))

        # --- Year built / floors / units ---
        listing.year_built = _parse_year_built(markdown)
        listing.floors = _parse_floors(markdown)
        listing.units = _parse_units(markdown)

        # --- Financials: sale ---
        listing.sale_price_usd = self._extract_sale_price(markdown)
        listing.cap_rate = _parse_cap_rate_text(markdown)
        listing.noi = _parse_noi_text(markdown)
        listing.occupancy_rate = _parse_occupancy_text(markdown)

        # --- Financials: lease ---
        self._extract_lease_rate(markdown, listing)

        # --- Description ---
        listing.description = self._extract_description(markdown)

        # --- Highlights ---
        listing.highlights = self._extract_highlights(markdown)

        # --- Zoning ---
        zoning_m = re.search(r"zoning[:\s]+([A-Za-z0-9\-/ ]+)", markdown, re.IGNORECASE)
        if zoning_m:
            listing.zoning = zoning_m.group(1).strip()[:50]

        # --- Contacts ---
        listing.contacts = self._extract_contacts(markdown)

        # --- Documents ---
        listing.documents = [
            {"doc_type": "pdf", "title": url.rstrip("/").split("/")[-1], "url": url}
            for url in self._extract_pdf_urls(markdown, links)
        ]

        # --- Images ---
        listing.images = self._extract_images(markdown, links, metadata)

        # --- Raw payload ---
        listing.markdown = markdown
        listing.raw_data = {
            "metadata": metadata,
            "links_count": len(links),
            "markdown_len": len(markdown),
        }

        return listing

    # ------------------------------------------------------------------
    # Private extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_id(url: str) -> Optional[str]:
        """Pull the CBRE property ID (US-SMPL-6130 pattern) from the detail URL."""
        m = _ID_RE.search(url)
        return m.group(1).upper() if m else None

    @staticmethod
    def _extract_h1(markdown: str) -> Optional[str]:
        """Return text of the first H1 heading in markdown."""
        m = re.search(r"^#\s+(.+)$", markdown, re.MULTILINE)
        return m.group(1).strip() if m else None

    @staticmethod
    def _extract_transaction_type(url: str, markdown: str, title: str) -> Optional[str]:
        """Determine sale / lease / sale_or_lease from URL path and page text."""
        combined = f"{url} {title} {markdown[:500]}"
        if _SALE_OR_LEASE_RE.search(combined):
            return "sale_or_lease"
        # URL path carries 'for-sale' or 'for-lease'
        if "for-sale" in url.lower():
            return "sale"
        if "for-lease" in url.lower():
            return "lease"
        if _FOR_SALE_RE.search(combined):
            return "sale"
        if _FOR_LEASE_RE.search(combined):
            return "lease"
        return None

    @staticmethod
    def _extract_address(markdown: str, listing: ListingData) -> None:
        """Parse address / city / state / zip from the first matching address pattern."""
        m = _ADDRESS_FULL_RE.search(markdown)
        if m:
            listing.address = m.group(1).strip()
            listing.city = m.group(2).strip()
            listing.state = m.group(3).upper()
            listing.zip = m.group(4)
            return
        # Fallback: state from "XX 12345" pattern
        state_m = re.search(r",\s*([A-Z]{2})\s+(\d{5})", markdown)
        if state_m:
            listing.state = state_m.group(1)
            listing.zip = state_m.group(2)

    @staticmethod
    def _extract_sale_price(markdown: str) -> Optional[float]:
        """Extract a sale/asking price from markdown."""
        m = _ASKING_PRICE_RE.search(markdown)
        if m:
            return normalize_price(m.group(1))
        # Fallback: generic dollar amount near "price" keyword
        price_section = re.search(r"price[^\n]{0,100}", markdown[:3000], re.IGNORECASE)
        if price_section:
            return normalize_price(price_section.group(0))
        return None

    @staticmethod
    def _extract_lease_rate(markdown: str, listing: ListingData) -> None:
        """Populate lease_rate_min, lease_rate_max, lease_rate_type."""
        m = _LEASE_RATE_RE.search(markdown)
        if not m:
            return
        try:
            listing.lease_rate_min = float(m.group(1).replace(",", ""))
        except (ValueError, TypeError):
            pass
        if m.group(2):
            try:
                listing.lease_rate_max = float(m.group(2).replace(",", ""))
            except (ValueError, TypeError):
                pass
        if m.group(3):
            raw_type = m.group(3).strip().lower()
            listing.lease_rate_type = _LEASE_TYPE_MAP.get(raw_type, m.group(3).strip())

    @staticmethod
    def _extract_description(markdown: str) -> Optional[str]:
        """Pull the first substantive paragraph as the description.

        Skips headings, short lines, and navigation noise.
        """
        paragraphs = [
            p.strip()
            for p in re.split(r"\n{2,}", markdown)
            if p.strip() and not p.strip().startswith("#") and len(p.strip()) > 80
        ]
        return paragraphs[0] if paragraphs else None

    @staticmethod
    def _extract_highlights(markdown: str) -> list:
        """Extract bullet-point highlights from the markdown.

        Looks for a 'Highlights' / 'Key Features' / 'Property Highlights' section
        and returns the individual bullet items.
        """
        highlights_section = re.search(
            r"(?:highlights|key\s+features|property\s+highlights)[^\n]*\n((?:[-*]\s+.+\n?)+)",
            markdown,
            re.IGNORECASE,
        )
        if not highlights_section:
            return []
        items = re.findall(r"[-*]\s+(.+)", highlights_section.group(1))
        return [item.strip() for item in items if item.strip()][:10]

    @staticmethod
    def _extract_contacts(markdown: str) -> list:
        """Parse broker/agent contact cards from the markdown.

        CBRE detail pages typically render contacts as:
          **John Smith**
          Vice President
          (713) 555-1234
          john.smith@cbre.com

        Returns a list of Contact-compatible dicts.
        """
        contacts = []
        # Split on bold-name anchors
        blocks = re.split(r"\n(?=\*\*[A-Z])", markdown)
        for block in blocks:
            name_m = re.match(r"\*\*([^*]+)\*\*", block)
            if not name_m:
                continue
            name = name_m.group(1).strip()
            # Filter out non-person bold text (property names, section headers)
            if len(name.split()) < 2 or any(
                kw in name.lower()
                for kw in ("property", "available", "offering", "floor", "unit", "building")
            ):
                continue

            contact: dict = {"name": name, "title": None, "phone": None, "email": None}

            title_m = _CONTACT_TITLE_RE.search(block)
            if title_m:
                contact["title"] = title_m.group(0).strip()[:100]

            phones = _PHONE_RE.findall(block)
            if phones:
                contact["phone"] = clean_phone(phones[0])

            emails = _EMAIL_RE.findall(block)
            if emails:
                # Exclude cbre.com marketing addresses
                real_emails = [e for e in emails if not e.lower().endswith("@cbre.com") is False]
                contact["email"] = emails[0]

            contacts.append(contact)

        return contacts[:5]  # cap at 5 contacts per listing

    @staticmethod
    def _extract_pdf_urls(markdown: str, links: list) -> list:
        """Extract PDF/document URLs from the markdown body and link set.

        CBRE PDFs are usually at paths containing 'fileassets' or 'resources'.
        """
        docs = set()

        # Pull from markdown text directly
        for m in _PDF_LINK_RE.finditer(markdown):
            docs.add(m.group(0))

        # Pull from Firecrawl links array
        for href in links:
            if (
                isinstance(href, str)
                and href.lower().endswith(".pdf")
                and ("fileassets" in href or "resources" in href)
                and "cbre.com" in href
            ):
                docs.add(href)

        return sorted(docs)

    @staticmethod
    def _extract_images(markdown: str, links: list, metadata: dict) -> list:
        """Collect image URLs from markdown, links, and OG metadata.

        Returns a list of dicts: {url, is_primary}.
        """
        images = []
        seen: set = set()

        def _add(url: str, primary: bool = False):
            if url and url not in seen:
                seen.add(url)
                images.append({"url": url, "is_primary": primary})

        # OG image first (highest quality)
        og_image = metadata.get("ogImage") or metadata.get("og:image")
        if og_image:
            _add(og_image, primary=True)

        # Images embedded in markdown
        for m in _IMAGE_RE.finditer(markdown):
            _add(m.group(0))

        # Images in links
        for href in links:
            if isinstance(href, str) and any(href.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
                if "cbre.com" in href:
                    _add(href)

        return images[:20]  # cap at 20 images
