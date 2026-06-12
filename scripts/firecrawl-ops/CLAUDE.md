# CLAUDE.md - scripts/firecrawl-ops/

Self-hosted Firecrawl operations layer for this fork. Two distinct systems live
here: (1) the **CRE listing intelligence pipeline** that feeds EQUIRE's
ListingHunterAgent, and (2) **local Firecrawl ops tooling** for running,
configuring, and debugging the self-hosted stack.

Parent context: root `CLAUDE.md` and `.agents/skills/firecrawl-ops/` skill.

---

## Directory map

```
scripts/firecrawl-ops/
  CLAUDE.md                    ← this file
  cre_pipeline.py              Legacy Python scraper CLI entry point (run / status / export)
  cbre_scrape.py               Standalone CBRE page scraper (original single-broker tool)
  cre_access_matrix.py         Live access probe across all broker sites

  cre_scrapers/                Legacy Python scraper package (source probes / enrichment)
    CLAUDE.md                  Scraper-specific guide
    config.py                  BrokerConfig dataclass + BROKERS dict for legacy probes
    normalizer.py              ListingData dataclass + field normalizers
    base.py                    Abstract BaseScraper
    pipeline.py                CREScrapingPipeline (orchestrates all scrapers)
    cbre.py / jll.py / ...     Per-broker scrapers (10 total)

  sql/                         Supabase schema migrations
    CLAUDE.md                  SQL-specific guide
    000_run_all.sql            Master psql runner (includes CREATE SCHEMA)
    001_cre_brokerages.sql     Registry table + 10-broker seed
    002_cre_listings.sql       cre_listings + contacts/documents/images child tables
    003_cre_scrape_tracking.sql cre_scrape_jobs + cre_scrape_log
    004_cre_indexes.sql        Performance indexes (geo, FTS, GIN, price, cap rate)
    005_cre_views.sql          Agent-facing views, search_cre_listings(), updated_at triggers

  cre_collector/               PRODUCTION multi-source collector + Supabase ingestor
    CLAUDE.md                  Collector guide (source matrix, ingest semantics, daily runs)
    START_HERE.md              Current status and new-session runbook
    LESSONS_2026-06-11.md      Lessons from the verified buildout
    collect.ts                 14-source collector (local Firecrawl, sale + lease, full pagination)
    cre_ingest.py              Collector JSON -> credeals schema upserts (stdlib + psql)
    cre_daily_update.sh        Daily refresh: healthcheck -> collect -> ingest --mark-missing
    HANDOFF_LOG_2026-06-11.md  Detailed run, ingest, and Supabase evidence log

  prometheus/                  Reference Prometheus collector + pre-collected dataset
    CLAUDE.md                  Prometheus-specific guide (key: CBRE internal API)
    script.ts                  Original TypeScript collector (cloud Firecrawl SDK)
    data.json                  Pre-collected CBRE dataset (5,877 listings, 11MB, 2026-06-11)
    README.md                  Original Prometheus README
    multi_source/              Original 14-source Prometheus script (unmodified reference)
    archive/                   Original zip artifacts as delivered

  firecrawl_healthcheck.sh     Stack smoke test
  firecrawl_cli.sh             Upstream Firecrawl CLI pinned to http://localhost:3002
  firecrawl_request.py         Dependency-free HTTP helper (stdlib only)
  firecrawl_mcp.sh             MCP wrapper pinned to local API
  set_model_profile.sh         Writes OPENAI_BASE_URL + MODEL_NAME to root .env
  sync_upstream_main.sh        Safe upstream firecrawl/main merge helper
  sync_agent_skills.sh         Copies skills to ~/.agents/skills and symlinks
  install_git_hooks.sh         Installs repo git hooks

  local_firepdf_ocr.sh         Start/stop/health/smoke/doctor for local Docling OCR
  local_firepdf_ocr_service.py Fire-PDF-compatible adapter (Firecrawl -> Docling)
  local-firepdf-adapter.Dockerfile  Dockerfile for the adapter service
  pdf_ocr_benchmark.py         PDF parser matrix benchmark (modes x profiles)
  pdf_ocr_profiles.json        Named Docling OCR profile configs

  bulk_triage_runner.py        Budget-first triage with escalation batches
  crawl_swarm.py               Batch discovery / crawl workflows
  firecrawl_swarm_pipeline.py  Multi-broker swarm pipeline
  platform_access_probe.py     Access probe for web platforms
  artificialanalysis_snapshot.py  Refresh model benchmark snapshot
  google_flights_scrape.py     Atlas flight-deal scraper
  parse_flight_deals.py        Flight deal parser
  tests/                       Unit tests for local OCR service
```

---

## Part 1: CRE Listing Intelligence Pipeline

Scrapes commercial listing data from major national CRE brokerages, normalizes
it into a canonical schema, and upserts to the EQUIRE Supabase project
(`fhqycqubkkrdgzswccwd`). The data feeds EQUIRE's `ListingHunterAgent`,
`MarketStrategistAgent`, and deal origination flows.

### Database: `credeals` schema

All tables live in the `credeals` schema of `fhqycqubkkrdgzswccwd`
(same schema as the main EQUIRE application). Never use `public` for these.

| Table | Purpose |
|-------|---------|
| `credeals.cre_brokerages` | Registry of brokerages + per-site Firecrawl config |
| `credeals.cre_listings` | Canonical listing rows (one per property) |
| `credeals.cre_listing_contacts` | Broker/agent contacts per listing |
| `credeals.cre_listing_documents` | OM/brochure/flyer URLs per listing |
| `credeals.cre_listing_images` | Photo URLs per listing |
| `credeals.cre_scrape_jobs` | One row per scrape run (per broker) |
| `credeals.cre_scrape_log` | One row per URL attempt within a job |

Agent-facing objects (do not rename or drop without coordinating with CRE_EQUIRE repo):
- `credeals.v_cre_listings_full` - listing + all child data as JSON arrays
- `credeals.v_cre_active_for_sale` - active for-sale listings with primary contact
- `credeals.v_cre_active_for_lease` - active for-lease listings with primary contact
- `credeals.v_cre_market_summary` - per-(city, state, type) aggregates
- `credeals.search_cre_listings(query, city, state, type, transaction)` - FTS + filters

### Running migrations

Migrations are already applied to `fhqycqubkkrdgzswccwd`. To re-apply or apply
to a new project (run from `scripts/firecrawl-ops/sql/`):

```bash
# Option A: psql (idempotent - safe to re-run)
export DATABASE_URL='postgresql://postgres:<pwd>@db.fhqycqubkkrdgzswccwd.supabase.co:5432/postgres'
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 000_run_all.sql

# Option B: Supabase MCP - apply_migration per file in order (001 → 005)
# project_id = "fhqycqubkkrdgzswccwd"

# Option C: Supabase SQL editor - paste each file in order
```

`000_run_all.sql` includes `CREATE SCHEMA IF NOT EXISTS credeals;` so it is
safe on a fresh project. Each migration is idempotent (`IF NOT EXISTS` / `CREATE
OR REPLACE` / `ON CONFLICT DO UPDATE`).

See `sql/CLAUDE.md` for schema conventions and the full constraint reference.

### Broker status

Bulk collection now runs through `cre_collector/collect.ts` (see its
CLAUDE.md for the verified per-source matrix with sale/lease totals). The
2026-06-11 verification flipped two former blockers: Newmark works via its
public Algolia search API (creds embedded in the page), and Marcus &
Millichap works under stealth with retries.

| Broker | Slug | Status | Collection method (cre_collector) |
|--------|------|--------|-----------------------------------|
| CBRE | `cbre` | Active | Internal JSON API, stealth (sale 5.9k + lease 14.8k); also `cbre-dealflow` sub-source |
| JLL | `jll` | Active | Search pages (sale 333 + lease 4.3k); also `jll-investor` sub-source |
| Cushman & Wakefield | `cushman-wakefield` | Active (limited) | First rendered Coveo cards only; POST API blocked |
| Colliers | `colliers` | Unsupported in collector | POST-only API; Python scraper path still available |
| Marcus & Millichap | `marcus-millichap` | Active (flaky) | Stealth + 120s timeout + retries; sale-only |
| Avison Young | `avison-young` | Active (limited) | SPA sidebar; only first ~11 cards render locally |
| NAI Global | `nai-global` | Active (limited) | Infabode widget; synthesized `card:` ids, first batch only |
| Newmark | `newmark` | Active | Algolia API, per-state facets plus property-type sub-splits (latest sale 1.1k + lease 3.2k) |
| SVN | `svn` | Active | Buildout inventory API (~5.5k items) |
| Lee & Associates | `lee-associates` | Active but blocked in latest run | Buildout inventory API; latest run aborted at 12/333 failed pages due HTML interstitials |
| Savills | `savills` | Active | Server-rendered pages; sale 100 of 105 source cards, US lease empty |

### Source of truth alignment

Production collector source mapping lives in `cre_collector/cre_ingest.py`
`SOURCE_TO_BROKERAGE` and **must** match `001_cre_brokerages.sql` seed slugs.
The legacy `cre_scrapers/config.py` mapping matters only when using the older
Python scraper package.

`cap_rate` and `occupancy_rate` are stored as fractions `[0, 1]`:
6.5% = `0.065`. This matches the EQUIRE valuation layer.

### Legacy Python pipeline CLI

The daily production path is `cre_collector/`. The commands below are for the
legacy Python scraper package and source-specific experiments.

```bash
# From scripts/firecrawl-ops/

# Run all active brokers (max 100 listings each), output to ./output/
python3 cre_pipeline.py run-all --max=100 --out=./output

# Run specific brokers only
python3 cre_pipeline.py run-all --broker=cbre --broker=colliers --max=50

# Run a single broker
python3 cre_pipeline.py run colliers --max=25

# Check checkpoint status across all brokers
python3 cre_pipeline.py status

# Export all scraped listings to JSONL (local files or Supabase)
python3 cre_pipeline.py export --out=./cre_listings.jsonl

# Apply schema for the legacy REST loader
python3 cre_pipeline.py apply-schema
python3 cre_pipeline.py apply-schema --dry-run
```

### CBRE internal API (faster than page scraping)

CBRE exposes an undocumented internal listings JSON API:

```
GET https://www.cbre.com/listings-api/propertylistings/query
    ?site=us-comm&Common.Aspects=isSale&PageSize=200&Page=1
```

Response: `{ "DocumentCount": 5877, "Documents": [[...listing objects...]] }`

Still behind Cloudflare - must route through local Firecrawl with
`proxy: "stealth"` and `formats: ["rawHtml"]`. Use `waitFor: 4000` (faster
than the SPA detail pages which need 6000+).

Pre-collected dataset: `prometheus/data.json` (5,877 listings, 2026-06-11).
Reference TypeScript implementation: `prometheus/script.ts`.
Production collector implementation: `cre_collector/collect.ts`.

Other large brokerages (JLL, Colliers, Cushman) likely have similar internal
APIs - check browser DevTools network tab before building a page scraper.

### Environment variables

```bash
FIRECRAWL_API_URL=http://localhost:3002    # default; override for remote
FIRECRAWL_API_KEY=                         # empty OK for self-hosted
SUPABASE_URL=https://fhqycqubkkrdgzswccwd.supabase.co
POSTGRES_URL_NON_POOLING=postgresql://postgres:<pwd>@db.fhqycqubkkrdgzswccwd.supabase.co:5432/postgres
POSTGRES_URL=postgresql://postgres:<pwd>@db.fhqycqubkkrdgzswccwd.supabase.co:5432/postgres

# Legacy cre_pipeline.py REST upsert path only:
SUPABASE_SERVICE_KEY=<service-role-key>   # never commit
```

---

## Part 2: Local Firecrawl Ops Tooling

Wrappers and helpers for the self-hosted Firecrawl Docker stack
(OrbStack on this Mac, `http://localhost:3002`).

### Health and status

```bash
bash firecrawl_healthcheck.sh          # smoke test: API, queue, models
docker compose ps                      # container status
```

### CLI wrapper

```bash
# Scrape a page
bash firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty

# Parse a local PDF
bash firecrawl_cli.sh parse ./report.pdf --json --pretty

# Search
bash firecrawl_cli.sh search "Dallas office for sale" --limit 5 --json

# With model profile and health check
bash firecrawl_cli.sh --firecrawl-model-profile budget --firecrawl-healthcheck \
  scrape https://example.com --format summary --json
```

### Direct HTTP helper

Use `firecrawl_request.py` (stdlib only, no pip required) for advanced options
the CLI doesn't expose, or when you need saved split artifacts:

```bash
python3 firecrawl_request.py scrape https://example.com \
  --formats markdown,links --pretty --out ./out/example.json

python3 firecrawl_request.py parse ./report.pdf \
  --formats markdown,html --pdf-mode ocr --max-pages 25 --out-dir ./out/report
```

### Model profiles

```bash
bash set_model_profile.sh budget       # OpenRouter deepseek/deepseek-v4-flash (cheap)
bash set_model_profile.sh escalated    # OpenRouter deepseek/deepseek-v4-pro (smarter)
bash set_model_profile.sh openai-direct  # OpenAI gpt-5.4-mini
# After switching, recreate the API container:
docker compose up -d --force-recreate api
```

### Cloudflare bypass

For sites behind Cloudflare Managed Challenge (CBRE, others):
- `proxy: "stealth"` in the scrape request - routes through playwright-extra stealth
- `waitFor: 6000` for SPA detail pages; `waitFor: 4000` for JSON API endpoints
- Requires the `stealthProxy: true` fix in `apps/api/src/scraper/scrapeURL/engines/index.ts`
  (already applied in this fork)

Reference: `docs/firecrawl-ops/references/playwright-stealth-cloudflare.md`

### Local PDF OCR (Docling)

```bash
bash local_firepdf_ocr.sh start --profile research-page-aware
bash local_firepdf_ocr.sh health
bash local_firepdf_ocr.sh smoke ./report.pdf     # end-to-end readiness check
bash local_firepdf_ocr.sh stop

# Benchmark modes and profiles on a PDF
python3 pdf_ocr_benchmark.py ./report.pdf --modes fast,auto,ocr \
  --profiles default,research-page-aware,tables-accurate --out-dir /tmp/bench
```

Mode guidance: `fast` for born-digital text PDFs; `ocr` for scanned/image-only.
Named profiles: `pdf_ocr_profiles.json`. See `.agents/skills/firecrawl-ops/` for
full reference.

### Upstream sync

```bash
bash sync_upstream_main.sh    # merges firecrawl/main onto a branch (never main)
bash sync_agent_skills.sh     # copies skills to ~/.agents/skills
```

---

## Key references

| File | Purpose |
|------|---------|
| `docs/firecrawl-ops/references/cre-listing-system-design.md` | Full architecture doc: data model, broker matrix, agent SQL examples, scale notes |
| `docs/firecrawl-ops/references/cbre-scraping.md` | CBRE-specific scraping guide (stealth settings, URL structure, API path) |
| `docs/firecrawl-ops/references/playwright-stealth-cloudflare.md` | How the Cloudflare bypass works end to end |
| `docs/firecrawl-ops/references/model-routing.md` | Model profiles and escalation rules |
| `docs/firecrawl-ops/references/ops-playbook.md` | Health checks, logs, restart procedures |
| `cre_scrapers/CLAUDE.md` | Scraper package internals, adding brokers, known issues |
| `sql/CLAUDE.md` | Schema conventions, migration instructions, agent-facing object list |
| `prometheus/CLAUDE.md` | CBRE internal API discovery, field mapping, Python adaptation guide |
