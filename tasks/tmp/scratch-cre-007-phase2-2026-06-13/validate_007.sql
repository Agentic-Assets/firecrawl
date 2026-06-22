\set ON_ERROR_STOP on
\pset pager off
SET lock_timeout = '10s';

BEGIN;

\i /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/tasks/tmp/deltas_007.sql

\echo '=== VALIDATION (inside a transaction that will ROLL BACK) ==='
\echo '--- new tables present (expect 4) ---'
SELECT count(*) AS new_tables FROM information_schema.tables
WHERE table_schema='credeals'
  AND table_name IN ('cre_listing_events','cre_source_index','cre_enrichment_queue','cre_source_baseline');
\echo '--- widened status CHECK (expect 9 values incl under_contract/pending/off_market) ---'
SELECT pg_get_constraintdef(oid) AS widened_check FROM pg_constraint
WHERE conrelid='credeals.cre_listings'::regclass AND conname='cre_listings_status_check';
\echo '--- new cre_listings columns (expect canonical_key, last_seen_at, source_lastmod) ---'
SELECT string_agg(column_name, ', ' ORDER BY column_name) AS new_cols
FROM information_schema.columns
WHERE table_schema='credeals' AND table_name='cre_listings'
  AND column_name IN ('last_seen_at','source_lastmod','canonical_key');
\echo '--- active visible count (MUST still be 72544; constraint widening drops nothing) ---'
SELECT count(*) AS active_visible FROM credeals.cre_listings WHERE deleted_at IS NULL AND status='active';
\echo '--- new view is queryable (expect 0 rows, no events yet) ---'
SELECT count(*) AS recent_changes_rows FROM credeals.v_cre_recent_changes;
\echo '--- RLS enabled on the 4 new tables (expect all t) ---'
SELECT c.relname, c.relrowsecurity AS rls FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='credeals' AND c.relname IN ('cre_listing_events','cre_source_index','cre_enrichment_queue','cre_source_baseline')
ORDER BY c.relname;

ROLLBACK;

\echo '=== validation transaction ROLLED BACK; nothing persisted ==='
