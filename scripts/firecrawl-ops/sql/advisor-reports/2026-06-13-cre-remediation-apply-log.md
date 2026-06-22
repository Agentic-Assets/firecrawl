# CRE Advisor Remediation — Live Apply Log

**Project:** `supabase-agentic-assets-v2` (`fhqycqubkkrdgzswccwd`)  
**Started:** 2026-06-13  
**Scope:** Collector `credeals.cre_*` advisor remediation (highest priority first)  
**Plan:** `2026-06-13-cre-execution-readiness.md`

## Quick index

| Step | Priority | Action | Status |
| --- | --- | --- | --- |
| 1 | **P0** | Apply `008_cre_fk_indexes.sql` (2 FK covering indexes) | **done** |
| 2 | P3 | Accept 11× `rls_enabled_no_policy` (no DDL) | _documented_ |
| 3 | P3 | Defer `unused_index` on `cre_*` (no drops; includes 2 new FK indexes) | _documented_ |
| 4 | EQUIRE | `credeals.handle_new_user` search_path (EQUIRE-owned) | **live fixed, durable migration created** |

## Execution entries

_Entries appended below as work completes._

### 2026-06-14 02:29:17Z — Step 1: `008_cre_fk_indexes.sql`

- **Env file:** `/Users/caymanseagraves/Documents/GitHub/agentic-assets/dynamically-display-cre-listing-data/.env.local` (credentials not logged)
- **Row counts:** `cre_listing_events:0; cre_source_baseline:11`
- **Indexes before:** `none`
- **Missing before apply:** `['cre_listing_events_scrape_job_idx', 'cre_source_baseline_last_accepted_job_idx']`
- **Apply result:** migration applied
- **Indexes after:**
```
cre_listing_events_scrape_job_idx|CREATE INDEX cre_listing_events_scrape_job_idx ON credeals.cre_listing_events USING btree (scrape_job_id)
cre_source_baseline_last_accepted_job_idx|CREATE INDEX cre_source_baseline_last_accepted_job_idx ON credeals.cre_source_baseline USING btree (last_accepted_job_id)
```


### 2026-06-14 02:30:07Z — Step 1 verification: advisors + collector smoke

- **Advisor exports:** `/tmp/supabase-advisors-fhqycqubkkrdgzswccwd/security.json`, `/tmp/supabase-advisors-fhqycqubkkrdgzswccwd/performance.json`
- **`credeals.cre_*` scope (schema=`credeals`, table `cre_*` only):**
  - Security: **11** (`rls_enabled_no_policy` on all 11 collector tables; accepted, no DDL)
  - Performance: **15** (`unused_index` only; **0** `unindexed_foreign_keys`)
  - **Total:** **26** (was **27** before 008; delta = 2 FK lints cleared, +2 new `unused_index` on FK indexes, `cre_source_index_first_seen_idx` no longer flagged)
- **P0 success criterion:** `unindexed_foreign_keys` on `cre_listing_events` / `cre_source_baseline` = **0** ✓
- **Collector validation:** `npm run validate:supabase` in `cre_collector/` exited **0** (search smoke, child orphans, bad URLs all clean)

### Out of scope (not changed)

- `public.cre_business_plan_runs`: `auth_rls_initplan` + 2× `unused_index` (EQUIRE legacy; not collector `credeals.cre_*`)
- Durable EQUIRE migration for `credeals.handle_new_user`: created in `CRE_EQUIRE/supabase/migrations/20260614023249_harden_handle_new_user_search_path.sql`

## Bottom line

**P0 complete.** Only actionable collector DDL (`008_cre_fk_indexes.sql`) is applied on live Supabase. The adjacent EQUIRE `handle_new_user` live WARN is also fixed; RLS and unused-index findings remain accepted/deferred per `2026-06-13-cre-execution-readiness.md`.


### 2026-06-14 02:32:05Z - EQUIRE function hardening: `credeals.handle_new_user()`

- **Env file:** `/Users/caymanseagraves/Documents/GitHub/agentic-assets/dynamically-display-cre-listing-data/.env.local` (credentials not logged)
- **Action:** `ALTER FUNCTION credeals.handle_new_user() SET search_path TO '';`
- **Before:** `credeals.handle_new_user()|true|`
- **After:** `credeals.handle_new_user()|true|search_path=""`
- **Security advisor:** `function_search_path_mutable` for `credeals.handle_new_user` = **0**
- **Durable follow-up:** created `CRE_EQUIRE/supabase/migrations/20260614023249_harden_handle_new_user_search_path.sql` so future schema rebuilds keep `SET search_path = ''`.


### 2026-06-14 02:36:13Z - Safe remaining collector posture checks

- **Env file:** `/Users/caymanseagraves/Documents/GitHub/agentic-assets/dynamically-display-cre-listing-data/.env.local` (credentials not logged)
- **`v_cre_*` security invoker:** all already set
- **Function configs:**
```
handle_new_user()|true|search_path=""
search_cre_listings(query text, p_city text, p_state text, p_type text, p_transaction text)|false|search_path=""
update_cre_listing_timestamp()|false|search_path=""
```
- **Listing helper grants:**
```
search_cre_listings|postgres|EXECUTE
search_cre_listings|service_role|EXECUTE
update_cre_listing_timestamp|postgres|EXECUTE
update_cre_listing_timestamp|service_role|EXECUTE
```
- **Advisor WARN spot checks:** `security_definer_view` for `v_cre_` = **0**; `function_search_path_mutable` for `credeals.handle_new_user` = **0**
- **Remaining action:** do not add RLS policies, drop unused indexes, or alter/drop backup tables without a product or retention decision.

## Remaining decision queue

No further low-risk CRE collector remediation remains after the live fixes above.

| Area | Current state | Recommendation |
| --- | --- | --- |
| `credeals.cre_*` RLS INFO | 11× `rls_enabled_no_policy`; private schema, service-role-only reads/writes | Accept and leave unchanged until EQUIRE explicitly needs public/role-scoped access |
| `credeals.cre_*` unused indexes | Expected while monitor/app search traffic is immature; includes the two new FK indexes until used | Do not drop; revisit after 60-90 days of production traffic |
| `public.cre_business_plan_runs` | Non-collector legacy table with advisor lints | Handle in EQUIRE app pass, not CRE collector pass |
| `_schema_bloat_backup`, `_beachwalk_rentroll_backup_20260610` | Backup tables, not collector migrations | Retention decision required before drop or PK-wrap |
| Extension-in-public WARNs | Platform-wide Supabase hygiene (`postgis`, `pg_trgm`, etc.) | Defer to separate shared-project migration plan |

