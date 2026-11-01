"""test_ingest_forward_geo.py: forward-path geo derivation (AGENTIC-5133).

cre_ingest.to_row now calls cre_geo.derive_geo for every staged row, so new and
updated listings carry county / cbsa_code / cbsa_name / geo_source instead of
waiting for a one-off cre_geo_backfill.py pass.

Covers:
  - ZIP-only row -> crosswalk_zip bundle.
  - lat/lng-only row -> crosswalk_latlng bundle; a cross-state centroid fails closed.
  - Row with neither -> all-None bundle (nothing staged, nothing written).
  - Source-verbatim county (Newmark/Pyramid) -> geo_source='source', county kept,
    CBSA filled from the ZIP.
  - A stored CBSA (or any stored geo value) is kept: the generated UPDATE is
    keep-wins COALESCE(t.col, s.col), the same precedence as the backfill.
  - merge_rows takes a sale/lease pass's geo bundle whole, never column-mixed.
  - The geo columns are staged through COPY in STAGE_COLS order.
  - ZipCbsaCrosswalk.by_latlng's latitude band keeps every in-range centroid.

No network, no DB. Real production functions with synthetic listings and the
committed mini crosswalk fixture.
"""

import csv
import os
from datetime import datetime, timezone

import cre_geo
import cre_ingest as ci
import cre_validate
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MINI_CSV = os.path.join(_HERE, "fixtures", "geo", "mini_crosswalk.csv")
_FULL_CSV = os.path.join(os.path.dirname(_HERE), "data", "zip_cbsa_crosswalk.csv")
_SCRAPED_AT = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def mini_crosswalk(monkeypatch):
    monkeypatch.setattr(ci, "_GEO_CROSSWALK", cre_geo.ZipCbsaCrosswalk(csv_path=_MINI_CSV))


def _row(**fields):
    listing = {"sourceKey": "cbre", "url": "https://cbre.com/p/geo-1", "id": "geo-1"}
    listing.update(fields)
    return ci.to_row(listing, {}, _SCRAPED_AT)


def _geo(row):
    return {k: row[k] for k in ci.GEO_COLS}


def test_zip_only_row_gets_crosswalk_zip_bundle():
    row = _row(postalCode="60601", state="IL")
    assert row["lat"] is None and row["lng"] is None
    assert _geo(row) == {
        "county": "Cook County, IL",
        "cbsa_code": "16980",
        "cbsa_name": "Chicago-Naperville-Elgin IL-IN-WI",
        "geo_source": "crosswalk_zip",
    }


def test_zip_plus_four_resolves_like_zip5():
    assert _geo(_row(postalCode="60601-1234"))["cbsa_code"] == "16980"


def test_latlng_only_row_gets_crosswalk_latlng_bundle():
    row = _row(latitude=32.7870, longitude=-96.7970, state="TX")
    assert row["zip"] is None
    assert _geo(row) == {
        "county": "Dallas County, TX",
        "cbsa_code": "19100",
        "cbsa_name": "Dallas-Fort Worth-Arlington, TX",
        "geo_source": "crosswalk_latlng",
    }


def test_latlng_centroid_in_another_state_fails_closed():
    # The nearest centroid is Dallas, TX, but the broker says OK.
    assert set(_geo(_row(latitude=32.7870, longitude=-96.7970, state="OK")).values()) == {None}


def test_row_with_neither_zip_nor_latlng_stages_no_geo():
    row = _row(city="Chicago", state="IL")
    assert set(_geo(row).values()) == {None}


def test_unknown_zip_without_coordinates_stages_no_geo():
    assert set(_geo(_row(postalCode="00000")).values()) == {None}


def test_source_county_is_kept_and_cbsa_filled_from_zip():
    row = _row(county="Cook", postalCode="60606", state="IL")
    assert _geo(row) == {
        "county": "Cook",
        "cbsa_code": "16980",
        "cbsa_name": "Chicago-Naperville-Elgin IL-IN-WI",
        "geo_source": "source",
    }


def test_geo_columns_are_staged_through_copy():
    row = _row(postalCode="10019")
    sql = ci.build_sql([row], [], _SCRAPED_AT, set())
    copy_start = sql.index("COPY _stage")
    copy_header = sql[copy_start:sql.index("\n", copy_start)]
    for col in ci.GEO_COLS:
        assert col in copy_header
    data_start = sql.index("\n", copy_start) + 1
    fields = sql[data_start:sql.index("\n\\.\n", data_start)].split("\t")
    assert len(fields) == len(ci.STAGE_COLS)
    staged = {col: fields[ci.STAGE_COLS.index(col)] for col in ci.GEO_COLS}
    assert staged == {
        "county": "New York County, NY",
        "cbsa_code": "35620",
        "cbsa_name": "New York-Newark-Jersey City NY-NJ-PA",
        "geo_source": "crosswalk_zip",
    }
    stage_ddl = sql[sql.index("CREATE TEMP TABLE _stage"):copy_start]
    assert "county text, cbsa_code text, cbsa_name text, geo_source text" in stage_ddl


def test_stored_geo_values_are_never_overwritten():
    """A row whose stored CBSA (source or derived) exists keeps it: keep-wins COALESCE."""
    sql = ci.build_sql([_row(postalCode="10019")], [], _SCRAPED_AT, set())
    for col in ci.GEO_COLS:
        assert f"{col} = COALESCE(t.{col}, s.{col})" in " ".join(sql.split())
        # The source-wins form would let a fresh derivation clobber a stored value.
        assert f"COALESCE(s.{col}, t.{col})" not in sql
        assert f"COALESCE(EXCLUDED.{col}, t.{col})" not in sql


def test_merge_takes_other_pass_geo_bundle_whole():
    sale = _row(transactionMode="sale", city="Chicago")
    lease = _row(transactionMode="lease", postalCode="60601")
    merged = ci.merge_rows(sale, lease)
    assert _geo(merged) == _geo(_row(postalCode="60601"))


def test_merge_keeps_first_pass_bundle_over_second():
    zip_pass = _row(transactionMode="sale", postalCode="60601")
    other = _row(transactionMode="lease", postalCode="10019")
    merged = ci.merge_rows(zip_pass, other)
    assert merged["cbsa_code"] == "16980"
    assert merged["county"] == "Cook County, IL"


def test_validate_reports_geo_coverage_without_gating():
    sql = cre_validate.QUERIES["quality_by_source"]
    assert "AS geo_inputs_without_cbsa" in sql
    assert "AS geo_inputs_underived" in sql


def test_latlng_band_keeps_centroids_up_to_the_radius(tmp_path):
    path = tmp_path / "one.csv"
    path.write_text(
        "zip5,county_fips,county_name,state,cbsa_code,cbsa_name,centroid_lat,centroid_lng\n"
        "60601,17031,Cook County,IL,16980,Chicago,41.8864,-87.6186\n",
        encoding="utf-8",
    )
    cw = cre_geo.ZipCbsaCrosswalk(csv_path=str(path))
    # Due north/south, distance is ~111.2 km per degree of latitude.
    assert cw.by_latlng(41.8864 - 0.44, -87.6186)["cbsa_code"] == "16980"  # ~48.9 km
    assert cw.by_latlng(41.8864 + 0.44, -87.6186)["cbsa_code"] == "16980"
    assert cw.by_latlng(41.8864 - 0.46, -87.6186) is None  # ~51.2 km
    assert cw.by_latlng(float("nan"), -87.6186) is None


def test_committed_crosswalk_is_the_ingest_default():
    """The forward path loads the committed CSV; a missing file would stage no geo."""
    cw = cre_geo.ZipCbsaCrosswalk()
    assert len(cw) == 33791
    assert cw.by_zip("60601")["cbsa_code"] == "16980"


def test_empty_crosswalk_warns_once(monkeypatch, tmp_path, capsys):
    absent = str(tmp_path / "absent.csv")
    real = cre_geo.ZipCbsaCrosswalk
    monkeypatch.setattr(ci, "_GEO_CROSSWALK", None)
    monkeypatch.setattr(cre_geo, "ZipCbsaCrosswalk", lambda: real(csv_path=absent))
    assert set(_geo(_row(postalCode="60601")).values()) == {None}
    _row(postalCode="10019")
    assert capsys.readouterr().err.count("zip_cbsa_crosswalk.csv is missing or empty") == 1


def test_latlng_at_each_unique_centroid_returns_that_zip():
    cw = cre_geo.ZipCbsaCrosswalk(csv_path=_FULL_CSV)
    with open(_FULL_CSV, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    seen = {}
    for r in rows:
        seen.setdefault((r["centroid_lat"], r["centroid_lng"]), []).append(r["zip5"])
    unique = [(k, v[0]) for k, v in seen.items() if len(v) == 1 and k[0] and k[1]]
    for (lat, lng), zip5 in unique[:: max(1, len(unique) // 300)]:
        assert cw.by_latlng(float(lat), float(lng)) is cw.by_zip(zip5)
