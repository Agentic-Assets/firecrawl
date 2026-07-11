# CLAUDE.md - cre_collector/

> **ACTIVE production path.** This is the only supported collector and ingest
> implementation. It is not currently scheduled on the Mac mini. Do **not**
> implement daily collection in `../cre_scrapers/` (legacy Python
> probes/archives only).

> **Current runtime source, 2026-07-11:** The Mac mini audit found no CRE
> launchd jobs, markers, or collector artifacts. Treat dated counts and
> scheduler claims below as historical. The [operator runbook](../../../tasks/2026-07-10-cre-consolidation-review/2026-07-11-firecrawl-operator-runbook.md)
> is the current ordered recovery and canary procedure.

Multi-source CRE listing collector + Supabase ingestor. This is the
**production path** for building and refreshing the `credeals` listing
database (EQUIRE feed). It supersedes the per-broker Python scrapers in
`../cre_scrapers/` for bulk collection (`../cre_scrapers/brokers/*/scraper.py`
is **stale** for production; README/archive there is reference-only).

## Agent routing: where to edit

| Task | Edit here (ACTIVE) | Do NOT use (STALE) |
|------|-------------------|-------------------|
| New/changed broker collect logic | `sources/<broker>.ts`, `collect.ts` | `../cre_scrapers/brokers/*/scraper.py` |
| Ingest / board upsert | `cre_ingest.py` | `../cre_pipeline.py`, `../cre_scrapers/pipeline.py` |
| Daily / scheduled refresh | `cre_daily_update.sh`, `launchd/` | `../cre_scrapers/config.py` |
| Live counts / source status | `START_HERE.md` | `../cre_scrapers/config.py` `active` flags |
| Broker endpoint research notes | read `../cre_scrapers/brokers/*/README.md` | do not treat as runtime code |

Adapted from the Prometheus cloud collector reference in `../prometheus/`.
Runs entirely against the local self-hosted Firecrawl API.

## Live counts and run-health (agents)

`START_HERE.md` holds the canonical **Latest Source Matrix** and operational
snapshot. Those figures (board totals, per-source rows, pytest counts, launchd
last-run rc, index sizes) **stale quickly**. Before quoting or editing them,
follow the **Agent rule: verify counts** section at the top of `START_HERE.md`:
run `cre_status.sh`, re-run the test suites, and query Supabase when you need
inventory numbers. Update the dated banner when you refresh docs.

## Read order

1. `START_HERE.md` - live counts, next steps, session bootstrap, Known Limits
2. `BROKERAGE_STATUS_2026-06-12.md` - per-broker completion status and upgrade order
3. This file - orchestration, ingest contract, monitor safety rails
4. Module docs (each has its own `CLAUDE.md`): `sources/`, `lib/`, `launchd/`, `tests/`
5. `../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md` when touching
   `--monitor`, 007 change-tracking tables, `cre_monitor.py`, or `cre_gate.py`

## Files

| File / dir | Purpose |
|------------|---------|
| `collect.ts` | Orchestrator: 20 sources, CLI, broker merge, artifact write |
| `types.ts` | Shared listing vocabulary + `SourceResult` (`truncated?`, etc.) |
| `sources/` | Per-broker adapters - see `sources/CLAUDE.md` |
| `lib/` | Shared scrape/config/util - see `lib/CLAUDE.md` |
| `cre_ingest.py` | Collector JSON → `credeals` upsert (stdlib + psql) |
| `cre_monitor.py` | Observe-only diff/event runner (007 tables); never writes `status`/`deleted_at`; enqueues new/changed into `cre_enrichment_queue` |
| `cre_gate.py` | Per-source coverage gate (`cre_source_baseline`); emits `mark_missing_safe` rollup |
| `cre_enrich.py` | Tier-B queue worker: claims a batch from `cre_enrichment_queue`, runs `collect.ts --enrich-input` (targeted detail), re-ingests additively (`cre_ingest.py --in`), deletes done rows. Additive by construction (never `--mark-missing`/`--activate-status`); URL-matched, id-keyed completion; pure builders + thin `run()` |
| `cre_daily_update.sh` | healthcheck → full collect → gate [3/4] → ingest → prune `out/daily/` artifacts |
| `cre_status.sh` | Read-only run-health heartbeat: launchd state, per-tier staleness vs cadence, last-run verdict (from `out/daily/last_run_<tier>.json`), last-ingest counts, `out/` footprint + lock state (hung/stale), stack/env/TCC. Exits nonzero if unhealthy. `--full-health` runs the full healthcheck |
| `cre_setup.sh` | One-command preflight + bootstrap for a fresh clone (toolchain, deps, env, offline smoke); run first. See `SETUP.md` |
| `cre_validate.py` | Post-ingest Supabase validation (`npm run validate:supabase`); not in daily script |
| `backfill_media_from_raw_data.py` | One-time additive lift of media/docs already stranded in `raw_data` into `cre_listing_media`/`cre_listing_links`/`cre_listing_documents`; `--dry-run` default, `--apply` gated on go-ahead; `011` DDL now applied 2026-06-15 (only the media backfill RUN remains gated). See `HANDOFF_MEDIA_CAPTURE_2026-06-15.md` |
| `cre_parse.py` | Python mirror of `lib/parse.ts`: shared CRE text parsers (price/sqft/cap-rate/address). Imported by `cre_ingest.py` and the raw_data backfill; verifiably identical to the TS side via a shared golden test-vector table |
| `cre_geo.py` | Offline ZIP->county+CBSA crosswalk resolver (`data/zip_cbsa_crosswalk.csv`). `ZipCbsaCrosswalk` + `derive_geo()`; used by `cre_geo_backfill.py` and optionally `cre_ingest.py`. Pure, stdlib-only, no network |
| `cre_backfill_raw_data.py` | One-time additive/idempotent Class-1 scalar backfill from `raw_data` into `cre_listings` (canonical_url + institutional cols). `--dry-run` default, `--apply` gated; APPLIED 2026-06-15 (canonical_url 0->87,324, 0 decode failures). Never touches `status`/`deleted_at` |
| `cre_geo_backfill.py` | Additive/idempotent geo derivation (county/cbsa/geo_source) for existing rows via `cre_geo`. `--dry-run` default, `--apply` gated; APPLIED 2026-06-15 (85,618 of 87,328 rows) |
| `om_classify_existing.py` | One-time additive re-classification of `doc_type='brochure'` rows into flyer/floor_plan/om/financials. `--dry-run` default; APPLIED 2026-06-15 (14,087 of 70,414 upgraded). Upgrade-only, never downgrades |
| `om_url_resolver.py` | Resolves viewer-wrapped / non-`.pdf` brochure URLs (Buildout iframe, DocumentCloud, etc.) to the real `.pdf` document URL for parser regression coverage |
| `om_parse.py` | Retired Firecrawl OM writer. Pure extractors and dry-run artifacts remain for regression coverage; `--apply` exits `78`. GetCREdata is the sole production OM extraction writer. |
| `run_colliers_main_full.sh` | Resumable colliers-main batch driver (~15,896 URLs) |
| `launchd/` | macOS tier schedules (portable `*.plist.template` + `install_launchd.sh`) - see `launchd/CLAUDE.md` |
| `tests/` | pytest contracts - see `tests/CLAUDE.md` |
| `SETUP.md` | Fresh-clone setup runbook (Mac mini production + dev): `cre_setup.sh`, env, launchd generator |
| `START_HERE.md` | Current status and new-session runbook |
| `BROKERAGE_STATUS_2026-06-12.md` | Per-broker coverage counts (live) |
| `HANDOFF_COLLIERS_MAIN_2026-06-13.md` | colliers-main full detail run handoff |
| `HANDOFF_MONITOR_FIRST_APPLY_2026-06-13.md` | Monitor hardening, module split, first `--apply` seed |
| `SECURITY_REVIEW_2026-06-14.md` | Branch security review: verdict, the `standard_conforming_strings` pin fix, deferred base-table REVOKE |
| `ENRICHMENT_WORKER_DESIGN_2026-06-15.md` | Tier-B enrichment-queue worker + cadence restructure (monitor 2x/day, enrich every 4h, weekly additive full backstop, daily retained for rollback). Implemented in code; live scheduler recovery remains gated. |
| `HANDOFF_MEDIA_CAPTURE_2026-06-15.md` | Capture all videos/links/docs/images + full markdown + stranded structured fields. Generic harvester (`lib/harvest.ts`), richer scrape formats, NEW `cre_listing_media`/`cre_listing_links` tables (`sql/011`), Buildout-iframe Tier-B detail for lee/svn, raw_data backfill. BUILT + verified in code; live apply GATED |
| `PHASE2_DATA_LIFT_CONTRACT_2026-06-15.md` | Phase-2 data-lift implementation contract: the spec the `011`-`014` DDL, the backfills, and the `cre_parse`/`cre_geo`/`om_*` modules implement |
| `HANDOFF_DATA_LIFT_2026-06-15.md` | Phase-2 data-lift handoff: what shipped (DDL, three additive backfills, OM-parse tier), the prod apply log, and test counts |
| `RAW_DATA_GAP_CLASSIFICATION_2026-06-15.md` | Which structured fields/media/docs are stranded in `raw_data`, plus the document-corpus audit that scopes the backfills |
| `CRE_LISTINGS_COLUMN_COVERAGE_2026-06-15.md` | Per-column fill-rate report for `cre_listings` (drives backfill targeting); raw outputs in `reports/` |
| `FRESHNESS_HISTORY_REVIEW_2026-06-15.md` | Freshness/accuracy/historic-retention review (the H/M/L findings behind `009` and the ingest-written price history) |
| `TODO.md` | Collector working TODO list |
| `data/` | Geo crosswalk reference (`zip_cbsa_crosswalk.csv` consumed by `cre_geo.py`) + `build_zip_cbsa_crosswalk.py` builder and `README.md` |
| `reports/` | Coverage report outputs (column/brokerage/transaction CSVs + summary JSON; see `CRE_LISTINGS_COLUMN_COVERAGE_2026-06-15.md`) |
| `workflows/` | Executable Workflow scripts; `cre_enrichment_worker.workflow.js` is the build/test/review/cutover plan for the enrichment design above |
| `archive/` | Dated buildout history (see `archive/README.md`) |
| `../../../docs/firecrawl-ops/references/cre-intelligence-system-design.md` | Architecture + go-forward plan (§14) |
| `../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md` | Monitor run model and operational gotchas |
| `../../../docs/firecrawl-ops/references/cre-equire-consumer-api.md` | How EQUIRE reads the data |
| `../../../docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md` | Brokerage upgrade process |
| `../../../docs/firecrawl-ops/references/cre-cloud-hosting-options-2026-06-14.md` | Where to run the pipeline (cloud vs Mac mini): platform comparison, anti-bot IP risk, recommendation (decision aid, not actioned) |
| `out/` | Run artifacts (gitignored) |

## Quick start

```bash
cd scripts/firecrawl-ops/cre_collector
bash cre_setup.sh                # fresh clone: preflight + bootstrap (deps, checks, smoke). See SETUP.md
npm install                      # once (cre_setup.sh does this for you)
npm run typecheck                # TypeScript validation
npm test                         # TypeScript typecheck + unit tests
npm run test:unit                # TypeScript unit tests only
python3 -m pytest tests/ -q      # Python collector tests

# Small probe of one source, both transactions
npx tsx collect.ts --source=svn --transaction=both --max-items=6 --out=/tmp/probe.json

# Full US collection (everything, sale + lease)
npx tsx collect.ts --source=all --transaction=both --max-items=0 \
  --page-cap=400 --concurrency=3 --out=out/run.json

# Ingest to Supabase credeals schema
python3 cre_ingest.py --in out/run.json                  # additive upsert
python3 cre_ingest.py --in out/run.json --mark-missing   # full-run reconcile

# Safe daily cycle while any source is partial or blocked
bash cre_daily_update.sh --no-mark-missing
```

## collect.ts

Flags: `--source=all|csv` `--transaction=sale|lease|both` (default `both`)
`--max-items` (0 = unlimited) `--page-cap` (default 60; use 400 for full runs)
`--concurrency` (1–6, default 3) `--out=path` `--monitor` (enumeration-only pass)
`--enrich-input` (targeted detail enrichment from `cre_enrich.py` queue batch).

Env: `FIRECRAWL_API_URL` (default `http://localhost:3002`);
`FIRECRAWL_API_KEY` (optional; defaults to `local-self-hosted` when unset).

**20 source keys** (sale/lease support, methods, monitor matrix, Buildout rules,
`external_id` gotchas, per-source env vars): `sources/CLAUDE.md`. Live row counts:
`START_HERE.md` (Latest Source Matrix; **verify before quoting**, see agent rule
at top of that file) and `BROKERAGE_STATUS_2026-06-12.md` (may lag; prefer a
fresh `psql` check when accuracy matters).

**`--page-cap`** bounds pagination on `jll`, `cbre-dealflow`, `colliers`
(SalesTracker), `nai-global` (`PAGE_CAP × page size`), and Savills sale pages.
It does not cap API-paginated or sitemap-driven sources (`cushman-wakefield`,
`transwestern`, `marcus-millichap`, `colliers-main`, Buildout).

**Scrape primitives** (`scrapeJson`, `brokerRef`, `pmap`, `prune`): `lib/CLAUDE.md`.

## cre_ingest.py

Maps collector JSON to `credeals.cre_listings` (+ contacts/documents/images
children, + `cre_scrape_jobs` row per brokerage). Stdlib only; talks to Postgres
via psql (`PSQL_BIN`, else `/opt/homebrew/opt/libpq/bin/psql`,
`/usr/local/opt/libpq/bin/psql`, then `PATH`). Document and image child rows
store external URLs only.

Credentials: reads `POSTGRES_URL_NON_POOLING` (preferred) or `POSTGRES_URL`.
Discovery order: `--env-file` flag, then `CRE_ENV_FILE` env var, then the
`~/Documents/GitHub/agentic-assets/dynamically-display-cre-listing-data/.env.local`
default (fallback `~/Documents/GitHub/agentic-assets/CRE_EQUIRE/.env.local`).
Set `CRE_ENV_FILE` on any machine where the EQUIRE repo is not at the default
`~/Documents` path (production Mac mini uses `~/.config/cre/equire.env` via
launchd). `cre_monitor.py` and `cre_gate.py` import the same loader.
Live runs print only the env file path, never the URL.

Key behavior:
- Dedup key `(brokerage_id, external_id)`; sub-sources fold into the parent
  brokerage with prefixed ids (`dealflow:`, `investor:`, `main:`). Missing ids get
  `url:<sha1-16>` from the listing URL; Buildout (`svn`, `lee-associates`)
  prefers URL `propertyId` with `-sale`/`-lease` stripped first. Buildout sources:
  `svn`, `lee-associates`, `franklin-street`.
- Sale + lease passes merge to `transaction_type='sale_or_lease'`.
- `cap_rate` stored as a decimal fraction (e.g. `0.065`); `norm_cap_rate` drops
  non-numeric, `<= 0`, percent inputs `>= 30`, and `>= 0.5`. Upsert uses
  `COALESCE` so a null new cap rate keeps the existing DB value. Price columns
  (`sale_price_usd`, `sale_price_per_sf`, `lease_rate_min`, `lease_rate_max`)
  also COALESCE-keep: a transient parse miss does not blank a previously-good
  numeric price.
- Upsert sets `status='active'`, resurrects soft-deleted rows (`deleted_at=NULL`),
  but only resets status to `'active'` when the prior status was `'inactive'` (a
  mark-missing soft-delete); a real terminal (`sold`/`leased`/`off_market`) that
  flickered back into a feed keeps its prior status label. Wholesale-replaces
  contacts/documents/images **unless**
  `jsonb_path_exists(raw_data, '$.**.detailError')` (preserves children on
  transient detail failures).
- **Status activation is OPT-IN and default-OFF.** Source-derived statuses are
  suppressed unless `--activate-status` is passed on the CLI or
  `CRE_ACTIVATE_STATUS=1` is set in the environment. New helpers:
  `_status_activation_enabled()` and `apply_status_activation_gate()`. When off,
  the upsert inserts `COALESCE->'active'` and the activation UPDATE is a no-op.
  Do NOT assume status activation fires on the next daily/manual full ingest;
  it requires the explicit flag plus consumer-side gate deploy first.
- **Ingest-written value history (2026-06-15, applied to prod):**
  `build_sql()` now captures a `_prior_vals` temp table of watched fields BEFORE
  the upsert, then writes one row to `cre_listing_price_history` per listing
  where a watched field (`sale_price_usd`, `sale_price_per_sf`, `lease_rate_min`,
  `lease_rate_max`, `status`, `cap_rate`) IS DISTINCT FROM its prior value.
  First-ever inserts produce no history row (history starts at the first CHANGE).
  The INSERT is existence-guarded via `to_regclass` (no-op if `009` is absent).
  Dry-run emits the plain unguarded INSERT so tests can assert the shape.
- **Contacts + documents archived at retirement (2026-06-15, applied to prod):**
  When `--mark-missing` fires, the soft-delete block first captures retired rows
  in a `_retired` temp table (with `prior_status`), then runs the UPDATE, then
  INSERTs one `cre_listing_events` row per retired listing with
  `event_type='disappeared'` / `source_value='mark_missing'`, and then (guarded)
  snapshots the retired listings' final contacts and documents into
  `cre_listing_contacts_archive` and `cre_listing_documents_archive`. Images are
  excluded (high volume, low historical value). These tables have no FK to
  `cre_listings` so they survive a future hard delete of the source row.
- **`--mark-missing`: soft-deletes unseen rows (`status='inactive'`,
  `deleted_at=now()`).** Per brokerage, only when every source pass for that
  brokerage ran error-free, staged `>= --mark-missing-floor` (default 100), and
  **folded coverage is count-aware and complete**: every folded key (e.g. `cbre` +
  `cbre-dealflow`, `jll` + `jll-investor`, `colliers` + `colliers-main`) must
  appear in the artifact AND have a nonzero `listingsCollected` count
  (`discovered_by_source_key` dict built from `source_entries`). Singletons still
  short-circuit on `len(known_keys) == 1`.
  Never use on partial/subset runs.
- `--dry-run --keep-artifacts DIR` writes SQL without connecting.

Date semantics (ingest scope):
- `listing_date`: not populated by bulk collector; leave null unless a source
  proves a true first-listed/on-market field.
- `updated_date` ← `listing.lastUpdated` (YYYY-MM-DD prefix only).
- `scraped_at` ← artifact `finishedAt`.
- `created_at`, `updated_at`, `deleted_at`: database lifecycle fields.

Supabase access: `credeals.cre_*` tables and `v_cre_*` views are service-role
only. Read `archive/SUPABASE_SECURITY_NOTE_2026-06-12.md` before changing grants
or view privileges. Consumer API details: `cre-equire-consumer-api.md`.

Phase-2 data-lift applied to prod 2026-06-15 (all additive, board-unchanged):
DDL `011`-`014` (media/links tables, institutional + geo columns, OM-facts and
zip/CBSA crosswalk tables) plus three additive backfills (`cre_backfill_raw_data`,
`om_classify_existing`, `cre_geo_backfill`); `status` was never touched. Live
coverage figures: `START_HERE.md`.

## Monitor mode (hard rules)

`collect.ts --monitor` writes the same artifact shape with `runMeta.mode="monitor"`.
**Never feed a monitor artifact to `cre_ingest.py` (especially not with
`--mark-missing`).** Monitor artifacts go to `cre_monitor.py` only;
`cre_gate.py` reads baselines for weekly `mark_missing_safe`.

- Enumeration `external_id` must match full-collect / ingest keys (enforced by
  `tests/test_enum_key_invariant.py`).
- **`cre_monitor.py`** refuses disappearance events for sources whose enumeration
  pass reported `error` or `truncated` (not overridable by `--force-disappear`).
- Per-source monitor behavior (excluded sources, supersets, detail skips):
  `sources/CLAUDE.md` + `cre-monitor-subsystem.md`.
- Scheduled monitor tier uses default `--page-cap=60` unless overridden; see
  `launchd/CLAUDE.md`. **Current audit (2026-07-11):** no CRE scheduler is
  installed on the Mac mini. Run `bash cre_status.sh` only as a read-only
  preflight, then follow the operator runbook before a recovery attempt. `cre_gate.py`
  is wired into `cre_daily_update.sh` as observe-only step [3/4]
  (`--in RUN --apply --strict --out gate.json`); if the strict gate detects any
  partial/regressed source, the script auto-downgrades to `--no-mark-missing`.

## Daily updates

`cre_daily_update.sh` = healthcheck → full collect (sale+lease, unlimited,
`CRE_PAGE_CAP` default 400) → observe-only gate [3/4] → ingest → prune (EXIT
trap; keeps 14 `run_*.json`, 29 `run_*.log`, 14 `gate_*.json` under
`out/daily/`). Script default includes `--mark-missing`; use
`bash cre_daily_update.sh --no-mark-missing` while Savills sale stays
structurally capped (colliers-main is now complete). See `START_HERE.md` Known
Limits and Operational Recovery.

Tiered schedules: `launchd/CLAUDE.md`. They are implemented in code but not
currently loaded on the Mac mini. Step [3/4] runs `cre_gate.py` observe-only.

## Adding a source

1. Implement `srcNewSource(tx, max, monitor)` in `sources/<name>.ts` returning
   `SourceResult`; register in `collect.ts` (`SOURCE_KEYS` + `runSource`).
2. Map its key in `cre_ingest.py` `SOURCE_TO_BROKERAGE` (add seed row in
   `../sql/001_cre_brokerages.sql` and apply).
3. Probe: `npx tsx collect.ts --source=<key> --transaction=both --max-items=6`,
   then `cre_ingest.py --dry-run`. Adapter contract, monitor support, env vars:
   `sources/CLAUDE.md`.
