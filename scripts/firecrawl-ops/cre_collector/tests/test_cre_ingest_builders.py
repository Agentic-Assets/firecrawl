"""
test_cre_ingest_builders.py

Pure-transform unit coverage for the uncovered branches of cre_ingest.py's
field normalizers and the to_row / merge_rows row builders. Complements the
existing ingest suite (test_dq_guards, test_norm_status_canonical_and_guards,
test_ingest_status_activation, test_price_*, test_*_mark_missing, ...) by
targeting branches those files leave uncovered: the norm_cap_rate edge grid,
extra_facts_or_none / om_facts_rows provenance clamps, parse_source_lastmod,
copy_field COPY encoding, the to_row id-derivation matrix, the to_row price /
child / media / link staging, and the merge_rows COALESCE-keep / drop-wins /
dual-raw_data fan-in.

No network, no live DB. Calls the real cre_ingest functions directly with
synthetic listing dicts. Where SQL shape matters, it asserts against the
already-covered builders only enough to lock the branch (build_sql shape is
owned by the price/history/mark-missing test files).

Re-implementation rule (tests/CLAUDE.md): assertions encode the observable
contract, never a copy of production logic.
"""

import hashlib
from datetime import datetime, timezone

import pytest

import cre_ingest as ci

_SCRAPED_AT = datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc).isoformat()


def _row(listing, brokers=None):
    return ci.to_row(listing, brokers or {}, _SCRAPED_AT)


# ===========================================================================
# norm_cap_rate: full edge grid (line 192-202)
# Contract (CLAUDE.md): decimal fraction; drops non-numeric, <= 0, percent
# inputs >= 30, and resulting fraction >= 0.5.
# ===========================================================================


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),            # non-numeric
        ("6.5", None),           # string is non-numeric (isinstance int/float gate)
        (True, None),            # bool -> frac 1.0 -> >= 0.5 dropped
        (0, None),               # <= 0
        (-5, None),              # negative
        (0.065, 0.065),          # already a fraction, kept
        (0.5, None),             # fraction == 0.5 dropped (strict < 0.5)
        (0.6, None),             # fraction > 0.5 dropped
        (1, None),               # ==1 -> frac 1.0 dropped
        (6.5, 0.065),            # percent -> fraction
        (25, 0.25),              # percent < 30 -> fraction
        (29.9, round(0.299, 6)), # just under the 30-percent ceiling
        (30, None),              # percent == 30 dropped (elif v < 30)
        (35, None),              # percent >= 30 dropped
    ],
)
def test_norm_cap_rate_edge_grid(value, expected):
    assert ci.norm_cap_rate(value) == expected


def test_norm_cap_rate_rounds_to_six_places():
    # 6.789 percent -> 0.06789 (already <= 6 dp); use a value that exercises round.
    assert ci.norm_cap_rate(6.1234567) == round(6.1234567 / 100.0, 6)


# ===========================================================================
# norm_property_type keyword loop + fallbacks (line 182-189)
# ===========================================================================


def test_norm_property_type_first_match_wins():
    # "mixed" precedes everything in PROPERTY_TYPE_RULES.
    assert ci.norm_property_type("Mixed Use / Retail") == "mixed_use"


def test_norm_property_type_keyword_hits():
    assert ci.norm_property_type("Large Warehouse") == "industrial"
    assert ci.norm_property_type("Medical Office Building") == "office"
    assert ci.norm_property_type("Self-Storage Facility") == "special_purpose"


def test_norm_property_type_unknown_is_other():
    assert ci.norm_property_type("Quokka Habitat") == "other"


def test_norm_property_type_empty_and_non_str_is_none():
    assert ci.norm_property_type("") is None
    assert ci.norm_property_type(None) is None
    assert ci.norm_property_type(42) is None


# ===========================================================================
# norm_state US_STATES.get fallback (line 164-170)
# ===========================================================================


def test_norm_state_full_name_maps(): assert ci.norm_state("California") == "CA"
def test_norm_state_two_letter_code(): assert ci.norm_state("ca") == "CA"
def test_norm_state_unknown_is_none(): assert ci.norm_state("ZZ") is None
def test_norm_state_non_str_is_none(): assert ci.norm_state(7) is None


# ===========================================================================
# extra_facts_or_none: type coercion + empty drops (line 340-372)
# ===========================================================================


def test_extra_facts_non_dict_is_none():
    assert ci.extra_facts_or_none(["a"]) is None
    assert ci.extra_facts_or_none("x") is None


def test_extra_facts_empty_dict_is_none():
    assert ci.extra_facts_or_none({}) is None


def test_extra_facts_all_empty_values_is_none():
    assert ci.extra_facts_or_none({"a": "", "b": None, "c": "   "}) is None


def test_extra_facts_empty_after_strip_key_dropped():
    # A whitespace-only key strips to "" and is dropped; the real key survives.
    assert ci.extra_facts_or_none({"   ": "v", "real": "x"}) == {"real": "x"}


def test_extra_facts_keeps_supported_value_types_and_strips_keys():
    out = ci.extra_facts_or_none(
        {
            " key1 ": " val ",   # trimmed key + trimmed string value
            "k_none": None,      # dropped
            "k_empty": "",       # dropped (empty after strip)
            5: "x",              # non-string key dropped
            "k_bool": True,      # bool kept
            "k_num": 3.5,        # number kept
            "k_list": [1, 2],    # non-empty list kept
            "k_empty_list": [],  # empty list dropped
            "k_dict": {"a": 1},  # non-empty dict kept
            "k_empty_dict": {},  # empty dict dropped
            "k_tuple": ("t",),   # unsupported type dropped
        }
    )
    assert out == {"key1": "val", "k_bool": True, "k_num": 3.5,
                   "k_list": [1, 2], "k_dict": {"a": 1}}


# ===========================================================================
# om_facts_rows: provenance contract + clamps (line 378-417)
# ===========================================================================


def test_om_facts_non_list_is_empty():
    assert ci.om_facts_rows("x") == []
    assert ci.om_facts_rows(None) == []


def test_om_facts_drops_non_dict_items():
    assert ci.om_facts_rows([42, "x", None]) == []


def test_om_facts_requires_full_provenance():
    # Missing source_doc_url or parser_version -> dropped (never fabricate audit).
    assert ci.om_facts_rows([{"factKey": "noi"}]) == []
    assert ci.om_facts_rows(
        [{"factKey": "noi", "sourceDocUrl": "https://x.com/om.pdf"}]
    ) == []
    assert ci.om_facts_rows(
        [{"factKey": "noi", "parserVersion": "v1"}]
    ) == []


def test_om_facts_full_row_with_clamps():
    rows = ci.om_facts_rows(
        [
            {
                "factKey": "noi",
                "sourceDocUrl": "https://x.com/om.pdf",
                "parserVersion": "v1",
                "factGroup": "weird",   # not in allowed set -> clamps to scalar
                "confidence": 0.7,
                "factValueNum": 12345,
                "unitCount": 10,
                "factValueText": "Net operating income",
            }
        ]
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["factGroup"] == "scalar"
    assert r["factKey"] == "noi"
    assert r["sourceDocUrl"] == "https://x.com/om.pdf"
    assert r["parserVersion"] == "v1"
    assert r["confidence"] == 0.7
    assert r["factValueNum"] == 12345.0
    assert r["unitCount"] == 10


def test_om_facts_out_of_range_confidence_clamps_to_none():
    rows = ci.om_facts_rows(
        [
            {
                "factKey": "k",
                "sourceDocUrl": "https://x.com/d.pdf",
                "parserVersion": "v2",
                "confidence": 5,  # > 1 -> num_or_none(hi=1) -> None
            }
        ]
    )
    assert rows[0]["confidence"] is None


def test_om_facts_tiny_negative_confidence_clamped_to_none():
    # A tiny negative survives num_or_none(lo=-0.0001) but fails the explicit
    # 0 <= conf <= 1 re-check, so it clamps to None.
    rows = ci.om_facts_rows(
        [
            {
                "factKey": "k",
                "sourceDocUrl": "https://x.com/d.pdf",
                "parserVersion": "v",
                "confidence": -0.00005,
            }
        ]
    )
    assert rows[0]["confidence"] is None


def test_om_facts_snake_case_aliases_accepted():
    rows = ci.om_facts_rows(
        [
            {
                "fact_key": "rent",
                "source_doc_url": "https://x.com/rr.pdf",
                "parser_version": "v3",
                "fact_group": "rent_roll",
            }
        ]
    )
    assert rows[0]["factKey"] == "rent"
    assert rows[0]["factGroup"] == "rent_roll"


# ===========================================================================
# transaction_type_of (line 425-430)
# ===========================================================================


def test_transaction_type_of_sale_and_lease():
    assert ci.transaction_type_of({"transactionType": "For Sale and Lease"}) == "sale_or_lease"
    assert ci.transaction_type_of({"transactionType": "Sale or Let"}) == "sale_or_lease"


def test_transaction_type_of_mode_fallback():
    assert ci.transaction_type_of({"transactionMode": "lease"}) == "lease"
    assert ci.transaction_type_of({"transactionMode": "sale"}) == "sale"


def test_transaction_type_of_unknown_mode_is_none():
    assert ci.transaction_type_of({"transactionMode": "barter"}) is None
    assert ci.transaction_type_of({}) is None


# ===========================================================================
# parse_source_lastmod (line 685-711)
# ===========================================================================


def test_parse_lastmod_z_suffix_to_offset():
    assert ci.parse_source_lastmod("2026-03-18T14:23:05Z") == "2026-03-18T14:23:05+00:00"


def test_parse_lastmod_space_separator_via_regex():
    assert ci.parse_source_lastmod("2026-03-18 14:23:05") == "2026-03-18T14:23:05"


def test_parse_lastmod_date_only():
    # date-only parses straight through datetime.fromisoformat (midnight).
    assert ci.parse_source_lastmod("2026-03-18") == "2026-03-18T00:00:00"


def test_parse_lastmod_regex_branch_on_trailing_garbage():
    # fromisoformat rejects the trailing " (EST)", but the leading regex matches
    # a valid timestamp prefix, which is returned.
    assert ci.parse_source_lastmod("2026-03-18T14:23:05 (EST)") == "2026-03-18T14:23:05"


def test_parse_lastmod_out_of_range_rejected():
    assert ci.parse_source_lastmod("2024-13-45") is None


def test_parse_lastmod_garbage_and_empty_and_none():
    assert ci.parse_source_lastmod("not a date") is None
    assert ci.parse_source_lastmod("   ") is None
    assert ci.parse_source_lastmod(None) is None
    assert ci.parse_source_lastmod(42) is None


def test_group_source_lastmod_prefers_lastupdated_within_listing():
    # Within ONE listing, lastUpdated is scanned before dateModified.
    flat = [{"lastUpdated": "2026-05-05T09:00:00Z", "dateModified": "2020-01-01"}]
    assert ci.group_source_lastmod(flat) == "2026-05-05T09:00:00+00:00"


def test_group_source_lastmod_first_listing_with_signal_wins():
    # The outer scan is listing-by-listing: the first listing carrying any
    # parseable value wins, even if a later listing has a newer date.
    flat = [
        {"dateModified": "2026-01-01"},
        {"lastUpdated": "2026-05-05T09:00:00Z"},
    ]
    assert ci.group_source_lastmod(flat) == "2026-01-01T00:00:00"


def test_group_source_lastmod_none_when_no_signal():
    assert ci.group_source_lastmod([{"foo": "bar"}, {}]) is None


# ===========================================================================
# copy_field: COPY text encoding (line 1060-1076)
# ===========================================================================


def test_copy_field_null():
    assert ci.copy_field(None) == "\\N"


def test_copy_field_bool():
    assert ci.copy_field(True) == "t"
    assert ci.copy_field(False) == "f"


def test_copy_field_integer_float_truncates():
    assert ci.copy_field(5.0) == "5"


def test_copy_field_non_integer_float_kept():
    assert ci.copy_field(5.25) == "5.25"


def test_copy_field_dict_and_list_json_encoded():
    assert ci.copy_field({"a": 1}) == '{"a":1}'
    assert ci.copy_field([1, 2]) == "[1,2]"


def test_copy_field_escapes_control_chars():
    assert ci.copy_field("a\tb\nc\rd\\e") == "a\\tb\\nc\\rd\\\\e"


# ===========================================================================
# to_row id derivation matrix (line 726-752)
# ===========================================================================


def test_to_row_url_sha1_fallback_when_no_id():
    url = "https://cbre.com/p/xyz"
    r = _row({"sourceKey": "cbre", "url": url})
    expected = "url:" + hashlib.sha1(url.encode()).hexdigest()[:16]
    assert r["external_id"] == expected


def test_to_row_blank_id_falls_to_sha1():
    r = _row({"sourceKey": "cbre", "url": "https://cbre.com/q", "id": "   "})
    assert r["external_id"].startswith("url:")


def test_to_row_buildout_propertyid_strips_sale_suffix():
    r = _row({"sourceKey": "svn",
              "url": "https://svn.com/x?propertyId=1614726-sale", "id": "99"})
    # The propertyId base wins over the raw inventory id, and -sale is stripped.
    assert r["external_id"] == "1614726"


def test_to_row_buildout_propertyid_strips_lease_suffix():
    r = _row({"sourceKey": "lee-associates",
              "url": "https://buildout.com/x?propertyId=42-lease"})
    assert r["external_id"] == "42"


def test_to_row_buildout_no_pid_uses_raw_id():
    r = _row({"sourceKey": "svn", "url": "https://svn.com/x", "id": "99"})
    assert r["external_id"] == "99"


def test_to_row_franklin_street_buildout_propertyid_strips_suffix():
    r = _row({
        "sourceKey": "franklin-street",
        "url": "https://www.franklinst.com/properties/?propertyId=777-sale",
        "id": "raw-777",
    })
    assert r["external_id"] == "777"


@pytest.mark.parametrize(
    "source_key,prefix",
    [("cbre-dealflow", "dealflow:"), ("jll-investor", "investor:"),
     ("colliers-main", "main:")],
)
def test_to_row_folded_prefixes(source_key, prefix):
    r = _row({"sourceKey": source_key, "url": "https://x.com/p", "id": "abc"})
    assert r["external_id"] == prefix + "abc"


def test_to_row_unmapped_source_is_none():
    assert _row({"sourceKey": "nope", "url": "https://x.com", "id": "y"}) is None


def test_to_row_missing_or_non_http_url_is_none():
    assert _row({"sourceKey": "cbre", "id": "x"}) is None
    assert _row({"sourceKey": "cbre", "url": "ftp://x.com", "id": "x"}) is None
    assert _row({"sourceKey": "cbre", "url": 123, "id": "x"}) is None


# ===========================================================================
# to_row scalar mapping branches (line 754-995)
# ===========================================================================


def test_to_row_price_per_sf_computed_from_price_and_size():
    r = _row({"sourceKey": "cbre", "url": "https://cbre.com/h", "id": "1",
              "salePriceUsd": 1000000, "buildingSizeSqft": 5000})
    assert r["sale_price_per_sf"] == 200.0


def test_to_row_per_sf_sale_text_suppresses_absolute_price():
    r = _row({"sourceKey": "lee-associates",
              "url": "https://buildout.com/x?propertyId=5",
              "salePriceText": "$6.00/SF", "salePriceUsd": 6.0})
    assert r["sale_price_usd"] is None
    assert r["sale_price_per_sf"] == 6.0


def test_to_row_nai_pound_currency_label_recovered_as_usd():
    r = _row({"sourceKey": "nai-global", "url": "https://nai.com/x", "id": "1",
              "salePriceText": "POUND 545000"})
    assert r["sale_price_usd"] == 545000.0


def test_to_row_size_from_text_when_numeric_absent():
    r = _row({"sourceKey": "cbre", "url": "https://cbre.com/s", "id": "1",
              "sizeText": "10,000 SF"})
    assert r["size_sf"] == 10000.0


def test_to_row_lot_size_from_acres():
    r = _row({"sourceKey": "cbre", "url": "https://cbre.com/l", "id": "1",
              "lotSizeAcres": 2})
    assert r["lot_size_sf"] == 2 * ci.SQFT_PER_ACRE


def test_to_row_year_built_clamped():
    assert _row({"sourceKey": "cbre", "url": "https://x.com", "id": "1",
                 "yearBuilt": 1850})["year_built"] == 1850
    assert _row({"sourceKey": "cbre", "url": "https://x.com", "id": "1",
                 "yearBuilt": 1600})["year_built"] is None
    assert _row({"sourceKey": "cbre", "url": "https://x.com", "id": "1",
                 "yearBuilt": 2200})["year_built"] is None


def test_to_row_title_truncated_to_500():
    long_name = "Z" * 600
    r = _row({"sourceKey": "cbre", "url": "https://x.com", "id": "1", "name": long_name})
    assert len(r["title"]) == 500


def test_to_row_lat_lng_clamped_to_bounds():
    r = _row({"sourceKey": "cbre", "url": "https://x.com", "id": "1",
              "latitude": 200, "longitude": -400})
    assert r["lat"] is None and r["lng"] is None


# --- contacts: detailed, broker fallback, license, isPrimary -----------------


def test_to_row_contacts_detailed_first_is_primary():
    r = _row({"sourceKey": "marcus-millichap", "url": "https://mm.com/x", "id": "1",
              "contactsDetailed": [
                  {"name": "Lead", "license": "01234567",
                   "profileUrl": "https://mm.com/lead"},
                  {"email": "two@x.com"},
              ]})
    assert [c["isPrimary"] for c in r["contacts"]] == [True, False]
    assert r["contacts"][0]["license"] == "01234567"


def test_to_row_contacts_detailed_all_empty_falls_to_brokers():
    brokers = {0: {"name": "BrokerA", "email": "a@x.com"}}
    r = _row(
        {"sourceKey": "cbre", "url": "https://cbre.com/x", "id": "1",
         "contactsDetailed": [{"foo": "bar"}],  # no identifying field -> skipped
         "brokerIds": [0]},
        brokers=brokers,
    )
    assert [c["name"] for c in r["contacts"]] == ["BrokerA"]


def test_to_row_broker_fallback_skips_unidentified_broker():
    brokers = {0: {"title": "no name no email"}, 1: {"name": "Real"}}
    r = _row(
        {"sourceKey": "cbre", "url": "https://cbre.com/y", "id": "1", "brokerIds": [0, 1]},
        brokers=brokers,
    )
    assert [c["name"] for c in r["contacts"]] == ["Real"]


# --- documents (brochures + documents channel), images, media, links --------


def test_to_row_documents_from_both_channels_with_doctype():
    r = _row({"sourceKey": "cbre", "url": "https://cbre.com/i", "id": "1",
              "brochures": [{"name": "B", "url": "https://x.com/b.pdf"}],
              "documents": [{"title": "D", "url": "https://x.com/d.pdf", "docType": "om"}]})
    assert [d["docType"] for d in r["documents"]] == ["brochure", "om"]


def test_to_row_documents_skip_non_http_url():
    r = _row({"sourceKey": "cbre", "url": "https://cbre.com/i2", "id": "1",
              "brochures": [{"name": "B", "url": "not-a-url"}]})
    assert r["documents"] == []


def test_to_row_images_only_http_strings_with_order():
    r = _row({"sourceKey": "cbre", "url": "https://cbre.com/p", "id": "1",
              "photos": ["https://x.com/1.jpg", "bad", "https://x.com/2.jpg"]})
    # 'bad' is skipped but the enumerate index is preserved for the kept ones.
    assert [(im["url"], im["order"], im["isPrimary"]) for im in r["images"]] == [
        ("https://x.com/1.jpg", 0, True),
        ("https://x.com/2.jpg", 2, False),
    ]


def test_to_row_media_string_and_dict_forms():
    r = _row({"sourceKey": "cbre", "url": "https://cbre.com/j", "id": "1",
              "media": ["https://x.com/v.mp4",
                        {"url": "https://x.com/t", "mediaType": "video", "provider": "yt",
                         "embedUrl": "https://x.com/embed", "title": "Tour"},
                        {"url": "not-a-url"}]})  # filtered
    assert [m["mediaType"] for m in r["media"]] == ["other", "video"]
    assert r["media"][1]["embedUrl"] == "https://x.com/embed"


def test_to_row_links_string_and_dict_forms():
    r = _row({"sourceKey": "cbre", "url": "https://cbre.com/k", "id": "1",
              "links": ["https://x.com/l",
                        {"url": "https://x.com/l2", "linkType": "website", "rel": "canonical"},
                        {"url": "not-a-url"}]})  # filtered
    assert [ln["linkType"] for ln in r["links"]] == ["other", "website"]
    assert r["links"][1]["rel"] == "canonical"


def test_to_row_discards_legacy_om_facts_payload():
    r = _row({
        "sourceKey": "cbre",
        "url": "https://cbre.com/om",
        "id": "1",
        "omFacts": [{
            "factKey": "noi",
            "factValueNum": 1000000,
            "sourceDocUrl": "https://cbre.com/om.pdf",
            "parserVersion": "legacy/1",
        }],
    })
    assert r["om_facts"] == []

    sql = ci.build_sql([r], [], _SCRAPED_AT, set())
    copy_start = sql.index("COPY _stage")
    copy_data_start = sql.index("\n", copy_start) + 1
    copy_data_end = sql.index("\n\\.\n", copy_data_start)
    fields = sql[copy_data_start:copy_data_end].split("\t")
    assert "legacy/1" in sql  # retained only inside raw_data provenance
    assert fields[ci.STAGE_COLS.index("om_facts")] == "[]"


def test_build_sql_discards_direct_legacy_om_facts_rows():
    row = _row({"sourceKey": "cbre", "url": "https://cbre.com/om", "id": "1"})
    row["om_facts"] = [{
        "factKey": "noi",
        "sourceDocUrl": "https://cbre.com/om.pdf",
        "parserVersion": "legacy/1",
    }]
    sql = ci.build_sql([row], [], _SCRAPED_AT, set())
    assert "legacy/1" not in sql


# ===========================================================================
# merge_rows: COALESCE-keep, sale_or_lease, child fill, drop-wins, dual raw
# (line 998-1052)
# ===========================================================================


def test_merge_sale_plus_lease_modes_to_sale_or_lease():
    a = _row({"sourceKey": "svn", "url": "https://svn.com/x?propertyId=100-sale",
              "transactionMode": "sale", "salePriceUsd": 500000})
    b = _row({"sourceKey": "svn", "url": "https://svn.com/x?propertyId=100-lease",
              "transactionMode": "lease", "leaseRateMin": 20})
    m = ci.merge_rows(a, b)
    assert m["transaction_type"] == "sale_or_lease"
    assert m["sale_price_usd"] == 500000.0     # a kept
    assert m["lease_rate_min"] == 20.0         # filled from b


def test_merge_secondary_sale_or_lease_promotes():
    # b already classified sale_or_lease (via transactionType text), modes do
    # not span sale+lease; the b-is-sale_or_lease branch still promotes.
    a = _row({"sourceKey": "cbre", "url": "https://cbre.com/sl", "id": "1",
              "transactionMode": "sale"})
    b = _row({"sourceKey": "cbre", "url": "https://cbre.com/sl", "id": "1",
              "transactionType": "For Sale or Lease"})
    assert b["transaction_type"] == "sale_or_lease"
    assert ci.merge_rows(a, b)["transaction_type"] == "sale_or_lease"


def test_merge_fills_none_scalar_from_b():
    a = _row({"sourceKey": "cbre", "url": "https://cbre.com/a", "id": "1",
              "transactionMode": "sale"})
    b = _row({"sourceKey": "cbre", "url": "https://cbre.com/a", "id": "1",
              "transactionMode": "sale", "salePriceUsd": 999999})
    assert ci.merge_rows(a, b)["sale_price_usd"] == 999999.0


def test_merge_keeps_a_scalar_when_b_none_coalesce_keep():
    a = _row({"sourceKey": "cbre", "url": "https://cbre.com/c", "id": "1",
              "transactionMode": "sale", "salePriceUsd": 111})
    b = _row({"sourceKey": "cbre", "url": "https://cbre.com/c", "id": "1",
              "transactionMode": "sale"})
    assert ci.merge_rows(a, b)["sale_price_usd"] == 111.0  # never blanked


def test_merge_fills_empty_child_lists_from_b():
    a = _row({"sourceKey": "cbre", "url": "https://cbre.com/d", "id": "1"})
    b = _row({"sourceKey": "cbre", "url": "https://cbre.com/d", "id": "1",
              "contactsDetailed": [{"name": "Jane"}]})
    assert [c["name"] for c in ci.merge_rows(a, b)["contacts"]] == ["Jane"]


def test_merge_markdown_prefers_longer():
    a = _row({"sourceKey": "cbre", "url": "https://cbre.com/e", "id": "1",
              "markdown": "short"})
    b = _row({"sourceKey": "cbre", "url": "https://cbre.com/e", "id": "1",
              "markdown": "a much longer markdown body wins"})
    assert ci.merge_rows(a, b)["markdown"] == "a much longer markdown body wins"


def test_merge_extra_facts_union_a_wins_collision():
    a = _row({"sourceKey": "cbre", "url": "https://cbre.com/f", "id": "1",
              "extraFacts": {"x": "1"}})
    b = _row({"sourceKey": "cbre", "url": "https://cbre.com/f", "id": "1",
              "extraFacts": {"x": "2", "y": "3"}})
    assert ci.merge_rows(a, b)["extra_facts"] == {"x": "1", "y": "3"}


def test_merge_status_drop_signal_wins_over_transitional():
    a = _row({"sourceKey": "cushman-wakefield", "url": "https://cw.com/1", "id": "1",
              "listingStatus": "Under Contract"})
    b = _row({"sourceKey": "cushman-wakefield", "url": "https://cw.com/1", "id": "1",
              "listingStatus": "Sold"})
    assert ci.merge_rows(a, b)["status"] == "sold"


def test_merge_status_fills_none_from_b():
    a = _row({"sourceKey": "cbre", "url": "https://cbre.com/g", "id": "1"})
    b = _row({"sourceKey": "cushman-wakefield", "url": "https://cw.com/g", "id": "1",
              "listingStatus": "Pending"})
    assert a["status"] is None
    assert ci.merge_rows(a, b)["status"] == "pending"


def test_merge_dual_raw_data_when_payloads_differ():
    a = _row({"sourceKey": "cbre", "url": "https://cbre.com/h", "id": "1", "city": "A"})
    b = _row({"sourceKey": "cbre", "url": "https://cbre.com/h", "id": "1", "city": "B"})
    m = ci.merge_rows(a, b)
    assert set(m["raw_data"].keys()) == {"primary", "secondary_pass"}
