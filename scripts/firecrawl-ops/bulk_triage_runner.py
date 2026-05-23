#!/usr/bin/env python3
"""Tiered bulk triage runner for Firecrawl scrape jobs.

Pass 1: cheap model profile (budget) should run before this script.
This script evaluates scrape quality and emits escalation batches.

Usage:
  python bulk_triage_runner.py --input urls.txt --out reports/triage.json
"""

import argparse
import json
import re
from pathlib import Path
import requests

BLOCK_PATTERNS = [r"access denied", r"forbidden", r"captcha", r"verify you are human"]


def quality(md: str, min_len: int):
    t = (md or "").lower()
    blocked = any(re.search(p, t) for p in BLOCK_PATTERNS)
    if blocked:
        return "blocked"
    if len(md or "") < min_len:
        return "low_content"
    return "ok"


def load_urls(path: Path):
    return [ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.strip().startswith("#")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:3002/v1")
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", default="triage_report.json")
    ap.add_argument("--min-len", type=int, default=1200)
    args = ap.parse_args()

    urls = load_urls(Path(args.input))
    results = []

    for u in urls:
        try:
            r = requests.post(f"{args.api}/scrape", json={"url": u, "formats": ["markdown"]}, timeout=180)
            j = r.json()
            md = (j.get("data") or {}).get("markdown", "")
            q = quality(md, args.min_len)
            results.append({
                "url": u,
                "success": j.get("success"),
                "markdown_len": len(md),
                "quality": q,
            })
        except Exception as e:
            results.append({"url": u, "success": False, "quality": "error", "error": str(e)})

    escalate_primary = [r["url"] for r in results if r["quality"] in {"low_content", "error", "blocked"}]

    report = {
        "summary": {
            "total": len(results),
            "ok": sum(1 for r in results if r["quality"] == "ok"),
            "escalate_primary": len(escalate_primary),
        },
        "results": results,
        "next_batches": {
            "escalated_deepseek_pro_batch": escalate_primary,
        },
        "notes": [
            "Run budget profile first (DeepSeek V4 Flash)",
            "Re-run escalated_deepseek_pro_batch under escalated profile (DeepSeek V4 Pro)",
        ],
    }

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
