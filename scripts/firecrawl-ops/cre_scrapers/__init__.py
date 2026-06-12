"""
cre_scrapers -- CRE listing scraper package for EQUIRE listing intelligence.

Exposes the canonical public API for all broker scraper modules:
  - BaseScraper: abstract base all broker scrapers inherit from
  - ListingData: canonical dataclass mirroring the cre_listings SQL schema
  - normalize_price / normalize_sqft: field-level normalizers
  - BROKERS / BrokerConfig: broker config registry (from config.py)

Broker-specific scraper code lives in ``cre_scrapers/brokers/<slug>/``.
Top-level modules such as ``cre_scrapers.cushman`` remain as compatibility
shims for older imports.
"""

from .base import BaseScraper
from .normalizer import ListingData, normalize_price, normalize_sqft
from .config import BROKERS, BrokerConfig

__all__ = [
    "BaseScraper",
    "ListingData",
    "normalize_price",
    "normalize_sqft",
    "BROKERS",
    "BrokerConfig",
]


def get_scraper(slug: str) -> BaseScraper:
    """Instantiate and return a scraper by broker slug.

    Raises KeyError for unimplemented slugs.

    Example:
        scraper = get_scraper("jll")
        result = scraper.run(max_listings=20)
    """
    from .avison_young import AvisonYoungScraper
    from .cbre import CBREScraper
    from .colliers import ColliersScraper
    from .cushman import CushmanScraper
    from .jll import JLLScraper
    from .marcus_millichap import MarcusMillichapScraper
    from .nai_global import NAIGlobalScraper
    from .newmark import NewmarkScraper
    from .svn import SVNScraper

    registry: dict[str, type] = {
        "avison-young": AvisonYoungScraper,
        "cbre": CBREScraper,
        "colliers": ColliersScraper,
        "cushman": CushmanScraper,
        "jll": JLLScraper,
        "cushman-wakefield": CushmanScraper,
        "marcus-millichap": MarcusMillichapScraper,
        "nai-global": NAIGlobalScraper,
        "newmark": NewmarkScraper,
        "svn": SVNScraper,
    }
    if slug not in registry:
        raise KeyError(
            f"No scraper implemented for '{slug}'. "
            f"Available: {', '.join(sorted(registry))}"
        )
    return registry[slug]()
