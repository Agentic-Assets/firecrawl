# CRE Listing Intelligence: Unified System Architecture

Owner: EQUIRE deal-intelligence feed. Status: design, ready for phased build.
Last reviewed against code: 2026-06-13 (collect.ts 5,558 lines; cre_ingest.py 867 lines; sql/001-006).

## 1. Purpose and goals

EQUIRE needs one system that does three things across many brokerage sites:

1. ACQUIRE every commercial-real-estate for-sale and for-lease listing (the inventory).
2. DETECT NEW listings added to any source, with low latency and without re-scraping everything.
3. DETECT CHANGES to existing listings: status lifecycle (for_sale to under_contract to pending to sold or leased or withdrawn) and price moves, plus disappearance and re-listing.

The current system does (1) well (about 34,000 active rows from 15 source adapters across 13 parent brokerages) and does (2) and (3) only implicitly. This document defines a cohesive architecture that keeps the working acquisition layer, adds a persisted change ledger and a normalized status, and layers an incremental monitor and safety gates on top, with every adversarial review fix folded in.

## 2. What already exists (verified) vs what must be built

### Already exists and is reused as-is
- collect.ts: 15 source adapters dispatched by a 15-case `runSource` switch (lines 5400-5469), each returning the shared `SourceResult` contract (342-349) and the flat listing field vocabulary (336-340). Shared primitives: `scrapeRaw`/`scrapeDoc`/`scrapeJson` with retry and JSON repair, `pmap` bounded concurrency, `brokerRef` global dedup, two sitemap parsers (`extractSitemapLocs`, `extractSitemapUrlEntries` returning {loc, lastmod} at 4627-4638), `colliersMainIsChallenge` (4585), durable JSONL detail cache, and the bounded-fetch resume pattern (4944-5040).
- cre_ingest.py: COPY-to-staging then single-statement CTE upsert keyed on (brokerage_id, external_id), with prefixed id folding (`dealflow:`, `investor:`, `main:`) and per-brokerage mark-missing eligibility (797-812). `raw_data = listing` (437) so every source payload, including source status fields, is already persisted.
- SQL schema sql/001-006: `cre_listings` with a status CHECK allowing six values (002:27-28), child tables, `cre_scrape_jobs`/`cre_scrape_log` (003), GIN index on raw_data (004), four EQUIRE views plus `search_cre_listings()` all gating on `status = 'active' AND deleted_at IS NULL` (005:73,108-109,145-146,174-175,237-238), and the BEFORE UPDATE `updated_at` trigger (005:279-283).
- Ops: cre_daily_update.sh (healthcheck, full collect at page-cap 400 concurrency 3 in about 27 minutes, ingest, prune), run_colliers_main_full.sh (bounded-chunk resumable driver with convergence detection), cre_validate.py (read-only 8-query quality reporter), and the launchd plist template.
- cre_validate.py `SOURCE_KEY_SQL` (20-28): a CASE that derives the logical source key from the prefixed external_id (`dealflow:%`, `investor:%`, `main:%`) plus a `raw_data->>'sourceKey'` fallback. This is the reusable prefix-aware source resolver the new code must share.
- Local self-hosted Firecrawl at http://localhost:3002 with stealth proxy (handles Cloudflare).

### Must be built
- A source-status normalizer (`norm_status`) in cre_ingest.py modeled on the existing `transaction_type_of` and `PROPERTY_TYPE_RULES` ordered-rule patterns, returning the canonical status or NULL when no signal exists.
- A widened status CHECK (add under_contract, pending, off_market) and new columns: last_seen_at, source_lastmod, canonical_key.
- An append-only event ledger `cre_listing_events` (new, status_change, price_change, disappeared, reappeared, possible_relist).
- A persisted enumeration index `cre_source_index` keyed exactly like cre_listings (brokerage_id, external_id), so the monitor and the pipeline agree on ids.
- A change-detecting upsert: a `_before` snapshot CTE that captures OLD values before the overwrite, emitting events in the same transaction, with COALESCE(status) and conditional deleted_at to stop resurrection of terminal rows.
- An incremental monitor tier (cheap enumeration plus diff) split by source class, feeding only NEW and CHANGED ids to a bounded enrichment worker for the two sitemap-detail sources, and reading NEW or CHANGED directly from the daily full run for the bulk-API sources.
- A coverage-and-anomaly GATE (cre_gate.py) that makes mark-missing prefix-aware and refuses to soft-delete on partial or short runs.
- A mutual flock lock across all three drivers, a draft-only notify hook, and per-URL `cre_scrape_log` writers.

## 3. System overview and unified data flow

The system is one logical pipeline with two cadences sharing one schema and one ingest path.

```
SOURCES (15 adapters, 13 brokerages)
     |
     |  (A) cheap enumeration: sitemap id+lastmod OR bulk JSON id-set
     v
[ MONITOR ]  cre_monitor  ->  diff against cre_source_index
     |                          |
     |  Tier-A (bulk API): NEW/CHANGED come straight from the full daily run
     |  Tier-B (sitemap):  NEW/CHANGED ids -> cre_enrichment_queue
     v                          v
[ ACQUISITION ]  collect.ts (full daily run)  +  enrich_queue (id-scoped, Tier-B only)
     |                          |
     v                          v
[ GATE ]  cre_gate.py: per-source coverage + anomaly + challenge-rate checks
     |     decides the prefix-aware mark-missing allowlist; quarantines bad sources
     v
[ INGEST ]  cre_ingest.py upsert (changed):
     |   _before snapshot -> norm_status -> upsert with COALESCE(status), conditional deleted_at
     |   -> emit cre_listing_events (new/status_change/price_change/reappeared)
     |   -> mark-missing (prefix-aware, gated) -> disappeared events
     v
[ DATA MODEL ]  cre_listings (+ status, last_seen_at, source_lastmod, canonical_key),
                cre_source_index, cre_enrichment_queue, cre_listing_events
     v
[ VALIDATION ]  cre_validate.py --gate (thresholds, baseline delta) + draft-only alert
     v
EQUIRE views (active + a new pending/under-contract surface) and v_cre_recent_changes
```

The single most important invariant: the monitor index, the ingest dedup key, and the validate source-resolver all key on the SAME prefixed external_id. This is what makes NEW detection correct rather than a per-run false-positive storm.

## 4. Subsystem 1: reusable acquisition

### Decision: do NOT rewrite collect.ts into a framework first
The adversarial review is correct that a 5,558-line to 25-module refactor delivers only ergonomics (adding source 16 is four touchpoints instead of two lists) and is sequenced ahead of its consumers. Sources are added rarely. The refactor is deferred to the last phase and only after the change-detection and monitor consumers exist to dictate a thin interface. Until then, collect.ts stays the single tsx entrypoint so cre_daily_update.sh and launchd are untouched.

### What is built now in the acquisition layer (minimal, consumer-driven)
1. Extract the two sitemap parsers and the per-source bulk-list enumeration cores into exported helper functions (no behavior change to the full run) so the monitor can import them.
2. Add an `--ids` or `--queue` id-scoped mode to collect.ts so existing adapters can render a specific URL set for the Tier-B enrichment worker, reusing the per-source detail parsers verbatim.
3. Generalize the colliers-main durable-cache plus bounded-fetch resume engine (4944-5040) into a shared module, used by the enrichment worker, with a SEPARATE cache namespace per mode (out/cache/monitor/<source>/...) so the monitor never appends to the live colliers-main detail-cache.jsonl an in-progress full run is using.
4. Add a small, declarative per-source descriptor table (the monitor registry) carrying ONLY fields with a current consumer: enumeration method (sitemap, bulk_api, render_list, none), enumeration_key (the field that becomes listing.id), supportsSale, supportsLease, foldsInto, idPrefix, and a per-source coverage floor. Speculative capability flags are deferred.

### The enumeration-key invariant (folds in the FATAL Marcus fix)
Before any monitor code ships, a unit test asserts per adapter that `enumeration_key == the field that becomes listing.id == cre_ingest external_id (minus prefix)`. Verified failure: Marcus enumerates on `row.ActivityId` (collect.ts:2421) but the listing id is `row.DealId` (2389). The monitor must key cre_source_index on DealId (carrying ActivityId only as the map-fetch handle), or every Marcus listing is flagged NEW forever. Buildout (svn, lee-associates) is the second trap: ingest external_id strips `-sale`/`-lease` from the propertyId (cre_ingest.py:319-322), so the monitor must apply the same collapse before diffing. The test fails loudly on any drift.

### Source class taxonomy (drives everything downstream)
- Tier-A, bulk JSON or feed returns full id-set plus native fields in one cheap pass without per-detail render: cbre, cushman-wakefield, newmark, marcus-millichap, nai-global, svn, lee-associates, avison-young, transwestern, plus the RCM card sources cbre-dealflow and colliers (SalesTracker). NEW and CHANGED are derived from the existing daily full run (which already calls every Tier-A discovery) using last_seen_at, the new-row label, and a raw-payload fingerprint compare. No separate monitor-mode fork, no queue.
- Tier-B, public XML sitemap of detail URLs plus mandatory per-listing render: colliers-main (sitemap carries per-URL lastmod, verified 4637) and jll-investor. Cheap pass is a pure GET of the XML and a diff of {loc, lastmod}; NEW or CHANGED ids go to the durable-cache queue and worker.
- Tier-C, rendered SPA search, no public id list: jll (main). New-listing latency stays at the daily run today; a follow-up adds a search-pages-only enumeration tier (parse card ids and slugs before detail enrichment, skipping the render) to push latency below 24 hours. Flagged enumeration:render_list, not none.

## 5. Subsystem 2: new-listing monitor

### Loop
For each enumeration-capable source: (1) enumerate the cheapest id inventory; (2) diff the (external_id, lastmod) set against cre_source_index, which holds the prior accepted snapshot AND the deletion state mirrored from cre_listings; (3) classify each id as NEW, CHANGED (lastmod advanced or fingerprint moved), REAPPEARED (id exists in index AND cre_listings.deleted_at was set), or UNCHANGED; (4) for Tier-B, enqueue NEW and CHANGED into cre_enrichment_queue; for Tier-A, the daily run already ingested them, so the monitor only records the event; (5) the enrichment worker drains the queue id-scoped, then additive ingest.

### Folded review fixes
- jll-investor enumerates the POST-FILTER id-set, not the raw sitemap. The sitemap lists about 1,857 global URLs but only 934 survive the US filter (which requires detail render). The monitor diffs the sitemap against the 934 US ids persisted in cre_source_index; sitemap-only ids are queued as candidate-needs-render-to-classify, never counted as NEW or used to compute a disappearance ratio. Note: `jllInvestorDetailUrlsFromSitemap` (collect.ts:1604) is a flat loc regex that discards lastmod, so the monitor uses `extractSitemapUrlEntries` for jll-investor and falls back to detail dateModified when the sitemap has no per-URL lastmod.
- The monitor never resurrects daily-killed rows. The enrichment ingest uses the change-aware upsert (Subsystem 3), which does COALESCE(status) and clears deleted_at only when the detail render confirms a positive active status. A sold listing that lingers in a sitemap with a bumped lastmod gets re-rendered, sees its terminal status, and stays terminal.
- The monitor is signal-only for disappearance. enumeration_gone never drives a soft-delete; it emits an event. Soft-delete stays the gated daily mark-missing job. For non-sitemap sources, NEW and gone candidates require confirmation across N >= 2 consecutive passes plus an enumerated-count-vs-totalAvailable sanity check before emitting, which kills transient-miss false positives and feed-redirect false NEWs.
- Mutual flock. cre_monitor, cre_daily_update, and run_colliers_main_full all acquire the SAME lock, so a monitor pass yields to the live colliers-main enrichment the task warns about.
- Initial backfill seeds cre_source_index from current cre_listings INCLUDING soft-deleted rows with an explicit soft_deleted flag, so run one neither flags 34k rows as NEW nor suppresses real reappearance.

## 6. Subsystem 3: change detector

The center of gravity is the existing single-statement CTE upsert (cre_ingest.py:549-655), the one place OLD (t.*) and NEW (EXCLUDED.*) are both in scope.

### (1) Status normalization at ingest
Add `norm_status(listing)` modeled on `transaction_type_of` (294-299) and the ordered `PROPERTY_TYPE_RULES` (82-122). It reads the source status collect.ts already attaches (jll-investor status at 1619-1635; nai listingStatus at 3679; marcus newlyListed/newlyReduced at 2407; buildout underContract at 867; colliers and cbre-dealflow card .status; cushman listingStatus at 2170; colliers-main propertyStatus at 4934), then falls back to a conservative, source-scoped, word-boundary text scan of title/slug/JSON-LD name (SOLD or CLOSED to sold; UNDER CONTRACT or IN CONTRACT or UNDER OFFER to under_contract; SALE PENDING or PENDING to pending; LEASED to leased; WITHDRAWN or OFF MARKET to off_market).

Critical correctness rule (folds in the review fix): `norm_status` returns NULL, never 'active', when no explicit status is present. prune() (collect.ts:120) drops false and null, so `underContract:false` and absent `marcusFlags` are simply gone from the payload; treating absence as active would clobber a known terminal status on every status-less re-run. The INSERT path defaults to 'active'; the UPDATE path uses COALESCE(EXCLUDED.status, t.status). Dual sale/lease raw_data is nested as {primary, secondary_pass} after merge_rows (463-464); the normalizer handles both shapes, with terminal status from either pass winning.

### Honest per-source capability statement (folds in the overstated-promise fix)
Status transitions are observable only for a subset. Status-transition tier: jll-investor, cbre-dealflow, colliers (SalesTracker), colliers-main, cushman, buildout (svn, lee). Disappearance-only tier: cbre (about 19k rows, emits no status field), nai-global and avison-young (pre-filter to active at the collector), marcus (live map feed only). For the disappearance-only tier the only lifecycle signal is vanishing, which is the gated mark-missing job plus, for colliers-main, the per-URL 410. EQUIRE expectations and the documentation are scoped accordingly; full lifecycle is not promised for CBRE/NAI/Avison/Marcus.

### (2) Append-only event ledger
`cre_listing_events` is emitted inside the upsert transaction by extending the CTE with a `_before` TEMP TABLE snapshot taken before INSERT...ON CONFLICT runs, then joined to the upserted set. Events: new (no matching id in _before), status_change (old <> new status), price_change (one row per changed money field), reappeared (deleted_at was NOT NULL, now NULL), disappeared (from gated mark-missing or a confirmed 410), possible_relist (advisory). Every event INSERT is idempotent via NOT EXISTS keyed on (listing_id, event_type, field, new_value, run_id), so re-running an artifact emits nothing.

Events are derived strictly from real `_before`-vs-`_src` value deltas, never from updated_at (the trigger touches updated_at on every row, so it does not mean a change happened).

### scrape_job_id linkage (folds in the unsatisfiable-FK fix)
cre_scrape_jobs is currently inserted at the END of build_sql (658-665), one row per brokerage, with no id captured. The fix: generate one run-level uuid in Python at the top of build_sql, INSERT a per-run jobs row FIRST with that id, then stamp every event with that run_id. cre_listing_events.scrape_job_id references that pre-inserted row, so the FK is satisfied.

### (3) Prioritized re-scrape via lastmod (folds in the day-granularity fix)
updated_date is day-truncated by iso_date_or_none (282-286), so it cannot gate intra-day changes. A new `source_lastmod timestamptz` column stores the full timestamp; the Tier-B incremental gate re-renders a URL only when sitemap lastmod > stored source_lastmod (full-precision) or the id is new. When a sitemap entry has null lastmod, that URL falls back to always-re-render (no silent skip). The weekly full run is the acknowledged backstop for any lastmod-silent content change.

### (4) Disappearance vs sold (folds in the conflation fix)
mark-missing sets status='off_market' (not the legacy ambiguous 'inactive') and emits a disappeared event. A colliers-main 410 emits a gone payload that the ingest maps to off_market plus a disappeared event with source_value='http_410', but only after the SAME URL returns gone on N consecutive runs (sitemaps lag; one 410 during a Cloudflare wave must not retire a listing). A row that disappears having last carried under_contract or pending is annotated likely-sale in the event payload; a single-run absence with no prior signal is a transient miss and produces nothing until the gated guard clears.

### (5) Re-listing detection (advisory, off critical path)
canonical_key = lower(address) + state + round(lat,4), computed in to_row(). When a new listing's canonical_key matches a recently sold or off_market row of the same brokerage within a 180-day window, emit possible_relist linking the two (no hard merge). Folds in the geo fix: geoless rows (all jll-investor rows lack coordinates, verified) downgrade to address+state-only matching tagged as a weaker advisory, so multi-tenant single-address buildings do not generate confident false links.

### Backfill (zero re-scraping, with a safety gate)
Because raw_data already retains every source status, a one-shot cre_backfill_status.py derives status from existing rows. Folds in the flood fix: it runs dry-run first, prints a per-source histogram of derived statuses and a count of active-to-terminal flips, and requires a manual threshold check before writing. It seeds current state silently (no events), so the first live run emits only true deltas.

### EQUIRE view coordination (folds in the coverage-cliff fix)
The four EQUIRE views and search_cre_listings() gate on status='active' (005). The instant status is populated, every under_contract/pending/sold/off_market row drops out of all four surfaces. This is a prerequisite, not an open question. The views are updated to gate on status IN ('active','under_contract','pending') with status surfaced as a badge column, and a new v_cre_recent_changes view exposes the event ledger for the last 7 days. The coverage change is coordinated with the CRE_EQUIRE codebase before status is populated.

## 7. Schema migrations (DDL sketches)

Migration ordering note (folds in the ordering-bug fix): 000_run_all.sql runs views (005) LAST, in order 001,002,003,004,006,005. Any new view that selects from a new table must be created AFTER that table. So 007 (the new tables) is registered between 004 and 006, and v_cre_recent_changes lives in 005 (which still runs last) so the events table exists before the view.

```sql
-- 007_cre_change_tracking.sql (registered after 004, before 006 in 000_run_all.sql)

-- Append-only change ledger (the history sink the system lacks today).
CREATE TABLE IF NOT EXISTS credeals.cre_listing_events (
  id           uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  listing_id   uuid NOT NULL REFERENCES credeals.cre_listings(id) ON DELETE CASCADE,
  brokerage_id uuid REFERENCES credeals.cre_brokerages(id),
  scrape_job_id uuid REFERENCES credeals.cre_scrape_jobs(id),  -- per-run row, inserted first
  event_type   text NOT NULL CHECK (event_type IN
                 ('new','status_change','price_change','disappeared','reappeared','possible_relist')),
  field        text,
  old_value    text,
  new_value    text,
  source_value text,
  detected_at  timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cre_listing_events_listing_idx   ON credeals.cre_listing_events (listing_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS cre_listing_events_type_idx      ON credeals.cre_listing_events (event_type, detected_at DESC);
CREATE INDEX IF NOT EXISTS cre_listing_events_brokerage_idx ON credeals.cre_listing_events (brokerage_id, detected_at DESC);

-- Persisted enumeration snapshot for the monitor. Keyed EXACTLY like cre_listings.
CREATE TABLE IF NOT EXISTS credeals.cre_source_index (
  id            uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  brokerage_id  uuid NOT NULL REFERENCES credeals.cre_brokerages(id),
  external_id   text NOT NULL,                 -- the SAME prefixed id cre_ingest derives
  source_key    text,                          -- non-key attribute (cbre-dealflow, colliers-main, ...)
  url           text,
  source_lastmod timestamptz,
  fingerprint   text,                          -- price+status hash for Tier-A change diff
  soft_deleted  boolean DEFAULT false,         -- mirrored from cre_listings.deleted_at
  observed_status text,
  first_seen    timestamptz DEFAULT now(),
  last_seen     timestamptz DEFAULT now(),
  last_enumerated_at timestamptz DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS cre_source_index_uq         ON credeals.cre_source_index (brokerage_id, external_id);
CREATE INDEX IF NOT EXISTS cre_source_index_first_seen_idx    ON credeals.cre_source_index (first_seen DESC);

-- Durable work queue for Tier-B (sitemap-detail) enrichment only.
CREATE TABLE IF NOT EXISTS credeals.cre_enrichment_queue (
  id           uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  brokerage_id uuid REFERENCES credeals.cre_brokerages(id),
  source_key   text,
  external_id  text,
  url          text,
  reason       text CHECK (reason IN ('new','changed')),
  priority     int DEFAULT 100,
  enqueued_at  timestamptz DEFAULT now(),
  claimed_at   timestamptz,
  done_at      timestamptz,
  attempts     int DEFAULT 0,
  last_error   text,
  UNIQUE (brokerage_id, external_id, reason)
);
CREATE INDEX IF NOT EXISTS cre_enrichment_queue_drain_idx ON credeals.cre_enrichment_queue (priority, enqueued_at) WHERE done_at IS NULL;

-- Per-source health baseline; updated ONLY after a clean gated run (rolling median, not last).
CREATE TABLE IF NOT EXISTS credeals.cre_source_baseline (
  source_key            text PRIMARY KEY,
  brokerage_slug        text,
  median_active_rows    integer,
  last_active_rows      integer,
  last_accepted_scraped_at timestamptz,
  last_accepted_job_id  uuid REFERENCES credeals.cre_scrape_jobs(id),
  challenge_rate        numeric,
  updated_at            timestamptz DEFAULT now()
);
```

```sql
-- 002_cre_listings.sql (idempotent ALTERs appended)
ALTER TABLE credeals.cre_listings DROP CONSTRAINT IF EXISTS cre_listings_status_check;
ALTER TABLE credeals.cre_listings ADD CONSTRAINT cre_listings_status_check
  CHECK (status IN ('active','inactive','under_contract','pending','sold','leased','off_market','expired','withdrawn'));
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS last_seen_at   timestamptz;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS source_lastmod timestamptz;  -- full-precision, not day-truncated
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS canonical_key  text;
```

```sql
-- 004_cre_indexes.sql (idempotent additions)
CREATE INDEX IF NOT EXISTS cre_listings_canonical_key_idx ON credeals.cre_listings (brokerage_id, canonical_key) WHERE canonical_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS cre_listings_last_seen_idx     ON credeals.cre_listings (last_seen_at DESC);
```

```sql
-- 005_cre_views.sql (runs last; safe to reference 007 tables)
CREATE OR REPLACE VIEW credeals.v_cre_recent_changes AS
SELECT e.*, l.title, l.source_url, b.slug
FROM credeals.cre_listing_events e
JOIN credeals.cre_listings l   ON l.id = e.listing_id
JOIN credeals.cre_brokerages b ON b.id = e.brokerage_id
WHERE e.detected_at > now() - interval '7 days'
ORDER BY e.detected_at DESC;
-- The four existing views change their gate to status IN ('active','under_contract','pending')
-- and add a status badge column; coordinate with CRE_EQUIRE before applying.
```

The ingest staging changes: STAGE_COLS and the _stage TEMP TABLE DDL gain status, source_lastmod, canonical_key; the INSERT and UPDATE column lists carry status (COALESCE on update, not the hardcoded literal), last_seen_at (every upsert), and the conditional deleted_at clear.

## 8. Per-source capability and monitoring matrix

Columns: enumeration class; enumeration key (must equal listing.id minus prefix); public sitemap with lastmod; native source status; status tier; coverage floor (from BROKERAGE_STATUS 2026-06-12 active counts).

| Source | Enumeration | Enum key | Sitemap+lastmod | Native status | Status tier | Floor |
|---|---|---|---|---|---|---|
| cbre | bulk JSON API | Common.PrimaryKey | no | none (aspects only) | disappearance-only | ~17,000 (of 19,028) |
| cbre-dealflow | RCM cards | listingPv | no | card .status + detail | transition | ~1,600 (of 1,836) |
| jll (main) | render_list (Tier-C) | slug then property.id | no | none (tenure) | disappearance-only | ~9,500 (of 10,741) |
| jll-investor | sitemap (US post-filter) | listing.id / slug | sitemap yes, lastmod via detail | isUnderContract/stageName | transition | ~850 (of 934) |
| cushman-wakefield | bulk JSON API | row.id | no | listing_status | transition | ~10,000 (of 11,318) |
| colliers (SalesTracker) | RCM cards+map | ProjectId | no | card .status + SLP | transition | ~1,000 (of 1,172) |
| colliers-main | sitemap | usa####### | YES lastmod (4637) | propertyStatus (markdown) | transition | calibrate after full run (15,896 URLs) |
| newmark | Algolia | h.slug | no | none (facet) | disappearance-only | ~4,000 (of 4,371) |
| marcus-millichap | bulk map feed | DealId (NOT ActivityId) | no | NewlyListed/NewlyReduced (freshness) | disappearance-only | ~2,800 (of 3,124) |
| avison-young | SharpLaunch feed | row.id | no | pre-filtered active | disappearance-only | ~2,000 (of 2,201) |
| savills | render_list | slug / ExternalPropertyID | no | none | disappearance-only | exempt (small: 104) |
| svn | Buildout inventory | x.id (strip -sale/-lease) | no | closed/under_contract | transition | ~4,800 (of 5,287) |
| lee-associates | Buildout inventory | x.id (strip -sale/-lease) | no | closed/under_contract | transition | ~8,400 (of 9,223) |
| nai-global | GraphQL feed | infabode:id | no | listingStatus (filtered active) | disappearance-only | exempt-ish (small: 241) |
| transwestern | bulk ajax feed | PageUrl slug | no | none (bucket) | disappearance-only | ~1,800 (of 2,021) |

Small-source exemption: savills, nai-global, and colliers-main-until-complete are exempt from ratio bands and from auto mark-missing (a 0.5x band on 3 rows is noise). Floors are absolute per source, not one global 100. The band auto-calibrates only after several clean runs feed cre_source_baseline.

## 9. Robustness, ops, and the guards that protect live data

### Three flock-serialized launchd tiers
1. MONITOR every few hours: Tier-B sitemap GET diffs plus Tier-A event recording. Mutates nothing in cre_listings beyond what the daily ingest writes; new-listing latency drops from about 24h to the monitor interval. For Tier-B it enqueues and drains a bounded chunk, then additive ingest.
2. DAILY FULL RECONCILE 06:30 (existing collect plus ingest): now wrapped by cre_gate.py (pre-ingest coverage and anomaly), runs --no-mark-missing by default, and only the gate-blessed sources get reconciliation. Post-ingest cre_validate.py --gate verifies no regression.
3. WEEKLY FULL: the completeness backstop and the only run permitted to set --mark-missing with its full guard, so off_market and disappeared events fire only on a complete, error-free pass.

### The coverage-and-anomaly gate (cre_gate.py)
Before any mark-missing soft-delete: each source must return at least its absolute floor, be within a tolerance band of the rolling-median baseline (start permissive, then auto-calibrate), have an error rate under cap, and a challenge-rate under cap (tracked as a first-class input using colliersMainIsChallenge and the Buildout 3% abort, so a 60%-challenged run quarantines regardless of row count). On failure the source is quarantined: ingested additively, never soft-deleted, and a draft-only alert fires. A CBRE 0-row run or an 80%-vanished run degrades to stale-but-intact.

### Prefix-aware mark-missing (folds in the slug-scope fix)
The current soft-delete is slug-scoped (646-655): marking 'colliers' eligible would delete every colliers-main row whether or not colliers-main ran. The fix rewrites the DELETE to scope by (slug, external_id prefix), reusing the exact prefix logic that validate.py SOURCE_KEY_SQL (20-28) already encodes: only delete `main:%` rows when colliers-main is HEALTHY, independently of SalesTracker; same for cbre/dealflow and jll/investor. This generalizes the existing has_complete_folded_coverage guard rather than regressing it. Plus a per-source completeness floor: a sitemap or cache source must have enriched at least a floor fraction (about 90%) of its sitemap URLs, and a deferred-under-cap run is marked ineligible, closing the transient-miss-as-disappearance hole the bounded-fetch pattern would otherwise widen.

### Observability and politeness
Per-URL cre_scrape_log writers (the table exists at 003 but has no writers today), capturing 410/404 and detailError. A draft-only notify hook (cre_notify.sh) emits source_key plus counts plus log path only, never company names or listing content (enforced as a test, since slug resembles company for some sources); email strictly draft-only per house rules. cre_validate.py gains thresholds, a baseline-delta query, and a --gate mode with non-zero exit, while keeping its report modes. The enrichment worker requires CAP and defaults HEAP_MB=6144, refusing to run unbounded (the 0.8 MB-per-render heap leak lesson).

## 10. Phased implementation roadmap

The sequencing rule: ship the change-detection plumbing and the safety gate FIRST (they move the stated goals and protect live data), defer the acquisition framework refactor LAST, and never run the collector or heavy crawls while the live colliers-main enrichment is active.

Phase 1, schema and safety (no collect.ts edits, safe during the live run):
- Apply 007 (events, source_index, enrichment_queue, baseline) and the 002/004 ALTERs.
- Backfill last_seen_at = scraped_at; seed cre_source_index from cre_listings including soft-deleted rows.
- Build cre_gate.py and flip cre_daily_update.sh to additive-by-default behind the gate, with prefix-aware mark-missing and per-source floors. Add cre_notify.sh.

Phase 2, status normalization and event ledger:
- Add norm_status (returns NULL when no signal), STATUS_RULES, and the status/canonical_key/source_lastmod staging columns.
- Add the _before snapshot CTE and event-emitting CTEs; insert the per-run jobs row first to satisfy scrape_job_id.
- Run cre_backfill_status.py dry-run, review the histogram, then seed silently.
- Coordinate and apply the EQUIRE view gate change (active + under_contract + pending, status badge) plus v_cre_recent_changes.

Phase 3, incremental monitor:
- Export the sitemap and bulk-list enumeration cores; add the enumeration-key invariant unit test (fixes Marcus and Buildout keys before anything ships).
- Build cre_monitor and the Tier-B enrich_queue worker with a separate cache namespace and the mutual flock across all three drivers.
- Wire the launchd monitor plist; start Tier-A event recording from the daily run, Tier-B sitemap diff plus queue.

Phase 4, coverage gaps and JLL-main latency:
- Add the jll (main) search-pages-only enumeration tier to push NEW latency below 24h.
- Investigate marcus public lease and savills US sale.
- Auto-calibrate per-source bands from accumulated cre_source_baseline.

Phase 5 (deferred, lowest ROI), acquisition framework refactor:
- Only after Phases 1-4 prove the consumers, extract the per-source adapters behind a thin interface dictated by what the monitor and change detector actually read, gated per source by a deterministic cache-only golden-output test, with an explicit single-process-instance guard for the shared run-state singletons (brokerRef, colliers-main memo, Buildout cache).

## 11. Top risks and mitigations
See the structured topRisks list. The highest are: enumeration-key vs ingest-id drift (mitigated by the invariant test), the monitor resurrecting daily-killed rows (mitigated by COALESCE-status and conditional deleted_at), and the gate thresholds becoming a new single point of failure (mitigated by permissive start, rolling-median baselines, and quarantine-not-delete on failure).

---

## 12. Adversarial completeness review: required refinements

An independent completeness critic reviewed the synthesized design above and
found gaps that are NOT yet folded into sections 1-11. Treat these as binding
refinements before/while implementing.

### 12.1 Cadence and cost math is the prerequisite (do this first)
The incremental monitor's whole justification is latency-per-cost, yet no
numbers exist. Before writing monitor code, produce a per-source matrix:
enumeration cost (Firecrawl calls + bytes), detail-renders generated per cycle,
and whether the bulk API exposes a newest-first sort or modified-since/date
filter that enables a TRUE cheap incremental tier. This likely collapses the
"15-source monitor" to: 2 sitemap sources (colliers-main, jll-investor) get
genuine sub-daily Tier-B; the rest are the daily run plus event emission, unless
a date-filtered API call exists. State this limitation up front.

### 12.2 Tier-A is not low-latency for ~13 of 15 sources
"Derive NEW/CHANGED from the daily full run" IS the 24h run for the bulk-API
sources (cbre ~19k, cushman, newmark, buildout, rcm, marcus, nai, transwestern).
The headline "low-latency NEW detection across many sites" is true for 2 sitemap
sources and false for the largest (CBRE). Investigate per source whether the API
supports sort-by-newest / date filter for a real incremental tier.

### 12.3 Enumeration-key invariant test must cover EVERY monitored source
Ship it first (pure code, zero risk to the live run). Mandatory cases beyond
Marcus (DealId not ActivityId) and Buildout (strip -sale/-lease): colliers-main
(sitemap entry.id vs ingest main: prefix vs cache id.replace) and jll-investor
(monitor must diff the post-US-filter 934 id-set, NOT the raw 1,857-URL sitemap,
or ~900 non-US URLs flag as perpetual NEW/gone every cycle).

### 12.4 NULL-status coverage cliff (largest source)
CBRE (~19k), and other active-prefiltered sources, have no source status field,
so norm_status() returns NULL. The EQUIRE view gate MUST be
`status IN ('active','under_contract','pending') OR status IS NULL`, else
populating status silently drops ~19k CBRE rows. Confirm with CRE_EQUIRE.

### 12.5 Explicit per-(sourceKey x raw_data-shape) status/price JSON-path map
raw_data is flat for single-mode listings but `{primary, secondary_pass}` for
dual sale+lease rows (cre_ingest.py merge). norm_status() and price extraction
must read from an explicit per-source path table covering BOTH shapes, with a
test against real cached artifacts in out/, or they silently return NULL for
every dual-mode row.

### 12.6 Event ledger must store raw source evidence, not just parsed values
Add raw `salePriceText`/`leaseRateText`, the raw source status value,
`source_url`, and a content hash to cre_listing_events. The ingest overwrites
price unconditionally and parse_money returns NULL for "Negotiable"/"Call for
offers", so parsed-only price_change events are dominated by parser-NULL noise
and cannot be distinguished from real changes. This also satisfies the AGENTS.md
primary-source grounding rule (cre_listings.markdown is currently never written).

### 12.7 Two-sided + quality gate, not just a lower floor
cre_gate.py needs an UPPER anomaly bound (catch silent row-count inflation/junk),
a per-source field-presence canary (assert expected non-null rates for key fields
to tell a structural adapter break from a quiet day), and a signal-staleness
alert (page when the last mark-missing-eligible run for a disappearance-only
source like CBRE is older than its expected cadence). Static floors from today's
counts wrongly quarantine during a real sell-off and miss silent junk growth.

### 12.8 Disappearance latency for disappearance-only sources
With mark-missing gated to a weekly full run and N>=2-pass confirmation, CBRE
sold-detection is up to ~14 days, and indefinite if a weekly run is quarantined.
Add a signal-staleness monitor so "we detect sales" does not silently become
"sometimes, up to two weeks late, sometimes never".

### 12.9 source_lastmod trustworthiness must be verified per source
Sitemap lastmod is often the sitemap GENERATION time (whole-file bump on regen),
timezone-ambiguous, or absent. If colliers-main lastmod bumps globally per regen,
"re-render only advanced-lastmod URLs" degrades to "re-render everything" and the
Tier-B saving evaporates. Verify per-URL meaningfulness before gating on it.

### 12.10 Transaction lock blast radius on the ~33k-row live table
The change-emitting upsert adds a _before snapshot TEMP TABLE + 4-5 event CTEs to
the SAME ~33k-row COPY+upsert transaction (statement_timeout=600s, ON_ERROR_STOP).
Benchmark duration/lock against a table copy first; if it materially extends the
lock, emit events in a SECOND transaction from the _up/_before temp tables rather
than risk a timeout aborting the whole nightly ingest.

### 12.11 Other noted gaps (lower priority, track them)
- Cross-brokerage co-listing/syndication: dedup key is (brokerage_id,
  external_id), so one physical property co-listed on two sources double-counts
  and emits correlated phantom NEW/gone events. Cross-brokerage identity
  resolution is out of scope but materially affects counts and alerts.
- canonical_key relist linkage is non-functional for geoless sources
  (jll-investor has no coords) and noisy elsewhere; keep possible_relist
  advisory and say so.
- Economically important columns are never captured (available_sf,
  lease_rate_type NNN-vs-gross, divisibility, occupancy, etc.); lease
  change-detection is thin until at least available_sf and lease_rate_type land.
- Define the change-ledger CONSUMER contract: how EQUIRE consumes events
  (poll v_cre_recent_changes? queue? status column it reads). A draft-only file
  alert nobody watches is not an operational path.
- Monitor interlock independent of flock: refuse to run if cre_scrape_jobs shows
  a non-terminal run, so run-one does not diff against a half-finished upsert.

### 12.12 Highest-value next steps (ordered)
1. Cadence/cost + true-incremental-capability matrix (12.1/12.2) before any monitor code.
2. Enumeration-key invariant test covering all monitored sources (12.3) - pure code, zero risk now.
3. Resolve the NULL-status gate decision with CRE_EQUIRE (12.4) before touching the 005 views.
4. Build the explicit per-source status/price JSON-path map + test (12.5).
5. Apply 007 + idempotent 002/004 ALTERs + seed cre_source_index (safe during the live colliers run), with a non-terminal-run interlock.
6. Upgrade cre_gate.py to two-sided + quality + signal-staleness (12.7/12.8) before re-enabling mark-missing.
7. Add raw price text + source-evidence snapshot to cre_listing_events (12.6).
8. Benchmark the change-emitting upsert transaction before deploying it (12.10).

---

## 13. Cadence and cost matrix (resolves section 12.1/12.2)

Empirically grounded 2026-06-13 by reading every adapter's actual endpoint and
light live probing. The real axis is not "date filter or not" but "does
monitoring gate the EXPENSIVE per-listing detail work to deltas." Three tiers:

**Tier 1 (sub-daily, ~hourly, near-zero Firecrawl render cost):**
colliers-main (sitemap lastmod, TRUE incremental, replaces the multi-hour
15,896-URL enrich), newmark (Algolia, 0 renders), marcus-millichap (1 map POST),
avison-young (2 GETs), transwestern (5 GETs), cbre (~96 stealth JSON pages that
carry full data, 0 detail renders), cbre-dealflow (~12 plain-HTTP calls),
colliers SalesTracker (~24 plain-HTTP GETs). Most cost ~0 Firecrawl renders
because enumeration returns full data or detail is plain HTTP.

**Tier 2 (a few times/day, render-gated by new-id diff):**
jll (~239 search-page renders, diff slugs, detail-render only the ~10-50 new),
jll-investor (2 sitemap renders, render only newly-appeared URLs), cushman
(~114 JSON id-list pages, detail-render only the tens of new ids). The win is
new-id detection that gates detail work, NOT a cheaper full sweep.

**Tier 3 (daily full sweep only, event emission, NO latency win):**
svn, lee-associates (Buildout throttles sustained paging, so do not poll
aggressively), nai-global, savills. Fold into the existing ~27-min daily run.

**Hard guardrail:** never run colliers-main / cushman / jll / jll-investor /
transwestern / marcus / avison FULL detail enrich on a tight schedule. The
monitor value is exclusively new-id/changed-id detection gating detail to deltas.
A monitor that re-pays a full enrich on a worse-than-daily schedule is worthless.

**Approx hourly Tier-1 load (steady state):** ~100 Firecrawl page-scrapes
(dominated by CBRE's ~96 JSON pages) plus a single-digit-to-low-tens render tail
for deltas. Far below the full daily run. Tier-2 every 6-8h adds the ~239 + ~114
+ 2 render bundles plus delta detail.

### Two one-line code wins surfaced
- jll-investor: capture `<lastmod>` (currently discarded at collect.ts:1604-1609)
  if the US sitemap carries it, to upgrade its change-detection toward Tier B.
- cbre: capture `Common.Created` (currently dropped; adapter keeps only
  LastUpdated.slice(0,10)) to derive a true source listing_date and NEW signal
  instead of a first-seen heuristic.

### Verification: RESOLVED and remaining
- RESOLVED (offline, from the live colliers-main cache, 2026-06-13): colliers-main
  `lastmod` is PER-URL meaningful, 548 distinct dates spanning 2022-2026 with the
  top value only 3.5% of rows. So the colliers-main monitor supports
  CHANGED-detection, not only NEW-detection. The collector currently slices
  lastmod to day granularity; capture full-precision into source_lastmod for
  intra-day gating.
- REMAINING (cheap, one light GET each): cushman RFK sort/recency param;
  nai-global Infabode PostFilter publishedAfter/orderBy; jll search/detail date
  keys (expected none); jll-investor US sitemap per-URL lastmod.

### Per-source matrix
| sourceKey | incremental capability | monitor tier | enumeration cost | renders/cycle | NEW latency | needs-live-verify |
|---|---|---|---|---|---|---|
| colliers-main | sitemap_lastmod | B_sitemap_diff | 2 stealth XML renders (index + props child, ~3-8MB) | only NEW ids + lastmod-advanced ids (single-digit to low-tens/day) vs 15,896 full enrich | ~hours (sitemap regen cadence) | yes: is lastmod per-URL or one global regen stamp |
| newmark | date_filter_or_newest_sort | B_date_filtered_api | ~free: optional 1 cred-bootstrap render (cacheable) + a few dozen direct Algolia JSON calls | 0 (rows built from Algolia hits; contacts are cached direct fetch) | ~hours (poll 1-6h, diff objectID + updateDate) | yes: confirm Algolia key stability and that client-diff (no server range filter) is the path |
| marcus-millichap | date_filter_or_newest_sort | B_date_filtered_api | 1 light POST returns full ~3,124 ActivityId map array | ~2 light HTTP calls per NEW id (mappropertydetail + detail HTML), 0 Firecrawl | ~hours (ActivityId set-diff; NewlyListed corroboration) | yes: confirm NewlyListed expiry behavior; ActivityId->DealId two-step holds |
| avison-young | date_filter_or_newest_sort | B_date_filtered_api | 2 light GETs (website + team_member full arrays, ~few MB) | only NEW ids or updated_at-advanced rows vs ~2,201 full enrich; base data needs 0 renders | ~hours (diff id / updated_at / on_market_at) | yes: confirm no hidden modified-since param; client-diff is the path |
| transwestern | full_sweep_only (cheap structured feed) | B_date_filtered_api (enumeration-as-diff sense) | 5 plain JSON GETs (2 sale + 3 lease buckets), full inventory, no pagination | only NEW slugs vs 2,151 full detail enrich | ~hours if 5 GETs run several times/day | no |
| cbre | full_sweep_only | A_daily_full_then_events | ~96 stealth JSON page-scrapes (~80-140MB), no sort/date param | 0 (enumeration JSON carries full detail) | ~1h achievable via dedicated cheap sweep | no |
| cbre-dealflow | full_sweep_only | A_daily_full_then_events | 1 homepage GET + 1 GetFilters + ~10 GetListingsHtml POSTs (<~5MB, plain HTTP) | only NEW/changed cards (single-digit warm) vs ~1,836 full enrich | ~hours (diff listingPv) | no |
| svn | full_sweep_only | A_daily_full_then_events | ~185 sequential JSON GETs (concurrency 1, few MB) | 0 (inventory carries all fields) | = poll cadence; no native signal; daily realistic (Buildout throttles) | no |
| lee-associates | full_sweep_only | A_daily_full_then_events | ~333 sequential JSON GETs (concurrency 1, ~2x SVN); most throttle-prone | 0 (inventory carries all fields) | = poll cadence; daily only (most throttle-prone Buildout source) | no |
| colliers | full_sweep_only | A_daily_full_then_events | ~24 plain HTTP GETs (list+map pairs) + 1 homepage GET; no Firecrawl billing | only NEW ids need 1 SLP-Init GET; 0 Firecrawl renders | ~hours (cheap plain-HTTP sweep, diff project ids) | no |
| jll | date_filter_or_newest_sort (search-card diff, no recency field) | C_render_pages_first | ~239 SPA search-page renders (upper bound; early-stop shrinks it) | only NEW slugs (~10-50/day) vs 11,230 full detail enrich | ~hours if sweep runs a few times/day | yes: confirm no date key surfaces; 239 is upper bound |
| jll-investor | full_sweep_only | C_render_pages_first | 2 sitemap renders (index + us child, ~1,857 loc URLs) | only newly-appeared loc URLs rendered to apply US filter (single-digit to low-tens/day) vs ~1,857 | ~hours for NEW; each candidate costs 1 render to confirm US | yes: does us/sitemap-us.xml carry per-URL lastmod (would upgrade change-detection toward B) |
| cushman-wakefield | full_sweep_only | A_daily_full_then_events | ~114 Firecrawl /scrape JSON id-list pages (no sort/date param) | only NEW ids (tens/day) vs ~11,318 full detail enrich | ~hours for NEW-id detection (cheap id-list re-enumerate) | yes: does RFK property_search accept a sort= or recency facet |
| savills | full_sweep_only | A_daily_full_then_events | ~3-6 renders (sale /page/N + 1 lease NEXT_DATA); tiny inventory (~101 sale + ~3 lease) | 0 (adapter never detail-renders Savills) | ~hours to 24h; cost unconstrained | no |
| nai-global | full_sweep_only | A_daily_full_then_events | full offset walk in steps of 18 over content types 4+10 across 118 orgs (~tens of GraphQL POSTs) | only NEW ids need 1 publicPost POST; full walk still required to find them | 24h at best; no latency win (publishedAt is a field, not a filter/sort) | yes: does Infabode PostFilter accept publishedAfter/orderBy:publishedAt |

