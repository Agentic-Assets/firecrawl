"""
test_backfill_media.py

Locks down the pure-Python classifier and stranded-shape extractor in
backfill_media_from_raw_data.py.

The backfill recovers already-stranded media/docs out of cre_listings.raw_data
into the child tables. Its url classifier mirrors lib/harvest.ts conceptually:
provider/mediaType host table (classify_media) + ordered docType keyword table
(classify_doc, clamped to the live cre_listing_documents CHECK). extract_from_raw
walks the three known stranded shapes (JLL jllDetail videos/virtualTours/
view360URLs, Marcus gatedDocuments, Colliers brochureUrl/agreementUrl) across
both the flat and {primary, secondary_pass} raw_data layouts.

Pure Python, no DB connection, no network. The module imports cre_ingest only
for load_db_url/find_psql/sql_lit (not exercised here).
"""

from backfill_media_from_raw_data import (
    DOC_TYPE_DB_ALLOWED,
    build_sql,
    classify_doc,
    classify_media,
    extract_from_raw,
    http_url_or_none,
)

# ---------------------------------------------------------------------------
# classify_media: provider/mediaType host table (mirrors harvest.ts)
# ---------------------------------------------------------------------------


def test_classify_media_vimeo_is_video():
    m = classify_media("https://vimeo.com/123456789")
    assert m is not None
    assert m["mediaType"] == "video"
    assert m["provider"] == "vimeo"
    assert m["url"] == "https://vimeo.com/123456789"


def test_classify_media_youtube_is_video():
    m = classify_media("https://www.youtube.com/watch?v=abc123def")
    assert m is not None and m["mediaType"] == "video" and m["provider"] == "youtube"


def test_classify_media_matterport_is_matterport():
    m = classify_media("https://my.matterport.com/show/?m=AbCdEf")
    assert m is not None and m["mediaType"] == "matterport" and m["provider"] == "matterport"


def test_classify_media_360_path_is_virtual_tour():
    m = classify_media("https://tours.example.com/listing/360/")
    assert m is not None and m["mediaType"] == "virtual_tour"


def test_classify_media_kuula_is_virtual_tour():
    m = classify_media("https://kuula.co/share/abcde")
    assert m is not None and m["mediaType"] == "virtual_tour"


def test_classify_media_rejects_non_media_and_lookalike_host():
    # A plain doc/listing host is not media.
    assert classify_media("https://www.loopnet.com/Listing/123") is None
    # Label-boundary anchoring: notvimeo.com / vimeo.com.evil.test are NOT vimeo.
    assert classify_media("https://notvimeo.com/123") is None
    assert classify_media("https://vimeo.com.evil.test/123") is None


def test_classify_media_rejects_non_http_and_garbage():
    assert classify_media("data:video/mp4;base64,AAAA") is None
    assert classify_media("javascript:alert(1)") is None
    assert classify_media(None) is None
    assert classify_media(12345) is None
    assert classify_media("") is None


# ---------------------------------------------------------------------------
# classify_doc: ordered keyword table + extension fallback, clamped to CHECK
# ---------------------------------------------------------------------------


def test_classify_doc_offering_memorandum_is_om():
    d = classify_doc("https://deals.example.com/offering-memorandum.pdf")
    assert d is not None and d["docType"] == "om"


def test_classify_doc_brochure_is_brochure():
    d = classify_doc("https://cdn.example.com/marketing/property-brochure.pdf")
    assert d is not None and d["docType"] == "brochure"


def test_classify_doc_bare_pdf_extension_is_other():
    d = classify_doc("https://cdn.example.com/files/doc-9931.pdf")
    assert d is not None and d["docType"] == "other"


def test_classify_doc_keyword_without_extension_qualifies():
    # Gated deal-room links often lack a file extension.
    d = classify_doc("https://dealroom.example.com/secure/data-room/listing-42")
    assert d is not None and d["docType"] == "om"


def test_classify_doc_financials_clamped_to_other_for_live_check():
    # harvest.ts would emit 'financials', but the live cre_listing_documents
    # CHECK does not allow it yet -> clamp to 'other'.
    d = classify_doc("https://cdn.example.com/proforma-financials.xlsx")
    assert d is not None
    assert d["docType"] == "other"
    assert d["docType"] in DOC_TYPE_DB_ALLOWED


def test_classify_doc_rejects_non_document_url():
    assert classify_doc("https://www.example.com/about-us") is None
    assert classify_doc(None) is None


def test_classify_doc_doctype_always_db_allowed():
    for url in [
        "https://x.example/offering-memorandum.pdf",
        "https://x.example/floor-plan.pdf",
        "https://x.example/rent-roll.xlsx",
        "https://x.example/financials.xlsx",
        "https://x.example/flyer.pdf",
        "https://x.example/brochure.pdf",
        "https://x.example/random.pdf",
    ]:
        d = classify_doc(url)
        assert d is not None
        assert d["docType"] in DOC_TYPE_DB_ALLOWED, f"{url} -> {d['docType']} not in CHECK set"


# ---------------------------------------------------------------------------
# http_url_or_none guard
# ---------------------------------------------------------------------------


def test_http_url_or_none_accepts_https_rejects_others():
    assert http_url_or_none("https://example.com/x") == "https://example.com/x"
    assert http_url_or_none("http://example.com") == "http://example.com"
    assert http_url_or_none("ftp://example.com/x") is None
    assert http_url_or_none("/relative/path") is None
    assert http_url_or_none("mailto:a@b.com") is None
    assert http_url_or_none("  https://e.com/x  ") == "https://e.com/x"
    assert http_url_or_none(None) is None


# ---------------------------------------------------------------------------
# extract_from_raw: the three known stranded shapes
# ---------------------------------------------------------------------------


def test_extract_jll_videos_virtual_tours_360():
    raw = {
        "sourceKey": "jll",
        "jllDetail": {
            "videos": ["https://vimeo.com/111", {"url": "https://www.youtube.com/watch?v=xyz789abc"}],
            "virtualTours": ["https://my.matterport.com/show/?m=ZZ"],
            "view360URLs": ["https://tours.example.com/unknownhost/spin"],
        },
    }
    media, docs = extract_from_raw(raw)
    urls = {m["url"] for m in media}
    assert "https://vimeo.com/111" in urls
    assert "https://www.youtube.com/watch?v=xyz789abc" in urls
    assert "https://my.matterport.com/show/?m=ZZ" in urls
    # An unknown-host 360 url under view360URLs is force-recovered as a tour.
    assert "https://tours.example.com/unknownhost/spin" in urls
    types = {m["url"]: m["mediaType"] for m in media}
    assert types["https://my.matterport.com/show/?m=ZZ"] == "matterport"
    assert types["https://tours.example.com/unknownhost/spin"] == "virtual_tour"
    assert docs == []


def test_extract_marcus_gated_documents():
    raw = {
        "sourceKey": "marcus-millichap",
        "gatedDocuments": [
            {"name": "Offering Memorandum & Deal Room", "url": "https://www.marcusmillichap.com/dealroom/12345", "gated": True},
        ],
    }
    media, docs = extract_from_raw(raw)
    assert media == []
    assert len(docs) == 1
    d = docs[0]
    assert d["url"] == "https://www.marcusmillichap.com/dealroom/12345"
    assert d["docType"] in DOC_TYPE_DB_ALLOWED


def test_extract_colliers_brochure_and_agreement():
    raw = {
        "sourceKey": "colliers",
        "colliersSalesTrackerDetail": {
            "brochureUrl": "https://salestracker.colliers.com/brochure/abc.pdf",
            "agreementUrl": "https://salestracker.colliers.com/agreement/abc",
        },
    }
    media, docs = extract_from_raw(raw)
    assert media == []
    by_url = {d["url"]: d for d in docs}
    assert "https://salestracker.colliers.com/brochure/abc.pdf" in by_url
    assert "https://salestracker.colliers.com/agreement/abc" in by_url
    # Agreement must never be promoted into a marketing bucket.
    assert by_url["https://salestracker.colliers.com/agreement/abc"]["docType"] == "other"


def test_extract_walks_primary_and_secondary_pass():
    # merge_rows() wraps a dual sale+lease payload; stranded fields on either
    # sub-pass must be found.
    raw = {
        "primary": {
            "sourceKey": "colliers",
            "colliersSalesTrackerDetail": {"brochureUrl": "https://x.colliers.com/b1.pdf"},
        },
        "secondary_pass": {
            "sourceKey": "colliers",
            "colliersSalesTrackerDetail": {"agreementUrl": "https://x.colliers.com/a1"},
        },
    }
    _media, docs = extract_from_raw(raw)
    urls = {d["url"] for d in docs}
    assert "https://x.colliers.com/b1.pdf" in urls
    assert "https://x.colliers.com/a1" in urls


def test_extract_dedupes_within_listing():
    raw = {
        "jllDetail": {
            "videos": ["https://vimeo.com/777", "https://vimeo.com/777"],
        }
    }
    media, _docs = extract_from_raw(raw)
    assert len([m for m in media if m["url"] == "https://vimeo.com/777"]) == 1


def test_extract_empty_and_garbage_inputs_never_throw():
    assert extract_from_raw({}) == ([], [])
    assert extract_from_raw(None) == ([], [])
    assert extract_from_raw({"jllDetail": "not-a-dict"}) == ([], [])
    assert extract_from_raw({"gatedDocuments": "nope"}) == ([], [])


# ---------------------------------------------------------------------------
# build_sql: additive + idempotent + existence-guarded shape
# ---------------------------------------------------------------------------


def _doc_row():
    return {"listing_id": "00000000-0000-0000-0000-000000000001",
            "doc_type": "om", "title": "OM", "url": "https://x/om.pdf"}


def _media_row():
    return {"listing_id": "00000000-0000-0000-0000-000000000001",
            "media_type": "video", "provider": "vimeo",
            "url": "https://vimeo.com/1", "embed_url": None, "title": None}


def test_build_sql_documents_insert_is_anti_joined_not_delete():
    sql = build_sql([], [_doc_row()], [])
    assert "INSERT INTO credeals.cre_listing_documents" in sql
    assert "NOT EXISTS" in sql
    # Pure-additive: must never delete existing child rows.
    assert "DELETE FROM credeals.cre_listing_documents" not in sql
    assert "DELETE FROM" not in sql


def test_build_sql_media_and_links_are_existence_guarded():
    sql = build_sql([_media_row()], [], [])
    assert "to_regclass('credeals.cre_listing_media')" in sql
    assert "to_regclass('credeals.cre_listing_links')" in sql


def test_build_sql_never_touches_listings_status_or_deleted_at():
    sql = build_sql([_media_row()], [_doc_row()], [])
    # Additive backfill must never mutate the parent listing.
    assert "UPDATE credeals.cre_listings" not in sql
    assert "deleted_at" not in sql
    assert "status" not in sql


def test_build_sql_documents_table_not_existence_guarded():
    # cre_listing_documents exists in sql/002, so its INSERT is unguarded.
    sql = build_sql([], [_doc_row()], [])
    assert "to_regclass('credeals.cre_listing_documents')" not in sql
