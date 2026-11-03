"""Coordinate observations must never be assembled across source passes."""

import cre_ingest as ci


def _row(**fields):
    return ci.to_row(
        {
            "sourceKey": "daum-commercial",
            "id": "synthetic",
            "url": "https://example.test/property",
            **fields,
        },
        {},
        "2026-11-03T00:00:00Z",
    )


def test_partial_passes_never_make_a_point_and_raw_observations_remain():
    a = _row(transactionMode="sale", latitude=40.0)
    b = _row(transactionMode="lease", longitude=-80.0)
    merged = ci.merge_rows(a, b)
    assert (merged["lat"], merged["lng"]) == (40.0, None)
    assert merged["raw_data"]["primary"]["latitude"] == 40.0
    assert merged["raw_data"]["secondary_pass"]["longitude"] == -80.0


def test_complete_pair_replaces_partial_but_not_an_existing_complete_pair():
    a = _row(latitude=40.0)
    b = _row(latitude=41.0, longitude=-80.0)
    assert (ci.merge_rows(a, b)["lat"], a["lng"]) == (41.0, -80.0)
    c = _row(latitude=39.0, longitude=-81.0)
    ci.merge_rows(a, c)
    assert (a["lat"], a["lng"]) == (41.0, -80.0)


def test_guard_rejects_known_placeholder_without_losing_source():
    lat, lng = next(iter(ci.cre_geo.PLACEHOLDER_POINTS))
    row = _row(latitude=lat, longitude=lng)
    assert (row["lat"], row["lng"]) == (None, None)
    assert (row["raw_data"]["latitude"], row["raw_data"]["longitude"]) == (lat, lng)


def test_opposite_partial_merge_order_and_complete_pair_both_orders():
    first = ci.merge_rows(_row(longitude=-80.0), _row(latitude=40.0))
    assert (first["lat"], first["lng"]) == (None, -80.0)
    assert first["raw_data"]["primary"]["longitude"] == -80.0
    assert first["raw_data"]["secondary_pass"]["latitude"] == 40.0
    for a, b in [
        (_row(latitude=40.0), _row(latitude=41.0, longitude=-80.0)),
        (_row(latitude=41.0, longitude=-80.0), _row(latitude=40.0)),
    ]:
        merged = ci.merge_rows(a, b)
        assert (merged["lat"], merged["lng"]) == (41.0, -80.0)
