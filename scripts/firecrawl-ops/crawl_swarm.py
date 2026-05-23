#!/usr/bin/env python3
"""Simple Firecrawl swarm runner: map seeds, then scrape discovered URLs.

Usage:
  python crawl_swarm.py --seeds seeds.txt --limit 8 --scrape-per-seed 5 --out swarm_results.json
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import requests


def read_lines(path):
    return [l.strip() for l in Path(path).read_text().splitlines() if l.strip() and not l.startswith('#')]


def map_seed(api, seed, limit):
    r = requests.post(f"{api}/map", json={"url": seed, "limit": limit}, timeout=180)
    j = r.json()
    return seed, j.get("links", [])


def scrape_url(api, url):
    r = requests.post(f"{api}/scrape", json={"url": url, "formats": ["markdown"]}, timeout=180)
    j = r.json()
    md = (j.get("data") or {}).get("markdown", "")
    return {"url": url, "success": j.get("success"), "markdown_len": len(md)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:3002/v2")
    ap.add_argument("--seeds", required=True)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--scrape-per-seed", type=int, default=5)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default="swarm_results.json")
    args = ap.parse_args()

    seeds = read_lines(args.seeds)
    mapped = {}

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(map_seed, args.api, s, args.limit) for s in seeds]
        for f in as_completed(futures):
            seed, links = f.result()
            mapped[seed] = links[: args.scrape_per_seed]

    scrape_targets = sorted({u for links in mapped.values() for u in links})
    scraped = []

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(scrape_url, args.api, u) for u in scrape_targets]
        for f in as_completed(futures):
            scraped.append(f.result())

    out = {
        "seeds": seeds,
        "mapped": mapped,
        "scraped": scraped,
        "summary": {
            "seed_count": len(seeds),
            "scrape_target_count": len(scrape_targets),
            "success_count": sum(1 for x in scraped if x.get("success")),
        },
    }

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
