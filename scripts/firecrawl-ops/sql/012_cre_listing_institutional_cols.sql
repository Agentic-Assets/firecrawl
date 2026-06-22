-- =============================================================================
-- 012_cre_listing_institutional_cols.sql
-- CRE Listing Intelligence for EQUIRE
-- Supabase project: supabase-agentic-assets-v2 (fhqycqubkkrdgzswccwd)
--
-- Phase-2 data-lift: additive high-value institutional columns on cre_listings,
-- the WS4 geo-derivation columns, the WS3 broker-license column on
-- cre_listing_contacts, and the single extra_facts jsonb long-tail blob.
-- Spec: cre_collector/PHASE2_DATA_LIFT_CONTRACT_2026-06-15.md Section A.4.
--
-- ADDITIVE ONLY and idempotent. Every cre_listings/cre_listing_contacts change
-- is ADD COLUMN IF NOT EXISTS; every CHECK is added only when absent via the
-- 002 status-CHECK / range-CHECK guarded template (DO $$ ... pg_get_constraintdef
-- / IF NOT EXISTS (SELECT 1 FROM pg_constraint ...) $$), so a re-run on a
-- populated table never takes ACCESS EXCLUSIVE + a full validating scan twice.
-- No TABLE, COLUMN, or DATA is dropped, renamed, or narrowed.
--
-- Columns are nullable with no default (except extra_facts DEFAULT '{}'::jsonb)
-- so a 0%-filled column reads as 0% in the coverage report, never as
-- "populated". cap_rate / noi / occupancy_rate / units / year_built /
-- gross_revenue / size_sf / lease_rate_* / sale_price_* ALREADY EXIST (002) and
-- are REUSED via the COALESCE-keep upsert path; this file adds no column for them.
-- county / market / submarket / canonical_url ALREADY EXIST (002); WS1/WS4
-- populate them and this file adds no column for those four either.
--
-- Registered in 000_run_all.sql AFTER 011 and BEFORE 013/014/006/005. 006 also
-- ALTERs cre_listing_contacts; both use ADD COLUMN IF NOT EXISTS so order is
-- immaterial. 005 re-creates v_cre_listings_full LAST so the view sees these
-- new columns (l.* freezes to columns present when the view CREATE runs).
--
-- Requires: 001_cre_brokerages.sql, 002_cre_listings.sql (cre_listings,
--           cre_listing_contacts). 003..011 run before this in 000_run_all.sql.
--
-- Security posture: no new tables here, so RLS is unchanged. cre_listings and
-- cre_listing_contacts already have RLS enabled with no public policy (002).
-- =============================================================================

-- ---------------------------------------------------------------------------
-- cre_listings: WS3 discrete institutional columns + WS4 geo columns +
-- extra_facts long-tail jsonb. Every statement is ADD COLUMN IF NOT EXISTS.
-- CHECKs are added separately (guarded) below so a re-run does not re-validate.
-- ---------------------------------------------------------------------------
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS building_class        text;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS property_subtype      text;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS apn                   text;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS tenant_name           text;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS guarantor             text;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS lease_years_remaining numeric;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS price_per_unit        numeric;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS grm                   numeric;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS price_per_acre        numeric;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS num_rooms             integer;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS revpar                numeric;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS clear_height_ft       numeric;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS dock_doors            integer;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS drive_in_doors        integer;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS power_service         text;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS rail_served           boolean;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS cbsa_code             text;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS cbsa_name             text;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS geo_source            text;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS extra_facts           jsonb DEFAULT '{}'::jsonb;

COMMENT ON COLUMN credeals.cre_listings.building_class        IS 'Building class A/B/C/D when the source states it (JLL buildingClass, Transwestern Class, NAI tags, AY subtype). NULL = unstated, never inferred.';
COMMENT ON COLUMN credeals.cre_listings.property_subtype      IS 'Source-stated property subtype string (e.g. ''Warehouse/Distribution'', ''office.medical''). Finer than property_type; not an enum; length-capped to 96 in ingest.';
COMMENT ON COLUMN credeals.cre_listings.apn                   IS 'Assessor parcel number / parcel id when the source exposes it (Transwestern Parcel, OM cover). Free text; not validated.';
COMMENT ON COLUMN credeals.cre_listings.tenant_name           IS 'Single-tenant net-lease tenant name (M&M ''Tenant Name'').';
COMMENT ON COLUMN credeals.cre_listings.guarantor             IS 'Lease guarantor / credit entity (M&M ''Guarantor'').';
COMMENT ON COLUMN credeals.cre_listings.lease_years_remaining IS 'Years remaining on the in-place lease (M&M ''Years Remaining On Lease''). Range guard [0,99].';
COMMENT ON COLUMN credeals.cre_listings.price_per_unit        IS 'Sale price per unit (M&M ''Price/Unit''). USD. Must be > 0 when present.';
COMMENT ON COLUMN credeals.cre_listings.grm                   IS 'Gross rent multiplier (M&M ''GRM''). Range guard (0,100).';
COMMENT ON COLUMN credeals.cre_listings.price_per_acre        IS 'Sale price per acre (M&M ''Price/Acre''). USD. Must be > 0 when present.';
COMMENT ON COLUMN credeals.cre_listings.num_rooms             IS 'Hotel room count (M&M ''Number of Rooms''). Must be > 0 when present.';
COMMENT ON COLUMN credeals.cre_listings.revpar                IS 'Hotel revenue per available room (M&M ''RevPAR''). USD. Must be > 0 when present.';
COMMENT ON COLUMN credeals.cre_listings.clear_height_ft       IS 'Industrial clear height in feet (Transwestern). Range guard (0,200).';
COMMENT ON COLUMN credeals.cre_listings.dock_doors            IS 'Dock-high door count (industrial). Must be >= 0 when present.';
COMMENT ON COLUMN credeals.cre_listings.drive_in_doors        IS 'Drive-in / grade-level door count (industrial). Must be >= 0 when present.';
COMMENT ON COLUMN credeals.cre_listings.power_service         IS 'Electrical service description (e.g. ''2000A 480V''). Free text; length-capped to 128 in ingest.';
COMMENT ON COLUMN credeals.cre_listings.rail_served           IS 'True when the property is rail-served (industrial). NULL = unstated.';
COMMENT ON COLUMN credeals.cre_listings.cbsa_code             IS '5-digit CBSA (metro market) code from the offline ZIP->CBSA crosswalk. Geo-derived, not scraped.';
COMMENT ON COLUMN credeals.cre_listings.cbsa_name             IS 'CBSA (metro market) name from the crosswalk (e.g. ''Dallas-Fort Worth-Arlington, TX''). Geo-derived.';
COMMENT ON COLUMN credeals.cre_listings.geo_source            IS 'Provenance of derived county/market/submarket: ''source'' (broker gave it verbatim, e.g. Newmark), ''crosswalk_zip'', or ''crosswalk_latlng''. NULL = no geo derivation ran.';
COMMENT ON COLUMN credeals.cre_listings.extra_facts           IS 'Long-tail source facts with no discrete column and no consumer query need. snake_case keys. Additive: merged (jsonb ||) on upsert with a null/empty guard so a sparse pass never clobbers a prior blob.';

-- ---------------------------------------------------------------------------
-- Range / enum CHECKs for the new numeric/text columns. Guarded exactly like
-- the 002 cap_rate/occupancy_rate range CHECKs (002:211-225): added ONLY when
-- absent, so the first apply validates the (empty) column once and a re-run is a
-- pure no-op (no ACCESS EXCLUSIVE, no re-scan). All are NULL-permissive so an
-- unpopulated column never fails the constraint.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid='credeals.cre_listings'::regclass
                     AND conname='cre_listings_building_class_check') THEN
        ALTER TABLE credeals.cre_listings ADD CONSTRAINT cre_listings_building_class_check
            CHECK (building_class IS NULL OR building_class IN ('A','B','C','D'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid='credeals.cre_listings'::regclass
                     AND conname='cre_listings_lease_years_remaining_check') THEN
        ALTER TABLE credeals.cre_listings ADD CONSTRAINT cre_listings_lease_years_remaining_check
            CHECK (lease_years_remaining IS NULL OR (lease_years_remaining >= 0 AND lease_years_remaining <= 99));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid='credeals.cre_listings'::regclass
                     AND conname='cre_listings_price_per_unit_check') THEN
        ALTER TABLE credeals.cre_listings ADD CONSTRAINT cre_listings_price_per_unit_check
            CHECK (price_per_unit IS NULL OR price_per_unit > 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid='credeals.cre_listings'::regclass
                     AND conname='cre_listings_grm_check') THEN
        ALTER TABLE credeals.cre_listings ADD CONSTRAINT cre_listings_grm_check
            CHECK (grm IS NULL OR (grm > 0 AND grm < 100));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid='credeals.cre_listings'::regclass
                     AND conname='cre_listings_price_per_acre_check') THEN
        ALTER TABLE credeals.cre_listings ADD CONSTRAINT cre_listings_price_per_acre_check
            CHECK (price_per_acre IS NULL OR price_per_acre > 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid='credeals.cre_listings'::regclass
                     AND conname='cre_listings_num_rooms_check') THEN
        ALTER TABLE credeals.cre_listings ADD CONSTRAINT cre_listings_num_rooms_check
            CHECK (num_rooms IS NULL OR num_rooms > 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid='credeals.cre_listings'::regclass
                     AND conname='cre_listings_revpar_check') THEN
        ALTER TABLE credeals.cre_listings ADD CONSTRAINT cre_listings_revpar_check
            CHECK (revpar IS NULL OR revpar > 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid='credeals.cre_listings'::regclass
                     AND conname='cre_listings_clear_height_ft_check') THEN
        ALTER TABLE credeals.cre_listings ADD CONSTRAINT cre_listings_clear_height_ft_check
            CHECK (clear_height_ft IS NULL OR (clear_height_ft > 0 AND clear_height_ft < 200));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid='credeals.cre_listings'::regclass
                     AND conname='cre_listings_dock_doors_check') THEN
        ALTER TABLE credeals.cre_listings ADD CONSTRAINT cre_listings_dock_doors_check
            CHECK (dock_doors IS NULL OR dock_doors >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid='credeals.cre_listings'::regclass
                     AND conname='cre_listings_drive_in_doors_check') THEN
        ALTER TABLE credeals.cre_listings ADD CONSTRAINT cre_listings_drive_in_doors_check
            CHECK (drive_in_doors IS NULL OR drive_in_doors >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid='credeals.cre_listings'::regclass
                     AND conname='cre_listings_geo_source_check') THEN
        ALTER TABLE credeals.cre_listings ADD CONSTRAINT cre_listings_geo_source_check
            CHECK (geo_source IS NULL OR geo_source IN ('source','crosswalk_zip','crosswalk_latlng'));
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- cre_listing_contacts: WS3 broker real-estate license string. title already
-- exists (002). ADD COLUMN IF NOT EXISTS; no CHECK (free text, format varies by
-- state, e.g. 'IL: 475.188007'). The matching contacts INSERT in cre_ingest.py
-- gains the license column in the same change.
-- ---------------------------------------------------------------------------
ALTER TABLE credeals.cre_listing_contacts ADD COLUMN IF NOT EXISTS license text;

COMMENT ON COLUMN credeals.cre_listing_contacts.license IS 'Broker real-estate license string as printed (e.g. ''IL: 475.188007''). Free text; not validated. title already exists (002).';
