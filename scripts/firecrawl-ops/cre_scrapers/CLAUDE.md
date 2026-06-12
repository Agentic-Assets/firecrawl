# CLAUDE.md  -  cre_scrapers

Legacy Python scraper package for the EQUIRE CRE listing intelligence pipeline.
This package is useful for broker-specific experiments and detail-page
enrichment. It is no longer the production daily bulk path. Use
`../cre_collector/collect.ts`, `../cre_collector/cre_ingest.py`, and
`../cre_collector/cre_daily_update.sh` for daily sale and lease inventory.

Full system design: `docs/firecrawl-ops/references/cre-listing-system-design.md`

## Package layout

```
cre_scrapers/
  config.py          BrokerConfig dataclass + BROKERS dict for this legacy package
  normalizer.py      ListingData dataclass + field-level helpers (price, sqft, cap rate, state)
  base.py            Abstract BaseScraper: scrape_url(), batch_scrape(), run(), save_to_supabase()
  __init__.py        Package exports: BaseScraper, ListingData, BROKERS, BrokerConfig
  cbre.py, jll.py, etc.
                    Compatibility shims for older imports
  brokers/
    cbre/            CBRE scraper code + source notes
    jll/             JLL scraper code + source notes
    cushman/         Cushman & Wakefield scraper code + source notes
    colliers/        Colliers scraper code + source notes
    marcus_millichap/
                    Marcus & Millichap scraper code + source notes
    avison_young/    Avison Young scraper code + source notes
    svn/             SVN scraper code + source notes
    nai_global/      NAI Global scraper code + source notes
    newmark/         Newmark scraper code + source notes
  pipeline.py        CREScrapingPipeline: run_all(), run_broker(), get_status(), export_jsonl()
```

Entry point CLI: `../cre_pipeline.py` (run from `scripts/firecrawl-ops/`).

## The most important rules

**config.py is the single source of truth for this legacy package only.**
Production collector source mapping lives in `../cre_collector/cre_ingest.py`
`SOURCE_TO_BROKERAGE` and must stay aligned with `../sql/001_cre_brokerages.sql`.

**Never change `proxy` from `stealth` to `basic` for a broker** without verifying
the site is not behind Cloudflare. Stealth is safe on unprotected sites; basic
will 403 on any CF-protected site. Default to `stealth`.

**`active=False` means disabled for the legacy Python path.** Do not infer
production collector status from these flags. Current collector status is in
`../cre_collector/CLAUDE.md`.

**ListingData field names map to cre_listings columns.** `listing_to_supabase_dict()`
in `normalizer.py` uses `asdict()`  -  adding a field to `ListingData` without a
matching column in `../sql/002_cre_listings.sql` will cause a REST upsert 400 error.

## Env vars required

```bash
FIRECRAWL_API_URL=http://localhost:3002    # default; override for remote
FIRECRAWL_API_KEY=                         # empty is fine for self-hosted
SUPABASE_URL=https://fhqycqubkkrdgzswccwd.supabase.co
SUPABASE_SERVICE_KEY=<service-role-key>   # legacy REST upsert only; never commit
```

## Running scrapers

```bash
# From scripts/firecrawl-ops/

# Single broker, 20 listings, save output to ./out/jll/
python3 -c "
from cre_scrapers.jll import JLLScraper
from pathlib import Path
JLLScraper().run(max_listings=20, output_dir=Path('./out/jll'))
"

# Via pipeline CLI
python3 cre_pipeline.py run jll --max=20 --out=./out
python3 cre_pipeline.py run-all --max=50
python3 cre_pipeline.py status
python3 cre_pipeline.py export --out=./cre_listings.jsonl

# Config self-check
python3 cre_scrapers/config.py
# Legacy self-check only. Production source status lives in ../cre_collector/START_HERE.md.

# Syntax check all files, including broker subfolders
python3 -m compileall -q cre_scrapers && echo OK
```

## CBRE API path (faster than page scraping)

CBRE exposes an internal listings JSON API that returns paginated structured
data for all ~5,877 US for-sale listings. This is far faster than scraping
detail pages one by one. The API endpoint is still behind Cloudflare so it
still needs Firecrawl stealth, but with `waitFor=4000` instead of 6000 and
`rawHtml` format to get the JSON body.

Reference implementation: `../prometheus/script.ts` (cloud Firecrawl version).
Production local implementation: `../cre_collector/collect.ts`.
Pre-collected reference dataset: `../prometheus/data.json` (11MB, 5,877 listings, 2026-06-11).

The same API pattern may exist for other large brokerages  -  check their
network requests via browser devtools before building a page scraper.

## Adding a new broker

1. Add a `BrokerConfig` entry to `config.py` BROKERS dict with verified settings.
2. Add a matching `INSERT` row to `../sql/001_cre_brokerages.sql`.
3. Create `brokers/<slug>/scraper.py` extending `BaseScraper`. Implement:
   - `discover_listings(search_url) -> list[str]`  -  scrape search page, return detail URLs
   - `parse_listing(url, scraped_dict) -> ListingData | None`  -  extract fields from markdown
4. Add `brokers/<slug>/README.md` with endpoint, pagination, URL, and limitation notes.
5. Add `brokers/<slug>/__init__.py` and a top-level shim such as `cre_scrapers/<slug>.py`.
6. Register in `__init__.py` exports.
7. Test: `python3 -c "from cre_scrapers.<slug> import <Class>; <Class>().run(max_listings=3)"`

## Known issues / gotchas

- **Production status lives in `../cre_collector/CLAUDE.md`.** Do not update this
  legacy package and assume the daily run changed.
- **Marcus & Millichap** works in the collector through rendered first-page cards
  under stealth with retries, but it remains limited.
- **Avison Young** works in the collector as a first-sidebar-batch source. Full
  coverage likely needs scroll actions or a public API.
- **NAI Global** works in the collector as a first-rendered-batch source with
  synthesized card ids because the widget exposes no stable per-card links.
- **Newmark** works in the collector via its public Algolia API, with state and
  property-type sub-splits to avoid the 1,000-hit cap.
- `batch_scrape()` polls every 8s. For large batches (>200 URLs) set a longer
  poll timeout or submit in chunks of 50-100.
