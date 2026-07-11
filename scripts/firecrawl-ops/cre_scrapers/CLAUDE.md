# CLAUDE.md - cre_scrapers

> **STALE for production.** This entire package is legacy. It does **not** feed the
> EQUIRE board on a schedule. For any listing collect/ingest/monitor work, use
> **`../cre_collector/`** instead (see table below).

Legacy Python scraper package for broker experiments, manual probes, and archived
research notes. **Not** the production daily bulk path.

## Agent routing: ACTIVE vs STALE (read first)

**If the task is daily inventory, ingest, monitor, enrichment, or live board
counts: work in `../cre_collector/`, not here.**

| Status | Path (from `scripts/firecrawl-ops/`) | Agent use |
|--------|--------------------------------------|-----------|
| **ACTIVE** | `cre_collector/collect.ts` | Supported production collect CLI (20 source keys), not currently scheduled |
| **ACTIVE** | `cre_collector/sources/*.ts` | Per-broker adapters for the supported collector |
| **ACTIVE** | `cre_collector/cre_ingest.py` | Supabase `credeals` upsert |
| **ACTIVE** | `cre_collector/cre_daily_update.sh` | Full-refresh implementation, not currently scheduled |
| **ACTIVE** | `cre_collector/cre_monitor.py`, `cre_gate.py`, `cre_enrich.py` | Change tracking / gate / enrich |
| **STALE** | `cre_scrapers/brokers/*/scraper.py` | Manual one-off runs only; **never scheduled** |
| **STALE** | `cre_scrapers/config.py`, `pipeline.py`, `cre_pipeline.py` | Legacy config/orchestrator; **not** production status |
| **REFERENCE OK** | `cre_scrapers/brokers/*/README.md`, `archive/` | Endpoint/pagination notes; read-only context for collector work |

**Hard rules for agents:**
- Do **not** change `brokers/*/scraper.py` expecting the live board to update.
- Do **not** treat `config.py` `active=True/False` as production coverage (use
  `../cre_collector/START_HERE.md`).
- Do **not** route monitor artifacts or daily JSON through `cre_ingest.py` from here.

**Production path (the only supported daily pipeline):** broker adapters live in
`../cre_collector/sources/*.ts`, orchestrated by `../cre_collector/collect.ts`,
ingested via `../cre_collector/cre_ingest.py`, refreshed by
`../cre_collector/cre_daily_update.sh`. Monitor uses `collect.ts --monitor`
→ `cre_monitor.py` / `cre_gate.py` (never ingest).

Reference docs (read parent `../CLAUDE.md` Start Here for routing):

- `../../../docs/firecrawl-ops/references/cre-intelligence-system-design.md`
- `../../../docs/firecrawl-ops/references/cre-equire-consumer-api.md`

## Two scraper paths (legacy package only — both STALE for production)

Both paths below live inside this **stale** package. Production adapters are in
`../cre_collector/sources/*.ts` only.

1. **`base.py` broker scrapers** (preferred for broker-specific logic): each
   `brokers/<slug>/scraper.py` extends `BaseScraper` and is exposed through a
   top-level shim (`cre_scrapers/jll.py`, etc.). Run with
   `JLLScraper().run(...)` or `get_scraper("jll")` from `__init__.py`.
   `get_scraper()` registers 9 config slugs (10 registry keys; `cushman` aliases
   `cushman-wakefield`): `avison-young`, `cbre`, `colliers`, `cushman`,
   `cushman-wakefield`, `jll`, `marcus-millichap`, `nai-global`, `newmark`, `svn`
   (not `lee-associates`). Implements `discover_listings()`
   and `parse_listing()`.

2. **`pipeline.py` orchestrator** (used by `../cre_pipeline.py`): class
   `CREScrapingPipeline` checkpoints runs and optionally REST-upserts to
   Supabase when `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` are set. Only
   `cbre`, `colliers`, and `nai-global` have dedicated `BaseBrokerScraper`
   subclasses inside `pipeline.py`. All other configured slugs fall back to the
   generic `BaseBrokerScraper` stub (`discover()` on `search_url` only). For
   JLL, Cushman, Newmark, and the other rich parsers, use path (1), not
   `cre_pipeline.py run <broker>`.

## Package layout

```
cre_scrapers/
  config.py          BrokerConfig dataclass + BROKERS dict (10 slugs; legacy package only)
  normalizer.py      ListingData dataclass + field helpers + listing_to_supabase_dict()
  base.py            Abstract BaseScraper: scrape_url(), batch_scrape(), run(),
                     discover_listings(), parse_listing()
  __init__.py        __all__: BaseScraper, ListingData, BROKERS, BrokerConfig,
                     normalize_price, normalize_sqft; also defines get_scraper()
                     (not in __all__; 9 config slugs, 10 keys incl. cushman alias)
  brokers/__init__.py  Package marker (broker code lives in brokers/<slug>/)
  pipeline.py        CREScrapingPipeline + optional Supabase REST upsert
  cbre.py, jll.py, cushman.py, colliers.py, marcus_millichap.py,
  avison_young.py, svn.py, nai_global.py, newmark.py
                     Compatibility shims re-exporting brokers/*/scraper.py
  brokers/           (implemented dirs: scraper.py + README + __init__.py; some + archive/)
    cbre/            implemented
    jll/             implemented + archive/
    cushman/         implemented
    colliers/        implemented + archive/
    marcus_millichap/ implemented
    avison_young/    implemented + archive/
    svn/             implemented + archive/
    nai_global/      implemented + INFABODE_LISTING_STATUS_POLICY_2026-06-12.md
    newmark/         implemented + archive/
    lee_associates/  README + archive only (no scraper.py; prod: cre_collector Buildout)
    savills/         README + SAVILLS_US_SALE_PUBLIC_PATH_RECHECK_2026-06-12.md
                     + archive/artifacts (no scraper.py; not in config.py; prod: savills)
    transwestern/    README + archive only (no scraper.py; not in config.py; prod: transwestern)
```

Entry point CLI: `../cre_pipeline.py` (orchestrator path above; also supports
`apply-schema` for SQL migrations).

## The most important rules

**config.py is the single source of truth for this legacy package only** (10
slugs). Production collector source mapping lives in
`../cre_collector/cre_ingest.py` `SOURCE_TO_BROKERAGE` and must stay aligned
with `../cre_collector/collect.ts` `SOURCE_KEYS` and
`../sql/001_cre_brokerages.sql` (20 collector source keys folding into 17
brokerage slugs; folded sub-sources: `cbre-dealflow`, `jll-investor`,
`colliers-main`; regional Buildout keys: `matthews`, `franklin-street`, `srs`,
`hanley`, `kidder-mathews`).

**Never change `proxy` from `stealth` to `basic` for a broker** without
verifying the site is not behind Cloudflare. Stealth is safe on unprotected
sites; basic will 403 on any CF-protected site. Default to `stealth`.

**`active=False` means disabled for the legacy Python path.** Do not infer
production collector status from these flags. Current collector status is in
`../cre_collector/START_HERE.md` and `../cre_collector/BROKERAGE_STATUS_2026-06-12.md`.

**ListingData field names map to cre_listings columns.** `listing_to_supabase_dict()`
in `normalizer.py` uses `asdict()`; adding a field to `ListingData` without a
matching column in `../sql/002_cre_listings.sql` will cause a REST upsert 400
error when using the pipeline REST path.

**Date fields are provenance-sensitive.** Do not populate `listing_date` from
generic scraped, updated, or `lastUpdated` values. Use it only when the source
explicitly exposes a true first-listed/date-published/on-market field. Put
broker recency in `updated_date`; `scraped_at`, `created_at`, and `updated_at`
are our collection/database lifecycle timestamps.

## Env vars required

```bash
FIRECRAWL_API_URL=http://localhost:3002    # default; override for remote
FIRECRAWL_API_KEY=                         # empty is fine for self-hosted
SUPABASE_URL=https://fhqycqubkkrdgzswccwd.supabase.co
SUPABASE_SERVICE_KEY=...                  # legacy REST upsert via pipeline.py only; never commit
```

Direct `BaseScraper.run()` writes local JSON only unless you wire your own
persistence. Production ingest uses `../cre_collector/cre_ingest.py` with
`POSTGRES_URL_NON_POOLING` / `POSTGRES_URL`, not this REST path.

## Running scrapers

```bash
# From scripts/firecrawl-ops/

# Preferred: full broker-specific parser (base.py path)
python3 -c "
from cre_scrapers.jll import JLLScraper
from pathlib import Path
JLLScraper().run(max_listings=20, output_dir=Path('./out/jll'))
"

# Orchestrator CLI (checkpoint + optional REST upsert; specialized for
# cbre, colliers, nai-global only; other slugs use generic discovery)
python3 cre_pipeline.py run colliers --max=20 --out=./out
python3 cre_pipeline.py run-all --max=50
python3 cre_pipeline.py status
python3 cre_pipeline.py export --out=./cre_listings.jsonl
python3 cre_pipeline.py apply-schema --dry-run

# Config self-check (10 brokers)
python3 cre_scrapers/config.py

# Syntax check all modules
python3 -m compileall -q cre_scrapers && echo OK
```

## CBRE API path (faster than page scraping)

CBRE uses an internal JSON API, not page scraping. See `../prometheus/CLAUDE.md`
for the endpoint, verified curl, and response shape. Production implementation:
`../cre_collector/sources/cbre.ts`.

Check network requests via browser devtools before building a page scraper for
any large Next.js/React SPA brokerage; a similar API pattern may exist.

## Adding a new broker

1. Add a `BrokerConfig` entry to `config.py` `BROKERS` dict with verified settings.
2. Add a matching `INSERT` row to `../sql/001_cre_brokerages.sql`.
3. Create `brokers/<slug>/scraper.py` extending `BaseScraper`. Implement:
   - `discover_listings(search_url) -> list[str]`
   - `parse_listing(url, scraped_dict) -> ListingData | None`
4. Add `brokers/<slug>/README.md` with endpoint, pagination, URL, and limitation notes.
5. Add `brokers/<slug>/__init__.py` (`SLUG`, `SCRAPER_CLASS`) and a top-level shim
   such as `cre_scrapers/<slug>.py`.
6. Register the slug in `get_scraper()` inside `__init__.py`.
7. Test: `python3 -c "from cre_scrapers.<module> import <Class>; <Class>().run(max_listings=3)"`

For production daily inventory, also add a source adapter in
`../cre_collector/sources/`, register it in `../cre_collector/collect.ts`
(`SOURCE_KEYS` + `runSource`), and map it in
`../cre_collector/cre_ingest.py` `SOURCE_TO_BROKERAGE`.

## Known issues / gotchas

- **Production status lives in `../cre_collector/START_HERE.md` and
  `../cre_collector/BROKERAGE_STATUS_2026-06-12.md`.** Do not update this
  legacy package and assume the daily run changed.
- `base.py` `batch_scrape()` and `pipeline.py` `_batch_poll()` both poll every
  8s. For large batches (>200 URLs) set a longer poll timeout or submit in
  chunks of 50-100.
- **`lee-associates` is in `config.py` and `cre_pipeline.py` broker lists but
  has no `brokers/lee_associates/scraper.py` and is not registered in
  `get_scraper()`.** Production coverage is complete via the collector Buildout
  path (`../cre_collector/sources/buildout.ts`).
- **The 007 change-tracking tables (`cre_listing_events`, `cre_source_index`,
  `cre_enrichment_queue`, `cre_source_baseline`) are collector-owned and
  observe-only**, maintained by `../cre_collector/cre_monitor.py` /
  `cre_gate.py`. This legacy REST path writes `cre_listings` (+ children) only
  and must not write the monitor tables. See
  `../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md`.
