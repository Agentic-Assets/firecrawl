#!/usr/bin/env python3
"""Parse Google Flights markdown output from Firecrawl into structured CSV.

Usage:
  python3 parse_flight_deals.py --input gflights-scrape.json --output deals.csv
  python3 parse_flight_deals.py --input gflights-scrape.json --format json
  
Input: Firecrawl scrape JSON with markdown content from Google Flights explore
Output: CSV or JSON with structured flight deals
"""

import argparse
import json
import re
import csv
from pathlib import Path
from datetime import datetime


def parse_google_flights_markdown(markdown: str, origin: str = "TUL") -> list[dict]:
    """Extract flight deals from Google Flights markdown.
    
    Expected format:
    1.  ### Destination
        
        Apr 9–15
        
        1 stop 13 hr 12 min
        
        $460
    """
    deals = []
    
    # Pattern matches the numbered list items with destination, dates, duration, price
    pattern = r'\d+\.\s+###\s+(.+?)\n+\s+(.+?)\n+\s+(.+?)\n+\s+\$(\d+)'
    
    matches = re.findall(pattern, markdown, re.MULTILINE | re.DOTALL)
    
    for idx, match in enumerate(matches, 1):
        destination, dates, duration_raw, price = match
        
        # Clean up extracted data
        destination = destination.strip()
        dates = dates.strip()
        price = int(price)
        
        # Parse duration/stops
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
            "origin": origin,
            "destination": destination,
            "dates": dates,
            "stops": stops,
            "duration": duration,
            "price_usd": price,
            "scraped_at": datetime.now().isoformat()
        })
    
    return deals


def load_firecrawl_json(path: Path) -> str:
    """Load markdown content from Firecrawl scrape JSON."""
    data = json.loads(path.read_text())
    
    # Handle both direct data and nested response formats
    if "data" in data and "markdown" in data["data"]:
        return data["data"]["markdown"]
    elif "markdown" in data:
        return data["markdown"]
    else:
        raise ValueError("Could not find markdown content in JSON")


def write_csv(deals: list[dict], output_path: Path):
    """Write deals to CSV."""
    if not deals:
        print("No deals found to write")
        return
    
    fieldnames = ["rank", "origin", "destination", "dates", "stops", "duration", "price_usd", "scraped_at"]
    
    with open(output_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(deals)
    
    print(f"Wrote {len(deals)} deals to {output_path}")


def write_json(deals: list[dict], output_path: Path):
    """Write deals to JSON."""
    output_path.write_text(json.dumps(deals, indent=2))
    print(f"Wrote {len(deals)} deals to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Parse Google Flights markdown into structured data")
    parser.add_argument("--input", "-i", required=True, type=Path, help="Input Firecrawl JSON file")
    parser.add_argument("--output", "-o", type=Path, help="Output file (CSV or JSON)")
    parser.add_argument("--origin", default="TUL", help="Origin airport code")
    parser.add_argument("--format", choices=["csv", "json"], default="csv", help="Output format")
    parser.add_argument("--append-history", type=Path, help="Append to price history CSV")
    args = parser.parse_args()
    
    # Load and parse
    markdown = load_firecrawl_json(args.input)
    deals = parse_google_flights_markdown(markdown, args.origin)
    
    print(f"Found {len(deals)} flight deals")
    
    # Print summary
    if deals:
        print("\nTop 5 deals:")
        for deal in deals[:5]:
            print(f"  {deal['rank']}. {deal['destination']}: ${deal['price_usd']} ({deal['stops']}, {deal['dates']})")
    
    # Write output
    if args.output:
        if args.format == "json":
            write_json(deals, args.output)
        else:
            write_csv(deals, args.output)
    
    # Append to history if requested
    if args.append_history:
        with open(args.append_history, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=deals[0].keys())
            if args.append_history.stat().st_size == 0:
                writer.writeheader()
            writer.writerows(deals)
        print(f"Appended {len(deals)} deals to {args.append_history}")


if __name__ == "__main__":
    main()
