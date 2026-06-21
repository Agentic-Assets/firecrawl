#!/usr/bin/env python3
"""
Find CRE brokerage domains that expose public Buildout inventory feeds.

This is a recovery-port of the stace-june20 discovery helper, adapted to the
current modular collector. It scans the source registry plus source modules for
already-wired source keys and Buildout plugin tokens before suggesting new work.
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )
}
CTX = ssl.create_default_context()

HERE = Path(__file__).resolve().parent
TYPES_TS = HERE / "types.ts"
COLLECT_TS = HERE / "collect.ts"
SOURCES_DIR = HERE / "sources"


def get(url: str, timeout: int = 12) -> str:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as response:
            return response.read().decode("utf-8", "replace")
    except Exception:
        return ""


def source_text() -> str:
    parts = []
    for path in [TYPES_TS, COLLECT_TS, *sorted(SOURCES_DIR.glob("*.ts"))]:
        if path.exists():
            parts.append(path.read_text())
    return "\n".join(parts)


def already_onboarded() -> tuple[set[str], set[str]]:
    src = source_text()
    tokens = set(re.findall(r"\b[a-f0-9]{40}\b", src))
    slugs: set[str] = set()
    if TYPES_TS.exists():
        types = TYPES_TS.read_text()
        match = re.search(r"export const SOURCE_KEYS = \[([\s\S]*?)\] as const", types)
        if match:
            slugs.update(re.findall(r'"([a-z0-9-]+)"', match.group(1)))
    return tokens, slugs


def test_token(token: str) -> dict | None:
    body = get(f"https://buildout.com/plugins/{token}/inventory.json?page=0", timeout=20)
    try:
        data = json.loads(body)
    except Exception:
        return None
    meta = data.get("meta") or {}
    inventory = data.get("inventory") or []
    total = meta.get("total")
    if not isinstance(total, int) or total <= 0:
        return None
    sale = sum(1 for item in inventory if item.get("sale") is True)
    return {"token": token, "total": total, "sale0": sale, "lease0": len(inventory) - sale}


def slugify(domain: str) -> str:
    base = re.sub(r"^www\.", "", domain).split(".")[0]
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")


def normalize_domain(domain: str) -> str | None:
    value = re.sub(r"^https?://", "", domain.strip().strip("/").lower())
    return value or None


def candidate_urls(domain: str) -> list[str]:
    return [
        f"https://www.{domain}",
        f"https://{domain}",
        f"https://www.{domain}/properties/",
        f"https://www.{domain}/listings/",
        f"https://www.{domain}/properties/for-sale/",
        f"https://www.{domain}/properties/for-lease/",
    ]


def fingerprint(domain: str) -> dict | None:
    normalized = normalize_domain(domain)
    if not normalized:
        return None

    blob = "\n".join(get(url) for url in candidate_urls(normalized))
    if not blob.strip():
        return None

    tokens = set(re.findall(r"plugins/([a-f0-9]{40})", blob))
    if "buildout" in blob.lower():
        tokens.update(re.findall(r"\b([a-f0-9]{40})\b", blob))

    feeds = []
    for token in sorted(tokens)[:12]:
        result = test_token(token)
        if result:
            feeds.append(result)
    if not feeds:
        return None

    seen = set()
    deduped = []
    for feed in sorted(feeds, key=lambda item: -item["total"]):
        if feed["token"] in seen:
            continue
        seen.add(feed["token"])
        deduped.append(feed)

    return {"domain": normalized, "slug": slugify(normalized), "feeds": deduped}


def read_domains(args: argparse.Namespace) -> list[str]:
    domains = list(args.domains)
    if args.file:
        domains.extend(
            line.strip()
            for line in Path(args.file).read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    return list(dict.fromkeys(domains))


def print_snippet(row: dict) -> None:
    feeds = row["feeds"]
    feed_text = " | ".join(
        f'{feed["total"]} rows (sale0={feed["sale0"]}, lease0={feed["lease0"]}) {feed["token"]}'
        for feed in feeds
    )
    dual = " dual-token feed" if len(feeds) > 1 else ""
    print(f'  {row["slug"]:24} {row["domain"]:28} {feed_text}{dual}')
    if len(feeds) == 1:
        feed = feeds[0]
        company = row["slug"].replace("-", " ").title()
        print("      suggested wrapper:")
        print(
            "      "
            f'return srcBuildout("{company}", "{feed["token"]}", '
            f'"https://www.{row["domain"]}/", tx, max, monitor, '
            f'{{ preferDirectJson: true, directReferer: "https://www.{row["domain"]}/", '
            f'pageConcurrency: 1, requireCompletePages: true, cacheSlug: "{row["slug"]}", '
            "usePageCache: true }});"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("domains", nargs="*")
    parser.add_argument("--domains", dest="file", help="file with one domain per line")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--json", help="write full new-result JSON here")
    args = parser.parse_args()

    domains = read_domains(args)
    if not domains:
        print("no domains given. Use --domains file.txt or pass domains as args.", file=sys.stderr)
        return 2

    known_tokens, known_slugs = already_onboarded()
    print(
        f"probing {len(domains)} domains ({len(known_tokens)} tokens, "
        f"{len(known_slugs)} source keys already onboarded)...",
        file=sys.stderr,
    )
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        results = [result for result in executor.map(fingerprint, domains) if result]

    new = []
    for result in results:
        feeds = [feed for feed in result["feeds"] if feed["token"] not in known_tokens]
        if feeds and result["slug"] not in known_slugs:
            result["feeds"] = feeds
            new.append(result)

    print(f"\n{len(new)} new Buildout firms found ({len(results)} on Buildout total):\n", file=sys.stderr)
    for row in sorted(new, key=lambda item: -max(feed["total"] for feed in item["feeds"])):
        print_snippet(row)

    if args.json:
        Path(args.json).write_text(json.dumps(new, indent=2) + "\n")
        print(f"\nwrote {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
