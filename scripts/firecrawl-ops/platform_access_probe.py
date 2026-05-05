#!/usr/bin/env python3
"""Probe platform accessibility via local Firecrawl scrape endpoint.

Usage:
  python platform_access_probe.py
  python platform_access_probe.py --url https://example.com --url https://foo.com
"""

import argparse
import json
import re
from datetime import datetime, timezone
import requests

DEFAULT_URLS = [
    "https://www.costar.com/",
    "https://www.loopnet.com/",
    "https://www.crexi.com/",
    "https://www.reonomy.com/",
    "https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/tulsa-marketbeats",
]

BLOCK_PATTERNS = [
    r"access denied",
    r"forbidden",
    r"captcha",
    r"verify you are human",
    r"cloudflare",
    r"akamai",
]

LOGIN_PATTERNS = [r"log in", r"sign in", r"login", r"create account"]


def classify(text: str):
    t = (text or "").lower()
    blocked = any(re.search(p, t) for p in BLOCK_PATTERNS)
    login = any(re.search(p, t) for p in LOGIN_PATTERNS)
    if blocked:
        return "blocked"
    if login:
        return "login-gated"
    return "accessible"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:3002/v1")
    ap.add_argument("--url", action="append", default=[])
    args = ap.parse_args()

    urls = args.url or DEFAULT_URLS
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "api": args.api,
        "results": [],
    }

    for u in urls:
        try:
            r = requests.post(
                f"{args.api}/scrape",
                json={"url": u, "formats": ["markdown"]},
                timeout=180,
            )
            j = r.json()
            md = (j.get("data") or {}).get("markdown", "")
            status = classify(md)
            out["results"].append(
                {
                    "url": u,
                    "http_status": r.status_code,
                    "success": j.get("success"),
                    "markdown_len": len(md),
                    "access_status": status,
                    "snippet": md[:180].replace("\n", " "),
                }
            )
        except Exception as e:
            out["results"].append(
                {
                    "url": u,
                    "access_status": "error",
                    "error": str(e),
                }
            )

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
