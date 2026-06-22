"""
test_cre_geo_more.py

Targets missing lines in cre_geo.py (current 96%, goal >=99%):
  61     _normalize_zip: len(digits) < 5 branch -> return None
  109    _load: row with empty zip5 skipped (continue branch)
  122-123  _load: invalid lat/lng -> except (TypeError, ValueError): pass
  170    _county_label: name present but no state -> return name

Pure-offline, no network, no DB. Uses the mini_crosswalk.csv fixture for the
ZipCbsaCrosswalk tests and a temp CSV for edge-case loading tests.
"""

import csv
import os
import tempfile

import pytest

from cre_geo import ZipCbsaCrosswalk, _county_label, _normalize_zip

_HERE = os.path.dirname(os.path.abspath(__file__))
_MINI_CSV = os.path.join(_HERE, "fixtures", "geo", "mini_crosswalk.csv")


# ---------------------------------------------------------------------------
# _normalize_zip: short-digit branch (line 61)
# ---------------------------------------------------------------------------


def test_normalize_zip_too_short_returns_none():
    """Fewer than 5 digits after stripping non-numerics -> None (line 63-64)."""
    assert _normalize_zip("123") is None
    assert _normalize_zip("1234") is None
    assert _normalize_zip("AB") is None  # no digits at all -> len(digits)==0 < 5
    assert _normalize_zip("") is None   # empty string -> len(digits)==0


def test_normalize_zip_exactly_five_digits_ok():
    """Exactly 5 digits -> returned as-is (not < 5, so no early return)."""
    assert _normalize_zip("75201") == "75201"


def test_normalize_zip_leading_zero_padded():
    """A 5-char string with a leading zero is NOT stripped by zfill (already 5)."""
    result = _normalize_zip("02110")
    assert result == "02110"


def test_normalize_zip_non_string_returns_none():
    """Non-string input (the isinstance guard at line 60) -> None."""
    assert _normalize_zip(12345) is None
    assert _normalize_zip(None) is None


# ---------------------------------------------------------------------------
# _county_label: name-only branch (line 170)
# ---------------------------------------------------------------------------


def test_county_label_name_and_state():
    """Both name and state -> 'Name, ST' (the if-branch, line 168-169)."""
    row = {"county_name": "Cook", "state": "IL"}
    assert _county_label(row) == "Cook, IL"


def test_county_label_name_only_no_state():
    """Name present, state absent -> bare name (line 170 'return name or None')."""
    row = {"county_name": "SomeName", "state": ""}
    result = _county_label(row)
    assert result == "SomeName"


def test_county_label_state_only_no_name():
    """State present but name absent -> None (line 170 'return name or None', name is '')."""
    row = {"county_name": "", "state": "TX"}
    result = _county_label(row)
    assert result is None


def test_county_label_both_empty():
    """Both empty -> None."""
    row = {"county_name": "", "state": ""}
    assert _county_label(row) is None


def test_county_label_missing_keys():
    """Missing keys -> None (row.get returns None, strip fails gracefully)."""
    result = _county_label({})
    assert result is None


# ---------------------------------------------------------------------------
# ZipCbsaCrosswalk._load: empty zip5 skip (line 109)
# ---------------------------------------------------------------------------


def test_load_skips_rows_with_empty_zip5():
    """A CSV row with an empty zip5 column must be silently skipped (line 108-109)."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="") as tf:
        writer = csv.DictWriter(tf, fieldnames=[
            "zip5", "county_fips", "county_name", "state",
            "cbsa_code", "cbsa_name", "centroid_lat", "centroid_lng"
        ])
        writer.writeheader()
        # Row with an empty zip5 -> should be skipped
        writer.writerow({
            "zip5": "", "county_fips": "48113",
            "county_name": "Dallas", "state": "TX",
            "cbsa_code": "19100", "cbsa_name": "Dallas-FW, TX",
            "centroid_lat": "32.79", "centroid_lng": "-96.80"
        })
        # Row with a whitespace-only zip5 -> also skipped
        writer.writerow({
            "zip5": "   ", "county_fips": "48113",
            "county_name": "Dallas", "state": "TX",
            "cbsa_code": "19100", "cbsa_name": "Dallas-FW, TX",
            "centroid_lat": "32.79", "centroid_lng": "-96.80"
        })
        # One valid row
        writer.writerow({
            "zip5": "75201", "county_fips": "48113",
            "county_name": "Dallas", "state": "TX",
            "cbsa_code": "19100", "cbsa_name": "Dallas-FW, TX",
            "centroid_lat": "32.79", "centroid_lng": "-96.80"
        })
        tmp_path = tf.name

    try:
        cw = ZipCbsaCrosswalk(csv_path=tmp_path)
        # Only the valid row was loaded; the empty-zip5 rows were skipped.
        assert len(cw) == 1
        assert cw.by_zip("75201") is not None
        # The empty strings produced no entries.
        assert cw.by_zip("") is None
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# ZipCbsaCrosswalk._load: invalid lat/lng -> except branch (lines 122-123)
# ---------------------------------------------------------------------------


def test_load_row_with_invalid_latlng_still_loads_by_zip():
    """A row with non-numeric centroid_lat/lng is still indexed by zip5,
    but NOT added to the centroid list (the except branch at line 122-123)."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="") as tf:
        writer = csv.DictWriter(tf, fieldnames=[
            "zip5", "county_fips", "county_name", "state",
            "cbsa_code", "cbsa_name", "centroid_lat", "centroid_lng"
        ])
        writer.writeheader()
        # Row with invalid centroid_lat (non-numeric) -> except branch fires
        writer.writerow({
            "zip5": "90210", "county_fips": "06037",
            "county_name": "Los Angeles", "state": "CA",
            "cbsa_code": "31080", "cbsa_name": "Los Angeles, CA",
            "centroid_lat": "not_a_float", "centroid_lng": "-118.406"
        })
        # Row with empty centroid_lng -> also triggers except
        writer.writerow({
            "zip5": "90211", "county_fips": "06037",
            "county_name": "Los Angeles", "state": "CA",
            "cbsa_code": "31080", "cbsa_name": "Los Angeles, CA",
            "centroid_lat": "34.09", "centroid_lng": ""
        })
        tmp_path = tf.name

    try:
        cw = ZipCbsaCrosswalk(csv_path=tmp_path)
        # Both rows loaded by zip (the zip5 index is populated before the try block).
        assert len(cw) == 2
        rec = cw.by_zip("90210")
        assert rec is not None
        assert "Los Angeles" in rec["county"]
        # No centroids loaded (invalid latlng on all rows)
        # by_latlng returns None (no centroids to search)
        assert cw.by_latlng(34.09, -118.4) is None
    finally:
        os.unlink(tmp_path)


def test_load_row_with_none_centroid_values_skips_centroid():
    """None/missing centroid columns also hit the except branch."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="") as tf:
        writer = csv.DictWriter(tf, fieldnames=[
            "zip5", "county_fips", "county_name", "state",
            "cbsa_code", "cbsa_name", "centroid_lat", "centroid_lng"
        ])
        writer.writeheader()
        writer.writerow({
            "zip5": "77002", "county_fips": "48201",
            "county_name": "Harris", "state": "TX",
            "cbsa_code": "26420", "cbsa_name": "Houston, TX",
            "centroid_lat": "", "centroid_lng": ""
        })
        tmp_path = tf.name

    try:
        cw = ZipCbsaCrosswalk(csv_path=tmp_path)
        assert len(cw) == 1
        assert cw.by_zip("77002") is not None
        # No centroid entry -> by_latlng returns None even for nearby coords
        assert cw.by_latlng(29.76, -95.37) is None
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Fixture-based crosswalk: __len__ with full mini fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cw_mini():
    return ZipCbsaCrosswalk(csv_path=_MINI_CSV)


def test_mini_crosswalk_len_is_20(cw_mini):
    """Sanity check that the mini fixture is intact (20 rows)."""
    assert len(cw_mini) == 20


def test_by_zip_short_zip_returns_none(cw_mini):
    """A 4-digit string does not normalize to a 5-digit key -> None."""
    assert cw_mini.by_zip("7520") is None


def test_by_zip_four_digit_numeric_string(cw_mini):
    """Even if padded to 5 digits it produces '07520' which is not in the fixture."""
    # '7520' -> digits='7520' -> len<5 -> _normalize_zip returns None
    assert cw_mini.by_zip("7520") is None
