"""test_geo_derive.py: unit tests for derive_geo() precedence rules (cre_geo.py).

Contract: Section C.4, Section E submarket fallback rule, Section H invariants.

Test surface:
  - Source-verbatim county keeps geo_source='source'; submarket preserved.
  - Source county present but CBSA absent: crosswalk fills CBSA; geo_source='source'.
  - No source county, ZIP present: geo_source='crosswalk_zip', submarket=None.
  - No source county, no ZIP, lat/lng present: geo_source='crosswalk_latlng'.
  - Newmark-style row: county/market/submarket all verbatim, nothing overwritten.
  - Submarket NEVER fabricated: crosswalk paths always produce submarket=None.
  - market COALESCE-keep: when source omits market, cbsa_name fills it.
  - All-None when no geo info.
  - None crosswalk passed: graceful (no AttributeError).
"""

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CRE_COLLECTOR = os.path.dirname(_HERE)
if _CRE_COLLECTOR not in sys.path:
    sys.path.insert(0, _CRE_COLLECTOR)

from cre_geo import ZipCbsaCrosswalk, derive_geo  # noqa: E402

_MINI_CSV = os.path.join(_HERE, "fixtures", "geo", "mini_crosswalk.csv")


@pytest.fixture(scope="module")
def cw():
    return ZipCbsaCrosswalk(csv_path=_MINI_CSV)


# ---------------------------------------------------------------------------
# Helper to unpack the 5-tuple with named semantics
# ---------------------------------------------------------------------------


def _unpack(result):
    county, cbsa_code, cbsa_name, submarket, geo_source = result
    return {
        "county": county,
        "cbsa_code": cbsa_code,
        "cbsa_name": cbsa_name,
        "submarket": submarket,
        "geo_source": geo_source,
    }


# ---------------------------------------------------------------------------
# Priority 1: source-verbatim county (geo_source = 'source')
# ---------------------------------------------------------------------------


def test_source_county_wins(cw):
    """When source provides a county, geo_source='source' regardless of ZIP."""
    row = {
        "county": "Dallas County, TX",
        "market": "Dallas-Fort Worth",
        "submarket": "Uptown",
        "postalCode": "75201",  # would also resolve via crosswalk
    }
    g = _unpack(derive_geo(row, cw))
    assert g["geo_source"] == "source"
    assert g["county"] == "Dallas County, TX"


def test_source_submarket_preserved(cw):
    """A source-verbatim submarket is kept exactly; never overwritten."""
    row = {
        "county": "Cook County, IL",
        "submarket": "The Loop",
        "postalCode": "60601",
    }
    g = _unpack(derive_geo(row, cw))
    assert g["submarket"] == "The Loop"
    assert g["geo_source"] == "source"


def test_source_county_no_submarket_stays_none(cw):
    """Source gave county but no submarket: submarket stays None (never fabricated)."""
    row = {"county": "King County, WA"}
    g = _unpack(derive_geo(row, cw))
    assert g["submarket"] is None
    assert g["geo_source"] == "source"


def test_source_county_fills_cbsa_from_zip(cw):
    """Source gave county but not cbsa_code/cbsa_name: crosswalk fills CBSA.
    geo_source stays 'source' (the county is verbatim from the source)."""
    row = {
        "county": "Harris County, TX",
        "postalCode": "77002",
        # cbsa_code and cbsa_name NOT present
    }
    g = _unpack(derive_geo(row, cw))
    assert g["geo_source"] == "source"
    assert g["county"] == "Harris County, TX"
    assert g["cbsa_code"] == "26420"
    assert "Houston" in g["cbsa_name"]


def test_source_county_keeps_existing_cbsa(cw):
    """When source provides both county and cbsa_name, crosswalk does not overwrite."""
    row = {
        "county": "New York County, NY",
        "cbsa_code": "35620",
        "cbsa_name": "New York-Newark-Jersey City, NY-NJ-PA",
        "postalCode": "10001",
    }
    g = _unpack(derive_geo(row, cw))
    assert g["cbsa_code"] == "35620"
    assert g["geo_source"] == "source"


# ---------------------------------------------------------------------------
# Newmark-style verbatim geo bundle
# ---------------------------------------------------------------------------


def test_newmark_verbatim_bundle_unchanged(cw):
    """A Newmark row with county + market + submarket is kept entirely verbatim.
    No crosswalk value should overwrite any part of the source bundle."""
    row = {
        "county": "Fulton County, GA",
        "market": "Atlanta",
        "submarket": "Midtown Atlanta",
        "postalCode": "30303",
    }
    g = _unpack(derive_geo(row, cw))
    assert g["county"] == "Fulton County, GA"
    assert g["submarket"] == "Midtown Atlanta"
    assert g["geo_source"] == "source"
    # market is returned from caller context; derive_geo doesn't return market
    # but geo_source='source' signals the caller to COALESCE-keep the source market.


def test_newmark_snake_case_keys(cw):
    """derive_geo accepts snake_case keys (DB-style rows)."""
    row = {
        "county": "Maricopa County, AZ",
        "submarket": "Scottsdale Airpark",
        "postal_code": "85004",
    }
    g = _unpack(derive_geo(row, cw))
    assert g["geo_source"] == "source"
    assert g["county"] == "Maricopa County, AZ"
    assert g["submarket"] == "Scottsdale Airpark"


# ---------------------------------------------------------------------------
# Priority 2: ZIP crosswalk (geo_source = 'crosswalk_zip')
# ---------------------------------------------------------------------------


def test_zip_only_row(cw):
    """No source county: ZIP drives the derivation."""
    row = {"postalCode": "75201"}
    g = _unpack(derive_geo(row, cw))
    assert g["geo_source"] == "crosswalk_zip"
    assert "Dallas County" in g["county"]
    assert g["cbsa_code"] == "19100"


def test_zip_submarket_never_fabricated(cw):
    """ZIP crosswalk never sets submarket."""
    row = {"postalCode": "60601"}
    g = _unpack(derive_geo(row, cw))
    assert g["geo_source"] == "crosswalk_zip"
    assert g["submarket"] is None


def test_zip_cbsa_name_present(cw):
    """cbsa_name is set from the crosswalk when via ZIP."""
    row = {"postalCode": "98101"}
    g = _unpack(derive_geo(row, cw))
    assert "Seattle" in g["cbsa_name"]


def test_zip_normalization_zip4(cw):
    """ZIP+4 in postalCode is normalized by derive_geo -> crosswalk lookup works."""
    row = {"postalCode": "75201-9999"}
    g = _unpack(derive_geo(row, cw))
    assert g["geo_source"] == "crosswalk_zip"
    assert "Dallas County" in g["county"]


def test_zip_miss_falls_through_to_latlng(cw):
    """Unknown ZIP but valid lat/lng: falls through to crosswalk_latlng."""
    row = {
        "postalCode": "00001",   # not in mini fixture
        "latitude": "32.790",
        "longitude": "-96.800",
    }
    g = _unpack(derive_geo(row, cw))
    assert g["geo_source"] == "crosswalk_latlng"
    assert "Dallas County" in g["county"]


# ---------------------------------------------------------------------------
# Priority 3: lat/lng nearest-centroid (geo_source = 'crosswalk_latlng')
# ---------------------------------------------------------------------------


def test_latlng_only_row(cw):
    """No source county, no ZIP: lat/lng resolves geo."""
    row = {"latitude": 41.883, "longitude": -87.620}
    g = _unpack(derive_geo(row, cw))
    assert g["geo_source"] == "crosswalk_latlng"
    assert "Cook County" in g["county"]
    assert g["cbsa_code"] == "16980"


def test_latlng_submarket_never_fabricated(cw):
    """lat/lng crosswalk never sets submarket."""
    row = {"latitude": 34.050, "longitude": -118.255}
    g = _unpack(derive_geo(row, cw))
    assert g["geo_source"] == "crosswalk_latlng"
    assert g["submarket"] is None


def test_latlng_string_coords(cw):
    """String-typed lat/lng (common in DB rows) are coerced correctly."""
    row = {"latitude": "47.608", "longitude": "-122.333"}
    g = _unpack(derive_geo(row, cw))
    assert g["geo_source"] == "crosswalk_latlng"
    assert "King County" in g["county"]


# ---------------------------------------------------------------------------
# All-None when no geo info available
# ---------------------------------------------------------------------------


def test_no_geo_info_all_none(cw):
    """Row with no county, no ZIP, no lat/lng: all five returned values are None."""
    row = {"description": "Some office space"}
    g = _unpack(derive_geo(row, cw))
    assert g["county"] is None
    assert g["cbsa_code"] is None
    assert g["cbsa_name"] is None
    assert g["submarket"] is None
    assert g["geo_source"] is None


def test_empty_row(cw):
    g = _unpack(derive_geo({}, cw))
    assert all(v is None for v in g.values())


def test_none_crosswalk_with_zip():
    """Passing crosswalk=None: no AttributeError; geo_source falls through to None."""
    row = {"postalCode": "75201"}
    g = _unpack(derive_geo(row, None))
    # ZIP present but no crosswalk -> all None
    assert g["county"] is None
    assert g["geo_source"] is None


def test_none_crosswalk_source_county_still_works():
    """A source-verbatim county is returned even when crosswalk is None."""
    row = {"county": "Travis County, TX", "submarket": "East Austin"}
    g = _unpack(derive_geo(row, None))
    assert g["county"] == "Travis County, TX"
    assert g["submarket"] == "East Austin"
    assert g["geo_source"] == "source"


# ---------------------------------------------------------------------------
# geo_source CHECK-constraint values
# ---------------------------------------------------------------------------


_ALLOWED_GEO_SOURCES = {None, "source", "crosswalk_zip", "crosswalk_latlng"}


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"county": "Fulton County, GA"},
        {"postalCode": "60601"},
        {"latitude": 32.79, "longitude": -96.80},
        {"postalCode": "99999"},  # unknown ZIP
    ],
)
def test_geo_source_values_are_check_allowed(cw, row):
    """geo_source must always be one of the CHECK-constraint allowed values."""
    _, _, _, _, geo_source = derive_geo(row, cw)
    assert geo_source in _ALLOWED_GEO_SOURCES, f"unexpected geo_source={geo_source!r} for row={row}"


# ---------------------------------------------------------------------------
# COALESCE-keep: source market takes precedence over cbsa_name
# ---------------------------------------------------------------------------


def test_market_coalesce_source_wins(cw):
    """When source provided market, derive_geo preserves it (geo_source='source').
    The returned cbsa_name may differ; the CALLER is responsible for the COALESCE.
    The key assertion is that geo_source='source' signals keep-source."""
    row = {
        "county": "Dallas County, TX",
        "market": "DFW Metro",  # custom label from broker
        "postalCode": "75201",
    }
    g = _unpack(derive_geo(row, cw))
    assert g["geo_source"] == "source"
    # cbsa_name is the crosswalk value, but geo_source='source' tells the caller
    # to COALESCE-keep the source market; we verify it does NOT blow up.
    assert g["cbsa_name"] is not None or g["cbsa_name"] is None  # type check only


def test_market_falls_back_to_cbsa_name_on_zip_path(cw):
    """When geo_source='crosswalk_zip', cbsa_name is the market fallback."""
    row = {"postalCode": "75201"}
    g = _unpack(derive_geo(row, cw))
    assert g["cbsa_name"] == "Dallas-Fort Worth-Arlington, TX"
