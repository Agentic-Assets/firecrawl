\pset pager off
\echo '--- connection ---'
SELECT current_database() AS db, current_user AS usr;
\echo '--- cre_listings CHECK constraints (need the real name for the DROP) ---'
SELECT conname, pg_get_constraintdef(oid) AS def
FROM pg_constraint
WHERE conrelid = 'credeals.cre_listings'::regclass AND contype = 'c'
ORDER BY conname;
\echo '--- RLS state on credeals cre_% base tables ---'
SELECT c.relname, c.relrowsecurity AS rls_enabled
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'credeals' AND c.relkind = 'r' AND c.relname LIKE 'cre_%'
ORDER BY c.relname;
\echo '--- existing cre_% tables ---'
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'credeals' AND table_name LIKE 'cre_%' ORDER BY table_name;
\echo '--- do the 4 new tables already exist? (expect 0) ---'
SELECT count(*) AS new_tables_present FROM information_schema.tables
WHERE table_schema='credeals'
  AND table_name IN ('cre_listing_events','cre_source_index','cre_enrichment_queue','cre_source_baseline');
\echo '--- baseline row counts (must be UNCHANGED after apply) ---'
SELECT
  count(*) AS total_rows,
  count(*) FILTER (WHERE deleted_at IS NULL) AS not_deleted,
  count(*) FILTER (WHERE deleted_at IS NULL AND status='active') AS active_visible
FROM credeals.cre_listings;
\echo '--- distinct status values present today ---'
SELECT status, count(*) FROM credeals.cre_listings GROUP BY status ORDER BY 2 DESC;
\echo '--- non-terminal scrape jobs (running?) ---'
SELECT status, count(*) FROM credeals.cre_scrape_jobs GROUP BY status ORDER BY 2 DESC;
