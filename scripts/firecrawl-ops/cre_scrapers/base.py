#!/usr/bin/env python3
"""
base.py -- Abstract base class for all EQUIRE CRE listing scrapers.

Every broker scraper (jll.py, cushman.py, colliers.py, etc.) inherits from
BaseScraper and implements two abstract methods:

  discover_listings(search_url) -> list[str]
      Scrape the broker's search/browse page and return a deduplicated list of
      detail-page URLs for individual property listings.

  parse_listing(url, scraped_dict) -> ListingData | None
      Given a Firecrawl scrape result dict (with "markdown", "links",
      "metadata" keys), extract structured fields and return a populated
      ListingData.  Return None to signal that the page is un-parseable
      (error, redirect, gated, empty).

The base class provides:

  scrape_url(url, options)     -- single-URL Firecrawl POST /v2/scrape
  batch_scrape(urls)           -- async /v2/batch/scrape + polling
  run(max_listings, output_dir)-- full discover -> scrape -> parse pipeline
                                  with per-URL JSON output

Firecrawl connection comes from environment variables (same as cbre_scrape.py):
  FIRECRAWL_API_URL  (default: http://localhost:3002)
  FIRECRAWL_API_KEY  (default: empty, fine for self-hosted)
"""

from __future__ import annotations

import json
import os
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from .normalizer import ListingData

# ---------------------------------------------------------------------------
# Firecrawl connection
# ---------------------------------------------------------------------------

_FIRECRAWL_API_URL = os.environ.get("FIRECRAWL_API_URL", "http://localhost:3002")
_FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")

_POLL_INTERVAL = 8   # seconds between batch-job status polls
_HTTP_TIMEOUT = 90   # urllib timeout for sync calls


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if _FIRECRAWL_API_KEY:
        h["Authorization"] = f"Bearer {_FIRECRAWL_API_KEY}"
    return h


def _post(path: str, body: dict) -> dict:
    """POST to the local Firecrawl instance; returns parsed JSON."""
    import urllib.request
    import urllib.error

    url = f"{_FIRECRAWL_API_URL}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read()
        try:
            err = json.loads(body_bytes)
        except Exception:
            err = {"raw": body_bytes.decode(errors="replace")}
        print(
            f"[base] POST {path} -> HTTP {exc.code}: {json.dumps(err, indent=2)}",
            file=sys.stderr,
        )
        return {"success": False, "error": f"HTTP {exc.code}"}


def _get(path: str) -> dict:
    """GET from the local Firecrawl instance; returns parsed JSON."""
    import urllib.request
    import urllib.error

    url = f"{_FIRECRAWL_API_URL}{path}"
    req = urllib.request.Request(url, headers=_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read()
        try:
            err = json.loads(body_bytes)
        except Exception:
            err = {"raw": body_bytes.decode(errors="replace")}
        print(
            f"[base] GET {path} -> HTTP {exc.code}: {json.dumps(err, indent=2)}",
            file=sys.stderr,
        )
        return {"success": False, "error": f"HTTP {exc.code}"}


# ---------------------------------------------------------------------------
# Abstract base scraper
# ---------------------------------------------------------------------------

class BaseScraper(ABC):
    """Abstract base class for CRE broker scrapers.

    Subclass contract
    -----------------
    Required class variables:

      BROKER_SLUG: str
          Stable join key matching cre_brokerages.slug and config.BROKERS.

      SEARCH_URL: str
          Default search/browse URL for the discovery pass.

      FIRECRAWL_OPTIONS: dict
          Base Firecrawl options dict (proxy, waitFor, timeout).
          Merge per-call overrides on top when needed.

    Required methods:

      discover_listings(search_url) -> list[str]
      parse_listing(url, scraped_dict) -> ListingData | None
    """

    # --- Subclass must set these ---
    BROKER_SLUG: str = ""
    SEARCH_URL: str = ""
    FIRECRAWL_OPTIONS: dict = {
        "proxy": "stealth",
        "waitFor": 6000,
        "timeout": 60000,
    }

    # Default formats for detail-page scrapes
    _DEFAULT_FORMATS: list[str] = ["markdown", "links"]

    # ---------------------------------------------------------------------------
    # Core Firecrawl operations
    # ---------------------------------------------------------------------------

    def scrape_url(self, url: str, options: Optional[dict] = None) -> dict:
        """Scrape a single URL.

        Merges FIRECRAWL_OPTIONS with any ``options`` override dict.
        Returns the raw Firecrawl result dict (keys: success, data, error).
        """
        payload: dict = {
            "url": url,
            "formats": self._DEFAULT_FORMATS,
            **self.FIRECRAWL_OPTIONS,
        }
        if options:
            payload.update(options)
        return _post("/v2/scrape", payload)

    def batch_scrape(self, urls: list[str]) -> list[dict]:
        """Submit a batch job for ``urls`` and poll until complete.

        Returns a list of individual scrape result dicts (one per URL, in
        the same order Firecrawl returns them).  Failed items are included
        with empty markdown so the caller can log them.
        """
        if not urls:
            return []

        payload: dict = {
            "urls": urls,
            "formats": self._DEFAULT_FORMATS,
            **self.FIRECRAWL_OPTIONS,
        }
        submit = _post("/v2/batch/scrape", payload)
        if not submit.get("success") and "id" not in submit:
            print(
                f"[{self.BROKER_SLUG}] batch submit failed: {submit}",
                file=sys.stderr,
            )
            return []

        job_id = submit.get("id") or submit.get("jobId")
        print(f"[{self.BROKER_SLUG}] batch job {job_id} - {len(urls)} URLs")

        # Poll until complete
        while True:
            result = _get(f"/v2/batch/scrape/{job_id}")
            status = result.get("status", "unknown")
            total = result.get("total", "?")
            completed = result.get("completed", "?")
            print(
                f"[{self.BROKER_SLUG}] batch status={status}  {completed}/{total}",
                end="\r",
            )
            if status == "completed":
                print()
                return result.get("data", [])
            if status in ("failed", "cancelled"):
                print(f"\n[{self.BROKER_SLUG}] batch ended: status={status}", file=sys.stderr)
                return result.get("data", [])
            time.sleep(_POLL_INTERVAL)

    # ---------------------------------------------------------------------------
    # Abstract methods (must be implemented by each broker subclass)
    # ---------------------------------------------------------------------------

    @abstractmethod
    def discover_listings(self, search_url: Optional[str] = None) -> list[str]:
        """Scrape the broker's search page and return detail-page URLs.

        Args:
            search_url: Override the default SEARCH_URL when provided.

        Returns:
            Deduplicated list of absolute detail-page URLs (strings).
            Empty list on failure -- do not raise.
        """

    @abstractmethod
    def parse_listing(self, url: str, scraped_dict: dict) -> Optional[ListingData]:
        """Parse a Firecrawl scrape result into a structured ListingData.

        Args:
            url: The source URL that was scraped.
            scraped_dict: One item from Firecrawl's result list.  Typical
                keys: "markdown", "links", "metadata", "html".

        Returns:
            Populated ListingData on success, None on unparseable content.
        """

    # ---------------------------------------------------------------------------
    # Full pipeline
    # ---------------------------------------------------------------------------

    def run(
        self,
        max_listings: int = 50,
        output_dir: Optional[Path] = None,
        search_url: Optional[str] = None,
    ) -> dict:
        """Discover -> batch-scrape -> parse pipeline.

        Args:
            max_listings: Hard cap on the number of detail pages to scrape
                after discovery.  Useful for testing without a full run.
            output_dir: If provided, write each scrape result to
                ``output_dir/{slug}.json`` and each parsed listing to
                ``output_dir/parsed/{slug}.json``.
            search_url: Override the default SEARCH_URL.

        Returns:
            Summary dict:
              {
                "broker": BROKER_SLUG,
                "discovered": int,
                "scraped": int,
                "parsed": int,
                "errors": int,
                "listings": [ListingData, ...],  # successfully parsed
              }
        """
        summary: dict = {
            "broker": self.BROKER_SLUG,
            "discovered": 0,
            "scraped": 0,
            "parsed": 0,
            "errors": 0,
            "listings": [],
        }

        # Step 1: discover
        print(f"[{self.BROKER_SLUG}] discovering listings …")
        detail_urls = self.discover_listings(search_url)
        summary["discovered"] = len(detail_urls)
        print(f"[{self.BROKER_SLUG}] found {len(detail_urls)} detail URLs")

        if not detail_urls:
            print(f"[{self.BROKER_SLUG}] no URLs found; aborting run.", file=sys.stderr)
            return summary

        # Apply cap
        if max_listings and len(detail_urls) > max_listings:
            print(
                f"[{self.BROKER_SLUG}] capping at {max_listings} (of {len(detail_urls)} discovered)"
            )
            detail_urls = detail_urls[:max_listings]

        # Persist discovered URL list
        if output_dir:
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            links_file = out / "discovered_links.json"
            with open(links_file, "w") as f:
                json.dump(detail_urls, f, indent=2)
            print(f"[{self.BROKER_SLUG}] discovery links -> {links_file}")

        # Step 2: batch-scrape
        print(f"[{self.BROKER_SLUG}] scraping {len(detail_urls)} pages …")
        scrape_results = self.batch_scrape(detail_urls)
        summary["scraped"] = len(scrape_results)

        # Step 3: parse
        parsed_dir = Path(output_dir) / "parsed" if output_dir else None
        if parsed_dir:
            parsed_dir.mkdir(parents=True, exist_ok=True)

        for i, item in enumerate(scrape_results):
            url = (
                item.get("metadata", {}).get("sourceURL", "")
                or item.get("url", detail_urls[i] if i < len(detail_urls) else f"item_{i}")
            )

            # Save raw scrape
            if output_dir:
                slug = _url_to_slug(url) or f"item_{i:04d}"
                raw_path = Path(output_dir) / f"{slug}.json"
                with open(raw_path, "w") as f:
                    json.dump(item, f, indent=2)

            listing = self.parse_listing(url, item)
            if listing is None:
                summary["errors"] += 1
                print(f"[{self.BROKER_SLUG}] parse None: {url[:80]}", file=sys.stderr)
                continue

            summary["parsed"] += 1
            summary["listings"].append(listing)

            # Save parsed listing
            if parsed_dir:
                slug = _url_to_slug(url) or f"item_{i:04d}"
                parsed_path = parsed_dir / f"{slug}.json"
                from dataclasses import asdict
                with open(parsed_path, "w") as f:
                    json.dump(asdict(listing), f, indent=2, default=str)

        print(
            f"[{self.BROKER_SLUG}] run complete: "
            f"discovered={summary['discovered']} scraped={summary['scraped']} "
            f"parsed={summary['parsed']} errors={summary['errors']}"
        )
        return summary

    # ---------------------------------------------------------------------------
    # Helpers available to subclasses
    # ---------------------------------------------------------------------------

    @staticmethod
    def _dedup(urls: list[str]) -> list[str]:
        """Deduplicate a list of URLs preserving order."""
        seen: set[str] = set()
        result: list[str] = []
        for u in urls:
            if u and u not in seen:
                seen.add(u)
                result.append(u)
        return result

    @staticmethod
    def _extract_links(scraped_dict: dict) -> list[str]:
        """Pull href strings from a Firecrawl links array."""
        links_raw = scraped_dict.get("data", scraped_dict).get("links", [])
        hrefs: list[str] = []
        for item in links_raw:
            if isinstance(item, str):
                hrefs.append(item)
            elif isinstance(item, dict):
                href = item.get("href") or item.get("url", "")
                if href:
                    hrefs.append(href)
        return hrefs

    @staticmethod
    def _get_markdown(scraped_dict: dict) -> str:
        """Return the markdown string from a Firecrawl result, or empty string."""
        data = scraped_dict.get("data", scraped_dict)
        return data.get("markdown", "") or ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _url_to_slug(url: str) -> str:
    """Derive a safe filename stem from a URL."""
    import hashlib
    # Take the last two path segments
    path = url.split("?")[0].rstrip("/")
    parts = [p for p in path.split("/") if p]
    stem = "-".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "")
    # Sanitize
    slug = "".join(c if c.isalnum() or c in "-_." else "-" for c in stem)
    slug = slug[:80]
    if not slug:
        slug = hashlib.md5(url.encode()).hexdigest()[:12]
    return slug
