#!/usr/bin/env python3
"""Watch for Mac Studio listings with 512GB unified memory and 4TB+ SSD."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_BASE_URL = os.environ.get("FIRECRAWL_API_URL", "http://localhost:3002")
SEARCH_QUERIES = [
    "Mac Studio 512GB unified memory 4TB SSD",
    "Mac Studio 512GB unified memory 8TB SSD",
    "Mac Studio 512GB unified memory 16TB SSD",
    "M3 Ultra Mac Studio 512GB unified memory 4TB",
    "Refurbished Mac Studio 512GB unified memory 4TB",
    "Apple Certified Refurbished Mac Studio 512GB 8TB",
]
WATCH_URLS = [
    "https://www.apple.com/shop/buy-mac/mac-studio",
    "https://www.apple.com/shop/refurbished/mac/mac-studio",
    "https://www.cdw.com/product/apple-mac-studio-m3-ultra-512-gb-ram-8-tb-ssd/8288107",
    "https://hssl.us/apple-mac-studio-with-m3-ultra-512gb-unified-ram-4tb-ssd-apple-m3-ultra-32-core-cpu-msm4ul23/",
    "https://hssl.us/apple-mac-studio-with-m3-ultra-512gb-unified-ram-8tb-ssd-apple-m3-ultra-32-core-cpu-msm4ul24/",
    "https://hssl.us/apple-mac-studio-with-m3-ultra-512gb-unified-ram-16tb-ssd-apple-m3-ultra-32-core-cpu-msm4ul25/",
    "https://www.ebay.com/shop/apple-mac-studio?_nkw=apple+mac+studio",
]


def request_json(base_url: str, path: str, payload: dict, timeout: int) -> dict:
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def search(base_url: str, query: str, timeout: int) -> list[dict]:
    data = request_json(
        base_url,
        "/v2/search",
        {"query": query, "limit": 8, "scrapeOptions": {"formats": ["markdown"]}},
        timeout,
    )
    result = data.get("data")
    if isinstance(result, dict):
        items = [x for x in result.get("web", []) if isinstance(x, dict)]
        return [compact_search_item(x) for x in items]
    if isinstance(result, list):
        return [compact_search_item(x) for x in result if isinstance(x, dict)]
    return []


def compact_search_item(item: dict) -> dict:
    return {
        "url": item.get("url"),
        "title": item.get("title"),
        "description": item.get("description"),
    }


def scrape(base_url: str, url: str, timeout: int) -> dict:
    return request_json(
        base_url,
        "/v2/scrape",
        {"url": url, "formats": ["markdown", "links"], "onlyMainContent": False},
        timeout,
    )


def classify_text(text: str, title: str = "") -> dict:
    normalized = re.sub(r"\s+", " ", f"{title} {text}")
    title_normalized = re.sub(r"\s+", " ", title)
    lower = normalized.lower()
    title_lower = title_normalized.lower()
    has_mac_studio = "mac studio" in lower
    has_512_memory = bool(re.search(r"512\s*gb (unified memory|unified ram|ram|memory)", lower))
    storage = None
    for source in (title_lower, lower):
        for size in ("16", "8", "4"):
            if re.search(rf"{size}\s*tb (ssd|storage|hard drive|total installed storage)", source):
                storage = f"{size}TB"
                break
        if storage:
            break
    direct_buy = any(term in lower for term in ["buy it now", "add to cart", "add to bag"])
    quote_only = "request a quote" in lower
    unavailable = any(term in lower for term in ["discontinued", "sold out", "last price", "out of stock"])
    return {
        "exact_spec_signal": has_mac_studio and has_512_memory and storage is not None,
        "has_mac_studio": has_mac_studio,
        "has_512_memory": has_512_memory,
        "storage_signal": storage,
        "direct_buy_signal": direct_buy,
        "quote_only_signal": quote_only,
        "unavailable_signal": unavailable,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--out", default="watcher_run.json")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--skip-search", action="store_true")
    args = parser.parse_args()

    records: list[dict] = []
    if not args.skip_search:
        for query in SEARCH_QUERIES:
            try:
                items = search(args.base_url, query, args.timeout)
                records.append({"type": "search", "query": query, "items": items})
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                records.append({"type": "search", "query": query, "error": repr(exc)})

    for url in WATCH_URLS:
        try:
            payload = scrape(args.base_url, url, args.timeout)
            page = (payload.get("data") or {}) if isinstance(payload, dict) else {}
            text = page.get("markdown") or ""
            title = (page.get("metadata") or {}).get("title")
            records.append(
                {
                    "type": "scrape",
                    "url": url,
                    "title": title,
                    "classification": classify_text(text, title or ""),
                    "snippet": re.sub(r"\s+", " ", text[:3000]),
                }
            )
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            records.append({"type": "scrape", "url": url, "error": repr(exc)})

    output = {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "records": records}
    Path(args.out).write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
