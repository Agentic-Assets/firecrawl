# CLAUDE.md - cre_collector/

Multi-source CRE listing collector + Supabase ingestor. This is the
**production path** for building and refreshing the `credeals` listing
database (EQUIRE feed). It supersedes the per-broker Python scrapers in
`../cre_scrapers/` for bulk collection (those remain useful for detail-page
enrichment).

Adapted from the Prometheus cloud collector preserved at
`../prometheus/multi_source/script.ts`. Runs entirely against the local
self-hosted Firecrawl API.

## Files

| File | Purpose |
|------|---------|
| `collect.ts` | 14-source collector (TypeScript, Firecrawl JS SDK pinned to local API) |
| `cre_ingest.py` | Collector JSON -> `credeals` schema upsert (stdlib + psql) |
| `cre_daily_update.sh` | Daily refresh: healthcheck -> collect all -> ingest |
| `START_HERE.md` | Current status and new-session runbook |
| `HANDOFF_LOG_2026-06-11.md` | Detailed evidence log from buildout and verification |
| `LESSONS_2026-06-11.md` | Operational lessons and future verification pattern |
| `VALIDATION_2026-06-12.md` | Supabase reconciliation, quality checks, and current gaps |
| `BROKERAGE_STATUS_2026-06-12.md` | Per-broker coverage status, counts, and next upgrade order |
| `SUPABASE_SECURITY_NOTE_2026-06-12.md` | Display-app security follow-up for RLS, view, and function grants |
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
runs) `--concurrency` (1-6, default 3) `--out=path`.

Env: `FIRECRAWL_API_URL` (default `http://localhost:3002`),
`FIRECRAWL_API_KEY` (any non-empty value for self-hosted).

### Source status

Latest full ingested all-source run started 2026-06-12 04:04:23 UTC. Several
sources were upgraded after that run on 2026-06-12 local time through
source-specific full runs and validation: CBRE Deal Flow, Cushman & Wakefield,
NAI Global active feed, Colliers SalesTracker, Transwestern, Marcus &
Millichap public sale, Lee & Associates, Newmark refined contacts/state, JLL
Investor Center full sitemap detail run, and Avison Young full detail-enriched
run.

| Source key | Method | Sale | Lease | Notes |
|------------|--------|------|-------|-------|
| `cbre` | Internal JSON API, stealth proxy | 4,222 | 13,145 | Cloudflare; waitFor 4000; 1,661 sale_or_lease; 19,028 active total |
| `cbre-dealflow` | Public RCM ListingEngine endpoint | 1,809 public cards collected of 2,042 reported | 27 of 27 | Full source-specific run live-ingested additively from `out/cbre_dealflow_full_2026-06-12_041740.json`; ids prefixed `dealflow:` and folded into parent `cbre` |
| `jll` | Search pages, waitFor 8000 | 1,247 | 8,733 | tenure=sale / tenure=rent; 761 sale_or_lease; 10,741 active total |
| `jll-investor` | `__NEXT_DATA__` first search page plus detail enrichment | 934 active from full sitemap run | n/a | Complete: full sitemap detail run live-ingested 2026-06-12 22:47 UTC; 50 stale probe rows soft-deleted; no coordinates available from the Investor detail path (known limitation) |
| `cushman-wakefield` | Public `/api/properties/search` JSON plus detail enrichment | 2,743 live total | 8,575 live total | Full API pagination verified; detail pages enrich docs, photos, visible contacts, JSON-LD, and VCard/profile URLs. Use `CUSHMAN_QUERY='1800 Central'` for targeted probes |
| `newmark` | Algolia API plus public People exact-name lookup | 1,121 live rows | 3,250 live rows | Complete public Algolia feed from `out/newmark_full_refined_2026-06-12.json`; no-state DC recovery, raw hit preservation, 3,961 contact/profile rows, source-scoped cleanup |
| `marcus-millichap` | Public map ActivityId feed, mappropertydetail tiles, plus public detail HTML | 3,124 active live rows | n/a | Complete for public sale feed; source-scoped `--mark-missing` applied from `out/marcus_full_2026-06-12_130035.json`; public lease unsupported; gated deal-room URLs remain raw metadata only |
| `avison-young` | Public SharpLaunch feed plus detail pages | 636 live rows | 1,432 live rows plus 133 sale_or_lease | Complete public feed from `out/avison_full_detail_2026-06-12.json`; 2,201 active rows, 2,571 document URLs, 31,570 image URLs, 4,128 contacts, 0 photo leaks; VCards absent and profile URLs sparse |
| `savills` | Server-rendered pages /page/N | 101 active | 3 active | near-empty US lease inventory (3 rows live); foreign fallback cards filtered; US parser accepts state names, ZIP-only rows, and city/state/ZIP variants |
| `svn` | Buildout inventory API | 2,660 live | 2,192 live plus 435 sale_or_lease | 5,287 active rows live; source-scoped ingest complete; 34 soft-deleted |
| `lee-associates` | Buildout inventory API with durable page cache/window assembly | 2,611 live sale rows | 5,691 live lease rows plus 921 sale_or_lease | Complete public Buildout feed from `out/lee_full_cache_2026-06-12_assembled.json`; source-scoped `--mark-missing` applied after cache pages 0-332 assembled cleanly |
| `nai-global` | Public Infabode GraphQL feed and `publicPost` details | 183 live | 58 live | Stable `infabode:` ids and detail URLs; contacts only when public fields exist; 241 active rows live |
| `colliers` | Public SalesTracker RCM GET list/map plus SLP detail | 1,172 unique active | n/a | Investment-sale subset; retained alongside `colliers-main`; no POST/gated path |
| `colliers-main` | Public XML sitemap (`/sitemap` -> `en/sitemap?type=properties`) through local Firecrawl + per-listing detail render | 15,896 sitemap URLs; bounded 2,000 batch live (943 rows) | included | Main `www.colliers.com` unblocked via sitemap + `RealEstateListing` JSON-LD/markdown parse; folds into `colliers` brokerage with `main:` prefix; 404s and alternate-template pages tombstoned; durable resumable cache; full run in progress 2026-06-13. See `HANDOFF_COLLIERS_MAIN_2026-06-13.md` |
| `transwestern` | Public GET feed plus detail pages | 389 live | 1,502 live plus 130 sale_or_lease | Complete public GET feed; 2,021 active rows live; full run, live ingest, and validation done |

Buildout semantics (svn, lee-associates): the inventory feed has **no
server-side sale/lease filter** (`lease=true` is ignored). Items carry
`sale: boolean` (false = lease availability), `also_for_sale_or_lease`,
`sublease`, `closed`. The collector fetches the full inventory once per
brokerage (cached across both transaction passes), skips `closed`, and
partitions client-side. Dual-mode properties appear twice (`-sale`/`-lease`
propertyId suffixes); the ingestor merges them.

Rate limiting: Buildout occasionally returns HTML interstitials under
sustained paging. `scrapeJson` retries with backoff; the inventory fetch
tolerates isolated page failures but aborts the source if more than ~3% of
pages fail, then caches that failure for the second transaction pass. This
prevents a gappy run from soft-deleting live rows downstream. Lee now uses the
durable Buildout page cache controls documented in its broker README; assemble
from cache only after pages 0 through 332 are present.

## cre_ingest.py

Maps collector JSON to `credeals.cre_listings` (+ contacts/documents/images
children, + `cre_scrape_jobs` row per brokerage). Stdlib only; talks to
Postgres via psql (Homebrew libpq at `/opt/homebrew/opt/libpq/bin/psql`).
Document and image child rows store external URLs only. The collector does not
download or upload PDF/image binaries into Supabase storage.

Credentials: reads `POSTGRES_URL_NON_POOLING` (preferred) or `POSTGRES_URL`
at runtime from `dynamically-display-cre-listing-data/.env.local` (fallback
`CRE_EQUIRE/.env.local`), or `--env-file`. Values are never printed or
persisted into artifacts. Never commit them.

Key behavior:
- Dedup key `(brokerage_id, external_id)`; sub-sources fold into the parent
  brokerage with prefixed ids (`dealflow:`, `investor:`). Missing ids get
  `url:<sha1-16>` synthesized from the listing URL.
- A listing collected in both sale and lease passes merges to
  `transaction_type='sale_or_lease'`.
- `cap_rate` stored as fraction [0,1]. Lease rates parsed only when
  explicitly $/SF (monthly per-SF annualized); everything else stays in
  `raw_data` (full original payload, always kept).
- Upsert refreshes content fields, resurrects soft-deleted rows
  (`deleted_at=NULL`), and wholesale-replaces child rows for touched
  listings.
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

Read `SUPABASE_SECURITY_NOTE_2026-06-12.md` before changing grants, views,
or function privileges. The display app hardened view security and revoked
public execute on helper functions while preserving service-role collector use.

## Daily updates

`cre_daily_update.sh` = healthcheck -> full collect (sale+lease, unlimited)
-> ingest -> prune old artifacts (keeps 14 runs). Logs in `out/daily/`.
The script default includes `--mark-missing`; while the `colliers-main` full
run is still in progress and Savills remains partial, keep daily ingest
additive with `bash cre_daily_update.sh --no-mark-missing`. Latest measured full collection was about 27 minutes at concurrency
3; additive ingest finished in under a minute.

## Adding a source

1. Write `srcNewSource(tx, max)` in `collect.ts` returning `SourceResult`;
   register it in `SOURCE_KEYS` + `runSource`.
2. Map its key in `cre_ingest.py` `SOURCE_TO_BROKERAGE` (new slug -> add a
   seed row in `../sql/001_cre_brokerages.sql` and apply it).
3. Probe: `npx tsx collect.ts --source=<key> --transaction=both --max-items=6`,
   then `cre_ingest.py --dry-run` and check the staged TSV row.
