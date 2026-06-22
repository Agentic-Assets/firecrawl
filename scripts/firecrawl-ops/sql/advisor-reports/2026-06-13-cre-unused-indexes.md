# CRE collector `unused_index` advisor review

**Project:** `fhqycqubkkrdgzswccwd` (supabase-agentic-assets-v2)  
**Snapshot:** `/tmp/supabase-advisors-fhqycqubkkrdgzswccwd/performance.json`  
**Date:** 2026-06-13  
**Scope:** `unused_index` INFO lints on collector-owned `cre_*` tables (plus `public.cre_business_plan_runs`, which predates this schema).

## Executive summary

**Lint counts (parsed from `performance.json`, 2026-06-13):** **16** `unused_index` rows in this report's scope — **14** on `credeals.cre_*` (matches `2026-06-13-cre-execution-readiness.md`) plus **2** on `public.cre_business_plan_runs` (legacy, out of collector migrations). An earlier draft said 17; the advisor export has no seventeenth row.

All 16 flagged indexes are **expected to show zero `idx_scan`** given current traffic: the display app hits geographic/transaction partial paths (those indexes are *not* flagged), EQUIRE agent FTS/cap-rate/jsonb queries are documented but not exercised in prod yet, `cre_scrape_log` has **no writers** (`cre_ingest.py` INSERTs `cre_listings` + children + `cre_scrape_jobs` only; no `cre_scrape_log` INSERT), and the 007 monitor layer has only a gated first `--apply` seed (avison-young) with no launchd tiers or enrichment worker. **Do not bulk-drop from the advisor alone.** Re-check `pg_stat_user_indexes` after monitor launchd goes live, `search_cre_listings()` is called by agents, and Tier-B enrichment drain ships (target **60–90 days**).

### Advisor inventory (`performance.json`)

| Schema | Table | Index | In `cre_*` rollup (14) |
|--------|-------|-------|------------------------:|
| `credeals` | `cre_scrape_jobs` | `cre_scrape_jobs_status_idx` | 1 |
| `credeals` | `cre_scrape_log` | `cre_scrape_log_job_idx`, `cre_scrape_log_status_idx`, `cre_scrape_log_listing_idx` | 3 |
| `credeals` | `cre_listings` | `cre_listings_cap_rate_idx`, `cre_listings_raw_data_gin_idx`, `cre_listings_highlights_gin_idx`, `cre_listings_fts_idx`, `cre_listings_last_seen_idx` | 5 |
| `credeals` | `cre_listing_events` | `cre_listing_events_listing_idx`, `cre_listing_events_type_idx`, `cre_listing_events_brokerage_idx` | 3 |
| `credeals` | `cre_source_index` | `cre_source_index_first_seen_idx` | 1 |
| `credeals` | `cre_enrichment_queue` | `cre_enrichment_queue_drain_idx` | 1 |
| `public` | `cre_business_plan_runs` | `cre_business_plan_runs_created_at_idx`, `cre_business_plan_runs_user_id_idx` | — (legacy +2) |

## Methodology and Supabase guidance

| Source | Guidance applied |
|--------|------------------|
| [Supabase lint `0005_unused_index`](https://supabase.com/docs/guides/database/database-linter?lint=0005_unused_index) (Splinter) | INFO only; `pg_stat_user_indexes.idx_scan = 0`, excluding PK/unique and extension-owned tables. Candidate for review, not auto-removal. |
| `pg_stat_user_indexes` + `pg_stat_statements` | Confirm real query patterns before DROP; advisor does not see rare/admin queries or pre-reset usage. |
| Partial indexes (`004_cre_indexes.sql`, `007`) | `cre_listings_cap_rate_idx` and `cre_listings_sale_price_idx` use `WHERE … IS NOT NULL` (only `cap_rate` is flagged unused; `sale_price_idx` is already scanned by display traffic). `cre_enrichment_queue_drain_idx` is partial `WHERE done_at IS NULL` for the Tier-B drain worker. |
| Monitor subsystem | 007 tables are write-heavy on gated apply; read paths (`v_cre_recent_changes`, enrichment drain) not in daily traffic yet. |

**Verification query (run before any DROP):**

```sql
SELECT schemaname,
       relname AS table_name,
       indexrelname AS index_name,
       idx_scan,
       idx_tup_read,
       idx_tup_fetch,
       pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
FROM pg_catalog.pg_stat_user_indexes
WHERE schemaname IN ('credeals', 'public')
  AND relname IN (
    'cre_listings', 'cre_scrape_jobs', 'cre_scrape_log',
    'cre_listing_events', 'cre_source_index', 'cre_enrichment_queue',
    'cre_business_plan_runs'
  )
ORDER BY relname, indexrelname;
```

Valid on Postgres 17 (`pg_stat_user_indexes` / `indexrelid` unchanged). Reset stats only for a controlled experiment (`pg_stat_reset()`), never in prod without ops approval.

## Traffic context (why indexes look unused)

| Path | What runs today | Index impact |
|------|-----------------|--------------|
| Display app board | Ranks IDs from base tables → hydrates `v_cre_listings_full` (~159 ms board, ~23 ms TX sale filter per security note 2026-06-12) | Uses `state`, `city`, `transaction_type`, `status` paths; **not** cap_rate FTS, GIN, or `last_seen_at`. |
| `cre_ingest.py` | Upsert `cre_listings` + INSERT `cre_scrape_jobs` per brokerage | Uses PK/unique `(brokerage_id, external_id)`; does **not** write `cre_scrape_log`. |
| `cre_monitor.py` | Gated `--apply` only; INSERT events, UPSERT `cre_source_index`, INSERT `cre_enrichment_queue` | Uses `cre_source_index_uq`, `cre_listing_events_idem_uq`; **no SELECT** on event listing/type/brokerage indexes yet. |
| `search_cre_listings()` | Documented agent entry (`005_cre_views.sql`); not observed in prod stats | `cre_listings_fts_idx` should match when non-empty `query` is passed. |
| Enrichment worker | **Not built** (design doc §14) | `cre_enrichment_queue_drain_idx` unused until drain worker ships. |

**Indexes on `cre_listings` that are used (not in advisor unused list):** `city_state_idx`, `state_type_idx`, `txn_status_idx`, `sale_price_idx`, `size_sf_idx`, `brokerage_idx`, `status_idx`, `canonical_key_idx`.

---

## Per-index recommendations

### `credeals.cre_scrape_jobs`

| Index | Migration purpose | Future query path | Category | Recommendation |
|-------|-------------------|-------------------|----------|----------------|
| `cre_scrape_jobs_status_idx` | Filter jobs by `running` / `completed` / `failed` / `partial` (`003`) | Operator dashboards, `cre_validate.py --gate`, failed-run triage | Intentional future use | **Defer 60 days.** Keep. Ingest only INSERTs completed/partial rows; no status-filtered SELECT yet. `cre_scrape_jobs_brokerage_idx` and `cre_scrape_jobs_started_idx` are already used. |

**DROP SQL:** none.

---

### `credeals.cre_scrape_log`

| Index | Migration purpose | Future query path | Category | Recommendation |
|-------|-------------------|-------------------|----------|----------------|
| `cre_scrape_log_job_idx` | Per-job URL audit (`job_id` FK) | ListingHunterAgent progress, per-URL failure reports (design §9) | Monitor-not-live / intentional future | **Defer 90 days.** Table exists (`003`) but **no production writer** (`cre_ingest.py` / `cre_monitor.py` do not INSERT). Indexes add write cost once logging ships. |
| `cre_scrape_log_status_idx` | Filter `success` / `error` / `skipped` / `duplicate` | Error-rate dashboards, retry queues | Same | **Defer 90 days.** Keep until per-URL writers land or design explicitly drops `cre_scrape_log`. |
| `cre_scrape_log_listing_idx` | Join log rows to `cre_listings` | Listing-level scrape history | Same | **Defer 90 days.** Keep. |

**DROP SQL:** none. If per-URL logging is cancelled, revisit all three together after 90 days with `idx_scan` still zero **and** confirmed empty table.

---

### `credeals.cre_listings`

| Index | Migration purpose | Future query path | Category | Recommendation |
|-------|-------------------|-------------------|----------|----------------|
| `cre_listings_cap_rate_idx` | Partial index on `cap_rate WHERE cap_rate IS NOT NULL` for mandate-fit (`004`) | `v_cre_active_for_sale` + `cap_rate >= 0.065 ORDER BY cap_rate DESC` (consumer API §2) | Intentional future use | **Defer 60 days.** Keep. Display app and agents have not run cap-rate-sorted screening in prod; index is small (partial). |
| `cre_listings_raw_data_gin_idx` | `jsonb_ops` containment on `raw_data` (`004`) | Agent ad-hoc `raw_data @>`, `raw_data ?` key probes; status backfill analytics | Intentional future use | **Defer 90 days.** Keep. Large GIN; high value once agents query nested source payloads. |
| `cre_listings_highlights_gin_idx` | `array_ops` on `highlights text[]` (`004`) | Array membership (`highlights @> '{NNN}'`) in agent search | Intentional future use | **Defer 90 days.** Keep. |
| `cre_listings_fts_idx` | GIN on `to_tsvector(...)` over title/address/city/description (`004`) | `search_cre_listings()` when `query` is non-empty (`005`, consumer API §2) | Intentional future use | **Defer 60 days.** Keep. Expression matches the function body; unused because FTS path not called in prod yet (filter-only browse uses geo indexes). |
| `cre_listings_last_seen_idx` | `last_seen_at DESC` change-tracking (`004`, design §7) | Staleness / disappearance analytics on `cre_listings` | **Column unused today**; partially redundant with `cre_source_index.last_seen` | **Defer 90 days.** Keep for now. `cre_monitor.py` explicitly does **not** UPDATE `cre_listings.last_seen_at` (comments at ingest SQL build ~L650–652; tests in `tests/test_monitor.py`). Enumeration freshness is `cre_source_index.last_seen` / `last_enumerated_at`. Re-evaluate after monitor scale-out: if no product path ever filters `cre_listings.last_seen_at`, this index may become droppable. |

**DROP SQL:** none.

---

### `credeals.cre_listing_events` (007)

| Index | Migration purpose | Future query path | Category | Recommendation |
|-------|-------------------|-------------------|----------|----------------|
| `cre_listing_events_listing_idx` | `(listing_id, detected_at DESC)` | Per-listing change history | Monitor-not-live | **Defer 60 days.** Keep. Monitor INSERT-only so far; reads will follow `v_cre_recent_changes` and listing drill-down. |
| `cre_listing_events_type_idx` | `(event_type, detected_at DESC)` | Filter `price_change` / `disappeared` feeds | Monitor-not-live | **Defer 60 days.** Keep. `v_cre_recent_changes` filters only `detected_at > now() - 7 days` (no leading `detected_at` index today); this index helps **typed** feeds, not the current view scan. Consider adding `(detected_at DESC)` if 7-day view becomes hot. |
| `cre_listing_events_brokerage_idx` | `(brokerage_id, detected_at DESC)` | Per-broker change dashboards | Monitor-not-live | **Defer 60 days.** Keep. |

**Note:** `cre_listing_events_idem_uq` is **used** (ON CONFLICT on monitor INSERT) and is not flagged.

**DROP SQL:** none.

---

### `credeals.cre_source_index` (007)

| Index | Migration purpose | Future query path | Category | Recommendation |
|-------|-------------------|-------------------|----------|----------------|
| `cre_source_index_first_seen_idx` | `first_seen DESC` | "New listings since …" analytics, cohort reports | Monitor-not-live / intentional future | **Defer 60 days.** Keep. Monitor UPSERT uses `cre_source_index_uq` (used); no `first_seen` range queries yet. |

**Note:** `cre_source_index_uq` and `cre_source_index_source_key_idx` are not flagged unused.

**DROP SQL:** none.

---

### `credeals.cre_enrichment_queue` (007)

| Index | Migration purpose | Future query path | Category | Recommendation |
|-------|-------------------|-------------------|----------|----------------|
| `cre_enrichment_queue_drain_idx` | Partial `(priority, enqueued_at) WHERE done_at IS NULL` | Tier-B enrichment worker drain (design §7, §14) | Monitor-not-live | **Defer 90 days.** Keep until worker ships. Monitor may INSERT rows; no drain SELECT yet. |

**DROP SQL:** none.

---

### `public.cre_business_plan_runs` (predates collector schema)

| Index | Migration purpose | Future query path | Category | Recommendation |
|-------|-------------------|-------------------|----------|----------------|
| `cre_business_plan_runs_created_at_idx` | Not in `003`/`004`/`007`; legacy EQUIRE table | App queries by recency | Out of collector scope | **Defer 90 days.** Coordinate with CRE_EQUIRE / display app owners before any DROP. Not created by collector migrations. |
| `cre_business_plan_runs_user_id_idx` | Legacy user-scoped listing | `WHERE user_id = …` | Out of collector scope | **Defer 90 days.** Same. |

**DROP SQL:** none (collector agents must not drop without EQUIRE sign-off).

---

## Category rollup

| Category | Count | Action |
|----------|------:|--------|
| Intentional future use (EQUIRE agents: cap rate, FTS, GIN on `raw_data` / `highlights`) | 4 | Keep; re-check after agent traffic |
| Monitor / operator deferred (`cre_scrape_jobs` status, `cre_scrape_log`×3, 007 event/source/enrichment indexes) | 9 | Keep; re-check after launchd monitor + scrape log writers + enrichment worker |
| Column unused today (`cre_listings_last_seen_idx`; monitor uses `cre_source_index` only) | 1 | Keep 90d; revisit if `last_seen_at` is never populated |
| Truly redundant / safe DROP | **0** | No DROP recommended today |
| Out of collector scope (`public.cre_business_plan_runs`) | 2 | Defer; EQUIRE owns |

**Sum:** 4 + 9 + 1 + 2 = **16** (`credeals.cre_*` subset = **14**, per execution-readiness).

## Re-check triggers (60–90 day window)

1. **Monitor launchd** (`ai.agentic.cre-monitor`) loaded and `cre_monitor.py --apply` running on schedule.
2. **`search_cre_listings()`** invoked from EQUIRE agents (confirm `cre_listings_fts_idx` `idx_scan > 0`).
3. **Mandate-fit agent queries** with `cap_rate` filters (confirm `cre_listings_cap_rate_idx`).
4. **Tier-B enrichment worker** deployed (confirm `cre_enrichment_queue_drain_idx`).
5. **Per-URL `cre_scrape_log` writers** implemented or explicitly removed from design.
6. **`pg_stat_user_indexes`** export saved beside this report for before/after comparison.

## Safe DROP statements

**None at this time.** Every flagged index maps to a documented future query path or a subsystem not yet in production traffic. Bulk-dropping from Supabase Performance Advisor would remove capacity for `search_cre_listings()`, mandate-fit screening, monitor feeds, and enrichment drain without measurable write savings today (most tables are INSERT-heavy with modest index size).

If after 90 days an index still shows `idx_scan = 0` **and** its future path is cancelled in writing, use idempotent drops, for example:

```sql
-- EXAMPLE ONLY — do not run until re-check triggers pass and path is cancelled
-- DROP INDEX IF EXISTS credeals.cre_listings_last_seen_idx;
```

## References

- `scripts/firecrawl-ops/sql/003_cre_scrape_tracking.sql`
- `scripts/firecrawl-ops/sql/004_cre_indexes.sql`
- `scripts/firecrawl-ops/sql/007_cre_change_tracking.sql`
- `scripts/firecrawl-ops/sql/005_cre_views.sql` (`search_cre_listings`, `v_cre_recent_changes`)
- `docs/firecrawl-ops/references/cre-equire-consumer-api.md`
- `docs/firecrawl-ops/references/cre-intelligence-system-design.md` (§7, §9, §14)
- `scripts/firecrawl-ops/cre_collector/archive/SUPABASE_SECURITY_NOTE_2026-06-12.md`
- `scripts/firecrawl-ops/cre_collector/HANDOFF_MONITOR_FIRST_APPLY_2026-06-13.md`
- `scripts/firecrawl-ops/sql/advisor-reports/2026-06-13-cre-execution-readiness.md` (14× `cre_*` lint rollup)

---

## Peer review (2026-06-13)

**Reviewer:** Postgres performance / Supabase advisor peer pass (in-repo).

### Count reconciliation

| Source | `credeals.cre_*` | `public.cre_business_plan_runs` | Total |
|--------|-----------------:|--------------------------------:|------:|
| `performance.json` (parsed 2026-06-13) | 14 | 2 | **16** |
| `2026-06-13-cre-execution-readiness.md` | 14 | (out of scope) | 14 |
| This report (pre-fix) | — | — | 17 (incorrect) |

**Fix:** Executive summary and category rollup now use **16** total / **14** collector scope. No missing advisor row exists in the export.

### Lint `0005_unused_index` alignment

Splinter flags non-PK, non-unique indexes with `idx_scan = 0` in `pg_stat_user_indexes`. That matches all 16 rows. INFO level means review, not DROP. Partial indexes (`cap_rate`, enrichment drain) are still flagged when unused; that is expected.

### Keep/defer vs code paths

| Index group | Evidence | Verdict |
|-------------|----------|---------|
| `cre_listings_fts_idx` | Expression in `004_cre_indexes.sql` matches `search_cre_listings()` `to_tsvector(...)` in `005_cre_views.sql` L229–243; consumer API §2 shows FTS call pattern | **Keep** (not called in prod yet) |
| `cre_listings_cap_rate_idx` | Partial `WHERE cap_rate IS NOT NULL`; consumer API §2 `cap_rate >= 0.065 ORDER BY cap_rate DESC` | **Keep** |
| `cre_listings_raw_data_gin_idx` / `highlights_gin_idx` | `cre_ingest.py` writes `raw_data` / `highlights` on upsert; no agent jsonb `@>` or array `@>` queries in prod yet | **Keep** |
| `cre_scrape_jobs_status_idx` | `cre_ingest.py` INSERTs completed/partial jobs only (L1064–1071); no `WHERE status = …` SELECT | **Defer** |
| `cre_scrape_log_*` | No `INSERT INTO credeals.cre_scrape_log` in `cre_ingest.py` or `cre_monitor.py` | **Defer** |
| `cre_listing_events_*` | `cre_monitor.py` INSERT events + `v_cre_recent_changes` 7-day scan (`005` L308); indexes unused on INSERT-only traffic | **Defer** |
| `cre_source_index_first_seen_idx` | Monitor UPSERT uses `cre_source_index_uq` (not flagged); no `ORDER BY first_seen` reads yet | **Defer** |
| `cre_enrichment_queue_drain_idx` | Partial drain index in `007` L109; monitor INSERTs queue rows, no drain SELECT | **Defer** |
| `cre_listings_last_seen_idx` | `cre_monitor.py` L650–652: does **not** set `cre_listings.last_seen_at` | **Defer 90d**; only index with a plausible future DROP if column stays null |
| Display app paths | `SUPABASE_SECURITY_NOTE_2026-06-12.md`: board uses `state` / `city` / `transaction_type` / `status`; explains why geo indexes scan and FTS/GIN do not | Consistent |
| `cre_business_plan_runs_*` | Not in `003`/`004`/`007`; predates collector | **Out of scope**; EQUIRE owns |

**No index wrongly recommended for DROP.**

### Verification SQL (Postgres 17)

`pg_catalog.pg_stat_user_indexes` with `pg_relation_size(indexrelid)` is valid. Renamed alias `table` → `table_name` to avoid reserved-word friction in some clients.

### Partial index guidance

Accurate after fix: only `cre_listings_cap_rate_idx` among partial listing indexes is unused; `cre_listings_sale_price_idx` (same migration, `WHERE sale_price_usd IS NOT NULL`) is **not** in the advisor list because display/ingest traffic already scans it. Enrichment drain partial index correctly documented in `007`.

### Defer recommendation

**Unchanged:** defer all **16** indexes; **no DROP** today. Re-check window **60–90 days** after monitor launchd, `search_cre_listings()` agent traffic, enrichment worker, and optional `cre_scrape_log` writers.
