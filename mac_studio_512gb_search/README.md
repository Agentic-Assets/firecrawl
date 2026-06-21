# Mac Studio 512GB Search

This folder contains the live search evidence for finding a purchasable Apple Mac Studio with 512GB unified memory and at least 4TB SSD.

## Files

- `search_log.md`: every local Firecrawl search, probe, scrape, and map pass.
- `candidates.json`: exact or plausible exact-match listings with evidence.
- `rejected.json`: near misses, unavailable pages, and unverified pages with reasons.
- `final_report.md`: ranked decision report.
- `screenshots/`: rendered Browser proof for dynamic or availability-sensitive pages.
- `raw/`: raw local Firecrawl and Browser verification outputs.
- `check_inventory.py`: reusable stdlib watcher that uses the local Firecrawl API.

## Rerun

From the repo root:

```bash
python3 mac_studio_512gb_search/check_inventory.py \
  --base-url http://localhost:3002 \
  --out mac_studio_512gb_search/watcher_run.json
```

The script uses `FIRECRAWL_API_KEY` if it is present in the environment. It does not attempt purchase, login, checkout, or payment.

For dynamic pages such as Apple pickup, B&H bot checks, or eBay listing state, rerun Browser verification and save updated screenshots under `screenshots/`.
