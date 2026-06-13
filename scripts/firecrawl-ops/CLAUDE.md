# CLAUDE.md - scripts/firecrawl-ops/

Agent routing for this folder. This file stays compact; use `README.md` for
the longer human guide and runbook.

Parent context: repo `AGENTS.md`, root `CLAUDE.md`, and
`.agents/skills/firecrawl-ops/SKILL.md`.

## Scope

Two systems live here:

1. CRE listing intelligence for EQUIRE. The production path is
   `cre_collector/`, not the older `cre_scrapers/` package.
2. Local Firecrawl ops tooling for the self-hosted stack at
   `http://localhost:3002`.

Do not commit secrets, local artifacts, `out/`, `node_modules/`,
`__pycache__/`, generated SQL, API keys, service-role keys, or database URLs.

## Start Here

For CRE listing work, read in order:

1. `cre_collector/START_HERE.md`
2. `cre_collector/CLAUDE.md`
3. `cre_collector/BROKERAGE_STATUS_2026-06-12.md`
4. `../../docs/firecrawl-ops/references/cre-intelligence-system-design.md` (architecture + go-forward plan)
5. `../../docs/firecrawl-ops/references/cre-equire-consumer-api.md` (how EQUIRE reads the data)
6. `../../docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md`

For local Firecrawl stack work, read:

1. `../../.agents/skills/firecrawl-ops/SKILL.md`
2. `../../LOCAL_DEVELOPMENT_GUIDE.md`
3. `../../docs/firecrawl-ops/references/ops-playbook.md`
4. `../../docs/firecrawl-ops/references/tools-capabilities.md`
5. `../../docs/firecrawl-ops/references/model-routing.md`

## Directory Map

```text
scripts/firecrawl-ops/
  CLAUDE.md                    Agent routing, compact
  README.md                    Human guide, fuller runbook

  cre_collector/               Production CRE collector, Supabase ingestor, and
                               observe-only monitor/change-tracking layer
                               (cre_monitor.py, cre_gate.py, collect.ts --monitor)
  cre_scrapers/                Legacy Python probes and detail enrichment
  sql/                         Idempotent credeals schema migrations
  prometheus/                  Original Prometheus CBRE reference dataset
  tests/                       Unit tests for local OCR service

  firecrawl_healthcheck.sh     Local stack smoke test
  firecrawl_cli.sh             Firecrawl CLI pinned to local API
  firecrawl_request.py         Stdlib HTTP helper for saved artifacts and parse options
  firecrawl_mcp.sh             MCP wrapper pinned to local API
  set_model_profile.sh         Writes model profile values into root .env
  sync_agent_skills.sh         Copies repo skills into user-level skill folders
  sync_upstream_main.sh        Safe upstream merge helper on a branch
  install_git_hooks.sh         Installs advisory git hooks

  local_firepdf_ocr.sh         Docling OCR adapter lifecycle and smoke tests
  local_firepdf_ocr_service.py Fire PDF compatible /ocr adapter
  local-firepdf-adapter.Dockerfile
  pdf_ocr_benchmark.py         PDF mode/profile benchmark runner
  pdf_ocr_profiles.json        Named OCR profile configs

  cre_pipeline.py              Legacy CRE scraper CLI
  cbre_scrape.py               Original standalone CBRE scraper
  cre_access_matrix.py         Platform access probe
  bulk_triage_runner.py        Budget-first triage workflow
  crawl_swarm.py               Batch crawl helper
  firecrawl_swarm_pipeline.py  Multi-source scrape pipeline example
  platform_access_probe.py     Web platform probe
  artificialanalysis_snapshot.py
  google_flights_scrape.py
  parse_flight_deals.py
```

## CRE Production Path

Use `cre_collector/` for current listing data. It collects public sale and
lease inventory, writes JSON artifacts, then ingests into the EQUIRE Supabase
project `fhqycqubkkrdgzswccwd`, schema `credeals`.

Core commands:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
npx tsx collect.ts --source=svn --transaction=both --max-items=6 --out=/tmp/probe.json
python3 cre_ingest.py --in /tmp/probe.json --dry-run
bash cre_daily_update.sh --no-mark-missing
```

Use `--mark-missing` only after a clean all-source full run. While Lee and
Associates or another source is blocked, keep daily ingest additive with
`--no-mark-missing`.

Change tracking (007 tables) runs through a separate observe-only path:
`collect.ts --monitor` produces a cheap enumeration artifact consumed by
`cre_monitor.py` and `cre_gate.py`, never by `cre_ingest.py`. NEVER feed a
`--monitor` artifact to `cre_ingest.py` (it is sparse and the upsert would erase
enriched prices, `raw_data`, and child rows). See
`../../docs/firecrawl-ops/references/cre-monitor-subsystem.md` for the full run
model and gotchas.

Latest all-source baseline: 2026-06-12. See `cre_collector/START_HERE.md`
for live counts and post-run source-specific changes before quoting coverage.

Supabase objects live under `credeals`, not `public`:

- `cre_brokerages`
- `cre_listings`
- `cre_listing_contacts`
- `cre_listing_documents`
- `cre_listing_images`
- `cre_scrape_jobs`
- `cre_scrape_log`
- `cre_listing_events` (change ledger, 007)
- `cre_source_index` (enumeration snapshot, 007)
- `cre_enrichment_queue` (detail-render work queue, 007)
- `cre_source_baseline` (coverage health baseline, 007)
- `v_cre_listings_full`
- `v_cre_active_for_sale`
- `v_cre_active_for_lease`
- `v_cre_market_summary`
- `v_cre_recent_changes` (7-day change ledger feed, 007+005)
- `search_cre_listings(query, p_city, p_state, p_type, p_transaction)`

Document and image tables store source URLs only. Do not download public PDFs
or images into Supabase storage for the bulk collector.

## Broker Status Rules

Current per-source status and counts live in `cre_collector/START_HERE.md` and
`cre_collector/BROKERAGE_STATUS_2026-06-12.md` (the canonical homes). Do not
restate counts here, and do not treat the legacy `cre_scrapers/config.py`
active flags as production coverage.

Durable cautions:

- Colliers has two folded sources under the `colliers` brokerage: `colliers`
  (SalesTracker investment-sale subset) and `colliers-main` (full public site
  via XML sitemap, `main:` ids). The main-site full run is still in progress;
  see `cre_collector/HANDOFF_COLLIERS_MAIN_2026-06-13.md`.
- Keep daily ingest additive (`cre_daily_update.sh --no-mark-missing`) while
  `colliers-main` is mid-run and Savills sale stays partial. Use the default
  `--mark-missing` only after a clean all-source run.

## Local Firecrawl Ops

Run from repo root unless a command says otherwise.

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
docker compose ps
```

This Mac uses OrbStack. Expected local API:

```text
http://localhost:3002
```

Use the wrapper for local calls:

```bash
scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh parse ./report.pdf --json --pretty
scripts/firecrawl-ops/firecrawl_cli.sh search "Dallas office for sale" --limit 5 --json
```

Use `firecrawl_request.py` when the CLI lacks a needed option, especially
advanced `/v2/parse` PDF options or split saved artifacts.

```bash
scripts/firecrawl-ops/firecrawl_request.py parse ./report.pdf \
  --formats markdown,html --pdf-mode ocr --max-pages 25 --out-dir ./out/report
```

## Model Profiles

Switch model routing with:

```bash
scripts/firecrawl-ops/set_model_profile.sh budget
scripts/firecrawl-ops/set_model_profile.sh escalated
scripts/firecrawl-ops/set_model_profile.sh gateway
scripts/firecrawl-ops/set_model_profile.sh gateway-codex
scripts/firecrawl-ops/set_model_profile.sh openai-direct
docker compose up -d --force-recreate api
```

The script writes local `.env` values. Add provider keys manually and never
commit them.

## PDF OCR

Use the local Docling adapter for scanned, image-only, slide-style, or
layout-heavy PDFs:

```bash
scripts/firecrawl-ops/local_firepdf_ocr.sh start --profile research-page-aware
scripts/firecrawl-ops/local_firepdf_ocr.sh health
scripts/firecrawl-ops/local_firepdf_ocr.sh doctor --smoke-pdf ./report.pdf
scripts/firecrawl-ops/local_firepdf_ocr.sh stop
```

Use `fast` for born-digital text PDFs first, `ocr` for scanned or image-only
files, and `pdf_ocr_benchmark.py` when quality matters.

## Sync And Branch Safety

Never push to `main`. For upstream Firecrawl sync:

```bash
scripts/firecrawl-ops/sync_upstream_main.sh
```

After editing repo skills or their source docs:

```bash
scripts/firecrawl-ops/sync_agent_skills.sh
```

Keep fork-owned ops assets in `.agents/`, `docs/firecrawl-ops/`,
`scripts/firecrawl-ops/`, `LOCAL_DEVELOPMENT_GUIDE.md`, and `AGENTS.md`.
