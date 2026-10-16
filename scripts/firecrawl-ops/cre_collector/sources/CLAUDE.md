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

For provider or page variations, use the [brokerage completion playbook](../../../../docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md)
for evidence and fail-closed rules. Keep the source-specific canonical
external-ID recipe, and use one source-owned comparison normalizer in mapping
and verification. Preserve raw source IDs and URLs in provenance; add only
primary-evidenced aliases or layout fallbacks with regression tests. Do not
infer a provider change from a parser gap.

Essex listing keys preserve the validated owned property's literal URL pathname
without outer slashes, decoding or lowercasing, matching the historical
properties/<slug> namespace. The observed currentPropertyId
remains raw essexPropertyId metadata: distinct real pages can share it, so it is
not a unique page identity. New essexWordpressPage provenance explicitly identifies
one positive postid-* on the canonical single-properties page. Version-2
receipts bind that typed page ID separately from the raw variable; old evidence
is not reinterpreted. Duplicate page IDs or output paths fail closed; a changed
path never authorizes a rekey. Canonical detail and lifecycle checks remain
required. Missing historical typed page IDs remain unknown; path/URL
contradictions still refuse. This rule does not admit first-seen writes or
authorize closed/missing lifecycle updates.
Current Essex inventory includes on-market and under-contract properties only;
the closed archive reconciles coverage but never inflates current counts. With
no baseline the raw gate reports `first_seen` before the default 100-row floor;
Essex's explicit singleton current-inventory admission requires two matching
complete current/closed/sitemap reconciliations and every fresh current detail.
Closed cards bind coverage URLs/lifecycle only and never enter current output.
Only successful full, uncapped, non-monitor proof can supply the exact same
generation/start's empty lease receipt. Raw `first_seen` and baseline absence
remain; sub100 current cohorts may qualify without lowering any global floor or
waiving baseline-backed holds. Prewrite checks refuse contradictory literal
URL/path/known native-ID associations across all historical parents. These
implemented checks do not establish fresh production acceptance or authorize
closed/missing lifecycle changes.

Lyon-Stahl property sitemaps may include the exact owned listing archive
`https://lyonstahl.com/properties/`. Exclude only that URL from detail enumeration;
every other member still requires a valid owned detail URL, and empty or
archive-only shards fail. Sitemap membership does not establish active status,
historical identity compatibility or first-seen admission.

DAUM and Foundry output keys preserve the validated owned property's literal
pathname without outer slashes (`property/<slug>`); Lyon-Stahl preserves only
the final literal slug. Keep case and percent spelling unchanged. Positive
WordPress IDs remain separate `daumPostId`, `foundryPostId`, or `lyonStahlPostId`
provenance. Native IDs and output keys must each be unique within an emitted
source pass; changed paths never authorize aliases or rekeys. These recipes do
not establish current availability, change strict detail/lifecycle checks or
admit absent-baseline writes. Foundry's missing explicit status remains held.

## Transport & Scrape Primitives

| Transport | Sources |
|-----------|---------|
| Firecrawl `scrapeJson` / `scrapeDoc` / `scrapeRaw` | cbre, colliers-main, jll, jll-investor, cushman, transwestern, avison-young (detail), newmark (cred bootstrap), buildout (fallback), savills (validated fallback only) |
| Direct `fetch` (no Firecrawl) | cbre-dealflow, colliers (RCM), newmark (Algolia), marcus-millichap, nai-global, buildout (preferred path for registered sources), savills (server-rendered public list pages) |

Shared: `lib/scrape.ts` (3× scrape retry, `jsonAttempts`/`jsonBackoffMs` for interstitials), `lib/broker.ts`, `lib/html.ts`, `lib/util.ts` (`pmap`, `prune`).

Marcus direct POST and detail HTML requests share a fixed 30-second deadline
covering response headers and body. Timeouts abort the request and attempt to
cancel unread or late response bodies, without adding transport retries or
changing strict detail evidence.
The guarded owner's overall work deadline remains a separate bound.
Strict map inventory recognizes the native `Results: [...]` envelope as well
as the existing `Properties` array envelope. Neither shape supplies the source
total: the separate properties response must still provide a finite integer
`TotalCount` matching the unique map ActivityIds before detail expansion.

Matthews failed HTTP attempts emit bounded `matthews/http` diagnostics containing
only request role, attempt/max, header/body/HTTP phase, numeric status when known,
elapsed milliseconds and the planned cooldown. Successful requests stay quiet;
URLs, headers, response bodies and exception text are never included in these
lines. The existing 30-second per-phase deadlines, six attempts, provider pacing
and all cooldowns remain unchanged. A final cooldown is labeled before failure,
not as a seventh retry: it may protect the next transaction or outer acquisition.

Pyramid's WordPress inventory requests only `_embed=wp:term`: the mapper needs
those taxonomy labels for state, city, county and property type. Numeric term IDs
alone are insufficient. Keep 100-record pages, bounded response bodies and complete
ordered snapshot convergence; full mode still requires each selected detail page.
The [WordPress REST contract](https://developer.wordpress.org/rest-api/using-the-rest-api/global-parameters/#_embed)
supports selecting the term relation without broadly embedding other resources.

Pyramid canonical listing keys preserve the validated owned URL's literal pathname
without surrounding slashes. Do not decode or lowercase that path. Numeric
WordPress post IDs remain separate inventory/detail-shortlink evidence, and two
posts claiming the same canonical path fail closed. A changed path is a different
key, never an automatic rekey. Full detail mapping reads literal property-map
address text and scoped property photos; only the observed HTTPS image CDN is
allowed alongside owned images. Detail ownership stays on the Pyramid host.
No office-map/footer-logo fallback or coordinate inference is permitted.

Pyramid mapping and strict verification share exact detail transaction classification:
`Sale` maps to sale; `Lease` and `Sublease` map to lease, ignoring surrounding
whitespace and case while retaining the literal `detailFacts` value. Unknown or
ambiguous values and disagreement with inventory taxonomy fail with post ID and
bounded raw fact. Do not guess from descriptions or default unfamiliar labels
to sale; validate observed labels without loosening ownership or detail proof.

Pyramid strict first-seen proof is singleton full/uncapped sale+lease only. A
successful immutable complete snapshot may be reused only within the exact
nonempty generation+start; monitor, capped and standalone calls do not populate
or consume it. Receipts retain two consecutive equal full-payload passes and
each fresh detail's native ID, owned canonical URL, observation and body digest.
No missing detail or absent taxonomy label is covered by an inventory receipt.
When the fresh detail has property facts but genuinely no Sale/Lease field, only
that generation's complete inventory row may supply tenure: its published
`pbc-listings` identity/link and single record-owned embedded `sale-type` term
must agree literally (246/Sale/sale or 249/Lease/lease), bound to the second
converged page receipt. `detailTransactionFacts` records presence separately;
`transactionEvidence` identifies the HTML fact or bound WordPress term without
inventing a detail fact. Python source and aggregate admission independently
check this evidence. Present blank, unknown, duplicate/hidden or conflicting
property facts still fail. Global search filters, a numeric taxonomy ID alone,
content modification clocks and descriptions never establish tenure. This
adds no per-post inventory requests and leaves every fresh detail required.

## external_id Rules (collector id → ingest)

| Source | Stable `external_id` | Gotcha |
|--------|---------------------|--------|
| cbre | `Common.PrimaryKey` | Dual-aspect docs → `sale_or_lease` merge across passes |
| cbre-dealflow | `dealflow:` + detail `data.projectid` | Card `listingPv` ≠ projectid (~78%); monitor disabled |
| colliers (ST) | SLP `summary.ProjectId` | Index-paired card id ≠ ProjectId (~45%); **lease skipped**; monitor disabled |
| colliers-main | `main:` + `usa#####` | Monitor: sale pass only; full pass filters tx after enrich |
| jll | detail `property.id` (numeric) | URL slug ≠ id; monitor disabled |
| jll-investor | `investor:` + Salesforce `listing.id` | Sitemap slug ≠ id; US filter post-detail; no coords |
| cushman-wakefield | `url:v1:` + first 32 hex characters of normalized public-URL SHA-256 | Provider GUID is observational, not canonical identity; strict inventory preserves detail-only fields and children |
| transwestern | `PageUrl` slug | Invalid rows fail closed. The provider-published `-` slug requires fresh unique official-sitemap membership and an exact primary name/full-address match, including in monitor mode. |
| newmark | Algolia `slug` | Algolia 1000-hit cap per query; state+type sub-split |
| marcus-millichap | `DealId` from map tile | **Lease skipped**; list API caps ~100; map ActivityIds required |
| avison-young | SharpLaunch `row.id` | Strict refresh validates every selected property detail; only ordinary unlimited runs default to no detail enrichment |
| interra-realty | Numeric WordPress `ID` | Legacy `listing/<slug>` records require an explicitly reviewed identity transition; additive ingestion refuses exact-URL cross-identity collisions |
| nai-global | `infabode:{id}` (prefixed in adapter) | Bulk public `publicPosts` detail fields; full and monitor share the conservative `FOR_SALE_ON_MARKET` source-eligibility rule. Do not activate provider status. |
| Buildout sources | `propertyId` base from URL | Strip `-(sale|lease)`; dual rows merge to `sale_or_lease` |
| savills | `ExternalPropertyID` | Direct server-rendered `__NEXT_DATA__`; provider `NextUrl` only, so an invalid or incomplete page fails closed |

## Monitor Mode Matrix

| Behavior | Sources |
|----------|---------|
| Monitor ≈ full (same rows) | cbre, registered Buildout sources, savills |
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
| `buildout.ts` | Shared fail-closed inventory adapter |
| `buildout-registry.ts` | Governed single-token Buildout source definitions |
| `savills.ts` | Sale and lease: structured `__NEXT_DATA__`; provider `NextUrl` pagination; direct fetch with validated Firecrawl fallback |

## Buildout (`buildout.ts`) - Shared Adapter

- **Diagnose the actual adapter before changing its transport.** From a clean,
  genuine isolated clone, replay the registered source's `buildoutInventoryUrl`
  and `directBuildoutJson` with its configured sort and Referer. Compare the
  exact query, User-Agent, Accept, Referer and redirect behavior: a different
  ad-hoc client returning HTML or repeated pages does not establish a current
  collector failure. Keep diagnostic requests bounded, with no automatic
  retries or fallback, and retain private raw bodies, observation times,
  request metadata and hashes. A successful page proves only that page;
  complete inventory still requires every declared page and unique-ID
  reconciliation. Reproduce the failure in the unchanged adapter and isolate
  the cause before adding URL variants, headers, cache changes or HTML parsing.
- **No server-side sale/lease filter**; one inventory fetch per `pluginKey`, `buildoutCache` shared across sale+lease passes.
- Current-inventory receipts preserve literal owned HTTP(S) listing URLs, including
  one nonempty `propertyId` alongside empty template parameters and meaningful
  path-routed URLs using the existing native-ID fallback. Admission still requires
  complete pages, unique URL-to-stored-ID bindings and explained dual-mode merges.
  HTTP metadata proves neither redirects nor TLS; detail iframe resolution remains
  HTTPS-only and requires its existing query `propertyId`.
- **Registered strict sources**: `requireCompletePages: true` (any page fail aborts); stable provider ordering and durable source-scoped page cache at `out/cache/buildout/{slug}/page-NNNN.json`.
- Cache policy is fail-fresh by default: ordinary collection reads live and
  writes every validated page (`read=false`, `write=true`). Explicit
  `BUILDOUT_USE_PAGE_CACHE`, cache-only, and cache-assembly modes read and
  write. `BUILDOUT_REFRESH_PAGE_CACHE=1` forces live reads and still writes.
- Lee assembly: all pages 0–332 present before `BUILDOUT_ASSEMBLE_FROM_CACHE=1`. Partial windows without cache-only → hard error.
- `buildoutFailureCache`: sale pass failure blocks lease pass retry in same process.
- Env: `BUILDOUT_CACHE_DIR`, `BUILDOUT_PAGE_START`/`END`, `BUILDOUT_CACHE_ONLY`, `BUILDOUT_ASSEMBLE_FROM_CACHE`, `BUILDOUT_PAGE_JITTER_MS`.
- Manual freshness run: `BUILDOUT_REFRESH_PAGE_CACHE=1` bypasses durable page-cache reads, fetches the current inventory once per source invocation, and overwrites each successfully fetched cache page. It keeps the in-process sale/lease share. It fails fast if combined with cache-only or cache-assembly recovery modes.
- The governed registry contains all 25 historical single-token public feeds
  recovered from commit `6245a7144`. `REGISTERED_BUILDOUT_SOURCE_KEYS` is the
  exact typed inventory; registry, ingest, checkpoint, SQL, and enrichment
  parity tests prevent partial admission. Offline proof does not replace a
  current live canary and guarded database readback for each source.
- Listing ownership centrally treats the same DNS host with one optional leading
  `www.` label as equivalent, in both producer/detail checks and Python admission
  (AGENTIC-4271, parent AGENTIC-1972). It does not admit other domains,
  subdomains, explicit ports, credentials, fragments or non-HTTPS URLs. Original
  listing URLs, property-ID identity and configured plugin/page/iframe coordinates
  remain intact; endpoint and source-proof binding stay exact.
- A different DNS domain requires a reviewed source-specific coordinate update,
  not a broader host alias. Bind the current official firm identity and its
  properties page to the exact registered Buildout token; retain primary
  evidence on AGENTIC-4271. An unavailable legacy host is not proof of a
  redirect or rebrand. Update only the verified registry page/host, preserving
  source keys, plugin tokens, original listing URLs and listing identities.
- Hudson's October 15, 2026 public feed and historical URLs use
  `hudsonpeters.com`; its tuple keeps that observed coordinate. Both old and
  corrected composed detail iframe probes returned 404. Host compatibility does
  not establish live detail availability.

## Kidder Mathews complete inventory

Strict Kidder runs request one `startIndex=0, numResults=8000` response from
`https://services.kidder.com/search/public/listing`, matching the public
property-search frontend's bulk request. This avoids unsorted 50-row page
boundaries; it does not deduplicate provider rows. The independent provider
total, returned count and unique `listing_key` set must agree within the
8000-row ceiling. A provider cap, duplicate identity, malformed body or larger
inventory fails closed. The transport disables redirects and bounds the whole
request/body read to 30 seconds and decoded content to 32 MiB. Generation-bound
caching preserves the actual observation clock, and non-strict pagination is
unchanged. Provider evidence and the guarded live acceptance are tracked by
[AGENTIC-4269](https://linear.app/agenticassets/issue/AGENTIC-4269); transport
success alone does not prove source-gate or database acceptance.

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
| `AVISON_YOUNG_DIRECT_DETAIL_ATTEMPTS` / `AVISON_YOUNG_DIRECT_DETAIL_RETRY_MS` | avison-young | Strict direct-property-detail retry count (default 3, max 5) and linear backoff base (default 250 ms, max 5 s). Only the failed page is retried; terminal failures report bounded listing IDs and reasons. |
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
  public brochure, plus cards with no link at all. They remain source-index
  inventory evidence and never become canonical comparable listings. Their
  provider-supplied agreement or brochure URL remains supporting evidence in
  `cre_source_index.url`. They use
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
# Ordinary unlimited Avison enrichment: AVISON_YOUNG_DETAIL_LIMIT=50 ...
# Strict Avison refresh requires all selected details; this limit cannot cap it.
# NAI full inventory: --page-cap=400
# Lee cache assembly: BUILDOUT_ASSEMBLE_FROM_CACHE=1 (after pages 0-332 cached)
```

## References

- Parent: `../collect.ts`, `../CLAUDE.md`
- Coverage counts: `../START_HERE.md`, `../BROKERAGE_STATUS_2026-06-12.md`
- Monitor layer: `../../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md`
- Colliers-main handoff: `../HANDOFF_COLLIERS_MAIN_2026-06-13.md`, `run_colliers_main_full.sh`
- CBRE reference: `../../prometheus/CLAUDE.md`
