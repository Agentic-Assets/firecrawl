# CLAUDE.md - scripts/firecrawl-ops/

Agent routing for this folder. This file stays compact; use `README.md` for
the longer human guide and runbook.

Parent context: repo `AGENTS.md`, root `CLAUDE.md`, and
`.agents/skills/firecrawl-ops/SKILL.md`.

> **Current runtime source, 2026-07-11:** The Mac mini audit found no active
> CRE launchd labels, plists, cron entries, markers, or collector artifacts.
> Treat dated run-health and schedule claims below as historical evidence, not
> current state. Before a runtime recovery, scheduler load, database write, or
> canary, follow the [operator runbook](../../tasks/2026-07-10-cre-consolidation-review/2026-07-11-firecrawl-operator-runbook.md).

## Scope

Two systems live here. **Agents: default to `cre_collector/` for any listing work.**

| System | Path | Status |
|--------|------|--------|
| CRE listing intelligence (EQUIRE board) | `cre_collector/` | **ACTIVE** — collect, ingest, monitor, launchd |
| Legacy Python probes + archives | `cre_scrapers/`, `cre_scrapers/brokers/` | **STALE** — manual experiments and README notes only |
| Local Firecrawl ops | `firecrawl_*.sh`, `set_model_profile.sh`, etc. | **ACTIVE** — stack health and API |

Do not commit secrets, local artifacts, `out/`, `node_modules/`,
`__pycache__/`, generated SQL, API keys, service-role keys, or database URLs.

## Start Here

For CRE listing work, read in order (fresh machine? `cre_collector/SETUP.md`
first and run `bash cre_collector/cre_setup.sh`):

1. `cre_collector/START_HERE.md`
2. `cre_collector/CLAUDE.md`
3. `cre_collector/BROKERAGE_STATUS_2026-06-12.md`
4. `../../docs/firecrawl-ops/references/cre-intelligence-system-design.md` (architecture + go-forward plan)
5. `../../docs/firecrawl-ops/references/cre-equire-consumer-api.md` (how EQUIRE reads the data)
6. `../../docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md`
7. `../../docs/firecrawl-ops/references/cre-monitor-subsystem.md` (monitor run model + gotchas)
8. `../../docs/firecrawl-ops/references/cre-phase2-board-impact-2026-06-13.md` (Phase-2 status activation board impact)
9. `cre_collector/HANDOFF_MONITOR_FIRST_APPLY_2026-06-13.md` (monitor hardening, modular refactor, first `--apply` seed)
10. `cre_collector/ENRICHMENT_WORKER_DESIGN_2026-06-15.md` (Tier-B worker + launchd cadence; when touching enrich or tiers)
11. `cre_collector/launchd/CLAUDE.md` (tier schedules, install, gates; when touching launchd)
12. `../../docs/firecrawl-ops/references/cre-cloud-hosting-options-2026-06-14.md` (where to run the pipeline: cloud vs Mac mini, platform comparison, anti-bot IP risk; decision aid, not actioned)

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

  cre_collector/               Production CRE collector, Supabase ingestor, monitor,
                               gate, and Tier-B enrich worker
                               (cre_monitor.py, cre_gate.py, cre_enrich.py, collect.ts)
                               Full file table: `cre_collector/CLAUDE.md`
  cre_scrapers/                Legacy Python probes and detail enrichment
  sql/                         Idempotent credeals schema migrations
  prometheus/                  Original Prometheus CBRE reference dataset
  tests/                       Local ops wrapper tests + OCR service tests

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
bash cre_status.sh               # run-health heartbeat
```

Use `--mark-missing` only after a clean all-source full run and explicit
go-ahead. While Savills sale is structurally capped, keep daily ingest
additive with `--no-mark-missing`.

Change tracking (007 tables) runs through a separate observe-only path:
`collect.ts --monitor` produces a cheap enumeration artifact consumed by
`cre_monitor.py` and `cre_gate.py`, never by `cre_ingest.py`. NEVER feed a
`--monitor` artifact to `cre_ingest.py` (it is sparse and the upsert would erase
enriched prices, `raw_data`, and child rows). See
`../../docs/firecrawl-ops/references/cre-monitor-subsystem.md` for the full run
model and gotchas.

Historical all-source artifact baseline: 2026-06-12. Live per-source matrix and
board totals: `cre_collector/START_HERE.md` only (2026-07-05 snapshot there).

Supabase objects live under `credeals`, not `public`:

- `cre_brokerages`
- `cre_listings`
- `cre_listing_contacts`
- `cre_listing_documents`
- `cre_listing_images`
- `cre_scrape_jobs`
- `cre_scrape_log`
- `cre_listing_events` (change ledger, 007)
- `cre_source_index` (enumeration snapshot + prior price columns, 007+009)
- `cre_enrichment_queue` (detail-render work queue, 007)
- `cre_source_baseline` (coverage health baseline, 007)
- `cre_listing_price_history` (append-only watched-field snapshots, 009; APPLIED)
- `cre_listing_contacts_archive` (contacts snapshot at retirement, 009; APPLIED)
- `cre_listing_documents_archive` (documents snapshot at retirement, 009; APPLIED)
- `cre_listing_media`, `cre_listing_links` (detail media/link capture, 011; + `*_archive`; applied to prod, ingest writes gated)
- `cre_listing_om_facts` (shared OM/PDF underwriting facts, 013; production
  writer is GetCREdata; local `om_parse.py --apply` is fail-closed)
- `cre_zip_cbsa_crosswalk` (offline ZIP->county+CBSA reference, 014; loaded 33,791 rows)
- `v_cre_enrichment_queue_pending`, `v_cre_enrichment_dead` (enrichment health, 010)
- `v_cre_listings_full`
- `v_cre_active_for_sale`
- `v_cre_active_for_lease`
- `v_cre_market_summary`
- `v_cre_recent_changes` (7-day change ledger feed, 007+005)
- `search_cre_listings(query, p_city, p_state, p_type, p_transaction)`

Document and image tables store source URLs only. Do not download public PDFs
or images into Supabase storage for the bulk collector.

## Monitor, enrichment, and automation

The collector registers **20 source keys** in `types.ts` / `collect.ts`. Live
counts, per-source status, and run-health belong in
`cre_collector/START_HERE.md` only. Run `bash cre_status.sh` before quoting
schedules or last-run verdicts.

**Shipped architecture:**
- Observe-only monitor (007): `collect.ts --monitor` → `cre_monitor.py` only;
  never ingest monitor artifacts. Four sources return `[]` on monitor because
  `external_id` is detail-derived (`jll`, `jll-investor`, `cbre-dealflow`,
  `colliers` SalesTracker).
- Coverage gate: `cre_gate.py` wired into `cre_daily_update.sh` step [3/4]
  (`--strict` auto-downgrades to `--no-mark-missing` on partial sources).
- Tier-B enrichment: `cre_enrich.py` + `collect.ts --enrich-input` + `sql/010`
  health views. Additive by construction (never `--mark-missing` /
  `--activate-status`).
- Status activation is OPT-IN default-off in `cre_ingest.py` (`--activate-status`
  or `CRE_ACTIVATE_STATUS=1` required).
- Migrations `009` through `014` are APPLIED to prod. Phase-2 backfills
  (`cre_backfill_raw_data`, `om_classify_existing`, `cre_geo_backfill`) ran
  additively; `status` was never touched.

**Current runtime, audited 2026-07-11:** no CRE scheduler is installed or
running on the Mac mini. The 2026-07-05 launchd cutover text is historical and
does not authorize a reload. Use the operator runbook for ordered recovery;
aa-hub is the only approved GetCREdata scheduling lane, and remains disabled
until its explicit readiness gates pass.

**Still gated for explicit go-ahead:** live status activation, consumer
board-gate deploy + widened `005`/`006` views, media backfill
(`backfill_media_from_raw_data.py`), and weekly mark-missing escalation
(`CRE_WEEKLY_MARK_MISSING=1`). `om_parse.py` is a retired local writer and must
remain fail-closed.

| Module | Role |
|---|---|
| `collect.ts` | CLI entry; orchestrates source runs, `--monitor`, `--enrich-input` |
| `types.ts` | Shared listing types and `SourceResult` contract |
| `lib/` | `config`, `scrape`, `harvest`, `parse`, `geo`, `enrich`, `broker`, `html`, `util` |
| `sources/*.ts` | Per-broker adapters (one file per source key) |
| `cre_ingest.py` | Full artifact upsert into `cre_listings` (+ children) |
| `cre_monitor.py` | Observe-only diff/events/index (007 tables) |
| `cre_gate.py` | Per-source coverage baseline and `mark_missing_safe` rollup |
| `cre_enrich.py` | Tier-B queue worker: targeted detail collect + additive re-ingest |
| `cre_daily_update.sh` | Healthcheck → full collect → gate → ingest → prune |
| `cre_status.sh` | Read-only run-health heartbeat (schedules, staleness, locks) |
| `launchd/` | macOS tier schedules (`install_launchd.sh`, `cre_run_tier.sh`) |

## Broker Status Rules

Current per-source status and counts live in `cre_collector/START_HERE.md` and
`cre_collector/BROKERAGE_STATUS_2026-06-12.md` (the canonical homes). Do not
restate counts here, and do not treat the legacy `cre_scrapers/config.py`
active flags as production coverage.

Durable cautions:

- Colliers has two folded sources under the `colliers` brokerage: `colliers`
  (SalesTracker investment-sale subset) and `colliers-main` (full public site
  via XML sitemap, `main:` ids). Main-site full run COMPLETE 2026-06-14.
- Keep daily ingest additive (`cre_daily_update.sh --no-mark-missing`) while
  Savills sale remains structurally capped (no public US commercial-sale feed).
  Use `--mark-missing` only after a clean all-source run and explicit go-ahead.

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
