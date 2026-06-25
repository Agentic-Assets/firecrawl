# Domain Availability Checker - Agent Guide

## What This Does

Bulk domain availability checker that uses Playwright with stealth to scrape InstantDomainSearch.com. Bypasses Vercel bot detection via `playwright-stealth`.

## Scripts

| Script | Status | Purpose |
|--------|--------|---------|
| `check-domains-playwright.py` | **Active** | Playwright + stealth browser. Works. |
| `check-domains-optimized.py` | **Deprecated** | Firecrawl-based. Blocked by Vercel security checkpoint as of 2026-01-30. |

Historical domain candidate lists live in `ideas/`; they are not active
Firecrawl docs.

## Quick Start

```bash
# Install deps (one-time)
pip install playwright playwright-stealth
playwright install chromium

# Check domains from domains.txt
python check-domains-playwright.py

# Check from custom file
python check-domains-playwright.py -i my-list.txt

# Force re-check (skip dedup)
python check-domains-playwright.py -i my-list.txt --no-skip

# Accepted for CLI compatibility, but currently ignored by check_domains_batch
python check-domains-playwright.py -i my-list.txt -w 3
```

## How It Works

1. Reads domain list from input file (one domain per line, `#` comments allowed)
2. Loads already-checked domains from `out/results.json` to skip duplicates
3. Launches headless Chromium with `playwright-stealth` to bypass bot detection
4. For each domain, navigates to `https://instantdomainsearch.com/?q={domain}`
5. Parses page text for status indicators after the domain name line:
   - `"Continue"` -> **available**
   - `"Lookup"` / `"Make offer"` / `"WHOIS"` -> **taken**
   - `"$..."` (price) -> **taken** (premium listing)
6. Saves results to `out/runs/{timestamp}/` and appends to `out/results.json`

## Architecture

```
check-domains-playwright.py
├── parse_domains()           # Read + deduplicate input file
├── load_checked_domains()    # Skip already-checked from results.json
├── check_single_domain()     # Navigate + parse one domain
├── check_domains_batch()     # Sequential loop with single browser
└── main()                    # CLI, orchestration, output
```

**Sequential processing** (not threaded): Playwright's sync API uses greenlets
internally and cannot be used across Python threads. One browser, one context,
and one page are reused for all domains.

## Critical Code Patterns

### Page Text Parsing
```python
# Find the domain name in page text, then check the NEXT lines
for i, line in enumerate(lines):
    if stripped.lower() == domain.lower():
        for j in range(i + 1, min(i + 8, len(lines))):
            next_line = lines[j].strip().lower()
            if next_line == "continue":
                availability = "available"
                break
            elif next_line in ("lookup", "make offer", "whois"):
                availability = "taken"
                break
```

**Why `break` after first match**: The page contains multiple sections (main result, extensions, suggestions). The first occurrence of the exact domain name is the authoritative result.

### Bot Detection Bypass
```python
from playwright_stealth import Stealth
stealth = Stealth()
browser = p.chromium.launch(
    headless=True,
    args=["--disable-blink-features=AutomationControlled"],
)
context = browser.new_context(
    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...",
    viewport={"width": 1920, "height": 1080},
    locale="en-US",
)
stealth.apply_stealth_sync(context)
```

**Why stealth is needed**: InstantDomainSearch runs on Vercel, which has JavaScript-based bot detection (Code 21 challenge). Regular headless browsers and Firecrawl both get blocked. `playwright-stealth` patches the browser fingerprint to pass these checks.

## Output Structure

```
out/
├── runs/
│   └── YYYYMMDD-HHMMSS/
│       ├── results.json    # This run's full data
│       └── results.csv     # Sortable spreadsheet format
├── results.json            # Ledger of ALL runs (append-only)
└── available-domains.txt   # Deduplicated list of all available domains
```

## Known Issues

- **3-second sleep per domain**: Required for dynamic content rendering. Faster checks miss results.
- **Sequential only**: ~5 seconds per domain (3s render + delay). 100 domains is about 8 minutes.
- **`-w` / `DOMAIN_CHECK_CONCURRENCY` is API-compatible only**: the active
  `check_domains_batch()` signature accepts a worker count but ignores it.
  Do not assume parallel domain checks are happening.
- **InstantDomainSearch markup may change**: If results suddenly show all "unknown," the page structure likely changed. Inspect page text manually to update parsing logic.
- **Not authoritative**: Use WHOIS/RDAP for final purchase verification.

## Dependencies

- Python 3.10+
- `playwright` + `playwright-stealth`
- Chromium browser (installed via `playwright install chromium`)
- No Firecrawl or API keys needed

## When Making Changes

1. **Test with known domains first**: capro.ai (available), google.ai (taken)
2. **Don't add threading**: Playwright sync API + greenlets = crash
3. **Don't reduce the 3-second sleep**: Dynamic content won't render
4. **Update this file** if parsing logic or output format changes
