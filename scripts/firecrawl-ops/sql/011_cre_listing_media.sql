-- =============================================================================
-- 011_cre_listing_media.sql
-- CRE Listing Intelligence for EQUIRE
-- Supabase project: supabase-agentic-assets-v2 (fhqycqubkkrdgzswccwd)
--
-- Detail-page media + outbound-link capture layer (capture design:
-- cre_collector/out/capture/IMPL_SPEC.md Section 5). ADDITIVE ONLY and
-- idempotent. Nothing here alters or drops a TABLE or any data; the only
-- non-additive statement is the purely WIDENING doc_type CHECK rebuild on
-- cre_listing_documents (DROP CONSTRAINT IF EXISTS + ADD), which mirrors the
-- 002 status-CHECK rebuild template and cannot make an existing row invalid.
--
-- Items in this file:
--   cre_listing_media          -- video / virtual-tour / matterport / 360 URLs
--   cre_listing_links          -- external / social / map / other outbound links
--                                 (broker-bio links stay in cre_listing_contacts)
--   cre_listing_documents      -- doc_type CHECK widened additively to add
--                                 'financials','rent_roll' (reuses existing table)
--   cre_listing_media_archive  -- mark-missing snapshot mirror (009 pattern)
--   cre_listing_links_archive  -- mark-missing snapshot mirror (009 pattern)
--
-- The v_cre_listings_full media/links LATERAL json_agg blocks live in 005, NOT
-- here: 005 runs AFTER 011 and re-creates the view (CREATE OR REPLACE VIEW), so
-- the new tables exist before the view references them.
--
-- Registered in 000_run_all.sql AFTER 010 and BEFORE 006 (so these tables exist
-- before 005, the view migration, runs last).
--
-- Requires: 001_cre_brokerages.sql, 002_cre_listings.sql (cre_listings,
--           cre_listing_documents). 003..010 run before this in 000_run_all.sql.
--
-- Security posture mirrors existing collector-owned tables: RLS enabled, no
-- public row policies. Service-role / direct-postgres connection bypasses RLS.
-- Do NOT grant anon/authenticated access (see SUPABASE_SECURITY_NOTE_2026-06-12.md).
-- =============================================================================

-- ---------------------------------------------------------------------------
-- cre_listing_media -- video / virtual-tour / matterport / 360 media URLs
-- harvested from a listing detail page. URLs only (download/embed on demand).
-- Wholesale-replaced per listing on a clean detail touch by cre_ingest.py
-- (detailError rows are excluded from the refresh set), mirroring
-- cre_listing_images. Routing: video/tour -> here; documents ->
-- cre_listing_documents; everything else outbound -> cre_listing_links.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credeals.cre_listing_media (
    id          uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    listing_id  uuid        NOT NULL REFERENCES credeals.cre_listings(id) ON DELETE CASCADE,
    media_type  text        NOT NULL DEFAULT 'other'
                            CHECK (media_type IN ('video', 'virtual_tour', 'matterport', 'other')),
    provider    text,                       -- vimeo / youtube / wistia / brightcove / matterport / kuula / ...
    url         text        NOT NULL,
    embed_url   text,                       -- player/embed URL when distinct from url
    title       text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cre_listing_media_listing_idx
    ON credeals.cre_listing_media (listing_id);
-- NULLS NOT DISTINCT so the dedup holds even if a (defensive) NULL leaked into
-- the key columns; a plain UNIQUE would treat NULLs as all-distinct and let
-- duplicate media rows accumulate (mirrors 007's unique-index convention).
CREATE UNIQUE INDEX IF NOT EXISTS cre_listing_media_uq
    ON credeals.cre_listing_media (listing_id, media_type, url) NULLS NOT DISTINCT;

ALTER TABLE credeals.cre_listing_media ENABLE ROW LEVEL SECURITY;  -- collector-owned; RLS on, no public policy (see 001).

COMMENT ON TABLE credeals.cre_listing_media IS
    'Video / virtual-tour / matterport / 360 media URLs for a listing. URLs only; service-role only (RLS on, no public policy).';

-- ---------------------------------------------------------------------------
-- cre_listing_links -- external / social / map / other outbound links
-- harvested from a listing detail page. Broker-bio links are NOT stored here:
-- they live in cre_listing_contacts.profile_url. The link_type CHECK retains
-- broker_bio / document / video tokens for forward compatibility even though
-- the ingest path does not route those values here (bios -> contacts; docs ->
-- documents; video -> media); a defensible value set never blocks a future
-- classifier. Wholesale-replaced per listing on a clean detail touch by
-- cre_ingest.py (detailError rows excluded), mirroring cre_listing_images.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credeals.cre_listing_links (
    id          uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    listing_id  uuid        NOT NULL REFERENCES credeals.cre_listings(id) ON DELETE CASCADE,
    link_type   text        NOT NULL DEFAULT 'other'
                            CHECK (link_type IN ('external_listing', 'social', 'map', 'broker_bio', 'document', 'video', 'other')),
    url         text        NOT NULL,
    rel         text,                       -- raw rel attribute when present (e.g. canonical, nofollow)
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cre_listing_links_listing_idx
    ON credeals.cre_listing_links (listing_id);
-- NULLS NOT DISTINCT for the same dedup-safety reason as cre_listing_media.
CREATE UNIQUE INDEX IF NOT EXISTS cre_listing_links_uq
    ON credeals.cre_listing_links (listing_id, link_type, url) NULLS NOT DISTINCT;

ALTER TABLE credeals.cre_listing_links ENABLE ROW LEVEL SECURITY;  -- collector-owned; RLS on, no public policy (see 001).

COMMENT ON TABLE credeals.cre_listing_links IS
    'External / social / map / other outbound links for a listing (broker-bio links live in cre_listing_contacts.profile_url and are not stored here). URLs only; service-role only.';

-- ---------------------------------------------------------------------------
-- Widen cre_listing_documents.doc_type CHECK additively to add 'financials' and
-- 'rent_roll' so harvested financials / rent-roll documents classify natively
-- instead of silently downgrading to 'other'. PURELY WIDENING ('other' is
-- already allowed, every prior value stays valid), so it validates cleanly on
-- the populated table. Mirrors the 002 status-CHECK rebuild template
-- (002:199-202): DROP CONSTRAINT IF EXISTS then ADD. The matching ingest
-- doc_type CASE allow-list (cre_ingest.py) ships in the same change.
-- ---------------------------------------------------------------------------
ALTER TABLE credeals.cre_listing_documents DROP CONSTRAINT IF EXISTS cre_listing_documents_doc_type_check;
ALTER TABLE credeals.cre_listing_documents ADD CONSTRAINT cre_listing_documents_doc_type_check
    CHECK (doc_type IN ('brochure', 'om', 'flyer', 'floor_plan', 'financials', 'rent_roll', 'other'));

-- ---------------------------------------------------------------------------
-- cre_listing_media_archive / cre_listing_links_archive -- bounded, durable
-- history slice. The mark-missing reconciliation snapshots a retired listing's
-- FINAL media and links here in the SAME transaction as the soft-delete, so the
-- final tour/video and outbound links survive the next re-scrape's wholesale
-- child replace (mirrors the 009 contacts/documents archive). No FK to
-- cre_listings: the archive must outlive a future hard delete of the source row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credeals.cre_listing_media_archive (
    id                uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    source_listing_id uuid        NOT NULL,
    archived_at       timestamptz NOT NULL DEFAULT now(),
    media_type        text,
    provider          text,
    url               text,
    embed_url         text,
    title             text
);

CREATE INDEX IF NOT EXISTS cre_listing_media_archive_listing_idx
    ON credeals.cre_listing_media_archive (source_listing_id, archived_at DESC);

ALTER TABLE credeals.cre_listing_media_archive ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE credeals.cre_listing_media_archive IS
    'Append-only snapshot of a listing''s final media URLs, captured by cre_ingest.py mark-missing at retirement. No FK: survives a later hard delete of the source listing.';

CREATE TABLE IF NOT EXISTS credeals.cre_listing_links_archive (
    id                uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    source_listing_id uuid        NOT NULL,
    archived_at       timestamptz NOT NULL DEFAULT now(),
    link_type         text,
    url               text,
    rel               text
);

CREATE INDEX IF NOT EXISTS cre_listing_links_archive_listing_idx
    ON credeals.cre_listing_links_archive (source_listing_id, archived_at DESC);

ALTER TABLE credeals.cre_listing_links_archive ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE credeals.cre_listing_links_archive IS
    'Append-only snapshot of a listing''s final outbound links, captured by cre_ingest.py mark-missing at retirement. No FK: survives a later hard delete of the source listing.';
