-- =============================================================================
-- 021_cre_market_summary_structure_gate.sql
-- Part of AGENTIC-5129 (D1 view half; review findings D-20 and D-21, narrowed by
-- the D1 group skeptic review on 2026-11-02).
--
-- credeals.v_cre_market_summary averaged every stored sale price and every
-- stored sale_price_per_sf, so one junk amount set a market's figure: on
-- 2026-11-02 Washington DC "other" published avg_price 2.475e37 and Islandia
-- NY industrial an avg_price_per_sf of 52,742,048. It also published a median
-- from any number of listings: 6,273 of 9,294 market $/SF figures rested on one
-- listing. This migration makes each price leg the producer's own market
-- evidence:
--
--   price_per_sf leg  sale_price_usd / size_sf where GetCREdata's leg gate
--                     credeals.cre_listing_market_evidence_exclusion_v1('price_per_sf', ...)
--                     admits the listing, psf_allowed holds and the ratio is
--                     inside the band (non-land $5 to $3,000, land $0.10 to
--                     $1,000); the stored sale_price_per_sf is no longer read
--   cap_rate leg      cap_rate where the same gate admits it for 'cap_rate'
--                     (AGENTIC-5127: it excludes leased fee, condo units and
--                     portfolios and keeps an unpriced listing's stated cap rate)
--   size leg          size_sf where the size basis is not unresolved
--   avg_price         sale price where is_property_price
--
-- Every average and median is null below 3 values, the floor GetCREdata's
-- asking legs (cre_market_index) and EQUIRE's MIN_QUALIFIED_MEDIAN_N use, and
-- the counts are published so a reader can say "2 listings, too few":
--
--   avg_price_per_sf, median_price_per_sf   price_per_sf_n >= 3
--   median_cap_rate                         cap_rate_n >= 3
--   median_size_sf                          size_sf_n >= 3
--   avg_price                               3 or more property prices
--
-- Ratios above $3,000/SF, mostly drive-thru pads under 1,000 SF, are excluded,
-- as the EQUIRE display band excludes them; a size-aware band is a later change
-- to both owners together. The band equals the default of EQUIRE's display band
-- flags (listing-price-per-sf-min-usd and siblings); a flag change does not reach
-- this view. A row is land when its property_type is land or its size basis is
-- land_area, as in EQUIRE. A listing with no structure row (not yet classified,
-- or lease-only) adds to the counts, the size leg and occupancy, never to a price
-- leg. avg_size_sf and avg_occupancy_rate are unchanged legacy columns; EQUIRE
-- reads median_size_sf.
--
-- Deviation from the D-20 wording ("cap_rate_allowed"): the cap leg uses the
-- shared leg gate instead, so this view and cre_market_index agree; a stricter
-- column (market_evidence_eligible) would drop 365 unpriced cap rates the gate
-- admits.
--
-- Shape: CREATE OR REPLACE keeps every existing column's name, type and
-- position and appends five, so credeals.get_cre_market_summary(jsonb)
-- (EQUIRE, RETURNS SETOF this view, RETURN QUERY SELECT m.*) keeps working and
-- returns them too. No view depends on this one (live 2026-11-02).
-- The on-market predicate is 005's status IN ('active','under_contract',
-- 'pending'); the live view still filters status = 'active' from before the
-- Phase-2 widening, and all 137,596 undeleted rows are 'active', so the
-- predicate changes no value today.
--
-- This file is now the only definition of the view: 005 no longer creates it,
-- because the view reads GetCREdata objects and 005 runs before GetCREdata's
-- migrations on a fresh database.
--
-- Requires: 002; GetCREdata sql/migrations/20261101_listing_structure_classification.sql
-- (credeals.v_cre_listing_structure_current) and
-- 20261101_listing_structure_market_evidence.sql (the leg gate); and GetCREdata
-- 20261102_listing_structure_implausible_price.sql applied and backfilled
-- (AGENTIC-5115 D-8). The guard below refuses to run until the refresh names
-- lsc-2026.11.1 and no unreviewed current row is on another classifier version,
-- because before D-8 a placeholder price such as $9.9e37 is still a property
-- price. Excluded from 000_run_all.sql; psql only, with the approval variables
-- below.
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
-- prior expressions and keeps the five appended columns as nulls, because
-- CREATE OR REPLACE cannot drop a column and DROP VIEW would first need the
-- EQUIRE function get_cre_market_summary dropped. EQUIRE hides a figure whose
-- count is null, so after a rollback it shows no median and claims no gate.
-- Roll this back before GetCREdata's D-8 rollback (its guard needs D-8).
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
DECLARE
    v_refresh oid := to_regprocedure('credeals.refresh_cre_listing_structure_classifications(boolean, text, integer)');
BEGIN
    IF to_regclass('credeals.v_cre_listing_structure_current') IS NULL THEN
        RAISE EXCEPTION '021 needs credeals.v_cre_listing_structure_current (GetCREdata 20261101_listing_structure_classification.sql)';
    END IF;
    IF (SELECT count(*) FROM pg_attribute
         WHERE attrelid = 'credeals.v_cre_listing_structure_current'::regclass
           AND attname IN ('listing_id', 'is_property_price', 'psf_allowed', 'size_basis', 'interest_kind', 'asset_scope',
                           'price_basis', 'structure_tags', 'structure_method', 'classifier_version', 'review_state')
           AND NOT attisdropped) <> 11 THEN
        RAISE EXCEPTION '021 needs the listing_id, gate, basis, method, version and review columns of credeals.v_cre_listing_structure_current';
    END IF;
    IF to_regprocedure('credeals.cre_listing_market_evidence_exclusion_v1(text, text, text, text, text, text, text[], text, text)') IS NULL THEN
        RAISE EXCEPTION '021 needs credeals.cre_listing_market_evidence_exclusion_v1 (GetCREdata 20261101_listing_structure_market_evidence.sql)';
    END IF;
    IF v_refresh IS NULL
       OR position('''lsc-2026.11.1''' in (SELECT prosrc FROM pg_proc WHERE oid = v_refresh)) = 0 THEN
        RAISE EXCEPTION '021 needs GetCREdata 20261102_listing_structure_implausible_price.sql applied: the refresh does not name lsc-2026.11.1';
    END IF;
    IF EXISTS (SELECT 1 FROM credeals.v_cre_listing_structure_current
                WHERE classifier_version <> 'lsc-2026.11.1'
                  AND review_state NOT IN ('reviewed_confirmed', 'reviewed_corrected')) THEN
        RAISE EXCEPTION '021 needs the lsc-2026.11.1 backfill finished: unreviewed current rows remain on an earlier classifier version';
    END IF;
END
$guard$;

CREATE OR REPLACE VIEW credeals.v_cre_market_summary
WITH (security_invoker = true) AS
WITH legs AS (
    SELECT l.city, l.state, l.property_type, l.transaction_type, l.sale_price_usd, l.size_sf,
           l.occupancy_rate, l.scraped_at,
           CASE WHEN s.is_property_price AND l.sale_price_usd IS NOT NULL THEN l.sale_price_usd END AS property_price,
           CASE WHEN s.psf_allowed AND l.sale_price_usd > 0 AND l.size_sf > 0
                     AND credeals.cre_listing_market_evidence_exclusion_v1('price_per_sf', l.transaction_type,
                             s.interest_kind, s.asset_scope, s.price_basis, s.size_basis, s.structure_tags,
                             s.structure_method->>'price_basis', s.structure_method->>'size_basis') IS NULL
                     AND l.sale_price_usd / l.size_sf
                         BETWEEN CASE WHEN l.property_type = 'land' OR s.size_basis = 'land_area' THEN 0.10 ELSE 5 END
                             AND CASE WHEN l.property_type = 'land' OR s.size_basis = 'land_area' THEN 1000 ELSE 3000 END
                THEN l.sale_price_usd / l.size_sf END AS price_per_sf,
           CASE WHEN l.cap_rate IS NOT NULL
                     AND credeals.cre_listing_market_evidence_exclusion_v1('cap_rate', l.transaction_type,
                             s.interest_kind, s.asset_scope, s.price_basis, s.size_basis, s.structure_tags,
                             s.structure_method->>'price_basis', s.structure_method->>'size_basis') IS NULL
                THEN l.cap_rate END AS cap_rate,
           CASE WHEN l.size_sf > 0 AND s.size_basis IS DISTINCT FROM 'unresolved' THEN l.size_sf END AS market_size_sf
      FROM credeals.cre_listings l
      LEFT JOIN credeals.v_cre_listing_structure_current s ON s.listing_id = l.id
     WHERE l.deleted_at IS NULL
       AND l.status IN ('active', 'under_contract', 'pending')
       AND l.city IS NOT NULL
       AND l.state IS NOT NULL
)
SELECT
    city,
    state,
    property_type,
    count(*)                                                                    AS listing_count,
    count(*) FILTER (WHERE transaction_type IN ('sale', 'sale_or_lease'))       AS for_sale_count,
    count(*) FILTER (WHERE transaction_type IN ('lease', 'sale_or_lease'))      AS for_lease_count,
    CASE WHEN count(property_price) >= 3 THEN round(avg(property_price)) END    AS avg_price,
    CASE WHEN count(price_per_sf) >= 3 THEN round(avg(price_per_sf), 2) END     AS avg_price_per_sf,
    CASE WHEN count(cap_rate) >= 3
         THEN round(percentile_cont(0.5) WITHIN GROUP (ORDER BY cap_rate::double precision)::numeric, 4) END
                                                                                AS median_cap_rate,
    round(avg(size_sf) FILTER (WHERE size_sf IS NOT NULL))                      AS avg_size_sf,
    round(avg(occupancy_rate) FILTER (WHERE occupancy_rate IS NOT NULL), 4)     AS avg_occupancy_rate,
    max(scraped_at)                                                             AS last_scraped_at,
    CASE WHEN count(price_per_sf) >= 3
         THEN round(percentile_cont(0.5) WITHIN GROUP (ORDER BY price_per_sf::double precision)::numeric, 2) END
                                                                                AS median_price_per_sf,
    count(price_per_sf)                                                         AS price_per_sf_n,
    count(cap_rate)                                                             AS cap_rate_n,
    CASE WHEN count(market_size_sf) >= 3
         THEN round(percentile_cont(0.5) WITHIN GROUP (ORDER BY market_size_sf::double precision)::numeric) END
                                                                                AS median_size_sf,
    count(market_size_sf)                                                       AS size_sf_n
FROM legs
GROUP BY city, state, property_type;

COMMENT ON VIEW credeals.v_cre_market_summary IS 'Per-(city,state,property_type) aggregates over on-market listings (active/under_contract/pending). Counts, avg_size_sf and avg_occupancy_rate cover every listing (avg_size_sf is legacy; read median_size_sf). Price legs are the producer market evidence: the GetCREdata leg gate cre_listing_market_evidence_exclusion_v1 per leg, psf_allowed and the band for $/SF (non-land $5 to $3,000, land $0.10 to $1,000; ratios above $3,000/SF, mostly drive-thru pads under 1,000 SF, are excluded), is_property_price for avg_price. Every average and median is null below 3 values; price_per_sf_n, cap_rate_n and size_sf_n are published at every n. 021_cre_market_summary_structure_gate.sql, AGENTIC-5129.';
COMMENT ON COLUMN credeals.v_cre_market_summary.avg_price IS 'Mean sale price of listings whose is_property_price gate holds; null below 3 such listings.';
COMMENT ON COLUMN credeals.v_cre_market_summary.avg_price_per_sf IS 'Mean of sale_price_usd / size_sf over the price_per_sf leg (leg gate, psf_allowed, band); null when price_per_sf_n is below 3. Not the stored sale_price_per_sf.';
COMMENT ON COLUMN credeals.v_cre_market_summary.median_cap_rate IS 'Median cap rate over the cap_rate leg (GetCREdata leg gate); null when cap_rate_n is below 3.';
COMMENT ON COLUMN credeals.v_cre_market_summary.avg_size_sf IS 'Legacy: mean of every stored size_sf, ungated, so one junk size sets it. Read median_size_sf.';
COMMENT ON COLUMN credeals.v_cre_market_summary.median_price_per_sf IS 'Median of the price_per_sf leg; null when price_per_sf_n is below 3. Appended by 021 (AGENTIC-5129).';
COMMENT ON COLUMN credeals.v_cre_market_summary.price_per_sf_n IS 'Listings in the price_per_sf leg (published at every n). Appended by 021.';
COMMENT ON COLUMN credeals.v_cre_market_summary.cap_rate_n IS 'Listings in the cap_rate leg (published at every n). Appended by 021.';
COMMENT ON COLUMN credeals.v_cre_market_summary.median_size_sf IS 'Median size_sf of listings whose size basis is not unresolved (lease listings, which have no structure row, included); null when size_sf_n is below 3. Appended by 021.';
COMMENT ON COLUMN credeals.v_cre_market_summary.size_sf_n IS 'Listings in the size leg (published at every n). Appended by 021.';

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
