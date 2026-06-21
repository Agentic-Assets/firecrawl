# EQUIRE CRE Listing Intelligence System (ARCHIVED 2026-06-13)

> SUPERSEDED. The canonical architecture is now
> `../cre-intelligence-system-design.md` and the EQUIRE consumer/SQL/env/
> quick-start content was extracted into `../cre-equire-consumer-api.md`. This
> file is retained only for historical reference; its broker coverage table and
> "Colliers blocked" status are out of date. Do not cite it for current status.

> Production design for the commercial-real-estate listing pipeline that feeds
> EQUIRE's sourcing and deal-intelligence agents. Verified Firecrawl behavior as
> of 2026-06-11. Owner: EQUIRE acquisitions platform.

## 1. Executive Summary

EQUIRE turns fragmented deal evidence into source-grounded investment judgment.
Before a deal exists, the acquisitions team needs a steady, structured supply of
*candidate* properties drawn from the public broker market. This system is that
supply.

It scrapes for-sale and for-lease listings from the major national CRE
brokerages (CBRE, JLL, Cushman & Wakefield, Colliers, and others), normalizes
every listing into one canonical schema, and lands it in Supabase where EQUIRE's
agents already operate. Each listing carries its full scraped markdown (the
primary-source ledger), a structured field set that mirrors EQUIRE's
`PropertyInfo` / `AcquisitionInfo` models, and the listing broker's contact
details.

What this unlocks for EQUIRE:

- **ListingHunterAgent** scores mandate fit (`scoreMandateFit`) against a live,
  deduplicated inventory instead of re-scraping the open web every run.
- **OriginationBrief** generation seeds `askingPrice`, `address`, `city`,
  `state`, `market`, and `distressSignal` straight from a `cre_listings` row,
  with `source_url` + `markdown` as the evidence backing each fact path.
- **deal_parties** gets a broker (company, contact, email, phone) the moment a
  prospect is created, feeding outreach and broker-reliability memory.
- **MarketStrategistAgent** reads `v_cre_market_summary` for count, average
  price/PSF, and median cap rate by market and property type.
- Converting a candidate to a deal is a lookup, not a re-extraction: the listing
  already holds cap rate, NOI, price/SF, size, year built, and occupancy.

The incremental-intelligence fields the schema is tuned to capture (beyond a raw
listing page): **going-in cap rate, trailing-12 NOI, price per SF, broker
contact, occupancy, market/submarket, and year renovated** -- the signals that
most sharpen mandate-fit screening and DCF seeding.

## 2. Architecture

```
                         EQUIRE CRE Listing Intelligence Pipeline
                         =========================================

  +-----------------------------------------------------------------------+
  |  CRE BROKERAGES (public listing sites)                                |
  |  CBRE  JLL  Cushman&Wakefield  Colliers  SVN  Avison Young  NAI ...   |
  |  Cloudflare / Coveo / Liferay SPA / WordPress / CookieYes consent     |
  +-----------------------------------------------------------------------+
                                   |
                                   |  HTTPS (proxy=stealth, waitFor tuned per site)
                                   v
  +-----------------------------------------------------------------------+
  |  FIRECRAWL  (self-hosted, http://localhost:3002)                      |
  |  playwright-extra stealth engine -> clears Cloudflare Managed         |
  |  Challenge. /v2/scrape (sync) + /v2/batch/scrape (async, polled).     |
  |  Returns: markdown + links + optional JSON structured extraction.     |
  +-----------------------------------------------------------------------+
                                   |
                                   |  scrape results (markdown, links, json)
                                   v
  +-----------------------------------------------------------------------+
  |  CRE COLLECTOR  (scripts/firecrawl-ops/cre_collector/)                |
  |   collect.ts    -> multi-source collection, sale + lease              |
  |   cre_ingest.py -> staged psql upsert into credeals                   |
  |   daily script  -> healthcheck, collect, ingest, optional reconcile   |
  |   older Python scrapers remain for experiments and detail enrichment  |
  +-----------------------------------------------------------------------+
                                   |
                                   |  upsert (service-role)
                                   v
  +-----------------------------------------------------------------------+
  |  SUPABASE POSTGRES  (fhqycqubkkrdgzswccwd, credeals schema)           |
  |   cre_brokerages ---<  cre_listings ---<  cre_listing_contacts        |
  |                                      ---<  cre_listing_documents       |
  |                                      ---<  cre_listing_images          |
  |   cre_scrape_jobs ---<  cre_scrape_log                                |
  |   VIEWS: v_cre_listings_full, v_cre_active_for_sale,                  |
  |          v_cre_active_for_lease, v_cre_market_summary                 |
  |   FN:    search_cre_listings(query, city, state, type, transaction)   |
  +-----------------------------------------------------------------------+
                                   |
                                   |  SQL reads (views + search fn)
                                   v
  +-----------------------------------------------------------------------+
  |  EQUIRE AGENTS  (Vercel AI SDK v6 ToolLoopAgent)                      |
  |   ListingHunterAgent   -> scoreMandateFit, dedup, save SearchResult   |
  |   MarketStrategistAgent-> market context from v_cre_market_summary    |
  |   ProspectResearchAgent-> enrich a single candidate                   |
  |   IntakeAgent          -> OriginationBrief from a cre_listings row    |
  |   Deal assistant       -> sourcing tools over search_cre_listings()   |
  +-----------------------------------------------------------------------+
```

## 3. Broker Coverage

The production source matrix lives in `scripts/firecrawl-ops/cre_collector/`.
Latest verified all-source run:

```bash
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=all --transaction=both --max-items=0 --page-cap=400 --concurrency=3 --out=out/full_latest_2026-06-11_230423.json
```

Run metadata: started `2026-06-12T04:04:23.566Z`, finished
`2026-06-12T04:31:24.562Z`, wall clock `27:01.56`, 35,510 raw listing records,
3,878 unique brokers. Additive ingest staged 33,488 unique upsert rows with
0 missing URLs.

| Source | Active live count | Status | Notes |
|---|---:|---|---|
| CBRE | 19,028 | Active | Internal JSON API through local Firecrawl stealth, sale and lease. |
| CBRE Deal Flow | 1,836 | Active | 1,809 sale + 27 lease; full source-specific run live-ingested; gated deal rooms stay metadata-only. |
| JLL | 10,741 | Active | Public search pages, sale and lease. |
| JLL Investor | 934 | Active, complete | Sale-only; 1,857 sitemap detail URLs scanned 2026-06-12; no coordinates from Investor detail path. |
| Cushman & Wakefield | 11,318 | Active | Public /api/properties/search JSON with full pagination and detail enrichment; 2,743 sale / 8,575 lease. |
| Newmark | 4,371 | Active | Public Algolia API. State and property-type sub-splits avoid most 1,000-hit caps. Latest lease collected 3,247 of 3,250 source total. |
| Marcus & Millichap | 3,124 | Active | Public map ActivityId feed plus detail HTML; sale only; public lease unsupported. |
| Avison Young | 2,201 | Active | Full SharpLaunch feed with detail-page enrichment live-ingested 2026-06-13 00:35 UTC; 636 sale / 1,432 lease / 133 sale_or_lease; 2,571 document URLs, 31,570 image URLs, 4,128 contacts; VCards absent from the public path by design. |
| Savills | 104 | Active, limited | 101 sale + 3 lease; server-rendered US pages; partial coverage. |
| SVN | 5,287 | Active | Buildout inventory API, sale and lease; 2,660 sale / 2,192 lease / 435 sale_or_lease. |
| NAI Global | 241 | Active | 183 sale / 58 lease; public Infabode GraphQL feed and publicPost details; stable infabode ids and detail URLs. |
| Lee & Associates | 9,223 | Active | 2,611 sale / 5,691 lease / 921 sale_or_lease; complete public Buildout feed via durable page-cache assembly; source-scoped mark-missing applied. |
| Colliers | 1,172 | Active, limited | Public SalesTracker RCM GET path only (sale); main Coveo sale/lease search blocked. |
| Transwestern | 2,021 | Active | 389 sale / 1,502 lease / 130 sale_or_lease; public GET feed plus detail pages; full run live-ingested 2026-06-12. |

Known limits are accepted and documented. Do not fake coverage for sources that
only expose POST-only, authenticated, or scroll/action-dependent paths.

## 4. Database Schema

Lives in the `credeals` schema of `fhqycqubkkrdgzswccwd`, snake_case with the
`cre_` prefix (safe: only `cre_business_plan_runs` predated this). All PKs are
`uuid DEFAULT gen_random_uuid()`; timestamps are `timestamptz`.

### Tables and key fields

- **cre_brokerages** -- broker registry. `slug` (join key to Python config),
  `base_url`, `search_url`, `scrape_config` jsonb (proxy, wait_for_ms,
  timeout_ms, pagination_strategy, listing_url_pattern, notes), `active`.
- **cre_listings** -- canonical listing, one row per property. Identity
  (`brokerage_id` FK, `external_id`, `source_url`, `canonical_url`); status &
  type (`status`, `transaction_type`, `property_type`); location (address ->
  zip, county, `market`, `submarket`, lat/lng); size (`size_sf`, `lot_size_sf`,
  `available_sf`, divisibility, floors, `year_built`, `units`, parking); sale
  fields (`sale_price_usd`, `sale_price_per_sf`, `cap_rate`, `noi`,
  `gross_revenue`, `occupancy_rate`); lease fields (`lease_rate_min/max`,
  `lease_rate_type`, `term_min/max_months`); details (`description`,
  `highlights[]`, `amenities[]`, `zoning`); raw content (`markdown`,
  `raw_data` jsonb); timestamps incl. `scraped_at` and soft-delete `deleted_at`.
  - **Conventions:** money is USD numeric; `cap_rate` and `occupancy_rate` are
    fractions in `[0,1]` (0.065 = 6.5%) to match the EQUIRE valuation layer.
  - **Dedup:** `UNIQUE (brokerage_id, external_id) WHERE external_id IS NOT NULL`.
- **cre_listing_contacts** -- listing brokers/agents (`name`, `title`, `email`,
  `phone`, `brokerage_name`, `is_primary`). Feeds EQUIRE `deal_parties`.
- **cre_listing_documents** -- brochure / OM / flyer / floor_plan URLs.
- **cre_listing_images** -- photo URLs, ordered, `is_primary` hero flag.
- **cre_scrape_jobs** -- one row per run: status, discovered/scraped/saved
  counts, errors.
- **cre_scrape_log** -- one row per URL attempt: success | error | skipped |
  duplicate, http_status, error_message.

### Relationships

```
  cre_brokerages (1) ----< (N) cre_listings ----< (N) cre_listing_contacts
        |                          |          \---< (N) cre_listing_documents
        |                          |           \---< (N) cre_listing_images
        |                          |
        |                          +----< (N) cre_scrape_log (listing_id, nullable)
        |
        +----< (N) cre_scrape_jobs ----< (N) cre_scrape_log (job_id)
```

All child FKs are `ON DELETE CASCADE` from `cre_listings`; scrape-log links are
nullable so an error before a listing row exists still logs.

### Views and function (the agent contract)

- `v_cre_listings_full` -- listing + brokerage name + contacts/documents/images
  as JSON arrays. Excludes soft-deleted.
- `v_cre_active_for_sale` / `v_cre_active_for_lease` -- active listings with
  brokerage name and primary contact inlined.
- `v_cre_market_summary` -- per (city, state, property_type) counts, avg
  price/PSF/size, **median cap rate**, avg occupancy.
- `search_cre_listings(query, p_city, p_state, p_type, p_transaction)` -- FTS +
  optional filters, `ts_rank`-ordered, capped at 200.

## 5. Production Package Structure

```
scripts/firecrawl-ops/
|-- cre_collector/                 # production daily bulk path
|   |-- collect.ts                 # local Firecrawl multi-source collector
|   |-- cre_ingest.py              # collector JSON -> staged psql upsert
|   |-- cre_daily_update.sh        # healthcheck -> collect -> ingest
|   |-- START_HERE.md              # new-session status and next commands
|   |-- HANDOFF_LOG_2026-06-11.md  # latest verified run and ingest evidence
|   |-- LESSONS_2026-06-11.md      # operational lessons from this buildout
|   |-- package.json               # pinned validation deps and scripts
|   +-- tsconfig.json              # TypeScript validation config
|-- cbre_scrape.py                 # reference single-broker scraper
|-- cre_scrapers/
|   |-- ...                        # legacy Python scraper package for probes and enrichment
|-- sql/
|   |-- 000_run_all.sql            # idempotent master runner
|   |-- 001_cre_brokerages.sql     # registry seed
|   |-- 002_cre_listings.sql       # canonical listing + child tables
|   |-- 003_cre_scrape_tracking.sql# jobs + log
|   |-- 004_cre_indexes.sql        # query + FTS indexes
|   +-- 005_cre_views.sql          # views, search fn, triggers
+-- ...
```

The production bulk path now lives in `scripts/firecrawl-ops/cre_collector/`.
The older `cre_scrapers/` package remains useful for source-specific probes and
detail-page enrichment, but daily sale and lease inventory refreshes should use
`collect.ts`, `cre_ingest.py`, and `cre_daily_update.sh`.

## 6. Environment Variables

```bash
# Firecrawl (self-hosted). Matches cbre_scrape.py default.
FIRECRAWL_API_URL=http://localhost:3002
FIRECRAWL_API_KEY=                       # optional; empty for local self-hosted

# Supabase target (project fhqycqubkkrdgzswccwd)
POSTGRES_URL_NON_POOLING=                 # preferred by cre_ingest.py
POSTGRES_URL=                             # fallback accepted by cre_ingest.py

# For psql-based migration runs (Option A in 000_run_all.sql)
DATABASE_URL=postgresql://postgres:<pwd>@db.fhqycqubkkrdgzswccwd.supabase.co:5432/postgres
```

`cre_ingest.py` reads `POSTGRES_URL_NON_POOLING` or `POSTGRES_URL` from
`dynamically-display-cre-listing-data/.env.local` first, then
`CRE_EQUIRE/.env.local`, or from `--env-file`. It prints only the env file path,
never the credential value. Older `SUPABASE_SERVICE_KEY` guidance applies only
to legacy REST-loader experiments.

## 7. Quick Start

```bash
# --- 0. Firecrawl up? (firecrawl-ops skill / healthcheck) ---
scripts/firecrawl-ops/firecrawl_healthcheck.sh

# --- 1. Production collector quick probe ---
cd scripts/firecrawl-ops/cre_collector
npm install
npm run typecheck
npx tsx collect.ts --source=savills,nai-global,newmark --transaction=both --max-items=3 --page-cap=5 --concurrency=2 --out=/tmp/cre_probe.json
python3 cre_ingest.py --in /tmp/cre_probe.json --dry-run --keep-artifacts /tmp/cre_probe_ingest

# --- 2. Latest safe daily command ---
bash cre_daily_update.sh --no-mark-missing

# --- 3. Full collect only, no ingest ---
npx tsx collect.ts --source=all --transaction=both --max-items=0 --page-cap=400 --concurrency=3 --out=out/run.json
```

## 8. Agent API: Queries and Views for Deal Intelligence

EQUIRE agents read the views and call `search_cre_listings()`; they should not
query base tables directly. Examples below match the named-agent workflows.

**Mandate-fit screening (ListingHunterAgent).** Industrial for-sale in Texas,
cap rate >= 6.5%, under $20M:

```sql
SELECT id, title, address, city, state, sale_price_usd, cap_rate, noi,
       sale_price_per_sf, size_sf, primary_contact_name, primary_contact_email,
       source_url
FROM   v_cre_active_for_sale
WHERE  state = 'TX'
  AND  property_type = 'industrial'
  AND  cap_rate >= 0.065
  AND  sale_price_usd <= 20000000
ORDER BY cap_rate DESC;
```

**Keyword + filter discovery (deal-assistant sourcing tool).**

```sql
SELECT * FROM search_cre_listings('class a logistics dock high',
                                  p_state => 'TX', p_type => 'industrial',
                                  p_transaction => 'sale');
```

**Seed an OriginationBrief from a single listing (IntakeAgent).** Returns the
full evidence-bearing record including `markdown` and `raw_data`:

```sql
SELECT * FROM v_cre_listings_full WHERE id = $1;
-- map: title->subject, sale_price_usd->askingPrice, address/city/state,
--      market, source_url->_sources[*].url, markdown->evidence ledger.
```

**Market context (MarketStrategistAgent).** Where is for-sale industrial
inventory deepest and at what cap rate?

```sql
SELECT city, state, listing_count, avg_price_per_sf, median_cap_rate, avg_size_sf
FROM   v_cre_market_summary
WHERE  property_type = 'industrial' AND for_sale_count > 0
ORDER BY listing_count DESC
LIMIT 25;
```

**Populate deal_parties on conversion.** Pull the listing's primary broker:

```sql
SELECT name, title, email, phone, brokerage_name
FROM   cre_listing_contacts
WHERE  listing_id = $1
ORDER BY is_primary DESC, created_at
LIMIT 1;
-- -> credeals.deal_parties (party_type = 'broker').
```

**Run health / freshness (operator + prospecting-ops admin).**

```sql
SELECT b.name, j.status, j.listings_discovered, j.listings_scraped,
       j.listings_saved, j.errors_count, j.started_at, j.completed_at
FROM   cre_scrape_jobs j JOIN cre_brokerages b ON b.id = j.brokerage_id
ORDER BY j.started_at DESC
LIMIT 20;
```

## 9. Scale Notes

**Rate limiting.** No hard rate limit observed against the brokers in testing,
but the stealth engine is the bottleneck: budget ~15-20s per detail page.
Stagger sustained batch requests at 2-3 requests/sec to avoid IP flagging.
Raise `MAX_CONCURRENT_PAGES` in the playwright-service env for more parallelism.

**Batch size.** `/v2/batch/scrape` is async; submit in chunks of 50-100 URLs and
poll every ~8s (see `cbre_scrape.py` `POLL_INTERVAL`). Smaller chunks give
finer-grained progress for `cre_scrape_jobs` counters and let a failure abort
less work.

**Measured full-collector timing.** The latest all-source `collect.ts` run at
`--page-cap=400 --concurrency=3` took `27:01.56` and wrote a 41.6 MB JSON
artifact. Additive live ingest through `psql` took under a minute after SQL was
generated.

**Operational cadence.** Use `cre_daily_update.sh --no-mark-missing` while Colliers main Coveo sale/lease search remains blocked and Savills coverage remains partial. Lee & Associates is complete with source-scoped mark-missing applied. Use default `cre_daily_update.sh` only after a clean all-source run has no blocking source errors and the per-broker mark-missing guards are acceptable for that day.

**Document enrichment (on demand).** Do not bulk-download brochures. When a
candidate is promoted, fetch its `cre_listing_documents` URLs through Firecrawl
`/v2/parse` with the same `proxy=stealth` settings (CRE PDFs typically sit behind
the same Cloudflare layer as the listing page).
