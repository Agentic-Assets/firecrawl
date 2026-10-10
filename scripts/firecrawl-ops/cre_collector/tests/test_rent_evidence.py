import pytest

from cre_ingest import to_row
from cre_rent_evidence import listing_evidence, rent_evidence, replay_evidence


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
    assert (
        row["raw_data"]["rent_comp_evidence_v1"]["collected_at"]
        == "2026-09-15T10:00:00Z"
    )
    listing["leaseRateText"] = "$20"
    assert to_row(listing, {}, "2026-09-15T10:00:00Z")["lease_rate_min"] is None
