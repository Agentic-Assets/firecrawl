# CRE SQL Layer: Best-Practices Review and Hardening

**Project:** `supabase-agentic-assets-v2` (`fhqycqubkkrdgzswccwd`), schema `credeals`, Postgres 17.6
**Date:** 2026-06-13
**Method:** Supabase Postgres best-practices review (5 dimensions: indexing, schema
integrity, idempotency/migration-chain, security surface, ingest robustness),
adversarially verified against the live schema read-only (30-agent workflow, 24
findings confirmed, 1 refuted). Plus a throwaway-Postgres-17 migration smoke test
that the existing advisor pass did not perform.

This review complements the advisor remediation in
`2026-06-13-cre-remediation-apply-log.md`. The advisor flags table/view/index
lints; this pass adds the things it cannot see: migration-chain re-runnability,
function-body vs source drift, expression-index match, and constraint NULL
semantics.

## Test evidence (re-runnable)

Migration smoke test (`/tmp/cre_sql_migration_smoketest.sh`, not committed): runs
`000_run_all.sql` twice on a fresh `postgres:17-alpine` container.

- APPLY #1 (clean) and APPLY #2 (idempotent re-run): both OK.
- Inventory: 11 tables, 5 views, 2 functions, 2 triggers, 46 indexes,
  **11 RLS-enabled tables**, 5/5 views `security_invoker=true`, 3 views carrying
  the widened on-market predicate, status CHECK with 9 values.
- New range CHECKs present; both hardened unique indexes report
  `indnullsnotdistinct = t`.
- Functional: a row stays on the board through `under_contract`, drops at `sold`,
  hides on soft-delete; the `updated_at` trigger fires; FTS search returns it.
- Negative test: `cap_rate = 6.5` is rejected by the new range CHECK.

Application suites: `pytest` 254 passed, `npm test` 157 passed (typecheck clean).

Live advisors re-run (collector slice only): security = 11 INFO
`rls_enabled_no_policy`; performance = 14 INFO `unused_index`; zero ERROR/WARN on
`credeals.cre_*`. The FTS GIN index was proven usable by `search_cre_listings`
via a live `EXPLAIN` (Bitmap Index Scan on `cre_listings_fts_idx`); its "unused"
flag is traffic-driven only.

## Two real defects found and fixed (on-branch, fresh-apply verified)

1. **RLS migration-completeness gap.** `001`/`002`/`003` did not
   `ENABLE ROW LEVEL SECURITY` on the 7 base tables, so a fresh `000_run_all.sql`
   brought them up with RLS OFF, diverging from live (all 11 enabled) and the
   Supabase security checklist. Added idempotent `ENABLE ROW LEVEL SECURITY` to
   `cre_brokerages`, `cre_listings`, `cre_listing_contacts`,
   `cre_listing_documents`, `cre_listing_images`, `cre_scrape_jobs`,
   `cre_scrape_log`. Smoke test now shows 11/11.

2. **`001` Transwestern seed jsonb escaping bug.** The `scrape_config` notes held
   `'-'` (un-doubled single quotes) inside a single-quoted jsonb literal. psql
   tokenized it as `stringA - stringB::jsonb`, aborting the whole chain at
   migration `001` on any fresh apply. Live had been seeded from the same buggy
   SQL (its notes were corrupted to `empty or '.`). Doubled the quotes to
   `''-''`; `ON CONFLICT DO UPDATE` corrects live on the next `001` apply.

## Best-practice hardening applied (fresh-apply; live unchanged until re-applied)

These take effect on a fresh `000_run_all.sql`. On the live DB the affected
objects already exist, so `CREATE TABLE/INDEX IF NOT EXISTS` skips them: the live
definitions are unchanged until `002`/`003`/`005`/`007` are re-applied (gated).
The file is the go-forward source of truth and is now smoke-test-clean.

| Finding | File | Change |
|---|---|---|
| 13 | `002` | Status CHECK rebuilt only when missing/pre-widening (DO-block guard), so re-runs no longer take ACCESS EXCLUSIVE + a full validating scan every time. |
| 12 | `002` | Added `cap_rate` range CHECK `(>0 AND <0.5)` (mirrors `norm_cap_rate`) and `occupancy_rate` `[0,1]`, idempotent; live data complies. |
| 20 | `002` | Documented ingestor-writable vs reserved (`expired`/`withdrawn`) status values on the CHECK. |
| 3  | `006` | Added `ALTER VIEW v_cre_listings_full SET (security_invoker=true)` so standalone 006 cannot silently demote the view. |
| 2  | `006` | Documented the `l.*` stale-star hazard (view must be re-applied after any `cre_listings` column add). |
| 8  | `007` | `cre_listing_events_idem_uq` now `NULLS NOT DISTINCT` (a NULL `scrape_job_id` no longer defeats the within-run idempotency guard). |
| 4  | `007` | `cre_enrichment_queue` unique now `NULLS NOT DISTINCT` (NULL `brokerage_id` no longer defeats dedup). |
| 19 | `007` | `cre_listing_events.brokerage_id`/`scrape_job_id` -> `ON DELETE SET NULL` (append-only ledger no longer blocks brokerage/job pruning). |
| 10 | `003` | `cre_scrape_log.job_id`/`listing_id` -> `ON DELETE SET NULL` (job/listing pruning no longer blocked by audit rows). |
| 21 | `005` | Dropped the pointless `service_role` EXECUTE grant on the trigger function (kept defensive REVOKEs). |
| 22 | `005` | Added declarative `REVOKE SELECT` on all 5 views from PUBLIC/anon/authenticated (defense in depth vs a future broad GRANT). |

## Deferred / gated / documented (no file change, by design)

- **[1] Live `search_cre_listings()` body is still `status='active'`** (the
  function half of the gated 005 widening). The source is already widened. The
  activation runbook now requires the gated 005 apply to include
  `CREATE OR REPLACE FUNCTION` and a post-apply body check. See the phase2
  board-impact doc.
- **[2] Live `v_cre_listings_full` is missing `last_seen_at` / `source_lastmod` /
  `canonical_key`** (stale `l.*` frozen before those columns were added). The
  committed chain is correct on fresh apply; the gated live 005 apply (CREATE OR
  REPLACE VIEW) refreshes the column list. EQUIRE agents do not currently read
  these internal fields, so impact is latent.
- **[5][6][7][17] Index tuning** (partial FTS / partial `(txn,status)` WHERE
  `deleted_at IS NULL`; drop `cre_source_index_first_seen_idx` and
  `cre_listings_last_seen_idx` as unused). Deferred per the documented
  unused-index posture (revisit after 60-90 days of production traffic); the
  board is 100% active today so status indexes are non-selective.
- **[9] `cre_scrape_jobs.brokerage_id` nullable.** By design: multi-source
  monitor runs write NULL (no single brokerage). Documented in `cre_monitor.py`.
- **[11] 50 JLL rows have `deleted_at IS NOT NULL` but `status='active'`.** A
  legacy inconsistency from a pre-current soft-delete path. These rows are
  board-invisible (views filter `deleted_at IS NULL`), so impact is cosmetic.
  Optional one-time live cleanup (board-invisible, low risk):
  `UPDATE credeals.cre_listings SET status='inactive' WHERE deleted_at IS NOT NULL AND status='active';`
  A consistency CHECK was NOT added to the chain because a validating ADD would
  fail on live until those rows are cleaned.
- **[14] `REVOKE USAGE ON SCHEMA credeals FROM anon, authenticated`.** NOT done:
  `credeals` is shared with EQUIRE app-owned objects (e.g. `handle_new_user`,
  `match_*`, `cre_business_plan_runs`); revoking schema USAGE could break the
  EQUIRE app. Views and functions already deny anon/authenticated, so exposure is
  introspection-only. Requires a full EQUIRE app role audit first.
- **[15][16][23][24] Ingestor edge cases** (cross-terminal downgrade guard width;
  circuit-breaker resurrection inflation; `merge_rows` first-pass-wins for
  uc/pending; cap_rate stale-value on resurrection). All gated to the T3.1 live
  activation decision; the breaker is default-OFF so none affects the unattended
  daily ingest today. To revisit with added tests before enabling the breaker on
  the first live activation run.

## File-vs-live divergence (intentional, documented)

After this pass the migration files express the correct go-forward target. The
live DB does NOT yet carry: the 7 base-table RLS enables (live already has them
via an out-of-band apply, so this is moot), the `cap_rate`/`occupancy_rate`
CHECKs, `NULLS NOT DISTINCT` on the two unique indexes, the audit-FK `ON DELETE
SET NULL`, the 006 `security_invoker` reassertion, and the view REVOKEs. Applying
them to live is low risk but is live DDL (gated); it happens naturally on the next
full `000_run_all.sql` or the gated 005 apply. The smoke test proves they apply
clean and idempotent on a fresh database.
