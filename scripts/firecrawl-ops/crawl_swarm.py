#!/usr/bin/env python3
"""Simple Firecrawl swarm runner: map seeds, then scrape discovered URLs.

Usage:
  python crawl_swarm.py --seeds seeds.txt --limit 8 --scrape-per-seed 5 --out swarm_results.json
"""

import argparse
import json
import re
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.request import Request, urlopen


def read_lines(path):
    return [l.strip() for l in Path(path).read_text().splitlines() if l.strip() and not l.startswith('#')]


def link_url(item):
    if isinstance(item, str):
        return urldefrag(item)[0]
    if isinstance(item, dict):
        url = item.get("url") or item.get("link") or item.get("href")
        return urldefrag(url)[0] if url else None
    return None


def normalize_links(items):
    out = []
    seen = set()
    for item in items or []:
        url = link_url(item)
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


class AnchorParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(urljoin(self.base_url, href))


def links_from_html(base_url, html):
    parser = AnchorParser(base_url)
    parser.feed(html or "")
    return normalize_links(parser.links)


def same_domain(url, seed):
    try:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.netloc == urlparse(seed).netloc
    except ValueError:
        return False


def canonical_url_key(url):
    parsed = urlparse(urldefrag(url)[0])
    path = parsed.path.rstrip("/")
    if path.endswith("/index.php"):
        path = path[: -len("/index.php")]
    return parsed._replace(path=path, params="", query="", fragment="").geturl()


def sort_expanded_links(links, prefer_pattern):
    pattern = re.compile(prefer_pattern, re.IGNORECASE) if prefer_pattern else None
    detail_pattern = re.compile(r"/profile/|/bio/|/people/|/team/|/staff/", re.IGNORECASE)
    strong_pattern = re.compile(r"profile|bio|people|team|staff", re.IGNORECASE)

    def score(url):
        path = urlparse(url).path
        if detail_pattern.search(path):
            return 0
        if strong_pattern.search(path):
            return 1
        if pattern and pattern.search(path):
            return 2
        return 3

    return sorted(
        normalize_links(links),
        key=lambda url: (score(url), url),
    )


def post_json(url, payload, timeout=180):
    data = json.dumps(payload).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except HTTPError as e:
        body = e.read().decode(errors="replace")
        return {"success": False, "error": f"HTTP {e.code}: {body}"}
    except (URLError, TimeoutError, json.JSONDecodeError) as e:
        return {"success": False, "error": str(e)}


def map_seed(api, seed, limit):
    j = post_json(f"{api}/map", {"url": seed, "limit": limit}, timeout=180)
    return seed, normalize_links(j.get("links", []))


def scrape_url(api, url):
    j = post_json(f"{api}/scrape", {"url": url, "formats": ["markdown", "links", "rawHtml"]}, timeout=180)
    data = j.get("data") or {}
    md = data.get("markdown", "")
    html = data.get("rawHtml", "")
    links = normalize_links(data.get("links", []))
    if html:
        links = normalize_links([*links, *links_from_html(url, html)])
    return {
        "url": url,
        "success": j.get("success"),
        "markdown_len": len(md),
        "raw_html_len": len(html or ""),
        "links": links,
        "error": None if j.get("success") else j.get("error"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:3002/v2")
    ap.add_argument("--seeds", required=True)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--scrape-per-seed", type=int, default=5)
    ap.add_argument("--expand-links", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--expand-links-per-seed", type=int, default=None)
    ap.add_argument("--prefer-link-regex", default=r"profile|faculty|bio|people|team|staff")
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

    scrape_targets = sorted({u for links in mapped.values() for u in links if u})
    scraped = []

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(scrape_url, args.api, u) for u in scrape_targets]
        for f in as_completed(futures):
            scraped.append(f.result())

    expanded_targets = []
    if args.expand_links:
        per_seed = args.expand_links_per_seed or args.scrape_per_seed
        already_seen = set(scrape_targets)
        for seed, seed_links in mapped.items():
            candidates = []
            excluded = {canonical_url_key(seed), *(canonical_url_key(link) for link in seed_links)}
            for item in scraped:
                if item["url"] not in seed_links:
                    continue
                candidates.extend(
                    link
                    for link in item.get("links", [])
                    if same_domain(link, seed)
                    and link not in already_seen
                    and canonical_url_key(link) not in excluded
                )
            for link in sort_expanded_links(candidates, args.prefer_link_regex)[:per_seed]:
                already_seen.add(link)
                expanded_targets.append(link)

    expanded_scraped = []
    if expanded_targets:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = [ex.submit(scrape_url, args.api, u) for u in expanded_targets]
            for f in as_completed(futures):
                expanded_scraped.append(f.result())

    all_scraped = scraped + expanded_scraped

    out = {
        "seeds": seeds,
        "mapped": mapped,
        "scraped": all_scraped,
        "expanded_targets": expanded_targets,
        "summary": {
            "seed_count": len(seeds),
            "scrape_target_count": len(scrape_targets),
            "expanded_target_count": len(expanded_targets),
            "success_count": sum(1 for x in all_scraped if x.get("success")),
        },
    }

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
