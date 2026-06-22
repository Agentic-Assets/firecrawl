\pset pager off
\echo '--- 1. new monitor tables persisted (expect 4) ---'
SELECT table_name FROM information_schema.tables
WHERE table_schema='credeals'
  AND table_name IN ('cre_listing_events','cre_source_index','cre_enrichment_queue','cre_source_baseline')
ORDER BY table_name;
\echo '--- 2. widened status CHECK persisted ---'
SELECT pg_get_constraintdef(oid) AS widened_check FROM pg_constraint
WHERE conrelid='credeals.cre_listings'::regclass AND conname='cre_listings_status_check';
\echo '--- 3. new cre_listings columns persisted ---'
SELECT column_name, data_type FROM information_schema.columns
WHERE table_schema='credeals' AND table_name='cre_listings'
  AND column_name IN ('last_seen_at','source_lastmod','canonical_key') ORDER BY column_name;
\echo '--- 4. active visible count UNCHANGED (expect 72544) ---'
SELECT count(*) AS active_visible FROM credeals.cre_listings WHERE deleted_at IS NULL AND status='active';
\echo '--- 5. all v_cre_* views (expect the 4 originals + v_cre_recent_changes = 5) ---'
SELECT table_name FROM information_schema.views
WHERE table_schema='credeals' AND table_name LIKE 'v_cre_%' ORDER BY table_name;
\echo '--- 6. RLS enabled on all 4 new tables (expect t) ---'
SELECT c.relname, c.relrowsecurity AS rls FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='credeals' AND c.relname IN ('cre_listing_events','cre_source_index','cre_enrichment_queue','cre_source_baseline')
ORDER BY c.relname;
\echo '--- 7. new change-tracking indexes present ---'
SELECT indexname FROM pg_indexes
WHERE schemaname='credeals'
  AND indexname IN ('cre_listings_canonical_key_idx','cre_listings_last_seen_idx',
                    'cre_source_index_uq','cre_source_index_first_seen_idx','cre_source_index_source_key_idx',
                    'cre_listing_events_idem_uq','cre_enrichment_queue_drain_idx')
ORDER BY indexname;
\echo '--- 8. the 4 existing EQUIRE views are still security_invoker (untouched) ---'
SELECT c.relname, (c.reloptions::text) AS opts FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='credeals' AND c.relkind='v'
  AND c.relname IN ('v_cre_listings_full','v_cre_active_for_sale','v_cre_active_for_lease','v_cre_market_summary')
ORDER BY c.relname;
