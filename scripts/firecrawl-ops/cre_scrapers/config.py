#!/usr/bin/env python3
"""
config.py -- Broker scrape configuration for EQUIRE CRE listing intelligence.

Single source of truth (Python side) for the 10 national CRE brokerages the
ListingHunterAgent scrapes via the self-hosted Firecrawl instance. Values here
MUST stay aligned with the cre_brokerages seed rows in
``scripts/firecrawl-ops/sql/001_cre_brokerages.sql`` (slug is the join key).

proxy / wait_for_ms come from live Firecrawl testing on 2026-06-11. CBRE is the
reference implementation (see scripts/firecrawl-ops/cbre_scrape.py and
docs/firecrawl-ops/references/cbre-scraping.md).

Usage:
    from cre_scrapers.config import BROKERS, BrokerConfig, STEALTH

    cfg = BROKERS["colliers"]
    payload = {"url": cfg.search_url, **cfg.firecrawl_options()}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Proxy mode constants (Firecrawl `proxy` field).
#   stealth -> playwright-extra stealth engine; required for Cloudflare sites.
#   basic   -> plain fetch/proxy; fine for unprotected sites.
# ---------------------------------------------------------------------------
STEALTH: str = "stealth"
BASIC: str = "basic"

# Default timeout for a stealth render (ms). Stealth is slower than plain fetch;
# budget ~15-20s per property page.
DEFAULT_TIMEOUT_MS: int = 60000


@dataclass
class BrokerConfig:
    """Scrape configuration for one brokerage.

    Mirrors a cre_brokerages row. ``slug`` is the stable join key against the
    database; ``active=False`` marks brokers excluded from scheduled runs
    (access-gated or consistently failing sites).
    """

    name: str
    slug: str
    base_url: str
    search_url: str
    proxy: str = STEALTH
    wait_for_ms: int = 6000
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    pagination: str = "unknown"
    notes: str = ""
    active: bool = True
    # Optional per-site extras passed straight through to Firecrawl.
    # NAI Global needs an `actions` click to clear the CookieYes consent wall.
    actions: Optional[list] = None
    listing_url_pattern: Optional[str] = None
    external_id_pattern: Optional[str] = None

    def firecrawl_options(self, formats: Optional[list] = None) -> dict:
        """Return the Firecrawl scrape options dict for this broker.

        Maps the snake_case config fields to Firecrawl's camelCase payload keys
        (proxy, waitFor, timeout), matching cbre_scrape.py STEALTH_OPTIONS.
        """
        opts: dict = {
            "proxy": self.proxy,
            "waitFor": self.wait_for_ms,
            "timeout": self.timeout_ms,
        }
        if formats is not None:
            opts["formats"] = formats
        if self.actions:
            opts["actions"] = self.actions
        return opts


# ---------------------------------------------------------------------------
# BROKERS: slug -> BrokerConfig for all 10 national CRE brokerages.
# Keep in lockstep with sql/001_cre_brokerages.sql.
# ---------------------------------------------------------------------------
BROKERS: dict[str, BrokerConfig] = {
    # 1. Reference implementation. Cloudflare Managed Challenge -> stealth, waitFor>=6000.
    "cbre": BrokerConfig(
        name="CBRE",
        slug="cbre",
        base_url="https://www.cbre.com",
        search_url="https://www.cbre.com/properties/properties-for-sale/commercial-space",
        proxy=STEALTH,
        wait_for_ms=6000,
        timeout_ms=60000,
        pagination="search_filter_combinations",
        listing_url_pattern="/properties/properties-for-{sale|lease}/commercial-space/details/{id}/{slug}",
        external_id_pattern=r"US-[A-Z]+-[0-9]+",
        notes="Reference impl. waitFor>=6000 for SPA hydration post-CF challenge. PDFs also behind CF.",
        active=True,
    ),
    # 2. Own Next.js site, no CF. Browse pages thin; scrape category/search pages.
    "jll": BrokerConfig(
        name="JLL",
        slug="jll",
        base_url="https://property.jll.com",
        search_url="https://property.jll.com/sale-office",
        proxy=STEALTH,
        wait_for_ms=5000,
        timeout_ms=60000,
        pagination="search_query_params",
        listing_url_pattern="/listings/{slug-address-market}",
        notes="Homepage thin; scrape category/search pages (/sale-office, /rent-industrial). ~20 cards/page, client-side paginated.",
        active=True,
    ),
    # 3. Coveo faceted search. Basic geo-redirect, no challenge. Use US URL directly.
    "cushman-wakefield": BrokerConfig(
        name="Cushman & Wakefield",
        slug="cushman-wakefield",
        base_url="https://www.cushmanwakefield.com",
        search_url="https://www.cushmanwakefield.com/en/united-states/properties/invest/search",
        proxy=STEALTH,
        wait_for_ms=6000,
        timeout_ms=60000,
        pagination="coveo_fragment_facets",
        listing_url_pattern="/en/united-states/properties/for-sale/{type}/{state}/{city}/{slug}/{slug}-s",
        notes="Use US URL directly (skip geo-redirect). Search page renders full cards with prices. Trailing -s on detail URLs. Coveo #first=N paging.",
        active=True,
    ),
    # 4. Cleanest target. No CF. Listing IDs (usa+7digit) directly in links.
    "colliers": BrokerConfig(
        name="Colliers",
        slug="colliers",
        base_url="https://www.colliers.com",
        search_url="https://www.colliers.com/en/properties",
        proxy=STEALTH,
        wait_for_ms=5000,
        timeout_ms=60000,
        pagination="coveo_hash_facets",
        listing_url_pattern="/en/properties/{name-slug}/{address-slug}/usa{7-digit-id}",
        external_id_pattern=r"usa[0-9]{7}",
        notes="Easiest target. Listing IDs stable/parseable from URL tail. Raise wait_for to 7000 if render slow.",
        active=True,
    ),
    # 5. TIMEOUT even at 90s. Headless detection suspected. Disabled pending API approach.
    "marcus-millichap": BrokerConfig(
        name="Marcus & Millichap",
        slug="marcus-millichap",
        base_url="https://www.marcusmillichap.com",
        search_url="https://www.marcusmillichap.com/properties",
        proxy=STEALTH,
        wait_for_ms=10000,
        timeout_ms=120000,
        pagination="unknown",
        listing_url_pattern="/properties/{listing-id}",
        notes="FAILED: SCRAPE_TIMEOUT at 60s and 90s. Try wait_for=10000 timeout=120000. May need fingerprint spoofing or map API. Disabled.",
        active=False,
    ),
    # 6. Liferay hash SPA. Listings hydrate client-side; not in initial HTML. Hard.
    "avison-young": BrokerConfig(
        name="Avison Young",
        slug="avison-young",
        base_url="https://www.avisonyoung.us",
        search_url="https://www.avisonyoung.us/property-search",
        proxy=STEALTH,
        wait_for_ms=9000,
        timeout_ms=90000,
        pagination="hash_spa_or_internal_api",
        listing_url_pattern="/web/{market}/properties/{listing-id}",
        notes="HARD: hash SPA, listings not in initial HTML. waitFor 8000-10000 to hydrate. May need internal listing API.",
        active=True,
    ),
    # 7. CookieYes consent wall blocks listings. Click Accept All before scraping.
    "nai-global": BrokerConfig(
        name="NAI Global",
        slug="nai-global",
        base_url="https://www.naiglobal.com",
        search_url="https://www.naiglobal.com/north-american-listings/",
        proxy=STEALTH,
        wait_for_ms=5000,
        timeout_ms=60000,
        pagination="unknown_until_consent_bypassed",
        listing_url_pattern=None,
        actions=[
            {"type": "click", "selector": "[data-cky-tag=accept-button], .cky-btn-accept"}
        ],
        notes="MEDIUM: barrier is CookieYes consent, not CF. actions click clears it. Franchise listings may live on partner subdomains.",
        active=True,
    ),
    # 8. Legacy page scraper is disabled. Production collection uses public Algolia in ../cre_collector.
    "newmark": BrokerConfig(
        name="Newmark",
        slug="newmark",
        base_url="https://www.nmrk.com",
        search_url="https://www.nmrk.com/properties?saleOrLease=sale",
        proxy=STEALTH,
        wait_for_ms=5000,
        timeout_ms=60000,
        pagination="legacy_page_scraper_disabled",
        listing_url_pattern="/properties?saleOrLease=sale&propertyTypes={n}",
        notes="Legacy page scraper is disabled because rendered pages gate behind nim.nmrk.com. Production collection uses the public Algolia API in cre_collector/collect.ts.",
        active=False,
    ),
    # 9. WordPress, no CF. Browse page nav-only; listings behind a search form.
    "svn": BrokerConfig(
        name="SVN",
        slug="svn",
        base_url="https://svn.com",
        search_url="https://svn.com/properties/?propertyTypes=3",
        proxy=STEALTH,
        wait_for_ms=5000,
        timeout_ms=60000,
        pagination="wordpress_query_params",
        listing_url_pattern="/properties/?propertyTypes={type_id}&searchText={text}",
        notes="MEDIUM: no cards on browse page; trigger search via ?propertyTypes={id}. Standard WP paginated archive (&page=N).",
        active=True,
    ),
    # 10. Not in test batch. Default-safe stealth; verify on first run, then update.
    "lee-associates": BrokerConfig(
        name="Lee & Associates",
        slug="lee-associates",
        base_url="https://www.lee-associates.com",
        search_url="https://www.lee-associates.com/properties/",
        proxy=STEALTH,
        wait_for_ms=6000,
        timeout_ms=60000,
        pagination="unknown",
        listing_url_pattern="/properties/{listing-id}",
        notes="UNVERIFIED: not in test batch. Start default stealth/6000. Run discovery scrape, then update measured values.",
        active=True,
    ),
}


# ---------------------------------------------------------------------------
# Convenience accessors.
# ---------------------------------------------------------------------------
def active_brokers() -> dict[str, BrokerConfig]:
    """Brokers enabled for scheduled scrape runs (excludes gated/failing sites)."""
    return {slug: cfg for slug, cfg in BROKERS.items() if cfg.active}


def get_broker(slug: str) -> BrokerConfig:
    """Look up a broker by slug; raises KeyError with the valid set on miss."""
    try:
        return BROKERS[slug]
    except KeyError:
        raise KeyError(f"Unknown broker slug '{slug}'. Valid: {', '.join(sorted(BROKERS))}")


if __name__ == "__main__":
    # Quick self-report: python3 cre_scrapers/config.py
    print(f"{len(BROKERS)} brokers configured ({len(active_brokers())} active):\n")
    for slug, cfg in BROKERS.items():
        state = "active " if cfg.active else "DISABLED"
        print(f"  [{state}] {slug:18s} proxy={cfg.proxy:8s} waitFor={cfg.wait_for_ms:>6}ms  {cfg.name}")
