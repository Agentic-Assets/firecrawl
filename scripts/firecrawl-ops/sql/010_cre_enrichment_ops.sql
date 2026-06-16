-- =============================================================================
-- 010_cre_enrichment_ops.sql
-- CRE Listing Intelligence for EQUIRE
-- Supabase project: supabase-agentic-assets-v2 (fhqycqubkkrdgzswccwd)
--
-- Tier-B enrichment-queue operability views (design:
-- cre_collector/ENRICHMENT_WORKER_DESIGN_2026-06-15.md Section 6;
-- cre_collector/out/enrich/IMPL_SPEC.md Section 4.9). ADDITIVE ONLY: two
-- read-only views over the existing credeals.cre_enrichment_queue table (created
-- in 007). NO table change: 007 already has claimed_at, done_at, attempts,
-- last_error, and the (priority, enqueued_at) WHERE done_at IS NULL drain index.
-- Idempotent (CREATE OR REPLACE VIEW). Nothing here alters or drops an existing
-- object, and re-running is safe.
--
-- These views give cre_enrich.py / cre_status.sh a one-line queue-health probe:
--   - v_cre_enrichment_queue_pending : live work the worker still drains
--     (done_at IS NULL AND attempts < 5), ordered exactly like the claim SQL.
--     The worker DELETEs rows on completion, so this is every live non-dead row.
--   - v_cre_enrichment_dead          : dead-lettered rows the worker gave up on
--     (done_at IS NULL AND attempts >= 5), for inspection / requeue decisions.
-- The attempts-5 threshold matches cre_enrich.py MAX_ATTEMPTS and the claim SQL
-- `attempts < 5` drain predicate; do not drift one without the other.
--
-- Registered in 000_run_all.sql AFTER 009 (these views depend only on the 007
-- queue table). Apply is GATED to the enrichment cutover runbook (design Section
-- 9); this migration does NOT load any launchd tier and does NOT enable
-- mark-missing.
--
-- Security posture mirrors the 007 collector-owned tables (see
-- cre_collector/archive/SUPABASE_SECURITY_NOTE_2026-06-12.md): credeals schema,
-- service-role / direct postgres connection only. Do NOT grant
-- anon/authenticated access to these views.
-- =============================================================================

-- Live work queue: every pending row the worker is eligible to claim, in claim
-- order (priority, enqueued_at) matching cre_enrich.build_claim_sql.
CREATE OR REPLACE VIEW credeals.v_cre_enrichment_queue_pending
  WITH (security_invoker = true) AS
  SELECT *
    FROM credeals.cre_enrichment_queue
   WHERE done_at IS NULL
     AND attempts < 5
   ORDER BY priority, enqueued_at;

COMMENT ON VIEW credeals.v_cre_enrichment_queue_pending IS
  'Live Tier-B enrichment work: pending queue rows (done_at IS NULL AND attempts < 5) in claim order (priority, enqueued_at). The enrich worker DELETEs rows on completion, so this is every live non-dead-letter row. Service-role only.';

-- Dead-letter inspection: rows the worker exhausted (attempts >= 5). They have
-- left the claim SQL's `attempts < 5` drain set; a later monitor-detected change
-- to the same listing re-enqueues a fresh row (ON CONFLICT DO NOTHING dedups
-- only while the old row exists, and done rows are deleted).
CREATE OR REPLACE VIEW credeals.v_cre_enrichment_dead
  WITH (security_invoker = true) AS
  SELECT *
    FROM credeals.cre_enrichment_queue
   WHERE done_at IS NULL
     AND attempts >= 5;

COMMENT ON VIEW credeals.v_cre_enrichment_dead IS
  'Dead-lettered Tier-B enrichment rows (done_at IS NULL AND attempts >= 5): the worker exhausted its attempts. Surfaced for inspection; the weekly additive full scrape is the backstop that still refreshes these listings. Service-role only.';
