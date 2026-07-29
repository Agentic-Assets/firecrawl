"""
test_media_links_ingest.py

"Capture Everything" media/links/markdown wiring in the production ingest
(cre_ingest.py to_row / merge_rows / build_sql), per the locked SHARED CONTRACT.

Covers the additive, forward-only capture of detail-page artifacts:

  - to_row() builds media[] and links[] from listing['media']/['links'],
    normalizing bare strings to {mediaType:'other'} / {linkType:'other'} and
    http-url filtering both. It carries listing['markdown'] onto the row and
    honors a per-document docType (CASE-map; default 'brochure'). Harvested
    DocItems on listing['documents'] fold into the documents channel.
  - The stranded structured fields (noi, occupancy_rate, units, ...) lift into
    the EXISTING cre_listings columns (no new columns).
  - merge_rows() folds media/links across the sale+lease passes and prefers the
    longer non-empty markdown.
  - STAGE_COLS + the _stage DDL gain (media jsonb, links jsonb, markdown text)
    plus the lifted structured columns.
  - build_sql() emits the to_regclass-guarded DELETE+reinsert for both
    cre_listing_media and cre_listing_links (mirroring the images block),
    excludes detailError rows from the child-refresh set, uses ON CONFLICT
    (listing_id, <type>, url) DO NOTHING, COALESCE-keeps markdown via
    NULLIF(EXCLUDED.markdown,'') and COALESCE-keeps the lifted numeric columns,
    and widens the documents doc_type CASE to allow financials/rent_roll.

Hard safety invariants enforced here (contract):
  - the new child blocks are existence-guarded so cre_ingest.py stays safe to run
    BEFORE migration 011 is applied;
  - the detailError row is excluded from the child-refresh set (no delete fires
    on a dirty detail touch);
  - markdown + lifted numeric cols COALESCE-keep so a sparse pass never clobbers.

Pure transform, no network / DB. Subprocess cases use --dry-run --keep-artifacts
for the full mark-missing path (singleton svn) so the detailError-exclusion and
archive emit can be asserted against the real generated SQL.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from cre_ingest import (
    STAGE_COLS,
    build_sql,
    merge_rows,
    to_row,
)

_SCRAPED_AT = datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc).isoformat()
_COLLECTOR_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _row(listing):
    return to_row(listing, {}, _SCRAPED_AT)


def _svn(**overrides):
    base = {
        "sourceKey": "svn",
        "url": "https://www.svn.com/property?propertyId=svn-0001-sale",
        "id": "svn-0001",
        "transactionMode": "sale",
    }
    base.update(overrides)
    return base


def _sql():
    return build_sql([], [], _SCRAPED_AT, set())


# ---------------------------------------------------------------------------
# to_row(): media[] / links[] build + normalization + http filter
# ---------------------------------------------------------------------------


def test_to_row_builds_media_from_dicts():
    row = _row(_svn(media=[
        {"mediaType": "video", "provider": "vimeo",
         "url": "https://vimeo.com/123",
         "embedUrl": "https://player.vimeo.com/video/123", "title": "Tour"},
    ]))
    assert row["media"] == [
        {"mediaType": "video", "provider": "vimeo",
         "url": "https://vimeo.com/123",
         "embedUrl": "https://player.vimeo.com/video/123", "title": "Tour"},
    ]


def test_to_row_normalizes_bare_string_media():
    row = _row(_svn(media=["https://my.matterport.com/show/?m=abc"]))
    assert row["media"] == [
        {"mediaType": "other", "provider": None,
         "url": "https://my.matterport.com/show/?m=abc",
         "embedUrl": None, "title": None},
    ]


def test_to_row_filters_non_http_media():
    # bare non-url string and a dict whose url is not http(s) are both dropped.
    row = _row(_svn(media=["not-a-url", {"url": "ftp://x/y"}, {"mediaType": "video"}]))
    assert row["media"] == []


def test_to_row_media_embed_url_http_filtered():
    row = _row(_svn(media=[{"url": "https://vimeo.com/1", "embedUrl": "javascript:void"}]))
    assert row["media"][0]["embedUrl"] is None


def test_to_row_builds_links_and_normalizes_bare_string():
    row = _row(_svn(links=[
        {"url": "https://www.loopnet.com/Listing/1", "linkType": "external_listing", "rel": "nofollow"},
        "https://www.google.com/maps/place/x",
    ]))
    assert row["links"] == [
        {"url": "https://www.loopnet.com/Listing/1", "rel": "nofollow", "linkType": "external_listing"},
        {"url": "https://www.google.com/maps/place/x", "rel": None, "linkType": "other"},
    ]


def test_to_row_filters_non_http_links():
    row = _row(_svn(links=["ftp://bad", {"url": "mailto:a@b.com"}]))
    assert row["links"] == []


def test_to_row_media_links_default_empty():
    row = _row(_svn())
    assert row["media"] == []
    assert row["links"] == []


# ---------------------------------------------------------------------------
# to_row(): markdown carried
# ---------------------------------------------------------------------------


def test_to_row_carries_markdown():
    row = _row(_svn(markdown="# Property\n\nGreat building."))
    assert row["markdown"] == "# Property\n\nGreat building."


def test_to_row_markdown_blank_is_none():
    assert _row(_svn(markdown="   "))["markdown"] is None
    assert _row(_svn())["markdown"] is None
    assert _row(_svn(markdown=123))["markdown"] is None


# ---------------------------------------------------------------------------
# to_row(): documents docType CASE (om stays om, default brochure, harvested fold)
# ---------------------------------------------------------------------------


def test_to_row_document_doctype_default_brochure():
    # A brochure entry with no docType defaults to 'brochure'.
    row = _row(_svn(brochures=[{"name": "Flyer", "url": "https://cdn.x/a.pdf"}]))
    assert row["documents"] == [
        {"title": "Flyer", "url": "https://cdn.x/a.pdf", "docType": "brochure"},
    ]


def test_to_row_document_doctype_om_preserved():
    row = _row(_svn(brochures=[
        {"name": "OM", "url": "https://cdn.x/om.pdf", "docType": "om"},
    ]))
    assert row["documents"][0]["docType"] == "om"


def test_to_row_harvested_documents_fold_in_with_doctype():
    # harvested DocItems (financials/rent_roll) ride the documents channel.
    row = _row(_svn(documents=[
        {"title": "T-12", "url": "https://cdn.x/t12.pdf", "docType": "financials"},
        {"title": "Rent Roll", "url": "https://cdn.x/rr.pdf", "docType": "rent_roll"},
    ]))
    types = {d["docType"] for d in row["documents"]}
    assert types == {"financials", "rent_roll"}


def test_to_row_harvested_document_default_brochure_and_http_filtered():
    row = _row(_svn(documents=[
        {"title": "No type", "url": "https://cdn.x/x.pdf"},
        {"title": "bad", "url": "not-a-url"},
    ]))
    assert row["documents"] == [
        {"title": "No type", "url": "https://cdn.x/x.pdf", "docType": "brochure"},
    ]


# ---------------------------------------------------------------------------
# to_row(): stranded structured fields lift into existing columns
# ---------------------------------------------------------------------------


def test_to_row_lifts_structured_numeric_fields():
    row = _row(_svn(noi=250000, grossRevenue=400000, occupancyRate=92, units=24,
                    floors=3, parkingSpaces=50, parkingRatio=2.5, availableSf=12000,
                    minDivisibleSf=1000, maxDivisibleSf=12000, termMinMonths=12,
                    termMaxMonths=120))
    assert row["noi"] == 250000.0
    assert row["gross_revenue"] == 400000.0
    assert row["occupancy_rate"] == 0.92          # percent -> fraction
    assert row["units"] == 24.0
    assert row["floors"] == 3.0
    assert row["parking_spaces"] == 50.0
    assert row["parking_ratio"] == 2.5
    assert row["available_sf"] == 12000.0
    assert row["min_divisible_sf"] == 1000.0
    assert row["max_divisible_sf"] == 12000.0
    assert row["term_min_months"] == 12.0
    assert row["term_max_months"] == 120.0


def test_to_row_lifts_text_and_array_fields():
    row = _row(_svn(market="Dallas-Fort Worth", submarket="North Dallas",
                    zoning="C-2", leaseRateType="nnn",
                    highlights=["Corner lot", "New roof", "Corner lot"],
                    amenities=["Parking"]))
    assert row["market"] == "Dallas-Fort Worth"
    assert row["submarket"] == "North Dallas"
    assert row["zoning"] == "C-2"
    assert row["lease_rate_type"] == "nnn"
    assert row["highlights"] == ["Corner lot", "New roof"]   # deduped, order-preserving
    assert row["amenities"] == ["Parking"]


def test_to_row_array_fields_none_when_empty_or_wrong_type():
    assert _row(_svn(highlights=[]))["highlights"] is None
    assert _row(_svn(highlights="not a list"))["highlights"] is None
    assert _row(_svn())["amenities"] is None


def test_to_row_lease_rate_type_maps_variants_to_enum_tokens():
    # Common source variants (incl. uppercase / punctuation) map to the four
    # tokens the cre_listings.lease_rate_type CHECK allows.
    assert _row(_svn(leaseRateType="NNN"))["lease_rate_type"] == "nnn"
    assert _row(_svn(leaseRateType="Triple Net"))["lease_rate_type"] == "nnn"
    assert _row(_svn(leaseRateType="triple-net"))["lease_rate_type"] == "nnn"
    assert _row(_svn(leaseRateType="Modified Gross"))["lease_rate_type"] == "modified_gross"
    assert _row(_svn(leaseRateType="mod gross"))["lease_rate_type"] == "modified_gross"
    assert _row(_svn(leaseRateType="Gross"))["lease_rate_type"] == "gross"
    assert _row(_svn(leaseRateType="Full Service"))["lease_rate_type"] == "full_service"
    assert _row(_svn(leaseRateType="FSG"))["lease_rate_type"] == "full_service"


def test_to_row_lease_rate_type_junk_clamps_to_none():
    # Unrecognized free text clamps to None (NOT 'other', which is not in the
    # CHECK) so the COALESCE-keep upsert never writes a CHECK-violating value.
    for junk in ("Negotiable", "Contact broker", "per month", "", 42, None):
        assert _row(_svn(leaseRateType=junk))["lease_rate_type"] is None
    # A price string that embeds a known token (e.g. "NNN") is intentionally
    # mapped to that token (a defensible classification), confirming substring
    # detection still produces only CHECK-allowed values, never raw free text.
    assert _row(_svn(leaseRateType="$25.00 PSF, NNN"))["lease_rate_type"] == "nnn"


# ---------------------------------------------------------------------------
# merge_rows(): fold media/links + markdown-prefers-longer
# ---------------------------------------------------------------------------


def test_merge_folds_media_links_from_other_pass():
    a = _row(_svn())  # sale pass, no media/links
    b = _row(_svn(
        url="https://www.svn.com/property?propertyId=svn-0001-lease",
        transactionMode="lease",
        media=[{"url": "https://vimeo.com/9"}],
        links=[{"url": "https://x.com/feed", "linkType": "social"}],
    ))
    merged = merge_rows(a, b)
    assert merged["media"] == [
        {"mediaType": "other", "provider": None, "url": "https://vimeo.com/9",
         "embedUrl": None, "title": None},
    ]
    assert merged["links"] == [
        {"url": "https://x.com/feed", "rel": None, "linkType": "social"},
    ]


def test_merge_markdown_prefers_longer():
    a = _row(_svn(markdown="short"))
    b = _row(_svn(
        url="https://www.svn.com/property?propertyId=svn-0001-lease",
        transactionMode="lease",
        markdown="a much longer markdown body with detail",
    ))
    assert merge_rows(a, b)["markdown"] == "a much longer markdown body with detail"


def test_merge_markdown_keeps_existing_when_other_blank():
    a = _row(_svn(markdown="kept body"))
    b = _row(_svn(
        url="https://www.svn.com/property?propertyId=svn-0001-lease",
        transactionMode="lease",
    ))
    assert merge_rows(a, b)["markdown"] == "kept body"


def test_merge_markdown_none_when_both_empty():
    a = _row(_svn())
    b = _row(_svn(
        url="https://www.svn.com/property?propertyId=svn-0001-lease",
        transactionMode="lease",
    ))
    assert merge_rows(a, b)["markdown"] is None


def test_merge_folds_lifted_numeric_first_non_none():
    a = _row(_svn())  # no noi
    b = _row(_svn(
        url="https://www.svn.com/property?propertyId=svn-0001-lease",
        transactionMode="lease",
        noi=99000,
    ))
    assert merge_rows(a, b)["noi"] == 99000.0


# ---------------------------------------------------------------------------
# STAGE_COLS + _stage DDL include media / links / markdown + lifted cols
# ---------------------------------------------------------------------------


def test_stage_cols_include_media_links_markdown():
    for col in ("media", "links", "markdown"):
        assert col in STAGE_COLS


def test_stage_cols_include_lifted_structured_cols():
    for col in ("noi", "gross_revenue", "occupancy_rate", "units", "floors",
                "parking_spaces", "parking_ratio", "available_sf",
                "min_divisible_sf", "max_divisible_sf", "term_min_months",
                "term_max_months", "lease_rate_type", "zoning", "market",
                "submarket", "highlights", "amenities"):
        assert col in STAGE_COLS


def test_stage_ddl_declares_media_links_markdown_types():
    sql = _sql()
    assert "media jsonb, links jsonb" in sql
    assert "description text, markdown text" in sql


# ---------------------------------------------------------------------------
# build_sql(): guarded DELETE+reinsert for media + links (mirror images block)
# ---------------------------------------------------------------------------


def test_build_sql_media_block_guarded_delete_reinsert():
    sql = _sql()
    assert "IF to_regclass('credeals.cre_listing_media') IS NOT NULL THEN" in sql
    assert "DELETE FROM credeals.cre_listing_media WHERE listing_id IN (SELECT id FROM _child_refresh)" in sql
    assert "INSERT INTO credeals.cre_listing_media (listing_id, media_type, provider, url, embed_url, title)" in sql
    assert "ON CONFLICT (listing_id, media_type, url) DO NOTHING" in sql


def test_build_sql_links_block_guarded_delete_reinsert():
    sql = _sql()
    assert "IF to_regclass('credeals.cre_listing_links') IS NOT NULL THEN" in sql
    assert "DELETE FROM credeals.cre_listing_links WHERE listing_id IN (SELECT id FROM _child_refresh)" in sql
    assert "INSERT INTO credeals.cre_listing_links (listing_id, link_type, url, rel)" in sql
    assert "ON CONFLICT (listing_id, link_type, url) DO NOTHING" in sql


def test_build_sql_media_links_delete_inside_the_guard():
    # The DELETE for each new table must sit INSIDE its to_regclass guard, so a
    # pre-011 ingest never deletes with no table to refill. Assert the DELETE
    # appears AFTER the guard's IF and BEFORE its END IF.
    sql = _sql()
    for table, col in (("media", "media_type"), ("links", "link_type")):
        guard = f"IF to_regclass('credeals.cre_listing_{table}') IS NOT NULL THEN"
        delete = f"DELETE FROM credeals.cre_listing_{table} WHERE listing_id IN (SELECT id FROM _child_refresh)"
        gi = sql.index(guard)
        di = sql.index(delete, gi)
        ei = sql.index("END IF;", gi)
        assert gi < di < ei


def test_build_sql_media_links_excludes_detail_error_via_child_refresh():
    # Both new INSERTs filter on the shared _child_refresh set, which is built
    # excluding detailError rows -- so the wholesale-replace only fires on a
    # clean detail touch (mirrors the images block).
    sql = _sql()
    assert "WHERE NOT jsonb_path_exists(s.raw_data, '$.**.detailError')" in sql
    assert "$.**.preserveChildCollections ? (@ == true || @ == \"true\")" in sql
    # the media/links INSERTs reference the same _child_refresh gate
    assert sql.count("u.id IN (SELECT id FROM _child_refresh)") >= 5  # contacts, docs, images, media, links


def test_dual_pass_preserve_flag_remains_recursively_guarded():
    sale = _row(_svn(preserveChildCollections=True))
    lease = _row(
        _svn(
            url="https://www.svn.com/property?propertyId=svn-0001-lease",
            transactionMode="lease",
            preserveChildCollections=True,
        )
    )
    merged = merge_rows(sale, lease)
    assert merged["raw_data"]["primary"]["preserveChildCollections"] is True
    assert merged["raw_data"]["secondary_pass"]["preserveChildCollections"] is True
    assert "$.**.preserveChildCollections" in _sql()


# ---------------------------------------------------------------------------
# build_sql(): markdown + numeric COALESCE-keep ; doc_type CASE widening
# ---------------------------------------------------------------------------


def test_build_sql_markdown_coalesce_keep_with_nullif():
    assert "markdown          = COALESCE(NULLIF(EXCLUDED.markdown, ''), t.markdown)" in _sql()


def test_build_sql_numeric_structured_coalesce_keep():
    sql = _sql()
    assert "noi               = COALESCE(EXCLUDED.noi, t.noi)" in sql
    assert "occupancy_rate    = COALESCE(EXCLUDED.occupancy_rate, t.occupancy_rate)" in sql
    assert "units             = COALESCE(EXCLUDED.units, t.units)" in sql
    # arrays COALESCE-keep too
    assert "highlights        = COALESCE(EXCLUDED.highlights, t.highlights)" in sql
    assert "amenities         = COALESCE(EXCLUDED.amenities, t.amenities)" in sql


def test_build_sql_doc_type_case_includes_financials_rent_roll():
    sql = _sql()
    assert "IN ('brochure','om','flyer','floor_plan','financials','rent_roll')" in sql


# ---------------------------------------------------------------------------
# Subprocess (dry-run): detailError row excluded from child-refresh -> no delete
# ---------------------------------------------------------------------------


def _write_artifact(payload, tmp_path, name="artifact.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


def _run_dry(artifact_path, tmp_path, mark_missing=False):
    artifacts_dir = str(tmp_path / "artifacts")
    os.makedirs(artifacts_dir, exist_ok=True)
    cmd = [sys.executable, "cre_ingest.py", "--in", artifact_path,
           "--dry-run", "--keep-artifacts", artifacts_dir]
    if mark_missing:
        cmd += ["--mark-missing", "--mark-missing-floor", "1"]
    result = subprocess.run(cmd, cwd=_COLLECTOR_DIR, capture_output=True, text=True)
    sql_path = os.path.join(artifacts_dir, "ingest.sql")
    sql_text = None
    if os.path.isfile(sql_path):
        with open(sql_path) as f:
            sql_text = f.read()
    return result.returncode, result.stderr, sql_text


def test_detail_error_row_present_but_child_refresh_self_excludes(tmp_path):
    # A row carrying a detailError still ingests (the upsert runs), but the shared
    # _child_refresh set excludes it (NOT jsonb_path_exists ... detailError), so
    # NO media/links/images delete+reinsert fires for it. We assert the staged
    # row carries the detailError marker and the guard clause is present; the
    # _child_refresh exclusion is what enforces the no-delete invariant.
    payload = {
        "runMeta": {"startedAt": _SCRAPED_AT, "finishedAt": _SCRAPED_AT},
        "brokers": [],
        "sources": [{"sourceKey": "svn", "transaction": "sale", "listingsCollected": 1}],
        "listings": [_svn(detailError="timeout", media=[{"url": "https://vimeo.com/1"}])],
    }
    art = _write_artifact(payload, tmp_path)
    rc, stderr, sql = _run_dry(art, tmp_path)
    assert rc == 0, f"ingestor exited {rc}. stderr:\n{stderr}"
    assert sql is not None
    # the detailError marker is staged in raw_data...
    assert "detailError" in sql
    # ...and the _child_refresh gate excludes any detailError-bearing row, so the
    # guarded media/links delete+reinsert never touches it.
    assert "WHERE NOT jsonb_path_exists(s.raw_data, '$.**.detailError')" in sql


def test_base_row_preserves_child_collections(tmp_path):
    payload = {
        "runMeta": {"mode": "full", "startedAt": _SCRAPED_AT, "finishedAt": _SCRAPED_AT},
        "brokers": [],
        "sources": [{"sourceKey": "svn", "transaction": "sale", "listingsCollected": 1}],
        "listings": [_svn(preserveChildCollections=True)],
    }
    art = _write_artifact(payload, tmp_path)
    rc, stderr, sql = _run_dry(art, tmp_path)
    assert rc == 0, f"ingestor exited {rc}. stderr:\n{stderr}"
    assert sql is not None
    assert "preserveChildCollections" in sql
    assert "$.**.preserveChildCollections ? (@ == true || @ == \"true\")" in sql


def test_media_links_archive_emitted_guarded_on_mark_missing(tmp_path):
    # Singleton svn clears the mark-missing floor; the generated SQL must include
    # the existence-guarded media/links archive snapshots alongside the 009
    # contacts/documents archives.
    payload = {
        "runMeta": {"startedAt": _SCRAPED_AT, "finishedAt": _SCRAPED_AT},
        "brokers": [],
        "sources": [{"sourceKey": "svn", "transaction": "sale", "listingsCollected": 3}],
        "listings": [
            _svn(url=f"https://www.svn.com/property?propertyId=svn-{i:04d}-sale", id=f"svn-{i:04d}")
            for i in range(3)
        ],
    }
    art = _write_artifact(payload, tmp_path)
    rc, stderr, sql = _run_dry(art, tmp_path, mark_missing=True)
    assert rc == 0, f"ingestor exited {rc}. stderr:\n{stderr}"
    assert sql is not None
    assert "INSERT INTO credeals.cre_listing_media_archive" in sql
    assert "INSERT INTO credeals.cre_listing_links_archive" in sql
    # archive snapshots are existence-guarded (011 not yet applied)
    assert "to_regclass('credeals.cre_listing_media_archive')" in sql
    assert "to_regclass('credeals.cre_listing_links_archive')" in sql
    # joined to the retired set
    assert "JOIN _retired r" in sql
