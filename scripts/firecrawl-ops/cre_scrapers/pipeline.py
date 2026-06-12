#!/usr/bin/env python3
"""
pipeline.py -- CRE scraping pipeline orchestrator.

Coordinates discovery and detail-page scraping across all configured CRE
brokerages. Builds on cbre_scrape.py primitives (Firecrawl HTTP helpers) and
cre_scrapers/config.py (BROKERS registry).

Each broker run is checkpointed to disk (JSON files under output_dir/checkpoints/)
so partial runs can resume without re-scraping already-fetched pages.

Supabase persistence is optional: set SUPABASE_URL + SUPABASE_SERVICE_KEY to
enable upsert; otherwise results live only in checkpoint JSON files.

Usage (from Python):
    from cre_scrapers.pipeline import CREScrapingPipeline
    pipeline = CREScrapingPipeline()
    stats = pipeline.run_broker("colliers", max_listings=50)
    print(stats)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Logging setup (module-level; callers can reconfigure root logger)
# ---------------------------------------------------------------------------
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Supabase REST helper (stdlib-only, no postgrest-py dependency)
# ---------------------------------------------------------------------------

class _SupabaseClient:
    """Minimal Supabase REST client for upsert + select. No SDK required."""

    def __init__(self, url: str, service_key: str) -> None:
        self.url = url.rstrip("/")
        self.service_key = service_key

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Prefer": "return=representation",
        }

    def _request(self, method: str, path: str, body: dict | list | None = None) -> list | dict:
        url = f"{self.url}/rest/v1{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            raise RuntimeError(f"Supabase {method} {path} -> HTTP {e.code}: {raw}") from e

    def upsert(self, table: str, rows: list[dict], on_conflict: str = "") -> list:
        """Upsert rows into `table`. Returns inserted/updated rows."""
        path = f"/{table}"
        if on_conflict:
            path += f"?on_conflict={on_conflict}"
        # Switch Prefer header to upsert resolution
        self._headers()  # base
        # POST with Prefer: resolution=merge-duplicates
        url = f"{self.url}/rest/v1{path}"
        data = json.dumps(rows).encode()
        headers = self._headers()
        headers["Prefer"] = "resolution=merge-duplicates,return=representation"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            raise RuntimeError(f"Supabase upsert /{table} -> HTTP {e.code}: {raw}") from e

    def select(self, table: str, query: str = "") -> list:
        """SELECT from `table` with optional PostgREST query string (e.g. 'active=eq.true')."""
        path = f"/{table}"
        if query:
            path += f"?{query}"
        headers = self._headers()
        headers["Prefer"] = "count=none"
        url = f"{self.url}/rest/v1{path}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            raise RuntimeError(f"Supabase select /{table} -> HTTP {e.code}: {raw}") from e

    def rpc(self, fn: str, params: dict) -> list | dict:
        """Call a Postgres function via /rest/v1/rpc/{fn}."""
        return self._request("POST", f"/rpc/{fn}", params)


# ---------------------------------------------------------------------------
# Firecrawl HTTP helpers (mirrors cbre_scrape.py pattern)
# ---------------------------------------------------------------------------

def _fc_headers(api_key: str) -> dict:
    h = {"Content-Type": "application/json"}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def _fc_post(base_url: str, api_key: str, path: str, body: dict) -> dict:
    url = f"{base_url}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=_fc_headers(api_key), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        raise RuntimeError(f"Firecrawl POST {path} -> HTTP {e.code}: {raw}") from e


def _fc_get(base_url: str, api_key: str, path: str) -> dict:
    url = f"{base_url}{path}"
    req = urllib.request.Request(url, headers=_fc_headers(api_key), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        raise RuntimeError(f"Firecrawl GET {path} -> HTTP {e.code}: {raw}") from e


# ---------------------------------------------------------------------------
# Base scraper (used when no broker-specific scraper class exists)
# ---------------------------------------------------------------------------

class BaseBrokerScraper:
    """Generic scraper that works for any broker in the BROKERS registry.

    Implements the minimal two-phase pipeline:
      1. discover() -- scrape the search_url and extract detail links
      2. scrape_detail(url) -- scrape a single listing detail page

    Broker-specific subclasses override these methods to handle pagination,
    consent walls, SPA hydration, etc.
    """

    def __init__(self, slug: str, config, firecrawl_url: str, firecrawl_key: str) -> None:
        from cre_scrapers.config import BrokerConfig  # noqa: F401  (type hint only)
        self.slug = slug
        self.config = config
        self.firecrawl_url = firecrawl_url
        self.firecrawl_key = firecrawl_key

    # ------------------------------------------------------------------
    # Firecrawl convenience wrappers
    # ------------------------------------------------------------------

    def _scrape(self, url: str, extra_opts: dict | None = None) -> dict:
        """Single-page scrape via /v2/scrape. Returns the Firecrawl response."""
        payload = {
            "url": url,
            "formats": ["markdown", "links"],
            **self.config.firecrawl_options(),
        }
        if extra_opts:
            payload.update(extra_opts)
        return _fc_post(self.firecrawl_url, self.firecrawl_key, "/v2/scrape", payload)

    def _batch_submit(self, urls: list[str]) -> str:
        """Submit a batch scrape job; return job ID."""
        payload = {
            "urls": urls,
            "formats": ["markdown", "links"],
            **self.config.firecrawl_options(),
        }
        result = _fc_post(self.firecrawl_url, self.firecrawl_key, "/v2/batch/scrape", payload)
        job_id = result.get("id") or result.get("jobId")
        if not job_id:
            raise RuntimeError(f"No job ID in batch submit response: {result}")
        return job_id

    def _batch_poll(self, job_id: str, interval: int = 8) -> dict:
        """Poll a batch job until completion; return the final result."""
        while True:
            result = _fc_get(self.firecrawl_url, self.firecrawl_key, f"/v2/batch/scrape/{job_id}")
            status = result.get("status", "unknown")
            log.debug(
                "batch %s  status=%s  %s/%s",
                job_id,
                status,
                result.get("completed", "?"),
                result.get("total", "?"),
            )
            if status in ("completed", "failed", "cancelled"):
                return result
            time.sleep(interval)

    # ------------------------------------------------------------------
    # Discovery phase
    # ------------------------------------------------------------------

    def discover(self, max_listings: int | None = None) -> list[str]:
        """Scrape the broker's search page and return listing detail URLs."""
        log.info("[%s] discover: %s", self.slug, self.config.search_url)
        result = self._scrape(self.config.search_url)
        if not result.get("success"):
            log.warning("[%s] discover scrape failed: %s", self.slug, result.get("error"))
            return []

        links_raw = result.get("data", {}).get("links", [])
        detail_urls = self._filter_detail_links(links_raw)

        if max_listings:
            detail_urls = detail_urls[:max_listings]

        log.info("[%s] discover: %d detail URLs", self.slug, len(detail_urls))
        return detail_urls

    def _filter_detail_links(self, links: list) -> list[str]:
        """Extract and deduplicate listing detail URLs from raw Firecrawl links.

        Subclasses should override this with broker-specific URL pattern matching.
        Default: return all links that contain the broker's base_url and look like
        detail pages (heuristic: longer paths, contain a numeric or slug segment).
        """
        seen: set[str] = set()
        out: list[str] = []
        base = self.config.base_url.rstrip("/")

        for item in links:
            href = item if isinstance(item, str) else item.get("href", "")
            if not href:
                continue
            # Ensure absolute URL
            if href.startswith("/"):
                href = base + href
            # Must belong to this broker's domain
            if base not in href:
                continue
            # Skip navigation, images, PDFs that are not listings
            if any(ext in href.lower() for ext in [".pdf", ".jpg", ".png", ".svg", ".css", ".js"]):
                continue
            if href not in seen:
                seen.add(href)
                out.append(href)
        return out

    # ------------------------------------------------------------------
    # Detail scrape phase
    # ------------------------------------------------------------------

    def scrape_detail(self, url: str) -> dict:
        """Scrape a single listing detail page. Returns the Firecrawl data dict."""
        result = self._scrape(url)
        if result.get("success"):
            return result.get("data", {})
        log.warning("[%s] detail scrape failed %s: %s", self.slug, url, result.get("error"))
        return {}

    def scrape_details_batch(self, urls: list[str]) -> list[dict]:
        """Batch-scrape a list of detail URLs. Returns list of data dicts."""
        if not urls:
            return []
        log.info("[%s] batch scraping %d detail pages", self.slug, len(urls))
        job_id = self._batch_submit(urls)
        log.info("[%s] batch job %s submitted", self.slug, job_id)
        result = self._batch_poll(job_id)
        items = result.get("data", [])
        log.info("[%s] batch done: %d items", self.slug, len(items))
        return items

    # ------------------------------------------------------------------
    # Listing normalization (minimal; subclasses extend)
    # ------------------------------------------------------------------

    def normalize(self, raw: dict, source_url: str) -> dict:
        """Convert a raw Firecrawl data dict into a partial cre_listings row dict."""
        metadata = raw.get("metadata", {})
        return {
            "source_url": source_url,
            "title": metadata.get("title") or metadata.get("ogTitle"),
            "description": metadata.get("description") or metadata.get("ogDescription"),
            "markdown": raw.get("markdown", ""),
            "raw_data": {
                "metadata": metadata,
                "links_count": len(raw.get("links", [])),
            },
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# Broker-specific scraper subclasses
# ---------------------------------------------------------------------------

class CBREScraper(BaseBrokerScraper):
    """CBRE scraper. Cloudflare stealth, property IDs in URL."""

    def _filter_detail_links(self, links: list) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in links:
            href = item if isinstance(item, str) else item.get("href", "")
            if "/details/" in href and "cbre.com" in href:
                if href not in seen:
                    seen.add(href)
                    out.append(href)
        return out

    def normalize(self, raw: dict, source_url: str) -> dict:
        row = super().normalize(raw, source_url)
        # Extract CBRE property ID from URL (US-SMPL-6130 pattern)
        parts = source_url.split("/details/")
        if len(parts) == 2:
            row["external_id"] = parts[1].split("/")[0]
        return row


class ColliersScraper(BaseBrokerScraper):
    """Colliers scraper. Coveo hash facets, usa{7digit} IDs."""

    def _filter_detail_links(self, links: list) -> list[str]:
        import re
        seen: set[str] = set()
        out: list[str] = []
        pattern = re.compile(r"colliers\.com/en/properties/.+/usa\d{6,}", re.IGNORECASE)
        for item in links:
            href = item if isinstance(item, str) else item.get("href", "")
            if href.startswith("/"):
                href = "https://www.colliers.com" + href
            if pattern.search(href) and href not in seen:
                seen.add(href)
                out.append(href)
        return out

    def normalize(self, raw: dict, source_url: str) -> dict:
        import re
        row = super().normalize(raw, source_url)
        m = re.search(r"(usa\d{6,})", source_url, re.IGNORECASE)
        if m:
            row["external_id"] = m.group(1).lower()
        return row


class NAIGlobalScraper(BaseBrokerScraper):
    """NAI Global scraper. CookieYes consent wall requires actions click."""

    def discover(self, max_listings: int | None = None) -> list[str]:
        # NAI Global has a consent wall; actions are already in config.actions
        return super().discover(max_listings)


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------

class CREScrapingPipeline:
    """
    Orchestrates CRE listing discovery and scraping across all brokerages.

    Responsibilities:
    - Maintain a registry of broker scraper classes (slug -> scraper).
    - Manage checkpoints (per-broker JSON files in output_dir/checkpoints/).
    - Optionally persist results to Supabase cre_listings.
    - Provide status reporting and JSONL export.

    Checkpoint format (output_dir/checkpoints/{slug}.json):
    {
        "slug": "colliers",
        "discovered": ["https://...", ...],
        "scraped": [{"url": "...", "data": {...}}, ...],
        "saved": 42,
        "last_run": "2026-06-11T14:22:00Z",
        "errors": [{"url": "...", "error": "..."}, ...]
    }
    """

    def __init__(
        self,
        firecrawl_url: str | None = None,
        supabase_url: str | None = None,
        supabase_key: str | None = None,
    ) -> None:
        self.firecrawl_url = firecrawl_url or os.environ.get(
            "FIRECRAWL_API_URL", "http://localhost:3002"
        )
        self.firecrawl_key = os.environ.get("FIRECRAWL_API_KEY", "")
        self.supabase_url = supabase_url or os.environ.get("SUPABASE_URL", "")
        self.supabase_key = supabase_key or os.environ.get("SUPABASE_SERVICE_KEY", "")

        self._db: _SupabaseClient | None = None
        if self.supabase_url and self.supabase_key:
            self._db = _SupabaseClient(self.supabase_url, self.supabase_key)

        # slug -> scraper class (populated by _init_scrapers)
        self._scraper_registry: dict[str, type[BaseBrokerScraper]] = {}
        self._init_scrapers()

    # ------------------------------------------------------------------
    # Scraper registry
    # ------------------------------------------------------------------

    def _init_scrapers(self) -> None:
        """Register all broker scraper classes. Gracefully skips missing modules."""
        # Built-in scrapers defined above
        self._scraper_registry["cbre"] = CBREScraper
        self._scraper_registry["colliers"] = ColliersScraper
        self._scraper_registry["nai-global"] = NAIGlobalScraper

        # Attempt to load optional broker-specific modules from cre_scrapers/brokers/
        brokers_pkg = Path(__file__).parent / "brokers"
        if brokers_pkg.is_dir():
            import importlib

            for mod_path in brokers_pkg.glob("*.py"):
                if mod_path.name.startswith("_"):
                    continue
                module_name = f"cre_scrapers.brokers.{mod_path.stem}"
                try:
                    mod = importlib.import_module(module_name)
                    # Convention: module exposes SLUG and SCRAPER_CLASS
                    slug = getattr(mod, "SLUG", None)
                    cls = getattr(mod, "SCRAPER_CLASS", None)
                    if slug and cls:
                        self._scraper_registry[slug] = cls
                        log.debug("Registered broker scraper: %s -> %s", slug, cls.__name__)
                except ImportError as exc:
                    log.warning("Could not import broker module %s: %s", module_name, exc)

    def _get_scraper(self, slug: str) -> BaseBrokerScraper:
        """Instantiate the scraper for a broker slug (falls back to BaseBrokerScraper)."""
        from cre_scrapers.config import get_broker

        config = get_broker(slug)
        cls = self._scraper_registry.get(slug, BaseBrokerScraper)
        return cls(slug, config, self.firecrawl_url, self.firecrawl_key)

    # ------------------------------------------------------------------
    # Checkpoint helpers
    # ------------------------------------------------------------------

    def _checkpoint_path(self, slug: str, output_dir: str) -> Path:
        cp_dir = Path(output_dir) / "checkpoints"
        cp_dir.mkdir(parents=True, exist_ok=True)
        return cp_dir / f"{slug}.json"

    def _load_checkpoint(self, slug: str, output_dir: str) -> dict:
        path = self._checkpoint_path(slug, output_dir)
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not read checkpoint %s: %s", path, exc)
        return {"slug": slug, "discovered": [], "scraped": [], "saved": 0, "errors": []}

    def _save_checkpoint(self, slug: str, output_dir: str, data: dict) -> None:
        path = self._checkpoint_path(slug, output_dir)
        data["last_run"] = datetime.now(timezone.utc).isoformat()
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    # ------------------------------------------------------------------
    # Supabase brokerage ID lookup
    # ------------------------------------------------------------------

    def _get_brokerage_id(self, slug: str) -> str | None:
        """Look up the Supabase brokerage UUID for a slug."""
        if not self._db:
            return None
        try:
            rows = self._db.select("cre_brokerages", f"slug=eq.{slug}&select=id")
            return rows[0]["id"] if rows else None
        except Exception as exc:
            log.warning("Could not look up brokerage_id for %s: %s", slug, exc)
            return None

    def _upsert_listings(self, slug: str, rows: list[dict]) -> int:
        """Upsert listing rows into Supabase cre_listings. Returns count saved."""
        if not self._db or not rows:
            return 0
        brokerage_id = self._get_brokerage_id(slug)
        if not brokerage_id:
            log.warning("[%s] brokerage_id not found in Supabase; skipping upsert", slug)
            return 0
        for row in rows:
            row["brokerage_id"] = brokerage_id
        try:
            saved = self._db.upsert(
                "cre_listings",
                rows,
                on_conflict="brokerage_id,external_id",
            )
            return len(saved) if isinstance(saved, list) else len(rows)
        except Exception as exc:
            log.error("[%s] Supabase upsert error: %s", slug, exc)
            return 0

    # ------------------------------------------------------------------
    # Single broker run
    # ------------------------------------------------------------------

    def run_broker(
        self,
        slug: str,
        max_listings: int | None = None,
        output_dir: str | None = None,
        resume: bool = True,
    ) -> dict:
        """
        Run the full scrape pipeline for one broker.

        Phases:
          1. Discover listing URLs from search page.
          2. Scrape each detail page (batch where possible).
          3. Normalize raw data to listing row dicts.
          4. Upsert to Supabase (if configured).
          5. Save checkpoint.

        Returns stats dict:
          {slug, discovered, scraped, saved, errors, duration_s}
        """
        from cre_scrapers.config import get_broker

        if output_dir is None:
            output_dir = "./output"

        t0 = time.time()
        log.info("[%s] === run_broker start ===", slug)

        # Validate slug
        try:
            cfg = get_broker(slug)
        except KeyError as exc:
            return {"slug": slug, "error": str(exc), "discovered": 0, "scraped": 0, "saved": 0}

        if not cfg.active:
            log.warning("[%s] broker is disabled (active=False); skipping", slug)
            return {
                "slug": slug,
                "skipped": True,
                "reason": "disabled",
                "discovered": 0,
                "scraped": 0,
                "saved": 0,
            }

        scraper = self._get_scraper(slug)

        # Load checkpoint for resume
        cp = self._load_checkpoint(slug, output_dir) if resume else {
            "slug": slug, "discovered": [], "scraped": [], "saved": 0, "errors": []
        }

        already_scraped_urls = {item["url"] for item in cp.get("scraped", [])}

        # --- Phase 1: Discovery ---
        if cp.get("discovered") and resume:
            log.info("[%s] resuming: %d discovered URLs from checkpoint", slug, len(cp["discovered"]))
            detail_urls = cp["discovered"]
        else:
            detail_urls = scraper.discover(max_listings=max_listings)
            cp["discovered"] = detail_urls
            self._save_checkpoint(slug, output_dir, cp)

        if not detail_urls:
            log.warning("[%s] no listing URLs discovered", slug)
            return {
                "slug": slug,
                "discovered": 0,
                "scraped": 0,
                "saved": cp.get("saved", 0),
                "errors": len(cp.get("errors", [])),
                "duration_s": round(time.time() - t0, 1),
            }

        # Apply max_listings cap after potential resume
        if max_listings and len(detail_urls) > max_listings:
            detail_urls = detail_urls[:max_listings]

        # Filter already-scraped URLs
        pending_urls = [u for u in detail_urls if u not in already_scraped_urls]
        log.info(
            "[%s] %d total discovered, %d pending (skipping %d already scraped)",
            slug, len(detail_urls), len(pending_urls), len(already_scraped_urls),
        )

        # --- Phase 2: Detail scraping (batch) ---
        errors: list[dict] = list(cp.get("errors", []))
        newly_scraped: list[dict] = []

        if pending_urls:
            try:
                raw_items = scraper.scrape_details_batch(pending_urls)
            except Exception as exc:
                log.error("[%s] batch scrape error: %s", slug, exc)
                errors.append({"phase": "batch_scrape", "error": str(exc)})
                raw_items = []

            for item in raw_items:
                url = item.get("metadata", {}).get("sourceURL", "") or item.get("url", "")
                if not url:
                    continue
                newly_scraped.append({"url": url, "data": item})

        all_scraped = list(cp.get("scraped", [])) + newly_scraped

        # --- Phase 3: Normalize ---
        listing_rows: list[dict] = []
        for entry in newly_scraped:
            try:
                row = scraper.normalize(entry["data"], entry["url"])
                listing_rows.append(row)
            except Exception as exc:
                log.warning("[%s] normalize error for %s: %s", slug, entry.get("url"), exc)
                errors.append({"url": entry.get("url"), "phase": "normalize", "error": str(exc)})

        # --- Phase 4: Supabase upsert ---
        saved_count = cp.get("saved", 0)
        if listing_rows:
            n_saved = self._upsert_listings(slug, listing_rows)
            saved_count += n_saved
            log.info("[%s] upserted %d rows to Supabase", slug, n_saved)
        else:
            log.info("[%s] no new listings to save", slug)

        # --- Phase 5: Save checkpoint ---
        cp["scraped"] = all_scraped
        cp["saved"] = saved_count
        cp["errors"] = errors
        self._save_checkpoint(slug, output_dir, cp)

        # Save raw detail pages to disk as JSON
        if newly_scraped:
            details_dir = Path(output_dir) / slug / "details"
            details_dir.mkdir(parents=True, exist_ok=True)
            for entry in newly_scraped:
                url = entry["url"]
                # Use last path segment as filename; fall back to index
                name = url.rstrip("/").split("/")[-1] or "unknown"
                name = name.replace("?", "_").replace("&", "_")[:80]
                out_path = details_dir / f"{name}.json"
                with open(out_path, "w") as f:
                    json.dump(entry["data"], f, indent=2)

        duration = round(time.time() - t0, 1)
        stats = {
            "slug": slug,
            "discovered": len(detail_urls),
            "scraped": len(all_scraped),
            "newly_scraped": len(newly_scraped),
            "saved": saved_count,
            "errors": len(errors),
            "duration_s": duration,
        }
        log.info("[%s] === run_broker done: %s ===", slug, stats)
        return stats

    # ------------------------------------------------------------------
    # Run all brokers
    # ------------------------------------------------------------------

    def run_all(
        self,
        broker_slugs: list[str] | None = None,
        max_per_broker: int | None = None,
        output_dir: str | None = None,
    ) -> dict:
        """
        Run all active brokers (or the specified subset) sequentially.

        Returns aggregate stats dict:
          {
            "total_discovered": N,
            "total_scraped": N,
            "total_saved": N,
            "total_errors": N,
            "duration_s": N,
            "brokers": {slug: per_broker_stats, ...}
          }
        """
        from cre_scrapers.config import active_brokers, BROKERS

        if output_dir is None:
            output_dir = "./output"

        if broker_slugs:
            # Validate slugs
            invalid = [s for s in broker_slugs if s not in BROKERS]
            if invalid:
                raise ValueError(f"Unknown broker slug(s): {', '.join(invalid)}")
            slugs = broker_slugs
        else:
            slugs = list(active_brokers().keys())

        log.info("run_all: %d brokers: %s", len(slugs), ", ".join(slugs))

        t0 = time.time()
        aggregate = {
            "total_discovered": 0,
            "total_scraped": 0,
            "total_saved": 0,
            "total_errors": 0,
            "brokers": {},
        }

        for i, slug in enumerate(slugs, 1):
            log.info("--- [%d/%d] %s ---", i, len(slugs), slug)
            try:
                stats = self.run_broker(slug, max_listings=max_per_broker, output_dir=output_dir)
            except Exception as exc:
                log.error("run_all: unhandled error for %s: %s", slug, exc)
                stats = {
                    "slug": slug,
                    "error": str(exc),
                    "discovered": 0,
                    "scraped": 0,
                    "saved": 0,
                    "errors": 1,
                }

            aggregate["brokers"][slug] = stats
            aggregate["total_discovered"] += stats.get("discovered", 0)
            aggregate["total_scraped"] += stats.get("scraped", 0)
            aggregate["total_saved"] += stats.get("saved", 0)
            aggregate["total_errors"] += stats.get("errors", 0)

        aggregate["duration_s"] = round(time.time() - t0, 1)
        log.info(
            "run_all complete: discovered=%d scraped=%d saved=%d errors=%d in %.1fs",
            aggregate["total_discovered"],
            aggregate["total_scraped"],
            aggregate["total_saved"],
            aggregate["total_errors"],
            aggregate["duration_s"],
        )
        return aggregate

    # ------------------------------------------------------------------
    # Status reporting
    # ------------------------------------------------------------------

    def get_status(self, output_dir: str | None = None) -> dict:
        """
        Check checkpoint files for all brokers (active and inactive).

        Returns a dict keyed by slug:
          {
            "cbre": {
              "discovered": 42,
              "scraped": 38,
              "saved": 38,
              "errors": 1,
              "last_run": "2026-06-11T14:22:00Z",
              "active": true,
              "checkpoint_exists": true
            },
            ...
          }
        """
        from cre_scrapers.config import BROKERS

        if output_dir is None:
            output_dir = "./output"

        status: dict[str, dict] = {}
        for slug, cfg in BROKERS.items():
            cp_path = self._checkpoint_path(slug, output_dir)
            if cp_path.exists():
                try:
                    with open(cp_path) as f:
                        cp = json.load(f)
                    status[slug] = {
                        "discovered": len(cp.get("discovered", [])),
                        "scraped": len(cp.get("scraped", [])),
                        "saved": cp.get("saved", 0),
                        "errors": len(cp.get("errors", [])),
                        "last_run": cp.get("last_run"),
                        "active": cfg.active,
                        "checkpoint_exists": True,
                    }
                except (json.JSONDecodeError, OSError):
                    status[slug] = {
                        "discovered": 0, "scraped": 0, "saved": 0, "errors": 0,
                        "last_run": None, "active": cfg.active, "checkpoint_exists": False,
                        "checkpoint_error": True,
                    }
            else:
                status[slug] = {
                    "discovered": 0, "scraped": 0, "saved": 0, "errors": 0,
                    "last_run": None, "active": cfg.active, "checkpoint_exists": False,
                }
        return status

    # ------------------------------------------------------------------
    # JSONL export
    # ------------------------------------------------------------------

    def export_jsonl(
        self,
        output_path: str,
        broker_slugs: list[str] | None = None,
        output_dir: str | None = None,
    ) -> int:
        """
        Export scraped listings to a JSONL file.

        Source priority:
          1. Supabase cre_listings (if DB configured and accessible).
          2. Checkpoint files in output_dir/checkpoints/ (fallback).

        Returns the count of records written.
        """
        if output_dir is None:
            output_dir = "./output"

        from cre_scrapers.config import BROKERS

        if broker_slugs:
            slugs = broker_slugs
        else:
            slugs = list(BROKERS.keys())

        count = 0
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        with open(out_path, "w") as f:
            # --- Try Supabase first ---
            if self._db:
                try:
                    for slug in slugs:
                        brokerage_id = self._get_brokerage_id(slug)
                        if not brokerage_id:
                            continue
                        offset = 0
                        page_size = 1000
                        while True:
                            query = (
                                f"brokerage_id=eq.{brokerage_id}"
                                f"&deleted_at=is.null"
                                f"&limit={page_size}&offset={offset}"
                            )
                            rows = self._db.select("cre_listings", query)
                            if not rows:
                                break
                            for row in rows:
                                f.write(json.dumps(row) + "\n")
                                count += 1
                            if len(rows) < page_size:
                                break
                            offset += page_size
                    log.info("export_jsonl: %d records from Supabase -> %s", count, out_path)
                    return count
                except Exception as exc:
                    log.warning("Supabase export failed (%s); falling back to checkpoints", exc)
                    # Reset file and retry from checkpoints
                    f.seek(0)
                    f.truncate()
                    count = 0

            # --- Fallback: checkpoint files ---
            for slug in slugs:
                cp_path = self._checkpoint_path(slug, output_dir)
                if not cp_path.exists():
                    continue
                try:
                    with open(cp_path) as cpf:
                        cp = json.load(cpf)
                    for entry in cp.get("scraped", []):
                        row = {
                            "slug": slug,
                            "source_url": entry.get("url"),
                            "scraped_at": cp.get("last_run"),
                            "data": entry.get("data", {}),
                        }
                        f.write(json.dumps(row) + "\n")
                        count += 1
                except (json.JSONDecodeError, OSError) as exc:
                    log.warning("Could not read checkpoint %s: %s", cp_path, exc)

        log.info("export_jsonl: %d records from checkpoints -> %s", count, out_path)
        return count
