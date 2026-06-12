#!/usr/bin/env python3
"""
cbre_scrape.py — CBRE commercial real estate scraper via local Firecrawl

Uses the self-hosted Firecrawl instance at http://localhost:3002.
Requires playwright-extra stealth to be active. See:
  docs/firecrawl-ops/references/playwright-stealth-cloudflare.md

Usage:
  python3 scripts/firecrawl-ops/cbre_scrape.py scrape US-SMPL-6130
  python3 scripts/firecrawl-ops/cbre_scrape.py scrape-url https://www.cbre.com/...
  python3 scripts/firecrawl-ops/cbre_scrape.py discover
  python3 scripts/firecrawl-ops/cbre_scrape.py batch US-SMPL-6130 US-SMPL-191142 ...
  python3 scripts/firecrawl-ops/cbre_scrape.py batch-file ids.txt
  python3 scripts/firecrawl-ops/cbre_scrape.py batch-discover --out ./out/cbre
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FIRECRAWL_API_URL = os.environ.get("FIRECRAWL_API_URL", "http://localhost:3002")
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")

STEALTH_OPTIONS = {
    "proxy": "stealth",
    "waitFor": 6000,
    "timeout": 60000,
}

CBRE_DETAIL_BASE = (
    "https://www.cbre.com/properties/properties-for-lease/commercial-space/details"
)
CBRE_SEARCH_URL = (
    "https://www.cbre.com/properties/properties-for-lease/commercial-space"
)

# How long to wait between batch poll attempts (seconds)
POLL_INTERVAL = 8

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if FIRECRAWL_API_KEY:
        h["Authorization"] = f"Bearer {FIRECRAWL_API_KEY}"
    return h


def _post(path: str, body: dict) -> dict:
    url = f"{FIRECRAWL_API_URL}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            err = json.loads(body_bytes)
        except Exception:
            err = {"raw": body_bytes.decode(errors="replace")}
        print(f"[ERROR] POST {path} -> HTTP {e.code}: {json.dumps(err, indent=2)}", file=sys.stderr)
        sys.exit(1)


def _get(path: str) -> dict:
    url = f"{FIRECRAWL_API_URL}{path}"
    req = urllib.request.Request(url, headers=_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            err = json.loads(body_bytes)
        except Exception:
            err = {"raw": body_bytes.decode(errors="replace")}
        print(f"[ERROR] GET {path} -> HTTP {e.code}: {json.dumps(err, indent=2)}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------

def id_to_url(property_id: str) -> str:
    """Build a CBRE detail URL from a property ID like US-SMPL-6130."""
    # The address slug is SEO-only; CBRE routes on the ID alone.
    return f"{CBRE_DETAIL_BASE}/{property_id}/property"


# ---------------------------------------------------------------------------
# Core scrape operations
# ---------------------------------------------------------------------------

def scrape_url(url: str, formats: list = None, extra: dict = None) -> dict:
    """Scrape a single URL with stealth settings."""
    if formats is None:
        formats = ["markdown", "links"]
    payload = {
        "url": url,
        "formats": formats,
        **STEALTH_OPTIONS,
    }
    if extra:
        payload.update(extra)
    return _post("/v2/scrape", payload)


def scrape_property(property_id: str) -> dict:
    """Scrape a single CBRE property detail page."""
    url = id_to_url(property_id)
    print(f"[scrape] {property_id} -> {url}")
    result = scrape_url(url, formats=["markdown", "links"])
    if result.get("success"):
        md = result.get("data", {}).get("markdown", "")
        print(f"[scrape] OK  — {len(md)} chars of markdown")
    else:
        print(f"[scrape] FAIL — {result.get('error', result)}", file=sys.stderr)
    return result


def discover_listings(search_url: str = None, wait_for: int = 8000) -> list[str]:
    """
    Scrape the CBRE search page and extract property detail URLs.
    Returns a list of full CBRE detail URLs.
    """
    if search_url is None:
        search_url = CBRE_SEARCH_URL
    print(f"[discover] Scraping search page: {search_url}")
    payload = {
        "url": search_url,
        "formats": ["links", "markdown"],
        "proxy": "stealth",
        "waitFor": wait_for,
        "timeout": 60000,
        "onlyMainContent": False,
    }
    result = _post("/v2/scrape", payload)
    if not result.get("success"):
        print(f"[discover] FAIL — {result.get('error', result)}", file=sys.stderr)
        return []

    # Filter links to only property detail pages
    links_data = result.get("data", {}).get("links", [])
    detail_links = []
    for item in links_data:
        href = item if isinstance(item, str) else item.get("href", "")
        if "/details/" in href and "cbre.com" in href:
            detail_links.append(href)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for link in detail_links:
        if link not in seen:
            seen.add(link)
            unique.append(link)

    print(f"[discover] Found {len(unique)} property detail URLs")
    return unique


def extract_ids_from_links(links: list[str]) -> list[str]:
    """Pull property IDs (e.g. US-SMPL-6130) out of a list of CBRE detail URLs."""
    ids = []
    for link in links:
        parts = link.split("/details/")
        if len(parts) == 2:
            prop_id = parts[1].split("/")[0]
            if prop_id:
                ids.append(prop_id)
    return ids


# ---------------------------------------------------------------------------
# Batch scraping
# ---------------------------------------------------------------------------

def submit_batch(urls: list[str], formats: list = None) -> str:
    """Submit a batch scrape job and return the job ID."""
    if formats is None:
        formats = ["markdown", "links"]
    payload = {
        "urls": urls,
        "formats": formats,
        **STEALTH_OPTIONS,
    }
    result = _post("/v2/batch/scrape", payload)
    if not result.get("success") and "id" not in result:
        print(f"[batch] Submit FAIL — {result}", file=sys.stderr)
        sys.exit(1)
    job_id = result.get("id") or result.get("jobId")
    print(f"[batch] Submitted {len(urls)} URLs — job {job_id}")
    return job_id


def poll_batch(job_id: str, interval: int = POLL_INTERVAL) -> dict:
    """Poll until the batch job completes. Returns the final result object."""
    print(f"[batch] Polling job {job_id} every {interval}s …")
    while True:
        result = _get(f"/v2/batch/scrape/{job_id}")
        status = result.get("status", "unknown")
        total = result.get("total", "?")
        completed = result.get("completed", "?")
        print(f"[batch]   status={status}  {completed}/{total}", end="\r")
        if status == "completed":
            print()
            return result
        if status in ("failed", "cancelled"):
            print(f"\n[batch] Job ended with status={status}", file=sys.stderr)
            return result
        time.sleep(interval)


def batch_scrape(urls: list[str], formats: list = None) -> dict:
    """Submit + poll a batch job and return the completed result."""
    job_id = submit_batch(urls, formats)
    return poll_batch(job_id)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def save_result(result: dict, out_path: Path):
    """Write a scrape result to a JSON file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"[save] {out_path}")


def save_batch_results(batch_result: dict, out_dir: Path):
    """Write each item in a batch result to its own JSON file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    items = batch_result.get("data", [])
    print(f"[save] Writing {len(items)} items to {out_dir}")
    for i, item in enumerate(items):
        url = item.get("metadata", {}).get("sourceURL", "") or item.get("url", f"item_{i}")
        # Derive filename from property ID in URL
        name = url.split("/details/")[-1].split("/")[0] if "/details/" in url else f"item_{i:04d}"
        out_path = out_dir / f"{name}.json"
        with open(out_path, "w") as f:
            json.dump(item, f, indent=2)
    print(f"[save] Done")


def print_summary(batch_result: dict):
    """Print a brief summary of batch results to stdout."""
    items = batch_result.get("data", [])
    ok = sum(1 for x in items if x.get("markdown") or x.get("content"))
    print(f"\nBatch summary: {ok}/{len(items)} pages with content")
    for item in items:
        url = item.get("metadata", {}).get("sourceURL", item.get("url", ""))
        md = item.get("markdown", item.get("content", ""))
        status = "OK" if md else "EMPTY"
        print(f"  [{status}] {url[:80]}  ({len(md)} chars)")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def usage():
    print(__doc__)
    sys.exit(0)


def cmd_scrape(args: list):
    if not args:
        print("Usage: scrape <PROPERTY_ID>", file=sys.stderr)
        sys.exit(1)
    prop_id = args[0]
    result = scrape_property(prop_id)
    print(json.dumps(result, indent=2))


def cmd_scrape_url(args: list):
    if not args:
        print("Usage: scrape-url <URL>", file=sys.stderr)
        sys.exit(1)
    url = args[0]
    print(f"[scrape-url] {url}")
    result = scrape_url(url)
    print(json.dumps(result, indent=2))


def cmd_discover(args: list):
    # Optional: pass a custom search URL as first arg
    search_url = args[0] if args else None
    links = discover_listings(search_url)
    ids = extract_ids_from_links(links)
    print("\nDiscovered property IDs:")
    for prop_id in ids:
        print(f"  {prop_id}")
    print(f"\nFull URLs:")
    for link in links:
        print(f"  {link}")


def cmd_batch(args: list):
    if not args:
        print("Usage: batch <ID1> <ID2> ...", file=sys.stderr)
        sys.exit(1)
    urls = [id_to_url(a) for a in args]
    result = batch_scrape(urls)
    print_summary(result)
    # Optionally save
    out_dir_env = os.environ.get("CBRE_OUT_DIR")
    if out_dir_env:
        save_batch_results(result, Path(out_dir_env))


def cmd_batch_file(args: list):
    if not args:
        print("Usage: batch-file <file-with-ids-or-urls.txt>", file=sys.stderr)
        sys.exit(1)
    input_file = Path(args[0])
    lines = [l.strip() for l in input_file.read_text().splitlines() if l.strip()]
    urls = []
    for line in lines:
        if line.startswith("http"):
            urls.append(line)
        else:
            urls.append(id_to_url(line))
    print(f"[batch-file] {len(urls)} URLs from {input_file}")
    result = batch_scrape(urls)
    print_summary(result)
    out_dir_env = os.environ.get("CBRE_OUT_DIR")
    if out_dir_env:
        save_batch_results(result, Path(out_dir_env))


def cmd_batch_discover(args: list):
    """
    Full pipeline: discover URLs from the search page, then batch-scrape all of them.

    Options (as --key=value args):
      --out=./out/cbre     Directory to save results (default: ./out/cbre)
      --search=<url>       Custom search page URL
      --wait=8000          waitFor ms on the discovery scrape
    """
    out_dir = Path("./out/cbre")
    search_url = None
    wait_for = 8000

    for arg in args:
        if arg.startswith("--out="):
            out_dir = Path(arg.split("=", 1)[1])
        elif arg.startswith("--search="):
            search_url = arg.split("=", 1)[1]
        elif arg.startswith("--wait="):
            wait_for = int(arg.split("=", 1)[1])

    # Step 1: discover
    links = discover_listings(search_url, wait_for=wait_for)
    if not links:
        print("[batch-discover] No listings found on search page.", file=sys.stderr)
        sys.exit(1)

    # Save discovered links
    out_dir.mkdir(parents=True, exist_ok=True)
    links_file = out_dir / "discovered_links.json"
    with open(links_file, "w") as f:
        json.dump(links, f, indent=2)
    print(f"[batch-discover] Saved {len(links)} links to {links_file}")

    # Step 2: batch scrape
    result = batch_scrape(links)
    print_summary(result)

    # Save results
    save_batch_results(result, out_dir / "properties")
    full_result_path = out_dir / "batch_result.json"
    with open(full_result_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"[batch-discover] Full result at {full_result_path}")


COMMANDS = {
    "scrape":          cmd_scrape,
    "scrape-url":      cmd_scrape_url,
    "discover":        cmd_discover,
    "batch":           cmd_batch,
    "batch-file":      cmd_batch_file,
    "batch-discover":  cmd_batch_discover,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        usage()
    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print(f"Available: {', '.join(COMMANDS)}", file=sys.stderr)
        sys.exit(1)
    COMMANDS[cmd](sys.argv[2:])
