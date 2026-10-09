import json
from pathlib import Path

import pytest

from cre_ingest import to_row
from cre_rent_evidence import listing_evidence, rent_evidence, replay_evidence

PARITY_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "rent_evidence_parity_vectors.json"
)
PARITY_VECTORS = json.loads(PARITY_FIXTURE.read_text(encoding="utf-8"))


def test_high_values_retained_and_flagged():
    evidence = rent_evidence("USD 2.50 - 250 SF/month NNN")
    assert (evidence["annual_psf_min"], evidence["annual_psf_max"]) == (30, 3000)
    assert evidence["anomalies"] == ["unusually_high_annual_psf", "wide_range"]
    assert evidence["raw_quote"] == "USD 2.50 - 250 SF/month NNN"


def test_amount_is_rent_not_term_and_basis_conflicts_are_not_nnn():
    assert rent_evidence("USD 1.125/SF/year")["annual_psf_min"] == 1.13
    assert rent_evidence("USD .75/SF/month NNN")["annual_psf_min"] == 9
    assert rent_evidence("5-year lease at USD 12/SF/year NNN")["annual_psf_min"] == 12
    assert rent_evidence("5-year lease at USD .75/SF/month NNN")["annual_psf_min"] == 9
    result = rent_evidence("USD 12/SF/year NNN or gross")
    assert result["lease_basis"] is None
    assert "lease_basis_conflict" in result["anomalies"]
    area = listing_evidence({"availableSpaceText": "10,000 - 20,000 SF"})["areas"][0]
    assert area["value"] is None
    assert area["raw_quote"] == "10,000 - 20,000 SF"


def test_explicit_field_units_and_conflicts():
    assert rent_evidence("20", "Rent USD/SF/year")["annual_psf_min"] == 20
    assert (
        rent_evidence("USD 20/SF/month", "Rent USD/SF/year")["original_period"]
        == "conflict"
    )
    assert rent_evidence("$20/month")["annual_psf_min"] is None
    assert rent_evidence("USD 20/SF")["annual_psf_min"] is None
    assert rent_evidence("USD 20/SF/year", "rent per sqm")["denominator"] == "conflict"


def test_replay_is_bounded_and_preserves_clocks():
    with pytest.raises(ValueError):
        replay_evidence([{}] * 1001)
    result = replay_evidence(
        [
            {
                "listing": {"leaseRateText": "USD 600/SF/year"},
                "collected_at": "2026-09-14T01:00:00Z",
            }
        ]
    )
    assert result[0]["rent"]["annual_psf_min"] == 600
    assert result[0]["collected_at"] == "2026-09-14T01:00:00Z"


def test_area_units_and_generic_coordinate_are_not_promoted():
    result = listing_evidence(
        {
            "sizeText": "100 units",
            "lotSizeText": "2 acres",
            "latitude": 30,
            "longitude": -80,
            "geo_source": "centroid",
        }
    )
    assert result["areas"][0]["area_kind"] == "land"
    assert result["areas"][0]["area_unit"] == "acres"
    assert result["areas"][1]["area_kind"] == "unknown"
    assert result["areas"][1]["area_unit"] == "units"
    assert result["coordinates"]["precision"] == "unknown"


def test_actual_ingest_carries_evidence_and_no_scalar_bypass():
    listing = {
        "sourceKey": "lee-associates",
        "id": "evidence-test",
        "url": "https://example.com/test",
        "leaseRateText": "USD 600/SF/year",
        "leaseRateMin": 3,
    }
    row = to_row(listing, {}, "2026-09-15T10:00:00Z")
    assert row["lease_rate_min"] == 600
    assert "rent_comp_evidence_v1" in row["raw_data"]
    listing["leaseRateText"] = "$20"
    assert to_row(listing, {}, "2026-09-15T10:00:00Z")["lease_rate_min"] is None


def test_ingest_evidence_clocks_never_use_run_or_wall_clock_fallback():
    listing = {
        "sourceKey": "lee-associates",
        "id": "evidence-clock-test",
        "url": "https://example.com/clock",
        "leaseRateText": "USD 20/SF/year",
    }
    row = to_row(listing, {}, "2026-09-15T10:00:00Z")
    evidence = row["raw_data"]["rent_comp_evidence_v1"]
    # The run-level fallback still feeds the legacy scraped_at column only.
    assert row["scraped_at"] == "2026-09-15T10:00:00Z"
    assert evidence["collected_at"] is None
    assert evidence["source_updated_at"] is None

    listing["detailObservedAt"] = "2026-09-14T08:30:00Z"
    listing["lastUpdated"] = "2026-09-01T00:00:00Z"
    evidence = to_row(listing, {}, "2026-09-15T10:00:00Z")["raw_data"][
        "rent_comp_evidence_v1"
    ]
    assert evidence["collected_at"] == "2026-09-14T08:30:00+00:00"
    assert evidence["source_updated_at"] == "2026-09-01T00:00:00+00:00"


@pytest.mark.parametrize(
    "vector", PARITY_VECTORS, ids=[v["note"] for v in PARITY_VECTORS]
)
def test_shared_parity_vectors_match_typescript(vector):
    """Same fixture as tests/ts/lib/rent-evidence.test.ts (generated from TS)."""
    got = rent_evidence(vector["input"], vector["label"])
    assert got == pytest.approx(vector["expected"])
