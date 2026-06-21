"""
test_cre_backfill_raw_data_more.py

Targets missing pure-logic lines in cre_backfill_raw_data.py (current 80%, goal >=90%):

  191    dual_mode_passes: raw is not dict -> return []
  242    _strip_comma_num: empty string after strip -> return None
  270    _add_extra: whitespace-only string value -> return (do not stage)
  471    derive_transwestern: availability row is not dict -> continue (skipped)
  497    derive_transwestern: lease_type_token is truthy -> set lease_rate_type
  606    derive_nai_global: mode=='lease' branch for NAI price
  707-710  _deriver_for: sub-source lookup path when source_key is not in DERIVERS
  731    _year_or_none: no 4-digit year match in string -> return None
  746    _first_subtype: string (not list) -> return stripped string
  756    _transwestern_net_token: no token matches -> return None
  765    _transwestern_net_token: non-string token in raw_arr -> skip
  798    _yesno_bool: bool input -> bool_or_none passthrough
  802    _yesno_bool: True-word in string ('yes'/'y'/'true')
  814    _yesno_bool: non-bool non-string input -> return None
  874-878  _nai_building_class: tag without a BuildingClass[A-D] pattern
  1025-1036  summarize: per-column and extra_keys counting
  1045-1052  print_summary: printing (verifies formatting without asserting stdout)
  stage_rows_for_slug is already covered; leaving main()/psql/_apply_sql I/O boundary.

Pure Python, no DB, no network.
I/O boundary intentionally left: main(), _apply_sql(), fetch_rows(), read_rows_sql(),
  and the __main__ guard (lines 1072-1137, 1141-1158, 1162).
"""


import pytest

import cre_backfill_raw_data as bf


# ---------------------------------------------------------------------------
# dual_mode_passes: non-dict input -> [] (line 191)
# ---------------------------------------------------------------------------


def test_dual_mode_passes_non_dict_returns_empty():
    assert bf.dual_mode_passes(None) == []
    assert bf.dual_mode_passes("string") == []
    assert bf.dual_mode_passes(42) == []
    assert bf.dual_mode_passes([]) == []


def test_dual_mode_passes_flat_dict():
    """A flat dict has no primary/secondary_pass -> just the flat blob."""
    raw = {"url": "https://x.example/p1"}
    passes = bf.dual_mode_passes(raw)
    assert len(passes) == 1
    assert passes[0] is raw


def test_dual_mode_passes_dual_payload():
    """primary + secondary_pass -> three entries (primary, secondary_pass, flat)."""
    p1 = {"url": "https://x.example/primary"}
    s1 = {"url": "https://x.example/secondary"}
    raw = {"primary": p1, "secondary_pass": s1, "url": "https://x.example/flat"}
    passes = bf.dual_mode_passes(raw)
    assert passes[0] is p1
    assert passes[1] is s1
    assert passes[2] is raw


# ---------------------------------------------------------------------------
# _strip_comma_num: empty string after strip -> None (line 242)
# ---------------------------------------------------------------------------


def test_strip_comma_num_empty_string_after_strip():
    """A string that is all commas or whitespace -> '' after strip -> None."""
    assert bf._strip_comma_num(",,,") is None
    assert bf._strip_comma_num("  ") is None


def test_strip_comma_num_non_numeric_string():
    """A non-numeric string raises ValueError in float() -> None."""
    assert bf._strip_comma_num("abc") is None
    assert bf._strip_comma_num("N/A") is None


def test_strip_comma_num_valid_comma_number():
    """A comma-formatted number is parsed correctly."""
    result = bf._strip_comma_num("25,000")
    assert result == pytest.approx(25000.0)


def test_strip_comma_num_range_clamp_lo():
    """A value below lo is rejected by num_or_none."""
    assert bf._strip_comma_num("0", lo=1) is None


def test_strip_comma_num_numeric_input():
    """A bare float or int passes through num_or_none."""
    assert bf._strip_comma_num(1500.0) == pytest.approx(1500.0)
    assert bf._strip_comma_num(0, lo=1) is None


# ---------------------------------------------------------------------------
# _add_extra: whitespace-only string value -> no-op (line 270)
# ---------------------------------------------------------------------------


def test_add_extra_whitespace_string_not_staged():
    """A string that is whitespace-only after strip must not be staged (line 270)."""
    extra = {}
    bf._add_extra(extra, "key", "   ")
    assert "key" not in extra


def test_add_extra_empty_string_not_staged():
    extra = {}
    bf._add_extra(extra, "key", "")
    assert "key" not in extra


def test_add_extra_none_not_staged():
    extra = {}
    bf._add_extra(extra, "key", None)
    assert "key" not in extra


def test_add_extra_valid_string_staged():
    extra = {}
    bf._add_extra(extra, "market", "Dallas")
    assert extra["market"] == "Dallas"


def test_add_extra_non_string_value_staged():
    """Non-string non-None values (e.g. bool True) are staged directly."""
    extra = {}
    bf._add_extra(extra, "is_investment", True)
    assert extra["is_investment"] is True


# ---------------------------------------------------------------------------
# derive_transwestern: non-dict availability row skipped (line 471)
# and lease_type_token branch (line 497)
# ---------------------------------------------------------------------------


def test_derive_transwestern_non_dict_avail_row_skipped():
    """A non-dict row in availability[] triggers the 'continue' branch (line 471)."""
    raw = {
        "transwesternFacts": {"Stories": "2", "Class": "A"},
        "availability": ["not-a-dict", None, 42],
    }
    cols, extra = bf.derive_columns("transwestern", raw)
    # Floors and building_class still derived from facts; no crash
    assert cols.get("floors") == 2
    assert cols.get("building_class") == "A"
    # No sizes -> no min/max divisible
    assert "min_divisible_sf" not in cols


def test_derive_transwestern_lease_type_token_set(monkeypatch):
    """When _transwestern_net_token returns a token, lease_rate_type is staged (line 497)."""
    raw = {
        "transwesternFacts": {},
        "availability": [
            {
                "type": "Lease",
                "size": "5000",
                "rate": "$12.00 /sf/yr (NNN)",
                "raw": ["NNN", "Triple Net Lease"],
            }
        ],
    }
    cols, _ = bf.derive_columns("transwestern", raw)
    assert cols.get("lease_rate_type") == "nnn"


def test_derive_transwestern_no_lease_type_token_not_staged():
    """When no NNN/gross token found, lease_rate_type is NOT staged."""
    raw = {
        "transwesternFacts": {},
        "availability": [
            {
                "type": "Lease",
                "size": "5000",
                "rate": "$15.00 /sf/yr",
                "raw": ["some unknown token"],
            }
        ],
    }
    cols, _ = bf.derive_columns("transwestern", raw)
    assert "lease_rate_type" not in cols


# ---------------------------------------------------------------------------
# derive_nai_global: mode == 'lease' branch (line 606)
# ---------------------------------------------------------------------------


def test_derive_nai_global_lease_mode_price_staged():
    """When transactionMode == 'lease', price -> lease_rate_min/max (line 609-613)."""
    raw = {
        "sourceKey": "nai-global",
        "sourceWebsiteUrl": "https://www.naicommercial.com/listing/1",
        "transactionMode": "lease",
        "publicPost": {
            "id": 9001,
            "price": 18.0,
            "sizeRangeL": 1000,
            "sizeRangeH": 5000,
        },
    }
    cols, _ = bf.derive_columns("nai-global", raw)
    assert cols.get("lease_rate_min") == pytest.approx(18.0)
    assert cols.get("lease_rate_max") == pytest.approx(18.0)
    # sale price must not be staged for a lease mode row
    assert "sale_price_usd" not in cols


def test_derive_nai_global_sale_mode_price_staged():
    """When transactionMode == 'sale', price -> sale_price_usd."""
    raw = {
        "sourceKey": "nai-global",
        "sourceWebsiteUrl": "https://www.naicommercial.com/listing/2",
        "transactionMode": "sale",
        "publicPost": {
            "id": 9002,
            "price": 2500000.0,
        },
    }
    cols, _ = bf.derive_columns("nai-global", raw)
    assert cols.get("sale_price_usd") == pytest.approx(2500000.0)
    assert "lease_rate_min" not in cols


def test_derive_nai_global_no_mode_no_price_staged():
    """transactionMode absent -> neither sale nor lease price is staged."""
    raw = {
        "sourceKey": "nai-global",
        "sourceWebsiteUrl": "https://www.naicommercial.com/listing/3",
        "publicPost": {"id": 9003, "price": 500000.0},
    }
    cols, _ = bf.derive_columns("nai-global", raw)
    assert "sale_price_usd" not in cols
    assert "lease_rate_min" not in cols


def test_derive_nai_global_string_price_with_pound_prefix():
    """A POUND-prefixed string price routes through parse_amount_ignoring_currency_label."""
    raw = {
        "sourceKey": "nai-global",
        "sourceWebsiteUrl": "https://www.naicommercial.com/listing/4",
        "transactionMode": "sale",
        "publicPost": {
            "id": 9004,
            "price": "POUND 1200000",
        },
    }
    cols, _ = bf.derive_columns("nai-global", raw)
    assert cols.get("sale_price_usd") == pytest.approx(1200000.0)


# ---------------------------------------------------------------------------
# _deriver_for: sub-source -> parent brokerage fallback (lines 707-710)
# ---------------------------------------------------------------------------


def test_deriver_for_sub_source_jll_investor():
    """jll-investor is a sub-source of jll; its deriver resolves to derive_jll."""
    fn = bf._deriver_for("jll-investor")
    assert fn is bf.derive_jll


def test_deriver_for_colliers_main():
    """colliers-main is a sub-source of colliers; resolves to derive_colliers."""
    fn = bf._deriver_for("colliers-main")
    assert fn is bf.derive_colliers


def test_deriver_for_cbre_dealflow():
    """cbre-dealflow is a sub-source of cbre; resolves to derive_cbre."""
    fn = bf._deriver_for("cbre-dealflow")
    assert fn is bf.derive_cbre


def test_deriver_for_unknown_source_key_returns_none():
    """A source_key not in SOURCE_TO_BROKERAGE returns None (line 702-703)."""
    fn = bf._deriver_for("totally-unknown-broker-xyz")
    assert fn is None


# ---------------------------------------------------------------------------
# _year_or_none: no 4-digit year match -> None (line 731)
# ---------------------------------------------------------------------------


def test_year_or_none_string_no_year():
    """A string with no 4-digit year in range 1700-2099 -> None (line 730-731)."""
    assert bf._year_or_none("built recently") is None
    assert bf._year_or_none("N/A") is None
    assert bf._year_or_none("0000") is None
    assert bf._year_or_none("9999") is None


def test_year_or_none_valid_string():
    """A 4-digit year string in range 1700-2099 is extracted."""
    assert bf._year_or_none("1985") == 1985
    assert bf._year_or_none("Year Built: 2003") == 2003


def test_year_or_none_numeric_in_range():
    """An int/float in range (1700, 2100) is returned as int."""
    assert bf._year_or_none(1975) == 1975
    assert bf._year_or_none(2024.0) == 2024


def test_year_or_none_numeric_out_of_range():
    """An int out of range -> None (the float/int branch returns None)."""
    assert bf._year_or_none(1699) is None
    assert bf._year_or_none(2100) is None


# ---------------------------------------------------------------------------
# _first_subtype: string (not list) -> return stripped string (line 746)
# ---------------------------------------------------------------------------


def test_first_subtype_string_input():
    """A bare string (not a list) returns itself stripped (line 745-746)."""
    assert bf._first_subtype("office.medical") == "office.medical"
    assert bf._first_subtype("  warehouse  ") == "warehouse"


def test_first_subtype_empty_string_returns_none():
    """An empty/whitespace string -> None."""
    assert bf._first_subtype("") is None
    assert bf._first_subtype("   ") is None


def test_first_subtype_list_first_nonempty():
    """A list returns the first non-empty string."""
    assert bf._first_subtype(["", "office.medical", "retail"]) == "office.medical"


def test_first_subtype_non_string_non_list_returns_none():
    """None / number / dict -> None."""
    assert bf._first_subtype(None) is None
    assert bf._first_subtype(42) is None


# ---------------------------------------------------------------------------
# _transwestern_net_token: no match -> None (line 756), and non-string skipped (line 765)
# ---------------------------------------------------------------------------


def test_transwestern_net_token_no_matching_token():
    """An array with no NNN/gross/etc token -> None (line 756)."""
    assert bf._transwestern_net_token(["monthly", "annual", "per unit"]) is None
    assert bf._transwestern_net_token([]) is None


def test_transwestern_net_token_non_list_input():
    """Non-list input -> None (line 754)."""
    assert bf._transwestern_net_token(None) is None
    assert bf._transwestern_net_token("NNN") is None


def test_transwestern_net_token_non_string_in_list_skipped():
    """Non-string elements in the list do not raise and are skipped (line 765)."""
    result = bf._transwestern_net_token([None, 42, True, "NNN"])
    assert result == "NNN"


def test_transwestern_net_token_all_non_string():
    """Array of non-strings -> None (all skipped by isinstance check)."""
    assert bf._transwestern_net_token([None, 42, {}, []]) is None


def test_transwestern_net_token_finds_nnn():
    assert bf._transwestern_net_token(["Lease Rate", "NNN"]) == "NNN"


def test_transwestern_net_token_finds_gross():
    assert bf._transwestern_net_token(["Gross lease"]) == "Gross lease"


# ---------------------------------------------------------------------------
# _yesno_bool: bool passthrough (line 798), True-word (line 802), non-bool non-str (line 814)
# ---------------------------------------------------------------------------


def test_yesno_bool_true_bool():
    """bool True -> bool_or_none(True) -> True (line 798)."""
    assert bf._yesno_bool(True) is True


def test_yesno_bool_false_bool():
    """bool False -> bool_or_none(False) -> False (line 798)."""
    assert bf._yesno_bool(False) is False


def test_yesno_bool_yes_string():
    """'Yes' -> True (line 802)."""
    assert bf._yesno_bool("Yes") is True
    assert bf._yesno_bool("yes") is True
    assert bf._yesno_bool("Y") is True
    assert bf._yesno_bool("true") is True


def test_yesno_bool_no_string():
    """'No' -> False (line 804-805)."""
    assert bf._yesno_bool("No") is False
    assert bf._yesno_bool("no") is False
    assert bf._yesno_bool("n") is False
    assert bf._yesno_bool("false") is False


def test_yesno_bool_unknown_string_returns_none():
    """A string that is neither yes nor no -> None (line 806 falls through)."""
    assert bf._yesno_bool("maybe") is None
    assert bf._yesno_bool("") is None


def test_yesno_bool_non_bool_non_string_returns_none():
    """Non-bool, non-string -> None (line 814 falls through after both if blocks)."""
    assert bf._yesno_bool(None) is None
    assert bf._yesno_bool(1) is None
    assert bf._yesno_bool([]) is None


# ---------------------------------------------------------------------------
# _nai_building_class: tag without BuildingClass[A-D] pattern (lines 874-878)
# ---------------------------------------------------------------------------


def test_nai_building_class_no_matching_tag():
    """Tags present but no BuildingClass[A-D] -> None (line 874-878 all miss)."""
    assert bf._nai_building_class(["RetailAsset", "Industrial", "SomethingElse"]) is None


def test_nai_building_class_non_string_tag_skipped():
    """Non-string tags are skipped by the isinstance(t, str) guard."""
    result = bf._nai_building_class([None, 42, "BuildingClassA"])
    assert result == "A"


def test_nai_building_class_non_list_input():
    """Non-list input -> None (line 871)."""
    assert bf._nai_building_class(None) is None
    assert bf._nai_building_class("BuildingClassA") is None


def test_nai_building_class_builds_correctly():
    """All four classes are parsed correctly."""
    for cls in "ABCD":
        assert bf._nai_building_class([f"BuildingClass{cls}"]) == cls


def test_nai_building_class_mixed_list_first_wins():
    """First matching tag in the list wins."""
    result = bf._nai_building_class(["RetailAsset", "BuildingClassB", "BuildingClassA"])
    assert result == "B"


# ---------------------------------------------------------------------------
# summarize: per-column and extra_keys counting (lines 1025-1036)
# ---------------------------------------------------------------------------


def test_summarize_counts_per_column_and_extra_keys():
    """summarize() returns correct per_col counts and extra_keys counts."""
    staged = [
        {
            "listing_id": "id-1",
            "cols": {"cap_rate": 0.07, "canonical_url": "https://x.com/1"},
            "extra": {"market": "Dallas", "on_market_at": "2024-01-01"},
        },
        {
            "listing_id": "id-2",
            "cols": {"cap_rate": 0.08, "size_sf": 10000.0},
            "extra": {"market": "Chicago"},
        },
        {
            "listing_id": "id-3",
            "cols": {},
            "extra": {},
        },
    ]
    result = bf.summarize("test-slug", staged)
    assert result["slug"] == "test-slug"
    # id-1 and id-2 both have non-empty cols/extra; id-3 does not
    assert result["listings_touched"] == 2
    assert result["per_col"]["cap_rate"] == 2
    assert result["per_col"]["canonical_url"] == 1
    assert result["per_col"]["size_sf"] == 1
    assert result["extra_keys"]["market"] == 2
    assert result["extra_keys"]["on_market_at"] == 1


def test_summarize_empty_staged_rows():
    """An empty staged list returns zeros."""
    result = bf.summarize("slugx", [])
    assert result["listings_touched"] == 0
    assert result["per_col"] == {}
    assert result["extra_keys"] == {}


def test_summarize_row_with_none_col_value_not_counted():
    """A col value of None is not counted in per_col (the 'if v is not None' guard)."""
    staged = [
        {"listing_id": "id-1", "cols": {"cap_rate": None, "size_sf": 5000.0}, "extra": {}}
    ]
    # id-1 has size_sf non-None so it is counted in listings_touched
    result = bf.summarize("s", staged)
    assert "cap_rate" not in result["per_col"]
    assert result["per_col"]["size_sf"] == 1


# ---------------------------------------------------------------------------
# print_summary: output formatting (lines 1045-1052)
# ---------------------------------------------------------------------------


def test_print_summary_produces_output(capsys):
    """print_summary writes lines to stdout; basic smoke that it doesn't crash."""
    stats = {
        "slug": "newmark",
        "listings_touched": 5,
        "per_col": {"canonical_url": 5, "county": 4},
        "extra_keys": {"on_market_at": 2},
    }
    bf.print_summary(stats, scanned=10)
    captured = capsys.readouterr()
    assert "newmark" in captured.out
    assert "scanned 10" in captured.out
    assert "canonical_url" in captured.out
    assert "on_market_at" in captured.out


def test_print_summary_no_cols_no_extra_no_crash(capsys):
    """Empty per_col and extra_keys -> no column/key lines, but header still prints."""
    stats = {
        "slug": "savills",
        "listings_touched": 0,
        "per_col": {},
        "extra_keys": {},
    }
    bf.print_summary(stats, scanned=100)
    captured = capsys.readouterr()
    assert "savills" in captured.out


# ---------------------------------------------------------------------------
# copy_field: COPY encoding edge cases (pure helper used by build_sql)
# ---------------------------------------------------------------------------


def test_copy_field_none_is_null():
    assert bf.copy_field(None) == "\\N"


def test_copy_field_bool_true():
    assert bf.copy_field(True) == "t"


def test_copy_field_bool_false():
    assert bf.copy_field(False) == "f"


def test_copy_field_string_with_backslash():
    """Backslashes in staged text are doubled for COPY format."""
    result = bf.copy_field("C:\\path\\to\\file")
    assert result == "C:\\\\path\\\\to\\\\file"


def test_copy_field_string_with_tab():
    result = bf.copy_field("a\tb")
    assert result == "a\\tb"


def test_copy_field_dict_encoded_as_json():
    result = bf.copy_field({"key": "val"})
    assert result == '{"key":"val"}'


def test_copy_field_list_encoded_as_json():
    result = bf.copy_field(["a", "b"])
    assert result == '["a","b"]'


# ---------------------------------------------------------------------------
# _deriver_for: for-loop body when slug is NOT directly in DERIVERS (lines 707-710)
#
# This branch fires when source_key's parent slug is not registered in DERIVERS
# but one of the slug's source keys is. We simulate it by temporarily patching
# DERIVERS and SLUG_TO_SOURCE_KEYS.
# ---------------------------------------------------------------------------


def test_deriver_for_slug_not_in_derivers_sk_found_in_loop(monkeypatch):
    """Lines 707-709: slug not in DERIVERS, but one sk in the slug's source keys IS.

    We register a fake sub-source in SOURCE_TO_BROKERAGE pointing to a slug
    that is NOT directly in DERIVERS, then register a sibling source key under
    that slug in SLUG_TO_SOURCE_KEYS that IS in DERIVERS.
    """
    from cre_ingest import SOURCE_TO_BROKERAGE

    fake_slug = "_test_slug_not_direct"
    fake_sk = "_test_source_key_direct"
    fake_sub = "_test_sub_source"

    # fake_sub -> (fake_slug, 'sub:') in SOURCE_TO_BROKERAGE
    monkeypatch.setitem(SOURCE_TO_BROKERAGE, fake_sub, (fake_slug, "sub:"))

    # fake_slug -> [fake_sub, fake_sk] in SLUG_TO_SOURCE_KEYS
    monkeypatch.setitem(bf.SLUG_TO_SOURCE_KEYS, fake_slug, [fake_sub, fake_sk])

    # Only fake_sk is in DERIVERS (NOT fake_slug, so slug-direct lookup misses)
    sentinel_fn = lambda raw: ({}, {})  # noqa: E731
    monkeypatch.setitem(bf.DERIVERS, fake_sk, sentinel_fn)

    # _deriver_for(fake_sub) path:
    #   - fake_sub not in DERIVERS (not patched in) -> no early return
    #   - mapping = (fake_slug, 'sub:') -> ok
    #   - slug = fake_slug -> NOT in DERIVERS -> no slug-direct return
    #   - for sk in [fake_sub, fake_sk]:
    #       fake_sub: not in DERIVERS -> skip
    #       fake_sk:  IS in DERIVERS -> return DERIVERS[fake_sk]  <- lines 707-709 hit
    fn = bf._deriver_for(fake_sub)
    assert fn is sentinel_fn


def test_deriver_for_slug_not_in_derivers_all_sk_missing(monkeypatch):
    """Line 710: the for-loop exhausts all source keys without finding a deriver
    -> return None at line 710."""
    from cre_ingest import SOURCE_TO_BROKERAGE

    fake_slug = "_test_slug_no_deriv"
    fake_sub = "_test_sub_no_deriv"

    monkeypatch.setitem(SOURCE_TO_BROKERAGE, fake_sub, (fake_slug, "sub:"))
    # SLUG_TO_SOURCE_KEYS: only fake_sub (not in DERIVERS), so loop finds nothing
    monkeypatch.setitem(bf.SLUG_TO_SOURCE_KEYS, fake_slug, [fake_sub])
    # Do NOT add fake_sub or fake_slug to DERIVERS -> loop returns None (line 710)
    fn = bf._deriver_for(fake_sub)
    assert fn is None
