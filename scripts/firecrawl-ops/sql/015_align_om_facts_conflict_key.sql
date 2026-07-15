-- =============================================================================
-- 015_align_om_facts_conflict_key.sql
--
-- Align legacy installations of cre_listing_om_facts with the canonical
-- parser-version-inclusive conflict key. This migration is intentionally
-- guarded: a database already on the five-column key is left untouched.
--
-- REVIEW BOUNDARY: this file is staged for review only. Do not apply it to a
-- production database without the schema-owner approval recorded in
-- CREDEALS_OWNERSHIP.md and a maintenance-window decision for index rebuilds.
-- It refuses to run unless psql receives
-- -v CRE_APPROVE_OM_FACTS_KEY_ALIGNMENT=1.
-- =============================================================================

\set ON_ERROR_STOP on

\if :{?CRE_APPROVE_OM_FACTS_KEY_ALIGNMENT}
\else
  \set CRE_APPROVE_OM_FACTS_KEY_ALIGNMENT 0
\endif

\if :CRE_APPROVE_OM_FACTS_KEY_ALIGNMENT
  \echo 'Approved legacy OM-facts key alignment requested'
\else
  \echo 'REFUSED: migration 015 requires -v CRE_APPROVE_OM_FACTS_KEY_ALIGNMENT=1'
  DO $$
  BEGIN
    RAISE EXCEPTION
      'migration 015 requires CRE_APPROVE_OM_FACTS_KEY_ALIGNMENT=1';
  END
  $$;
\endif

SELECT CASE WHEN EXISTS (
    SELECT 1
    FROM pg_index i
    JOIN pg_class idx ON idx.oid = i.indexrelid
    JOIN pg_class tbl ON tbl.oid = i.indrelid
    JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
    WHERE ns.nspname = 'credeals'
      AND tbl.relname = 'cre_listing_om_facts'
      AND idx.relname = 'cre_listing_om_facts_uq'
      AND i.indisunique
      AND i.indnullsnotdistinct
      AND pg_get_indexdef(i.indexrelid) LIKE '%(listing_id, fact_group, fact_key, source_doc_url, parser_version) NULLS NOT DISTINCT%'
) THEN false ELSE true END AS needs_om_facts_key_alignment \gset

\if :needs_om_facts_key_alignment
  \echo 'Aligning cre_listing_om_facts_uq to include parser_version'
  DROP INDEX IF EXISTS credeals.cre_listing_om_facts_uq;
  CREATE UNIQUE INDEX cre_listing_om_facts_uq
    ON credeals.cre_listing_om_facts
    (listing_id, fact_group, fact_key, source_doc_url, parser_version) NULLS NOT DISTINCT;
\else
  \echo 'cre_listing_om_facts_uq already uses the canonical five-column key; no change'
\endif
