# Firecrawl Ops Scripts

This folder contains the fork-owned operating layer around the self-hosted
Firecrawl stack and the CRE listing collector used by EQUIRE. It is not an
upstream Firecrawl package. Treat it as Agentic Assets local infrastructure.

Use `CLAUDE.md` for compact agent routing. Use this README when you need a
more detailed operator guide.

> **Current runtime source, 2026-07-11:** The Mac mini has no active CRE
> launchd job or healthy local Firecrawl runtime. Historical schedule and
> count claims in this guide are not a deployment record. Use the
> [operator runbook](../../tasks/2026-07-10-cre-consolidation-review/2026-07-11-firecrawl-operator-runbook.md)
> before any runtime, scheduler, database-write, or canary action.

## What Lives Here

There are two main workstreams:

1. **CRE listing intelligence.** `cre_collector/` collects public commercial
   listing inventory, stages JSON artifacts, and ingests into the EQUIRE
   Supabase project. It is the production path.
2. **Local Firecrawl operations.** The shell and Python helpers run,
   configure, and test the local Firecrawl stack, the CLI, MCP, model
   routing, and local PDF OCR.

Older exploratory scripts remain because they are useful references, but do
not treat every script here as production.

## Quick Checks

From the repo root:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
docker compose ps
```

The expected local API is:

```text
http://localhost:3002
```

This Mac uses OrbStack. If Docker commands fail, check that OrbStack is open
and that the active Docker context is `orbstack`.

## File Guide

| Path | Use |
|---|---|
| `CLAUDE.md` | Compact routing for agents |
| `README.md` | This operator guide |
| `cre_collector/` | Production collector, ingestor, monitor/gate, and enrichment worker |
| `cre_scrapers/` | Legacy Python probes and detail enrichment package |
| `sql/` | Idempotent `credeals` schema migrations |
| `prometheus/` | Original Prometheus CBRE reference implementation and dataset |
| `tests/` | Local OCR service tests |
| `firecrawl_healthcheck.sh` | Local stack smoke test |
| `firecrawl_cli.sh` | Wrapper around upstream Firecrawl CLI pinned to local API |
| `firecrawl_mcp.sh` | MCP wrapper pinned to local API |
| `firecrawl_request.py` | Stdlib HTTP helper for direct API calls, saved fields, and parse options |
| `set_model_profile.sh` | Writes local model routing values into root `.env` |
| `set_cre_resource_profile.sh` | Applies, shows, or restores the reversible local CRE resource profile |
| `sync_agent_skills.sh` | Copies repo skills into user-level skill folders |
| `sync_upstream_main.sh` | Creates an upstream-sync branch and merges `firecrawl/firecrawl:main` |
| `install_git_hooks.sh` | Installs advisory local git hooks |
| `local_firepdf_ocr.sh` | Starts, stops, checks, and smokes the local Docling OCR adapter |
| `local_firepdf_ocr_service.py` | Fire PDF compatible `/ocr` adapter |
| `local-firepdf-adapter.Dockerfile` | Container build for the OCR adapter |
| `pdf_ocr_benchmark.py` | Runs repeatable PDF parser and OCR comparisons |
| `pdf_ocr_profiles.json` | Named local OCR profile settings |
| `cre_pipeline.py`, `cre_access_matrix.py`, … | Legacy reference scripts (see `CLAUDE.md` directory map) |

## CRE Listing Collector

Current production path:

```bash
cd scripts/firecrawl-ops/cre_collector
```

Fresh machine? Run `bash cre_setup.sh` first (see `SETUP.md`).

Read these before making claims or changing behavior:

1. `START_HERE.md` (live counts, status matrix, launchd run-health, next steps)
2. `CLAUDE.md` (collector orchestration and ingest contract)
3. `BROKERAGE_STATUS_2026-06-12.md`
4. `../../docs/firecrawl-ops/references/cre-intelligence-system-design.md`
5. `../../docs/firecrawl-ops/references/cre-equire-consumer-api.md`
6. `../../docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md`
7. `../../docs/firecrawl-ops/references/cre-monitor-subsystem.md`
8. `HANDOFF_MONITOR_FIRST_APPLY_2026-06-13.md` (when touching monitor layer or 007 tables)
9. `ENRICHMENT_WORKER_DESIGN_2026-06-15.md` (when touching `cre_enrich.py`, enrich launchd, or `sql/010`)

Historical validation and handoff detail lives in `archive/` (see `archive/README.md`).

Common commands:

```bash
npm run typecheck

npx tsx collect.ts --source=svn --transaction=both --max-items=6 --out=/tmp/svn-probe.json
python3 cre_ingest.py --in /tmp/svn-probe.json --dry-run

npx tsx collect.ts --source=all --transaction=both --max-items=0 \
  --page-cap=400 --concurrency=3 --out=out/full-run.json

python3 cre_ingest.py --in out/full-run.json
bash cre_daily_update.sh --no-mark-missing
bash cre_status.sh               # run-health heartbeat (schedules, last runs, staleness)
```

Use `--mark-missing` only when every relevant source pass completed cleanly
and staged enough rows for the per-broker guardrails. While Savills sale stays
structurally capped (no public US commercial-sale feed), keep the ingest additive
(`--no-mark-missing`). The collector registers **51 source keys** in
`collect.ts` / `types.ts`; live per-source status is in `START_HERE.md`.

Live row counts, baseline artifacts, and per-source status belong in
`cre_collector/START_HERE.md`. Do not quote coverage from memory or from
`cre_scrapers/config.py`.

## CRE Database Target

Target Supabase project:

```text
fhqycqubkkrdgzswccwd
```

Target schema:

```text
credeals
```

Core objects:

| Object | Purpose |
|---|---|
| `cre_brokerages` | Broker registry and scrape configuration |
| `cre_listings` | Canonical listing rows |
| `cre_listing_contacts` | Broker and agent contacts |
| `cre_listing_documents` | OM, brochure, flyer, and related source URLs |
| `cre_listing_images` | Source image URLs |
| `cre_scrape_jobs` | One row per scrape run |
| `cre_scrape_log` | One row per attempted source URL |
| `cre_listing_events` | Append-only change ledger (007) |
| `cre_source_index` | Per-source enumeration snapshot + prior price columns (007+009) |
| `cre_enrichment_queue` | Tier-B detail-render work queue (007; drained by `cre_enrich.py`) |
| `cre_source_baseline` | Per-source coverage health baseline (007) |
| `v_cre_enrichment_queue_pending` | Enrichment queue health view (010) |
| `v_cre_enrichment_dead` | Dead-letter enrichment rows (010) |
| `cre_listing_price_history` | Append-only watched-field snapshots (009) |
| `cre_listing_contacts_archive` | Contacts snapshot at retirement (009) |
| `cre_listing_documents_archive` | Documents snapshot at retirement (009) |
| `cre_listing_media` | Detail media URLs (011) |
| `cre_listing_links` | Detail link URLs (011) |
| `cre_listing_om_facts` | Shared OM/PDF underwriting facts (013; GetCREdata is the sole production writer, and local `om_parse.py --apply` fails closed) |
| `cre_zip_cbsa_crosswalk` | Offline ZIP to county/CBSA reference (014) |
| `v_cre_listings_full` | Listing plus child data as JSON arrays |
| `v_cre_active_for_sale` | Active for-sale listings |
| `v_cre_active_for_lease` | Active for-lease listings |
| `v_cre_market_summary` | Per-market aggregates |
| `v_cre_recent_changes` | Seven-day change ledger feed (007+005) |
| `search_cre_listings(...)` | Full text search plus filters |

The collector stores source URLs for documents and images. It does not
download source PDFs or image binaries into Supabase storage.

The ingestor reads `POSTGRES_URL_NON_POOLING` or `POSTGRES_URL` from EQUIRE
environment files (`--env-file`, then `CRE_ENV_FILE`, then `~/Documents/...`
defaults) and shells out to `psql`. It must never print or persist credential
values. On this Mac, launchd sets `CRE_ENV_FILE=~/.config/cre/equire.env`.

Change tracking (007) runs through a separate observe-only path:
`collect.ts --monitor` feeds `cre_monitor.py` and `cre_gate.py`, **never**
`cre_ingest.py` (sparse monitor rows would wipe enriched prices, `raw_data`,
and child tables). Status activation in `cre_ingest.py` is OPT-IN default-off
(`--activate-status` / `CRE_ACTIVATE_STATUS=1`). See
`../../docs/firecrawl-ops/references/cre-monitor-subsystem.md`.

### Monitor, enrichment, and automation

The observe-only monitor layer (007 tables) is shipped: `collect.ts --monitor`
feeds `cre_monitor.py` only (never `cre_ingest.py`). `cre_gate.py` is wired
into `cre_daily_update.sh` as observe-only step [3/4] with a strict
`--no-mark-missing` fail-safe. The Tier-B worker `cre_enrich.py` drains
`cre_enrichment_queue` via `collect.ts --enrich-input` and additive re-ingest.

**Live ops status** (schedules, last-run verdicts, gated cutovers) belongs in
`cre_collector/START_HERE.md`. Run `bash cre_status.sh` before quoting
run-health. Migrations `009` through `014` are applied to prod; local OM
parsing is retired and fails closed. Live status activation, consumer board-gate
deploy, media backfill, and weekly mark-missing escalation
(`CRE_WEEKLY_MARK_MISSING=1`) stay gated for explicit go-ahead.

| Module | Role |
|---|---|
| `collect.ts` | CLI entry; orchestrates source runs, `--monitor`, and `--enrich-input` |
| `types.ts` | Shared listing types and `SourceResult` contract |
| `lib/` | `config`, `scrape`, `harvest`, `parse`, `geo`, `enrich`, `broker`, `html`, `util` |
| `sources/*.ts` | Per-broker adapters (one file per source key) |
| `cre_ingest.py` | Full artifact upsert into `cre_listings` (+ children) |
| `cre_monitor.py` | Observe-only diff/events/index (007 tables) |
| `cre_gate.py` | Per-source coverage baseline and `mark_missing_safe` rollup |
| `cre_enrich.py` | Tier-B queue worker: targeted detail collect + additive re-ingest |

## Source Coverage Notes

The source matrix changes often. Current status belongs in:

```text
cre_collector/START_HERE.md
cre_collector/BROKERAGE_STATUS_2026-06-12.md
```

High-signal cautions (no counts here; see `START_HERE.md`):

- CBRE main inventory is active through an internal JSON API behind
  Cloudflare, using local Firecrawl stealth.
- CBRE Deal Flow is a separate public RCM ListingEngine source folded into
  parent CBRE with prefixed IDs.
- Cushman is complete for its public API feed after full run, live ingest,
  source-scoped reconciliation, and Supabase validation.
- Newmark uses Algolia credentials embedded in the public page.
- Marcus and Millichap has public sale API coverage and detail enrichment;
  lease inventory is not proven.
- Avison Young uses a public SharpLaunch feed.
- SVN and Lee use Buildout inventory feeds; sustained paging can trigger HTML
  interstitials. Lee is complete via durable page cache.
- Colliers has two folded sources: SalesTracker investment-sale (`colliers`,
  public RCM GET) and the full main site (`colliers-main`, COMPLETE 2026-06-14)
  via the public XML sitemap through local Firecrawl plus detail-render JSON-LD
  parse (`main:` ids).
- Transwestern is complete for its public GET feed after full run, live ingest,
  source-scoped reconciliation, and Supabase validation.
- JLL Investor Center is complete for the public sitemap detail path. No
  coordinates are available from the Investor detail path (known limitation).
- Savills sale is structurally capped: no public US commercial-sale feed exists.
  Lease coverage is minimal (2 defensible Chicago retail rows after cleanup).
- Five sources were added 2026-06+: `matthews`, `franklin-street`, `srs`,
  `hanley`, `kidder-mathews`. Monitor baseline seed has not yet expanded for them.

## Local CLI Wrapper

Prefer `firecrawl_cli.sh` so calls always hit the local API:

```bash
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com \
  --format markdown,links --json --pretty

scripts/firecrawl-ops/firecrawl_cli.sh parse ./report.pdf --json --pretty

scripts/firecrawl-ops/firecrawl_cli.sh search "Dallas office for sale" \
  --limit 5 --json --pretty
```

For crawl jobs, submit first and poll by job ID. Local `crawl --wait` can hang
even after the API finishes.

## Direct HTTP Helper

Use `firecrawl_request.py` when an agent needs direct API control without extra
dependencies, split saved artifacts, or PDF parser options the CLI does not
expose.

```bash
scripts/firecrawl-ops/firecrawl_request.py scrape https://example.com \
  --formats markdown,links --pretty \
  --out ./out/example.json \
  --save-fields ./out/example-fields

scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html \
  --pdf-mode auto \
  --max-pages 25 \
  --out-dir ./out/firecrawl-report \
  --pretty
```

This helper is for local agent workflows and repeatable artifacts. Normal app
code should use the product's SDK or API client.

## Model Routing

Switch profiles with:

```bash
scripts/firecrawl-ops/set_model_profile.sh budget
scripts/firecrawl-ops/set_model_profile.sh escalated
scripts/firecrawl-ops/set_model_profile.sh gateway
scripts/firecrawl-ops/set_model_profile.sh gateway-codex
scripts/firecrawl-ops/set_model_profile.sh openai-direct
docker compose up -d --force-recreate api
```

The script creates or updates root `.env`, which is gitignored and is what
Docker Compose reads. Add provider keys manually. Never commit keys or local
env files.

Plain scrape, map, search, and parse can work without model keys. AI-backed
summary, JSON extraction, query, and extract flows require valid provider
settings.

## CRE Resource Safety Profile

Before a resource-sensitive CRE collection, apply the local profile with a
single Playwright page and one CPU each for the API and browser sidecar:

```bash
scripts/firecrawl-ops/set_cre_resource_profile.sh apply --with-pids
scripts/firecrawl-ops/set_cre_resource_profile.sh show
```

`--with-pids` adds a 192-process Playwright backstop. It is optional because
other local workflows may need a higher limit. The helper changes only
`PLAYWRIGHT_MAX_CONCURRENT_PAGES`, `PLAYWRIGHT_CPUS`, `API_CPUS`, and, when
requested, `PLAYWRIGHT_PIDS_LIMIT`; it neither prints nor copies secrets. It
does not restart running containers. After reviewing `show`, explicitly apply
the settings with:

```bash
docker compose up -d --force-recreate api playwright-service
```

After the CRE run, restore exactly the resource-key values that existed before
the profile was applied:

```bash
scripts/firecrawl-ops/set_cre_resource_profile.sh restore
```

The small restore state is stored under ignored `tasks/tmp/`. Do not run
`restore` without the state file: the helper intentionally refuses to guess.

## Local PDF OCR

Default parse mode is enough for many born-digital PDFs. For scanned,
image-only, table-heavy, slide-style, or layout-sensitive PDFs, start the
local Docling adapter:

```bash
scripts/firecrawl-ops/local_firepdf_ocr.sh start --profile research-page-aware
scripts/firecrawl-ops/local_firepdf_ocr.sh health
scripts/firecrawl-ops/local_firepdf_ocr.sh doctor --smoke-pdf ./report.pdf

scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html \
  --pdf-mode ocr \
  --max-pages 10 \
  --pretty

scripts/firecrawl-ops/local_firepdf_ocr.sh stop
```

Mode guidance:

- `fast`: born-digital text PDFs, lowest cost and quickest path.
- `auto`: default Firecrawl routing.
- `ocr`: scanned or image-only PDFs, slide decks, and layout-heavy files.

When quality matters, benchmark:

```bash
scripts/firecrawl-ops/pdf_ocr_benchmark.py ./report.pdf \
  --modes fast,auto,ocr \
  --profiles default,research-page-aware,tables-accurate \
  --max-pages 40 \
  --out-dir /tmp/firecrawl-pdf-ocr-benchmark \
  --strict
```

## Upstream Sync

Do not sync upstream directly on `main`.

```bash
scripts/firecrawl-ops/sync_upstream_main.sh
```

Conflict rule of thumb:

- Prefer upstream for product, API, SDK, and core Firecrawl files.
- Prefer this fork for `.agents/`, `docs/firecrawl-ops/`,
  `scripts/firecrawl-ops/`, `LOCAL_DEVELOPMENT_GUIDE.md`, and local ops docs.

## Skill Sync

After editing repo skills or the source docs they mirror:

```bash
scripts/firecrawl-ops/sync_agent_skills.sh
```

That copies `firecrawl-ops` and `firecrawl-local-api` into
`~/.agents/skills` and symlinks them into user-level agent folders.

## Safe Editing Rules

- Keep secrets out of commits and logs.
- Keep generated collector artifacts in gitignored `out/` paths.
- Do not stage `node_modules/`, `__pycache__/`, generated SQL, or benchmark
  output.
- Do not claim a broker is complete until collection, ingest, and Supabase
  validation are all documented.
- Do not use legacy `cre_scrapers` status as production source truth.
- Do not open a PR through an API. Cayman opens PRs manually.
