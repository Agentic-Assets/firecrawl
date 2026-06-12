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

# The safe daily cycle while Lee remains blocked
bash cre_daily_update.sh --no-mark-missing
```

## collect.ts

Flags: `--source=all|csv` `--transaction=sale|lease|both` `--max-items` (0 =
unlimited) `--page-cap` (rendered-page sources, default 60; use 400 for full
runs) `--concurrency` (1-6, default 3) `--out=path`.

Env: `FIRECRAWL_API_URL` (default `http://localhost:3002`),
`FIRECRAWL_API_KEY` (any non-empty value for self-hosted).

### Source status

Latest full ingested all-source run started 2026-06-12 04:04:23 UTC. Cushman
& Wakefield was upgraded after that run on 2026-06-12 local time; its new source
totals below are from live targeted probes and still need a full collection plus
Supabase ingest before database counts reflect them. CBRE Deal Flow was upgraded
and ingested additively after that validation from a source-specific full run.

| Source key | Method | Sale | Lease | Notes |
|------------|--------|------|-------|-------|
| `cbre` | Internal JSON API, stealth proxy | 5,879 | 14,805 | Cloudflare; waitFor 4000 |
| `cbre-dealflow` | Public RCM ListingEngine endpoint | 1,809 public cards collected of 2,042 reported | 27 of 27 | Full source-specific run live-ingested additively from `out/cbre_dealflow_full_2026-06-12_041740.json`; ids prefixed `dealflow:` and folded into parent `cbre` |
| `jll` | Search pages, waitFor 8000 | 333 | 4,345 | tenure=sale / tenure=rent |
| `jll-investor` | `__NEXT_DATA__` first search page plus detail enrichment | 8 in hardened probe, source total about 1,087 to 1,088 | n/a | Sale-only partial; full completion needs pagination or sitemap policy decision |
| `cushman-wakefield` | Public `/api/properties/search` JSON plus detail enrichment | 2,743 live total | 8,575 live total | Full API pagination verified; detail pages enrich docs, photos, visible contacts, JSON-LD, and VCard/profile URLs. Use `CUSHMAN_QUERY='1800 Central'` for targeted probes |
| `newmark` | Algolia API (creds scraped from page) | 1,121 | 3,250 | No-state recovery added; latest full probe collected 4,371 |
| `marcus-millichap` | Public contentsearch sale API plus public detail HTML | 12 in probe, 3,126 reported public sale total | n/a | Sale-only until public lease inventory is proven; detail enriches contacts/images and keeps gated deal-room URLs in raw data only |
| `avison-young` | Public SharpLaunch feed | 636 staged sale rows | 1,431 staged lease rows plus 133 sale_or_lease | Full SharpLaunch run live-ingested additively; still needs optional detail-page enrichment for PDFs, richer galleries, JSON-LD, VCard/profile URLs |
| `savills` | Server-rendered pages /page/N | ~100 of 105 source cards | 0 | US lease inventory empty; foreign fallback cards filtered; US parser accepts state names, ZIP-only rows, and city/state/ZIP variants |
| `svn` | Buildout inventory API | 2,988 in latest full artifact | 2,533 in latest full artifact | Mapping complete from prior artifact, but fresh live refresh partial because Buildout returned 403 HTML during probes |
| `lee-associates` | Buildout inventory API | blocked in latest full run | blocked in latest full run | Buildout throttles under sustained paging; latest fresh retry passed pages 93-104 but failed pages 286-297 and aborted at 12/333 failed pages |
| `nai-global` | Public Infabode GraphQL feed and `publicPost` details | 6 in probe | 6 in probe | Stable `infabode:` ids and detail URLs; contacts only when public fields exist |
| `colliers` | Public SalesTracker RCM GET list/map plus SLP detail | 3 in probe, 1,653 SalesTracker filtered total | 0, main lease search blocked | Partial investment-sale coverage only; main `www.colliers.com/en/properties` Coveo sale/lease path remains blocked; no POST, agreement, or gated document path is used |
| `transwestern` | Public GET feed plus detail pages | 4 in probe | 4 in probe | Implemented and dry-run proven; full run and live ingest still needed |

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
prevents a gappy run from soft-deleting live rows downstream. Lee can serve
individual failed pages later, but a 333-page run still fails without a
throttling-safe or resumable strategy.

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
The script default includes `--mark-missing`; while Lee & Associates remains
blocked, run `bash cre_daily_update.sh --no-mark-missing` so the ingest stays
additive. Latest measured full collection was about 27 minutes at concurrency
3; additive ingest finished in under a minute.

## Adding a source

1. Write `srcNewSource(tx, max)` in `collect.ts` returning `SourceResult`;
   register it in `SOURCE_KEYS` + `runSource`.
2. Map its key in `cre_ingest.py` `SOURCE_TO_BROKERAGE` (new slug -> add a
   seed row in `../sql/001_cre_brokerages.sql` and apply it).
3. Probe: `npx tsx collect.ts --source=<key> --transaction=both --max-items=6`,
   then `cre_ingest.py --dry-run` and check the staged TSV row.
