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
10. `../../docs/firecrawl-ops/references/cre-cloud-hosting-options-2026-06-14.md` (where to run the pipeline: cloud vs Mac mini, platform comparison, anti-bot IP risk; decision aid, not actioned)

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
                               collect.ts (CLI entry), types.ts, lib/, sources/
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
- `cre_source_index` (enumeration snapshot + prior price columns, 007+009)
- `cre_enrichment_queue` (detail-render work queue, 007)
- `cre_source_baseline` (coverage health baseline, 007)
- `cre_listing_price_history` (append-only watched-field snapshots, 009; existence-guarded pre-apply)
- `cre_listing_contacts_archive` (contacts snapshot at retirement, 009; existence-guarded pre-apply)
- `cre_listing_documents_archive` (documents snapshot at retirement, 009; existence-guarded pre-apply)
- `v_cre_listings_full`
- `v_cre_active_for_sale`
- `v_cre_active_for_lease`
- `v_cre_market_summary`
- `v_cre_recent_changes` (7-day change ledger feed, 007+005)
- `search_cre_listings(query, p_city, p_state, p_type, p_transaction)`

Document and image tables store source URLs only. Do not download public PDFs
or images into Supabase storage for the bulk collector.

## Monitor rollout (2026-06-13/14)

Track 1 shipped: monitor hardening, `collect.ts` modular split, coverage gate
triple-gating, four detail-id monitor exclusions. First gated `cre_monitor.py
--apply` seed completed on `avison-young` (baseline + index only; observe-only).
Track 2 shipped 2026-06-13: observe-only seed scaled to all 11 monitor-enabled
sources (`cre_source_baseline`=11, `cre_source_index`=73,693, 0 events, board
unchanged); `jll`/`jll-investor` monitor short-circuit; Phase-2 status
activation wired + hardened in `cre_ingest.py` (COALESCE + terminal guard +
default-off `CRE_STATUS_FLIP_MAX_FRACTION` breaker); EQUIRE board-gate widening
(Option B) committed on `dynamically-display-cre-listing-data`; the agent-facing
`005` views widened to the on-market set (`active`/`under_contract`/`pending`)
on the collector branch (apply gated, verified a zero-row no-op today).
Gate-0 prod status CHECK verified.
Track 2 additionally shipped 2026-06-14: colliers-main full run COMPLETE and
ingested additively (status activation OFF); colliers brokerage total now 17,001
active (15,829 from main: 5,750 sale + 8,897 lease + 1,182 sale_or_lease, 0
soft-deleted, 0 duplicate external_ids); live board total now 87,328 active.
Status activation is now OPT-IN default-off in `cre_ingest.py` (requires
`--activate-status` flag or `CRE_ACTIVATE_STATUS=1`; new helpers
`_status_activation_enabled()` and `apply_status_activation_gate()`).
`cre_gate.py` wired into `cre_daily_update.sh` as observe-only step [3/4]
(`--in RUN --apply --strict --out gate.json`), with auto-downgrade to
`--no-mark-missing` if the strict gate detects any partial/regressed source.
Monitor and daily launchd tiers loaded and EXECUTING on schedule:
`ai.agentic.cre-monitor` (every 3h at :15, `CRE_MONITOR_APPLY=1`; records 007
change events, never touches `status`/`deleted_at`) and `ai.agentic.cre-daily`
(06:30 daily, runs `cre_daily_update.sh --no-mark-missing`, status activation
OFF) now run from `/Users/caymanseagraves/Github/agentic-assets/firecrawl`
(relocated out of `~/Documents`, so the prior macOS TCC / Full Disk Access
exit-126 block no longer applies). The monitor tier has a confirmed clean run
(`out/daily/last_run_monitor.json` rc:0, 2026-06-15); the daily tier executes on
schedule (additive). Weekly reconcile tier
(`ai.agentic.cre-weekly`) intentionally NOT loaded (held for explicit
go-ahead). Live DB hardening applied (cap_rate/occupancy_rate CHECKs,
4 FK ON DELETE SET NULL, 2 NULLS NOT DISTINCT unique indexes,
`v_cre_listings_full` security_invoker reasserted; no board change).
Data-quality cleanups applied: 50 board-invisible JLL rows corrected to
`status='inactive'`; transwestern scrape_config notes restored; Savills
residential contamination removed (101 mis-categorized sale rows + 1 ghost
lease soft-deleted), leaving 2 defensible Chicago retail lease rows. Savills
sale is structurally capped with no public US commercial-sale feed.
Track 3 shipped 2026-06-15 (freshness/history remediation): count-aware folded
coverage guard in `main()` (M1 data-loss fix); price COALESCE-keep on all 4
price columns (L1); revival terminal-stickiness guard (M5); `disappeared` event
emitted in the same transaction as mark-missing (M3); ingest-written
`cre_listing_price_history` (H4a, existence-guarded); contacts + documents
archive at retirement (M2, existence-guarded); flip-breaker metric widened (L4a);
monitor `old_value` populated from `prior_sale_price`/`prior_lease_rate` (H4b);
Savills `IsCommercial` sale guard and lease pagination (L5/L3); signal-staleness
check for disappearance-only sources in `cre_status.sh` (H3);
`CRE_STATUS_FLIP_MAX_FRACTION=0.30` added to daily + weekly plist templates
(L4b). New migration `009_cre_history_retention.sql` adds the history + archive
tables and the `trg_cre_listings_block_history_delete` retention trigger
(registered in `000_run_all.sql`). RESOLVED: the `test_ingest_status_activation.py`
revival assertion now asserts the M5 guarded-revival CASE (and that the old
unconditional form is gone) and passes against current `cre_ingest.py`.
`009_cre_history_retention.sql` is APPLIED to prod (2026-06-15, verified live:
price-history + archive tables, `prior_*` columns, and the retention trigger all
present; history rows are being written). Still gated for go-ahead:
deploy the consumer board-gate branch (must precede live T3.1 activation);
trigger first live status activation; apply the widened `005` views (live DDL,
alongside the consumer deploy). Tier-B `cre_enrichment_queue` worker
remains deferred.

| Module | Role |
|---|---|
| `collect.ts` | CLI entry; orchestrates source runs and `--monitor` |
| `types.ts` | Shared listing types and `SourceResult` contract |
| `lib/` | `config`, `scrape`, `util`, `broker`, `html` primitives |
| `sources/*.ts` | Per-broker adapters (one file per source key) |
| `cre_ingest.py` | Full artifact upsert into `cre_listings` (+ children) |
| `cre_monitor.py` | Observe-only diff/events/index (007 tables) |
| `cre_gate.py` | Per-source coverage baseline and `mark_missing_safe` rollup |

## Broker Status Rules

Current per-source status and counts live in `cre_collector/START_HERE.md` and
`cre_collector/BROKERAGE_STATUS_2026-06-12.md` (the canonical homes). Do not
restate counts here, and do not treat the legacy `cre_scrapers/config.py`
active flags as production coverage.

Durable cautions:

- Colliers has two folded sources under the `colliers` brokerage: `colliers`
  (SalesTracker investment-sale subset) and `colliers-main` (full public site
  via XML sitemap, `main:` ids). The main-site full run is COMPLETE as of
  2026-06-14 (15,829 active rows ingested additively; colliers total 17,001).
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
