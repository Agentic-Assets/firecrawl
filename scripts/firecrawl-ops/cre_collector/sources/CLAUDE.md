# sources/ Module

## Most Critical Rule

**Every adapter exports `src*(tx, max, monitor): Promise<SourceResult>`** and maps broker-site data into the shared listing vocabulary in `types.ts`. Register the source key in `collect.ts` (`SOURCE_KEYS` + `runSource`); map it in `cre_ingest.py` `SOURCE_TO_BROKERAGE` and `../sql/001_cre_brokerages.sql`. **Never ingest a `--monitor` artifact through `cre_ingest.py`** (sparse rows wipe enriched prices, `raw_data`, and child tables).

## Adapter Contract

- Return `{ company, sourceUrl, method, totalAvailable, listings, note?, truncated? }`.
- Listings: `id`, `name`, `transactionType`, address/geo, prices, `brokerIds` (`brokerRef()`), `brochures`, `photos`, `url`, `lastUpdated`; use `prune()` before emit.
- **`truncated?: boolean`**: set when this pass collected less than the feed's
  reported total (or hit a hard cap). `cre_monitor.py` treats `truncated` like
  `error` for disappearance gating. Adapters that set it: `newmark` (Algolia
  ~1000-hit cap unsplit), `cbre` and `cushman-wakefield` (collected <
  `min(max, reported total)`), `nai-global` (`--page-cap` clip with a full last
  page), and `colliers-main` (deferred or errored detail renders).
- **`monitor=true`**: cheap enumeration only. Skip detail unless the feed is already complete.
- Folded sub-sources get ingest prefixes (`dealflow:`, `investor:`, `main:`), not adapter prefixes. `colliers-main` emits bare `usa#####`; ingest adds `main:`.

## Transport & Scrape Primitives

| Transport | Sources |
|-----------|---------|
| Firecrawl `scrapeJson` / `scrapeDoc` / `scrapeRaw` | cbre, colliers-main, jll, jll-investor, cushman, transwestern, avison-young (detail), newmark (cred bootstrap), buildout (fallback), savills (validated fallback only) |
| Direct `fetch` (no Firecrawl) | cbre-dealflow, colliers (RCM), newmark (Algolia), marcus-millichap, nai-global, buildout (preferred path for svn/lee), savills (server-rendered public list pages) |

Shared: `lib/scrape.ts` (3× scrape retry, `jsonAttempts`/`jsonBackoffMs` for interstitials), `lib/broker.ts`, `lib/html.ts`, `lib/util.ts` (`pmap`, `prune`).

## external_id Rules (collector id → ingest)

| Source | Stable `external_id` | Gotcha |
|--------|---------------------|--------|
| cbre | `Common.PrimaryKey` | Dual-aspect docs → `sale_or_lease` merge across passes |
| cbre-dealflow | `dealflow:` + detail `data.projectid` | Card `listingPv` ≠ projectid (~78%); monitor disabled |
| colliers (ST) | SLP `summary.ProjectId` | Index-paired card id ≠ ProjectId (~45%); **lease skipped**; monitor disabled |
| colliers-main | `main:` + `usa#####` | Monitor: sale pass only; full pass filters tx after enrich |
| jll | detail `property.id` (numeric) | URL slug ≠ id; monitor disabled |
| jll-investor | `investor:` + Salesforce `listing.id` | Sitemap slug ≠ id; US filter post-detail; no coords |
| cushman-wakefield | API `row.id` | Monitor omits real prices (detail-only) |
| transwestern | `PageUrl` slug | Invalid `PageUrl` rows dropped at feed stage |
| newmark | Algolia `slug` | Algolia 1000-hit cap per query; state+type sub-split |
| marcus-millichap | `DealId` from map tile | **Lease skipped**; list API caps ~100; map ActivityIds required |
| avison-young | SharpLaunch `row.id` | **Full runs skip detail** unless `AVISON_YOUNG_DETAIL_LIMIT` set |
| nai-global | `infabode:{id}` (prefixed in adapter) | Bulk public `publicPosts` detail fields; full and monitor share the conservative `FOR_SALE_ON_MARKET` source-eligibility rule. Do not activate provider status. |
| svn / lee | `propertyId` base from URL | Strip `-(sale|lease)`; dual rows merge to `sale_or_lease` |
| savills | `ExternalPropertyID` | Direct server-rendered `__NEXT_DATA__`; provider `NextUrl` only, so an invalid or incomplete page fails closed |

## Monitor Mode Matrix

| Behavior | Sources |
|----------|---------|
| Monitor ≈ full (same rows) | cbre, buildout (svn/lee), savills |
| Monitor = enum, skips detail | cushman, marcus, colliers-main, newmark (skips People lookup), avison-young, transwestern |
| Monitor ≈ full (bulk public detail fields) | nai-global |
| Monitor returns `[]` | jll, jll-investor, cbre-dealflow, colliers (ST) - id only on detail page |

Monitor artifacts → `cre_monitor.py` only. Sources with `[]` stay on full-sweep cadence.

## File Map

| File | Pattern |
|------|---------|
| `cbre.ts` | Internal JSON API, stealth, `waitFor: 4000` |
| `cbre-dealflow.ts`, `colliers.ts` | RCM GET + SLP detail; direct `fetch`; detail concurrency `min(CONCURRENCY, 2)` |
| `cushman-wakefield.ts`, `transwestern.ts`, `avison-young.ts` | Public API/feed + per-URL detail enrich |
| `newmark.ts` | Algolia via `fetch`; People lookup on full path only |
| `nai-global.ts` | Infabode GraphQL `publicPosts` bulk detail feed; identical conservative eligibility on full and monitor paths |
| `marcus-millichap.ts` | Map ActivityId tiles + detail HTML; JSONL detail cache |
| `jll.ts` | Search pages + `__NEXT_DATA__`; disk detail cache `out/cache/jll-detail/`; strict runs set `JLL_DETAIL_CACHE_MIN_CACHED_AT` |
| `jll-investor.ts` | US sitemap + detail; sale only |
| `colliers-main.ts` | XML sitemap + JSON-LD; resumable JSONL cache; live sitemap `lastmod` controls reuse; CF challenge retries |
| `buildout.ts` | Shared inventory; svn/lee wired in `collect.ts` |
| `savills.ts` | Sale and lease: structured `__NEXT_DATA__`; provider `NextUrl` pagination; direct fetch with validated Firecrawl fallback |

## Buildout (`buildout.ts`) - Shared Adapter

- **No server-side sale/lease filter**; one inventory fetch per `pluginKey`, `buildoutCache` shared across sale+lease passes.
- **svn/lee**: `requireCompletePages: true` (any page fail aborts); durable page cache at `out/cache/buildout/{slug}/page-NNNN.json`.
- Lee assembly: all pages 0–332 present before `BUILDOUT_ASSEMBLE_FROM_CACHE=1`. Partial windows without cache-only → hard error.
- `buildoutFailureCache`: sale pass failure blocks lease pass retry in same process.
- Env: `BUILDOUT_CACHE_DIR`, `BUILDOUT_PAGE_START`/`END`, `BUILDOUT_CACHE_ONLY`, `BUILDOUT_ASSEMBLE_FROM_CACHE`, `BUILDOUT_PAGE_JITTER_MS`.
- Manual freshness run: `BUILDOUT_REFRESH_PAGE_CACHE=1` bypasses durable page-cache reads, fetches the current inventory once per source invocation, and overwrites each successfully fetched cache page. It keeps the in-process sale/lease share. It fails fast if combined with cache-only or cache-assembly recovery modes.

## Key Env Vars (by source)

| Var | Source | Effect |
|-----|--------|--------|
| `CUSHMAN_QUERY` | cushman | Targeted API probe |
| `CUSHMAN_DETAIL_MODE` | cushman | `full` renders detail pages; `base` refreshes API inventory and explicitly preserves existing child collections |
| `COLLIERS_MAIN_*` | colliers-main | Detail concurrency, wait, challenge retries, `MAX_FETCHES_PER_RUN`, and run-scoped `DETAIL_CACHE_PATH` |
| `JLL_DETAIL_*` / `JLL_INVESTOR_*` | jll, jll-investor | Detail concurrency, wait, cache dir, minimum cache timestamp, sitemap scan limit |
| `SAVILLS_DIRECT_LIST_TIMEOUT_MS` | savills | Direct public list-page timeout (default 25s; bounded 5–60s; two attempts) |
| `SAVILLS_LIST_TIMEOUT_MS` | savills | Firecrawl fallback list-page timeout (default 30s; bounded 10–90s) |
| `NAI_GRAPHQL_TIMEOUT_MS` / `NAI_SOURCE_BATCH_SIZE` / `NAI_PAGE_SIZE` / `NAI_ENUMERATION_CONCURRENCY` | nai-global | Bound each `publicPosts` GraphQL body read (default 30s), split source-office filters (default 40), request up to 100 bulk-detail rows per page, and enumerate unlimited batches at a bounded fan-out of two. A timeout or page cap fails closed for monitor coverage. |
| `AVISON_YOUNG_DETAIL_LIMIT` | avison-young | **Required** for detail on unlimited full runs |
| `AVISON_YOUNG_DETAIL_CONCURRENCY` | avison-young | Detail parallelism |
| `--page-cap` | jll, colliers*, nai | Caps rendered pages / feed offsets |
| `--concurrency` | all Firecrawl-heavy | `pmap` limit (1–6) |

## Detail Failures & Status

- Most detail-enrich sources return rows with `detailError` string; ingest skips child-row refresh when `detailError` in `raw_data`.
- A provider-confirmed absence of public detail is not a scrape error. Such rows
  use structured `detailUnavailable` plus `preserveChildCollections=true`;
  fresh card fields still upsert, while `scraped_at` and the top-level raw
  payload remain the last successful detail observation. The current sparse
  payload and time are stored as `latestInventoryObservation` and
  `inventoryObservedAt`. Existing detail children remain; current card contacts
  and images are applied additively, and new listings insert card children.
- CBRE Deal Flow includes public cards whose only link is a gated agreement or
  public brochure, plus cards with no link at all. All remain inventory and use
  `gated_agreement`, `public_brochure_only`, or `card_not_linked`
  detail-unavailable reasons. Unlinked
  cards receive a deterministic provisional `card:` identity from their
  visible card fields and point to the public Deal Flow index. Because those
  fields are not a provider key, a later unlinked-to-linked transition is never
  auto-merged; reconcile it only with explicit evidence.
- A small number of CBRE/LightBox landing pages are valid public HTML but omit
  the normal embedded data object. They use `public_html_only`, prove that the
  public page was observed, and preserve last-good detail. The collector fails
  closed if this state exceeds the larger of five listings or 1% of a source
  pass, which guards against silently accepting a provider-wide layout change.
- Deliberately incomplete API/base rows carry `preserveChildCollections=true`;
  ingestion also excludes them from wholesale child replacement.
- A live ingest accepts only `runMeta.mode="full"` or `"enrich"`. Monitor
  artifacts can still be inspected with `--dry-run`, but cannot reach the
  database.
- Native terminal status: cbre-dealflow, colliers-main, cushman (`listingStatus`). NAI's provider status is used only for conservative source eligibility and is never activated. **Disappearance-only** (no status field): jll, jll-investor, newmark, marcus, savills, transwestern (monitor).

## Probe One Source

```bash
npx tsx collect.ts --source=<key> --transaction=both --max-items=6 --out=/tmp/probe.json
python3 cre_ingest.py --in /tmp/probe.json --dry-run
# Avison full detail on unlimited run: AVISON_YOUNG_DETAIL_LIMIT=50 ...
# NAI full inventory: --page-cap=400
# Lee cache assembly: BUILDOUT_ASSEMBLE_FROM_CACHE=1 (after pages 0-332 cached)
```

## References

- Parent: `../collect.ts`, `../CLAUDE.md`
- Coverage counts: `../START_HERE.md`, `../BROKERAGE_STATUS_2026-06-12.md`
- Monitor layer: `../../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md`
- Colliers-main handoff: `../HANDOFF_COLLIERS_MAIN_2026-06-13.md`, `run_colliers_main_full.sh`
- CBRE reference: `../../prometheus/CLAUDE.md`
