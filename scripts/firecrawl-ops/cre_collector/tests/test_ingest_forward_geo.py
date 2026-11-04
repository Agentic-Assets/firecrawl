"""test_ingest_forward_geo.py: forward-path geo derivation (AGENTIC-5133).

cre_ingest.to_row now calls cre_geo.derive_geo for every staged row, so new and
updated listings carry county / cbsa_code / cbsa_name / geo_source instead of
waiting for a one-off cre_geo_backfill.py pass.

Covers:
  - ZIP-only row -> crosswalk_zip bundle.
  - lat/lng-only row -> crosswalk_latlng bundle; a cross-state centroid fails closed.
  - Row with neither -> all-None bundle (nothing staged, nothing written).
  - Source-verbatim county (Newmark/Pyramid) -> geo_source='source', county
    canonicalized to "<County>, <ST>" when the ZIP confirms it, CBSA filled from
    the ZIP.
  - guard_row_coordinates drops a point in another state or on a provider
    default pin, before geo derives from it.
  - A stored CBSA (or any stored geo value) is kept: the generated UPDATE is
    keep-wins COALESCE(t.col, s.col), the same precedence as the backfill.
  - merge_rows takes a sale/lease pass's geo bundle whole, never column-mixed.
  - The geo columns are staged through COPY in STAGE_COLS order.
  - ZipCbsaCrosswalk.by_latlng's latitude band keeps every in-range centroid.

No network, no DB. Real production functions with synthetic listings and the
committed mini crosswalk fixture.
"""

import csv
import json
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


def test_source_county_is_canonicalized_and_cbsa_filled_from_zip():
    row = _row(county="Cook", postalCode="60606", state="IL")
    assert _geo(row) == {
        "county": "Cook County, IL",
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
    """A stored county/CBSA (source or derived) is kept; executable proof is the
    disposable-Postgres contract (run_ingest_institutional_postgres_contract.sh)."""
    sql = " ".join(ci.build_sql([_row(postalCode="10019")], [], _SCRAPED_AT, set()).split())
    for col in ("county", "geo_source"):
        assert f"{col} = COALESCE(t.{col}, s.{col})" in sql
    for col in ("cbsa_code", "cbsa_name"):
        assert (
            f"{col} = CASE WHEN t.cbsa_code IS NULL AND t.cbsa_name IS NULL "
            f"THEN s.{col} ELSE t.{col} END"
        ) in sql
    for col in ci.GEO_COLS:
        # The source-wins forms would let a fresh derivation clobber a stored value.
        assert f"COALESCE(s.{col}, t.{col})" not in sql
        assert f"COALESCE(EXCLUDED.{col}, t.{col})" not in sql


def test_merge_rederives_from_merged_location_fields():
    # The sale pass has only coordinates and no state; the lease pass adds the
    # state and a ZIP. The merged row follows ZIP precedence on merged inputs.
    sale = _row(transactionMode="sale", latitude=32.7870, longitude=-96.7970)
    assert sale["geo_source"] == "crosswalk_latlng"
    lease = _row(transactionMode="lease", state="NY", postalCode="10019")
    merged = ci.merge_rows(sale, lease)
    assert merged["state"] == "NY" and merged["zip"] == "10019"
    assert _geo(merged) == _geo(_row(state="NY", postalCode="10019"))


def test_merge_keeps_source_county_from_either_pass():
    zip_only = _row(transactionMode="sale", postalCode="60606", state="IL")
    with_county = _row(transactionMode="lease", county="Cook")
    merged = ci.merge_rows(zip_only, with_county)
    assert merged["county"] == "Cook County, IL"
    assert merged["cbsa_code"] == "16980"
    assert merged["geo_source"] == "source"


def test_geo_input_keys_are_not_staged():
    assert not set(ci.GEO_INPUT_KEYS) & set(ci.STAGE_COLS)


@pytest.mark.parametrize("country", ["CA", "Canada", "MX"])
def test_non_us_country_gets_no_us_cbsa(country):
    # Windsor, ON sits within the lat/lng radius of Detroit-area centroids.
    assert _row(latitude=42.3149, longitude=-83.0364)["cbsa_code"] == "19820"
    row = _row(latitude=42.3149, longitude=-83.0364, country=country)
    assert set(_geo(row).values()) == {None}


@pytest.mark.parametrize("country", ["US", "U.S.A.", "United States", "PR"])
def test_us_country_spellings_still_derive(country):
    assert _geo(_row(postalCode="60601", country=country))["cbsa_code"] == "16980"


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
    assert len(cw) > 30000
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


# ---- Canonical source county --------------------------------------------------


def test_source_county_with_zip_in_another_county_is_kept_verbatim():
    row = _row(county="Harris", postalCode="75201", state="TX")
    assert row["county"] == "Harris"
    assert row["geo_source"] == "source"


def test_source_county_without_zip_is_kept_verbatim():
    assert _row(county="Cook", state="IL")["county"] == "Cook"


def test_source_county_with_zip_in_another_state_is_kept_verbatim():
    assert _row(county="Cook", postalCode="60601", state="IN")["county"] == "Cook"


@pytest.mark.parametrize(
    "source",
    ["King", "king county", "KING COUNTY, WA", "  King  "],
)
def test_source_county_spellings_canonicalize(source):
    assert _row(county=source, postalCode="98101", state="WA")["county"] == "King County, WA"


def test_canonical_county_needs_a_whole_word_prefix():
    zip_rec = {"state": "SD", "county_name": "Kingsbury County"}
    assert cre_geo.canonical_source_county("King", "SD", zip_rec) == "King"
    city_rec = {"state": "MO", "county_name": "St. Louis city"}
    assert cre_geo.canonical_source_county("St. Louis", "MO", city_rec) == "St. Louis city, MO"
    parish_rec = {"state": "LA", "county_name": "Orleans Parish"}
    assert cre_geo.canonical_source_county("Orleans", "LA", parish_rec) == "Orleans Parish, LA"


# ---- Coordinate normalization ----------------------------------------------------


@pytest.mark.parametrize("latitude,longitude", [(-90, -180), (90, 180), (0, 0)])
def test_coordinate_bounds_are_inclusive(latitude, longitude):
    row = _row(latitude=latitude, longitude=longitude)
    assert (row["lat"], row["lng"]) == (latitude, longitude)
    assert row["raw_data"]["latitude"] == latitude
    assert row["raw_data"]["longitude"] == longitude


@pytest.mark.parametrize("invalid", [True, False, float("nan"), float("inf"),
                                     -float("inf"), 10**400, "32.7"])
@pytest.mark.parametrize("field,other,staged", [
    ("latitude", "longitude", "lat"), ("longitude", "latitude", "lng"),
])
def test_invalid_coordinate_is_not_staged_but_record_and_raw_value_remain(field, other, staged, invalid):
    row = _row(**{field: invalid, other: 40.0})
    assert row[staged] is None
    assert row["lng" if staged == "lat" else "lat"] == 40.0
    assert row["raw_data"][field] is invalid
    assert row["external_id"] == "geo-1"


@pytest.mark.parametrize("latitude,longitude", [(True, -80), ("32.7", -80), (-91, -80)])
def test_invalid_coordinate_preserves_valid_json_in_generated_copy(latitude, longitude):
    row = _row(latitude=latitude, longitude=longitude)
    sql = ci.build_sql([row], [], _SCRAPED_AT, set())
    start = sql.index("\n", sql.index("COPY _stage")) + 1
    fields = sql[start:sql.index("\n\\.\n", start)].split("\t")
    assert fields[ci.STAGE_COLS.index("lat")] == r"\N"
    assert fields[ci.STAGE_COLS.index("lng")] == "-80"
    raw = json.loads(fields[ci.STAGE_COLS.index("raw_data")])
    assert raw["latitude"] == latitude
    assert type(raw["latitude"]) is type(latitude)
    assert raw["longitude"] == longitude


@pytest.mark.parametrize("field,value,staged", [
    ("latitude", -90.01, "lat"), ("latitude", 90.01, "lat"),
    ("longitude", -180.01, "lng"), ("longitude", 180.01, "lng"),
])
def test_out_of_range_coordinate_is_not_staged(field, value, staged):
    row = _row(**{field: value})
    assert row[staged] is None
    assert row["raw_data"][field] == value


# ---- Coordinate guard ------------------------------------------------------------


def test_coordinates_in_another_state_are_dropped_before_geo():
    # Pre-July-2026 Colliers SalesTracker pairing: a Saint Paul card with a Miami pin.
    row = _row(city="Saint Paul", state="MN", latitude=25.7681, longitude=-80.1900,
               extraFacts={"project_type": "Office"})
    assert row["lat"] is None and row["lng"] is None
    assert set(_geo(row).values()) == {None}
    assert row["extra_facts"]["project_type"] == "Office"
    assert row["extra_facts"]["coordinates_rejected"] == {
        "latitude": 25.7681,
        "longitude": -80.19,
        "reason": "outside_listing_state",
        "listing_state": "MN",
        "coordinates_state": "FL",
    }


def test_coordinates_in_the_listing_state_are_kept():
    row = _row(state="TX", latitude=32.7870, longitude=-96.7970)
    assert (row["lat"], row["lng"]) == (32.7870, -96.7970)
    assert "coordinates_rejected" not in (row["extra_facts"] or {})


def test_zip_agreeing_with_the_point_keeps_coordinates():
    # The listing state is the suspect field: ZIP and point are both New York.
    row = _row(state="PA", postalCode="10019", latitude=40.7658, longitude=-73.9876)
    assert (row["lat"], row["lng"]) == (40.7658, -73.9876)


def test_point_far_from_every_centroid_is_kept():
    # No positive evidence of another state (open water, remote land).
    row = _row(state="FL", latitude=30.0, longitude=-60.0)
    assert (row["lat"], row["lng"]) == (30.0, -60.0)


def test_state_outside_crosswalk_coverage_is_not_judged():
    row = _row(state="BC", latitude=25.7681, longitude=-80.1900)
    assert row["lat"] == 25.7681


def test_provider_default_pin_is_dropped_in_any_state():
    row = _row(state="NE", latitude=40.054, longitude=-98.237)
    assert row["lat"] is None and row["lng"] is None
    assert row["extra_facts"]["coordinates_rejected"]["reason"] == "placeholder_point"


def test_merge_rejudges_coordinates_on_the_merged_row():
    # The lease pass has the point but no state; the sale pass supplies MN.
    sale = _row(transactionMode="sale", state="MN")
    lease = _row(transactionMode="lease", latitude=25.7681, longitude=-80.1900)
    assert lease["lat"] == 25.7681
    merged = ci.merge_rows(sale, lease)
    assert merged["lat"] is None and merged["lng"] is None
    assert merged["extra_facts"]["coordinates_rejected"]["coordinates_state"] == "FL"
    assert set(_geo(merged).values()) == {None}


def test_crosswalk_conflict_requires_positive_evidence():
    cw = cre_geo.ZipCbsaCrosswalk(csv_path=_MINI_CSV)
    assert cw.coordinate_state_conflict(25.7681, -80.19, "mn") == "FL"
    assert cw.coordinate_state_conflict(25.7681, -80.19, "FL") is None
    assert cw.coordinate_state_conflict(None, -80.19, "MN") is None
    assert cw.coordinate_state_conflict("x", "y", "MN") is None
    assert cw.coordinate_state_conflict(25.7681, -80.19, None) is None


def test_validate_reports_coordinate_conflicts_in_the_full_profile_only():
    full = cre_validate.queries_for_profile()
    preflight = cre_validate.queries_for_profile(cre_validate.CHECKPOINT_PREFLIGHT_PROFILE)
    assert "coordinate_conflicts" in full
    assert "coordinate_conflicts" not in preflight
    sql = full["coordinate_conflicts"]
    assert f"km < {cre_geo.MAX_LATLNG_KM}" in sql
    for lat, lng in cre_geo.PLACEHOLDER_POINTS:
        assert f"round(l.lat::numeric, 4) = {lat}" in sql
        assert f"round(l.lng::numeric, 4) = {lng}" in sql
    for column in ("coords_rejected", "placeholder_point", "outside_listing_state"):
        assert f"AS {column}" in sql
    detail = cre_validate.render_coordinate_conflict_sql(
        cre_validate.SOURCE_KEY_SQL, "::text", detail=True
    )
    assert "SELECT id, source_key, listing_state, lat, lng, reason, coordinates_state" in detail


def test_derive_geo_ignores_a_provider_default_pin(tmp_path):
    # The backfill calls derive_geo directly, without the ingest guard. A centroid
    # sits on the pin, so only the placeholder rule keeps it from matching.
    path = tmp_path / "pin.csv"
    path.write_text(
        "zip5,county_fips,county_name,state,cbsa_code,cbsa_name,centroid_lat,centroid_lng\n"
        "68978,31129,Nuckolls County,NE,,,40.0540,-98.2380\n",
        encoding="utf-8",
    )
    cw = cre_geo.ZipCbsaCrosswalk(csv_path=str(path))
    assert cw.by_latlng(40.054, -98.237) is not None
    near_pin = {"state": "NE", "latitude": 40.054, "longitude": -98.237}
    assert cre_geo.derive_geo(near_pin, cw) == (None, None, None, None, None)
    beside_pin = {"state": "NE", "latitude": 40.0541, "longitude": -98.2371}
    assert cre_geo.derive_geo(beside_pin, cw)[0] == "Nuckolls County, NE"
    assert cre_geo.canonical_source_county(None, "IL", {"state": "IL", "county_name": "Cook County"}) is None
