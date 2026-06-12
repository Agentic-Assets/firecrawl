-- =============================================================================
-- 006_cre_contact_urls.sql
-- Add public broker profile/avatar/VCard URL fields to listing contacts.
-- URLs only; no contact-card or image binaries are downloaded or stored.
-- =============================================================================

ALTER TABLE credeals.cre_listing_contacts
    ADD COLUMN IF NOT EXISTS profile_url text,
    ADD COLUMN IF NOT EXISTS avatar_url text,
    ADD COLUMN IF NOT EXISTS vcard_url text;

COMMENT ON COLUMN credeals.cre_listing_contacts.profile_url IS 'Public broker profile URL from the listing page, when exposed.';
COMMENT ON COLUMN credeals.cre_listing_contacts.avatar_url IS 'Public broker headshot/avatar URL from the listing page, when exposed.';
COMMENT ON COLUMN credeals.cre_listing_contacts.vcard_url IS 'Public VCard/contact-card URL from the listing page, when exposed. URL only.';

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
