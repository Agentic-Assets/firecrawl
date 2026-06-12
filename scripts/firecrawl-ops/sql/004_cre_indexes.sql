-- =============================================================================
-- 004_cre_indexes.sql
-- CRE Listing Intelligence for EQUIRE
-- Supabase project: supabase-agentic-assets-v2 (fhqycqubkkrdgzswccwd)
--
-- Query indexes for cre_listings, tuned for the EQUIRE agent access patterns:
-- mandate-fit screening (city/state/type/cap_rate/price), comp anchoring
-- (size_sf, price_per_sf), and full-text discovery.
--
-- pg_trgm and the vector extension are already installed on this project; the
-- GIN/FTS indexes below rely only on core Postgres + GIN ops.
--
-- Requires: 002_cre_listings.sql.
-- =============================================================================

-- --- Geographic / mandate filters ------------------------------------------
CREATE INDEX IF NOT EXISTS cre_listings_city_state_idx
    ON credeals.cre_listings (city, state);

CREATE INDEX IF NOT EXISTS cre_listings_state_type_idx
    ON credeals.cre_listings (state, property_type);

CREATE INDEX IF NOT EXISTS cre_listings_txn_status_idx
    ON credeals.cre_listings (transaction_type, status);

-- --- Investment-signal partial indexes (skip the many NULLs) ----------------
CREATE INDEX IF NOT EXISTS cre_listings_cap_rate_idx
    ON credeals.cre_listings (cap_rate)
    WHERE cap_rate IS NOT NULL;

CREATE INDEX IF NOT EXISTS cre_listings_sale_price_idx
    ON credeals.cre_listings (sale_price_usd)
    WHERE sale_price_usd IS NOT NULL;

-- --- Comp anchoring & general filters ---------------------------------------
CREATE INDEX IF NOT EXISTS cre_listings_size_sf_idx
    ON credeals.cre_listings (size_sf);

CREATE INDEX IF NOT EXISTS cre_listings_brokerage_idx
    ON credeals.cre_listings (brokerage_id);

CREATE INDEX IF NOT EXISTS cre_listings_status_idx
    ON credeals.cre_listings (status);

-- --- jsonb raw_data: containment / key-existence queries from agents --------
CREATE INDEX IF NOT EXISTS cre_listings_raw_data_gin_idx
    ON credeals.cre_listings USING gin (raw_data jsonb_ops);

-- --- highlights text[]: array membership queries ----------------------------
CREATE INDEX IF NOT EXISTS cre_listings_highlights_gin_idx
    ON credeals.cre_listings USING gin (highlights array_ops);

-- --- Full-text search over title + address + city + description -------------
-- Backs the search_cre_listings() function and ad-hoc agent keyword search.
CREATE INDEX IF NOT EXISTS cre_listings_fts_idx
    ON credeals.cre_listings
    USING gin (to_tsvector('english',
        coalesce(title, '') || ' ' ||
        coalesce(address, '') || ' ' ||
        coalesce(city, '') || ' ' ||
        coalesce(description, '')));
