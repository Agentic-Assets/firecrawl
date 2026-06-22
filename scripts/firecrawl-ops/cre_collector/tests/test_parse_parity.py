"""Golden-vector parity: cre_parse.py (Python) must match lib/parse.ts (TS).

Both parser implementations load the SAME committed fixture
(tests/fixtures/golden_parse_vectors.json). The TS side is asserted in
tests/ts/lib/parse.test.ts; this test closes the loop on the Python side so the
two parsers cannot silently drift on any covered edge. See the Phase-2 data-lift
contract Section C. Pure-transform, no network/DB.
"""
import json
from pathlib import Path

import pytest

import cre_parse as p

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "golden_parse_vectors.json"
VECTORS = json.loads(FIXTURE.read_text())


def _assert_scalar(got, want):
    if want is None:
        assert got is None
    else:
        assert got == pytest.approx(want, rel=1e-9, abs=1e-6)


def _check(vec):
    fn = vec["fn"]
    inp = vec["input"]
    exp = vec["expected"]
    if fn == "parseLeaseRate":
        mn, mx, ty = p.parse_lease_rate(inp)
        assert ty == exp["type"]
        _assert_scalar(mn, exp["min"])
        _assert_scalar(mx, exp["max"])
    elif fn == "parseSizeText":
        size_sf, lot_sf = p.parse_size_text(inp)
        _assert_scalar(size_sf, exp["sizeSf"])
        _assert_scalar(lot_sf, exp["lotSf"])
    elif fn == "acresToSf":
        _assert_scalar(p.acres_to_sf(inp), exp)
    elif fn == "parseAmountIgnoringCurrencyLabel":
        _assert_scalar(p.parse_amount_ignoring_currency_label(inp), exp)
    elif fn == "parseMoney":
        _assert_scalar(p.parse_money(inp), exp)
    elif fn == "parsePercentToFraction":
        _assert_scalar(p.parse_percent_to_fraction(inp), exp)
    elif fn == "normBuildingClass":
        assert p.norm_building_class(inp) == exp
    elif fn == "isPerSfText":
        assert bool(p.is_per_sf_text(inp)) == exp
    else:
        pytest.fail(f"unknown fn in golden fixture: {fn}")


@pytest.mark.parametrize(
    "vec", VECTORS, ids=[f'{v["fn"]}:{v["input"]}' for v in VECTORS]
)
def test_python_matches_golden_vector(vec):
    _check(vec)


def test_fixture_covers_every_parser():
    """Guard that the shared fixture exercises every parser the contract names."""
    seen = {v["fn"] for v in VECTORS}
    expected = {
        "parseLeaseRate",
        "parseSizeText",
        "acresToSf",
        "parseAmountIgnoringCurrencyLabel",
        "parseMoney",
        "parsePercentToFraction",
        "normBuildingClass",
        "isPerSfText",
    }
    missing = expected - seen
    assert not missing, f"golden fixture missing coverage for: {sorted(missing)}"
