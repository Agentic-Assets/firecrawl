\set ON_ERROR_STOP on
\pset pager off
SET lock_timeout = '10s';        -- abort cleanly rather than block the live table
SET statement_timeout = '120s';

BEGIN;

\i /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/tasks/tmp/deltas_007.sql

COMMIT;

\echo '=== 007 change-tracking layer applied and COMMITTED ==='
