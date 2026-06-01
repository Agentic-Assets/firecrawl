#!/usr/bin/env python3
"""CRE Platform Access Matrix Tester

Test accessibility of CRE (Commercial Real Estate) platforms via Firecrawl.
Classifies sources as accessible, blocked, or login-gated.

Usage:
  python3 cre_access_matrix.py
  python3 cre_access_matrix.py --sources news
  python3 cre_access_matrix.py --sources research --output results.json
"""

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import requests

FIRECRAWL_API = "http://localhost:3002/v2/scrape"

# CRE sources organized by category
CRE_SOURCES = {
    "brokerage": [
        "https://www.cbre.com/insights",
        "https://www.cushmanwakefield.com/",
        "https://www.jll.com/",
        "https://www.colliers.com/",
        "https://www.savills.com/",
        "https://www.marcusmillichap.com/",
        "https://www.newmark.com/",
        "https://www.knightfrank.com/",
        "https://www.befris.com/",
    ],
    "platforms": [
        "https://www.costar.com/",
        "https://www.loopnet.com/",
        "https://www.crexi.com/",
        "https://www.reonomy.com/",
        "https://www.commercialcafe.com/",
        "https://www.commercialsearch.com/",
        "https://www.cre-online.com/",
    ],
    "news": [
        "https://www.globest.com/",
        "https://www.nreionline.com/",
        "https://www.piexecutive.com/",
        "https://www.reuters.com/real-estate/",
        "https://www.wsj.com/real-estate",
        "https://www.bloomberg.com/real-estate",
    ],
    "research": [
        "https://www.greenstreet.com/",
        "https://www.ncreif.org/",
        "https://www.rca.ncreif.org/",
        "https://www.reit.com/",
        "https://www.nareit.com/",
        "https://www.prea.org/",
        "https://www.uli.org/",
        "https://www.naop.org/",
    ],
    "government": [
        "https://fred.stlouisfed.org/",
        "https://www.census.gov/construction/",
        "https://www.huduser.gov/",
        "https://www.bis.org/",
    ],
}

BLOCK_PATTERNS = [
    r"access denied",
    r"forbidden",
    r"captcha",
    r"verify you are human",
    r"cloudflare",
    r"akamai",
    r"edge suite",
    r"security verification",
    r"checking your browser",
]

LOGIN_PATTERNS = [
    r"log in",
    r"sign in",
    r"login",
    r"create account",
    r"subscribe",
    r"register",
    r"sign up",
]


def classify_access(markdown: str, status_code: int = 200) -> str:
    """Classify page accessibility based on content patterns."""
    if status_code != 200:
        return "error"
    if not markdown:
        return "empty"
    
    text = markdown.lower()
    
    # Check for blocks first
    if any(re.search(p, text) for p in BLOCK_PATTERNS):
        return "blocked"
    
    # Check for login gates
    if any(re.search(p, text) for p in LOGIN_PATTERNS):
        # But also check if there's substantial content despite login prompts
        if len(markdown) > 5000:
            return "partial"
        return "login-gated"
    
    # Check for substantial content
    if len(markdown) > 1000:
        return "accessible"
    
    return "minimal"


def test_url(url: str, timeout: int = 30) -> dict:
    """Test a single URL via Firecrawl."""
    try:
        response = requests.post(
            FIRECRAWL_API,
            json={"url": url, "formats": ["markdown"]},
            timeout=timeout,
        )
        data = response.json()
        
        if data.get("success"):
            markdown = data.get("data", {}).get("markdown", "")
            metadata = data.get("data", {}).get("metadata", {})
            status_code = metadata.get("statusCode", 200)
            
            status = classify_access(markdown, status_code)
            
            # Extract snippet
            snippet = markdown[:200].replace("\n", " ").strip() if markdown else ""
            
            return {
                "url": url,
                "status": status,
                "markdown_len": len(markdown),
                "snippet": snippet,
                "title": metadata.get("title", ""),
                "error": None,
            }
        else:
            return {
                "url": url,
                "status": "error",
                "markdown_len": 0,
                "snippet": "",
                "title": "",
                "error": data.get("error", "Unknown error"),
            }
    except Exception as e:
        return {
            "url": url,
            "status": "error",
            "markdown_len": 0,
            "snippet": "",
            "title": "",
            "error": str(e),
        }


def main():
    parser = argparse.ArgumentParser(description="CRE Platform Access Matrix Tester")
    parser.add_argument(
        "--sources",
        choices=["all", "brokerage", "platforms", "news", "research", "government"],
        default="all",
        help="Category of sources to test",
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Output file for results (JSON)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        help="Number of parallel workers",
    )
    args = parser.parse_args()
    
    # Get URLs to test
    if args.sources == "all":
        urls = []
        for category in CRE_SOURCES.values():
            urls.extend(category)
    else:
        urls = CRE_SOURCES.get(args.sources, [])
    
    if not urls:
        print("No URLs to test", file=sys.stderr)
        sys.exit(1)
    
    print(f"Testing {len(urls)} CRE sources...\n", file=sys.stderr)
    
    # Test URLs in parallel
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(test_url, url): url for url in urls}
        for future in as_completed(futures):
            results.append(future.result())
    
    # Sort by status priority then by markdown length
    status_order = {"accessible": 0, "partial": 1, "login-gated": 2, "minimal": 3, "blocked": 4, "empty": 5, "error": 6}
    results.sort(key=lambda x: (status_order.get(x["status"], 99), -x["markdown_len"]))
    
    # Print results
    print(f"{'Status':<12} {'Len':>8}  URL")
    print("-" * 70)
    
    status_counts = {}
    for r in results:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
        print(f"{r['status']:<12} {r['markdown_len']:>8}  {r['url']}")
    
    print("\n" + "=" * 70)
    print("Summary:")
    for status, count in sorted(status_counts.items()):
        emoji = {"accessible": "🟢", "partial": "🟡", "login-gated": "🟡", "blocked": "🔴", "error": "❌"}.get(status, "⚪")
        print(f"  {emoji} {status}: {count}")
    
    # Save to file if requested
    if args.output:
        output_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_urls": len(urls),
            "summary": status_counts,
            "results": results,
        }
        Path(args.output).write_text(json.dumps(output_data, indent=2))
        print(f"\nResults saved to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
