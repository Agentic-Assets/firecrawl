-- =============================================================================
-- 005_cre_views.sql
-- CRE Listing Intelligence for EQUIRE
-- Supabase project: supabase-agentic-assets-v2 (fhqycqubkkrdgzswccwd)
--
-- The agent-facing API surface. EQUIRE agents (ListingHunterAgent,
-- ProspectResearchAgent, the deal assistant) read these views and call
-- search_cre_listings() rather than touching base tables, so the contract
-- is stable even as the schema evolves.
--
-- Requires: 001..004. Idempotent: views use CREATE OR REPLACE; the trigger
-- function is CREATE OR REPLACE and triggers are dropped-then-created.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- updated_at trigger function (shared by cre_listings and cre_brokerages).
-- Defined first so the triggers at the bottom can attach to it.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION credeals.update_cre_listing_timestamp()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION credeals.update_cre_listing_timestamp() IS 'BEFORE UPDATE trigger: stamps updated_at = now() on cre_listings / cre_brokerages.';

-- ===========================================================================
-- VIEW: v_cre_listings_full
-- One row per listing with contacts, documents, and images folded into JSON
-- arrays. The single read an agent needs to render or reason over a listing.
-- ===========================================================================
CREATE OR REPLACE VIEW credeals.v_cre_listings_full AS
SELECT
    l.*,
    b.name AS brokerage_name,
    b.slug AS brokerage_slug,
    COALESCE(c.contacts,  '[]'::json) AS contacts,
    COALESCE(d.documents, '[]'::json) AS documents,
    COALESCE(i.images,    '[]'::json) AS images
FROM credeals.cre_listings l
JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
LEFT JOIN LATERAL (
    SELECT json_agg(json_build_object(
        'id', cc.id, 'name', cc.name, 'title', cc.title,
        'email', cc.email, 'phone', cc.phone,
        'brokerage_name', cc.brokerage_name,
        'profile_url', cc.profile_url,
        'avatar_url', cc.avatar_url,
        'vcard_url', cc.vcard_url,
        'is_primary', cc.is_primary
    ) ORDER BY cc.is_primary DESC, cc.name) AS contacts
    FROM credeals.cre_listing_contacts cc WHERE cc.listing_id = l.id
) c ON true
LEFT JOIN LATERAL (
    SELECT json_agg(json_build_object(
        'id', cd.id, 'doc_type', cd.doc_type, 'title', cd.title,
        'url', cd.url, 'file_size_bytes', cd.file_size_bytes
    ) ORDER BY cd.doc_type, cd.title) AS documents
    FROM credeals.cre_listing_documents cd WHERE cd.listing_id = l.id
) d ON true
LEFT JOIN LATERAL (
    SELECT json_agg(json_build_object(
        'id', ci.id, 'url', ci.url, 'alt_text', ci.alt_text,
        'is_primary', ci.is_primary, 'display_order', ci.display_order
    ) ORDER BY ci.is_primary DESC, ci.display_order) AS images
    FROM credeals.cre_listing_images ci WHERE ci.listing_id = l.id
) i ON true
WHERE l.deleted_at IS NULL;

COMMENT ON VIEW credeals.v_cre_listings_full IS 'Listing + brokerage name + contacts/documents/images as JSON arrays. Excludes soft-deleted. Primary agent read.';

-- ===========================================================================
-- VIEW: v_cre_active_for_sale
-- Active sale listings with brokerage name and the primary contact inlined.
-- Drives mandate-fit screening and OriginationBrief seeding.
-- ===========================================================================
CREATE OR REPLACE VIEW credeals.v_cre_active_for_sale AS
SELECT
    l.id, l.brokerage_id, b.name AS brokerage_name,
    l.external_id, l.source_url, l.canonical_url,
    l.title, l.address, l.city, l.state, l.zip, l.county,
    l.market, l.submarket, l.lat, l.lng,
    l.property_type,
    l.size_sf, l.lot_size_sf, l.year_built, l.units,
    l.sale_price_usd, l.sale_price_per_sf, l.cap_rate, l.noi,
    l.gross_revenue, l.occupancy_rate,
    l.highlights, l.description,
    pc.name  AS primary_contact_name,
    pc.email AS primary_contact_email,
    pc.phone AS primary_contact_phone,
    pc.brokerage_name AS primary_contact_firm,
    l.scraped_at, l.listing_date, l.updated_at
FROM credeals.cre_listings l
JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
LEFT JOIN LATERAL (
    SELECT name, email, phone, brokerage_name
    FROM credeals.cre_listing_contacts
    WHERE listing_id = l.id
    ORDER BY is_primary DESC, created_at
    LIMIT 1
) pc ON true
WHERE l.deleted_at IS NULL
  AND l.status = 'active'
  AND l.transaction_type IN ('sale', 'sale_or_lease');

COMMENT ON VIEW credeals.v_cre_active_for_sale IS 'Active for-sale listings with brokerage + primary contact. Mandate-fit screening / OriginationBrief seed.';

-- ===========================================================================
-- VIEW: v_cre_active_for_lease
-- Active lease listings with brokerage name and primary contact.
-- ===========================================================================
CREATE OR REPLACE VIEW credeals.v_cre_active_for_lease AS
SELECT
    l.id, l.brokerage_id, b.name AS brokerage_name,
    l.external_id, l.source_url, l.canonical_url,
    l.title, l.address, l.city, l.state, l.zip, l.county,
    l.market, l.submarket, l.lat, l.lng,
    l.property_type,
    l.size_sf, l.available_sf, l.min_divisible_sf, l.max_divisible_sf,
    l.year_built, l.floors,
    l.lease_rate_min, l.lease_rate_max, l.lease_rate_type,
    l.term_min_months, l.term_max_months,
    l.occupancy_rate, l.highlights, l.description,
    pc.name  AS primary_contact_name,
    pc.email AS primary_contact_email,
    pc.phone AS primary_contact_phone,
    pc.brokerage_name AS primary_contact_firm,
    l.scraped_at, l.listing_date, l.updated_at
FROM credeals.cre_listings l
JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
LEFT JOIN LATERAL (
    SELECT name, email, phone, brokerage_name
    FROM credeals.cre_listing_contacts
    WHERE listing_id = l.id
    ORDER BY is_primary DESC, created_at
    LIMIT 1
) pc ON true
WHERE l.deleted_at IS NULL
  AND l.status = 'active'
  AND l.transaction_type IN ('lease', 'sale_or_lease');

COMMENT ON VIEW credeals.v_cre_active_for_lease IS 'Active for-lease listings with brokerage + primary contact, including divisibility and lease terms.';

-- ===========================================================================
-- VIEW: v_cre_market_summary
-- Aggregate market intelligence by (city, state, property_type). Feeds the
-- MarketStrategistAgent and cross-deal comp context.
-- median cap rate via percentile_cont; price/size averages over priced rows.
-- ===========================================================================
CREATE OR REPLACE VIEW credeals.v_cre_market_summary AS
SELECT
    l.city,
    l.state,
    l.property_type,
    count(*)                                                              AS listing_count,
    count(*) FILTER (WHERE l.transaction_type IN ('sale', 'sale_or_lease')) AS for_sale_count,
    count(*) FILTER (WHERE l.transaction_type IN ('lease', 'sale_or_lease')) AS for_lease_count,
    round(avg(l.sale_price_usd)   FILTER (WHERE l.sale_price_usd   IS NOT NULL))      AS avg_price,
    round(avg(l.sale_price_per_sf) FILTER (WHERE l.sale_price_per_sf IS NOT NULL), 2) AS avg_price_per_sf,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY l.cap_rate)
          FILTER (WHERE l.cap_rate IS NOT NULL)::numeric, 4)              AS median_cap_rate,
    round(avg(l.size_sf) FILTER (WHERE l.size_sf IS NOT NULL))            AS avg_size_sf,
    round(avg(l.occupancy_rate) FILTER (WHERE l.occupancy_rate IS NOT NULL), 4) AS avg_occupancy_rate,
    max(l.scraped_at)                                                     AS last_scraped_at
FROM credeals.cre_listings l
WHERE l.deleted_at IS NULL
  AND l.status = 'active'
  AND l.city IS NOT NULL
  AND l.state IS NOT NULL
GROUP BY l.city, l.state, l.property_type;

COMMENT ON VIEW credeals.v_cre_market_summary IS 'Per-(city,state,property_type) aggregates: counts, avg price/PSF/size, median cap rate, avg occupancy. MarketStrategistAgent input.';

-- ===========================================================================
-- FUNCTION: search_cre_listings(...)
-- Full-text search over active listings with optional structured filters.
-- ts_rank-ordered. The canonical entry point for ListingHunterAgent and the
-- deal-assistant sourcing tools. Empty/NULL query -> filter-only browse.
-- ===========================================================================
CREATE OR REPLACE FUNCTION credeals.search_cre_listings(
    query         text,
    p_city        text DEFAULT NULL,
    p_state       text DEFAULT NULL,
    p_type        text DEFAULT NULL,
    p_transaction text DEFAULT NULL
)
RETURNS TABLE (
    id               uuid,
    brokerage_name   text,
    title            text,
    address          text,
    city             text,
    state            char(2),
    property_type    text,
    transaction_type text,
    size_sf          numeric,
    sale_price_usd   numeric,
    sale_price_per_sf numeric,
    cap_rate         numeric,
    noi              numeric,
    occupancy_rate   numeric,
    lease_rate_min   numeric,
    lease_rate_max   numeric,
    source_url       text,
    scraped_at       timestamptz,
    rank             real
)
LANGUAGE sql
STABLE
SET search_path = ''
AS $$
    SELECT
        l.id, b.name, l.title, l.address, l.city, l.state,
        l.property_type, l.transaction_type, l.size_sf,
        l.sale_price_usd, l.sale_price_per_sf, l.cap_rate, l.noi, l.occupancy_rate,
        l.lease_rate_min, l.lease_rate_max,
        l.source_url, l.scraped_at,
        CASE
            WHEN query IS NULL OR btrim(query) = '' THEN 0::real
            ELSE ts_rank(
                to_tsvector('english',
                    coalesce(l.title, '') || ' ' || coalesce(l.address, '') || ' ' ||
                    coalesce(l.city, '')  || ' ' || coalesce(l.description, '')),
                websearch_to_tsquery('english', query))
        END AS rank
    FROM credeals.cre_listings l
    JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
    WHERE l.deleted_at IS NULL
      AND l.status = 'active'
      AND (query IS NULL OR btrim(query) = '' OR
           to_tsvector('english',
               coalesce(l.title, '') || ' ' || coalesce(l.address, '') || ' ' ||
               coalesce(l.city, '')  || ' ' || coalesce(l.description, ''))
           @@ websearch_to_tsquery('english', query))
      AND (p_city        IS NULL OR l.city = p_city)
      AND (p_state       IS NULL OR l.state = upper(p_state)::char(2))
      AND (p_type        IS NULL OR l.property_type = p_type)
      AND (p_transaction IS NULL OR l.transaction_type = p_transaction
                                 OR l.transaction_type = 'sale_or_lease')
    ORDER BY rank DESC, l.scraped_at DESC
    LIMIT 200;
$$;

COMMENT ON FUNCTION credeals.search_cre_listings(text, text, text, text, text)
    IS 'FTS + optional filters (city/state/type/transaction) over active listings, ts_rank-ordered, capped at 200. Canonical agent search entry point.';

-- ===========================================================================
-- Triggers: keep updated_at fresh on mutation.
-- ===========================================================================
DROP TRIGGER IF EXISTS trg_cre_listings_updated_at ON credeals.cre_listings;
CREATE TRIGGER trg_cre_listings_updated_at
    BEFORE UPDATE ON credeals.cre_listings
    FOR EACH ROW
    EXECUTE FUNCTION credeals.update_cre_listing_timestamp();

DROP TRIGGER IF EXISTS trg_cre_brokerages_updated_at ON credeals.cre_brokerages;
CREATE TRIGGER trg_cre_brokerages_updated_at
    BEFORE UPDATE ON credeals.cre_brokerages
    FOR EACH ROW
    EXECUTE FUNCTION credeals.update_cre_listing_timestamp();
