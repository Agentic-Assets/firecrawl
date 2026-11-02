-- =============================================================================
-- 021_cre_market_summary_structure_gate.sql
-- Part of AGENTIC-5129 (D1 view half; review findings D-20 and D-21).
--
-- credeals.v_cre_market_summary averaged every stored sale price and every
-- stored sale_price_per_sf, so one junk amount set a market's figure: on
-- 2026-11-02 Washington DC "other" published avg_price 2.475e37 and Islandia
-- NY industrial an avg_price_per_sf of 52,742,048. This migration makes each
-- price leg read the GetCREdata listing-structure gates:
--
--   avg_price           sale price where is_property_price
--   avg_price_per_sf    sale_price_usd / size_sf where psf_allowed and the
--                       ratio is inside the shared band (non-land $5 to
--                       $3,000, land $0.10 to $1,000); the stored
--                       sale_price_per_sf column is no longer read
--   median_cap_rate     cap rate where cap_rate_allowed
--   median_price_per_sf NEW, appended last: the median of the same banded
--                       ratios (the figure EQUIRE shows)
--
-- A listing with no structure row (not yet classified, or lease-only) adds to
-- the counts, size and occupancy, never to a price leg. The band equals the
-- default of EQUIRE's display band flags (listing-price-per-sf-min-usd and
-- siblings); a flag change does not reach this view. A row is land when its
-- property_type is land or its size basis is land_area, as in EQUIRE.
--
-- Shape: CREATE OR REPLACE keeps every existing column's name, type and
-- position and appends one column, so credeals.get_cre_market_summary(jsonb)
-- (EQUIRE, RETURNS SETOF this view, RETURN QUERY SELECT m.*) keeps working and
-- returns the new column too. No view depends on this one (live 2026-11-02).
-- The on-market predicate is 005's status IN ('active','under_contract',
-- 'pending'); the live view still filters status = 'active' from before the
-- Phase-2 widening, and all 137,596 undeleted rows are 'active', so the
-- predicate changes no value today.
--
-- This file is now the only definition of the view: 005 no longer creates it,
-- because the view reads a GetCREdata object and 005 runs before GetCREdata's
-- migrations on a fresh database.
--
-- Requires: 002, and GetCREdata sql/migrations/20261101_listing_structure_
-- classification.sql (credeals.v_cre_listing_structure_current). Apply after
-- GetCREdata 20261102_listing_structure_implausible_price.sql and its backfill
-- (AGENTIC-5115 D-8): until then a placeholder price such as $9.9e37 is still
-- classified a property price and reaches avg_price. Excluded from
-- 000_run_all.sql; psql only, with the approval variables below.
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
--     -v CRE_MARKET_SUMMARY_APPROVE=1 \
--     -v CRE_MARKET_SUMMARY_OPERATOR='<cayman-or-stace>' \
--     -v CRE_MARKET_SUMMARY_APPROVAL_REF='AGENTIC-5129' \
--     -v CRE_MARKET_SUMMARY_CONFIRM='APPLY 021_cre_market_summary_structure_gate' \
--     -f 021_cre_market_summary_structure_gate.sql
--
-- Preview and read-back: 021_cre_market_summary_structure_gate_readback.sql
-- (read only; run before apply for the old values and after for the new).
-- Rollback: 021_cre_market_summary_structure_gate_rollback.sql restores the
-- prior expressions and keeps median_price_per_sf as a null column, because
-- CREATE OR REPLACE cannot drop a column and DROP VIEW would first need the
-- EQUIRE function get_cre_market_summary dropped.
-- =============================================================================

\set ON_ERROR_STOP on
\if :{?CRE_MARKET_SUMMARY_APPROVE}
\else
  \set CRE_MARKET_SUMMARY_APPROVE 0
\endif
\if :{?CRE_MARKET_SUMMARY_OPERATOR}
\else
  \set CRE_MARKET_SUMMARY_OPERATOR ''
\endif
\if :{?CRE_MARKET_SUMMARY_APPROVAL_REF}
\else
  \set CRE_MARKET_SUMMARY_APPROVAL_REF ''
\endif
\if :{?CRE_MARKET_SUMMARY_CONFIRM}
\else
  \set CRE_MARKET_SUMMARY_CONFIRM ''
\endif
SELECT (:'CRE_MARKET_SUMMARY_APPROVE' = '1'
 AND :'CRE_MARKET_SUMMARY_OPERATOR' IN ('cayman', 'stace')
 AND :'CRE_MARKET_SUMMARY_APPROVAL_REF' ~ '^AGENTIC-[0-9]+$'
 AND :'CRE_MARKET_SUMMARY_CONFIRM' = 'APPLY 021_cre_market_summary_structure_gate') AS authorized \gset
\if :authorized
\else
  DO $$ BEGIN RAISE EXCEPTION '021 requires operator, issue and exact confirmation'; END $$;
\endif

BEGIN;
SET LOCAL lock_timeout = '5s';

DO $guard$
BEGIN
    IF to_regclass('credeals.v_cre_listing_structure_current') IS NULL THEN
        RAISE EXCEPTION '021 needs credeals.v_cre_listing_structure_current (GetCREdata 20261101_listing_structure_classification.sql)';
    END IF;
    IF (SELECT count(*) FROM pg_attribute
         WHERE attrelid = 'credeals.v_cre_listing_structure_current'::regclass
           AND attname IN ('listing_id', 'is_property_price', 'psf_allowed', 'cap_rate_allowed', 'size_basis')
           AND NOT attisdropped) <> 5 THEN
        RAISE EXCEPTION '021 needs the listing_id, gate and size_basis columns of credeals.v_cre_listing_structure_current';
    END IF;
END
$guard$;

CREATE OR REPLACE VIEW credeals.v_cre_market_summary
WITH (security_invoker = true) AS
SELECT
    l.city,
    l.state,
    l.property_type,
    count(*)                                                                 AS listing_count,
    count(*) FILTER (WHERE l.transaction_type IN ('sale', 'sale_or_lease'))  AS for_sale_count,
    count(*) FILTER (WHERE l.transaction_type IN ('lease', 'sale_or_lease')) AS for_lease_count,
    round(avg(l.sale_price_usd) FILTER (WHERE s.is_property_price))          AS avg_price,
    round(avg(ratio.price_per_sf), 2)                                        AS avg_price_per_sf,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY l.cap_rate::double precision)
          FILTER (WHERE l.cap_rate IS NOT NULL AND s.cap_rate_allowed)::numeric, 4) AS median_cap_rate,
    round(avg(l.size_sf) FILTER (WHERE l.size_sf IS NOT NULL))               AS avg_size_sf,
    round(avg(l.occupancy_rate) FILTER (WHERE l.occupancy_rate IS NOT NULL), 4) AS avg_occupancy_rate,
    max(l.scraped_at)                                                        AS last_scraped_at,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY ratio.price_per_sf::double precision)::numeric, 2)
                                                                             AS median_price_per_sf
FROM credeals.cre_listings l
LEFT JOIN credeals.v_cre_listing_structure_current s ON s.listing_id = l.id
CROSS JOIN LATERAL (
    SELECT CASE
               WHEN s.psf_allowed AND l.sale_price_usd > 0 AND l.size_sf > 0
                    AND l.sale_price_usd / l.size_sf BETWEEN band.low AND band.high
                   THEN l.sale_price_usd / l.size_sf
           END AS price_per_sf
      FROM (SELECT CASE WHEN l.property_type = 'land' OR s.size_basis = 'land_area' THEN 0.10 ELSE 5 END AS low,
                   CASE WHEN l.property_type = 'land' OR s.size_basis = 'land_area' THEN 1000 ELSE 3000 END AS high) band
) ratio
WHERE l.deleted_at IS NULL
  AND l.status IN ('active', 'under_contract', 'pending')
  AND l.city IS NOT NULL
  AND l.state IS NOT NULL
GROUP BY l.city, l.state, l.property_type;

COMMENT ON VIEW credeals.v_cre_market_summary IS 'Per-(city,state,property_type) aggregates over on-market listings (active/under_contract/pending): counts, size and occupancy over every listing; price legs only where the GetCREdata listing-structure gates allow (avg_price: is_property_price; avg_price_per_sf and median_price_per_sf: psf_allowed and inside the band, non-land $5 to $3,000, land $0.10 to $1,000; median_cap_rate: cap_rate_allowed). 021_cre_market_summary_structure_gate.sql, AGENTIC-5129.';
COMMENT ON COLUMN credeals.v_cre_market_summary.avg_price_per_sf IS 'Mean of sale_price_usd / size_sf over listings whose psf_allowed gate holds and whose ratio is inside the band. Not the stored sale_price_per_sf.';
COMMENT ON COLUMN credeals.v_cre_market_summary.median_price_per_sf IS 'Median of the same banded ratios as avg_price_per_sf. Appended by 021 (AGENTIC-5129).';

REVOKE ALL ON credeals.v_cre_market_summary FROM PUBLIC;
DO $grants$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON credeals.v_cre_market_summary FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON credeals.v_cre_market_summary FROM authenticated;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT SELECT ON credeals.v_cre_market_summary TO service_role;
    END IF;
END
$grants$;

COMMIT;
