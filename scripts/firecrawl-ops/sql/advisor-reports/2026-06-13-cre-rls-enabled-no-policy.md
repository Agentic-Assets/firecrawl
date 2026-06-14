# CRE Collector `rls_enabled_no_policy` Advisor Report

**Date:** 2026-06-13  
**Project:** `supabase-agentic-assets-v2` (`fhqycqubkkrdgzswccwd`)  
**Schema:** `credeals`  
**Lint:** Splinter `0008` / advisor name `rls_enabled_no_policy` (INFO, SECURITY)  
**Scope:** Eleven collector-owned `cre_*` base tables (not views, not unrelated `credeals` app tables)

## Executive summary

All eleven collector-owned `credeals.cre_*` tables trigger the Supabase security advisor INFO lint **`rls_enabled_no_policy`**. This is **expected and acceptable** for the current architecture: RLS is enabled as defense-in-depth, `anon` and `authenticated` have no table-level `SELECT`/`INSERT`/`UPDATE`/`DELETE` grants on this surface, all five `v_cre_*` views use `security_invoker = true`, and the collector ingests via a **service-role / direct Postgres** path that **bypasses RLS**.

**Recommended action: ACCEPT (document, no migration required).** Do not add `service_role` RLS policies (Supabase documents that `service_role` never evaluates policies). Do not grant `anon`/`authenticated` read policies to silence the linter. If advisor hygiene is desired later, an **optional** migration can add explicit `USING (false)` rejection policies for `anon` and `authenticated` only (see Appendix B); that changes documentation and advisor counts, not effective access.

Parent remediation report (external: `agentic-assets-orbis/tasks/plans/2026-05-23-pdf-intelligence-rag-system/2026-06-13-supabase-advisors-prioritized-remediation-report.md`, not in this repo) already classifies project-wide `rls_enabled_no_policy` as intentional deny-by-default on private staging surfaces and places broader `credeals` RLS performance work in **P3 backlog**.

---

## Lint inventory (collector `cre_*` tables)

Source: `/tmp/supabase-advisors-fhqycqubkkrdgzswccwd/security.json` (fetched 2026-06-13).

| # | Table | Level | Category | Detail | Cache key | Remediation URL |
|---|-------|-------|----------|--------|-----------|-----------------|
| 1 | `credeals.cre_brokerages` | INFO | SECURITY | Table `credeals.cre_brokerages` has RLS enabled, but no policies exist | `rls_enabled_no_policy_credeals_cre_brokerages` | [0008](https://supabase.com/docs/guides/database/database-linter?lint=0008_rls_enabled_no_policy) |
| 2 | `credeals.cre_listings` | INFO | SECURITY | Table `credeals.cre_listings` has RLS enabled, but no policies exist | `rls_enabled_no_policy_credeals_cre_listings` | same |
| 3 | `credeals.cre_listing_contacts` | INFO | SECURITY | Table `credeals.cre_listing_contacts` has RLS enabled, but no policies exist | `rls_enabled_no_policy_credeals_cre_listing_contacts` | same |
| 4 | `credeals.cre_listing_documents` | INFO | SECURITY | Table `credeals.cre_listing_documents` has RLS enabled, but no policies exist | `rls_enabled_no_policy_credeals_cre_listing_documents` | same |
| 5 | `credeals.cre_listing_images` | INFO | SECURITY | Table `credeals.cre_listing_images` has RLS enabled, but no policies exist | `rls_enabled_no_policy_credeals_cre_listing_images` | same |
| 6 | `credeals.cre_scrape_jobs` | INFO | SECURITY | Table `credeals.cre_scrape_jobs` has RLS enabled, but no policies exist | `rls_enabled_no_policy_credeals_cre_scrape_jobs` | same |
| 7 | `credeals.cre_scrape_log` | INFO | SECURITY | Table `credeals.cre_scrape_log` has RLS enabled, but no policies exist | `rls_enabled_no_policy_credeals_cre_scrape_log` | same |
| 8 | `credeals.cre_listing_events` | INFO | SECURITY | Table `credeals.cre_listing_events` has RLS enabled, but no policies exist | `rls_enabled_no_policy_credeals_cre_listing_events` | same |
| 9 | `credeals.cre_source_index` | INFO | SECURITY | Table `credeals.cre_source_index` has RLS enabled, but no policies exist | `rls_enabled_no_policy_credeals_cre_source_index` | same |
| 10 | `credeals.cre_enrichment_queue` | INFO | SECURITY | Table `credeals.cre_enrichment_queue` has RLS enabled, but no policies exist | `rls_enabled_no_policy_credeals_cre_enrichment_queue` | same |
| 11 | `credeals.cre_source_baseline` | INFO | SECURITY | Table `credeals.cre_source_baseline` has RLS enabled, but no policies exist | `rls_enabled_no_policy_credeals_cre_source_baseline` | same |

**Shared lint metadata (all eleven rows):**

| Field | Value |
|-------|-------|
| `name` | `rls_enabled_no_policy` |
| `title` | RLS Enabled No Policy |
| `level` | INFO |
| `facing` | EXTERNAL |
| `description` | Detects cases where row level security (RLS) has been enabled on a table but no RLS policies have been created. |

**Out of scope for this report:** Other `credeals` tables in the same JSON (`access_requests`, backup tables, MCP OAuth tables, etc.) and non-`credeals` schemas (`ai`, `public` CamelCase app tables).

---

## Current security posture (repo + live design)

### Documented intent

| Source | Posture |
|--------|---------|
| `cre_collector/archive/SUPABASE_SECURITY_NOTE_2026-06-12.md` | RLS enabled on collector base tables; no public row policies for `anon`/`authenticated`; collector uses service-role Postgres; advisor INFO notices are expected. |
| `sql/007_cre_change_tracking.sql` header | Monitor tables mirror collector posture: RLS on, no public policies, service-role bypass, do not grant `anon`/`authenticated`. |
| `sql/CLAUDE.md` | `cre_*` tables and `v_cre_*` views are service-role only; RLS enabled with no public row policies by design. |
| `sql/005_cre_views.sql` | Five views use `security_invoker = true` (four display views plus `v_cre_recent_changes`); `search_cre_listings` / `update_cre_listing_timestamp` revoked from `PUBLIC`, `anon`, `authenticated`; granted to `service_role` only. |

### How access actually works

```text
Client (anon / authenticated JWT)
  -> PostgREST / Supabase JS
  -> credeals schema USAGE may exist (schema visible in API) but no table/view SELECT
     or DML grants on credeals.cre_* / v_cre_*  (privilege layer)
  -> Even if table grants were mis-added: RLS on + zero permissive policies =>
     zero visible rows for subject roles (RLS layer)
  -> Views with security_invoker=true evaluate RLS as the invoker (anon/authenticated),
     not as the view owner

Collector (cre_ingest.py / cre_monitor.py via psql + service role connection)
  -> postgres / service_role (bypasses RLS; policies are not evaluated)
  -> Full read/write on credeals.cre_* as today
```

`search_cre_listings` and `update_cre_listing_timestamp` are **not** `SECURITY DEFINER`; they run as the invoker with `SET search_path = ''`. Do not add `auth.role() = 'service_role'` policies as a collector gate; use server-side service-role clients and privilege revokes instead.

### Migration gap (documentation only)

`001`–`003` migrations in this repo **create** core listing tables but do not yet codify `ENABLE ROW LEVEL SECURITY` or `REVOKE` grants in SQL. Live DB already has RLS enabled (advisor confirms). `007` explicitly enables RLS on monitor tables. A future hygiene migration could align repo SQL with live posture; that is separate from whether the INFO lint is a security defect.

---

## Supabase / Splinter guidance

| Source | Relevant finding |
|--------|------------------|
| [Splinter 0008](https://supabase.github.io/splinter/0008_rls_enabled_no_policy/) | RLS with no policies means **no rows** via Supabase APIs. Intentional lock-down is valid; Splinter suggests an explicit rejection policy (`USING (false)`) when intent is deliberate. |
| [Supabase RLS docs](https://supabase.com/docs/guides/database/postgres/row-level-security) | Enable RLS on exposed schemas; without policies, access is denied until policies exist. Service keys bypass RLS for admin/server tasks. |
| [Service role troubleshooting](https://supabase.com/docs/guides/troubleshooting/why-is-my-service-role-key-client-getting-rls-errors-or-not-returning-data-7_1K9z) | **`service_role` never runs RLS policies.** Adding `TO service_role` policies does not change collector access and does not document bypass behavior. |

**Conclusion:** Deny-by-default RLS with no policies plus revoked API role grants is a **valid private-table pattern**. The INFO lint is advisory noise, not evidence of public data exposure.

---

## Options evaluated

| Option | Verdict | Rationale |
|--------|---------|-----------|
| **A. Accept / document** | **Recommended** | Matches 2026-06-12 security follow-up, sidecar staging pattern in parent remediation report, and Splinter’s “intentional restrict” case. Zero blast radius. |
| **B. `service_role`-only policies** | **Reject** | Supabase: service role bypasses RLS; policies never execute. Would not clarify security and might mislead future readers into thinking RLS gates the collector. |
| **C. Explicit DENY policies (`USING (false)`) for `anon`/`authenticated`** | **Optional / defer** | Silences advisor; encodes intent in DDL. No effective access change if grants stay revoked. Small policy-evaluation cost only if grants are mis-added later. |
| **D. Disable RLS** | **Reject** | Would remove defense-in-depth and likely **increase** risk if table grants are ever widened. |

---

## Recommended action

| Item | Decision |
|------|----------|
| Security remediation | **None required** |
| Advisor lint | **Accept** (INFO, expected for private collector surface) |
| SQL migration `008_*` | **No change** at this time |
| Docs | This report + existing `SUPABASE_SECURITY_NOTE_2026-06-12.md` |

**When to revisit:** Before any product decision to expose listing data to `authenticated` users via PostgREST (EQUIRE board, public API, or browser-direct Supabase client). At that point add **scoped allow** policies (or a server-side API), not blanket `service_role` policies.

---

## Blast radius

| Action | Collector ingest/monitor | Display app (service role server) | `anon` / `authenticated` API |
|--------|--------------------------|-----------------------------------|------------------------------|
| **Accept (no change)** | None | None | None (already blocked) |
| **Optional Appendix B deny policies** | None | None | Still blocked (deny policy + no grants) |
| **service_role policies (not recommended)** | None | None | N/A |
| **Accidental `GRANT SELECT` to `authenticated` without policies** | None | Could expose rows via views | **Mitigated today by RLS deny-default** |

---

## Verification queries

Run against project `fhqycqubkkrdgzswccwd` (service-role or `psql` only; do not paste connection strings into logs).

### 1. RLS enabled on all eleven tables

```sql
SELECT c.relname AS table_name, c.relrowsecurity AS rls_enabled, c.relforcerowsecurity AS rls_forced
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'credeals'
  AND c.relname IN (
    'cre_brokerages', 'cre_listings', 'cre_listing_contacts', 'cre_listing_documents',
    'cre_listing_images', 'cre_scrape_jobs', 'cre_scrape_log', 'cre_listing_events',
    'cre_source_index', 'cre_enrichment_queue', 'cre_source_baseline'
  )
ORDER BY 1;
```

Expected: `rls_enabled = true` for all rows.

### 2. Policy count (zero today; zero or eleven after optional migration)

```sql
SELECT tablename, count(*) AS policy_count
FROM pg_policies
WHERE schemaname = 'credeals'
  AND tablename IN (
    'cre_brokerages', 'cre_listings', 'cre_listing_contacts', 'cre_listing_documents',
    'cre_listing_images', 'cre_scrape_jobs', 'cre_scrape_log', 'cre_listing_events',
    'cre_source_index', 'cre_enrichment_queue', 'cre_source_baseline'
  )
GROUP BY tablename
ORDER BY 1;
```

Expected today: **no rows** (zero policies per table).

### 3. Privileges for API roles (should be empty or minimal)

```sql
SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE table_schema = 'credeals'
  AND table_name IN (
    'cre_brokerages', 'cre_listings', 'cre_listing_contacts', 'cre_listing_documents',
    'cre_listing_images', 'cre_scrape_jobs', 'cre_scrape_log', 'cre_listing_events',
    'cre_source_index', 'cre_enrichment_queue', 'cre_source_baseline'
  )
  AND grantee IN ('anon', 'authenticated', 'public')
ORDER BY table_name, grantee, privilege_type;
```

Expected: **no privileges** (or only `USAGE` on schema without table DML).

### 4. View invoker security (display + operator paths)

```sql
SELECT c.relname, c.reloptions
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'credeals'
  AND c.relname IN (
    'v_cre_listings_full', 'v_cre_active_for_sale', 'v_cre_active_for_lease',
    'v_cre_market_summary', 'v_cre_recent_changes'
  );
```

Expected: `security_invoker=true` in `reloptions` for all five views (`005_cre_views.sql`).

### 5. Re-fetch security advisors (after any optional migration)

```bash
supabase db advisors --db-url="$DATABASE_URL" --type=security --output-format=json --level=info \
  > /tmp/supabase-advisors-fhqycqubkkrdgzswccwd/security.json
```

Expected after **accept**: eleven `rls_enabled_no_policy` rows remain.  
Expected after **Appendix B**: those eleven rows absent from advisor output.

---

## SQL migration decision

### Primary: no new migration

No file `008_cre_rls_service_role_policies.sql` is recommended. Service-role policies would not execute under Supabase’s bypass semantics and would not remediate the underlying design goal (private collector tables).

### Appendix A — Not applied: `008_cre_rls_service_role_policies.sql`

**Status:** Not created. See options table (reject service_role policies).

### Appendix B — Optional deferred: explicit API rejection policies

Apply only if the team wants advisor silence and DDL-expressed deny intent without changing access. Save as `008_cre_rls_api_rejection_policies.sql` and add to `000_run_all.sql` after `007` if adopted.

```sql
-- =============================================================================
-- 008_cre_rls_api_rejection_policies.sql (OPTIONAL — advisor hygiene only)
-- Explicit deny policies for Supabase API roles on collector-owned cre_* tables.
-- Silences Splinter 0008 rls_enabled_no_policy INFO lints; does not change
-- effective access while anon/authenticated lack table grants.
-- service_role / postgres collector paths bypass RLS and are unaffected.
-- Do NOT scope policies TO service_role (policies are never evaluated for it).
-- Do NOT use auth.role() predicates here; plain USING (false) is sufficient.
-- =============================================================================

DO $$
DECLARE
  t text;
  tables text[] := ARRAY[
    'cre_brokerages', 'cre_listings', 'cre_listing_contacts', 'cre_listing_documents',
    'cre_listing_images', 'cre_scrape_jobs', 'cre_scrape_log', 'cre_listing_events',
    'cre_source_index', 'cre_enrichment_queue', 'cre_source_baseline'
  ];
  pol_name text;
BEGIN
  FOREACH t IN ARRAY tables LOOP
    pol_name := 'cre_collector_api_deny_' || t;
    IF NOT EXISTS (
      SELECT 1 FROM pg_policies
      WHERE schemaname = 'credeals' AND tablename = t AND policyname = pol_name
    ) THEN
      EXECUTE format(
        'CREATE POLICY %I ON credeals.%I AS PERMISSIVE FOR ALL TO anon, authenticated USING (false) WITH CHECK (false)',
        pol_name, t
      );
      EXECUTE format(
        'COMMENT ON POLICY %I ON credeals.%I IS %L',
        pol_name, t,
        'Intentional deny for PostgREST API roles; collector uses service-role/direct Postgres.'
      );
    END IF;
  END LOOP;
END $$;
```

**Rollback (if ever needed):**

```sql
DO $$
DECLARE
  t text;
  tables text[] := ARRAY[
    'cre_brokerages', 'cre_listings', 'cre_listing_contacts', 'cre_listing_documents',
    'cre_listing_images', 'cre_scrape_jobs', 'cre_scrape_log', 'cre_listing_events',
    'cre_source_index', 'cre_enrichment_queue', 'cre_source_baseline'
  ];
BEGIN
  FOREACH t IN ARRAY tables LOOP
    EXECUTE format('DROP POLICY IF EXISTS %I ON credeals.%I', 'cre_collector_api_deny_' || t, t);
  END LOOP;
END $$;
```

---

## References

- Security advisor export: `/tmp/supabase-advisors-fhqycqubkkrdgzswccwd/security.json`
- Parent triage (external repo): `agentic-assets-orbis/tasks/plans/2026-05-23-pdf-intelligence-rag-system/2026-06-13-supabase-advisors-prioritized-remediation-report.md`
- CRE security note: `../../cre_collector/archive/SUPABASE_SECURITY_NOTE_2026-06-12.md`
- SQL migrations: `../` (`001`–`007`)

---

## Peer review (2026-06-13)

Independent verification against the Supabase skill security checklist, live advisor export, and repo migrations.

### Checklist crosswalk

| Check | Result | Evidence |
|-------|--------|----------|
| RLS on + zero policies denies API roles | **Pass** | Postgres default: no permissive policy ⇒ no rows for roles subject to RLS. Matches Splinter 0008 and Supabase RLS docs cited above. |
| `service_role` bypasses RLS; do not add `service_role` policies | **Pass** | Option B rejection is correct. Collector path (`cre_ingest.py`, `cre_monitor.py`) uses service-role Postgres; policies would not execute. |
| Views use `security_invoker` | **Pass** | `005_cre_views.sql` sets `security_invoker = true` on all five `v_cre_*` views (four display + `v_cre_recent_changes`). None use `SECURITY DEFINER`. |
| Schema exposure vs table grants | **Pass** | `anon`/`authenticated` may hold `credeals` schema `USAGE` (schema listable) but lack table/view DML/SELECT on collector objects per security note and verification query 3. PostgREST needs both privilege and permissive RLS. |
| No unsafe `SECURITY DEFINER` / `auth.role()` advice in this report | **Pass** | Collector functions use invoker semantics + `search_path` hardening. Report does not recommend `auth.role() = 'service_role'` policies or public allow policies. |

### `security.json` inventory (2026-06-13)

All **eleven** collector `cre_*` lint rows verified in `/tmp/supabase-advisors-fhqycqubkkrdgzswccwd/security.json`: `cre_brokerages`, `cre_listings`, `cre_listing_contacts`, `cre_listing_documents`, `cre_listing_images`, `cre_scrape_jobs`, `cre_scrape_log`, `cre_listing_events`, `cre_source_index`, `cre_enrichment_queue`, `cre_source_baseline`. Each is INFO / `rls_enabled_no_policy` with matching `cache_key` values in the lint table above. Other `credeals` tables in the same export (e.g. `access_requests`, `deleted_accounts`) are correctly out of scope.

### `007` RLS comments

`007_cre_change_tracking.sql` header and inline comments match the report: RLS enabled on four monitor tables, no public row policies, service-role bypass, do not grant `anon`/`authenticated`.

### ACCEPT vs Appendix B

| Path | Verdict |
|------|---------|
| **Accept / no migration** | **Still recommended.** Deny-by-default RLS plus revoked API grants is a valid private-table pattern; INFO lint is documentation noise, not exposure. |
| **Appendix B `USING (false)` policies** | **Safe if ever applied.** Targets only `anon` and `authenticated`; does not grant access; does not affect `service_role`. Optional advisor hygiene only. |

### Repo migration gap (optional follow-up only)

`001`–`003` do not yet `ENABLE ROW LEVEL SECURITY` on the seven core listing tables; `007` does for monitor tables. Live DB already has RLS on all eleven (advisor confirms). A future idempotent hygiene migration could align repo SQL with live posture and add explicit `REVOKE`/`GRANT` documentation; **not required** for the ACCEPT decision.

### Edits applied in this peer review

- Clarified five-view `security_invoker` coverage and expanded verification query 4.
- Documented schema `USAGE` vs table/view privilege separation and invoker function semantics.
- Fixed external parent-report path wording; corrected relative reference links.
- Hardened Appendix B SQL (per-policy `COMMENT`, explicit anti-patterns, clearer header).

### Final recommendation

**ACCEPT recommendation unchanged.** No security remediation required. Do not create `008_cre_rls_service_role_policies.sql`. Appendix B remains optional deferred hygiene.
