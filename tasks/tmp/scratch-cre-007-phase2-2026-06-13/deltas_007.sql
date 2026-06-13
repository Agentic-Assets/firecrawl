-- Surgical, additive 007 delta set (single source of truth for the prod apply).
-- Creates the new monitor tables and the additive cre_listings columns/indexes
-- and the new event-ledger view. Does NOT recreate the four existing EQUIRE
-- views or any other object. Sourced by validate_007.sql (ROLLBACK) and
-- apply_007.sql (COMMIT).

-- 1. New monitor tables (canonical committed file = single source of truth).
\i /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/sql/007_cre_change_tracking.sql

-- 2. cre_listings additive ALTERs (verbatim from 002_cre_listings.sql tail).
ALTER TABLE credeals.cre_listings DROP CONSTRAINT IF EXISTS cre_listings_status_check;
ALTER TABLE credeals.cre_listings ADD CONSTRAINT cre_listings_status_check
    CHECK (status IN ('active', 'inactive', 'under_contract', 'pending',
                      'sold', 'leased', 'off_market', 'expired', 'withdrawn'));

ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS last_seen_at   timestamptz;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS source_lastmod timestamptz;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS canonical_key  text;

COMMENT ON COLUMN credeals.cre_listings.last_seen_at   IS 'Timestamp the listing was last re-observed in a source enumeration. Drives disappearance detection; distinct from scraped_at (last detail scrape).';
COMMENT ON COLUMN credeals.cre_listings.source_lastmod IS 'Full-precision upstream last-modified (e.g. sitemap <lastmod>), used to prioritize re-scrapes. Not day-truncated. Not necessarily the first-listed date.';
COMMENT ON COLUMN credeals.cre_listings.canonical_key  IS 'lower(address)+state(+rounded geo) key for advisory re-listing detection within a brokerage. Geoless sources downgrade to address+state-only (weaker advisory).';

-- 3. cre_listings change-tracking indexes (verbatim from 004_cre_indexes.sql tail).
CREATE INDEX IF NOT EXISTS cre_listings_canonical_key_idx
    ON credeals.cre_listings (brokerage_id, canonical_key)
    WHERE canonical_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS cre_listings_last_seen_idx
    ON credeals.cre_listings (last_seen_at DESC);

-- 4. New event-ledger view (verbatim from 005_cre_views.sql tail). Existing views untouched.
CREATE OR REPLACE VIEW credeals.v_cre_recent_changes AS
SELECT
    e.id, e.listing_id, e.brokerage_id, e.scrape_job_id,
    e.event_type, e.field, e.old_value, e.new_value, e.source_value,
    e.detected_at,
    l.title, l.source_url,
    b.slug AS brokerage_slug
FROM credeals.cre_listing_events e
JOIN      credeals.cre_listings  l ON l.id = e.listing_id
LEFT JOIN credeals.cre_brokerages b ON b.id = e.brokerage_id
WHERE e.detected_at > now() - interval '7 days'
ORDER BY e.detected_at DESC;

COMMENT ON VIEW credeals.v_cre_recent_changes IS 'Last 7 days of cre_listing_events with listing title/url + brokerage slug. Operator/prospecting-ops freshness read. Additive; existing display views unchanged.';
ALTER VIEW credeals.v_cre_recent_changes SET (security_invoker = true);
