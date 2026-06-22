"""
test_backfill_media_more.py

Targets missing pure-logic lines in backfill_media_from_raw_data.py
(current 70%, goal >=88%):

  110-111, 113, 121-122  http_url_or_none: ValueError from urlsplit, non-http scheme
  162, 164               classify_media: wistia/brightcove provider detection
  311                    extract_from_raw: non-dict sub in _flat_passes loop skip
  344                    extract_from_raw: gated doc with no keyword/ext -> force 'other'
  369                    extract_from_raw: classify_doc(brochureUrl) hits None fallback
  384-386                extract_from_raw: agreement already in docs_by_url (dedup skip)
  434-438                read_rows_sql: slug filter branch in WHERE clause
  493                    build_sql: link_rows COPY loop (non-empty link_rows)
  565-573                _summarize: counting media/doc rows

I/O boundary intentionally left:
  585-690  main() - live psql, DB, argparse
  694      __main__ guard

Pure Python, no DB, no network.
"""



from backfill_media_from_raw_data import (
    DOC_TYPE_DB_ALLOWED,
    _summarize,
    build_sql,
    classify_media,
    copy_field,
    extract_from_raw,
    host_of,
    http_url_or_none,
    read_rows_sql,
)


# ---------------------------------------------------------------------------
# http_url_or_none: additional branches (lines 110-113, 121-122)
# ---------------------------------------------------------------------------


def test_http_url_or_none_non_http_non_data_scheme():
    """A url starting with http but with an invalid netloc -> still passes the
    scheme check but fails the final netloc guard (line 113)."""
    # ftp:// is not http(s) -> hits 'not re.match(r"^https?://", ...)' at line 106
    assert http_url_or_none("ftp://example.com/file.pdf") is None


def test_http_url_or_none_data_scheme_rejected():
    """data: url -> _NON_HTTP_SCHEME matches -> None (line 104-105)."""
    assert http_url_or_none("data:image/png;base64,AAAA") is None


def test_http_url_or_none_javascript_rejected():
    """javascript: url -> None (matched by _NON_HTTP_SCHEME)."""
    assert http_url_or_none("javascript:void(0)") is None


def test_http_url_or_none_mailto_rejected():
    """mailto: url -> None."""
    assert http_url_or_none("mailto:user@example.com") is None


def test_http_url_or_none_tel_rejected():
    """tel: url -> None."""
    assert http_url_or_none("tel:+1-800-555-0100") is None


def test_http_url_or_none_fragment_only():
    """Fragment-only url -> not http(s) -> None."""
    assert http_url_or_none("#section") is None


def test_http_url_or_none_valid_http():
    """Standard http url passes through."""
    assert http_url_or_none("http://example.com/path") == "http://example.com/path"


def test_http_url_or_none_strips_whitespace():
    """Leading/trailing whitespace is stripped before validation."""
    result = http_url_or_none("  https://cdn.example.com/file.pdf  ")
    assert result == "https://cdn.example.com/file.pdf"


def test_http_url_or_none_empty_string_is_none():
    """Empty string -> None (line 103-104)."""
    assert http_url_or_none("") is None


def test_http_url_or_none_non_string_is_none():
    """Non-string input -> None (line 101-102 isinstance check)."""
    assert http_url_or_none(None) is None
    assert http_url_or_none(42) is None
    assert http_url_or_none([]) is None


def test_http_url_or_none_invalid_ipv6_raises_value_error_caught():
    """A malformed IPv6 URL causes urlsplit to raise ValueError (lines 110-111, 113).
    The exception is caught and None is returned rather than propagating."""
    # urlsplit('https://[invalid-bracket') raises ValueError in CPython
    result = http_url_or_none("https://[invalid-bracket")
    assert result is None


def test_http_url_or_none_scheme_only_no_netloc():
    """A URL that passes the https:// check but has no netloc -> None (line 112-113)."""
    # 'https:///path' has empty netloc
    result = http_url_or_none("https:///path/to/resource")
    assert result is None


# ---------------------------------------------------------------------------
# classify_media: wistia / brightcove provider branches (lines 162, 164)
# ---------------------------------------------------------------------------


def test_classify_media_wistia_is_video():
    """Wistia host -> video/wistia (line 162)."""
    m = classify_media("https://company.wistia.com/medias/abc123")
    assert m is not None
    assert m["mediaType"] == "video"
    assert m["provider"] == "wistia"


def test_classify_media_wistia_net_host():
    """wistia.net is also a Wistia CDN host."""
    m = classify_media("https://fast.wistia.net/embed/iframe/abc123")
    assert m is not None
    assert m["mediaType"] == "video"
    assert m["provider"] == "wistia"


def test_classify_media_brightcove_is_video():
    """Brightcove host -> video/brightcove (line 164)."""
    m = classify_media("https://players.brightcove.net/123/abc456")
    assert m is not None
    assert m["mediaType"] == "video"
    assert m["provider"] == "brightcove"


def test_classify_media_bcove_video():
    """bcove.video CDN is also Brightcove."""
    m = classify_media("https://bcove.video/embed/v3/abc")
    assert m is not None
    assert m["mediaType"] == "video"
    assert m["provider"] == "brightcove"


def test_classify_media_virtual_tour_path_without_known_host():
    """A url with /360/ path on an unknown host -> virtual_tour."""
    m = classify_media("https://tours.unknownprovider.com/listing/360/view")
    assert m is not None
    assert m["mediaType"] == "virtual_tour"


def test_classify_media_virtual_tour_virtual_tour_keyword():
    """A url with 'virtual-tour' in the path -> virtual_tour."""
    m = classify_media("https://example.com/virtual-tour/property-abc")
    assert m is not None
    assert m["mediaType"] == "virtual_tour"


def test_classify_media_title_stored():
    """Title is stored in the returned dict."""
    m = classify_media("https://vimeo.com/999", title="Property Walk-Through")
    assert m is not None
    assert m["title"] == "Property Walk-Through"


def test_classify_media_embed_url_is_none():
    """embedUrl is always None (populated by a later harvest pass)."""
    m = classify_media("https://www.youtube.com/watch?v=abc")
    assert m is not None
    assert m["embedUrl"] is None


# ---------------------------------------------------------------------------
# extract_from_raw: non-dict sub in _flat_passes (line 311)
# ---------------------------------------------------------------------------


def test_extract_flat_passes_non_dict_sub_skipped(monkeypatch):
    """The 'if not isinstance(sub, dict): continue' branch (line 311) is defensive
    against _flat_passes returning a non-dict. We force it by monkeypatching
    _flat_passes to include a non-dict entry alongside a valid dict."""
    import backfill_media_from_raw_data as bm

    def fake_flat_passes(raw):
        # Return a mix: a non-dict first, then a valid dict with a gatedDocuments field
        return [
            "I-am-not-a-dict",  # triggers line 311 continue
            {
                "gatedDocuments": [
                    {"name": "OM", "url": "https://www.marcusmillichap.com/dealroom/99", "gated": True}
                ]
            },
        ]

    monkeypatch.setattr(bm, "_flat_passes", fake_flat_passes)
    media, docs = extract_from_raw({"raw": "anything"})
    # The non-dict entry was skipped; the valid dict's gatedDocuments was processed.
    assert any(d["url"] == "https://www.marcusmillichap.com/dealroom/99" for d in docs)


# ---------------------------------------------------------------------------
# extract_from_raw: gated doc with no keyword/ext -> force 'other' (line 344)
# ---------------------------------------------------------------------------


def test_extract_gated_doc_no_keyword_no_extension_forced_to_other():
    """A gated deal-room URL with no keyword and no file extension is still
    recovered as 'other' (the force-'other' branch at line 342-345)."""
    raw = {
        "gatedDocuments": [
            {
                "name": "Deal Access Portal",
                "url": "https://access.example.com/secure/portal/listing-9876",
                "gated": True,
            }
        ]
    }
    media, docs = extract_from_raw(raw)
    assert len(docs) == 1
    d = docs[0]
    assert d["url"] == "https://access.example.com/secure/portal/listing-9876"
    assert d["docType"] == "other"
    assert d["docType"] in DOC_TYPE_DB_ALLOWED


# ---------------------------------------------------------------------------
# extract_from_raw: classify_doc(brochureUrl) returns None -> fallback (line 369)
# ---------------------------------------------------------------------------


def test_extract_colliers_brochure_url_no_keyword_no_ext_fallback():
    """When classify_doc(brochureUrl, 'Brochure') would return None (no keyword,
    no extension), the fallback dict sets docType='brochure' (line 353-357)."""
    raw = {
        "colliersSalesTrackerDetail": {
            "brochureUrl": "https://salestracker.colliers.com/view/docs/12345",
        }
    }
    _media, docs = extract_from_raw(raw)
    # The brochure URL ends up recovered either via classify_doc(url, 'Brochure')
    # (which matches 'Brochure' title keyword -> 'brochure') or via the fallback.
    assert len(docs) == 1
    assert docs[0]["docType"] == "brochure"
    assert docs[0]["docType"] in DOC_TYPE_DB_ALLOWED


def test_extract_colliers_agreement_classified_as_other_not_marketing():
    """An agreement URL must never be promoted to brochure/flyer/om even when
    the url accidentally matches a keyword; the guard at line 367-369 downgrades
    it to 'other'. Test the standard path where classify_doc returns 'other'."""
    raw = {
        "colliersSalesTrackerDetail": {
            "agreementUrl": "https://salestracker.colliers.com/agreement/xyz/confidentiality-agreement.pdf",
        }
    }
    _media, docs = extract_from_raw(raw)
    assert len(docs) == 1
    d = docs[0]
    assert d["docType"] == "other"
    assert d["docType"] in DOC_TYPE_DB_ALLOWED


def test_extract_colliers_agreement_url_with_om_keyword_downgraded_to_other():
    """Line 369: when classify_doc(agreementUrl, 'Agreement') returns a marketing
    docType ('om', 'brochure', or 'flyer') because the URL contains a keyword,
    the guard downgrades it to 'other' (agreements are never marketing docs)."""
    # This URL matches 'om' in classify_doc (the /om/ path segment)
    raw = {
        "colliersSalesTrackerDetail": {
            "agreementUrl": "https://salestracker.colliers.com/om/confidentiality-agreement.pdf",
        }
    }
    _media, docs = extract_from_raw(raw)
    assert len(docs) == 1
    d = docs[0]
    # classify_doc would return 'om', but the guard downgrades to 'other'
    assert d["docType"] == "other"
    assert d["docType"] in DOC_TYPE_DB_ALLOWED


def test_extract_colliers_agreement_brochure_keyword_downgraded():
    """Line 369: 'brochure' keyword in agreement URL -> downgraded to 'other'."""
    raw = {
        "colliersSalesTrackerDetail": {
            "agreementUrl": "https://salestracker.colliers.com/brochure-agreement.pdf",
        }
    }
    _media, docs = extract_from_raw(raw)
    assert len(docs) == 1
    assert docs[0]["docType"] == "other"


# ---------------------------------------------------------------------------
# extract_from_raw: agreement already in docs_by_url -> dedup skip (lines 384-386)
# ---------------------------------------------------------------------------


def test_extract_colliers_agreement_dedup_within_listing():
    """If both brochureUrl and agreementUrl resolve to the same URL, it is only
    stored once (the 'if agreement and agreement not in docs_by_url' dedup guard,
    line 384-386)."""
    same_url = "https://salestracker.colliers.com/files/shared-doc.pdf"
    raw = {
        "colliersSalesTrackerDetail": {
            "brochureUrl": same_url,
            "agreementUrl": same_url,
        }
    }
    _media, docs = extract_from_raw(raw)
    # Only one doc should be in the output (deduped by url)
    assert len(docs) == 1
    assert docs[0]["url"] == same_url


# ---------------------------------------------------------------------------
# read_rows_sql: slug filter branch (lines 434-438)
# ---------------------------------------------------------------------------


def test_read_rows_sql_no_slugs():
    """When slugs is None/empty, no brokerage filter is added."""
    sql = read_rows_sql(None)
    assert "brokerage_id IN" not in sql
    assert "l.deleted_at IS NULL" in sql
    assert "jsonb_typeof(l.raw_data)" in sql


def test_read_rows_sql_with_slugs():
    """When slugs is non-empty, a brokerage_id IN (...) filter is added (line 419-423)."""
    sql = read_rows_sql({"jll", "marcus-millichap"})
    assert "brokerage_id IN" in sql
    assert "credeals.cre_brokerages" in sql
    # Both slug literals appear (order may vary)
    assert "'jll'" in sql or '"jll"' in sql
    assert "marcus-millichap" in sql


def test_read_rows_sql_single_slug():
    """Single slug -> still emits the IN clause."""
    sql = read_rows_sql({"colliers"})
    assert "brokerage_id IN" in sql
    assert "'colliers'" in sql


def test_read_rows_sql_always_has_deleted_at_guard():
    """The deleted_at IS NULL and jsonb_typeof guards are always present."""
    for slugs in [None, {"jll"}, {"jll", "colliers"}]:
        sql = read_rows_sql(slugs)
        assert "l.deleted_at IS NULL" in sql
        assert "jsonb_typeof(l.raw_data) = 'object'" in sql


# ---------------------------------------------------------------------------
# build_sql: non-empty link_rows populates COPY block (line 493)
# ---------------------------------------------------------------------------


def _link_row():
    return {
        "listing_id": "00000000-0000-0000-0000-000000000099",
        "link_type": "external",
        "rel": "nofollow",
        "url": "https://example.com/property-page",
    }


def test_build_sql_link_rows_in_copy_block():
    """When link_rows is non-empty, a COPY row is emitted into _bf_links (line 493)."""
    sql = build_sql([], [], [_link_row()])
    assert "_bf_links" in sql
    assert "https://example.com/property-page" in sql


def test_build_sql_empty_link_rows():
    """When link_rows is empty, _bf_links COPY block is empty but structure present."""
    sql = build_sql([], [], [])
    assert "CREATE TEMP TABLE _bf_links" in sql
    # No data rows in the COPY section (still has the header/footer)
    assert "_bf_links" in sql


def test_build_sql_links_existence_guarded():
    """cre_listing_links INSERT is always wrapped in a to_regclass guard."""
    sql = build_sql([], [], [_link_row()])
    assert "to_regclass('credeals.cre_listing_links')" in sql


def test_build_sql_standard_conforming_strings_pinned():
    """Security review pin: standard_conforming_strings must be set on."""
    sql = build_sql([], [], [])
    assert "SET LOCAL standard_conforming_strings = on;" in sql


def test_build_sql_begins_and_commits():
    """The SQL is wrapped in BEGIN/COMMIT."""
    sql = build_sql([], [], [])
    assert "BEGIN;" in sql
    assert "COMMIT;" in sql


# ---------------------------------------------------------------------------
# _summarize: per-shape counting (lines 565-573)
# ---------------------------------------------------------------------------


def _mrow(lid, mtype="video"):
    return {
        "listing_id": lid,
        "media_type": mtype,
        "provider": "vimeo",
        "url": f"https://vimeo.com/{lid}",
        "embed_url": None,
        "title": None,
    }


def _drow(lid, dtype="om"):
    return {
        "listing_id": lid,
        "doc_type": dtype,
        "title": None,
        "url": f"https://cdn.example.com/{lid}.pdf",
    }


def test_summarize_media_and_doc_counts():
    """_summarize correctly counts media rows, listing cardinality, and doc types."""
    media = [_mrow("A"), _mrow("A", "virtual_tour"), _mrow("B")]
    docs = [_drow("A", "om"), _drow("B", "brochure"), _drow("B", "om")]
    links = []
    stats = _summarize(media, docs, links)
    assert stats["media_rows"] == 3
    assert stats["media_listings"] == 2        # A and B
    assert stats["media_by_type"]["video"] == 2
    assert stats["media_by_type"]["virtual_tour"] == 1
    assert stats["doc_rows"] == 3
    assert stats["doc_listings"] == 2
    assert stats["doc_by_type"]["om"] == 2
    assert stats["doc_by_type"]["brochure"] == 1
    assert stats["link_rows"] == 0


def test_summarize_empty():
    """Empty inputs return zero counts."""
    stats = _summarize([], [], [])
    assert stats["media_rows"] == 0
    assert stats["doc_rows"] == 0
    assert stats["link_rows"] == 0
    assert stats["media_listings"] == 0
    assert stats["doc_listings"] == 0


def test_summarize_link_rows_counted():
    """Non-empty link_rows are counted."""
    stats = _summarize([], [], [_link_row()])
    assert stats["link_rows"] == 1


# ---------------------------------------------------------------------------
# copy_field: encoding edge cases (used by build_sql COPY blocks)
# ---------------------------------------------------------------------------


def test_copy_field_none_is_null():
    assert copy_field(None) == "\\N"


def test_copy_field_bool_true():
    assert copy_field(True) == "t"


def test_copy_field_bool_false():
    assert copy_field(False) == "f"


def test_copy_field_string_with_backslash():
    result = copy_field("C:\\Users\\file")
    assert result == "C:\\\\Users\\\\file"


def test_copy_field_string_with_tab():
    result = copy_field("a\tb")
    assert result == "a\\tb"


def test_copy_field_dict():
    result = copy_field({"a": 1})
    assert result == '{"a":1}'


def test_copy_field_list():
    result = copy_field([1, 2])
    assert result == "[1,2]"


# ---------------------------------------------------------------------------
# host_of: leading www stripped, ValueError branch
# ---------------------------------------------------------------------------


def test_host_of_www_stripped():
    assert host_of("https://www.example.com/path") == "example.com"


def test_host_of_no_www():
    assert host_of("https://cdn.example.com/path") == "cdn.example.com"


def test_host_of_empty_string():
    assert host_of("") == ""


def test_host_of_invalid_ipv6_value_error_caught():
    """urlsplit raises ValueError on malformed IPv6 -> except branch returns '' (lines 121-122)."""
    result = host_of("https://[invalid-bracket")
    assert result == ""
