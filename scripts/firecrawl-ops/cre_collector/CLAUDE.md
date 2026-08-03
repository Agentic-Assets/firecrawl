# CLAUDE.md - cre_collector/

> **ACTIVE production path.** This is the only supported collector and ingest
> implementation. It is not currently scheduled on the Mac mini. Do **not**
> implement daily collection in `../cre_scrapers/` (legacy Python
> probes/archives only).

> **Historical runtime source, 2026-07-11:** The Mac mini audit found no CRE
> launchd jobs, markers, or collector artifacts at that point in time. For
> current refresh work, use `cre_status.sh`, the strict checkpoint runner, and
> the July 29 refresh ledger. The July 11 operator runbook remains historical
> scheduler-recovery and OM-canary context.

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
| Live counts / source status | fresh `cre_validate.py` output + checkpoint manifest | dated docs or `../cre_scrapers/config.py` `active` flags |
| Broker endpoint research notes | read `../cre_scrapers/brokers/*/README.md` | do not treat as runtime code |

Adapted from the Prometheus cloud collector reference in `../prometheus/`.
Runs entirely against the local self-hosted Firecrawl API.

## Live counts and run-health (agents)

No Markdown file is a live-count source. Before quoting board totals,
per-source rows, tests, launchd state, or index sizes, run `cre_status.sh`,
re-run the relevant test suites, and obtain a fresh `cre_validate.py` or exact
checkpoint-manifest readback. Dated docs are operational history.

## Read order

1. `START_HERE.md` - current procedures, next steps, session bootstrap, Known Limits
2. `BROKERAGE_STATUS_2026-06-12.md` - historical per-broker snapshot and upgrade order
3. This file - orchestration, ingest contract, monitor safety rails
4. Module docs (each has its own `CLAUDE.md`): `sources/`, `lib/`, `launchd/`, `tests/`
5. `../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md` when touching
   `--monitor`, 007 change-tracking tables, `cre_monitor.py`, or `cre_gate.py`

## Files

| File / dir | Purpose |
|------------|---------|
| `collect.ts` | Orchestrator: 51 sources, CLI, broker merge, artifact write |
| `types.ts` | Shared listing vocabulary + `SourceResult` (`truncated?`, etc.) |
| `sources/` | Per-broker adapters - see `sources/CLAUDE.md` |
| `lib/` | Shared scrape/config/util - see `lib/CLAUDE.md` |
| `cre_ingest.py` | Collector JSON → `credeals` upsert (stdlib + psql) |
| `cre_monitor.py` | Observe-only diff/event runner (007 tables); never writes `status`/`deleted_at`; enqueues new/changed into `cre_enrichment_queue` |
| `cre_gate.py` | Per-source coverage gate (`cre_source_baseline`); emits `mark_missing_safe` rollup |
| `cre_enrich.py` | Tier-B queue worker: claims a batch from `cre_enrichment_queue`, runs `collect.ts --enrich-input` (targeted detail), re-ingests additively (`cre_ingest.py --in`), deletes done rows. Additive by construction (never `--mark-missing`/`--activate-status`); URL-matched, id-keyed completion; pure builders + thin `run()`. Manual recovery may use `--source SOURCE_KEY` for an exact queue-claim filter: only that source can be claimed, retried, or have attempts incremented. |
| `cre_daily_update.sh` | healthcheck → full collect → gate [3/4] → ingest → prune `out/daily/` artifacts |
| `cre_status.sh` | Read-only run-health heartbeat: launchd state, per-tier staleness vs cadence, last-run verdict (from `out/daily/last_run_<tier>.json`), last-ingest counts, checkout branch/HEAD/dirty state, per-tier rendered-plist drift, `out/` footprint + lock state (hung/stale), stack/env/TCC. Exits nonzero if unhealthy. `--full-health` runs the full healthcheck; `--expected-sha <commit>` enforces the clean deployment identity gate |
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
| `START_HERE.md` | Current procedures and new-session runbook |
| `BROKERAGE_STATUS_2026-06-12.md` | Historical per-broker coverage snapshot |
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
| `workflows/` | Historical Workflow scripts; `cre_enrichment_worker.workflow.js` preserves build/test/review provenance and refuses database or scheduler cutover |
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

# Development collection (everything, sale + lease). Production proof uses
# cre_checkpoint_refresh.py instead.
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

**51 source keys** (sale/lease support, methods, monitor matrix, Buildout rules,
`external_id` gotchas, per-source env vars): `sources/CLAUDE.md`. Obtain current
row counts from a fresh validator or exact checkpoint readback. The matrices in
`START_HERE.md` and `BROKERAGE_STATUS_2026-06-12.md` are dated history.

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
- Provider cards without a canonical listing URL are not written to
  `cre_listings`. Complete CBRE Deal Flow and Colliers SalesTracker snapshots
  persist them as explicitly provisional `cre_source_index` rows under
  source-specific namespaces and watermarks. Colliers canonical ProjectIds are
  one-to-one and never merge, including identical duplicates.
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
- `scraped_at` prefers the admitted `detailObservedAt`; a
  source-revision-cache row uses its current-generation `validatedAt`; an
  authoritative-inventory-feed row uses `inventoryObservedAt`. Artifact
  `finishedAt` is a legacy fallback only. Generation-exact readback, not
  `scraped_at` alone, proves current-source freshness.
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
  `launchd/CLAUDE.md`. Scheduler state is host-local and can change without a
  repository commit. Run `bash cre_status.sh` as the read-only preflight, then
  follow the operator runbook before a recovery attempt. `cre_gate.py`
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

Tiered schedules: `launchd/CLAUDE.md`. Never infer whether they are loaded from
this file; use `bash cre_status.sh` and `launchctl list` on the target host.
Step [3/4] runs `cre_gate.py` observe-only.

For an operator-requested full freshness sweep, prefer
`cre_checkpoint_refresh.py` over the monolithic daily runner. It checkpoints
each source, binds observations to an immutable generation, bypasses Firecrawl
response caches for strict source reads, expires resumable generations after
24 hours, validates artifact provenance, and stops at the first non-`ok` source
gate. All configured artifacts must pass their source gates and the aggregate
gate before any live ingest begins. `first_seen` requires an explicit reviewed
baseline seed and readback before resuming the same immutable run.

The checkpoint runner always applies the host CPU guard documented in
`START_HERE.md`: five-second Darwin CPU samples, an 80-percent ceiling, and a
fail-closed interruption after 30 sustained seconds or any telemetry failure.
The interruption reaps the owned source process group, records JSONL evidence,
releases the canonical lock, and preserves exact resume state. Keep
`--source-workers=1` unless a separate no-write calibration proves a safe
pairing. Run the current 51-key registry as bounded generations; do not use one
`--sources all` generation when the 24-hour observation window cannot hold.
Use `cre_checkpoint_series.py --sources all` for the serial full-registry pass;
its manifest separates source-local failures from global and resource-guard
failures while every mutation remains inside `cre_checkpoint_refresh.py`.

The strict source set is `cbre`, `jll`, `jll-investor`, `colliers-main`,
`cushman-wakefield`, `svn`, `lee-associates`, `franklin-street`, `newmark`, `savills`,
`transwestern`, `marcus-millichap`, `nai-global`, `matthews`, `srs`, `hanley`,
and `kidder-mathews`. `cbre-dealflow` and `colliers` may emit explicitly
inventory-only provider cards. `avison-young` proves inventory and property
detail freshness while preserving existing contacts when its supplemental team
feed is unavailable; do not claim fresh contacts in that state.

Child admission has explicit source classes. CBRE replaces its
collector-owned children. All Buildout feeds, Interra Realty, Cushman &
Wakefield, SRS, Hanley, Kidder Mathews, and Newmark preserve existing children.
Every other strict source must provide an admitted current detail observation
and must not preserve children after a detail failure. The precommit child
guard compares only the same pre-existing parents that remain active, so
legitimate mark-missing retirement is not mistaken for physical child loss;
less than 70% retained for a child type with a baseline of at least 10 aborts
the transaction.

Use the checkpoint runner's generation-exact database readback as the write
admission proof. `cre_refresh_report.py --since <run-start>` is the broader
scope report; neither that report nor a fresh `scraped_at` proves that an old
detail cache was refreshed.

## Adding a source

1. Implement `srcNewSource(tx, max, monitor)` in `sources/<name>.ts` returning
   `SourceResult`; register in `collect.ts` (`SOURCE_KEYS` + `runSource`).
2. Map its key in `cre_ingest.py` `SOURCE_TO_BROKERAGE` (add seed row in
   `../sql/001_cre_brokerages.sql` and apply).
3. Probe: `npx tsx collect.ts --source=<key> --transaction=both --max-items=6`,
   then `cre_ingest.py --dry-run`. Adapter contract, monitor support, env vars:
   `sources/CLAUDE.md`.
