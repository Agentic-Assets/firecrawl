# CRE collector: unindexed foreign keys on `cre_scrape_jobs`

**Date:** 2026-06-13  
**Project:** `fhqycqubkkrdgzswccwd` (supabase-agentic-assets-v2)  
**Schema:** `credeals`  
**Advisor source:** `/tmp/supabase-advisors-fhqycqubkkrdgzswccwd/performance.json`  
**Scope:** CRE listing collector tables only (not EQUIRE deal/dashboard objects)

---

## Executive summary

| FK child column | Parent | Advisor lint | Priority | Action |
|-----------------|--------|--------------|----------|--------|
| `cre_listing_events.scrape_job_id` | `cre_scrape_jobs(id)` | `unindexed_foreign_keys` INFO | **P1** | Add `cre_listing_events_scrape_job_idx` |
| `cre_source_baseline.last_accepted_job_id` | `cre_scrape_jobs(id)` | `unindexed_foreign_keys` INFO | **P2** | Add `cre_source_baseline_last_accepted_job_idx` |

Both lints are **INFO / PERFORMANCE**, not security. They do not relate to `auth_rls_initplan` (collector `cre_*` tables have RLS enabled with **no policies**; service-role bypasses RLS by design).

Child listing FKs on `cre_listing_contacts`, `cre_listing_documents`, and `cre_listing_images` are **already indexed** in `002_cre_listings.sql`. No action required there.

---

## 1. Advisor lint details (confirmed)

Lint name: `unindexed_foreign_keys` (Supabase database linter `0001_unindexed_foreign_keys`).

### 1.1 `credeals.cre_listing_events`

```json
{
  "name": "unindexed_foreign_keys",
  "level": "INFO",
  "detail": "Table `credeals.cre_listing_events` has a foreign key `cre_listing_events_scrape_job_id_fkey` without a covering index.",
  "metadata": {
    "schema": "credeals",
    "name": "cre_listing_events",
    "fkey_name": "cre_listing_events_scrape_job_id_fkey",
    "fkey_columns": [4]
  },
  "cache_key": "unindexed_foreign_keys_credeals_cre_listing_events_cre_listing_events_scrape_job_id_fkey"
}
```

DDL (`007_cre_change_tracking.sql`):

```sql
scrape_job_id uuid REFERENCES credeals.cre_scrape_jobs(id)
```

Existing indexes on `cre_listing_events` do **not** cover this FK as a leftmost prefix:

- `cre_listing_events_listing_idx` (`listing_id`, `detected_at DESC`)
- `cre_listing_events_type_idx` (`event_type`, `detected_at DESC`)
- `cre_listing_events_brokerage_idx` (`brokerage_id`, `detected_at DESC`)
- `cre_listing_events_idem_uq` (`listing_id`, `event_type`, COALESCE(...), **scrape_job_id last**)

The idempotency unique index uses `scrape_job_id` only as the trailing key for `ON CONFLICT`; Postgres FK enforcement and `DELETE`/`UPDATE` on `cre_scrape_jobs` require a btree index **starting with** `scrape_job_id`.

### 1.2 `credeals.cre_source_baseline`

```json
{
  "name": "unindexed_foreign_keys",
  "level": "INFO",
  "detail": "Table `credeals.cre_source_baseline` has a foreign key `cre_source_baseline_last_accepted_job_id_fkey` without a covering index.",
  "metadata": {
    "schema": "credeals",
    "name": "cre_source_baseline",
    "fkey_name": "cre_source_baseline_last_accepted_job_id_fkey",
    "fkey_columns": [6]
  },
  "cache_key": "unindexed_foreign_keys_credeals_cre_source_baseline_cre_source_baseline_last_accepted_job_id_fkey"
}
```

DDL:

```sql
last_accepted_job_id uuid REFERENCES credeals.cre_scrape_jobs(id)
```

Table cardinality is tiny (~one row per collector `source_key`, on the order of 15 rows). Lint clearance still matters; runtime benefit is marginal except on parent `DELETE`/`UPDATE`.

---

## 2. Collector usage patterns

### 2.1 `cre_monitor.py` — `scrape_job_id` (write-heavy)

Monitor `--apply` generates SQL that:

1. **Inserts** a per-run `cre_scrape_jobs` row first (explicit `id = run_uuid`).
2. **Inserts** `cre_listing_events` rows with `scrape_job_id = run_uuid`.
3. Uses `ON CONFLICT (listing_id, event_type, COALESCE(field,''), COALESCE(new_value,''), scrape_job_id) DO NOTHING` for within-run idempotency.

There are **no** `SELECT ... WHERE scrape_job_id = ?` paths in the collector today. The column is audit/provenance metadata and the conflict target for idempotent re-runs.

### 2.2 `cre_gate.py` — `last_accepted_job_id` (write-only stamp)

`build_baseline_sql()` upserts `cre_source_baseline` with optional `--job-id`:

- `last_accepted_job_id` is set on `ok` / `first_seen` baseline updates.
- `COALESCE(EXCLUDED.last_accepted_job_id, cre_source_baseline.last_accepted_job_id)` preserves a prior job id when the new run omits one.

Gate reads baseline via `SELECT source_key, median_active_rows, last_active_rows` only. It does **not** filter or join on `last_accepted_job_id`.

### 2.3 `cre_ingest.py` — `cre_scrape_jobs` (separate path)

Ingest inserts `cre_scrape_jobs` **without** a predetermined `id` (server-generated UUID). Ingest does **not** populate `cre_listing_events` or `cre_source_baseline`. Monitor and ingest job rows coexist but serve different subsystems.

### 2.4 Reference: already-indexed sibling FK

`cre_scrape_log.job_id → cre_scrape_jobs(id)` is indexed (`cre_scrape_log_job_idx` in `003_cre_scrape_tracking.sql`). Same parent table, same linter rule; this is the naming pattern to follow.

---

## 3. CASCADE / delete impact assessment

| Child table | FK column | ON DELETE | Indexed? | If `cre_scrape_jobs` row deleted |
|-------------|-----------|-----------|----------|----------------------------------|
| `cre_scrape_log` | `job_id` | NO ACTION (default) | Yes (`cre_scrape_log_job_idx`) | FK check uses index |
| `cre_listing_events` | `scrape_job_id` | NO ACTION (default) | **No** | FK check seq-scans `cre_listing_events` |
| `cre_source_baseline` | `last_accepted_job_id` | NO ACTION (default) | **No** | FK check scans baseline (~15 rows) |

**Operational reality:** No collector script issues `DELETE FROM credeals.cre_scrape_jobs`. Rows accumulate from daily ingest (per brokerage) and monitor runs. Parent deletes are therefore **hypothetical** (manual retention cleanup or future pruning).

If retention is added later:

- Default FK behavior **blocks** deleting a job while events or baseline rows still reference it.
- Without child indexes, a attempted delete (or `UPDATE cre_scrape_jobs SET id = ...`) forces sequential scans on `cre_listing_events`, which will grow with monitor cadence.
- `cre_listing_events.listing_id` uses `ON DELETE CASCADE` from `cre_listings`; deleting a **listing** cascades to its events independently of `scrape_job_id`.

There is **no** `ON DELETE CASCADE` from `cre_scrape_jobs` into events or baseline. Adding FK indexes does not change delete semantics; it only speeds FK validation and enables efficient `JOIN ... ON scrape_job_id` for future audit queries.

---

## 4. Child listing FK verification (002)

Confirmed in `002_cre_listings.sql`:

| Table | FK | Child index |
|-------|-----|-------------|
| `cre_listing_contacts` | `listing_id → cre_listings(id) ON DELETE CASCADE` | `cre_listing_contacts_listing_idx` |
| `cre_listing_documents` | `listing_id → cre_listings(id) ON DELETE CASCADE` | `cre_listing_documents_listing_idx` |
| `cre_listing_images` | `listing_id → cre_listings(id) ON DELETE CASCADE` | `cre_listing_images_listing_idx` |

These should **not** appear under `unindexed_foreign_keys` for `listing_id`.

---

## 5. Index design: CONCURRENTLY vs inline

### Naming convention (matches `003` / `007`)

Pattern: `{table}_{column_or_role}_idx` (e.g. `cre_scrape_log_job_idx`, `cre_listing_events_listing_idx`).

Proposed:

- `cre_listing_events_scrape_job_idx` on `(scrape_job_id)`
- `cre_source_baseline_last_accepted_job_idx` on `(last_accepted_job_id)`

Partial indexes are **not** needed; both columns are non-null in normal monitor/gate writes (nullable in DDL but populated when FK is set).

### When to use which DDL

| Context | Method | Rationale |
|---------|--------|-----------|
| `000_run_all.sql` / fresh clone | `CREATE INDEX IF NOT EXISTS` (inline) | Runs inside `BEGIN…COMMIT`; tables may be empty |
| Live Supabase (`fhqycqubkkrdgzswccwd`) | `CREATE INDEX CONCURRENTLY IF NOT EXISTS` **outside** a transaction | Avoids long `ACCESS EXCLUSIVE` locks on `cre_listing_events` as monitor seeds grow |
| Idempotent re-apply | `IF NOT EXISTS` in both forms | Safe to re-run; if a prior `CONCURRENTLY` build failed, drop the **invalid** index before retry (PG 17.6) |

`CREATE INDEX CONCURRENTLY` cannot run inside `000_run_all.sql`'s transaction block. Ship inline indexes in `007`/`008` for greenfield parity; run the CONCURRENTLY script once on production when event volume warrants it.

**Caveats (PG 17.6):** `IF NOT EXISTS` skips creation when the **name** exists even if the index is invalid from a failed concurrent build. `CONCURRENTLY` cannot run inside any explicit or implicit transaction (Supabase SQL editor: run one statement per execution). See commented template in `008_cre_fk_indexes.sql`.

---

## 6. Migration SQL

Canonical file: `../008_cre_fk_indexes.sql`

### 6.1 Greenfield / `000_run_all` companion (inline, transactional)

```sql
-- 008_cre_fk_indexes.sql (excerpt)
CREATE INDEX IF NOT EXISTS cre_listing_events_scrape_job_idx
    ON credeals.cre_listing_events (scrape_job_id);

CREATE INDEX IF NOT EXISTS cre_source_baseline_last_accepted_job_idx
    ON credeals.cre_source_baseline (last_accepted_job_id);
```

### 6.2 Production one-shot (non-blocking)

Run in Supabase SQL editor or `psql` **without** wrapping `BEGIN`:

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS cre_listing_events_scrape_job_idx
    ON credeals.cre_listing_events (scrape_job_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS cre_source_baseline_last_accepted_job_idx
    ON credeals.cre_source_baseline (last_accepted_job_id);
```

Optional greenfield backport: the same two `CREATE INDEX IF NOT EXISTS` lines are at the end of `007_cre_change_tracking.sql` (also in `008`). `000_run_all.sql` runs `007` then `008`; `IF NOT EXISTS` keeps re-runs safe.

### 6.3 Register in `000_run_all.sql` (after 007)

Already wired:

```text
\echo '=== 008_cre_fk_indexes.sql ==='
\i 008_cre_fk_indexes.sql
```

---

## 7. Verification SQL

Run after migration:

```sql
-- Indexes exist
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'credeals'
  AND indexname IN (
    'cre_listing_events_scrape_job_idx',
    'cre_source_baseline_last_accepted_job_idx'
  )
ORDER BY 1;

-- FK columns are indexed (leftmost column check)
SELECT
    c.conname AS fkey_name,
    t.relname AS child_table,
    a.attname AS child_column,
    i.relname AS covering_index
FROM pg_constraint c
JOIN pg_class t ON t.oid = c.conrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
LEFT JOIN LATERAL (
    SELECT ic.relname
    FROM pg_index ix
    JOIN pg_class ic ON ic.oid = ix.indexrelid
    WHERE ix.indrelid = c.conrelid
      AND ix.indkey[0] = a.attnum
    LIMIT 1
) i ON true
WHERE n.nspname = 'credeals'
  AND c.contype = 'f'
  AND c.conname IN (
    'cre_listing_events_scrape_job_id_fkey',
    'cre_source_baseline_last_accepted_job_id_fkey'
  );

-- Child listing FKs still covered (regression guard)
SELECT indexname
FROM pg_indexes
WHERE schemaname = 'credeals'
  AND indexname IN (
    'cre_listing_contacts_listing_idx',
    'cre_listing_documents_listing_idx',
    'cre_listing_images_listing_idx'
  )
ORDER BY 1;
```

Re-export performance advisor JSON and confirm absence of:

- `unindexed_foreign_keys_credeals_cre_listing_events_cre_listing_events_scrape_job_id_fkey`
- `unindexed_foreign_keys_credeals_cre_source_baseline_cre_source_baseline_last_accepted_job_id_fkey`

---

## 8. Expected advisor lint clearance

After both indexes exist, Supabase performance advisor should drop **two** `unindexed_foreign_keys` INFO items for the CRE collector. Remaining `unindexed_foreign_keys` entries in the same JSON export (EQUIRE deal tables, `candidate_facts`, etc.) are out of scope for this migration.

No change expected for:

- `auth_rls_initplan` on collector tables (no policies by design; not an ingestion bug)
- `rls_enabled_no_policy` INFO notices (accepted private-schema posture per `SUPABASE_SECURITY_NOTE_2026-06-12.md`)

---

## 9. Priority rationale

| Item | Priority | Why |
|------|----------|-----|
| `cre_listing_events.scrape_job_id` | **P1** | Monitor `--apply` appends events every run; table grows; FK check cost scales; future job-scoped audit queries are likely |
| `cre_source_baseline.last_accepted_job_id` | **P2** | ~15 rows; gate writes only; low scan cost today; cheap to fix in same migration |

Neither is **P0 (incident)**: no production delete path today, INFO not WARN, and no observed query latency regression on ingest/monitor paths.

**Cross-reference:** `2026-06-13-cre-execution-readiness.md` labels both lints **P0 (DDL gate)** because they are the only actionable `cre_*` performance DDL in the advisor export. That rollup priority and the P1/P2 split here are compatible: ship both indexes before declaring the collector advisor-clean and before scaling monitor `--apply`.

---

## Peer review (2026-06-13)

Peer review against Splinter `0001_unindexed_foreign_keys` ([splinter.sql](https://github.com/supabase/splinter/blob/main/splinter.sql)), Supabase `schema-foreign-key-indexes` best practice, `cre_monitor.py` / `cre_gate.py` usage, and project DDL (`007`, `008`, `003`, `000_run_all.sql`). Supabase hosted remediation URL returned 404 at fetch time; Splinter source is authoritative.

### Verification checklist

| # | Claim | Verdict | Notes |
|---|--------|---------|-------|
| 1 | Only **two** `unindexed_foreign_keys` lints on collector `cre_*` tables | **Confirmed** | Grep of all `REFERENCES credeals.cre_*` in `001`–`007`: every other FK has a leftmost-prefix btree index (`002` child `listing_id` indexes, `003` `cre_scrape_log_job_idx`, `004` `cre_listings_brokerage_idx`, `007` `cre_source_index_uq`, enrichment `UNIQUE (brokerage_id, …)`). No third collector FK lint expected. |
| 2 | Single-column indexes are optimal | **Confirmed** | Both FKs are single-column references to `cre_scrape_jobs(id)`. Splinter requires index prefix `col_attnums[1:n]` matching FK columns exactly; a composite leading with another column does not clear the lint. `(scrape_job_id, detected_at)` would be optional query tuning only; not required for FK enforcement or advisor clearance. |
| 3 | P1/P2 vs execution-readiness P0 | **Reconciled** | Use two axes: **DDL gate = P0** (only `cre_*` performance DDL to apply; see execution-readiness rollup) and **operational urgency = P1/P2** within that gate (`cre_listing_events` scales with monitor; `cre_source_baseline` ~15 rows). Do not read P1 here as permission to defer the baseline index indefinitely; ship both in one migration. |
| 4 | `CREATE INDEX CONCURRENTLY IF NOT EXISTS` on live | **Valid** | Project is Postgres **17.6** (`000_run_all.sql` header). Syntax supported since PG 11. Must run **outside** `BEGIN` (including Supabase SQL editor with implicit txn). Caveats documented in §5 below and in commented block in `008_cre_fk_indexes.sql`. |
| 5 | `cre_listing_events_idem_uq` does not cover `scrape_job_id` as leftmost prefix | **Confirmed** | Unique index column order: `(listing_id, event_type, COALESCE(field,''), COALESCE(new_value,''), scrape_job_id)`. Splinter matches `fk.col_attnums = idx.col_attnums[1:array_length(fk.col_attnums,1)]`; trailing `scrape_job_id` does not satisfy a single-column FK on `scrape_job_id`. |

### Splinter semantics (vs naive FK checks)

Splinter joins FK `conkey` to index `indkey` **prefix**, not merely `attnum = ANY(indkey)`. The verification query in §7 (`indkey[0]`) is correct for these single-column FKs. Supabase skill sample using `any(i.indkey)` is looser and can false-negative on trailing-column matches.

### Collector code cross-check

- `cre_monitor.py`: inserts `cre_scrape_jobs` first, then `cre_listing_events` with `scrape_job_id`; `ON CONFLICT (…, scrape_job_id) DO NOTHING` hits `cre_listing_events_idem_uq`, not a `scrape_job_id`-leading index. No `WHERE scrape_job_id = ?` reads today.
- `cre_gate.py`: `last_accepted_job_id` written on baseline upsert; reads select `source_key, median_active_rows, last_active_rows` only.

### Corrections applied in this review

| Item | Action |
|------|--------|
| Greenfield parity | Added both `CREATE INDEX IF NOT EXISTS` lines to end of `007_cre_change_tracking.sql` with pointer to `008` |
| `000_run_all.sql` | Already includes `\i 008_cre_fk_indexes.sql` after `007` (verified) |
| `008_cre_fk_indexes.sql` | Added commented `CONCURRENTLY` production template and PG 17 / invalid-index caveat |
| Filename | Execution-readiness draft used `008_cre_fk_covering_indexes.sql`; canonical name is **`008_cre_fk_indexes.sql`** |
| Priority wording | Executive summary unchanged (P1/P2); §9 and cross-reference clarify DDL-gate P0 |

### Final priority recommendation

1. **Apply both indexes together** in the next collector DDL window (DDL gate **P0** per execution-readiness).
2. **Live `fhqycqubkkrdgzswccwd`:** if `cre_listing_events` is still small (pre–monitor scale-out), transactional `CREATE INDEX IF NOT EXISTS` from `008` is acceptable. Once monitor `--apply` seeding grows or launchd monitor tier loads, prefer the **`CONCURRENTLY`** block in `008` (one statement at a time, no wrapping transaction).
3. **Order:** `cre_listing_events_scrape_job_idx` first (P1 operational), then `cre_source_baseline_last_accepted_job_idx` (P2; trivial cost).
4. **Re-verify:** run §7 SQL and re-export performance advisor; expect exactly **two** `unindexed_foreign_keys` cache keys removed for `cre_*`.