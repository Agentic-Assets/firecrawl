#!/usr/bin/env python3
"""Quick ArtificialAnalysis snapshot via local Firecrawl API.

Outputs a compact summary of:
- leaderboard top rows (model, intelligence, blended price)
- coding/agentic benchmark highlight lines
"""

import json
import re
import requests

API = "http://localhost:3002/v1/scrape"

URLS = {
    "leaderboard": "https://artificialanalysis.ai/leaderboards/models",
    "coding": "https://artificialanalysis.ai/models/capabilities/coding",
    "terminalbench": "https://artificialanalysis.ai/evaluations/terminalbench-hard",
    "scicode": "https://artificialanalysis.ai/evaluations/scicode",
    "tau2": "https://artificialanalysis.ai/evaluations/tau2-bench",
}


def scrape(url: str) -> str:
    r = requests.post(API, json={"url": url, "formats": ["markdown"]}, timeout=180)
    r.raise_for_status()
    return (r.json().get("data") or {}).get("markdown", "")


leader = scrape(URLS["leaderboard"])
lines = leader.splitlines()
start = None
for i, l in enumerate(lines):
    if l.startswith("| Model |") and "Intelligence" in l and "Blended" in l:
        start = i
        break

rows = []
if start is not None:
    for l in lines[start + 2 : start + 14]:
        if not l.startswith("|"):
            break
        parts = [p.strip() for p in l.strip("|").split("|")]
        if len(parts) >= 5:
            model = re.sub(r"!\[[^\]]*\]\([^\)]*\)", "", parts[0]).strip()
            intelligence = parts[3].strip()
            price = parts[4].strip()
            rows.append({"model": model, "intelligence": intelligence, "blended_price": price})

highlights = {}
for key in ["terminalbench", "scicode", "tau2"]:
    md = scrape(URLS[key])
    hit = None
    for l in md.splitlines():
        if "scores the highest" in l.lower():
            hit = l.strip()
            break
    highlights[key] = hit

result = {
    "top_models": rows,
    "benchmark_highlights": highlights,
    "sources": URLS,
}

print(json.dumps(result, indent=2, ensure_ascii=False))
