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
import json
import sys
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


def test_to_row_uses_explicit_detail_observation_not_artifact_finish():
    r = _row(
        {
            "sourceKey": "jll",
            "url": "https://property.jll.com/listings/1",
            "id": "1",
            "detailObservedAt": "2026-06-14T21:30:00Z",
        }
    )
    assert r["scraped_at"] == "2026-06-14T21:30:00+00:00"


def test_to_row_uses_inventory_observation_for_authoritative_feed():
    r = _row(
        {
            "sourceKey": "svn",
            "url": "https://svn.com/x?propertyId=1-sale",
            "id": "1",
            "inventoryObservedAt": "2026-06-14T22:00:00Z",
            "freshnessProvenance": {
                "detailScope": "authoritative_inventory_feed"
            },
        }
    )
    assert r["scraped_at"] == "2026-06-14T22:00:00+00:00"


def test_to_row_uses_source_revision_validation_time_for_scraped_at():
    r = _row(
        {
            "sourceKey": "colliers-main",
            "url": "https://www.colliers.com/en/properties/x/usa12345",
            "id": "usa12345",
            "detailObservedAt": "2026-07-28T10:00:00Z",
            "freshnessProvenance": {
                "detailScope": "detail_page",
                "cacheDisposition": "source_revision_cache",
                "validatedAt": "2026-07-29T12:00:30Z",
            },
        }
    )
    assert r["scraped_at"] == "2026-07-29T12:00:30+00:00"


def test_strict_artifact_freshness_rejects_completion_time_only():
    payload = {
        "runMeta": {
            "freshness": {
                "generationId": "generation-1",
                "generationStartedAt": "2026-07-29T12:00:00Z",
                "requireFreshDetails": True,
            }
        },
        "listings": [
            {
                "sourceKey": "jll",
                "inventoryObservedAt": "2026-07-29T12:00:01Z",
                "freshnessProvenance": {
                    "generationId": "generation-1",
                    "detailScope": "detail_page",
                    "cacheDisposition": "live",
                },
            }
        ],
    }
    with pytest.raises(ValueError, match="stale detail"):
        ci.validate_strict_artifact_freshness(payload)


def _strict_freshness_payload(
    source="jll",
    *,
    detail_scope="detail_page",
    preserve_children=False,
):
    observed = "2026-07-29T12:00:01Z"
    listing = {
        "sourceKey": source,
        "inventoryObservedAt": observed,
        "freshnessProvenance": {
            "generationId": "generation-1",
            "detailScope": detail_scope,
            "cacheDisposition": "live",
        },
    }
    if detail_scope == "detail_page":
        listing["detailObservedAt"] = observed
    if preserve_children:
        listing["preserveChildCollections"] = True
    return {
        "runMeta": {
            "freshness": {
                "generationId": "generation-1",
                "generationStartedAt": "2026-07-29T12:00:00Z",
                "requireFreshDetails": True,
            }
        },
        "sources": [{"sourceKey": source}],
        "listings": [listing],
    }


@pytest.mark.parametrize(
    "field_path",
    [
        ("runMeta", "freshness", "generationStartedAt"),
        ("listings", 0, "inventoryObservedAt"),
        ("listings", 0, "detailObservedAt"),
    ],
)
def test_strict_artifact_freshness_rejects_naive_timestamps(field_path):
    payload = _strict_freshness_payload()
    target = payload
    for part in field_path[:-1]:
        target = target[part]
    target[field_path[-1]] = "2026-07-29T12:00:01"

    with pytest.raises(ValueError, match="timezone"):
        ci.validate_strict_artifact_freshness(payload)


def test_strict_source_revision_cache_rejects_naive_validation_timestamp():
    payload = _strict_freshness_payload()
    listing = payload["listings"][0]
    listing.pop("detailObservedAt")
    listing["freshnessProvenance"].update(
        {
            "cacheDisposition": "source_revision_cache",
            "validatedAt": "2026-07-29T12:00:01",
        }
    )

    with pytest.raises(ValueError, match="timezone"):
        ci.validate_strict_artifact_freshness(payload)


@pytest.mark.parametrize(
    "field_path",
    [
        ("runMeta", "freshness", "generationStartedAt"),
        ("listings", 0, "inventoryObservedAt"),
        ("listings", 0, "detailObservedAt"),
    ],
)
def test_strict_artifact_rejects_future_timestamps_beyond_clock_skew(field_path):
    payload = _strict_freshness_payload()
    target = payload
    for part in field_path[:-1]:
        target = target[part]
    target[field_path[-1]] = "2026-07-29T12:05:01Z"

    with pytest.raises(ValueError, match="clock-skew"):
        ci.validate_strict_artifact_freshness(
            payload,
            now=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
        )


def test_nonstrict_direct_artifact_rejects_future_listing_observation():
    payload = {
        "runMeta": {"freshness": {"requireFreshDetails": False}},
        "listings": [
            {
                "sourceKey": "jll",
                "inventoryObservedAt": "2026-07-29T12:05:01Z",
            }
        ],
    }

    with pytest.raises(ValueError, match="clock-skew"):
        ci.validate_strict_artifact_freshness(
            payload,
            now=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize("source", ["srs", "hanley", "kidder-mathews"])
def test_strict_child_preserving_authoritative_feed_is_accepted(source):
    ci.validate_strict_artifact_freshness(
        _strict_freshness_payload(
            source,
            detail_scope="authoritative_inventory_feed",
            preserve_children=True,
        )
    )


@pytest.mark.parametrize(
    "source",
    ["svn", "lee-associates", "franklin-street", "cbre"],
)
def test_strict_nonpreserving_authoritative_feed_rejects_child_preservation(source):
    payload = _strict_freshness_payload(
        source,
        detail_scope="authoritative_inventory_feed",
        preserve_children=True,
    )
    with pytest.raises(ValueError, match="must not preserve child collections"):
        ci.validate_strict_artifact_freshness(payload)


@pytest.mark.parametrize("source", ["srs", "hanley", "kidder-mathews"])
def test_strict_child_preserving_feed_requires_preservation_marker(source):
    payload = _strict_freshness_payload(
        source,
        detail_scope="authoritative_inventory_feed",
    )
    with pytest.raises(ValueError, match="must preserve child collections"):
        ci.validate_strict_artifact_freshness(payload)


def test_strict_detail_source_rejects_child_preservation():
    payload = _strict_freshness_payload("jll", preserve_children=True)
    with pytest.raises(ValueError, match="must not preserve child collections"):
        ci.validate_strict_artifact_freshness(payload)


@pytest.mark.parametrize("freshness", [None, {"requireFreshDetails": False}])
def test_direct_ingest_allows_unmarked_strict_source_without_explicit_flag(freshness):
    payload = {
        "runMeta": {"freshness": freshness} if freshness is not None else {},
        "sources": [{"sourceKey": "jll"}],
        "listings": [],
    }
    ci.validate_strict_artifact_freshness(payload)


@pytest.mark.parametrize(
    "source",
    ["cbre", "srs", "hanley", "kidder-mathews"],
)
def test_checkpoint_strict_sources_are_allowed_without_explicit_cli_contract(source):
    payload = {
        "runMeta": {},
        "sources": [{"sourceKey": source}],
        "listings": [],
    }
    ci.validate_strict_artifact_freshness(payload)


def test_avison_is_not_a_strict_contact_freshness_source():
    assert "avison-young" not in ci.STRICT_FRESHNESS_SOURCE_KEYS


def test_preservation_wrapper_retains_source_key_from_merged_payload():
    sql = ci.build_sql([], [], _SCRAPED_AT, set())
    assert "raw_data#>'{primary,sourceKey}'" in sql
    assert "raw_data#>'{secondary_pass,sourceKey}'" in sql
    assert "EXCLUDED.raw_data#>'{primary,sourceKey}'" in sql
    assert "EXCLUDED.raw_data#>'{secondary_pass,sourceKey}'" in sql


def test_fresh_detail_with_child_preservation_updates_listing_without_child_deletion():
    sql = ci.build_sql([], [], _SCRAPED_AT, set())
    assert "$.**.detailObservedWithChildPreservation" in sql
    assert "NOT jsonb_path_exists" in sql
    assert "_child_additive" in sql
    assert (
        "$.**.preserveChildCollections ? (@ == true || @ == \"true\")"
        in sql
    )


def test_explicit_strict_ingest_flag_rejects_unmarked_nonstrict_artifact():
    payload = {
        "runMeta": {},
        "sources": [{"sourceKey": "cbre"}],
        "listings": [],
    }
    with pytest.raises(ValueError, match="explicitly required"):
        ci.validate_strict_artifact_freshness(
            payload,
            require_strict_freshness=True,
        )


def test_cli_explicit_strict_flag_rejects_unmarked_input_before_database_access(
    tmp_path,
    monkeypatch,
):
    artifact = tmp_path / "jll-without-freshness.json"
    artifact.write_text(
        json.dumps(
            {
                "runMeta": {"mode": "full"},
                "sources": [{"sourceKey": "jll"}],
                "listings": [],
                "brokers": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cre_ingest.py",
            "--in",
            str(artifact),
            "--dry-run",
            "--require-strict-freshness",
        ],
    )

    with pytest.raises(SystemExit, match="explicitly required"):
        ci.main()


@pytest.mark.parametrize("mode", ["full", "enrich"])
def test_cli_live_direct_ingest_supports_unmarked_strict_source_modes(
    tmp_path,
    monkeypatch,
    mode,
):
    artifact = tmp_path / f"jll-{mode}.json"
    artifact.write_text(
        json.dumps(
            {
                "runMeta": {
                    "mode": mode,
                    "startedAt": _SCRAPED_AT,
                    "finishedAt": _SCRAPED_AT,
                },
                "sources": [
                    {
                        "sourceKey": "jll",
                        "listingsCollected": 1,
                    }
                ],
                "listings": [
                    {
                        "sourceKey": "jll",
                        "id": f"{mode}-1",
                        "url": f"https://example.com/jll/{mode}-1",
                        "name": f"JLL {mode} listing",
                    }
                ],
                "brokers": [],
            }
        ),
        encoding="utf-8",
    )
    calls = []

    class Proc:
        returncode = 0

    monkeypatch.setattr(
        ci,
        "load_db_url",
        lambda _env_file: ("postgres://user:SENTINEL@db.test/cre", "/fake/.env"),
    )
    monkeypatch.setattr(ci, "find_psql", lambda: "psql")

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return Proc()

    monkeypatch.setattr(ci.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["cre_ingest.py", "--in", str(artifact)])

    ci.main()

    assert len(calls) == 1
    assert calls[0][0][0] == "psql"
    assert all("SENTINEL" not in arg for arg in calls[0][0])
    assert calls[0][1]["env"]["PGHOST"] == "db.test"
    assert calls[0][1]["env"]["PGDATABASE"] == "cre"
    assert calls[0][1]["env"]["PGPASSWORD"] == "SENTINEL"


def test_psql_connection_env_clears_inherited_target_overrides(monkeypatch):
    for key in ci.PSQL_TARGET_ENV_KEYS:
        monkeypatch.setenv(key, "inherited-override")
    monkeypatch.setenv("CRE_UNRELATED", "preserved")
    url = (
        "postgresql://user:secret@db.example.test:5432/cre"
        "?sslmode=require"
        "&application_name=a+b"
        "&channel_binding=require"
        "&requiressl=1"
        "&target_session_attrs=read-write"
    )
    env = ci.psql_connection_env(url)
    assert env["PGHOST"] == "db.example.test"
    assert env["PGPORT"] == "5432"
    assert env["PGDATABASE"] == "cre"
    assert env["PGUSER"] == "user"
    assert env["PGPASSWORD"] == "secret"
    assert env["PGSSLMODE"] == "require"
    assert env["PGAPPNAME"] == "a+b"
    assert env["PGCHANNELBINDING"] == "require"
    assert env["PGREQUIRESSL"] == "1"
    assert env["PGTARGETSESSIONATTRS"] == "read-write"
    assert env["CRE_UNRELATED"] == "preserved"
    for key in ci.PSQL_TARGET_ENV_KEYS - {
        "PGDATABASE",
        "PGHOST",
        "PGPORT",
        "PGUSER",
        "PGPASSWORD",
        "PGSSLMODE",
        "PGAPPNAME",
        "PGCHANNELBINDING",
        "PGREQUIRESSL",
        "PGTARGETSESSIONATTRS",
    }:
        assert key not in env


def test_database_target_fingerprint_normalizes_dns_but_not_special_hosts():
    assert ci.database_target_fingerprint_from_url(
        "postgresql://user:one@DB.EXAMPLE.TEST./cre"
    ) == ci.database_target_fingerprint_from_url(
        "postgresql://user:two@db.example.test/cre"
    )
    assert ci.database_target_fingerprint_from_url(
        "postgresql://user:one@%2FUsers%2FCayman%2FPG/cre"
    ) != ci.database_target_fingerprint_from_url(
        "postgresql://user:two@%2Fusers%2Fcayman%2Fpg/cre"
    )
    assert ci.database_target_fingerprint_from_url(
        "postgresql://user:one@%2Ftmp%2Fpg./cre"
    ) != ci.database_target_fingerprint_from_url(
        "postgresql://user:two@%2Ftmp%2Fpg/cre"
    )
    assert ci.database_target_fingerprint_from_url(
        "postgresql://user:one@[fe80::1%25En0]/cre"
    ) != ci.database_target_fingerprint_from_url(
        "postgresql://user:two@[fe80::1%25en0]/cre"
    )


def test_psql_connection_args_preserve_uri_only_options_without_credentials():
    url = (
        "postgresql://user:secret@db.example.test:6543/cre"
        "?sslmode=require"
        "&keepalives=1"
        "&fallback_application_name=a+b"
    )
    args = ci.psql_connection_args(url)
    assert args[0] == "--dbname"
    assert args[1].startswith("postgresql://db.example.test:6543/cre?")
    assert "user" not in args[1]
    assert "secret" not in args[1]
    assert "sslmode" not in args[1]
    assert "keepalives=1" in args[1]
    assert "fallback_application_name=a%2Bb" in args[1]

    env = ci.psql_connection_env(url)
    assert env["PGUSER"] == "user"
    assert env["PGPASSWORD"] == "secret"
    assert env["PGSSLMODE"] == "require"


@pytest.mark.parametrize(
    ("url", "expected_host", "expected_uri_host"),
    [
        (
            (
                "postgresql://user:secret@%2FUsers%2FCayman%2FPG/cre"
                "?keepalives=0"
            ),
            "/Users/Cayman/PG",
            "postgresql://%2FUsers%2FCayman%2FPG/cre?",
        ),
        (
            (
                "postgresql://user:secret@[fe80::1%25en0]:5432/cre"
                "?keepalives=1"
            ),
            "fe80::1%en0",
            "postgresql://[fe80::1%25en0]:5432/cre?",
        ),
    ],
)
def test_psql_connection_args_reencode_special_hosts(
    url, expected_host, expected_uri_host
):
    env = ci.psql_connection_env(url)
    args = ci.psql_connection_args(url)
    assert env["PGHOST"] == expected_host
    assert args[1].startswith(expected_uri_host)
    assert "user" not in args[1]
    assert "secret" not in args[1]


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:secret@db.example.test/cre?application_name=%ZZ",
        "postgresql://user:%FF@db.example.test/cre",
    ],
)
def test_psql_connection_rejects_invalid_or_non_utf8_percent_encoding(url):
    with pytest.raises(ValueError, match="percent escape|valid UTF-8"):
        ci.psql_connection_env(url)


def test_psql_connection_rejects_secret_query_options():
    url = (
        "postgresql://user:secret@db.example.test/cre"
        "?sslmode=require&sslpassword=query-secret"
    )
    with pytest.raises(ValueError, match="cannot be moved out of process argv safely"):
        ci.psql_connection_env(url)


def test_cli_rejects_target_drift_before_psql_discovery(tmp_path, monkeypatch):
    artifact = tmp_path / "jll-full.json"
    artifact.write_text(
        json.dumps(
            {
                "runMeta": {
                    "mode": "full",
                    "startedAt": _SCRAPED_AT,
                    "finishedAt": _SCRAPED_AT,
                },
                "sources": [
                    {
                        "sourceKey": "jll",
                        "listingsCollected": 1,
                    }
                ],
                "listings": [
                    {
                        "sourceKey": "jll",
                        "id": "full-1",
                        "url": "https://example.com/jll/full-1",
                        "name": "JLL full listing",
                    }
                ],
                "brokers": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ci,
        "load_db_url",
        lambda _env_file: (
            "postgresql://user:secret@db.example.test/cre",
            "/fake/.env",
        ),
    )
    monkeypatch.setattr(
        ci,
        "find_psql",
        lambda: pytest.fail("target drift must fail before psql discovery"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cre_ingest.py",
            "--in",
            str(artifact),
            "--expected-db-target-sha256",
            "0" * 64,
        ],
    )

    with pytest.raises(SystemExit, match="does not match"):
        ci.main()


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


def test_normal_brokerage_scalars_are_not_rejected_with_retired_artifacts():
    row = _row({
        "sourceKey": "cbre",
        "url": "https://cbre.com/normal-listing",
        "id": "normal-1",
        "noi": 975000,
        "capRatePct": 6.25,
        "units": 42,
        "yearBuilt": 2004,
    })

    assert row["external_id"] == "normal-1"
    assert row["noi"] == 975000.0
    assert row["cap_rate"] == 0.0625
    assert row["units"] == 42.0
    assert row["year_built"] == 2004


def test_inventory_only_card_never_becomes_canonical_listing():
    listing = {
        "sourceKey": "cbre-dealflow",
        "id": "card:abc123",
        # Even if a future adapter supplies the shared index URL, the explicit
        # marker must win and keep the card out of cre_listings.
        "url": "https://www.cbredealflow.com/",
        "name": "Unlinked Deal",
        "city": "Tulsa",
        "state": "OK",
        "assetType": "Industrial",
        "provisionalIdentity": {"historyContinuity": "not_guaranteed"},
        "inventoryOnly": {
            "reason": "no_provider_id_or_listing_url",
            "indexUrl": "https://www.cbredealflow.com/",
        },
    }
    assert _row(listing) is None
    inventory = ci.to_inventory_only_row(listing, _SCRAPED_AT)
    assert inventory is not None
    assert inventory["slug"] == "cbre"
    assert inventory["external_id"] == "dealflow:card:abc123"
    assert inventory["source_key"] == "cbre-dealflow"


def test_inventory_only_reconciliation_requires_strict_full_enumeration():
    payload = {
        "runMeta": {
            "mode": "full",
            "transactions": ["sale", "lease"],
            "maxItemsPerSource": None,
            "startedAt": _SCRAPED_AT,
            "finishedAt": _SCRAPED_AT,
        },
        "sources": [
            {
                "sourceKey": "cbre-dealflow",
                "transaction": "sale",
                "supported": True,
                "listingsCollected": 10,
                "truncated": False,
            },
            {
                "sourceKey": "cbre-dealflow",
                "transaction": "lease",
                "supported": True,
                "listingsCollected": 2,
                "truncated": False,
            },
        ],
        "listings": [
            {
                "sourceKey": "cbre-dealflow",
                "transactionMode": "sale",
                "id": f"sale-{index}",
            }
            for index in range(10)
        ]
        + [
            {
                "sourceKey": "cbre-dealflow",
                "transactionMode": "lease",
                "id": f"lease-{index}",
            }
            for index in range(2)
        ],
        "totalListings": 12,
    }
    assert ci.inventory_only_full_scopes(payload) == [
        {
            "slug": "cbre",
            "source_key": "cbre-dealflow",
            "external_id_like": "dealflow:card:%",
            "watermark_external_id": (
                "dealflow:scope:inventory-only-watermark"
            ),
            "watermark_url": "https://www.cbredealflow.com/",
            "watermark_fingerprint": "inventory-only-scope-watermark-v1",
            "observed_at": _SCRAPED_AT,
        }
    ]

    # Existing all-source full artifacts remain compatible: only the Deal Flow
    # subset authorizes this namespace.
    payload["sources"].append(
        {
            "sourceKey": "svn",
            "transaction": "sale",
            "supported": True,
            "listingsCollected": 1,
            "truncated": False,
        }
    )
    payload["listings"].append(
        {"sourceKey": "svn", "transactionMode": "sale", "id": "svn-1"}
    )
    payload["totalListings"] = 13
    assert ci.inventory_only_full_scopes(payload)[0]["source_key"] == "cbre-dealflow"

    payload["sources"][0]["truncated"] = True
    assert ci.inventory_only_full_scopes(payload) == []

    payload["sources"][0]["truncated"] = False
    payload["sources"][0]["listingsCollected"] = 9
    assert ci.inventory_only_full_scopes(payload) == []

    payload["sources"][0]["listingsCollected"] = 10
    payload["runMeta"]["startedAt"] = "2026-06-16T00:00:00+00:00"
    assert ci.inventory_only_full_scopes(payload) == []

    payload["runMeta"]["startedAt"] = _SCRAPED_AT
    payload["listings"][0]["inventoryOnly"] = {"reason": "malformed"}
    assert ci.inventory_only_full_scopes(payload) == []


def test_empty_inventory_only_scope_is_still_reconcilable():
    payload = {
        "runMeta": {
            "mode": "full",
            "transactions": ["sale", "lease"],
            "maxItemsPerSource": None,
            "startedAt": _SCRAPED_AT,
            "finishedAt": _SCRAPED_AT,
        },
        "sources": [
            {
                "sourceKey": "cbre-dealflow",
                "transaction": tx,
                "supported": True,
                "listingsCollected": 0,
                "truncated": False,
            }
            for tx in ("sale", "lease")
        ],
        "listings": [],
        "totalListings": 0,
    }
    scopes = ci.inventory_only_full_scopes(payload)
    assert len(scopes) == 1
    assert scopes[0]["external_id_like"] == "dealflow:card:%"


def test_colliers_inventory_only_card_uses_salestracker_namespace():
    listing = {
        "sourceKey": "colliers",
        "transactionMode": "sale",
        "id": "salestracker:card:abc123",
        "name": "Unlinked Colliers Sale",
        "city": "Tulsa",
        "state": "OK",
        "provisionalIdentity": {"historyContinuity": "not_guaranteed"},
        "inventoryOnly": {
            "reason": "no_public_slp_detail_link",
            "indexUrl": "https://sales.colliers.com/",
        },
    }

    assert _row(listing) is None
    inventory = ci.to_inventory_only_row(listing, _SCRAPED_AT)
    assert inventory is not None
    assert inventory["slug"] == "colliers"
    assert inventory["external_id"] == "salestracker:card:abc123"
    assert inventory["source_key"] == "colliers"
    assert inventory["url"] == "https://sales.colliers.com/"


def test_colliers_full_snapshot_authorizes_its_own_inventory_scope():
    payload = {
        "runMeta": {
            "mode": "full",
            "transactions": ["sale", "lease"],
            "maxItemsPerSource": None,
            "startedAt": _SCRAPED_AT,
            "finishedAt": _SCRAPED_AT,
        },
        "sources": [
            {
                "sourceKey": "colliers",
                "transaction": "sale",
                "supported": True,
                "listingsCollected": 1,
                "truncated": False,
            },
            {
                "sourceKey": "colliers",
                "transaction": "lease",
                "supported": True,
                "listingsCollected": 0,
                "truncated": False,
            },
        ],
        "listings": [
            {
                "sourceKey": "colliers",
                "transactionMode": "sale",
                "id": "salestracker:card:abc123",
                "inventoryOnly": {
                    "reason": "no_public_slp_detail_link",
                    "indexUrl": "https://sales.colliers.com/",
                },
            }
        ],
        "totalListings": 1,
    }

    assert ci.inventory_only_full_scopes(payload) == [
        {
            "slug": "colliers",
            "source_key": "colliers",
            "external_id_like": "salestracker:card:%",
            "watermark_external_id": (
                "salestracker:scope:inventory-only-watermark"
            ),
            "watermark_url": "https://sales.colliers.com/",
            "watermark_fingerprint": (
                "inventory-only-scope-watermark-v1:colliers-salestracker"
            ),
            "observed_at": _SCRAPED_AT,
        }
    ]


def test_cli_rejects_duplicate_complete_inventory_scopes(
    tmp_path, monkeypatch
):
    payload = {
        "runMeta": {
            "mode": "full",
            "transactions": ["sale", "lease"],
            "maxItemsPerSource": None,
            "startedAt": _SCRAPED_AT,
            "finishedAt": _SCRAPED_AT,
        },
        "sources": [
            {
                "sourceKey": "cbre-dealflow",
                "transaction": tx,
                "supported": True,
                "listingsCollected": 0,
                "truncated": False,
            }
            for tx in ("sale", "lease")
        ],
        "listings": [],
        "brokers": [],
        "totalListings": 0,
    }
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps(payload), encoding="utf-8")
    second.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cre_ingest.py",
            "--in",
            str(first),
            "--in",
            str(second),
            "--dry-run",
        ],
    )
    with pytest.raises(SystemExit, match="duplicate complete inventory-only scopes"):
        ci.main()


def test_cli_does_not_let_complete_scope_authorize_partial_input(
    tmp_path, monkeypatch
):
    complete = {
        "runMeta": {
            "mode": "full",
            "transactions": ["sale", "lease"],
            "maxItemsPerSource": None,
            "startedAt": _SCRAPED_AT,
            "finishedAt": _SCRAPED_AT,
        },
        "sources": [
            {
                "sourceKey": "cbre-dealflow",
                "transaction": tx,
                "supported": True,
                "listingsCollected": 0,
                "truncated": False,
            }
            for tx in ("sale", "lease")
        ],
        "listings": [],
        "brokers": [],
        "totalListings": 0,
    }
    partial = {
        "runMeta": {
            "mode": "enrich",
            "startedAt": _SCRAPED_AT,
            "finishedAt": _SCRAPED_AT,
        },
        "sources": [],
        "listings": [
            {
                "sourceKey": "cbre-dealflow",
                "transactionMode": "sale",
                "id": "card:partial",
                "inventoryOnly": {
                    "reason": "no_provider_id_or_listing_url",
                    "indexUrl": "https://www.cbredealflow.com/",
                },
            }
        ],
        "brokers": [],
        "totalListings": 1,
    }
    complete_path = tmp_path / "complete.json"
    partial_path = tmp_path / "partial.json"
    complete_path.write_text(json.dumps(complete), encoding="utf-8")
    partial_path.write_text(json.dumps(partial), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cre_ingest.py",
            "--in",
            str(complete_path),
            "--in",
            str(partial_path),
            "--dry-run",
        ],
    )
    with pytest.raises(
        SystemExit,
        match="strict complete full scope in the same artifact",
    ):
        ci.main()


def test_inventory_only_sql_covers_appearance_disappearance_and_reappearance():
    row = {
        "slug": "cbre",
        "external_id": "dealflow:card:abc123",
        "source_key": "cbre-dealflow",
        "url": "https://www.cbredealflow.com/",
        "fingerprint": "abc",
        "observed_status": "Available",
        "observed_at": _SCRAPED_AT,
    }
    scope = {
        "slug": "cbre",
        "source_key": "cbre-dealflow",
        "external_id_like": "dealflow:card:%",
        "watermark_external_id": "dealflow:scope:inventory-only-watermark",
        "watermark_url": "https://www.cbredealflow.com/",
        "watermark_fingerprint": "inventory-only-scope-watermark-v1",
        "observed_at": _SCRAPED_AT,
    }
    sql = ci.build_sql(
        [],
        [],
        _SCRAPED_AT,
        set(),
        inventory_only_rows=[row],
        inventory_only_scopes=[scope],
    )
    assert "INSERT INTO credeals.cre_source_index AS si" in sql
    assert "soft_deleted = false" in sql
    assert "UPDATE credeals.cre_source_index si" in sql
    assert "SET soft_deleted = true" in sql
    assert "FROM _inventory_only_scope scope" in sql
    assert "current.external_id = si.external_id" in sql
    assert "refusing stale inventory-only replay" in sql
    assert "prior.last_enumerated_at > scope.observed_at" in sql
    assert "EXCLUDED.last_enumerated_at >= si.last_enumerated_at" in sql
    assert "si.last_enumerated_at <= scope.observed_at" in sql
    assert "prior.external_id = scope.watermark_external_id" in sql
    assert "scope.watermark_external_id" in sql
    assert "scope.watermark_url" in sql
    assert "scope.watermark_fingerprint" in sql
    assert "dealflow:scope:inventory-only-watermark" in sql
    assert "inventory-only-scope-watermark-v1" in sql
    assert "to_regclass('credeals.cre_source_index')" not in sql


def test_inventory_only_stale_replay_watermarks_are_source_specific():
    scopes = [
        {
            **definition,
            "source_key": source_key,
            "observed_at": _SCRAPED_AT,
        }
        for source_key, definition in ci.INVENTORY_ONLY_SOURCE_DEFINITIONS.items()
    ]
    sql = ci.build_sql(
        [],
        [],
        _SCRAPED_AT,
        set(),
        inventory_only_scopes=scopes,
    )

    assert "dealflow:scope:inventory-only-watermark" in sql
    assert "salestracker:scope:inventory-only-watermark" in sql
    assert "https://www.cbredealflow.com/" in sql
    assert "https://sales.colliers.com/" in sql
    assert (
        "inventory-only-scope-watermark-v1:colliers-salestracker" in sql
    )
    assert "prior.source_key = scope.source_key" in sql
    assert "prior.external_id = scope.watermark_external_id" in sql


def test_cli_refuses_conflicting_colliers_canonical_identity(
    tmp_path, monkeypatch
):
    artifact = tmp_path / "colliers-conflict.json"
    artifact.write_text(
        json.dumps(
            {
                "runMeta": {
                    "mode": "full",
                    "startedAt": _SCRAPED_AT,
                    "finishedAt": _SCRAPED_AT,
                },
                "sources": [],
                "brokers": [],
                "listings": [
                    {
                        "sourceKey": "colliers",
                        "id": "12345",
                        "url": (
                            "https://my.rcm1.com/handler/modern.aspx?pv=linked"
                        ),
                        "canonicalUrl": (
                            "https://my.rcm1.com/handler/modern.aspx?pv=linked"
                        ),
                        "name": "Linked Property",
                        "transactionMode": "sale",
                    },
                    {
                        "sourceKey": "colliers",
                        "id": "12345",
                        "url": "https://sales.colliers.com/#project-12345",
                        "canonicalUrl": (
                            "https://sales.colliers.com/#project-12345"
                        ),
                        "name": "Different Unlinked Property",
                        "transactionMode": "sale",
                    },
                ],
                "totalListings": 2,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["cre_ingest.py", "--in", str(artifact), "--dry-run"],
    )

    with pytest.raises(
        SystemExit,
        match="refusing duplicate canonical identity.*Colliers",
    ):
        ci.main()


def test_cli_refuses_duplicate_colliers_provisional_identity(
    tmp_path, monkeypatch
):
    artifact = tmp_path / "colliers-provisional-duplicate.json"
    provisional = {
        "sourceKey": "colliers",
        "id": "salestracker:card:12345",
        "url": None,
        "transactionMode": "sale",
        "inventoryOnly": {
            "reason": "card_not_linked",
            "indexUrl": "https://sales.colliers.com/",
        },
    }
    artifact.write_text(
        json.dumps(
            {
                "runMeta": {
                    "mode": "full",
                    "transactions": ["sale", "lease"],
                    "maxItemsPerSource": None,
                    "startedAt": _SCRAPED_AT,
                    "finishedAt": _SCRAPED_AT,
                },
                "sources": [
                    {
                        "sourceKey": "colliers",
                        "transaction": "sale",
                        "supported": True,
                        "listingsCollected": 2,
                        "truncated": False,
                    },
                    {
                        "sourceKey": "colliers",
                        "transaction": "lease",
                        "supported": True,
                        "listingsCollected": 0,
                        "truncated": False,
                    },
                ],
                "brokers": [],
                "listings": [provisional, dict(provisional)],
                "totalListings": 2,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["cre_ingest.py", "--in", str(artifact), "--dry-run"],
    )

    with pytest.raises(
        SystemExit,
        match="duplicate Colliers provisional inventory identity",
    ):
        ci.main()


def test_non_colliers_dual_mode_identity_still_merges():
    sale = _row(
        {
            "sourceKey": "cbre",
            "id": "dual-1",
            "url": "https://www.cbre.com/properties/dual-1",
            "transactionMode": "sale",
        }
    )
    lease = _row(
        {
            "sourceKey": "cbre",
            "id": "dual-1",
            "url": "https://www.cbre.com/properties/dual-1?mode=lease",
            "transactionMode": "lease",
        }
    )

    ci.validate_duplicate_identity_before_merge(sale, lease)
    assert ci.merge_rows(sale, lease)["transaction_type"] == "sale_or_lease"


def test_colliers_identity_guard_rejects_even_identical_duplicate_project_id():
    first = _row(
        {
            "sourceKey": "colliers",
            "id": "12345",
            "url": "https://my.rcm1.com/handler/modern.aspx?pv=linked",
            "name": "Same Property",
            "transactionMode": "sale",
        }
    )
    duplicate = _row(
        {
            "sourceKey": "colliers",
            "id": "12345",
            "url": "https://my.rcm1.com/handler/modern.aspx?pv=linked",
            "name": "Same Property",
            "transactionMode": "sale",
        }
    )
    with pytest.raises(ValueError, match="duplicate Colliers canonical ProjectId"):
        ci.validate_duplicate_identity_before_merge(first, duplicate)


def test_cli_refuses_marked_retired_om_parse_artifact(tmp_path, monkeypatch):
    artifact = tmp_path / "retired-om.json"
    artifact.write_text(json.dumps({
        "artifactKind": ci.RETIRED_OM_PARSE_ARTIFACT_KIND,
        "listings": [{
            "sourceKey": "cbre",
            "externalId": "legacy-id",
            "url": "https://cbre.com/legacy",
            "noi": 1000000,
            "omFacts": [],
        }],
    }))
    monkeypatch.setattr(sys, "argv", ["cre_ingest.py", "--in", str(artifact)])

    with pytest.raises(SystemExit, match="sole production OM extraction writer"):
        ci.main()


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
