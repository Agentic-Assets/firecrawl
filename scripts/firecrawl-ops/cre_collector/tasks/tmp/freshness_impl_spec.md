# CRE Collector Freshness / Accuracy / History Remediation: Authoritative Implementation Spec

**Date:** 2026-06-15
**Branch:** `feat/cre-brokerage-collectors-2026-06-12`
**Source review:** `cre_collector/FRESHNESS_HISTORY_REVIEW_2026-06-15.md`
**Consumed by:** 5 parallel implementer agents with DISJOINT file ownership.

This spec PINS every cross-file contract. Implementers MUST use the exact
table names, column names, types, event-type strings, trigger names, and GUC
names defined here. Do not invent alternatives. Where this spec gives DDL or a
code shape, treat it as the contract: another owner depends on the identical
string.

---

## 0. Hard constraints (ALL owners, non-negotiable)

- Make minimal, durable changes. Preserve unrelated behavior. No drive-by edits.
- Never use em dashes in code comments, SQL comments, or docs. Use commas,
  parentheses, colons, periods, or "and"/"or".
- Never use the words "genuinely", "honestly", or "straightforward" anywhere.
- Do NOT run the live pipeline. No `cre_ingest.py` against a DB, no `collect.ts`
  against the API, no docker, no `launchctl load`/`kickstart`. The Docker stack
  is DOWN and DB apply is gated and OUT OF SCOPE.
- All new tests MUST be OFFLINE: assert on generated SQL (`build_sql` /
  `build_write_sql` output), on pure Python functions, or on pure TS logic.
  Never connect to a database or the network in a test. Mirror existing tests in
  `cre_collector/tests/` (`test_ingest_mark_missing.py`, `test_monitor_events.py`,
  `test_ingest_status_activation.py`) and `tests/ts/sources/savills.test.ts`.
- Do NOT edit a file owned by another owner. Do NOT edit `tests/conftest.py` or
  any existing test file. ADD new test files with the EXACT names given.
- SQL must be idempotent: `CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT
  EXISTS`, `CREATE OR REPLACE FUNCTION`, `DROP TRIGGER IF EXISTS` then `CREATE
  TRIGGER`, `CREATE INDEX IF NOT EXISTS`.
- The ingestor MUST NOT break if the new tables/columns are not yet applied to
  prod (they are applied later, gated). Use the existence-guard mechanism in
  section 2 (H4a) exactly.

### Live data reality (verified against prod credeals 2026-06-15; size designs to this)

- `cre_listings` ~92,699 rows (active + inactive).
- `cre_listing_contacts` ~160,328; `cre_listing_documents` ~70,414;
  `cre_listing_images` ~488,685.
- `cre_source_index` and `cre_listing_events` are currently 0 rows (the monitor
  has never run successfully). H4b's new columns land on an EMPTY table: no
  backfill concern.
- A full daily run re-upserts most listings and the child-refresh DELETE +
  re-INSERTs children for EVERY re-scraped row. Any "archive on every child
  replace" design bloats by hundreds of thousands of rows per run and is
  FORBIDDEN. M2 is therefore a BOUNDED slice: archive only on retirement.

---

## 1. Ownership map (DISJOINT; state-of-record)

| Owner | Files (ONLY these) | Items |
|-------|--------------------|-------|
| **A** | `cre_collector/cre_ingest.py` and NEW test files: `tests/test_folded_coverage_count_aware.py`, `tests/test_price_coalesce.py`, `tests/test_revival_terminal_stickiness.py`, `tests/test_disappeared_event_on_mark_missing.py`, `tests/test_price_history_snapshot.py`, `tests/test_child_history_archive_on_retirement.py` | M1, L1, M5, M3, H4a-write, M2-archive-write, L4a |
| **B** | `sql/` (NEW `009_cre_history_retention.sql`) + `sql/000_run_all.sql` | H4a-table, H4b-columns, M2-archive-tables (contacts + documents only), L2-trigger+index |
| **C** | `cre_collector/cre_monitor.py` + NEW `tests/test_monitor_old_value.py` | H4b-populate (write prior price into `cre_source_index`; populate `old_value`) |
| **D** | `cre_collector/sources/savills.ts` + NEW `tests/ts/sources/savills-commercial.test.ts` | L5, L3 |
| **E** | `cre_collector/cre_status.sh` + `launchd/ai.agentic.cre-daily.plist.template` + `launchd/ai.agentic.cre-weekly.plist.template` | H3, L4b |

No two owners touch the same file. Owner A writes Python INSERTs that reference
the table/column names Owner B creates; Owner C writes to columns Owner B
creates. The names below are the single source of truth so they cannot drift.

---

## 2. PINNED CONTRACTS (the names every owner shares)

### 2.1 History table (H4a) - Owner B creates, Owner A writes

- **Schema-qualified name:** `credeals.cre_listing_price_history`
- **Exact column list (in this order), as Owner A's INSERT column list:**
  `listing_id, observed_at, sale_price_usd, sale_price_per_sf, lease_rate_min, lease_rate_max, status, cap_rate, source_lastmod, transaction_type`
- The ingest writes ONE row to this table per listing whenever a WATCHED field
  changed vs the existing DB row (the diff predicate is in section 2.6).

### 2.2 source_index new columns (H4b) - Owner B adds, Owner C writes

- Table: `credeals.cre_source_index`
- New columns (exact names and types):
  - `prior_sale_price numeric`
  - `prior_lease_rate numeric`
  - `prior_status text`
- Semantics: these hold the value observed on the PREVIOUS enumeration so the
  monitor can populate a real `old_value` on a `price_change` event this run.

### 2.3 Child-history archive tables (M2) - Owner B creates, Owner A writes

CONTACTS and DOCUMENTS only. NO images table. NO raw_data table.

- `credeals.cre_listing_contacts_archive`
- `credeals.cre_listing_documents_archive`

Exact column lists in section 2.5. Owner A INSERTs into them inside the
mark-missing block (section on M3/M2 below).

### 2.4 Event type string (M3) - Owner A emits, must match 007 CHECK

- The mark-missing disappeared event uses event_type = **`disappeared`**
  (already in the `cre_listing_events` CHECK in 007: `'new', 'status_change',
  'price_change', 'disappeared', 'reappeared', 'possible_relist'`). Do NOT add a
  new event_type and do NOT alter the 007 CHECK.
- The existing monitor price-change event type is **`price_change`** (unchanged;
  Owner C only fills its `old_value`).

### 2.5 Retention trigger + GUC (L2) - Owner B creates

- Trigger name: `trg_cre_listings_block_history_delete`
- Trigger function name: `credeals.cre_block_history_delete()`
- Bypass GUC name: `cre.allow_history_delete` (bypass when
  `current_setting('cre.allow_history_delete', true) = 'on'`)
- Partial index name: `cre_listings_deleted_at_idx` on
  `credeals.cre_listings (deleted_at) WHERE deleted_at IS NOT NULL`

### 2.6 The history-write diff predicate (H4a) - the exact contract

A history row is written for a listing when, comparing the staged row (`_src`,
aliased `s`) against the existing DB row (`cre_listings`, aliased `t`) BEFORE the
upsert mutates it, ANY of these WATCHED fields differs (SQL `IS DISTINCT FROM`):

- `sale_price_usd`
- `sale_price_per_sf`
- `lease_rate_min`
- `lease_rate_max`
- `status`
- `cap_rate`

Because L1 changes price columns to COALESCE-keep and `cap_rate` already
COALESCE-keeps, the comparison MUST use the value that will actually be written
(COALESCE(EXCLUDED.x, t.x)) so a transient NULL is NOT recorded as a change. See
H4a detail in section 4 for the exact CTE.

---

## 3. OWNER B: `sql/009_cre_history_retention.sql` + `000_run_all.sql`

Create a single new migration `sql/009_cre_history_retention.sql`. It owns
H4a-table, H4b-columns, M2-archive-tables, and L2 trigger + index. Register it
in `000_run_all.sql`.

### 3.1 File header (top of 009)

Mirror the existing migration headers (002, 007). State: ADDITIVE ONLY,
idempotent, requires 001/002/003, registered in 000 AFTER 008 and BEFORE 006.
No em dashes. Do not restate live counts.

### 3.2 H4a table: `cre_listing_price_history`

```sql
-- ---------------------------------------------------------------------------
-- cre_listing_price_history -- append-only value-over-time history. One row per
-- listing per ingest run in which a WATCHED field (price, status, cap_rate)
-- changed vs the prior DB row. Written by cre_ingest.py (existence-guarded so a
-- pre-apply ingestor is a no-op). Never updated in place; the row IS the
-- snapshot of the watched values at observed_at.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credeals.cre_listing_price_history (
    id                uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    listing_id        uuid        NOT NULL REFERENCES credeals.cre_listings(id) ON DELETE CASCADE,
    observed_at       timestamptz NOT NULL DEFAULT now(),
    sale_price_usd    numeric,
    sale_price_per_sf numeric,
    lease_rate_min    numeric,
    lease_rate_max    numeric,
    status            text,
    cap_rate          numeric,
    source_lastmod    timestamptz,
    transaction_type  text
);

CREATE INDEX IF NOT EXISTS cre_listing_price_history_listing_idx
    ON credeals.cre_listing_price_history (listing_id, observed_at DESC);

ALTER TABLE credeals.cre_listing_price_history ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE credeals.cre_listing_price_history IS
    'Append-only value-over-time history for cre_listings. One row per ingest run where a watched field (sale_price_usd, sale_price_per_sf, lease_rate_min/max, status, cap_rate) changed vs the prior DB row. Written by cre_ingest.py; never updated in place.';
```

Notes for Owner B:
- `ON DELETE CASCADE` on `listing_id` is consistent with the other child FKs.
  History rows die with their listing only on a real hard delete, which L2 now
  blocks for soft-deleted rows.
- RLS enabled, no public policy (mirror all other `cre_*` tables).
- The column ORDER above is the contract Owner A relies on for its INSERT column
  list. Keep it.

### 3.3 H4b columns on `cre_source_index`

```sql
ALTER TABLE credeals.cre_source_index ADD COLUMN IF NOT EXISTS prior_sale_price numeric;
ALTER TABLE credeals.cre_source_index ADD COLUMN IF NOT EXISTS prior_lease_rate numeric;
ALTER TABLE credeals.cre_source_index ADD COLUMN IF NOT EXISTS prior_status     text;

COMMENT ON COLUMN credeals.cre_source_index.prior_sale_price IS
    'Sale price observed on the PREVIOUS enumeration. Lets cre_monitor.py populate a real old_value on a price_change event instead of NULL.';
COMMENT ON COLUMN credeals.cre_source_index.prior_lease_rate IS
    'Lease rate (min) observed on the PREVIOUS enumeration. Companion to prior_sale_price for lease-priced rows.';
COMMENT ON COLUMN credeals.cre_source_index.prior_status IS
    'observed_status from the PREVIOUS enumeration. Reserved for richer status_change evidence.';
```

### 3.4 M2 archive tables (contacts + documents ONLY)

Column shapes MIRROR the live child tables (002) PLUS two provenance columns
(`archived_at`, `source_listing_id`). Do NOT use FKs to `cre_listings` on the
archive tables: a retired listing may later be hard-deleted under the L2 bypass,
and the archive must survive that. Owner A INSERTs into these by exact column
list.

```sql
-- ---------------------------------------------------------------------------
-- cre_listing_contacts_archive / cre_listing_documents_archive -- bounded,
-- durable history slice. The mark-missing reconciliation snapshots a retired
-- listing's FINAL contacts and documents here in the SAME transaction as the
-- soft-delete, so "who brokered this now-sold deal" and its final brochures
-- survive the next re-scrape's wholesale child replace. Images are excluded
-- (high volume, low historical value). No FK to cre_listings: the archive must
-- outlive a future hard delete of the source row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credeals.cre_listing_contacts_archive (
    id                uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    source_listing_id uuid        NOT NULL,
    archived_at       timestamptz NOT NULL DEFAULT now(),
    name              text,
    title             text,
    email             text,
    phone             text,
    brokerage_name    text,
    profile_url       text,
    avatar_url        text,
    vcard_url         text,
    is_primary        boolean
);

CREATE INDEX IF NOT EXISTS cre_listing_contacts_archive_listing_idx
    ON credeals.cre_listing_contacts_archive (source_listing_id, archived_at DESC);

ALTER TABLE credeals.cre_listing_contacts_archive ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE credeals.cre_listing_contacts_archive IS
    'Append-only snapshot of a listing''s final contacts, captured by cre_ingest.py mark-missing at retirement. No FK: survives a later hard delete of the source listing.';

CREATE TABLE IF NOT EXISTS credeals.cre_listing_documents_archive (
    id                uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    source_listing_id uuid        NOT NULL,
    archived_at       timestamptz NOT NULL DEFAULT now(),
    doc_type          text,
    title             text,
    url               text
);

CREATE INDEX IF NOT EXISTS cre_listing_documents_archive_listing_idx
    ON credeals.cre_listing_documents_archive (source_listing_id, archived_at DESC);

ALTER TABLE credeals.cre_listing_documents_archive ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE credeals.cre_listing_documents_archive IS
    'Append-only snapshot of a listing''s final documents/brochures, captured by cre_ingest.py mark-missing at retirement. No FK: survives a later hard delete of the source listing.';
```

Owner A INSERT column lists (the contract Owner A MUST use verbatim):
- contacts archive:
  `(source_listing_id, name, title, email, phone, brokerage_name, profile_url, avatar_url, vcard_url, is_primary)`
- documents archive:
  `(source_listing_id, doc_type, title, url)`
(`archived_at` defaults to `now()`; `id` defaults to `gen_random_uuid()`; both
are omitted from the INSERT column list.)

### 3.5 L2 retention trigger + GUC + partial index

```sql
-- ---------------------------------------------------------------------------
-- L2 retention guard: a soft-deleted (deleted_at IS NOT NULL) cre_listings row
-- is history. Block any hard DELETE of it unless the session explicitly opts in
-- via SET LOCAL cre.allow_history_delete = 'on'. Active (deleted_at IS NULL)
-- rows are unaffected: no production path deletes them today, and the upsert
-- never DELETEs the parent. Child FKs are ON DELETE CASCADE, so protecting the
-- parent protects the children and the price-history rows.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION credeals.cre_block_history_delete()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $$
BEGIN
    IF OLD.deleted_at IS NOT NULL
       AND COALESCE(current_setting('cre.allow_history_delete', true), '') <> 'on' THEN
        RAISE EXCEPTION
            'cre_listings: refusing to hard-delete soft-deleted history row % (deleted_at=%). Set cre.allow_history_delete = ''on'' to override.',
            OLD.id, OLD.deleted_at;
    END IF;
    RETURN OLD;
END;
$$;

COMMENT ON FUNCTION credeals.cre_block_history_delete() IS
    'BEFORE DELETE guard on cre_listings: raises on a soft-deleted (deleted_at IS NOT NULL) row unless cre.allow_history_delete = ''on''. Protects retained history from a future hard-delete migration.';

DROP TRIGGER IF EXISTS trg_cre_listings_block_history_delete ON credeals.cre_listings;
CREATE TRIGGER trg_cre_listings_block_history_delete
    BEFORE DELETE ON credeals.cre_listings
    FOR EACH ROW
    EXECUTE FUNCTION credeals.cre_block_history_delete();

CREATE INDEX IF NOT EXISTS cre_listings_deleted_at_idx
    ON credeals.cre_listings (deleted_at) WHERE deleted_at IS NOT NULL;

REVOKE EXECUTE ON FUNCTION credeals.cre_block_history_delete() FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE EXECUTE ON FUNCTION credeals.cre_block_history_delete() FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE EXECUTE ON FUNCTION credeals.cre_block_history_delete() FROM authenticated;
    END IF;
END
$$;
```

Owner B note: the trigger function is a trigger function (fires in the table
owner's context); the REVOKEs are defense in depth, matching the pattern already
used for `update_cre_listing_timestamp()` in 005.

### 3.6 `000_run_all.sql` registration

Insert a `009` step AFTER `008` and BEFORE `006`, mirroring the existing
dependency order block and the `\i` lines. Concretely:

- In the DEPENDENCY ORDER comment block, add a line for 009 after the 008 line:
  `--   009 cre_history_retention     price-history + child archive tables + retention trigger`
- In Option B (individual files) comment block, add a `psql ... -f
  009_cre_history_retention.sql` line after the 008 line and before the 006 line.
- In the executable body, add after the `\i 008_cre_fk_indexes.sql` block and
  before the `\i 006_cre_contact_urls.sql` block:
  ```
  \echo '=== 009_cre_history_retention.sql ==='
  \i 009_cre_history_retention.sql
  ```
- 009 only references `credeals.cre_listings`, `credeals.cre_source_index`
  (created in 007), and `gen_random_uuid()`, all present by the time 008 has run.
  Do NOT place 009 before 007 (it ALTERs `cre_source_index`).

### 3.7 Owner B verification (offline)

- `psql` is NOT available to apply. Confirm idempotent syntax by inspection and
  by the Python shell-syntax test already in the repo for `.sh`; there is no SQL
  linter requirement here. Confirm every statement uses IF NOT EXISTS /
  CREATE OR REPLACE / DROP-then-CREATE TRIGGER. Confirm no em dashes. Confirm the
  column orders match section 2.

---

## 4. OWNER A: `cre_ingest.py` + 6 new test files

Owner A makes all behavioral changes inside `build_sql()` and `main()` (and the
folded-coverage helper). The generated SQL is what the tests assert on (via
`--dry-run --keep-artifacts`, exactly like `test_ingest_mark_missing.py`, or by
calling `build_sql([...], ...)` directly like `test_ingest_status_activation.py`).

### 4.1 M1: count-aware folded coverage (DATA-LOSS BUG; must precede mark-missing)

**Location:** `main()`, the mark-missing eligibility loop at lines ~1281-1296,
specifically `has_complete_folded_coverage` at line 1285.

**Current:**
```python
has_complete_folded_coverage = len(known_keys) == 1 or known_keys <= seen_keys
```
`seen_keys` is presence-only (`source_keys_by_slug_seen`, populated from
`source_entries` regardless of count). A folded source that returned ZERO rows
without an error still satisfies `known_keys <= seen_keys`.

**Required behavior:** every folded key in `known_keys` must have a NONZERO
contribution this run. Use the per-source-key DISCOVERED or STAGED count.

**Implementation contract:**
- Build a per-source-key count map keyed by `sourceKey` (NOT by slug). Two
  options, both acceptable; pick the staged-count form for parity with the
  destructive UPDATE's actual scope:
  - From `source_entries`: sum `e.get("listingsCollected") or 0` per
    `e["sourceKey"]` into `discovered_by_source_key`.
  - OR from `rows`: count staged rows per `r`'s originating source key. NOTE
    `rows` do not carry `sourceKey` after `to_row` (they carry `slug`), so the
    artifact-entry `listingsCollected` path is the required one. Add a dict:
    ```python
    discovered_by_source_key = {}
    for e in source_entries:
        sk = e.get("sourceKey")
        if sk in SOURCE_TO_BROKERAGE:
            discovered_by_source_key[sk] = discovered_by_source_key.get(sk, 0) + (e.get("listingsCollected") or 0)
    ```
    (Build this in `main()` near the existing `slug_stats` accumulation, or fold
    it into that same loop.)
- Replace the coverage test with a count-aware one:
  ```python
  has_complete_folded_coverage = (
      len(known_keys) == 1
      or (known_keys <= seen_keys
          and all(discovered_by_source_key.get(k, 0) > 0 for k in known_keys))
  )
  ```
- Keep the existing `elif len(known_keys) > 1 and not has_complete_folded_coverage`
  note branch. Optionally enrich the note to mention a zero-count folded key, but
  the existing wording is acceptable; do not over-engineer.

**Singletons (`len(known_keys) == 1`) keep firing** on the floor + error-free
checks alone (the `discovered` count is not required for singletons; the
`--mark-missing-floor` staged-count check already covers them). Do NOT add a
zero-count gate to singletons; that would regress `test_ingest_mark_missing.py`
case 3 only if the count were zero, which it is not for a real run, but to be
safe keep singletons on the `len(known_keys) == 1` short-circuit.

**Test:** `tests/test_folded_coverage_count_aware.py`
- Reuse the artifact-builder pattern from `test_ingest_mark_missing.py` (run via
  `cre_ingest.py --dry-run --mark-missing --mark-missing-floor 1
  --keep-artifacts`).
- CASE A (the bug): artifact lists BOTH `colliers` and `colliers-main` in
  `sources[]`, but `colliers-main` has `listingsCollected: 0` and contributes NO
  listings, while `colliers` clears the floor. Assert the generated SQL does NOT
  contain a mark-missing block scoped to slug `colliers` (`_mark_missing_present_for_slug`
  helper, copy it locally). Assert stderr shows the skip note.
- CASE B (control): BOTH present with nonzero `listingsCollected` and listings ->
  mark-missing SQL for `colliers` IS present.
- CASE C (singleton unaffected): `svn` singleton with rows fires (mirror
  `test_ingest_mark_missing.py` case 3) to prove the count-gate did not regress
  singletons.

### 4.2 L1: price COALESCE-keep (BUG)

**Location:** `build_sql()`, the upsert `DO UPDATE SET` block, lines 949-953.

**Current:**
```sql
sale_price_usd    = EXCLUDED.sale_price_usd,
sale_price_per_sf = EXCLUDED.sale_price_per_sf,
...
lease_rate_min    = EXCLUDED.lease_rate_min,
lease_rate_max    = EXCLUDED.lease_rate_max,
```

**Required:**
```sql
sale_price_usd    = COALESCE(EXCLUDED.sale_price_usd, t.sale_price_usd),
sale_price_per_sf = COALESCE(EXCLUDED.sale_price_per_sf, t.sale_price_per_sf),
...
lease_rate_min    = COALESCE(EXCLUDED.lease_rate_min, t.lease_rate_min),
lease_rate_max    = COALESCE(EXCLUDED.lease_rate_max, t.lease_rate_max),
```
A real new numeric value still overwrites; only a NULL (transient parse miss)
keeps the prior good value. This mirrors the neighbors (`cap_rate`,
`property_type`, etc.) that already COALESCE-keep.

**Test:** `tests/test_price_coalesce.py`
- Call `build_sql([], [], scraped_at, set())` (like
  `test_ingest_status_activation.py::_sql`).
- Assert each of the four exact strings:
  `"sale_price_usd    = COALESCE(EXCLUDED.sale_price_usd, t.sale_price_usd)"`,
  same for `sale_price_per_sf`, `lease_rate_min`, `lease_rate_max`.
- Assert the OLD unconditional forms are GONE: e.g.
  `"sale_price_usd    = EXCLUDED.sale_price_usd," not in sql` (with the trailing
  comma to avoid matching the COALESCE form, which has `EXCLUDED.sale_price_usd,`
  followed by ` t.sale_price_usd)`). Use a precise substring that cannot match
  the new form, for example `"= EXCLUDED.sale_price_usd,\n"` must not appear.

### 4.3 M5: revival terminal-stickiness

**Location:** `build_sql()`, the upsert `DO UPDATE SET` status CASE, line 933.

**Current:**
```sql
status            = CASE WHEN t.deleted_at IS NOT NULL THEN 'active' ELSE t.status END,
```
This resets ANY soft-deleted row to `active` on revival, even a real terminal
(`sold`/`leased`/`off_market`) that flickered back into a feed.

**Required:** reset to `active` only when the prior status was `inactive` (the
mark-missing soft-delete marker). Otherwise keep the prior status:
```sql
status            = CASE WHEN t.deleted_at IS NOT NULL AND t.status = 'inactive'
                         THEN 'active' ELSE t.status END,
```

**deleted_at handling (decision):** KEEP the existing
`deleted_at = NULL` line (962) UNCHANGED. Revival un-deletes the row in both
cases. The intended semantics: a row that was mark-missing soft-deleted
(`status='inactive'`, `deleted_at` set) and reappears becomes a live `active`
row again; a row that carried a real terminal and was soft-deleted (only
possible once status activation is ON and a terminal row is later mark-missing'd)
reappears as a live row that KEEPS its terminal label, so the board gate (which
excludes terminal statuses) still hides it. This matches the review's "keep
prior status" instruction while preserving the documented un-delete recovery.
Add a one-line SQL comment on the CASE explaining the `= 'inactive'` guard (no em
dashes).

**Test:** `tests/test_revival_terminal_stickiness.py`
- Call `build_sql([], [], scraped_at, set())`.
- Assert the new CASE string is present:
  `"status            = CASE WHEN t.deleted_at IS NOT NULL AND t.status = 'inactive'"`.
- Assert the OLD unconditional revival string is GONE:
  `"status            = CASE WHEN t.deleted_at IS NOT NULL THEN 'active' ELSE t.status END," not in sql`.
- Assert `"deleted_at        = NULL," in sql` (un-delete still happens).
- Note: `test_ingest_status_activation.py::test_update_keeps_status_sticky_resetting_only_resurrected`
  asserts the OLD exact CASE string. That test is OWNED BY ANOTHER FILE and Owner
  A must NOT edit it. This is a KNOWN, intended conflict: Owner A's M5 change
  makes that one existing assertion stale. Flag it in the return summary as a
  follow-up for the integration owner to update (it is NOT Owner A's to edit, and
  not in any other owner's scope either). Do not silently break it without
  flagging. See section 7 "Cross-owner notes".

### 4.4 M3: emit `disappeared` event on mark-missing (pairs with H4a / M2)

**Location:** `build_sql()`, the mark-missing block (lines 1098-1109), gated by
`if mark_missing_slugs:`.

**Current:** a single `UPDATE ... SET deleted_at = now(), status = 'inactive'`.
No event row, no archive.

**Required (same transaction, in this order):**
1. Capture the soon-to-be-retired listing ids AND their prior status BEFORE the
   UPDATE overwrites status, using a CTE / temp selection.
2. Run the existing soft-delete UPDATE.
3. INSERT one `cre_listing_events` row per retired listing:
   - `event_type = 'disappeared'`
   - `field = 'status'`
   - `old_value` = the prior status (captured in step 1)
   - `new_value = 'inactive'`
   - `detected_at = now()` (column default is `now()`, so it can be omitted, but
     set it explicitly for clarity)
   - `brokerage_id` = the listing's brokerage_id
   - `source_value = 'mark_missing'` (distinguishes ingest-emitted disappearance
     from the monitor's `'enumeration_gone'`)
   - `scrape_job_id` left NULL (the ingest run inserts `cre_scrape_jobs` rows per
     brokerage AFTER this block; do not try to wire a job id here. The
     `cre_listing_events_idem_uq` index is `NULLS NOT DISTINCT`, so a NULL
     `scrape_job_id` collapses correctly; use `ON CONFLICT ... DO NOTHING` on the
     idempotency key to stay safe).

**Recommended SQL shape** (rewrite the mark-missing block as a chain that
captures retired rows once and reuses them for the event INSERT, the archive
INSERTs (M2), and the price-history is NOT needed here because retirement is a
status change already captured by the disappeared event; price-history is for
the upsert path, section 4.5). Use a temp table so the same retired set feeds the
event and both archive INSERTs:

```sql
-- Full-run reconciliation: soft-delete listings this clean full run no longer
-- sees. Capture the retired set FIRST (with prior status) so the disappeared
-- event and the contact/document archive snapshot reference the same rows in
-- this one transaction.
CREATE TEMP TABLE _retired ON COMMIT DROP AS
SELECT l.id, l.brokerage_id, l.status AS prior_status
FROM credeals.cre_listings l
JOIN credeals.cre_brokerages b ON l.brokerage_id = b.id
WHERE b.slug IN (<slug_list>)
  AND l.deleted_at IS NULL
  AND l.external_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM _up u WHERE u.id = l.id);

UPDATE credeals.cre_listings l
SET deleted_at = now(), status = 'inactive', updated_at = now()
FROM _retired r
WHERE l.id = r.id;

INSERT INTO credeals.cre_listing_events
    (listing_id, brokerage_id, event_type, field, old_value, new_value,
     source_value, detected_at)
SELECT r.id, r.brokerage_id, 'disappeared', 'status', r.prior_status, 'inactive',
       'mark_missing', now()
FROM _retired r
ON CONFLICT (listing_id, event_type, COALESCE(field, ''), COALESCE(new_value, ''), scrape_job_id)
DO NOTHING;
```
- `<slug_list>` is the SAME `slug_list` join the current code builds (the
  `", ".join("'" + s.replace("'", "''") + "'" ...)` expression). Reuse it.
- This replaces the current single UPDATE. The `_retired` temp table is the
  contract M2 (section 4.6) builds on.

**Existence-guard:** `cre_listing_events` already exists in 007 and is applied to
prod, so the event INSERT does NOT need the H4a-style existence guard. (Only the
NEW tables, price-history and the archives, need guarding. See 4.5/4.6.)

**Test:** `tests/test_disappeared_event_on_mark_missing.py`
- Build an artifact for a singleton brokerage (`svn`) that clears the floor and
  passes coverage so the mark-missing block IS emitted (mirror
  `test_ingest_mark_missing.py` case 3 to get a positive mark-missing run).
- Run `--dry-run --keep-artifacts`.
- Assert the generated SQL contains:
  - `CREATE TEMP TABLE _retired`
  - `INSERT INTO credeals.cre_listing_events`
  - `'disappeared'`
  - `'mark_missing'`
  - the `old_value` selection from `_retired` (e.g. `r.prior_status`)
  - `new_value` literal `'inactive'`
- Assert that when NO brokerage is mark-missing-eligible (e.g. an artifact whose
  only brokerage is blocked, like `test_ingest_mark_missing.py` case 1), the SQL
  contains NO `INSERT INTO credeals.cre_listing_events` and NO `_retired` table.

### 4.5 H4a-write: append-only price history on upsert

**Location:** `build_sql()`, immediately AFTER the `_up` CTE materializes (so the
`_src` vs prior-`cre_listings` comparison is taken on the values BEFORE the
upsert UPDATE mutates them). CRITICAL ORDERING: the comparison must read the
prior `cre_listings` values. Two correct approaches; use approach (i):

- (i) **Capture prior values in a temp table BEFORE the upsert.** Insert a
  `CREATE TEMP TABLE _prior_vals ON COMMIT DROP AS SELECT ...` BEFORE the `_up`
  INSERT, snapshotting the watched fields of every existing row that matches a
  staged `_src` key. Then after `_up`, join `_src` (new values) to `_prior_vals`
  (old values) and INSERT a history row where any watched field
  `IS DISTINCT FROM` (using the COALESCE-keep semantics, see below).

Because L1 makes the actual written value `COALESCE(EXCLUDED.x, t.x)`, the diff
must compare the staged new value's effective form to the prior. The exact
predicate uses the staged `s.*` values and the prior `p.*` values, treating a
NULL staged value as "no change" for that field:

```sql
-- (H4a) Capture prior watched values BEFORE the upsert mutates them, so the
-- append-only price history records a row only on a REAL change. Guarded at
-- write time below (no-op when the table is not yet applied to prod).
CREATE TEMP TABLE _prior_vals ON COMMIT DROP AS
SELECT t.id, t.brokerage_id, t.external_id,
       t.sale_price_usd, t.sale_price_per_sf, t.lease_rate_min, t.lease_rate_max,
       t.status, t.cap_rate
FROM credeals.cre_listings t
JOIN _src s ON s.brokerage_id = t.brokerage_id AND s.external_id = t.external_id;
```
(Place this BEFORE the `CREATE TEMP TABLE _up ...` block.)

Then, AFTER the `_up` CTE and AFTER the Phase-2 status activation UPDATE (so the
history captures the post-activation status when activation is ON; when
activation is OFF the staged status is NULL and the row's effective status is
unchanged, so this is consistent), emit the guarded history INSERT. The diff is
computed against the NEW effective values. To keep it simple and correct, read
the new effective values back from `cre_listings` (post-upsert) joined to
`_prior_vals`:

```sql
-- (H4a) Append-only price/status history: one row per listing whose watched
-- value actually changed this run. Existence-guarded: a no-op unless
-- credeals.cre_listing_price_history exists (so a pre-apply prod ingest still
-- runs). In --dry-run the guard is emitted UNCONDITIONALLY so tests can assert
-- the INSERT shape.
<HISTORY_GUARD_OPEN>
INSERT INTO credeals.cre_listing_price_history
    (listing_id, observed_at, sale_price_usd, sale_price_per_sf,
     lease_rate_min, lease_rate_max, status, cap_rate, source_lastmod, transaction_type)
SELECT t.id, now(), t.sale_price_usd, t.sale_price_per_sf,
       t.lease_rate_min, t.lease_rate_max, t.status, t.cap_rate, t.source_lastmod, t.transaction_type
FROM credeals.cre_listings t
JOIN _prior_vals p ON p.id = t.id
WHERE t.sale_price_usd    IS DISTINCT FROM p.sale_price_usd
   OR t.sale_price_per_sf IS DISTINCT FROM p.sale_price_per_sf
   OR t.lease_rate_min    IS DISTINCT FROM p.lease_rate_min
   OR t.lease_rate_max    IS DISTINCT FROM p.lease_rate_max
   OR t.status            IS DISTINCT FROM p.status
   OR t.cap_rate          IS DISTINCT FROM p.cap_rate;
<HISTORY_GUARD_CLOSE>
```
This records the NEW values (post-upsert) and only when something watched moved.
A first-ever INSERT of a brand-new listing has no `_prior_vals` row, so it is NOT
in this join and gets NO history row (history starts at the first CHANGE, which
is correct and bloat-minimal; document this in a comment).

**The existence-guard mechanism (the ONE clean mechanism, used for H4a AND M2):**

Add a `--dry-run`-aware boolean to `build_sql()` so the generated SQL wraps the
new-table writes in a guard. Implement it in SQL as a `DO $$ ... $$` block that
checks `to_regclass`, so the deployed (non-dry-run) ingest is a true no-op when
the table is absent, and the dry-run emits it unconditionally for tests.

Recommended exact mechanism (no Python preflight DB query needed; the guard
lives in the generated SQL so it is identical in dry-run and apply, and there is
NO connection in dry-run):

- `build_sql(...)` gains a parameter, e.g. `history_guard: bool = True`. When
  `True` (the default, used by real apply), wrap each new-table write in:
  ```sql
  DO $$ BEGIN
    IF to_regclass('credeals.cre_listing_price_history') IS NOT NULL THEN
      <the INSERT, as a nested statement via EXECUTE or plain SQL>
    END IF;
  END $$;
  ```
  Because the INSERT uses temp tables (`_prior_vals`, `_up` already committed to
  temp), and PL/pgSQL can reference temp tables, place the INSERT directly inside
  the `IF`. Use plain SQL inside the DO block (PL/pgSQL allows DML referencing
  temp tables by name).
- When `--dry-run` is set, `main()` calls `build_sql(..., history_guard=False)`
  so the INSERTs are emitted as PLAIN top-level statements (no DO wrapper), which
  the offline tests can assert on directly. RATIONALE: the dry-run never
  connects, so emitting the unguarded INSERT is safe and makes the contract
  testable; the real apply always uses the guarded form, which is a no-op
  pre-apply.

  Concretely in `main()`:
  ```python
  sql = build_sql(rows, job_meta, started_at, mark_missing_slugs,
                  history_guard=not args.dry_run)
  ```

- Apply the SAME `history_guard` wrapper to the M2 archive INSERTs (section 4.6)
  and to the H4a price-history INSERT. The `to_regclass` check for the archives
  targets `credeals.cre_listing_contacts_archive` and
  `credeals.cre_listing_documents_archive` respectively (one `IF` per table, or a
  single `IF` per table-pair; one-per-table is clearer).

- The `_prior_vals` temp table itself is NOT guarded (it only reads
  `cre_listings`, always present). Only the INSERT INTO the NEW table is guarded.

This satisfies the spec's requirement: "cre_ingest.py runs a cheap preflight
... only emits the history INSERT when true; in --dry-run emit it
unconditionally so tests assert it." Here the preflight is `to_regclass` inside
the transaction (cheaper and race-free vs a separate Python round-trip, and it
needs no DB connection at SQL-build time), and dry-run emits it unconditionally.

**Test:** `tests/test_price_history_snapshot.py`
- Call `build_sql([], [], scraped_at, set())` (dry-run shape: default
  `history_guard=True`) AND a second call with `history_guard=False` to mirror
  what `main --dry-run` produces. Test BOTH:
  - With `history_guard=False` (dry-run form): assert
    `"INSERT INTO credeals.cre_listing_price_history" in sql`, the exact column
    list string, and the six `IS DISTINCT FROM` watched-field predicates are all
    present, and `"_prior_vals" in sql`. Assert there is NO
    `to_regclass('credeals.cre_listing_price_history')` wrapper.
  - With `history_guard=True` (apply form): assert
    `"to_regclass('credeals.cre_listing_price_history')" in sql` and that the
    INSERT is present (inside the guard).
- Assert the column list is EXACTLY:
  `"(listing_id, observed_at, sale_price_usd, sale_price_per_sf,\n     lease_rate_min, lease_rate_max, status, cap_rate, source_lastmod, transaction_type)"`
  (match the substring you emit; keep it byte-identical to the spec).
- Assert `_prior_vals` is created BEFORE `_up`:
  `sql.index("_prior_vals") < sql.index("CREATE TEMP TABLE _up")`.

### 4.6 M2-archive-write: snapshot contacts + documents at retirement

**Location:** inside the `if mark_missing_slugs:` block, AFTER the `_retired`
temp table is created (section 4.4) and after the soft-delete UPDATE. EXCLUDE
images entirely. Never touch the live child tables. Guarded with the same
`history_guard` mechanism.

```sql
-- (M2) Snapshot the retired listings' FINAL contacts and documents into the
-- append-only archives, in this same transaction, BEFORE any future re-scrape
-- wholesale-replaces the live child rows. Contacts + documents only; images are
-- excluded (high volume, low historical value). Guarded so a pre-apply ingest
-- is a no-op.
<HISTORY_GUARD_OPEN cre_listing_contacts_archive>
INSERT INTO credeals.cre_listing_contacts_archive
    (source_listing_id, name, title, email, phone, brokerage_name,
     profile_url, avatar_url, vcard_url, is_primary)
SELECT c.listing_id, c.name, c.title, c.email, c.phone, c.brokerage_name,
       c.profile_url, c.avatar_url, c.vcard_url, c.is_primary
FROM credeals.cre_listing_contacts c
JOIN _retired r ON r.id = c.listing_id;
<HISTORY_GUARD_CLOSE>

<HISTORY_GUARD_OPEN cre_listing_documents_archive>
INSERT INTO credeals.cre_listing_documents_archive
    (source_listing_id, doc_type, title, url)
SELECT d.listing_id, d.doc_type, d.title, d.url
FROM credeals.cre_listing_documents d
JOIN _retired r ON r.id = d.listing_id;
<HISTORY_GUARD_CLOSE>
```
- Column lists MUST match section 3.4 exactly.
- These run only when `mark_missing_slugs` is non-empty (the block is already
  conditional). No images. No raw_data.

**Test:** `tests/test_child_history_archive_on_retirement.py`
- Positive mark-missing artifact (singleton `svn`, mirror case 3), run
  `--dry-run --keep-artifacts`.
- Assert generated SQL contains:
  - `INSERT INTO credeals.cre_listing_contacts_archive`
  - `INSERT INTO credeals.cre_listing_documents_archive`
  - the exact contacts archive column list string
  - the exact documents archive column list string
  - `JOIN _retired r`
- Assert there is NO images archive write: `"images_archive" not in sql` and
  no `INSERT INTO credeals.cre_listing_images_archive`.
- Assert the archive INSERTs do NOT appear when no brokerage is mark-missing
  eligible (blocked-brokerage artifact like case 1): the archive INSERT strings
  must be absent.
- Dry-run form (`history_guard=False`, which is what `main --dry-run` uses):
  assert the archive INSERTs are present and unguarded. (The subprocess
  `--dry-run` path already passes `history_guard=False` per 4.5, so the
  `--keep-artifacts` SQL is the unguarded form. Good: assert directly on it.)

### 4.7 L4a: widen the flip-breaker trip metric

**Location:** `build_sql()`, the status-flip pre-flight `DO $$` block, lines
992-994 (the `leaving_active` FILTER).

**Current:**
```sql
count(*) FILTER (
    WHERE s.status IS NOT NULL AND t.status = 'active'
) AS leaving_active,
```
Counts only rows leaving `status='active'`. A row moving `under_contract -> sold`
(both non-active) is not counted, so the breaker undercounts a regression that
churns non-active rows.

**Required:** count ANY non-active reclassification, i.e. any row whose CURRENT
status is not `active` AFTER the qualifying change. The metric should match the
`changes` FILTER's qualification but count reclassifications regardless of the
prior being `active`. Rename the semantics but KEEP the column name
`leaving_active` (the trip condition below references it) OR introduce a clear
new alias and update the trip condition. To minimize churn, KEEP the name and
widen the predicate:

```sql
count(*) FILTER (
    WHERE s.status IS NOT NULL
      AND t.status IS DISTINCT FROM s.status
      AND s.status <> 'active'
      AND NOT (t.status IN ('sold','leased','off_market')
               AND s.status IN ('under_contract','pending'))
) AS leaving_active,
```
This counts every row reclassified TO a non-active status this run (the true
"flip out of the on-market set" signal), including `under_contract -> sold`,
not only departures from `active`. The trip condition
(`rec.leaving_active::numeric / rec.active_base > v_cap`) is unchanged; the
denominator `active_base` (count of currently-`active` rows) stays as-is, which
remains a conservative base.

Add/extend the SQL comment above the block to state the widened metric (no em
dashes). Keep the `CRE_STATUS_FLIP_MIN_BASE` exemption and the terminal-guard
clause exactly as they are; the review's "reconsider the 200-row exemption" is
DEFERRED (see section 6), do not change `min_base`.

**Test:** Owner A may add assertions to `tests/test_price_coalesce.py` is NOT
allowed (that file is for L1). Add the L4a assertion to a NEW dedicated file?
The owner's allotted new test files are fixed (6 names). Put the L4a assertion
inside `tests/test_revival_terminal_stickiness.py` is also off-topic. DECISION:
add the L4a flip-metric assertion to `tests/test_folded_coverage_count_aware.py`
is off-topic too. To stay within the 6 named files and keep each focused, add the
L4a assertion as an extra test function inside
`tests/test_disappeared_event_on_mark_missing.py` under a clearly named function
`test_flip_breaker_metric_counts_any_non_active_reclassification`, since both
concern the destructive/risk-guard SQL. Assert against
`build_sql([], [], scraped_at, set())`:
- `"AND s.status <> 'active'" in sql` (the widened predicate)
- the `leaving_active` alias still present.
- the OLD narrow form `"WHERE s.status IS NOT NULL AND t.status = 'active'\n               ) AS leaving_active"` is GONE (match a precise substring that only the old form produced).
(If a single-purpose file is preferred by the integration owner later, it can be
split; for this delivery keep it in the named file to respect the 6-file limit.)

### 4.8 Owner A verification (offline)

```bash
cd scripts/firecrawl-ops/cre_collector
python3 -m pytest tests/test_folded_coverage_count_aware.py tests/test_price_coalesce.py \
  tests/test_revival_terminal_stickiness.py tests/test_disappeared_event_on_mark_missing.py \
  tests/test_price_history_snapshot.py tests/test_child_history_archive_on_retirement.py -q
python3 -c "import py_compile; py_compile.compile('cre_ingest.py', doraise=True)"
```
Do NOT run the full suite expecting 0 failures: the one known-stale assertion in
`test_ingest_status_activation.py` (M5, section 4.3) will fail until the
integration owner updates it. Report that explicitly. Everything Owner A OWNS
must pass.

---

## 5. OWNER C: `cre_monitor.py` + `tests/test_monitor_old_value.py`

H4b-populate: persist current price into the new `cre_source_index` columns on
each enumeration write, and populate `old_value` on `price_change` events from
the prior value.

### 5.1 Load the prior price columns

**Location:** `load_prior_state()` (lines 769-781), the `cre_source_index` read,
and the `prior_index` dict shape.

**Required:**
- Extend the `cre_source_index` SELECT to also read the three new columns:
  `COALESCE(prior_sale_price::text, '')`, `COALESCE(prior_lease_rate::text, '')`,
  `COALESCE(prior_status, '')`. Because the columns may not exist on an
  un-migrated DB, this is a read against `cre_source_index`. EXISTENCE NOTE: the
  monitor connects read-only and the columns are added by Owner B's 009 before
  the monitor's apply tier is ever loaded (gated). To be robust, Owner C SHOULD
  guard the read: select the new columns via a `to_regclass`-style column-present
  check is awkward in a single query. SIMPLER, ACCEPTED APPROACH: read the new
  columns directly; the apply path is gated and only runs after 009 is applied,
  consistent with how the monitor already assumes 007 columns exist. Document
  this assumption in a comment. (The monitor is observe-only and gated; it is not
  the pre-apply-safety-critical path that the ingestor is.)
- Extend the `prior_index[(bid, eid)]` dict with keys: `prior_sale_price`
  (float or None), `prior_lease_rate` (float or None), `prior_status` (str or
  None). Parse the text fields to float where non-empty (mirror the existing
  `fp or None` pattern). Use a small local `_num_or_none(text)` helper or inline
  `float(x) if x else None` guarded by a try/except for safety.

### 5.2 Populate `old_value` on `price_change`

**Location:** `derive_events()`, the `price_change` branch (lines 417-433).

**Current:** `old_value=None`. The prior fingerprint is carried in
`source_value` as evidence.

**Required:** set `old_value` from the prior price observed last run:
```python
prior_price = prior.get("prior_sale_price")
if prior_price is None:
    prior_price = prior.get("prior_lease_rate")
old_value_str = None
if prior_price is not None:
    if isinstance(prior_price, float) and prior_price.is_integer():
        old_value_str = str(int(prior_price))
    else:
        old_value_str = str(prior_price)
```
Pass `old_value=old_value_str` into the `_event(...)` call for `price_change`.
KEEP `source_value=prior["fingerprint"]` (the fingerprint evidence) and KEEP
`new_value` as before. When no prior price is known (first observation after the
columns were added, or a text-only prior), `old_value` stays None, which is the
pre-H4b behavior, so this is a strict improvement with no regression.

### 5.3 Persist current price into the new columns on the index refresh

**Location:** `build_write_sql()`, step (3) the `cre_source_index` upsert
(`INSERT INTO credeals.cre_source_index AS si ... ON CONFLICT ... DO UPDATE`,
lines 620-634), and the `_ENUM_COLS` / `_enum` staging (lines 486-489, 546-564).

**Required:** the index must store THIS run's price so NEXT run can read it as
`prior_*`. The cleanest shape:
- Extend the staged enumeration to carry the current numeric sale price and
  lease rate. Add to `_ENUM_COLS` and the `_enum` temp table DDL two columns:
  `cur_sale_price numeric, cur_lease_rate numeric`. Populate them in the COPY
  loop from the finalized group (`g["sale_price_usd"]`, `g["lease_rate_min"]`).
- On INSERT (new index row): set `prior_sale_price = NULL, prior_lease_rate =
  NULL, prior_status = NULL` (no prior on first sight), but ALSO you need the
  CURRENT price stored so next run sees it as prior. DECISION: store the current
  price AS the prior columns, because "prior" means "the value from the
  enumeration that wrote this index row", read by the NEXT run. Concretely:
  - On INSERT: `prior_sale_price = EXCLUDED-equivalent current sale price`,
    `prior_lease_rate = current lease rate`, `prior_status = observed_status`.
  - On `ON CONFLICT DO UPDATE`: BEFORE overwriting, the OLD row's
    `prior_sale_price` is what `load_prior_state` already read this run as the
    prior. So on update, SET `prior_sale_price = <this run's current sale
    price>`, `prior_lease_rate = <this run's current lease rate>`, `prior_status
    = EXCLUDED.observed_status`.

  IMPORTANT SEMANTIC: `prior_*` is named from the READER's perspective (the next
  run). At write time it holds the CURRENT run's value. `load_prior_state` reads
  it at the START of the next run (before this run's write), so it correctly sees
  the previous run's value. This is the same one-slot-history pattern the
  fingerprint already uses. Document this clearly in a SQL comment and a Python
  comment so it is not mistaken for a bug.

  Exact upsert additions:
  ```sql
  -- INSERT column list adds: prior_sale_price, prior_lease_rate, prior_status
  -- SELECT adds: cur_sale_price, cur_lease_rate, observed_status
  ...
  ON CONFLICT (brokerage_id, external_id) DO UPDATE SET
      ...
      prior_sale_price = EXCLUDED.prior_sale_price,
      prior_lease_rate = EXCLUDED.prior_lease_rate,
      prior_status     = EXCLUDED.prior_status,
      ...
  ```
  where the INSERT `SELECT` maps `cur_sale_price -> prior_sale_price`,
  `cur_lease_rate -> prior_lease_rate`, `observed_status -> prior_status`.

  (Using EXCLUDED in the DO UPDATE is correct because EXCLUDED holds the staged
  current values, which become the next run's "prior".)

- OBSERVE-ONLY INVARIANT PRESERVED: this only writes `cre_source_index`
  (monitor-owned), never `cre_listings.status`/`deleted_at`. The existing test
  `test_observe_only_generated_sql_has_no_listing_status_or_deleted_write`
  (owned by `test_monitor_events.py`, do NOT edit) must still pass: your changes
  touch only `cre_source_index`, so that invariant holds. Verify it stays green.

### 5.4 Test: `tests/test_monitor_old_value.py`

Pure, no DB. Two parts:
- `derive_events` old_value population: build a `prior_index` entry that carries
  `prior_sale_price` (use a helper like `test_monitor_events.py::_index_entry`
  but extend the dict with the three new keys). Drive a `price_change`:
  - prior fingerprint differs from current, status unchanged, prior_sale_price =
    500000, current sale_price_usd = 600000. Assert the emitted `price_change`
    event has `old_value == "500000"`, `new_value == "600000"`,
    `source_value == prior_fingerprint`.
  - lease fallback: prior_sale_price None, prior_lease_rate = 25.0, current lease
    move. Assert `old_value == "25"` (integer-valued float renders without
    decimal) for the lease case, matching the helper's int-render rule. (If the
    prior lease rate is 25.5, assert `"25.5"`.)
  - no prior: prior_sale_price None and prior_lease_rate None -> `old_value is
    None` (pre-H4b behavior preserved).
- `build_write_sql` shape: call
  `m.build_write_sql([g], [], {}, {}, [], RUN, started_at, notes, ["colliers"])`
  with a `g` carrying a sale price, and assert the generated SQL:
  - contains `prior_sale_price`, `prior_lease_rate`, `prior_status` in both the
    INSERT column list and the `ON CONFLICT ... DO UPDATE SET` clause.
  - still contains exactly one `UPDATE credeals.cre_listings` block and NO
    `cre_listings ... SET status`/`deleted_at` assignment (re-assert the
    observe-only invariant locally so a regression here is caught in Owner C's
    own file).
- You MUST replicate any `_g`/`_index_entry` helpers locally in the new test file
  (do not import from `test_monitor_events.py`); extend them with the new keys.

### 5.5 Owner C verification

```bash
cd scripts/firecrawl-ops/cre_collector
python3 -m pytest tests/test_monitor_old_value.py -q
python3 -m pytest tests/test_monitor_events.py tests/test_monitor.py -q   # must stay green
python3 -c "import py_compile; py_compile.compile('cre_monitor.py', doraise=True)"
```
The existing monitor tests assert `old_value is None` for a `price_change` in a
scenario WITHOUT prior price columns (`test_monitor_events.py::test_price_change_fires_when_only_price_moves`
builds `prior_index` via the OLD `_index_entry` with no `prior_sale_price` key).
Owner C must ensure that `derive_events` treats a MISSING `prior_sale_price` key
as None (use `prior.get("prior_sale_price")`, never `prior["prior_sale_price"]`),
so that existing test stays green (its prior entry has no such key -> `.get`
returns None -> `old_value` None). This is a HARD requirement: use `.get` with
default None for all three new keys.

---

## 6. OWNER D: `sources/savills.ts` + `tests/ts/sources/savills-commercial.test.ts`

### 6.1 L5: IsCommercial guard on the sale path (BUG/risk)

**Location:** `srcSavills()` sale branch, lines 280-365. The sale path scrapes
`/com/en/list/property-for-sale/...` (residential surface) and pushes EVERY card
with a US location, with NO commercial filter, unlike the lease branch which
filters `row?.IsCommercial === true` (line 221).

**Required:** add a commercial-surface guard so residential cards cannot be
ingested. The sale path parses HTML cards (not `__NEXT_DATA__` property objects),
so there is no `IsCommercial` boolean per card. Implement the guard as a
PURE, UNIT-TESTABLE helper plus its application:

- Add an exported helper, e.g.:
  ```ts
  // A Savills public sale card is only kept when it is a commercial-surface
  // listing. The generic /property-for-sale/ surface is residential luxury
  // homes; this guard prevents residential contamination (101 homes were
  // ingested and soft-deleted on 2026-06-14) if the sale path ever runs
  // additively again. Returns true only for commercial-classified cards.
  export function savillsSaleCardIsCommercial(card: {
    propertyType?: string | null;
    href?: string | null;
    cardText?: string | null;
  }): boolean { ... }
  ```
  Define "commercial" conservatively: TRUE when the card's URL path or property
  type or card text indicates a commercial asset class, FALSE otherwise. Use a
  small allowlist of commercial keywords (office, retail, industrial, warehouse,
  mixed use, land, hospitality, hotel, leisure, commercial, development) and/or a
  URL-segment check for `/commercial/`. RESIDENTIAL markers (house, apartment,
  flat, bedroom, residential, villa, cottage) force FALSE. When NOTHING indicates
  commercial, return FALSE (default-deny: the generic surface is residential, so
  the safe default is to drop). Document the default-deny rationale.

  Because the generic `/property-for-sale/` surface yields `totalItems: 0` for US
  commercial (verified, the cap stands), default-deny means the sale path will
  collect ~0 US commercial rows, which is the correct, capped outcome and stops
  residential contamination. This MATCHES the documented structural cap.

- APPLY the guard inside the `$('a[href*="/property-detail/"]').each(...)` loop:
  after computing `name`, `priceBlock`, `sizeText`, and BEFORE
  `listings.push(...)`, compute
  `const isCommercial = savillsSaleCardIsCommercial({ propertyType: <asset/type text if available>, href: abs, cardText: clean(card.text()) });`
  and `if (!isCommercial) { nonCommercialFiltered++; return; }`. Add a
  `let nonCommercialFiltered = 0;` counter and fold it into the returned `note`
  (mirroring `nonUsFiltered`). Do NOT throw when all cards are filtered as
  non-commercial: extend the terminal "no links" guard so a run that finds links
  but filters them all as residential returns an empty-but-valid result with a
  descriptive note, NOT an error (an error would trip the monitor's disappearance
  refusal, which is fine, but an empty commercial result is the expected capped
  state, so return cleanly). Update the existing
  `if (!listings.length && !nonUsFiltered)` throw to also spare the
  non-commercial-filtered case: `if (!listings.length && !nonUsFiltered &&
  !nonCommercialFiltered)`.

### 6.2 L3: paginate the commercial-lease path

**Location:** `srcSavillsCommercialLease()` (lines 218-272). Currently fetches
ONE page and slices, while the sale path paginates via `/page/N`.

**Required:** add a pagination loop mirroring the sale loop (lines 285-362),
driven by `savillsTotalItems(html, ...)`:
- Keep the first fetch of the base lease URL
  (`/com/en/list/commercial/property-to-let/united-states-of-america`).
- Read `const total = savillsTotalItems(html, ...)`.
- Loop additional pages `/page/N` while `listings.length < max` and there are
  more `totalItems` than rows collected and `page <= Math.max(PAGE_CAP, 10)` (use
  the same cap shape as the sale loop). Each page: `savillsNextDataProperties` +
  `IsCommercial === true` filter (preserve the existing filter), parse rows the
  same way, dedupe by detail id / url (mirror the sale loop's `seenHere` set or a
  url-set), and apply the same empty-streak break (`emptyStreak >= 3`) the sale
  loop uses.
- Set `truncated` appropriately on the returned `SourceResult`: set
  `truncated: true` when `listings.length < Math.min(max, total)` after the loop
  ends (i.e. you stopped before collecting the full reported total, or hit the
  page cap). This mirrors the adapter contract in `sources/CLAUDE.md` (`truncated`
  drives the monitor's disappearance gating). When the full set was collected,
  leave `truncated` unset/false.
- Preserve the existing `nonUsFiltered` accounting and `note`.
- Keep `savillsContact`, `savillsDocumentUrls`, `savillsImageUrls`,
  `parseSavillsUsLocation`, `savillsSqft` usage identical (do not change those
  helpers; they are unit-tested already in `savills.test.ts`, which Owner D must
  NOT edit).

Refactor the per-row mapping into a small local helper
(e.g. `function mapSavillsLeaseRow(row, sourceUrl): listing | null`) so the loop
body stays readable and so the row mapping can be unit-tested without a network
fetch. Keep it within `savills.ts`.

### 6.3 Test: `tests/ts/sources/savills-commercial.test.ts`

NEW file (do NOT edit the existing `savills.test.ts`). Use `node:test` +
`node:assert/strict`, import `.js` paths, mirror the existing savills test.
Cover PURE helpers only (no network):
- `savillsSaleCardIsCommercial`:
  - returns TRUE for an office/retail/industrial/commercial-keyword card or a
    `/commercial/` href.
  - returns FALSE for a residential card (house/apartment/bedroom keywords) and
    for a card with NO commercial signal (default-deny).
- If you extracted `mapSavillsLeaseRow`, export it and test it maps a synthetic
  `__NEXT_DATA__` row object (one with `IsCommercial: true`, AddressLine1/2, a US
  location string) to the expected listing fields (`transactionType: "Lease"`,
  city/state parsed, `leaseRateText`, url built from `ExternalPropertyID`), and
  returns null for a non-US row. Keep the synthetic input inline.
- Optionally assert `savillsTotalItems` drives pagination intent by testing it
  returns the embedded `totalItems` (this is a pure function already; a light
  assertion is fine but not required since `savills.test.ts` may already cover
  related helpers; do not duplicate an existing assertion).

### 6.4 Owner D verification

```bash
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
npm run test:unit   # includes the new savills-commercial.test.ts
```
The async `srcSavills` / `srcSavillsCommercialLease` themselves stay E2E-only
(network); only the extracted pure helpers are unit-tested. Confirm typecheck
passes and the new test file runs green.

---

## 7. OWNER E: `cre_status.sh` + the two plist templates

### 7.1 L4b: bake `CRE_STATUS_FLIP_MAX_FRACTION=0.30` into the plist TEMPLATES

**Location:** `launchd/ai.agentic.cre-daily.plist.template` and
`launchd/ai.agentic.cre-weekly.plist.template`, the `EnvironmentVariables` dict
(after the `PATH` key, before `__ENV_EXTRA__`).

**Required:** add the env var to BOTH templates' `EnvironmentVariables` dict:
```xml
        <key>PATH</key>
        <string>__BIN_PATH__</string>
        <key>CRE_STATUS_FLIP_MAX_FRACTION</key>
        <string>0.30</string>__ENV_EXTRA__
```
- Place the new `<key>/<string>` pair BETWEEN the existing PATH `<string>` line
  and the `__ENV_EXTRA__` token, so `install_launchd.sh`'s `__ENV_EXTRA__`
  substitution (which appends an optional `CRE_ENV_FILE` block) still renders
  valid plist XML. `install_launchd.sh` injects `env_extra` as a newline +
  `<key>CRE_ENV_FILE</key>...`; appending after our new `<string>` keeps the
  ordering valid. Verify by reading `install_launchd.sh` (the `__ENV_EXTRA__`
  replacement is a string substitution; placing it immediately after the new
  `</string>` is correct, exactly as PATH does today).
- Add a short XML comment above the new key in BOTH templates explaining: the
  flip-rate circuit breaker is conservative (0.30) and INERT until status
  activation is turned on; it only ever rolls back a bad run. No em dashes.
- This is INERT today: `cre_ingest.py`'s `_flip_circuit_breaker()` only acts when
  status activation writes non-active statuses, which is default-off. The env var
  simply pre-stages a safe ceiling for when activation is enabled.
- Do NOT add it to the monitor plist template (the monitor does not run
  `cre_ingest.py` and never flips status). Daily and weekly only.

### 7.2 H3: per-source signal-staleness check (read-only)

**Location:** `cre_status.sh`. Add a NEW read-only section that surfaces, for the
disappearance-only sources (CBRE, NAI, Avison, Marcus), the time since each was
last enumerated vs an expected cadence, and WARNs when stale.

**Constraint:** `cre_status.sh` is strictly read-only and secret-free. It does
NOT connect to the DB and NEVER prints `POSTGRES_URL`. The script's existing
heartbeat reads only local artifacts. `last_enumerated_at` lives in
`cre_source_index` (a DB table), which the script cannot read without a DB
connection. RESOLUTION (pick the read-only, offline-first design):

- The signal-staleness check reads the LOCAL monitor artifacts under
  `out/monitor/monitor_*.json` (already produced by the monitor tier and already
  referenced by `newest_artifact monitor`). The monitor artifact's per-source
  summary (`summary.by_source[<sk>].enumerated_flat` / `grouped`) and the file
  mtime give a local, no-DB signal of when each disappearance-only source was
  last enumerated. Implement the check as: for each of the four sources, find the
  newest `out/monitor/monitor_*.json` whose `by_source` includes that source key
  with `grouped > 0`, and compare its age to a per-source cadence threshold.
  - Cadence threshold: reuse the monitor staleness window. A disappearance-only
    source that has not appeared with nonzero `grouped` in any monitor artifact
    within, for example, 8 days (configurable constant in the script, e.g.
    `SIGNAL_STALE_SECS=$(( 8 * 86400 ))`) is WARNed as stale. Choose a
    conservative window longer than the weekly reconcile cadence so a normal
    weekly sweep does not trip it; 8 to 10 days is appropriate. Document the
    chosen value in a comment.
  - Parse the JSON with the existing primitives in the script
    (`marker_field`-style `grep -o`/`sed`, since the script deliberately avoids a
    JSON parser dependency). A light `grep` for the source key under
    `by_source` plus a presence check is acceptable; you do not need full JSON
    parsing. If exact per-source extraction via grep is brittle, the acceptable
    fallback is: WARN when there is NO monitor artifact at all newer than the
    threshold (a coarse but correct staleness signal for the disappearance-only
    set), and `note` the four source names so the operator knows which sources
    rely on the monitor sweep for their sold-signal.
- This is OBSERVE-ONLY and additive: it MUST NOT change the script's exit-code
  contract in a way that breaks existing behavior beyond incrementing `PROBLEMS`
  via `warn` when a real staleness is detected (consistent with the rest of the
  script). A clean, fresh clone with no monitor artifacts should `note` (not
  `warn`) "no monitor artifacts yet" so a brand-new setup is not falsely flagged
  (mirror the existing `no run artifacts yet` handling).
- Add the section with a clear `section "disappearance-only signal staleness"`
  header and the four source names listed (cbre, nai-global, avison-young,
  marcus-millichap). Keep it read-only: no DB, no launchctl mutation.

**Why this is the right read-only design:** the spec asks to "surface
last_enumerated_at (from cre_source_index/baseline) vs expected cadence." Since
`cre_status.sh` is contractually no-DB, the local monitor artifact is the
read-only proxy for `last_enumerated_at` (it is the same enumeration that writes
`cre_source_index.last_enumerated_at`). Document this equivalence in a comment so
a future maintainer who adds a DB-backed check knows the intent. A DB-backed
exact `last_enumerated_at` read is explicitly DEFERRED (section 8) to avoid
giving `cre_status.sh` a DB dependency.

### 7.3 Owner E verification (offline)

```bash
cd scripts/firecrawl-ops/cre_collector
bash -n cre_status.sh
plutil -lint launchd/ai.agentic.cre-daily.plist.template 2>/dev/null || true   # template has tokens; lint may warn on __TOKENS__, that is expected
python3 -m pytest tests/test_shell_scripts_syntax.py -q   # the bash -n guard over every *.sh
```
- `bash -n cre_status.sh` MUST pass (the existing `test_shell_scripts_syntax.py`
  parametrizes over every `.sh`; do not break it).
- The plist templates contain `__TOKENS__` so `plutil -lint` on the raw template
  is not authoritative; the real validation is that `install_launchd.sh` renders
  valid XML. Owner E MUST NOT run `install_launchd.sh` against the live machine
  (no install). Instead, statically confirm the new `<key>/<string>` pair is
  well-formed XML and placed before `__ENV_EXTRA__`. Optionally, render a copy in
  a tmp dir by hand-substituting tokens and `plutil -lint` that, WITHOUT
  installing. Do not load anything.

---

## 8. DEFERRED items (do NOT build; documented with rationale)

These are explicitly OUT OF SCOPE for this delivery. State them in the return.

- **Per-rescrape child versioning / soft-delete + consumer filtering of child
  tables.** Forbidden by the live-volume constraint (images ~489k, contacts
  ~160k, docs ~70k; a full daily run re-upserts most rows). Archiving every
  replaced child each run bloats catastrophically. The bounded retirement-only
  snapshot (M2) is the durable slice we ship.
- **Image archival.** High volume (~489k), low historical value. Excluded from
  M2 by design.
- **raw_data archival / versioning.** Bloat; overwrite-in-place stays.
- **M4: sub-daily detection for the four detail-id sources** (jll, jll-investor,
  cbre-dealflow, colliers SalesTracker). Large; needs URL-keyed reconciliation in
  `cre_monitor.py`. Deferred.
- **M6/L6: per-source source_lastmod trust verification / price_change noise.**
  Partially addressed by H4b's real `old_value`; deeper per-source lastmod
  verification deferred.
- **Reconsidering the flip-breaker 200-row `CRE_STATUS_FLIP_MIN_BASE` exemption.**
  L4a widens the metric only; the exemption threshold is unchanged this delivery.
- **DB-backed exact `last_enumerated_at` read in a staleness checker.** Deferred
  to keep `cre_status.sh` no-DB; H3 uses the local monitor-artifact proxy.
- **GATED operational go-lives (NOT code, do not action):** weekly
  `--mark-missing` tier load (#37), first live reconcile (#39), status-activation
  go-live (`--activate-status`), consumer board-gate deploy (#36), and the TCC /
  Full Disk Access fix (#41). These require explicit go-ahead and are out of
  scope for all owners.
- **R1 doc nit** (`START_HERE.md:276-277` "un-probed" wording). Trivial doc fix,
  not owned by any of the 5 code owners here; flag for a docs pass.

---

## 9. Cross-owner notes (integration; read before merging)

- **Known stale assertion (M5).** Owner A's M5 change to the revival CASE makes
  `test_ingest_status_activation.py::test_update_keeps_status_sticky_resetting_only_resurrected`
  stale (it asserts the OLD exact CASE string). That file is owned by neither A
  (it is an existing test, A only adds NEW files) nor any other owner. The
  integration owner MUST update that one assertion to the new CASE string after
  merge. This is the only existing test that any owner's change breaks. All other
  existing tests must stay green.
- **`build_sql` signature change.** Owner A adds a `history_guard` keyword
  parameter (default True). It has a default, so existing callers
  (`test_ingest_status_activation.py::_sql` calls `build_sql([], [], _SCRAPED_AT,
  set())`) still work unchanged. Do NOT make it positional or required.
- **`cre_monitor.py` imports from `cre_ingest`.** Owner C does not need any new
  import; the new monitor logic is self-contained in `cre_monitor.py`. Owner A's
  `build_sql` changes do not affect the symbols Owner C imports
  (`to_row`, `merge_rows`, etc. are untouched in signature).
- **Event-type CHECK (007).** M3 reuses the existing `disappeared` type. No 007
  CHECK change. Owner B does NOT alter the `cre_listing_events` CHECK.
- **`source_value` convention.** Ingest-emitted disappearance uses
  `source_value = 'mark_missing'`; the monitor uses `'enumeration_gone'`. Both are
  free-text and need no schema change. This lets an operator distinguish the two
  retirement paths in `v_cre_recent_changes`.
- **Apply order.** Owner B's 009 must be applied to prod BEFORE Owner A's guarded
  INSERTs do anything (they are no-ops until then) and BEFORE Owner C's monitor
  apply tier reads the new columns. All apply is GATED and out of scope; the code
  is written so a pre-apply prod is safe.

---

## 10. Per-owner deliverable checklist

- **A:** `cre_ingest.py` (M1 count-aware coverage; L1 price COALESCE; M5 revival
  guard; M3 disappeared event + `_retired` temp; H4a `_prior_vals` + guarded
  price-history INSERT + `history_guard` param; M2 guarded contacts/documents
  archive INSERTs; L4a widened flip metric) + 6 new test files. Run the 6 new
  tests + `py_compile`. Flag the known M5 stale assertion.
- **B:** `sql/009_cre_history_retention.sql` (price-history table; 3 source_index
  columns; 2 archive tables; retention trigger + function + GUC + partial index;
  all idempotent, RLS-on, no em dashes) + `000_run_all.sql` registration (009
  after 008, before 006). Inspect-only verification.
- **C:** `cre_monitor.py` (load 3 prior columns via `.get`; populate `old_value`
  on `price_change`; stage + upsert current price into `prior_*` columns;
  preserve observe-only invariant) + `tests/test_monitor_old_value.py`. Run new
  test + existing monitor tests green + `py_compile`.
- **D:** `sources/savills.ts` (L5 `savillsSaleCardIsCommercial` default-deny
  guard applied in sale loop; L3 paginated commercial-lease with `truncated`
  flag; optional `mapSavillsLeaseRow` helper) +
  `tests/ts/sources/savills-commercial.test.ts`. `npm run typecheck` +
  `npm run test:unit` green.
- **E:** both daily + weekly plist templates (`CRE_STATUS_FLIP_MAX_FRACTION=0.30`
  before `__ENV_EXTRA__`) + `cre_status.sh` (read-only disappearance-only
  signal-staleness section, no DB). `bash -n` + `test_shell_scripts_syntax.py`
  green; do not install/load.
