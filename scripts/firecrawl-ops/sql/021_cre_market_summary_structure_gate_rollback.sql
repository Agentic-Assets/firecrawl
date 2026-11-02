-- 021_cre_market_summary_structure_gate_rollback.sql (AGENTIC-5129).
-- Restores the pre-021 expressions of credeals.v_cre_market_summary (live
-- definition read 2026-11-02, status = 'active' predicate) and keeps the
-- appended median_price_per_sf column as null: CREATE OR REPLACE cannot drop a
-- column, and DROP VIEW would first need EQUIRE's get_cre_market_summary
-- (RETURNS SETOF this view) dropped. Same approval variables as 021.
-- Re-apply 021 to restore the gate.
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
 AND :'CRE_MARKET_SUMMARY_CONFIRM' = 'ROLLBACK 021_cre_market_summary_structure_gate') AS authorized \gset
\if :authorized
\else
  DO $$ BEGIN RAISE EXCEPTION '021 rollback requires operator, issue and exact confirmation'; END $$;
\endif

BEGIN;
SET LOCAL lock_timeout = '5s';

CREATE OR REPLACE VIEW credeals.v_cre_market_summary
WITH (security_invoker = true) AS
SELECT
    l.city,
    l.state,
    l.property_type,
    count(*)                                                                 AS listing_count,
    count(*) FILTER (WHERE l.transaction_type IN ('sale', 'sale_or_lease'))  AS for_sale_count,
    count(*) FILTER (WHERE l.transaction_type IN ('lease', 'sale_or_lease')) AS for_lease_count,
    round(avg(l.sale_price_usd) FILTER (WHERE l.sale_price_usd IS NOT NULL)) AS avg_price,
    round(avg(l.sale_price_per_sf) FILTER (WHERE l.sale_price_per_sf IS NOT NULL), 2) AS avg_price_per_sf,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY l.cap_rate::double precision)
          FILTER (WHERE l.cap_rate IS NOT NULL)::numeric, 4)                 AS median_cap_rate,
    round(avg(l.size_sf) FILTER (WHERE l.size_sf IS NOT NULL))               AS avg_size_sf,
    round(avg(l.occupancy_rate) FILTER (WHERE l.occupancy_rate IS NOT NULL), 4) AS avg_occupancy_rate,
    max(l.scraped_at)                                                        AS last_scraped_at,
    NULL::numeric                                                            AS median_price_per_sf
FROM credeals.cre_listings l
WHERE l.deleted_at IS NULL
  AND l.status = 'active'
  AND l.city IS NOT NULL
  AND l.state IS NOT NULL
GROUP BY l.city, l.state, l.property_type;

COMMENT ON VIEW credeals.v_cre_market_summary IS 'Per-(city,state,property_type) aggregates: counts, avg price/PSF/size, median cap rate, avg occupancy. MarketStrategistAgent input. (021 rolled back; median_price_per_sf is null.)';
COMMENT ON COLUMN credeals.v_cre_market_summary.avg_price_per_sf IS NULL;
COMMENT ON COLUMN credeals.v_cre_market_summary.median_price_per_sf IS 'Null after the 021 rollback; re-apply 021 to restore.';

COMMIT;
