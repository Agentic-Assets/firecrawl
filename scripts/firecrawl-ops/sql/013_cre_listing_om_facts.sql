-- =============================================================================
-- 013_cre_listing_om_facts.sql
-- CRE Listing Intelligence for EQUIRE
-- Supabase project: supabase-agentic-assets-v2 (fhqycqubkkrdgzswccwd)
--
-- Phase-2 data-lift WS2: OM/PDF-parsed facts (scalar underwriting + unit_mix +
-- rent_roll line items) with parse provenance, plus the mark-missing archive
-- mirror. Spec: cre_collector/PHASE2_DATA_LIFT_CONTRACT_2026-06-15.md Section A.2.
--
-- ADDITIVE ONLY and idempotent (CREATE TABLE / INDEX IF NOT EXISTS). Nothing
-- here alters, drops, or renames an existing TABLE, COLUMN, or DATA.
--
-- Why a child table and not jsonb on cre_listings: the shared OM writer records
-- a provenance row per extracted scalar (document, parser, confidence) and uses
-- this table for unit_mix / rent_roll line items, which have no fixed arity. The
-- collector does not promote or write OM values; GetCREdata is the sole
-- production OM writer under the ownership contract. A re-parse is idempotent
-- on the unique key.
--
-- Registered in 000_run_all.sql AFTER 012 and BEFORE 014/006/005 (so the table
-- exists before 005, the view migration, exposes it via v_cre_listings_full).
-- The collector keeps compatibility-only, to_regclass-guarded SQL for archive
-- handling, but it stages an empty om_facts array by policy. A pre-apply daily /
-- enrich run is therefore a no-op for OM facts.
--
-- Requires: 001_cre_brokerages.sql, 002_cre_listings.sql (cre_listings).
-- 003..012 run before this in 000_run_all.sql.
--
-- Security posture mirrors existing collector-owned tables: RLS enabled, no
-- public row policies. Service-role / direct-postgres bypasses RLS. Do NOT grant
-- anon/authenticated access (see SUPABASE_SECURITY_NOTE_2026-06-12.md).
-- =============================================================================

-- ---------------------------------------------------------------------------
-- cre_listing_om_facts -- OM/PDF-parsed facts with parse provenance. One row per
-- (listing, fact_group, fact_key, source_doc_url, parser_version). fact_group
-- 'scalar' rows are writer-defined facts. 'unit_mix' / 'rent_roll' rows are
-- non-scalar line items that have no cre_listings column. Every row carries
-- provenance (source_doc_url, parsed_at, parser_version, confidence) so every
-- OM-derived datum is traceable to a parsed page.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credeals.cre_listing_om_facts (
    id              uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    listing_id      uuid        NOT NULL REFERENCES credeals.cre_listings(id) ON DELETE CASCADE,
    fact_group      text        NOT NULL DEFAULT 'scalar'
                                CHECK (fact_group IN ('scalar', 'unit_mix', 'rent_roll')),
    fact_key        text        NOT NULL,        -- e.g. 'noi','cap_rate','unit_type','tenant'
    fact_value_text text,
    fact_value_num  numeric,
    unit_count      integer,                     -- unit_mix: # of units of this type
    -- provenance (required on every OM-derived row)
    source_doc_url  text        NOT NULL,        -- the parsed document URL
    parsed_at       timestamptz NOT NULL DEFAULT now(),
    parser_version  text        NOT NULL,        -- e.g. 'om-parse/1'
    confidence      numeric     CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cre_listing_om_facts_listing_idx
    ON credeals.cre_listing_om_facts (listing_id);
-- NULLS NOT DISTINCT so the dedup holds even if a (defensive) NULL leaked into a
-- key column; a plain UNIQUE would treat NULLs as all-distinct and let duplicate
-- fact rows accumulate (mirrors the 011 media/links unique-index convention).
-- The parser version is part of the key so parser generations can coexist and
-- be audited without overwriting one another. Migration 015 aligns existing
-- installations created with the former four-column key.
CREATE UNIQUE INDEX IF NOT EXISTS cre_listing_om_facts_uq
    ON credeals.cre_listing_om_facts (listing_id, fact_group, fact_key, source_doc_url, parser_version) NULLS NOT DISTINCT;

ALTER TABLE credeals.cre_listing_om_facts ENABLE ROW LEVEL SECURITY;  -- collector-owned; RLS on, no public policy (see 001).

COMMENT ON TABLE credeals.cre_listing_om_facts IS
    'OM/PDF-parsed facts (scalar underwriting + unit_mix + rent_roll) with parse provenance. The listing collector owns schema migration; GetCREdata documents pipeline is the sole production writer under the proposed CREDEALS_OWNERSHIP.md contract, and formal cross-repository approval remains required. The collector does not write or promote OM values. Service-role only (RLS on, no public policy).';
COMMENT ON COLUMN credeals.cre_listing_om_facts.fact_group     IS 'Row class: ''scalar'' (a writer-defined scalar fact), ''unit_mix'' (one row per unit type), or ''rent_roll'' (one row per tenant line).';
COMMENT ON COLUMN credeals.cre_listing_om_facts.fact_key       IS 'Stable snake_case fact name, e.g. ''noi'',''cap_rate'',''unit_type'',''tenant''. For scalar rows this matches the cre_listings column name.';
COMMENT ON COLUMN credeals.cre_listing_om_facts.fact_value_text IS 'Free-text value of the fact when not numeric (e.g. tenant name, unit_type label).';
COMMENT ON COLUMN credeals.cre_listing_om_facts.fact_value_num IS 'Numeric value of the fact when numeric (e.g. parsed noi, cap_rate, monthly_rent).';
COMMENT ON COLUMN credeals.cre_listing_om_facts.unit_count     IS 'For a unit_mix row: count of units of this type. NULL for scalar / rent_roll rows.';
COMMENT ON COLUMN credeals.cre_listing_om_facts.source_doc_url IS 'URL of the parsed document this fact was extracted from. Required: every OM-derived row is traceable to a doc.';
COMMENT ON COLUMN credeals.cre_listing_om_facts.parsed_at      IS 'When the parse that produced this row ran.';
COMMENT ON COLUMN credeals.cre_listing_om_facts.parser_version IS 'Parser version tag (e.g. ''om-parse/1'') so a re-parse with a newer parser is attributable.';
COMMENT ON COLUMN credeals.cre_listing_om_facts.confidence     IS 'Heuristic parse confidence in [0,1], interpreted by the approved external writer.';

-- ---------------------------------------------------------------------------
-- cre_listing_om_facts_archive -- bounded, durable history slice. The
-- mark-missing reconciliation snapshots a retired listing's FINAL om_facts here
-- in the SAME transaction as the soft-delete, so the parsed underwriting + line
-- items survive the next re-scrape's child replace (mirrors the 009/011
-- contacts/documents/media/links archive). No FK to cre_listings: the archive
-- must outlive a future hard delete of the source row. The cre_ingest.py archive
-- INSERT is to_regclass-guarded exactly like the 011 media/links archive.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credeals.cre_listing_om_facts_archive (
    id                uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    source_listing_id uuid        NOT NULL,
    archived_at       timestamptz NOT NULL DEFAULT now(),
    fact_group        text,
    fact_key          text,
    fact_value_text   text,
    fact_value_num    numeric,
    unit_count        integer,
    source_doc_url    text,
    parsed_at         timestamptz,
    parser_version    text,
    confidence        numeric
);

CREATE INDEX IF NOT EXISTS cre_listing_om_facts_archive_listing_idx
    ON credeals.cre_listing_om_facts_archive (source_listing_id, archived_at DESC);

ALTER TABLE credeals.cre_listing_om_facts_archive ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE credeals.cre_listing_om_facts_archive IS
    'Append-only snapshot of a listing''s final OM-parsed facts, captured by cre_ingest.py mark-missing at retirement. No FK: survives a later hard delete of the source listing.';
