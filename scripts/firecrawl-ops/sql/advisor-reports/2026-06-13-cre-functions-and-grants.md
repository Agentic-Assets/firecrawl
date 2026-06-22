# CRE Functions, Grants, and Advisor Follow-up

**Project:** `fhqycqubkkrdgzswccwd` (supabase-agentic-assets-v2)  
**Schema:** `credeals`  
**Advisor snapshot:** `/tmp/supabase-advisors-fhqycqubkkrdgzswccwd/security.json` (+ `performance.json`)  
**Date:** 2026-06-13  
**Scope:** Collector-owned listing functions/views (`scripts/firecrawl-ops/sql/005`, `006`) and EQUIRE-owned signup trigger `handle_new_user`

---

## Executive summary

| Object | Owner | Advisor status | Action |
|--------|-------|----------------|--------|
| `credeals.handle_new_user()` | **EQUIRE** (auth signup) | **WARN** `function_search_path_mutable` | Re-apply `SET search_path = ''` (regression from 20260419 migration) |
| `credeals.search_cre_listings(...)` | **Collector / shared agent API** | Clean (not in security WARN list) | None; verify grants in live DB |
| `credeals.update_cre_listing_timestamp()` | **Collector** | Clean | None; verify grants in live DB |
| `v_cre_*` views (`security_invoker=true`) | **Collector + EQUIRE consumer** | No view-specific security WARN | Keep; re-assert `security_invoker` after `006` if needed |
| `cre_listings.lat` / `lng` | **Collector** | Unaffected by `postgis` in `public` | No change (plain `double precision`, no geometry type) |
| `_schema_bloat_backup`, `_beachwalk_rentroll_backup_*` | **EQUIRE ops / ad hoc** | INFO `rls_enabled_no_policy` + perf `no_primary_key` | Drop, archive off-project, or add surrogate PK |

The only **credeals** function with a security **WARN** in this snapshot is `handle_new_user`. Collector listing functions already match the hardened pattern from `005_cre_views.sql`.

**Live apply update (2026-06-14):** `ALTER FUNCTION credeals.handle_new_user() SET search_path TO '';` was applied successfully. Before: `credeals.handle_new_user()|true|`; after: `credeals.handle_new_user()|true|search_path=""`. A fresh security advisor re-fetch reports **0** `function_search_path_mutable` warnings for `credeals.handle_new_user`. Durable EQUIRE migration created: `CRE_EQUIRE/supabase/migrations/20260614023249_harden_handle_new_user_search_path.sql`.

---

## Ownership: EQUIRE vs collector

| Surface | Owner | Repo / path | Do not break |
|---------|-------|-------------|--------------|
| `credeals.handle_new_user()`, `on_auth_user_created_equire` on `auth.users` | **EQUIRE** | `CRE_EQUIRE/supabase/migrations/` | Signup, invite accept, `equire_user_profiles` bootstrap |
| `credeals.equire_user_profiles`, org/deal tables | **EQUIRE** | `CRE_EQUIRE` | Product RLS and RPCs |
| `cre_*` base tables, monitor tables (007), ingest path | **Collector** | `firecrawl/scripts/firecrawl-ops/cre_collector/` | `cre_ingest.py`, `cre_monitor.py` (service-role psql) |
| `v_cre_listings_full`, `v_cre_active_for_sale`, `v_cre_active_for_lease`, `v_cre_market_summary`, `v_cre_recent_changes` | **Collector schema, EQUIRE consumer** | `sql/005_cre_views.sql`, `006_cre_contact_urls.sql` | Display app + agents read via service role |
| `search_cre_listings()`, `update_cre_listing_timestamp()` | **Collector migrations** | `sql/005_cre_views.sql` | Service-role execute only per `SUPABASE_SECURITY_NOTE_2026-06-12.md` |

`handle_new_user` is **not** defined in `scripts/firecrawl-ops/sql/`. It exists only in live DB + **EQUIRE** migrations. Fixing it belongs in EQUIRE (with a one-line `ALTER` for immediate relief).

---

## Advisor grep: credeals + `cre_*` (security.json)

### WARN (actionable)

| Lint | Object | Detail |
|------|--------|--------|
| `function_search_path_mutable` | `credeals.handle_new_user` | Role-mutable `search_path` on `SECURITY DEFINER` trigger function |

### INFO (accepted or out of scope for this pass)

| Lint | credeals objects |
|------|------------------|
| `rls_enabled_no_policy` | All collector `cre_*` tables (by design: RLS on, no `anon`/`authenticated` policies), plus EQUIRE tables (`access_requests`, `deleted_accounts`, MCP OAuth, etc.) and backup tables below |

**Not present in security.json for collector objects:** `search_cre_listings`, `update_cre_listing_timestamp`, any `v_cre_*` view, `security_invoker` violations.

### Project-wide WARN (tangential to listings)

| Lint | Object | CRE impact |
|------|--------|------------|
| `extension_in_public` | `postgis`, `pg_trgm`, `pgrouting`, `vector` in `public` | See PostGIS section; listing FTS uses core `to_tsvector`, not `pg_trgm` in `search_cre_listings` |

---

## 1. `credeals.handle_new_user` — search_path fix

### Where it lives

- **Not** in `scripts/firecrawl-ops/sql/`.
- **EQUIRE migrations:**
  - `CRE_EQUIRE/supabase/migrations/017_equire_functions_indexes.sql` — original with `SET search_path = ''`
  - `CRE_EQUIRE/supabase/migrations/20260419120000_add_first_last_name_to_equire_user_profiles.sql` — **latest body; omits `SET search_path`** (regression)
- Trigger: `on_auth_user_created_equire` `AFTER INSERT ON auth.users` → `credeals.handle_new_user()`

### Signature (live / migrations)

```text
credeals.handle_new_user()  RETURNS trigger  LANGUAGE plpgsql  SECURITY DEFINER
```

No arguments (trigger function). `ALTER` / `GRANT` must use empty arg list: `handle_new_user()`.

### Root cause

Migration `20260419120000` replaced the function body for `first_name` / `last_name` but dropped the hardening clause present in migration `017`.

### Immediate fix (live DB, idempotent; applied 2026-06-14)

```sql
-- EQUIRE-owned. Safe: INSERT targets credeals.equire_user_profiles (fully qualified);
-- NOW() resolves via pg_catalog even when search_path is empty.
ALTER FUNCTION credeals.handle_new_user()
  SET search_path TO '';
```

`ALTER FUNCTION ... SET search_path TO ''` and `SET search_path = ''` are equivalent in PostgreSQL. Use the zero-argument form `handle_new_user()` (trigger functions take no SQL args).

### Durable fix (EQUIRE repo migration; created 2026-06-14)

Migration created in `CRE_EQUIRE/supabase/migrations/20260614023249_harden_handle_new_user_search_path.sql`:

```sql
ALTER FUNCTION credeals.handle_new_user()
  SET search_path TO '';
```

If the function body is later replaced again, keep the `CREATE OR REPLACE` body hardened with `SET search_path = ''`:

```sql
CREATE OR REPLACE FUNCTION credeals.handle_new_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
  INSERT INTO credeals.equire_user_profiles (
    user_id,
    organization_id,
    display_name,
    first_name,
    last_name,
    role,
    created_at,
    updated_at
  )
  VALUES (
    NEW.id,
    '00000000-0000-0000-0000-000000000000',
    COALESCE(
      NEW.raw_user_meta_data->>'full_name',
      NEW.raw_user_meta_data->>'name',
      NEW.email
    ),
    NEW.raw_user_meta_data->>'first_name',
    NEW.raw_user_meta_data->>'last_name',
    'analyst',
    NOW(),
    NOW()
  )
  ON CONFLICT (user_id) DO NOTHING;
  RETURN NEW;
END;
$$;
```

### Signup safety check

After applying, run one test signup (or staging invite) and confirm:

```sql
SELECT user_id, display_name, first_name, last_name, role
FROM credeals.equire_user_profiles
WHERE user_id = '<new_auth_user_uuid>';
```

**Do not** move this function to `public` or change the trigger name without coordinating Corbis/EQUIRE shared-auth docs (`CRE_EQUIRE/docs/operations/email-setup.md`).

---

## 2. Collector functions: `search_cre_listings` and `update_cre_listing_timestamp`

### Source: `005_cre_views.sql`

Both functions already use:

```sql
SET search_path = ''
```

and fully qualified table references (`credeals.cre_listings`, `credeals.cre_brokerages`).

| Function | Type | `search_path` in repo | Advisor WARN |
|----------|------|----------------------|--------------|
| `search_cre_listings(text, text, text, text, text)` | `LANGUAGE sql` `STABLE` | `''` | None |
| `update_cre_listing_timestamp()` | `LANGUAGE plpgsql` trigger | `''` | None |

### EXECUTE grants (as migrated)

From `005_cre_views.sql`:

- `REVOKE EXECUTE ... FROM PUBLIC`
- `REVOKE ... FROM anon`, `authenticated` (if roles exist)
- `GRANT EXECUTE ... TO service_role` only

This matches `cre_collector/archive/SUPABASE_SECURITY_NOTE_2026-06-12.md`.

### Live verification SQL

```sql
-- search_path and security definer flags
SELECT
  n.nspname AS schema,
  p.proname AS name,
  pg_get_function_identity_arguments(p.oid) AS args,
  p.prosecdef AS security_definer,
  p.proconfig AS config
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'credeals'
  AND p.proname IN ('handle_new_user', 'search_cre_listings', 'update_cre_listing_timestamp')
ORDER BY p.proname, args;

-- EXECUTE grants (expect service_role only for listing helpers)
SELECT
  routine_name,
  grantee,
  privilege_type
FROM information_schema.role_routine_grants
WHERE specific_schema = 'credeals'
  AND routine_name IN ('search_cre_listings', 'update_cre_listing_timestamp')
ORDER BY routine_name, grantee;
```

### If grants drifted in live DB (re-apply)

```sql
REVOKE EXECUTE ON FUNCTION credeals.search_cre_listings(text, text, text, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION credeals.update_cre_listing_timestamp() FROM PUBLIC;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE EXECUTE ON FUNCTION credeals.search_cre_listings(text, text, text, text, text) FROM anon;
    REVOKE EXECUTE ON FUNCTION credeals.update_cre_listing_timestamp() FROM anon;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE EXECUTE ON FUNCTION credeals.search_cre_listings(text, text, text, text, text) FROM authenticated;
    REVOKE EXECUTE ON FUNCTION credeals.update_cre_listing_timestamp() FROM authenticated;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    GRANT EXECUTE ON FUNCTION credeals.search_cre_listings(text, text, text, text, text) TO service_role;
    GRANT EXECUTE ON FUNCTION credeals.update_cre_listing_timestamp() TO service_role;
  END IF;
END $$;
```

**Note:** `handle_new_user` is invoked by trigger, not direct client `EXECUTE`. Do not revoke trigger execution paths; only set `search_path`. `update_cre_listing_timestamp()` is **not** `SECURITY DEFINER` (invoker trigger only); `search_cre_listings` is `LANGUAGE sql STABLE` without definer elevation. Both still benefit from `SET search_path = ''` plus fully qualified object names.

### `raw_user_meta_data` vs RLS `user_metadata`

`handle_new_user` reads `NEW.raw_user_meta_data` inside an `auth.users` trigger to bootstrap **display** fields (`display_name`, `first_name`, `last_name`). That is appropriate: the Supabase checklist forbids `auth.jwt()->'user_metadata'` (or equivalent JWT claims) in **RLS authorization** policies because users can edit `raw_user_meta_data`. This function does not gate access; it seeds profile rows at signup. Do not copy these fields into RLS predicates or role checks.

---

## 3. `v_cre_*` views and `security_invoker`

### Views in `005_cre_views.sql`

| View | `security_invoker` in 005 | Security advisor |
|------|---------------------------|----------------|
| `v_cre_listings_full` | `true` | No WARN |
| `v_cre_active_for_sale` | `true` | No WARN |
| `v_cre_active_for_lease` | `true` | No WARN |
| `v_cre_market_summary` | `true` | No WARN |
| `v_cre_recent_changes` | `true` | No WARN |

`006_cre_contact_urls.sql` `CREATE OR REPLACE VIEW v_cre_listings_full` refreshes contact JSON only; it does **not** re-run `ALTER VIEW ... SET (security_invoker = true)`. After applying `006` on a fresh database, confirm invoker mode:

```sql
SELECT c.relname, c.reloptions
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'credeals'
  AND c.relkind = 'v'
  AND c.relname LIKE 'v_cre_%';
```

If any view lacks `security_invoker=true`:

```sql
ALTER VIEW credeals.v_cre_listings_full SET (security_invoker = true);
ALTER VIEW credeals.v_cre_active_for_sale SET (security_invoker = true);
ALTER VIEW credeals.v_cre_active_for_lease SET (security_invoker = true);
ALTER VIEW credeals.v_cre_market_summary SET (security_invoker = true);
ALTER VIEW credeals.v_cre_recent_changes SET (security_invoker = true);
```

Underlying `cre_*` tables show INFO `rls_enabled_no_policy`; that is **intentional** for the private collector surface (service-role access, no public row policies).

---

## 4. PostGIS in `public` vs `cre_listings` geo

| Fact | Implication |
|------|-------------|
| Advisor WARN: `extension_in_public` for `postgis` | Shared-project hygiene issue; not introduced by collector SQL |
| `cre_listings.lat`, `cre_listings.lng` are `double precision` | No `geometry`/`geography` columns; no PostGIS type dependency |
| `004_cre_indexes.sql` | B-tree / GIN only (`city_state`, FTS, etc.); **no** GiST / `ST_*` indexes |
| `search_cre_listings` | Filters on text FTS and scalar columns only |

**Conclusion:** Moving PostGIS out of `public` is a separate platform task. It does **not** block or break current listing lat/lng storage or agent queries unless future migrations add PostGIS functions or geometry columns.

Optional future geo index (not required today):

```sql
-- Only if radius queries become hot; not in current schema
-- CREATE INDEX ... ON credeals.cre_listings USING gist (
--   ST_SetSRID(ST_MakePoint(lng, lat), 4326)::geography
-- ) WHERE lat IS NOT NULL AND lng IS NOT NULL;
```

---

## 5. Backup tables: `_schema_bloat_backup`, `_beachwalk_rentroll_backup_20260610`

| Lint | Level | Object |
|------|-------|--------|
| `no_primary_key` | INFO (performance.json) | Both tables |
| `rls_enabled_no_policy` | INFO (security.json) | Both tables |

These are **not** collector migrations; ad hoc EQUIRE/ops backups.

### Options (pick one per table)

**A. Drop when no longer needed (preferred for lint clearance)**

```sql
-- Confirm row counts / owner sign-off first
DROP TABLE IF EXISTS credeals._schema_bloat_backup;
DROP TABLE IF EXISTS credeals._beachwalk_rentroll_backup_20260610;
```

**B. Keep with surrogate primary key (clears `no_primary_key`, keeps RLS INFO)**

```sql
ALTER TABLE credeals._schema_bloat_backup
  ADD COLUMN IF NOT EXISTS _backup_row_id bigserial PRIMARY KEY;

ALTER TABLE credeals._beachwalk_rentroll_backup_20260610
  ADD COLUMN IF NOT EXISTS _backup_row_id bigserial PRIMARY KEY;
```

**C. Leave as-is** — acceptable if tables are short-lived and service-role only; INFO lints are noisy but not an exposure by themselves (RLS on, no policies ⇒ deny for `anon`/`authenticated`).

---

## 6. Recommended apply order (production)

1. **EQUIRE:** `ALTER FUNCTION credeals.handle_new_user() SET search_path TO '';` applied live 2026-06-14; durable migration created. Still smoke-test next real signup/invite.
2. **Collector (if needed):** Re-run grant block from section 2 if verification shows drift.
3. **Collector:** Confirm `security_invoker` on all `v_cre_*` after any `006` deploy.
4. **EQUIRE ops:** Resolve or drop backup tables per section 5.
5. **Defer:** PostGIS schema move (platform-wide, coordinate with Corbis).

---

## Peer review (2026-06-13)

Independent validation against Supabase skill security checklist, live `security.json`, `005_cre_views.sql`, and EQUIRE migrations (read from `../CRE_EQUIRE/supabase/migrations/`; outside the firecrawl workspace but present on this host).

### Verdict summary

| Check | Result |
|-------|--------|
| `handle_new_user` immediate + durable `search_path` fix | **Confirmed correct** |
| Collector `search_cre_listings` / `update_cre_listing_timestamp` in advisor WARN list | **Confirmed absent** (grep on snapshot) |
| PostGIS-in-`public` vs `cre_listings.lat`/`lng` | **Confirmed unrelated** (`double precision`, no geometry) |
| Backup table options A/B/C | **Sound** (drop preferred; surrogate PK clears perf lint only) |

### `function_search_path_mutable`: which `search_path`?

| Pattern | Use when | This project |
|---------|----------|--------------|
| `SET search_path = ''` (empty) | `SECURITY DEFINER` or any function using **fully qualified** names; Supabase linter target | **Correct for `handle_new_user` and collector functions** |
| `pg_catalog, public` | Legacy helpers that rely on unqualified builtins/types in `public` | Not needed here |
| `pg_catalog, credeals` | Definer functions with unqualified `credeals` objects only | Weaker than `''` + qualification; collector already uses `''` |

Empty `search_path` closes search-path hijacking on `SECURITY DEFINER` triggers. Builtins (`now()`, `COALESCE`, casts) still resolve from `pg_catalog`. All table references in `handle_new_user` and collector SQL are schema-qualified (`credeals.*`), so `''` is the right hardening choice per [Supabase lint 0011](https://supabase.com/docs/guides/database/database-linter?lint=0011_function_search_path_mutable).

### `SECURITY DEFINER` in `credeals`

`handle_new_user` is `SECURITY DEFINER` in schema `credeals` (API-exposed in the shared Supabase project). Checklist preference is to keep definer functions in a non-exposed schema. **Accepted for now:** it runs only from `on_auth_user_created_equire` on `auth.users`, not from PostgREST RPC calls; fixing `search_path` clears the only credeals **WARN** in the snapshot. Moving the function to a private schema is a separate EQUIRE/Corbis coordination task, not a collector action.

Collector listing helpers are **not** definer functions; they rely on `REVOKE EXECUTE FROM PUBLIC` + `GRANT EXECUTE TO service_role` (verified in `005_cre_views.sql`).

### EQUIRE migration regression (verified)

| File | `SET search_path` | Body |
|------|-------------------|------|
| `017_equire_functions_indexes.sql` | Present (`SET search_path = ''`) | Includes `avatar_url`, org from metadata |
| `20260419120000_add_first_last_name_to_equire_user_profiles.sql` | **Missing** (regression) | Adds `first_name`/`last_name`; fixed org to placeholder UUID |

Durable fix: new EQUIRE migration with `CREATE OR REPLACE` body from `20260419` **plus** `SET search_path = ''`. Immediate relief: `ALTER FUNCTION` only (no body change). **Do not apply via `scripts/firecrawl-ops/sql/`**; EQUIRE owns execution.

### Collector execution readiness

| Step | Owner | Ready? |
|------|-------|--------|
| 1. `ALTER FUNCTION handle_new_user ... search_path` | **EQUIRE** | Yes (SQL above); smoke-test signup after |
| 2. Re-apply collector grant block if drifted | **Collector** | Yes (section 2); only if live verification fails |
| 3. Confirm `security_invoker` after `006` | **Collector** | Yes (section 3 query) |
| 4. Drop or PK-wrap backup tables | **EQUIRE ops** | Yes (section 5); not collector migrations |
| 5. PostGIS schema move | **Platform** | Defer |

### `security.json` grep evidence (2026-06-13)

- **WARN `function_search_path_mutable` in `credeals`:** `handle_new_user` only (line ~1739 in snapshot).
- **No matches** for `search_cre_listings`, `update_cre_listing_timestamp`, or `v_cre_*` view violations.
- Collector `cre_*` tables appear only under INFO `rls_enabled_no_policy` (expected private surface).

---

## References

- `scripts/firecrawl-ops/sql/005_cre_views.sql` — collector functions, views, grants
- `scripts/firecrawl-ops/sql/006_cre_contact_urls.sql` — contact URL columns + `v_cre_listings_full` refresh
- `scripts/firecrawl-ops/cre_collector/archive/SUPABASE_SECURITY_NOTE_2026-06-12.md` — service-role-only posture
- `CRE_EQUIRE/supabase/migrations/20260419120000_add_first_last_name_to_equire_user_profiles.sql` — `handle_new_user` regression source
- `CRE_EQUIRE/supabase/migrations/017_equire_functions_indexes.sql` — original hardened `handle_new_user`
