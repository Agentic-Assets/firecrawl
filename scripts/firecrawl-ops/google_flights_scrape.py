#!/usr/bin/env python3
"""Multi-region Google Flights scraper using local Firecrawl.

Usage:
  python3 google_flights_scrape.py --origin "Tulsa" --regions "Hawaii,Mexico"
  python3 google_flights_scrape.py --origin "Tulsa" --regions "Europe" --output-dir ./deals
  python3 google_flights_scrape.py --origin "Tulsa" --all-regions

Requirements:
  - Firecrawl running locally on http://localhost:3002
  - See: ~/.openclaw/skills/firecrawl-ops/
"""

import argparse
import json
import re
import sys
from pathlib import Path
from datetime import datetime
from urllib.parse import quote
import requests

FIRECRAWL_API = "http://localhost:3002/v1"

DEFAULT_REGIONS = [
    "Hawaii",
    "Mexico", 
    "Europe",
    "Caribbean",
    "California",
    "New York",
    "Florida"
]


def scrape_explore_page(origin: str, region: str | None = None) -> dict:
    """Scrape Google Flights explore page for given origin/region."""
    
    # Build URL
    query = f"flights+from+{quote(origin)}"
    if region:
        query += f"+to+{quote(region)}"
    
    url = f"https://www.google.com/travel/explore?q={query}&curr=USD&hl=en"
    
    print(f"  Scraping: {region or 'All destinations'}...", file=sys.stderr)
    
    payload = {
        "url": url,
        "formats": ["markdown"],
        "waitFor": 8000,
        "onlyMainContent": False
    }
    
    try:
        response = requests.post(
            f"{FIRECRAWL_API}/scrape",
            json=payload,
            timeout=60
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"  Error scraping {region}: {e}", file=sys.stderr)
        return {"success": False, "error": str(e)}


def extract_deals(markdown: str, origin: str, region: str) -> list[dict]:
    """Extract flight deals from markdown content."""
    deals = []
    
    # Pattern matches numbered list items with destination, dates, duration, price
    pattern = r'\d+\.\s+###\s+(.+?)\n+\s+(.+?)\n+\s+(.+?)\n+\s+\$(\d+)'
    matches = re.findall(pattern, markdown, re.MULTILINE | re.DOTALL)
    
    for idx, match in enumerate(matches, 1):
        destination, dates, duration_raw, price = match
        
        # Clean up
        destination = destination.strip()
        dates = dates.strip()
        
        # Parse stops/duration
        duration_raw = duration_raw.strip()
        stops_match = re.match(r'(Nonstop|\d+ stop[s]?)\s*(.*)', duration_raw, re.IGNORECASE)
        
        if stops_match:
            stops = stops_match.group(1)
            duration = stops_match.group(2).strip() if stops_match.group(2) else ""
        else:
            stops = "Unknown"
            duration = duration_raw
        
        deals.append({
            "rank": idx,
            "search_region": region,
            "origin": origin,
            "destination": destination,
            "dates": dates,
            "stops": stops,
            "duration": duration,
            "price_usd": int(price),
            "scraped_at": datetime.now().isoformat()
        })
    
    return deals


def main():
    parser = argparse.ArgumentParser(description="Scrape Google Flights deals with Firecrawl")
    parser.add_argument("--origin", "-o", default="Tulsa", help="Origin city (default: Tulsa)")
    parser.add_argument("--regions", "-r", help="Comma-separated regions to search")
    parser.add_argument("--all-regions", "-a", action="store_true", help="Search all default regions")
    parser.add_argument("--output-dir", "-d", type=Path, default=Path("./deals"), help="Output directory")
    parser.add_argument("--save-raw", action="store_true", help="Save raw Firecrawl responses")
    args = parser.parse_args()
    
    # Determine regions to search
    if args.all_regions:
        regions = DEFAULT_REGIONS
    elif args.regions:
        regions = [r.strip() for r in args.regions.split(",")]
    else:
        regions = [None]  # Just general explore
    
    print(f"Scraping flight deals from {args.origin}...")
    print(f"Regions: {', '.join(regions) if regions[0] else 'All destinations'}")
    
    # Create output directories
    args.output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = args.output_dir / "raw"
    if args.save_raw:
        raw_dir.mkdir(exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y-%m-%d")
    all_deals = []
    
    # Scrape each region
    for region in regions:
        region_name = region or "all"
        result = scrape_explore_page(args.origin, region)
        
        if not result.get("success"):
            print(f"  Failed to scrape {region_name}")
            continue
        
        markdown = result.get("data", {}).get("markdown", "")
        
        # Save raw if requested
        if args.save_raw:
            raw_file = raw_dir / f"gflights-{timestamp}-{region_name.lower()}.json"
            raw_file.write_text(json.dumps(result, indent=2))
        
        # Extract deals
        deals = extract_deals(markdown, args.origin, region_name)
        print(f"  Found {len(deals)} deals for {region_name}")
        all_deals.extend(deals)
    
    # Write combined results
    if all_deals:
        # JSON output
        json_file = args.output_dir / f"flights-{timestamp}.json"
        json_file.write_text(json.dumps(all_deals, indent=2))
        print(f"\nWrote {len(all_deals)} total deals to {json_file}")
        
        # Summary by region
        print("\nSummary:")
        for region in regions:
            region_name = region or "all"
            region_deals = [d for d in all_deals if d["search_region"] == region_name]
            if region_deals:
                best = min(region_deals, key=lambda x: x["price_usd"])
                print(f"  {region_name}: {len(region_deals)} deals, best: {best['destination']} @ ${best['price_usd']}")
    else:
        print("\nNo deals found")
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
