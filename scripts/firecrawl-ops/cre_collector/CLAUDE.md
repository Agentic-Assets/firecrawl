# CLAUDE.md - cre_collector/

Multi-source CRE listing collector + Supabase ingestor. This is the
**production path** for building and refreshing the `credeals` listing
database (EQUIRE feed). It supersedes the per-broker Python scrapers in
`../cre_scrapers/` for bulk collection (those remain useful for detail-page
enrichment).

Adapted from the Prometheus cloud collector reference in `../prometheus/`.
Runs entirely against the local self-hosted Firecrawl API.

## Read order

1. `START_HERE.md` — live counts, next steps, session bootstrap, Known Limits
2. `BROKERAGE_STATUS_2026-06-12.md` — per-broker completion status and upgrade order
3. This file — orchestration, ingest contract, monitor safety rails
4. Module docs (each has its own `CLAUDE.md`): `sources/`, `lib/`, `launchd/`, `tests/`
5. `../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md` when touching
   `--monitor`, 007 change-tracking tables, `cre_monitor.py`, or `cre_gate.py`

## Files

| File / dir | Purpose |
|------------|---------|
| `collect.ts` | Orchestrator: 15 sources, CLI, broker merge, artifact write |
| `types.ts` | Shared listing vocabulary + `SourceResult` (`truncated?`, etc.) |
| `sources/` | Per-broker adapters — see `sources/CLAUDE.md` |
| `lib/` | Shared scrape/config/util — see `lib/CLAUDE.md` |
| `cre_ingest.py` | Collector JSON → `credeals` upsert (stdlib + psql) |
| `cre_monitor.py` | Observe-only diff/event runner (007 tables); never writes `status`/`deleted_at` |
| `cre_gate.py` | Per-source coverage gate (`cre_source_baseline`); emits `mark_missing_safe` rollup |
| `cre_daily_update.sh` | healthcheck → full collect → ingest → prune `out/daily/` artifacts |
| `cre_validate.py` | Post-ingest Supabase validation (`npm run validate:supabase`); not in daily script |
| `run_colliers_main_full.sh` | Resumable colliers-main batch driver (~15,896 URLs) |
| `launchd/` | macOS tier schedules — see `launchd/CLAUDE.md` |
| `tests/` | pytest contracts — see `tests/CLAUDE.md` |
| `START_HERE.md` | Current status and new-session runbook |
| `BROKERAGE_STATUS_2026-06-12.md` | Per-broker coverage counts (live) |
| `HANDOFF_COLLIERS_MAIN_2026-06-13.md` | colliers-main full detail run handoff |
| `HANDOFF_MONITOR_FIRST_APPLY_2026-06-13.md` | Monitor hardening, module split, first `--apply` seed |
| `archive/` | Dated buildout history (see `archive/README.md`) |
| `../../../docs/firecrawl-ops/references/cre-intelligence-system-design.md` | Architecture + go-forward plan (§14) |
| `../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md` | Monitor run model and operational gotchas |
| `../../../docs/firecrawl-ops/references/cre-equire-consumer-api.md` | How EQUIRE reads the data |
| `../../../docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md` | Brokerage upgrade process |
| `out/` | Run artifacts (gitignored) |

## Quick start

```bash
cd scripts/firecrawl-ops/cre_collector
npm install                      # once
npm run typecheck                # TypeScript validation
npm test                         # typecheck + unit tests (lib/, sources/ helpers)
npm run test:unit                # TypeScript unit tests only

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
`--concurrency` (1–6, default 3) `--out=path` `--monitor` (enumeration-only pass).

Env: `FIRECRAWL_API_URL` (default `http://localhost:3002`);
`FIRECRAWL_API_KEY` (optional; defaults to `local-self-hosted` when unset).

**15 source keys** (sale/lease support, methods, monitor matrix, Buildout rules,
`external_id` gotchas, per-source env vars): `sources/CLAUDE.md`. Live row counts:
`START_HERE.md` (Latest Source Matrix) and `BROKERAGE_STATUS_2026-06-12.md`.

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

Credentials: reads `POSTGRES_URL_NON_POOLING` (preferred) or `POSTGRES_URL`
from `~/Documents/GitHub/agentic-assets/dynamically-display-cre-listing-data/.env.local`
(fallback `~/Documents/GitHub/agentic-assets/CRE_EQUIRE/.env.local`), or
`--env-file`. Live runs print only the env file path, never the URL.

Key behavior:
- Dedup key `(brokerage_id, external_id)`; sub-sources fold into the parent
  brokerage with prefixed ids (`dealflow:`, `investor:`, `main:`). Missing ids get
  `url:<sha1-16>` from the listing URL; Buildout (`svn`, `lee-associates`)
  prefers URL `propertyId` with `-sale`/`-lease` stripped first.
- Sale + lease passes merge to `transaction_type='sale_or_lease'`.
- `cap_rate` stored as a decimal fraction (e.g. `0.065`); `norm_cap_rate` drops
  non-numeric, `<= 0`, percent inputs `>= 30`, and `>= 0.5`. Upsert uses
  `COALESCE` so a null new cap rate keeps the existing DB value.
- Upsert sets `status='active'`, resurrects soft-deleted rows (`deleted_at=NULL`),
  and wholesale-replaces contacts/documents/images **unless**
  `jsonb_path_exists(raw_data, '$.**.detailError')` (preserves children on
  transient detail failures).
- **Status activation is OPT-IN and default-OFF.** Source-derived statuses are
  suppressed unless `--activate-status` is passed on the CLI or
  `CRE_ACTIVATE_STATUS=1` is set in the environment. New helpers:
  `_status_activation_enabled()` and `apply_status_activation_gate()`. When off,
  the upsert inserts `COALESCE->'active'` and the activation UPDATE is a no-op.
  Do NOT assume status activation fires on the next daily/manual full ingest;
  it requires the explicit flag plus consumer-side gate deploy first.
- `--mark-missing`: soft-deletes unseen rows (`status='inactive'`,
  `deleted_at=now()`). Per brokerage, only when every source pass for that
  brokerage ran error-free, staged `>= --mark-missing-floor` (default 100), and
  **folded coverage is complete** (e.g. `cbre` + `cbre-dealflow`, `jll` +
  `jll-investor`, `colliers` + `colliers-main` must all appear in the artifact).
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
  `launchd/CLAUDE.md`. Monitor (`ai.agentic.cre-monitor`, every 3h at :15,
  `CRE_MONITOR_APPLY=1`) and daily (`ai.agentic.cre-daily`, 06:30 daily,
  `--no-mark-missing`, status activation OFF) launchd tiers are LOADED as of
  2026-06-14. Weekly reconcile tier (`ai.agentic.cre-weekly`) is intentionally
  NOT loaded (held for explicit go-ahead). `cre_gate.py` is now wired into
  `cre_daily_update.sh` as observe-only step [3/4] (`--in RUN --apply --strict
  --out gate.json`); if the strict gate detects any partial/regressed source,
  the script auto-downgrades to `--no-mark-missing`.

## Daily updates

`cre_daily_update.sh` = healthcheck → full collect (sale+lease, unlimited,
`CRE_PAGE_CAP` default 400) → ingest → prune (keeps 14 `run_*.json`, 29
`run_*.log` under `out/daily/`). Script default includes `--mark-missing`;
use `bash cre_daily_update.sh --no-mark-missing` while partial sources remain
(colliers-main full run, Savills). See `START_HERE.md` Known Limits.

Tiered schedules (monitor / daily additive / weekly reconcile):
`launchd/CLAUDE.md`. Monitor and daily tiers are loaded (2026-06-14). Weekly
reconcile tier is intentionally NOT loaded; do not `launchctl load` it until
explicit go-ahead. Step [3/4] of the daily script now runs `cre_gate.py`
observe-only; see Monitor mode section above.

## Adding a source

1. Implement `srcNewSource(tx, max, monitor)` in `sources/<name>.ts` returning
   `SourceResult`; register in `collect.ts` (`SOURCE_KEYS` + `runSource`).
2. Map its key in `cre_ingest.py` `SOURCE_TO_BROKERAGE` (add seed row in
   `../sql/001_cre_brokerages.sql` and apply).
3. Probe: `npx tsx collect.ts --source=<key> --transaction=both --max-items=6`,
   then `cre_ingest.py --dry-run`. Adapter contract, monitor support, env vars:
   `sources/CLAUDE.md`.
