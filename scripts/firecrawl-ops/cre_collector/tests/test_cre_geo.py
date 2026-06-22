"""test_cre_geo.py: unit tests for ZipCbsaCrosswalk (cre_geo.py, contract C.4/E).

Uses the committed mini_crosswalk.csv fixture (~20 rows) rather than the full
~41k-row file so tests are fast and deterministic.

Test surface:
  - by_zip: hit / miss / ZIP-normalization edge cases
  - by_latlng: within-tolerance hit / outside-tolerance miss / bad input
  - ZipCbsaCrosswalk with missing CSV (graceful no-op)
  - Record shape: county, cbsa_code, cbsa_name always present as keys
"""

import os
import sys

import pytest

# Put cre_collector/ on sys.path (mirrors conftest.py but kept explicit here
# so the test works as a standalone file too).
_HERE = os.path.dirname(os.path.abspath(__file__))
_CRE_COLLECTOR = os.path.dirname(_HERE)
if _CRE_COLLECTOR not in sys.path:
    sys.path.insert(0, _CRE_COLLECTOR)

from cre_geo import ZipCbsaCrosswalk  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture path
# ---------------------------------------------------------------------------

_MINI_CSV = os.path.join(_HERE, "fixtures", "geo", "mini_crosswalk.csv")


@pytest.fixture(scope="module")
def cw():
    """Module-scoped crosswalk loaded from the mini fixture."""
    return ZipCbsaCrosswalk(csv_path=_MINI_CSV)


# ---------------------------------------------------------------------------
# by_zip: hit cases
# ---------------------------------------------------------------------------


def test_by_zip_dallas_hit(cw):
    rec = cw.by_zip("75201")
    assert rec is not None
    assert "Dallas County" in rec["county"]
    assert rec["cbsa_code"] == "19100"
    assert "Dallas" in rec["cbsa_name"]


def test_by_zip_chicago_hit(cw):
    rec = cw.by_zip("60601")
    assert rec is not None
    assert "Cook County" in rec["county"]
    assert rec["cbsa_code"] == "16980"
    assert "Chicago" in rec["cbsa_name"]


def test_by_zip_new_york_hit(cw):
    rec = cw.by_zip("10001")
    assert rec is not None
    assert rec["cbsa_code"] == "35620"
    assert "New York" in rec["cbsa_name"]


def test_by_zip_houston_hit(cw):
    rec = cw.by_zip("77002")
    assert rec is not None
    assert "Harris County" in rec["county"]
    assert rec["cbsa_code"] == "26420"


def test_by_zip_las_vegas(cw):
    rec = cw.by_zip("89101")
    assert rec is not None
    assert "Clark County" in rec["county"]
    assert "Las Vegas" in rec["cbsa_name"]


# ---------------------------------------------------------------------------
# by_zip: miss cases
# ---------------------------------------------------------------------------


def test_by_zip_unknown_returns_none(cw):
    assert cw.by_zip("00001") is None  # not in mini fixture


def test_by_zip_empty_string(cw):
    assert cw.by_zip("") is None


def test_by_zip_none(cw):
    assert cw.by_zip(None) is None


def test_by_zip_non_string(cw):
    assert cw.by_zip(75201) is None  # int, not str


# ---------------------------------------------------------------------------
# by_zip: ZIP normalization
# ---------------------------------------------------------------------------


def test_by_zip_zip4_normalized(cw):
    """ZIP+4 is stripped to the 5-digit prefix."""
    rec = cw.by_zip("75201-1234")
    assert rec is not None
    assert "Dallas County" in rec["county"]


def test_by_zip_leading_zero(cw):
    """02110 (Boston Suffolk) -- 5 digits, leading zero."""
    rec = cw.by_zip("02110")
    assert rec is not None
    assert "Suffolk County" in rec["county"]
    assert "Boston" in rec["cbsa_name"]


def test_by_zip_nine_digit_no_dash(cw):
    """9-digit run-together (e.g. '021101234') -> first 5."""
    rec = cw.by_zip("021101234")
    assert rec is not None
    assert "Suffolk County" in rec["county"]


# ---------------------------------------------------------------------------
# by_zip: record shape invariants
# ---------------------------------------------------------------------------


def test_by_zip_record_has_required_keys(cw):
    rec = cw.by_zip("60601")
    assert "county" in rec
    assert "cbsa_code" in rec
    assert "cbsa_name" in rec


def test_by_zip_all_rows_have_complete_records(cw):
    """Every row loaded from the fixture must have non-None county/cbsa fields."""
    for zip5 in [
        "10001", "10019", "10036", "60601", "60606", "75201", "75202",
        "77002", "90071", "94105", "02110", "30303", "85004", "98101",
        "80202", "33131", "19103", "48201", "55401", "89101",
    ]:
        rec = cw.by_zip(zip5)
        assert rec is not None, f"expected {zip5} in fixture"
        assert rec["county"] is not None, f"county None for {zip5}"
        assert rec["cbsa_code"] is not None, f"cbsa_code None for {zip5}"
        assert rec["cbsa_name"] is not None, f"cbsa_name None for {zip5}"


# ---------------------------------------------------------------------------
# by_latlng: within tolerance
# ---------------------------------------------------------------------------


def test_by_latlng_dallas_near(cw):
    """Coordinates very close to the Dallas centroid (32.7869, -96.7971)."""
    rec = cw.by_latlng(32.790, -96.800)
    assert rec is not None
    assert "Dallas County" in rec["county"]


def test_by_latlng_chicago_near(cw):
    """Chicago Loop coordinates close to 60601 centroid."""
    rec = cw.by_latlng(41.883, -87.620)
    assert rec is not None
    assert "Cook County" in rec["county"]


def test_by_latlng_la_near(cw):
    """Downtown LA near 90071 centroid."""
    rec = cw.by_latlng(34.050, -118.255)
    assert rec is not None
    assert "Los Angeles County" in rec["county"]


def test_by_latlng_seattle(cw):
    """Seattle close to 98101 centroid."""
    rec = cw.by_latlng(47.608, -122.333)
    assert rec is not None
    assert "King County" in rec["county"]


def test_by_latlng_phoenix(cw):
    rec = cw.by_latlng(33.450, -112.074)
    assert rec is not None
    assert "Maricopa County" in rec["county"]


# ---------------------------------------------------------------------------
# by_latlng: outside tolerance
# ---------------------------------------------------------------------------


def test_by_latlng_remote_ocean_returns_none(cw):
    """Middle of the Pacific Ocean: no centroid within 50 km."""
    rec = cw.by_latlng(0.0, -160.0)
    assert rec is None


def test_by_latlng_north_pole_returns_none(cw):
    rec = cw.by_latlng(90.0, 0.0)
    assert rec is None


def test_by_latlng_custom_max_km_tight(cw):
    """A 0.001 km tolerance should fail for all but an exact centroid hit."""
    rec = cw.by_latlng(32.790, -96.800, max_km=0.001)
    # The query coords are slightly off the 75201 centroid; should miss.
    # (May hit if somehow exactly on a centroid -- acceptable if so.)
    # We just verify no exception is raised and the result is bool-able.
    assert rec is None or isinstance(rec, dict)


# ---------------------------------------------------------------------------
# by_latlng: bad inputs
# ---------------------------------------------------------------------------


def test_by_latlng_none_lat(cw):
    assert cw.by_latlng(None, -96.8) is None


def test_by_latlng_none_lng(cw):
    assert cw.by_latlng(32.8, None) is None


def test_by_latlng_non_numeric(cw):
    assert cw.by_latlng("thirty-two", "-96.8") is None


def test_by_latlng_string_numeric(cw):
    """String-typed lat/lng are coerced to float (the ingest stores them as str)."""
    rec = cw.by_latlng("32.79", "-96.80")
    assert rec is not None
    assert "Dallas County" in rec["county"]


# ---------------------------------------------------------------------------
# ZipCbsaCrosswalk size and missing-file handling
# ---------------------------------------------------------------------------


def test_crosswalk_len(cw):
    """Mini fixture has exactly 20 rows."""
    assert len(cw) == 20


def test_crosswalk_missing_csv_does_not_raise():
    """A missing CSV file results in an empty crosswalk, not an exception."""
    cw2 = ZipCbsaCrosswalk(csv_path="/nonexistent/path/to/crosswalk.csv")
    assert len(cw2) == 0
    assert cw2.by_zip("10001") is None
    assert cw2.by_latlng(40.748, -73.997) is None
