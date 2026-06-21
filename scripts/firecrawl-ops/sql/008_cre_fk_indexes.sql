-- =============================================================================
-- 008_cre_fk_indexes.sql
-- CRE Listing Intelligence for EQUIRE
-- Supabase project: supabase-agentic-assets-v2 (fhqycqubkkrdgzswccwd)
--
-- Covers unindexed foreign keys flagged by Supabase performance advisor
-- (lint 0001_unindexed_foreign_keys) on collector-owned 007 tables:
--   cre_listing_events.scrape_job_id -> cre_scrape_jobs(id)
--   cre_source_baseline.last_accepted_job_id -> cre_scrape_jobs(id)
--
-- Idempotent. Safe inside 000_run_all.sql (transactional CREATE INDEX).
-- On a live DB with monitor event history, prefer the CONCURRENTLY variant
-- documented in advisor-reports/2026-06-13-cre-unindexed-foreign-keys.md.
--
-- Requires: 003_cre_scrape_tracking.sql, 007_cre_change_tracking.sql.
-- =============================================================================

CREATE INDEX IF NOT EXISTS cre_listing_events_scrape_job_idx
    ON credeals.cre_listing_events (scrape_job_id);

CREATE INDEX IF NOT EXISTS cre_source_baseline_last_accepted_job_idx
    ON credeals.cre_source_baseline (last_accepted_job_id);

-- -----------------------------------------------------------------------------
-- Live Supabase (Postgres 17.6): run OUTSIDE a transaction if events table is
-- large enough that ACCESS EXCLUSIVE on cre_listing_events would hurt monitor
-- writes. CONCURRENTLY IF NOT EXISTS is valid on PG 11+; this project is 17.6.
-- Re-run failed builds only after DROP INDEX CONCURRENTLY on invalid leftovers.
-- -----------------------------------------------------------------------------
-- CREATE INDEX CONCURRENTLY IF NOT EXISTS cre_listing_events_scrape_job_idx
--     ON credeals.cre_listing_events (scrape_job_id);
--
-- CREATE INDEX CONCURRENTLY IF NOT EXISTS cre_source_baseline_last_accepted_job_idx
--     ON credeals.cre_source_baseline (last_accepted_job_id);
