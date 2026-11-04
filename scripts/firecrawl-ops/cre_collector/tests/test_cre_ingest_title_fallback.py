"""Synthetic title selection contracts; no database, providers, or repair."""

import cre_ingest
import pytest


def row(**fields):
    return cre_ingest.to_row(
        {
            "sourceKey": "cbre",
            "id": "title-fixture",
            "url": "https://example.test/title-fixture",
            **fields,
        },
        {},
        "2026-10-13T00:00:00Z",
    )


@pytest.mark.parametrize(
    "name", [" ", "\t\n", 42, True, {"name": "Wrong shape"}, ["Wrong shape"]]
)
def test_unusable_name_does_not_hide_valid_headline(name):
    result = row(name=name, headline="  Valid headline  ", street="Valid street")
    assert result["title"] == "Valid headline"


@pytest.mark.parametrize("headline", [None, " ", 42, {"headline": "Wrong shape"}])
def test_unusable_name_and_headline_fall_back_to_street(headline):
    assert (
        row(name="\t", headline=headline, street="  Valid street  ")["title"]
        == "Valid street"
    )


def test_normalized_valid_name_retains_precedence():
    assert (
        row(name="  Valid name\n", headline="Headline", street="Street")["title"]
        == "Valid name"
    )


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"name": " ", "headline": "\t", "street": "\n"},
        {"name": 42, "headline": True, "street": []},
    ],
)
def test_all_unusable_candidates_produce_missing_title(fields):
    assert row(**fields)["title"] is None


@pytest.mark.parametrize("field", ["name", "headline", "street"])
def test_selected_title_keeps_existing_500_character_cap(field):
    assert row(**{field: "  " + "x" * 501 + "  "})["title"] == "x" * 500


@pytest.mark.parametrize("hide_price", [True, False])
@pytest.mark.parametrize("field", ["name", "headline", "street"])
def test_jll_withholding_applies_after_fallback_selection(field, hide_price):
    fields = {"name": " ", "headline": 42, "street": None}
    fields[field] = "  Warehouse asking $3.25M  "
    result = row(sourceKey="jll", hidePrice=hide_price, **fields)
    assert result["title"] == (
        "Warehouse asking [redacted]" if hide_price else "Warehouse asking $3.25M"
    )


def test_withheld_jll_safe_identity_text_survives_fallback():
    assert (
        row(sourceKey="jll", hidePrice=True, name=" ", headline="Building 3B")["title"]
        == "Building 3B"
    )


def test_title_cleanup_does_not_rewrite_original_provider_fields():
    result = row(name="  Valid name  ", headline=" Headline ", street=" Street ")
    assert result["title"] == "Valid name"
    assert result["raw_data"]["name"] == "  Valid name  "
    assert result["address"] == " Street "
