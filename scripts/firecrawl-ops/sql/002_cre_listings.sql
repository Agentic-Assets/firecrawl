-- =============================================================================
-- 002_cre_listings.sql
-- CRE Listing Intelligence for EQUIRE
-- Supabase project: supabase-agentic-assets-v2 (fhqycqubkkrdgzswccwd)
--
-- Canonical listing schema. One row per scraped commercial property listing,
-- normalized across all brokerages in cre_brokerages. Mirrors the EQUIRE
-- PropertyInfo / AcquisitionInfo data model so a listing can seed an
-- OriginationBrief, a SearchResult evidence record, or a new deal directly.
--
-- Child tables: contacts (-> deal_parties broker), documents (OM/brochures),
-- images. Money fields are USD numeric; cap_rate and occupancy_rate are
-- fractions in [0,1] (0.065 = 6.5%) to match the EQUIRE valuation layer.
--
-- Requires: 001_cre_brokerages.sql (FK target).
-- =============================================================================

CREATE TABLE IF NOT EXISTS credeals.cre_listings (
    -- --- Identity & Source -------------------------------------------------
    id              uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    brokerage_id    uuid        NOT NULL REFERENCES credeals.cre_brokerages(id),
    external_id     text,                       -- broker's own property ID, e.g. US-SMPL-6130 or usa1159737
    source_url      text        NOT NULL,       -- the URL actually scraped
    canonical_url   text,                       -- normalized/share URL when different from source_url

    -- --- Status & Type -----------------------------------------------------
    status          text        DEFAULT 'active'
                                CHECK (status IN ('active', 'inactive', 'sold', 'leased', 'expired', 'withdrawn')),
    transaction_type text       CHECK (transaction_type IN ('sale', 'lease', 'sale_or_lease')),
    property_type   text        CHECK (property_type IN ('office', 'retail', 'industrial', 'multifamily',
                                                         'land', 'mixed_use', 'hospitality', 'special_purpose', 'other')),

    -- --- Location ----------------------------------------------------------
    title           text,
    address         text,
    address2        text,
    city            text,
    state           char(2),
    zip             text,
    county          text,
    country         char(2)     DEFAULT 'US',
    lat             double precision,
    lng             double precision,
    market          text,                       -- e.g. "Dallas-Fort Worth" (maps toward cbsas.cbsa_name)
    submarket       text,

    -- --- Size --------------------------------------------------------------
    size_sf         numeric,                    -- rentable / building SF
    lot_size_sf     numeric,
    available_sf    numeric,
    min_divisible_sf numeric,
    max_divisible_sf numeric,
    floors          integer,
    year_built      integer,
    units           integer,                    -- multifamily unit count
    parking_spaces  integer,
    parking_ratio   numeric,                    -- spaces per 1,000 SF

    -- --- For Sale Fields ---------------------------------------------------
    sale_price_usd  numeric,
    sale_price_per_sf numeric,
    cap_rate        numeric,                    -- fraction in [0,1]; 0.065 = 6.5% (going-in)
    noi             numeric,                    -- trailing-12 NOI, USD
    gross_revenue   numeric,
    occupancy_rate  numeric,                    -- fraction in [0,1]

    -- --- For Lease Fields --------------------------------------------------
    lease_rate_min  numeric,                    -- $/SF/year (NNN-equivalent)
    lease_rate_max  numeric,
    lease_rate_type text        CHECK (lease_rate_type IN ('nnn', 'modified_gross', 'gross', 'full_service')),
    term_min_months integer,
    term_max_months integer,

    -- --- Details -----------------------------------------------------------
    description     text,
    highlights      text[],
    amenities       text[],
    zoning          text,

    -- --- Raw Content (evidence ledger) -------------------------------------
    markdown        text,                       -- full scraped markdown (source-grounding for EQUIRE)
    raw_data        jsonb       DEFAULT '{}'::jsonb,  -- full structured-extraction JSON + any unmapped fields

    -- --- Timestamps --------------------------------------------------------
    scraped_at      timestamptz DEFAULT now(),  -- our collector snapshot time, not broker listing date
    listing_date    timestamptz,                -- true first-listed/date-published only when source-proven
    updated_date    timestamptz,                -- broker/source recency, not necessarily first-listed
    created_at      timestamptz DEFAULT now(),
    updated_at      timestamptz DEFAULT now(),
    deleted_at      timestamptz                 -- soft delete; non-null = de-listed / pruned
);

-- Deduplicate on the broker's native ID when present. Listings without an
-- external_id (some SPA sites) are deduped at the application layer by URL.
CREATE UNIQUE INDEX IF NOT EXISTS cre_listings_brokerage_external_uq
    ON credeals.cre_listings (brokerage_id, external_id)
    WHERE external_id IS NOT NULL;

COMMENT ON TABLE  credeals.cre_listings                 IS 'Canonical CRE listing rows, normalized across brokerages. Seeds EQUIRE OriginationBrief / SearchResult / deals.';
COMMENT ON COLUMN credeals.cre_listings.external_id     IS 'Broker-native property ID (US-SMPL-6130, usa1159737). Used with brokerage_id for dedup.';
COMMENT ON COLUMN credeals.cre_listings.cap_rate        IS 'Going-in cap rate as a fraction in [0,1]. 0.065 = 6.5%. Matches EQUIRE valuation layer convention.';
COMMENT ON COLUMN credeals.cre_listings.occupancy_rate  IS 'Occupancy as a fraction in [0,1]. Mandate filter: core (high) vs value-add (low).';
COMMENT ON COLUMN credeals.cre_listings.markdown        IS 'Full scraped markdown. Primary-source grounding for any EQUIRE claim derived from this listing.';
COMMENT ON COLUMN credeals.cre_listings.raw_data        IS 'jsonb: full Firecrawl structured-extraction output plus any broker-specific fields not mapped to columns.';
COMMENT ON COLUMN credeals.cre_listings.scraped_at      IS 'Timestamp when our collector last scraped or refreshed the listing snapshot. This is our collection time, not a broker listing date.';
COMMENT ON COLUMN credeals.cre_listings.listing_date    IS 'Source-provided original listing or published date only when the upstream brokerage explicitly exposes one. Do not infer this from scrape time, updated_at, or generic lastUpdated fields.';
COMMENT ON COLUMN credeals.cre_listings.updated_date    IS 'Source-provided listing recency or last-modified date from the upstream brokerage when exposed. Not necessarily the first-listed/on-market date.';
COMMENT ON COLUMN credeals.cre_listings.deleted_at      IS 'Soft-delete marker. Non-null means the listing was de-listed upstream or pruned; views exclude these.';

-- -----------------------------------------------------------------------------
-- cre_listing_contacts -- listing brokers / agents.
-- Feeds EQUIRE deal_parties (party_type = 'broker') and broker-reliability memory.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credeals.cre_listing_contacts (
    id              uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    listing_id      uuid        NOT NULL REFERENCES credeals.cre_listings(id) ON DELETE CASCADE,
    name            text,
    title           text,
    email           text,
    phone           text,
    brokerage_name  text,                       -- broker's firm as printed on the listing
    profile_url     text,
    avatar_url      text,
    vcard_url       text,
    is_primary      boolean     DEFAULT false,
    created_at      timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cre_listing_contacts_listing_idx ON credeals.cre_listing_contacts (listing_id);
COMMENT ON TABLE credeals.cre_listing_contacts IS 'Listing brokers/agents. Populates EQUIRE deal_parties (broker) and outreach workflows.';

-- -----------------------------------------------------------------------------
-- cre_listing_documents -- brochures, OMs, flyers, floor plans.
-- URLs only; download/parse happens on demand via Firecrawl /v2/parse.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credeals.cre_listing_documents (
    id              uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    listing_id      uuid        NOT NULL REFERENCES credeals.cre_listings(id) ON DELETE CASCADE,
    doc_type        text        DEFAULT 'brochure'
                                CHECK (doc_type IN ('brochure', 'om', 'flyer', 'floor_plan', 'other')),
    title           text,
    url             text        NOT NULL,
    file_size_bytes bigint,
    scraped_at      timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cre_listing_documents_listing_idx ON credeals.cre_listing_documents (listing_id);
COMMENT ON TABLE credeals.cre_listing_documents IS 'Brochure / OM / flyer / floor-plan URLs for a listing. Download+parse on demand via Firecrawl /v2/parse (stealth).';

-- -----------------------------------------------------------------------------
-- cre_listing_images -- property photos.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credeals.cre_listing_images (
    id              uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    listing_id      uuid        NOT NULL REFERENCES credeals.cre_listings(id) ON DELETE CASCADE,
    url             text        NOT NULL,
    alt_text        text,
    is_primary      boolean     DEFAULT false,
    display_order   integer     DEFAULT 0
);

CREATE INDEX IF NOT EXISTS cre_listing_images_listing_idx ON credeals.cre_listing_images (listing_id);
COMMENT ON TABLE credeals.cre_listing_images IS 'Property photo URLs for a listing, ordered by display_order; is_primary marks the hero image.';

-- =============================================================================
-- Change-tracking additions (design doc section 7). ADDITIVE and idempotent;
-- re-running is safe. The status CHECK only ADDS allowed values
-- (under_contract, pending, off_market), so no existing row can violate it.
-- The new columns are nullable with no default (metadata-only ADD COLUMN).
-- =============================================================================
ALTER TABLE credeals.cre_listings DROP CONSTRAINT IF EXISTS cre_listings_status_check;
ALTER TABLE credeals.cre_listings ADD CONSTRAINT cre_listings_status_check
    CHECK (status IN ('active', 'inactive', 'under_contract', 'pending',
                      'sold', 'leased', 'off_market', 'expired', 'withdrawn'));

ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS last_seen_at   timestamptz;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS source_lastmod timestamptz;
ALTER TABLE credeals.cre_listings ADD COLUMN IF NOT EXISTS canonical_key  text;

COMMENT ON COLUMN credeals.cre_listings.last_seen_at   IS 'Reserved / currently unwritten. Enumeration recency and disappearance detection live in cre_source_index (last_enumerated_at, soft_deleted); the observe-only monitor deliberately does not write this column because doing so every run would churn updated_at (exposed in EQUIRE views). Kept nullable for a possible future per-listing signal; distinct from scraped_at (last detail scrape).';
COMMENT ON COLUMN credeals.cre_listings.source_lastmod IS 'Full-precision upstream last-modified (e.g. sitemap <lastmod>), used to prioritize re-scrapes. Not day-truncated. Not necessarily the first-listed date.';
COMMENT ON COLUMN credeals.cre_listings.canonical_key  IS 'lower(address)+state(+rounded geo) key for advisory re-listing detection within a brokerage. Geoless sources downgrade to address+state-only (weaker advisory).';
