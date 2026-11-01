"""AGENTIC-5119: size and money basis at capture (pure, no DB, no network).

Covers the Python side of the listing-structure parse fixes: the shared money
parser's magnitude suffixes, the cap-rate plausibility floor on both writers
(ingest and the raw_data backfill), land area routed to lot_size_sf for CBRE and
JLL land, Pyramid "+/-" sizes, "N units" size text, and the Avison per-acre unit
price. The TS side and the shared golden vectors are asserted separately.
"""

from datetime import datetime, timezone

import pytest

import cre_backfill_raw_data as bf
import cre_ingest as ci
import cre_parse

_SCRAPED_AT = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc).isoformat()


def _row(**listing):
    base = {"url": f"https://example.com/{listing.get('id', 'x')}", "id": "x"}
    return ci.to_row({**base, **listing}, {}, _SCRAPED_AT)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$275M", 275_000_000),
        ("Asking $2.5 MM", 2_500_000),
        ("$1.2 million", 1_200_000),
        ("$500K", 500_000),
        ("$1.5B", 1_500_000_000),
        ("$80,000,000", 80_000_000),
        ("$950,000 M&M exclusive", 950_000),
        ("$12 Monthly", 12),
        ("$0", None),
        ("Contact Broker for Pricing", None),
    ],
)
def test_parse_money_reads_magnitude_suffixes(text, expected):
    got = cre_parse.parse_money(text)
    assert got == (pytest.approx(expected) if expected is not None else None)


def test_currency_label_amount_keeps_its_magnitude():
    # Mirrors lib/parse.ts parseAmountIgnoringCurrencyLabel, which delegates to
    # parseMoney after stripping the label.
    assert cre_parse.parse_amount_ignoring_currency_label("POUND 1.5M") == 1_500_000
    assert cre_parse.parse_amount_ignoring_currency_label("POUND 2500000") == 2_500_000


@pytest.mark.parametrize(
    "value,expected",
    [(1, None), (1.38, None), (0.01, None), (1.99, None), (2, 0.02), (6.5, 0.065), (0.07, 0.07)],
)
def test_cap_rate_floor(value, expected):
    assert ci.norm_cap_rate(value) == expected


def test_marcus_raw_one_percent_cap_rate_is_rejected_on_both_writers():
    assert _row(sourceKey="marcus-millichap", capRatePct=1)["cap_rate"] is None
    raw = {"marcusSpecifications": {"Cap Rate": "1.00%"}, "capRatePct": 1}
    cols, _ = bf.derive_columns("marcus-millichap", raw)
    assert "cap_rate" not in cols
    raw = {"marcusSpecifications": {"Cap Rate": "6.25%"}}
    cols, _ = bf.derive_columns("marcus-millichap", raw)
    assert cols["cap_rate"] == 0.0625


@pytest.mark.parametrize("source_key", ["cbre", "jll"])
def test_land_area_goes_to_lot_not_building_size(source_key):
    row = _row(sourceKey=source_key, assetType="Land", buildingSizeSqft=217800.09377600838)
    assert row["size_sf"] is None
    assert row["lot_size_sf"] == 217800.09
    assert row["property_type"] == "land"


def test_land_lot_already_known_drops_the_duplicate_size():
    row = _row(sourceKey="cbre", assetType="Land", buildingSizeSqft=87120, lotSizeAcres=2.0)
    assert row["size_sf"] is None
    assert row["lot_size_sf"] == 87120


def test_non_land_and_other_sources_keep_building_size():
    office = _row(sourceKey="cbre", assetType="Office", buildingSizeSqft=50000)
    assert (office["size_sf"], office["lot_size_sf"]) == (50000, None)
    other = _row(sourceKey="newmark", assetType="Land", buildingSizeSqft=50000)
    assert other["size_sf"] == 50000


def test_land_with_distinct_building_keeps_both():
    assert ci.land_area_to_lot("jll", "land", 12000, 435600) == (12000, 435600)


def test_pyramid_plus_minus_sizes_parse():
    assert cre_parse.parse_size_text("4,863 +/-SF") == (4863, None)
    assert cre_parse.parse_size_text("2.00+/- Acres") == (None, 87120)
    row = _row(sourceKey="pyramid-brokerage", sizeText="123.50+/- Acres", assetType="Land")
    assert row["lot_size_sf"] == pytest.approx(123.5 * 43560)


@pytest.mark.parametrize(
    "text,expected",
    [("120 Units", 120), ("1,175 units", 1175), ("1 Unit", 1), ("931 sq ft", None), ("12 units on 2 acres", None), (None, None)],
)
def test_parse_units_text(text, expected):
    assert cre_parse.parse_units_text(text) == expected


def test_units_only_size_text_promotes_to_units_not_size():
    row = _row(sourceKey="colliers", sizeText="120 Units")
    assert row["units"] == 120
    assert row["size_sf"] is None
    # An explicit unit count wins over size text.
    assert _row(sourceKey="colliers", sizeText="120 Units", units=118)["units"] == 118


def _avison_raw(**rsl):
    base = {
        "id": 20228, "type": ["land.residential"], "sale_currency": "USD",
        "sale_price": 1_125_540, "sale_unit_price": 18_500,
        "description": "<p>60.84 +/- Acres available for sale in North Hall</p>",
    }
    return {"id": 20228, "rawSharpLaunch": {**base, **rsl}}


def test_avison_backfill_writes_price_per_acre_never_per_sf():
    cols, _ = bf.derive_columns("avison-young", _avison_raw())
    assert cols["price_per_acre"] == 18_500
    assert "sale_price_per_sf" not in cols


@pytest.mark.parametrize(
    "override",
    [
        {"description": "<p>For Sale</p>"},
        {"type": ["office.medical"]},
        {"sale_unit_price": 1_125_540},
        {"sale_currency": "CAD"},
    ],
)
def test_avison_unproved_unit_price_writes_nothing(override):
    cols, _ = bf.derive_columns("avison-young", _avison_raw(**override))
    assert "price_per_acre" not in cols
    assert "sale_price_per_sf" not in cols


def test_avison_acreage_from_total_surface():
    raw = _avison_raw(description=None, total_surface=60.84, total_surface_unit="ac")
    assert bf.avison_price_per_acre(raw["rawSharpLaunch"]) == 18_500
