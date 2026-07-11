-- =============================================================================
-- 000_run_all.sql
-- CRE Listing Schema for EQUIRE
-- Supabase project: supabase-agentic-assets-v2 (fhqycqubkkrdgzswccwd)
--   API URL : https://fhqycqubkkrdgzswccwd.supabase.co  (us-east-1, Postgres 17.6)
--
-- Master runner. Applies all CRE listing-intelligence migrations in order
-- inside a single transaction. Each migration is idempotent (IF NOT EXISTS /
-- CREATE OR REPLACE / ON CONFLICT), so re-running is safe.
--
-- -----------------------------------------------------------------------------
-- HOW TO RUN
-- -----------------------------------------------------------------------------
-- Option A -- psql against the pooler/direct connection string:
--
--     export DATABASE_URL='postgresql://postgres:<pwd>@db.fhqycqubkkrdgzswccwd.supabase.co:5432/postgres'
--     cd scripts/firecrawl-ops/sql
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 000_run_all.sql
--
--   The \i lines below pull in each migration; they resolve relative to the
--   directory psql is launched from, so run from scripts/firecrawl-ops/sql.
--
-- Option B -- run each file individually (Supabase SQL editor or psql):
--
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 001_cre_brokerages.sql
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 002_cre_listings.sql
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 003_cre_scrape_tracking.sql
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 004_cre_indexes.sql
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 007_cre_change_tracking.sql
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 008_cre_fk_indexes.sql
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 009_cre_history_retention.sql
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 010_cre_enrichment_ops.sql
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 011_cre_listing_media.sql
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 012_cre_listing_institutional_cols.sql
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 013_cre_listing_om_facts.sql
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 014_cre_geo_crosswalk.sql
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 015_align_om_facts_conflict_key.sql
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 006_cre_contact_urls.sql
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 005_cre_views.sql
--
-- Option C -- Supabase SQL editor: paste each file individually in the Option B
-- order above. The editor does not process psql \i includes.
--
-- -----------------------------------------------------------------------------
-- DEPENDENCY ORDER (do not reorder)
--   001 cre_brokerages          base registry (FK target)
--   002 cre_listings (+children) references cre_brokerages; + change-tracking ALTERs
--   003 cre_scrape_tracking     references cre_brokerages, cre_listings
--   004 cre_indexes             indexes on cre_listings (+ change-tracking indexes)
--   007 cre_change_tracking     monitor tables (events, source_index, queue, baseline)
--   008 cre_fk_indexes          FK covering indexes on 007 monitor tables (advisor 0001)
--   009 cre_history_retention   price-history + child archive tables + retention trigger
--   010 cre_enrichment_ops      enrichment-queue health views (depend only on 007 queue)
--   011 cre_listing_media       media/links tables (+ doc_type widen, archive mirrors)
--   012 cre_listing_institutional_cols  WS3 discrete cols + WS4 geo cols + extra_facts
--                                       on cre_listings; license on cre_listing_contacts
--   013 cre_listing_om_facts    OM/PDF-parsed facts table (+ archive mirror), provenance
--   014 cre_geo_crosswalk       ZIP->county+CBSA reference table (\copy load gated)
--   015 align_om_facts_conflict_key  guarded four-to-five-column index alignment
--   006 cre_contact_urls        contact profile/avatar/VCard URL fields
--   005 cre_views               views (incl v_cre_recent_changes), search fn, triggers
--
-- Extensions: pgcrypto (gen_random_uuid) is already installed on this project.
-- pg_trgm / postgis / vector / uuid-ossp are present and not required by these
-- migrations but available for future geo / similarity work.
-- =============================================================================

\set ON_ERROR_STOP on

BEGIN;

CREATE SCHEMA IF NOT EXISTS credeals;

\echo '=== 001_cre_brokerages.sql ==='
\i 001_cre_brokerages.sql

\echo '=== 002_cre_listings.sql ==='
\i 002_cre_listings.sql

\echo '=== 003_cre_scrape_tracking.sql ==='
\i 003_cre_scrape_tracking.sql

\echo '=== 004_cre_indexes.sql ==='
\i 004_cre_indexes.sql

\echo '=== 007_cre_change_tracking.sql ==='
\i 007_cre_change_tracking.sql

\echo '=== 008_cre_fk_indexes.sql ==='
\i 008_cre_fk_indexes.sql

\echo '=== 009_cre_history_retention.sql ==='
\i 009_cre_history_retention.sql

\echo '=== 010_cre_enrichment_ops.sql ==='
\i 010_cre_enrichment_ops.sql

\echo '=== 011_cre_listing_media.sql ==='
\i 011_cre_listing_media.sql

\echo '=== 012_cre_listing_institutional_cols.sql ==='
\i 012_cre_listing_institutional_cols.sql

\echo '=== 013_cre_listing_om_facts.sql ==='
\i 013_cre_listing_om_facts.sql

\echo '=== 014_cre_geo_crosswalk.sql ==='
\i 014_cre_geo_crosswalk.sql

\echo '=== 015_align_om_facts_conflict_key.sql ==='
\i 015_align_om_facts_conflict_key.sql

\echo '=== 006_cre_contact_urls.sql ==='
\i 006_cre_contact_urls.sql

\echo '=== 005_cre_views.sql ==='
\i 005_cre_views.sql

COMMIT;

\echo ''
\echo '=== CRE listing schema applied. Verification: ==='
SELECT 'tables' AS kind, count(*) AS n
  FROM information_schema.tables
 WHERE table_schema = 'credeals' AND table_name LIKE 'cre_%'
UNION ALL
SELECT 'views', count(*)
  FROM information_schema.views
 WHERE table_schema = 'credeals' AND table_name LIKE 'v_cre_%'
UNION ALL
SELECT 'brokerages_seeded', count(*) FROM credeals.cre_brokerages
UNION ALL
SELECT 'brokerages_active', count(*) FROM credeals.cre_brokerages WHERE active;
