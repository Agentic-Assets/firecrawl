# CRE Listings Supabase Advisor Execution Readiness

## Peer review (2026-06-13)

Supabase peer review against live advisor exports and repo migrations.

| Check | Result |
| --- | --- |
| Lint counts (`credeals.cre_*` only) | **Verified:** security 11 (`rls_enabled_no_policy`) + performance 16 (2 `unindexed_foreign_keys` + 14 `unused_index`) = **27** total. Re-counted from `/tmp/supabase-advisors-fhqycqubkkrdgzswccwd/{security,performance}.json`. |
| Migration filename | **Corrected:** canonical file is `008_cre_fk_indexes.sql` (not `008_cre_fk_covering_indexes.sql`). |
| Index names | **Corrected:** `cre_source_baseline_last_accepted_job_idx` (not `cre_source_baseline_last_job_idx`). Matches `008_cre_fk_indexes.sql`. |
| `000_run_all.sql` wiring | **Fixed:** added `\i 008_cre_fk_indexes.sql` after `007`, before `006`; dependency comment updated. |
| Go/no-go matrix | **Aligned** with sibling reports: FK indexes **GO**; RLS/unused-index **defer**; `handle_new_user` **defer** (EQUIRE-owned, `SET search_path = ''`). |
| Advisor re-fetch commands | **Confirmed:** use direct host `db.fhqycqubkkrdgzswccwd.supabase.co:5432`, not the pooler (`aws-0-...pooler.supabase.com`). Pooler is fine for `psql` migrations; advisor CLI should hit direct. |
| Sibling scope note | `2026-06-13-cre-unused-indexes.md` counts 17 `unused_index` rows including `public.cre_business_plan_runs` (2); this rollup scopes **collector `credeals.cre_*` only** (14). |

---

Generated: 2026-06-13  
Project: `supabase-agentic-assets-v2` (`fhqycqubkkrdgzswccwd`)  
Schema scope: **`credeals.cre_*` collector tables only** (excludes `deals.*`, `listings`, `listing_bovs`, EQUIRE app tables, backup tables)  
Master rollup: `agentic-assets-orbis/tasks/plans/2026-05-23-pdf-intelligence-rag-system/2026-06-13-supabase-advisors-prioritized-remediation-report.md`  
Sibling detail: `2026-06-13-cre-unindexed-foreign-keys.md`, `2026-06-13-cre-rls-enabled-no-policy.md`, `2026-06-13-cre-unused-indexes.md`, `2026-06-13-cre-functions-and-grants.md`

## Executive snapshot

| Metric | Value |
| --- | ---: |
| Security lints on `cre_*` tables | 11 |
| Performance lints on `cre_*` tables | 16 |
| **Total in scope** | **27** |
| ERROR-level | 0 |
| WARN-level (on `cre_*`) | 0 |
| INFO-level | 27 |

**Headline:** The collector-owned `cre_*` surface is clean. No ERROR or WARN lints touch `cre_*` tables. The only actionable DDL is **two missing FK covering indexes** on monitor tables added in `007_cre_change_tracking.sql`. Everything else is either **intentional private-schema RLS posture** or **premature `unused_index` noise** while monitor `--apply` and EQUIRE agent search paths are still ramping.

**Collector constraint:** `cre_ingest.py`, `cre_monitor.py`, and `cre_gate.py` use **service-role `psql`**, which bypasses RLS. Migrations must preserve that path and must not grant `anon`/`authenticated` table or view `SELECT` without an explicit product decision (`cre_collector/archive/SUPABASE_SECURITY_NOTE_2026-06-12.md`).

---

## 1. Complete inventory (`credeals.cre_*` only)

Parsed from `/tmp/supabase-advisors-fhqycqubkkrdgzswccwd/security.json` and `performance.json` (fetched 2026-06-13).

### Summary by lint type

| Lint type | Source | Level | Count | Priority | Action |
| --- | --- | --- | ---: | --- | --- |
| `rls_enabled_no_policy` | security | INFO | 11 | P3 (accepted) | **Document, do not fix** unless EQUIRE product adds public read policies |
| `unindexed_foreign_keys` | performance | INFO | 2 | **P0** | Add covering indexes in `008_cre_fk_indexes.sql` |
| `unused_index` | performance | INFO | 14 | P3 (defer) | **Do not drop** until monitor rollout + agent FTS traffic mature. Extended scope adds 2× `public.cre_business_plan_runs` (16 total); see `2026-06-13-cre-unused-indexes.md`. |

### Row-level inventory

| Lint type | Level | Table / object | Sub-object | Count | Priority | Action |
| --- | --- | --- | --- | ---: | --- | --- |
| `rls_enabled_no_policy` | INFO | `credeals.cre_brokerages` | — | 1 | P3 | Accepted private collector posture |
| `rls_enabled_no_policy` | INFO | `credeals.cre_listings` | — | 1 | P3 | Accepted |
| `rls_enabled_no_policy` | INFO | `credeals.cre_listing_contacts` | — | 1 | P3 | Accepted |
| `rls_enabled_no_policy` | INFO | `credeals.cre_listing_documents` | — | 1 | P3 | Accepted |
| `rls_enabled_no_policy` | INFO | `credeals.cre_listing_images` | — | 1 | P3 | Accepted |
| `rls_enabled_no_policy` | INFO | `credeals.cre_scrape_jobs` | — | 1 | P3 | Accepted |
| `rls_enabled_no_policy` | INFO | `credeals.cre_scrape_log` | — | 1 | P3 | Accepted |
| `rls_enabled_no_policy` | INFO | `credeals.cre_listing_events` | — | 1 | P3 | Accepted |
| `rls_enabled_no_policy` | INFO | `credeals.cre_source_index` | — | 1 | P3 | Accepted |
| `rls_enabled_no_policy` | INFO | `credeals.cre_enrichment_queue` | — | 1 | P3 | Accepted |
| `rls_enabled_no_policy` | INFO | `credeals.cre_source_baseline` | — | 1 | P3 | Accepted |
| `unindexed_foreign_keys` | INFO | `credeals.cre_listing_events` | FK `cre_listing_events_scrape_job_id_fkey` → `cre_scrape_jobs(id)` | 1 | **P0** | `CREATE INDEX cre_listing_events_scrape_job_idx ON (scrape_job_id)` |
| `unindexed_foreign_keys` | INFO | `credeals.cre_source_baseline` | FK `cre_source_baseline_last_accepted_job_id_fkey` → `cre_scrape_jobs(id)` | 1 | **P0** | `CREATE INDEX cre_source_baseline_last_accepted_job_idx ON (last_accepted_job_id)` |
| `unused_index` | INFO | `credeals.cre_scrape_jobs` | `cre_scrape_jobs_status_idx` | 1 | P3 | Keep (operator job status audits) |
| `unused_index` | INFO | `credeals.cre_scrape_log` | `cre_scrape_log_job_idx` | 1 | P3 | Keep |
| `unused_index` | INFO | `credeals.cre_scrape_log` | `cre_scrape_log_status_idx` | 1 | P3 | Keep |
| `unused_index` | INFO | `credeals.cre_scrape_log` | `cre_scrape_log_listing_idx` | 1 | P3 | Keep |
| `unused_index` | INFO | `credeals.cre_listings` | `cre_listings_cap_rate_idx` | 1 | P3 | Keep (EQUIRE mandate filters) |
| `unused_index` | INFO | `credeals.cre_listings` | `cre_listings_raw_data_gin_idx` | 1 | P3 | Keep (agent jsonb probes) |
| `unused_index` | INFO | `credeals.cre_listings` | `cre_listings_highlights_gin_idx` | 1 | P3 | Keep |
| `unused_index` | INFO | `credeals.cre_listings` | `cre_listings_fts_idx` | 1 | P3 | Keep (`search_cre_listings`) |
| `unused_index` | INFO | `credeals.cre_listings` | `cre_listings_last_seen_idx` | 1 | P3 | Keep (column unused; monitor writes `cre_source_index.last_seen` only — `cre_monitor.py` L650–652) |
| `unused_index` | INFO | `credeals.cre_listing_events` | `cre_listing_events_listing_idx` | 1 | P3 | Keep (`v_cre_recent_changes`) |
| `unused_index` | INFO | `credeals.cre_listing_events` | `cre_listing_events_type_idx` | 1 | P3 | Keep |
| `unused_index` | INFO | `credeals.cre_listing_events` | `cre_listing_events_brokerage_idx` | 1 | P3 | Keep |
| `unused_index` | INFO | `credeals.cre_source_index` | `cre_source_index_first_seen_idx` | 1 | P3 | Keep (enumeration analytics) |
| `unused_index` | INFO | `credeals.cre_enrichment_queue` | `cre_enrichment_queue_drain_idx` | 1 | P3 | Keep (Tier-B worker deferred) |

### Explicitly out of scope (same schema, not `cre_*`)

These appear in the security advisor JSON but are **not** collector listing tables:

| Object | Lint | Notes |
| --- | --- | --- |
| `credeals._beachwalk_rentroll_backup_20260610` | `rls_enabled_no_policy`, `no_primary_key` | One-off backup |
| `credeals._schema_bloat_backup` | `rls_enabled_no_policy`, `no_primary_key` | One-off backup |
| `credeals.access_requests`, `deleted_accounts`, `mcp_oauth_*`, `used_nonces` | `rls_enabled_no_policy` | EQUIRE app / auth |
| `credeals.handle_new_user` | `function_search_path_mutable` (WARN) | Auth trigger; see `2026-06-13-cre-functions-and-grants.md` |

No `auth_rls_initplan` or `multiple_permissive_policies` lints target `cre_*` tables in the current advisor export.

---

## 2. Recommended migration file order (`008_*.sql`)

Runner order: `001` → `002` → `003` → `004` → `007` → **`008`** → `006` → `005`.

| Order | File | Depends on | Contents |
| ---: | --- | --- | --- |
| 1 | `008_cre_fk_indexes.sql` | `003` (`cre_scrape_jobs`), `007` (`cre_listing_events`, `cre_source_baseline`) | Two `CREATE INDEX IF NOT EXISTS` statements for FK columns |
| 2 | `000_run_all.sql` | `008` | `\i 008_cre_fk_indexes.sql` after `007` block (wired 2026-06-13 peer review) |

### Canonical `008_cre_fk_indexes.sql`

```sql
-- 008_cre_fk_indexes.sql
-- Supabase advisor: unindexed_foreign_keys on monitor tables (2026-06-13).
-- Idempotent. Safe inside 000_run_all.sql transaction (tables are small today).

CREATE INDEX IF NOT EXISTS cre_listing_events_scrape_job_idx
    ON credeals.cre_listing_events (scrape_job_id);

CREATE INDEX IF NOT EXISTS cre_source_baseline_last_accepted_job_idx
    ON credeals.cre_source_baseline (last_accepted_job_id);
```

**Why not CONCURRENTLY:** Current event/baseline row counts are tiny. If `cre_listing_events` grows past ~1M rows before apply, run the two `CREATE INDEX CONCURRENTLY` statements outside `000_run_all.sql` during a maintenance window instead (see `2026-06-13-cre-unindexed-foreign-keys.md` §6.2).

### Adjacent (not `cre_*`, optional EQUIRE migration)

| Object | Lint | Approval |
| --- | --- | --- |
| `credeals.handle_new_user()` | `function_search_path_mutable` | Cayman (auth signup path); **EQUIRE repo**, not collector `008` |

```sql
-- EQUIRE-owned. Resolve signature: \df+ credeals.handle_new_user
ALTER FUNCTION credeals.handle_new_user()
  SET search_path = '';
```

---

## 3. Pre-apply checklist

### Backup

```bash
# Direct host (not pooler). Set from ~/.pgpass or secrets vault; never commit or print.
export DATABASE_URL='postgresql://postgres:<pwd>@db.fhqycqubkkrdgzswccwd.supabase.co:5432/postgres'
TS=$(date -u +%Y%m%dT%H%MZ)
pg_dump "$DATABASE_URL" \
  --schema=credeals \
  --table='credeals.cre_*' \
  --no-owner --no-privileges \
  -f "/tmp/credeals-cre-backup-${TS}.sql"
```

Minimum row-count snapshot before change:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "
SELECT 'cre_listings' AS t, count(*) FROM credeals.cre_listings
UNION ALL SELECT 'cre_listing_events', count(*) FROM credeals.cre_listing_events
UNION ALL SELECT 'cre_source_baseline', count(*) FROM credeals.cre_source_baseline;
"
```

### Dry-run (no live writes)

```bash
cd scripts/firecrawl-ops/cre_collector

# Typecheck + unit contracts (no DB)
npm run typecheck
python3 -m pytest tests/ -q

# Ingest SQL generation only
npx tsx collect.ts --source=svn --transaction=both --max-items=6 --out=/tmp/cre-probe.json
python3 cre_ingest.py --in /tmp/cre-probe.json --dry-run --keep-artifacts /tmp/cre-ingest-dryrun

# Monitor/gate SQL generation only (observe-only invariant)
python3 cre_monitor.py --help   # confirm CLI flags for your artifact path
python3 cre_gate.py --help
```

### Migration dry-run

```bash
cd scripts/firecrawl-ops/sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 008_cre_fk_indexes.sql
# Re-run is idempotent; if satisfied, no further action until formal apply window
```

### Collector smoke test (post-apply, service-role path)

```bash
cd scripts/firecrawl-ops/cre_collector

# 1) Small live collect + additive ingest
npx tsx collect.ts --source=svn --transaction=both --max-items=6 --out=/tmp/cre-smoke.json
python3 cre_ingest.py --in /tmp/cre-smoke.json

# 2) Read-only validation suite
npm run validate:supabase

# 3) Optional monitor dry-run on latest monitor artifact (never ingest monitor JSON)
# python3 cre_monitor.py --in out/monitor_*.json --dry-run
```

**Do not** run `cre_daily_update.sh --mark-missing` in the same window as schema apply unless Cayman has confirmed a clean all-source run (`START_HERE.md` gate).

---

## 4. Post-apply verification

### Advisor re-fetch

Use the **direct** Postgres host for `supabase db advisors` (not the transaction pooler). Password from EQUIRE `.env.local` or `~/.pgpass`; never log the URL.

```bash
source ~/Documents/GitHub/agentic-assets/dynamically-display-cre-listing-data/.env.local
DB_URL="postgresql://postgres:${SUPABASE_DATABASE_PASSWORD}@db.fhqycqubkkrdgzswccwd.supabase.co:5432/postgres"

supabase db advisors --db-url="$DB_URL" --type=security  --output-format=json --level=info \
  > /tmp/supabase-advisors-fhqycqubkkrdgzswccwd/security.json
supabase db advisors --db-url="$DB_URL" --type=performance --output-format=json --level=info \
  > /tmp/supabase-advisors-fhqycqubkkrdgzswccwd/performance.json
```

Re-parse CRE scope (grep):

```bash
rg 'credeals\.cre_' /tmp/supabase-advisors-fhqycqubkkrdgzswccwd/security.json
rg 'credeals\.cre_' /tmp/supabase-advisors-fhqycqubkkrdgzswccwd/performance.json
```

**Expected delta after `008`:** both `unindexed_foreign_keys` rows on `cre_listing_events` and `cre_source_baseline` cleared. `rls_enabled_no_policy` (11) and `unused_index` (14) unchanged. Total `cre_*` lints drop from **27** to **25**.

### SQL spot checks

```sql
-- New indexes present
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'credeals'
  AND tablename IN ('cre_listing_events', 'cre_source_baseline')
  AND indexname IN (
    'cre_listing_events_scrape_job_idx',
    'cre_source_baseline_last_accepted_job_idx'
  );

-- Collector grants unchanged (service_role still bypasses RLS)
SELECT relname, relrowsecurity
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'credeals' AND relname LIKE 'cre_%' AND relkind = 'r'
ORDER BY relname;

-- Display views still invoker-safe
SELECT viewname, reloptions
FROM pg_views v
JOIN pg_class c ON c.relname = v.viewname
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = v.schemaname
WHERE v.schemaname = 'credeals' AND v.viewname LIKE 'v_cre_%';

-- Function execute: service_role only on search helper
SELECT grantee, privilege_type
FROM information_schema.routine_privileges
WHERE routine_schema = 'credeals'
  AND routine_name IN ('search_cre_listings', 'update_cre_listing_timestamp');
```

### Functional smoke

```bash
cd scripts/firecrawl-ops/cre_collector
npm run validate:supabase
```

---

## 5. Go / no-go matrix

| Work item | Ship now? | Cayman approval? | Rationale |
| --- | --- | --- | --- |
| **`008_cre_fk_indexes.sql`** (2 indexes) | **GO** | Notify, not block | Low blast radius; fixes real FK gaps on monitor tables; no RLS/grant changes |
| Document 11× `rls_enabled_no_policy` as accepted | **GO** | No | Matches `SUPABASE_SECURITY_NOTE_2026-06-12.md` and `007` header |
| Drop any of 14× `unused_index` on `cre_*` | **NO-GO** | Yes if ever pursued | FTS/GIN/monitor indexes unused because traffic is immature; drops would harm EQUIRE search and monitor rollout |
| Add RLS policies on `cre_*` for `anon`/`authenticated` | **NO-GO** | **Yes** | Product/API decision; breaks private collector model if done casually |
| `ALTER FUNCTION credeals.handle_new_user() SET search_path = ''` | **DONE live + migration** | Approved in this session | EQUIRE auth signup trigger; durable migration in `CRE_EQUIRE/supabase/migrations/20260614023249_harden_handle_new_user_search_path.sql` |
| `auth_rls_initplan` / `multiple_permissive_policies` on non-`cre_*` `credeals` tables | Defer | Yes when refactoring EQUIRE app RLS | 200+ lints on `deals.*`, `listings`, etc.; out of collector scope |
| Drop `_schema_bloat_backup` / `_beachwalk_rentroll_backup_*` | Defer | **Yes** | Not `cre_*`; confirm backup retention first |

---

## References

| Doc | Role |
| --- | --- |
| `scripts/firecrawl-ops/sql/000_run_all.sql` | Migration runner (includes `008` after `007`) |
| `scripts/firecrawl-ops/sql/008_cre_fk_indexes.sql` | FK covering indexes |
| `scripts/firecrawl-ops/sql/007_cre_change_tracking.sql` | Source of the two unindexed FKs |
| `scripts/firecrawl-ops/cre_collector/archive/SUPABASE_SECURITY_NOTE_2026-06-12.md` | Accepted RLS posture |
| `scripts/firecrawl-ops/cre_collector/cre_ingest.py` | Service-role ingest path to preserve |
| `docs/firecrawl-ops/references/cre-monitor-subsystem.md` | Monitor table consumers |
| [Splinter 0001 unindexed_foreign_keys](https://supabase.com/docs/guides/database/database-linter?lint=0001_unindexed_foreign_keys) | FK index lint |
| [Splinter 0008 rls_enabled_no_policy](https://supabase.com/docs/guides/database/database-linter?lint=0008_rls_enabled_no_policy) | Accepted private-schema INFO |
| Master report §P3-1 | CRE RLS perf debt on non-`cre_*` tables |

---

## Bottom line (3 sentences)

**GO:** Apply `008_cre_fk_indexes.sql` (two FK covering indexes on `cre_listing_events` and `cre_source_baseline`), then re-run collector probe ingest and `npm run validate:supabase`. **NO-GO without Cayman:** adding public RLS policies, dropping any `cre_*` index, or changing auth trigger bodies. **Defer:** all 11 `rls_enabled_no_policy` and `unused_index` findings are expected noise for the private collector surface until monitor scale-up and EQUIRE agent search traffic justify revisiting.

---

## Live apply (2026-06-14)

**Status: P0 applied.** See `advisor-reports/2026-06-13-cre-remediation-apply-log.md` for timestamps, before/after index state, advisor re-fetch, and `validate:supabase` result. `unindexed_foreign_keys` on collector monitor tables cleared; `npm run validate:supabase` passed. Adjacent EQUIRE live hardening also applied: `credeals.handle_new_user()` now has `search_path=""`, the security advisor reports zero `function_search_path_mutable` warnings for that function, and durable migration `CRE_EQUIRE/supabase/migrations/20260614023249_harden_handle_new_user_search_path.sql` was created.
