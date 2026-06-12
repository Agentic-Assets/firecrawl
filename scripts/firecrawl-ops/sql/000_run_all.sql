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
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 005_cre_views.sql
--
-- Option C -- Supabase SQL editor: paste the contents of 001 -> 005 in order.
--
-- -----------------------------------------------------------------------------
-- DEPENDENCY ORDER (do not reorder)
--   001 cre_brokerages          base registry (FK target)
--   002 cre_listings (+children) references cre_brokerages
--   003 cre_scrape_tracking     references cre_brokerages, cre_listings
--   004 cre_indexes             indexes on cre_listings
--   005 cre_views               views, search fn, updated_at triggers
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
