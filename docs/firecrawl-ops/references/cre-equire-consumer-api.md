# CRE Listing Intelligence: EQUIRE Consumer API

How EQUIRE agents and the display app read the `credeals` CRE listing data, plus
the environment and quick-start commands for the collector/ingestor. This is the
consumer-facing companion to the architecture doc
(`cre-intelligence-system-design.md`), which owns the build/design decisions.

Extracted and de-staled 2026-06-13 from the superseded design doc (now at
`archive/cre-listing-system-design-2026-06-12.md`). Source-of-truth for live source status is
`scripts/firecrawl-ops/cre_collector/BROKERAGE_STATUS_2026-06-12.md`.

## 1. The agent contract (views and function)

EQUIRE agents read the views and call `search_cre_listings()`; they should not
query base tables directly. All `credeals.cre_*` base tables and `v_cre_*` views
are service-role only (`anon`/`authenticated` have schema USAGE but no SELECT);
query them from server-side code or a deliberate API layer.

- `v_cre_listings_full` -- listing + brokerage name + contacts/documents/images
  as JSON arrays. Excludes soft-deleted.
- `v_cre_active_for_sale` / `v_cre_active_for_lease` -- active listings with
  brokerage name and primary contact inlined.
- `v_cre_market_summary` -- per (city, state, property_type) counts, avg
  price/PSF/size, median cap rate, avg occupancy.
- `search_cre_listings(query, p_city, p_state, p_type, p_transaction)` -- FTS +
  optional filters, `ts_rank`-ordered, capped at 200.

## 2. Query patterns by agent workflow

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

The change-tracking layer (migration 007) has landed: recent status/price
changes are exposed through `v_cre_recent_changes` over `cre_listing_events`
(rolling 7-day window). The ledger is currently populated by the observe-only
`cre_monitor.py`; see `cre-monitor-subsystem.md` for the run model and
`cre-intelligence-system-design.md` sections 6-7 for the design.

## 3. Environment variables

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
never the credential value. Never commit these values.

## 4. Quick start

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

## 5. Scale and cadence notes

**Rate limiting.** The stealth render engine is the bottleneck for render-gated
sources: budget ~15-20s per detail page and keep sustained batches modest. Most
high-volume sources (CBRE, Cushman, Newmark, Marcus, Colliers SalesTracker,
Transwestern, the Buildout sources) enumerate through JSON/feed endpoints and do
not render per listing.

**Measured full-collector timing.** A full all-source `collect.ts` run at
`--page-cap=400 --concurrency=3` runs ~27 minutes and writes a ~40 MB JSON
artifact (colliers-main full detail enrich is the exception and runs in bounded,
resumable chunks). Additive live ingest through `psql` takes under a minute.

**Operational cadence.** Use `cre_daily_update.sh --no-mark-missing` while any
source is still being completed (currently the colliers-main full detail run and
Savills coverage). Use the default (with `--mark-missing`) only after a clean
all-source run with acceptable per-broker mark-missing guards.

**Document enrichment (on demand).** Do not bulk-download brochures. When a
candidate is promoted, fetch its `cre_listing_documents` URLs through Firecrawl
`/v2/parse` with `proxy=stealth` (CRE PDFs typically sit behind the same
Cloudflare layer as the listing page). The collector stores document and image
URLs only; it never downloads binaries into Supabase storage.
