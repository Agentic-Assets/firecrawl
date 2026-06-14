# CLAUDE.md - cre_collector/

Multi-source CRE listing collector + Supabase ingestor. This is the
**production path** for building and refreshing the `credeals` listing
database (EQUIRE feed). It supersedes the per-broker Python scrapers in
`../cre_scrapers/` for bulk collection (those remain useful for detail-page
enrichment).

Adapted from the Prometheus cloud collector reference in `../prometheus/`.
Runs entirely against the local self-hosted Firecrawl API.

## Files

| File | Purpose |
|------|---------|
| `collect.ts` | 15-source collector (TypeScript, Firecrawl JS SDK pinned to local API); `--monitor` adds a cheap enumeration-only pass |
| `cre_ingest.py` | Collector JSON -> `credeals` schema upsert (stdlib + psql) |
| `cre_monitor.py` | OBSERVE-ONLY change-tracking diff/event/snapshot runner (007 tables). Never writes `status`/`deleted_at`. See the monitor subsystem reference |
| `cre_gate.py` | Per-source coverage-and-anomaly gate (reads `cre_source_baseline`); emits `mark_missing_safe` rollup |
| `cre_daily_update.sh` | Daily refresh: healthcheck -> collect all -> ingest |
| `cre_validate.py` | Post-ingest Supabase validation (`npm run validate:supabase`) |
| `run_colliers_main_full.sh` | Resumable colliers-main batch runner (full ~15,896-URL detail run) |
| `launchd/` | launchd plist and setup for scheduled daily runs on macOS |
| `tests/` | Test suite for collector and ingestor |
| `START_HERE.md` | Current status and new-session runbook |
| `BROKERAGE_STATUS_2026-06-12.md` | Per-broker coverage status, counts, and next upgrade order (live) |
| `HANDOFF_COLLIERS_MAIN_2026-06-13.md` | Active handoff: colliers-main full detail run (in progress) |
| `archive/` | Dated buildout history: handoff log, lessons, validation snapshots, egress/security audits (see `archive/README.md`) |
| `../../../docs/firecrawl-ops/references/cre-intelligence-system-design.md` | Canonical architecture + go-forward monitoring plan (section 14) |
| `../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md` | Monitor/change-tracking subsystem: components, run model, hard gotchas |
| `../../../docs/firecrawl-ops/references/cre-equire-consumer-api.md` | How EQUIRE reads the data: views, SQL, env, quick start |
| `../../../docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md` | Reusable process for upgrading one brokerage to full public-feed coverage |
| `out/` | Run artifacts (gitignored) |

## Quick start

```bash
cd scripts/firecrawl-ops/cre_collector
npm install                      # once
npm run typecheck                # TypeScript validation

# Small probe of one source, both transactions
npx tsx collect.ts --source=svn --transaction=both --max-items=6 --out=/tmp/probe.json

# Full US collection (everything, sale + lease)
npx tsx collect.ts --source=all --transaction=both --max-items=0 \
  --page-cap=400 --concurrency=3 --out=out/run.json

# Ingest to Supabase credeals schema
python3 cre_ingest.py --in out/run.json                  # additive upsert
python3 cre_ingest.py --in out/run.json --mark-missing   # full-run reconcile

# The safe daily cycle while any all-source errors remain
bash cre_daily_update.sh --no-mark-missing
```

## collect.ts

Flags: `--source=all|csv` `--transaction=sale|lease|both` `--max-items` (0 =
unlimited) `--page-cap` (rendered-page sources, default 60; use 400 for full
runs) `--concurrency` (1-6, default 3) `--out=path` `--monitor` (cheap
enumeration-only pass; see Monitor mode below).

Env: `FIRECRAWL_API_URL` (default `http://localhost:3002`),
`FIRECRAWL_API_KEY` (optional; defaults to `local-self-hosted` when unset;
self-hosted accepts any non-empty value if you set one).

### Sources

Method and transaction support per source. Live row counts and per-source
coverage status live in `START_HERE.md` (Latest Source Matrix) and
`BROKERAGE_STATUS_2026-06-12.md`; this table is method/support only and carries
no counts, so it does not drift.

| Source key | Method | Sale | Lease |
|------------|--------|------|-------|
| `cbre` | Internal JSON API, stealth proxy (Cloudflare) | yes | yes |
| `cbre-dealflow` | Public RCM ListingEngine endpoint; folds into `cbre` (`dealflow:` ids) | yes | yes |
| `jll` | Rendered search pages (tenure=sale/rent) + `__NEXT_DATA__` detail | yes | yes |
| `jll-investor` | Public XML sitemap + detail `__NEXT_DATA__`; folds into `jll` (`investor:` ids) | yes | n/a |
| `cushman-wakefield` | Public `/api/properties/search` JSON + detail enrichment | yes | yes |
| `newmark` | Algolia API + public People exact-name lookup | yes | yes |
| `marcus-millichap` | Public map ActivityId feed, `mappropertydetail` tiles, detail HTML | yes | n/a |
| `avison-young` | Public SharpLaunch feed + detail pages | yes | yes |
| `savills` | Server-rendered pages `/page/N` | yes | yes (tiny US subset) |
| `svn` | Buildout inventory API (client-side sale/lease partition) | yes | yes |
| `lee-associates` | Buildout inventory API + durable page cache | yes | yes |
| `nai-global` | Public Infabode GraphQL + `publicPost` details; filtered to `FOR_SALE_ON_MARKET` on both sale and lease passes | yes | yes |
| `colliers` | Public SalesTracker RCM GET + anonymous SLP detail (investment-sale subset) | yes | n/a |
| `colliers-main` | Public XML sitemap + detail-render JSON-LD; folds into `colliers` (`main:` ids) | yes | yes |
| `transwestern` | Public GET feed + detail pages | yes | yes |

`n/a` lease = the lease pass is intentionally skipped (sale-only public
inventory). `colliers-main` collects both but its `--monitor` pass enumerates
the sale pass only.

Buildout semantics (svn, lee-associates): the inventory feed has **no
server-side sale/lease filter** (`lease=true` is ignored). Items carry
`sale: boolean` (false = lease availability), `also_for_sale_or_lease`, and
`closed`. The collector fetches the full inventory once per brokerage (cached
across both transaction passes), skips `closed`, and partitions client-side.
Dual-mode properties appear twice in Buildout `show_link` URLs (`-sale`/`-lease`
suffixes); `cre_ingest.py` merges those into `transaction_type='sale_or_lease'`.

Rate limiting: Buildout occasionally returns HTML interstitials under
sustained paging. `scrapeJson` retries with backoff. Default Buildout paging
tolerates isolated page failures but aborts if more than ~3% of pages fail
(`failureLimit = max(3, floor(pages * 0.03))`). **Production `svn` and
`lee-associates` set `requireCompletePages: true`**, so any failed page aborts
the source; then caches that failure for the second transaction pass. This
prevents a gappy run from soft-deleting live rows downstream. Lee uses the
durable Buildout page cache controls documented in its broker README; assemble
from cache only after pages 0 through 332 are present.

## cre_ingest.py

Maps collector JSON to `credeals.cre_listings` (+ contacts/documents/images
children, + `cre_scrape_jobs` row per brokerage). Stdlib only; talks to
Postgres via psql (Homebrew libpq at `/opt/homebrew/opt/libpq/bin/psql`).
Document and image child rows store external URLs only. The collector does not
download or upload PDF/image binaries into Supabase storage.

Credentials: reads `POSTGRES_URL_NON_POOLING` (preferred) or `POSTGRES_URL`
at runtime from `~/Documents/GitHub/agentic-assets/dynamically-display-cre-listing-data/.env.local`
(fallback `~/Documents/GitHub/agentic-assets/CRE_EQUIRE/.env.local`), or
`--env-file`. Live runs print only the env file path, never the URL. Never
commit credentials.

Key behavior:
- Dedup key `(brokerage_id, external_id)`; sub-sources fold into the parent
  brokerage with prefixed ids (`dealflow:`, `investor:`, `main:`). Missing ids get
  `url:<sha1-16>` synthesized from the listing URL.
- A listing collected in both sale and lease passes merges to
  `transaction_type='sale_or_lease'`.
- `cap_rate` stored as a decimal fraction (e.g. `0.065` for 6.5%); values
  `>= 0.5` are dropped as implausible. Lease rates parsed only when explicitly
  $/SF (monthly per-SF annualized); everything else stays in `raw_data` (full
  original payload, always kept).
- Upsert refreshes content fields, resurrects soft-deleted rows
  (`deleted_at=NULL`), and wholesale-replaces contacts/documents/images for
  upserted listings **unless** `raw_data` contains `detailError` (preserves
  prior children on transient detail failures).
- `--mark-missing`: soft-deletes rows a full run no longer sees. Guarded per
  brokerage: only applies when every source pass for that brokerage ran
  error-free AND staged >= `--mark-missing-floor` (default 100) rows. Never
  use on partial/subset runs.
- `--dry-run --keep-artifacts DIR` writes the generated SQL without
  connecting.

Date semantics:
- `listing_date` exists in the database but the current bulk collector does not
  populate it. Treat it as null unless a source-specific backfill is added and
  raw/source provenance proves a true first-listed/date-published/on-market
  field. Never infer it from generic `lastUpdated`.
- `updated_date` is source-provided listing recency. The collector maps each
  adapter's best public `lastUpdated`, `updated_at`, `on_market_at`,
  `dateModified`, `datePublished`, `datePosted`, or `publishedAt` value to
  `listing.lastUpdated`, and `cre_ingest.py` writes that to `updated_date`.
- `scraped_at` is the collector artifact/run timestamp, usually the artifact
  `finishedAt`.
- `created_at`, `updated_at`, and `deleted_at` are database lifecycle fields:
  first insert, latest row refresh, and soft-delete by `--mark-missing`.
- The live Supabase column comments were clarified on 2026-06-12; keep
  `../sql/002_cre_listings.sql` comments aligned when date handling changes.

Supabase access model: the collector-owned `credeals.cre_*` base tables and
`v_cre_*` views are service-role only. `anon` and `authenticated` have schema
USAGE in the broader EQUIRE project but no table or view SELECT on this listing
surface. RLS is enabled with no public row policies by design, so Supabase
advisor INFO notices for "RLS enabled no policy" on these tables are accepted
private-schema notices, not public access gaps. EQUIRE should query these
objects from server-side code or a deliberately designed API layer.

Read `archive/SUPABASE_SECURITY_NOTE_2026-06-12.md` before changing grants,
views, or function privileges. The display app hardened view security and revoked
public execute on helper functions while preserving service-role collector use.

## Monitor mode (change tracking, 007)

`collect.ts --monitor` runs each source's cheap enumeration step only (skips the
per-listing detail render) and writes the same artifact JSON shape with
`runMeta.mode="monitor"`. That artifact is consumed by `cre_monitor.py` (the
observe-only diff/event runner) and `cre_gate.py` (the coverage gate), NOT by
`cre_ingest.py`. Full operational rules, the run model, and all gotchas live in
`../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md`. The hardest
rules to remember:

- **Never feed a monitor artifact to `cre_ingest.py` (and never with
  `--mark-missing`).** Monitor artifacts are sparse; the ingest upsert would
  erase enriched prices, `raw_data`, and child rows. Monitor artifacts go
  through `cre_monitor.py` only.
- **`jll`, `jll-investor`, `cbre-dealflow`, and `colliers` (SalesTracker) are excluded from monitor mode** (emit 0 monitor
  rows, stay on the full-sweep cadence): persisted `external_id` is
  detail-derived and unrecoverable from cheap enumeration (`cbre-dealflow`:
  ingest persists `data.projectid`, monitor yields the URL `listingPv` token;
  `colliers` SalesTracker: ingest persists the SLP-detail `ProjectId`, monitor
  yields a `GetMapData` index-paired `ProjectId`). `colliers-main`
  (XML-sitemap ids) is unaffected and stays monitor-enabled. A source emitting
  0 monitor rows is safely ignored by disappearance detection.
- **`nai-global` and `colliers-main` monitor emit supersets** (skip detail-only
  filters); `colliers-main` emits on the sale pass only; `marcus-millichap`
  keeps the lightweight `mappropertydetail` POST. Enumeration-only sources
  (`cbre`, `savills`, `svn`, `lee-associates`) get no collect speedup, only
  downstream write savings.
- The enumeration key (`to_row` external_id) is identical across monitor, full,
  ingest, and gate. Preserve that invariant when adding monitor support.
- **The coverage gate refuses disappearance for any source whose enumeration
  pass errored this run.** That error gate is NOT overridable by
  `--force-disappear`.

`--apply` runs, launchd scheduling, gate wiring into the daily script, and
Phase-2 status activation are gated for explicit go-ahead.

## Daily updates

`cre_daily_update.sh` = healthcheck -> full collect (sale+lease, unlimited)
-> ingest -> prune old artifacts (keeps 14 `run_*.json`, 29 `run_*.log`).
Logs in `out/daily/`.
The script default includes `--mark-missing`; while the `colliers-main` full
run is still in progress and Savills remains partial, keep daily ingest
additive with `bash cre_daily_update.sh --no-mark-missing`. Latest measured
full collection was about 27 minutes at concurrency 3; additive ingest finished
in under a minute.

## Adding a source

1. Write `srcNewSource(tx, max, monitor)` in `collect.ts` returning `SourceResult`;
   register it in `SOURCE_KEYS` + `runSource`.
2. Map its key in `cre_ingest.py` `SOURCE_TO_BROKERAGE` (new slug -> add a
   seed row in `../sql/001_cre_brokerages.sql` and apply it).
3. Probe: `npx tsx collect.ts --source=<key> --transaction=both --max-items=6`,
   then `cre_ingest.py --dry-run` and check the staged TSV row.
