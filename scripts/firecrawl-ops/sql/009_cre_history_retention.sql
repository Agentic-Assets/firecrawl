-- =============================================================================
-- 009_cre_history_retention.sql
-- CRE Listing Intelligence for EQUIRE
-- Supabase project: supabase-agentic-assets-v2 (fhqycqubkkrdgzswccwd)
--
-- History retention layer (FRESHNESS_HISTORY_REVIEW_2026-06-15 items H4a, H4b,
-- M2, L2). ADDITIVE ONLY and idempotent. Nothing here alters or drops an
-- existing object (all CREATE TABLE IF NOT EXISTS, ADD COLUMN IF NOT EXISTS,
-- CREATE OR REPLACE FUNCTION, DROP TRIGGER IF EXISTS + CREATE TRIGGER,
-- CREATE INDEX IF NOT EXISTS).
--
-- Items in this file:
--   H4a  cre_listing_price_history -- append-only price/status history table
--   H4b  cre_source_index prior_* columns for monitor old_value population
--   M2   cre_listing_contacts_archive, cre_listing_documents_archive
--          (contacts + documents only; images excluded by design: high volume,
--          low historical value)
--   L2   cre_block_history_delete() trigger + partial deleted_at index
--
-- Registered in 000_run_all.sql AFTER 008 and BEFORE 006.
--
-- Requires: 001_cre_brokerages.sql, 002_cre_listings.sql,
--           007_cre_change_tracking.sql (cre_source_index), 008_cre_fk_indexes.sql.
--
-- Security posture mirrors existing collector-owned tables: RLS enabled, no
-- public row policies. Service-role / direct-postgres connection bypasses RLS.
-- Do NOT grant anon/authenticated access (see SUPABASE_SECURITY_NOTE_2026-06-12.md).
-- =============================================================================

-- ---------------------------------------------------------------------------
-- H4a: cre_listing_price_history -- append-only value-over-time history. One
-- row per listing per ingest run in which a WATCHED field (price, status,
-- cap_rate) changed vs the prior DB row. Written by cre_ingest.py
-- (existence-guarded so a pre-apply ingestor is a no-op). Never updated in
-- place; the row IS the snapshot of the watched values at observed_at.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credeals.cre_listing_price_history (
    id                uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    listing_id        uuid        NOT NULL REFERENCES credeals.cre_listings(id) ON DELETE CASCADE,
    observed_at       timestamptz NOT NULL DEFAULT now(),
    sale_price_usd    numeric,
    sale_price_per_sf numeric,
    lease_rate_min    numeric,
    lease_rate_max    numeric,
    status            text,
    cap_rate          numeric,
    source_lastmod    timestamptz,
    transaction_type  text
);

CREATE INDEX IF NOT EXISTS cre_listing_price_history_listing_idx
    ON credeals.cre_listing_price_history (listing_id, observed_at DESC);

ALTER TABLE credeals.cre_listing_price_history ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE credeals.cre_listing_price_history IS
    'Append-only value-over-time history for cre_listings. One row per ingest run where a watched field (sale_price_usd, sale_price_per_sf, lease_rate_min/max, status, cap_rate) changed vs the prior DB row. Written by cre_ingest.py; never updated in place.';

-- ---------------------------------------------------------------------------
-- H4b: three prior-price columns on cre_source_index. Written by
-- cre_monitor.py on each enumeration so the NEXT run can populate a real
-- old_value on price_change events instead of NULL. Table currently has 0
-- rows (monitor has never run successfully), so no backfill concern.
-- ---------------------------------------------------------------------------
ALTER TABLE credeals.cre_source_index ADD COLUMN IF NOT EXISTS prior_sale_price numeric;
ALTER TABLE credeals.cre_source_index ADD COLUMN IF NOT EXISTS prior_lease_rate numeric;
ALTER TABLE credeals.cre_source_index ADD COLUMN IF NOT EXISTS prior_status     text;

COMMENT ON COLUMN credeals.cre_source_index.prior_sale_price IS
    'Sale price observed on the PREVIOUS enumeration. Lets cre_monitor.py populate a real old_value on a price_change event instead of NULL.';
COMMENT ON COLUMN credeals.cre_source_index.prior_lease_rate IS
    'Lease rate (min) observed on the PREVIOUS enumeration. Companion to prior_sale_price for lease-priced rows.';
COMMENT ON COLUMN credeals.cre_source_index.prior_status IS
    'observed_status from the PREVIOUS enumeration. Reserved for richer status_change evidence.';

-- ---------------------------------------------------------------------------
-- M2: cre_listing_contacts_archive / cre_listing_documents_archive -- bounded,
-- durable history slice. The mark-missing reconciliation snapshots a retired
-- listing's FINAL contacts and documents here in the SAME transaction as the
-- soft-delete, so "who brokered this now-sold deal" and its final brochures
-- survive the next re-scrape's wholesale child replace. Images are excluded
-- (high volume, low historical value). No FK to cre_listings: the archive must
-- outlive a future hard delete of the source row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credeals.cre_listing_contacts_archive (
    id                uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    source_listing_id uuid        NOT NULL,
    archived_at       timestamptz NOT NULL DEFAULT now(),
    name              text,
    title             text,
    email             text,
    phone             text,
    brokerage_name    text,
    profile_url       text,
    avatar_url        text,
    vcard_url         text,
    is_primary        boolean
);

CREATE INDEX IF NOT EXISTS cre_listing_contacts_archive_listing_idx
    ON credeals.cre_listing_contacts_archive (source_listing_id, archived_at DESC);

ALTER TABLE credeals.cre_listing_contacts_archive ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE credeals.cre_listing_contacts_archive IS
    'Append-only snapshot of a listing''s final contacts, captured by cre_ingest.py mark-missing at retirement. No FK: survives a later hard delete of the source listing.';

CREATE TABLE IF NOT EXISTS credeals.cre_listing_documents_archive (
    id                uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    source_listing_id uuid        NOT NULL,
    archived_at       timestamptz NOT NULL DEFAULT now(),
    doc_type          text,
    title             text,
    url               text
);

CREATE INDEX IF NOT EXISTS cre_listing_documents_archive_listing_idx
    ON credeals.cre_listing_documents_archive (source_listing_id, archived_at DESC);

ALTER TABLE credeals.cre_listing_documents_archive ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE credeals.cre_listing_documents_archive IS
    'Append-only snapshot of a listing''s final documents/brochures, captured by cre_ingest.py mark-missing at retirement. No FK: survives a later hard delete of the source listing.';

-- ---------------------------------------------------------------------------
-- L2 retention guard: a soft-deleted (deleted_at IS NOT NULL) cre_listings row
-- is history. Block any hard DELETE of it unless the session explicitly opts in
-- via SET LOCAL cre.allow_history_delete = 'on'. Active (deleted_at IS NULL)
-- rows are unaffected: no production path deletes them today, and the upsert
-- never DELETEs the parent. Child FKs are ON DELETE CASCADE, so protecting the
-- parent protects the children and the price-history rows.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION credeals.cre_block_history_delete()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $$
BEGIN
    IF OLD.deleted_at IS NOT NULL
       AND COALESCE(current_setting('cre.allow_history_delete', true), '') <> 'on' THEN
        RAISE EXCEPTION
            'cre_listings: refusing to hard-delete soft-deleted history row % (deleted_at=%). Set cre.allow_history_delete = ''on'' to override.',
            OLD.id, OLD.deleted_at;
    END IF;
    RETURN OLD;
END;
$$;

COMMENT ON FUNCTION credeals.cre_block_history_delete() IS
    'BEFORE DELETE guard on cre_listings: raises on a soft-deleted (deleted_at IS NOT NULL) row unless cre.allow_history_delete = ''on''. Protects retained history from a future hard-delete migration.';

DROP TRIGGER IF EXISTS trg_cre_listings_block_history_delete ON credeals.cre_listings;
CREATE TRIGGER trg_cre_listings_block_history_delete
    BEFORE DELETE ON credeals.cre_listings
    FOR EACH ROW
    EXECUTE FUNCTION credeals.cre_block_history_delete();

-- Partial index on deleted_at to keep history-scans cheap (queries that filter
-- on soft-deleted rows hit this rather than a full-table scan).
CREATE INDEX IF NOT EXISTS cre_listings_deleted_at_idx
    ON credeals.cre_listings (deleted_at) WHERE deleted_at IS NOT NULL;

-- Defense-in-depth: revoke EXECUTE on the trigger function from public/anon/
-- authenticated roles, mirroring the pattern used for update_cre_listing_timestamp()
-- in 005. Trigger functions fire in the table owner's context; the REVOKEs prevent
-- direct invocation from unprivileged roles.
REVOKE EXECUTE ON FUNCTION credeals.cre_block_history_delete() FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE EXECUTE ON FUNCTION credeals.cre_block_history_delete() FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE EXECUTE ON FUNCTION credeals.cre_block_history_delete() FROM authenticated;
    END IF;
END
$$;
