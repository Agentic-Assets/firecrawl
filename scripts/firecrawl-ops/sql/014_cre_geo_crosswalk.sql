-- =============================================================================
-- 014_cre_geo_crosswalk.sql
-- CRE Listing Intelligence for EQUIRE
-- Supabase project: supabase-agentic-assets-v2 (fhqycqubkkrdgzswccwd)
--
-- Phase-2 data-lift WS4: the offline ZIP -> county + CBSA reference table that
-- backs geo derivation (cbsa_code / cbsa_name / county on cre_listings). Pure
-- US-Census public-domain reference data (the ZCTA5<->county relationship file
-- joined to the Census CBSA-to-county delineation, with ZCTA gazetteer
-- centroids), distilled to one committed CSV. (HUD's USPS crosswalk was dropped:
-- its portal download is bot-gated and its API needs a per-user token, neither
-- reproducible unattended; the all-Census path needs no token. See
-- data/build_zip_cbsa_crosswalk.py.) Spec:
-- cre_collector/PHASE2_DATA_LIFT_CONTRACT_2026-06-15.md Section E.
--
-- ADDITIVE ONLY and idempotent (CREATE TABLE IF NOT EXISTS). Pure reference data:
-- no FK to cre_listings (a listing is not deleted when a ZIP row changes, and a
-- crosswalk reload must not cascade). Nothing here alters / drops / renames an
-- existing object.
--
-- Source-of-truth note: the SAME committed CSV
-- (cre_collector/data/zip_cbsa_crosswalk.csv) is loaded directly from disk by the
-- Python path (cre_geo.py / the 87k backfill) with no DB round-trip; this table
-- is for consumer / ad-hoc SQL joins. They are byte-identical (one committed
-- file, one deterministic build script data/build_zip_cbsa_crosswalk.py). The
-- CSV is deduped to one row per ZIP (the county holding the largest land-area
-- share of the ZCTA, per Census AREALAND_PART, for a split ZIP), so zip5 is
-- UNIQUE; id uuid is the PK because zip5-as-PK would be wrong for the pre-dedup
-- multi-county shape and a surrogate key is safer for reloads.
--
-- Registered in 000_run_all.sql AFTER 013 and BEFORE 006/005. 005 does not read
-- this table today (geo derivation runs in Python), so its position relative to
-- 005 is not load-bearing; it is placed with the other Phase-2 migrations.
--
-- LOAD: the \copy below is INERT under 000_run_all.sql until the committed CSV
-- exists at the relative path; run it from scripts/firecrawl-ops/sql (psql
-- resolves \copy paths relative to the client cwd). It is commented out in this
-- file so 000_run_all.sql never fails on a missing CSV during a schema-only
-- apply; the geo agent runs the \copy explicitly after committing the CSV. The
-- TRUNCATE-then-load keeps a reload idempotent and deterministic.
--
-- Requires: nothing beyond the credeals schema (no FK). 001..013 run before this
-- in 000_run_all.sql by convention, not by dependency.
--
-- Security posture mirrors existing collector-owned tables: RLS enabled, no
-- public row policies. Service-role / direct-postgres bypasses RLS. The crosswalk
-- is public-domain data but the table stays service-role-only for consistency
-- (see SUPABASE_SECURITY_NOTE_2026-06-12.md).
-- =============================================================================

-- ---------------------------------------------------------------------------
-- cre_zip_cbsa_crosswalk -- one row per US ZIP. county_fips / county_name /
-- state from the Census ZCTA5<->county relationship file (largest land-area
-- county for a split ZIP); cbsa_code / cbsa_name from the Census CBSA-to-county
-- delineation; centroid_lat / centroid_lng from the Census ZCTA gazetteer for
-- the lat/lng nearest-centroid fallback match. Reference data, no FK to listings.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credeals.cre_zip_cbsa_crosswalk (
    id           uuid             DEFAULT gen_random_uuid() PRIMARY KEY,
    zip5         text             NOT NULL,
    county_fips  text,
    county_name  text,
    state        char(2),
    cbsa_code    text,
    cbsa_name    text,
    centroid_lat double precision,
    centroid_lng double precision,
    created_at   timestamptz      NOT NULL DEFAULT now()
);

-- One row per ZIP after the largest-land-area (AREALAND_PART) dedup. NULLS NOT
-- DISTINCT keeps the dedup safe if a defensive NULL ever reaches zip5 (mirrors
-- the 011 convention).
CREATE UNIQUE INDEX IF NOT EXISTS cre_zip_cbsa_crosswalk_zip5_uq
    ON credeals.cre_zip_cbsa_crosswalk (zip5) NULLS NOT DISTINCT;
-- Covering index for the CBSA-join consumer queries (group listings by metro).
CREATE INDEX IF NOT EXISTS cre_zip_cbsa_crosswalk_cbsa_idx
    ON credeals.cre_zip_cbsa_crosswalk (cbsa_code);

ALTER TABLE credeals.cre_zip_cbsa_crosswalk ENABLE ROW LEVEL SECURITY;  -- reference data; RLS on, no public policy (see 001).

COMMENT ON TABLE credeals.cre_zip_cbsa_crosswalk IS
    'Offline ZIP -> county + CBSA reference (Census ZCTA5<->county relationship x Census CBSA delineation x ZCTA gazetteer centroids, US-Census public domain). One row per ZIP (largest land-area county per Census AREALAND_PART for a split ZIP). Byte-identical to cre_collector/data/zip_cbsa_crosswalk.csv. Service-role only.';
COMMENT ON COLUMN credeals.cre_zip_cbsa_crosswalk.zip5         IS '5-digit US ZIP (Census ZCTA5). Unique after the largest-land-area (AREALAND_PART) dedup.';
COMMENT ON COLUMN credeals.cre_zip_cbsa_crosswalk.county_fips  IS '5-digit county FIPS (state+county) for the ZIP''s primary county.';
COMMENT ON COLUMN credeals.cre_zip_cbsa_crosswalk.county_name  IS 'County name for the ZIP''s primary county (e.g. ''Dallas County'').';
COMMENT ON COLUMN credeals.cre_zip_cbsa_crosswalk.state        IS '2-letter USPS state abbreviation.';
COMMENT ON COLUMN credeals.cre_zip_cbsa_crosswalk.cbsa_code    IS '5-digit CBSA (metro/micro) code from the Census delineation. NULL for a non-CBSA (rural) ZIP.';
COMMENT ON COLUMN credeals.cre_zip_cbsa_crosswalk.cbsa_name    IS 'CBSA name (e.g. ''Dallas-Fort Worth-Arlington, TX''). NULL for a non-CBSA ZIP.';
COMMENT ON COLUMN credeals.cre_zip_cbsa_crosswalk.centroid_lat IS 'ZIP centroid latitude for the lat/lng nearest-centroid fallback match.';
COMMENT ON COLUMN credeals.cre_zip_cbsa_crosswalk.centroid_lng IS 'ZIP centroid longitude for the lat/lng nearest-centroid fallback match.';

-- ---------------------------------------------------------------------------
-- DATA LOAD (run explicitly from scripts/firecrawl-ops/sql AFTER the CSV is
-- committed; intentionally COMMENTED OUT so 000_run_all.sql never fails on a
-- missing file during a schema-only apply). TRUNCATE-then-\copy keeps the reload
-- deterministic and idempotent. The CSV header columns MUST be exactly:
-- zip5, county_fips, county_name, state, cbsa_code, cbsa_name, centroid_lat, centroid_lng
--
--   TRUNCATE credeals.cre_zip_cbsa_crosswalk;
--   \copy credeals.cre_zip_cbsa_crosswalk (zip5, county_fips, county_name, state, cbsa_code, cbsa_name, centroid_lat, centroid_lng) FROM '../cre_collector/data/zip_cbsa_crosswalk.csv' WITH (FORMAT csv, HEADER true)
-- ---------------------------------------------------------------------------
