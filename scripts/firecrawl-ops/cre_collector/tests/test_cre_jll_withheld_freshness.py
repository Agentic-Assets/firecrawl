"""Withheld JLL prices do not erase validated, non-monetary freshness proof."""

import copy
import json

import cre_ingest as ingest
import cre_validate as validate
import pytest

GENERATION = "2026-10-14T055338Z-8da3d563a9b4"
INVENTORY = "2026-10-14T10:01:12.123Z"
DETAIL = "2026-10-14T10:02:00Z"


def listing(control=True):
    return {
        "sourceKey": "jll",
        "id": "synthetic-freshness",
        "url": "https://example.invalid/synthetic-freshness",
        "transactionMode": "sale",
        "jllSearchResult": {"hidePrice": control},
        "salePriceUsd": 1_250_000,
        "salePriceText": "$1,250,000",
        "description": "Asking price $1,250,000",
        "inventoryObservedAt": INVENTORY,
        "detailObservedAt": DETAIL,
        "freshnessProvenance": {
            "generationId": GENERATION,
            "detailScope": "detail_page",
            "method": "jll_detail",
            "cacheDisposition": "live",
            "arbitraryProse": "Asking price $1,250,000",
        },
        "documents": [
            {"url": "https://example.invalid/floor.pdf", "title": "$1,250,000"}
        ],
    }


def row(payload):
    return ingest.to_row(payload, {}, "2026-10-14T10:06:06Z")


@pytest.mark.parametrize("control", [True, "unexpected-control"])
@pytest.mark.parametrize(
    "generation",
    [
        GENERATION,
        "2026-07-31T121327Z",
        "generation-1",
        "certificate-test-generation",
        "opaque_token.v1:step-2",
        "a" * 256,
    ],
)
def test_hidden_and_unknown_rows_keep_original_proof_without_price_disclosure(
    control, generation
):
    payload = listing(control)
    payload["freshnessProvenance"]["generationId"] = generation
    original = copy.deepcopy(payload)
    staged = row(payload)
    proof = validate.persisted_freshness_provenance(staged["raw_data"])
    assert proof["generation_id"] == generation
    assert proof["inventory_observed_at"] == INVENTORY
    assert proof["detail_observed_at"] == DETAIL
    assert staged["raw_data"]["freshnessProvenance"]["generationId"] == generation
    assert staged["raw_data"]["inventoryObservedAt"] == INVENTORY
    assert staged["sale_price_usd"] is None
    assert staged["raw_data"]["jllPriceWithheld"] is True
    assert "arbitraryProse" not in staged["raw_data"]["freshnessProvenance"]
    assert "$1,250,000" not in json.dumps(staged["raw_data"])
    assert payload == original


@pytest.mark.parametrize(
    "inventory",
    [
        "2026-10-14T10:01:12Z",
        "2026-10-14 10:01:12+00:00",
        "2026-10-14T06:01:12.123456789-04:00",
    ],
)
def test_original_timezone_and_precision_are_not_rewritten(inventory):
    payload = listing()
    payload["inventoryObservedAt"] = inventory
    assert row(payload)["raw_data"]["inventoryObservedAt"] == inventory


@pytest.mark.parametrize(
    "inventory",
    [
        None,
        {},
        [],
        True,
        123,
        "",
        "not-a-date",
        "2026-10-14T10:01:12",
        "2026-13-14T10:01:12Z",
        "2026-10-14T10:01:12Z Asking price $1,250,000",
        "2026-10-14$10:01:12Z",
        "2026-10-14T10:01:12Z" + " " * 65,
    ],
)
def test_malformed_inventory_is_omitted_without_inventing_a_clock(inventory):
    payload = listing()
    payload["inventoryObservedAt"] = inventory
    raw = row(payload)["raw_data"]
    assert "inventoryObservedAt" not in raw
    assert validate.persisted_freshness_provenance(raw)["inventory_observed_at"] is None
    assert raw["detailObservedAt"] == DETAIL


@pytest.mark.parametrize(
    "generation",
    [
        None,
        {},
        [],
        True,
        123,
        "",
        " generation-1 ",
        "generation with prose",
        "$1,250,000",
        "USD_1250000",
        "USD1250000",
        "1250000_USD",
        "1250000USD",
        "asking-price-1250000",
        "price:1250000",
        "generation?other",
        "x" * 257,
    ],
)
def test_missing_or_monetary_generation_is_not_promoted_to_proof(generation):
    payload = listing()
    payload["freshnessProvenance"]["generationId"] = generation
    raw = row(payload)["raw_data"]
    assert "generationId" not in raw["freshnessProvenance"]
    assert validate.persisted_freshness_provenance(raw)["generation_id"] is None
    assert raw["freshnessProvenance"]["detailScope"] == "detail_page"


@pytest.mark.parametrize("hidden_first", [True, False])
@pytest.mark.parametrize("control", [True, "unexpected-control"])
def test_forced_hidden_merge_preserves_both_passes_without_promoting_price(
    hidden_first, control
):
    visible = listing(False)
    hidden = listing(control)
    hidden["transactionMode"] = "lease"
    hidden["inventoryObservedAt"] = "2026-10-14T10:03:31Z"
    hidden["detailObservedAt"] = "2026-10-14T10:04:00Z"
    hidden["freshnessProvenance"]["cacheDisposition"] = "generation_cache"
    ordered = [hidden, visible] if hidden_first else [visible, hidden]
    originals = copy.deepcopy(ordered)
    merged = ingest.merge_rows(row(ordered[0]), row(ordered[1]))
    assert merged["external_id"] == "synthetic-freshness"
    assert merged["sale_price_usd"] is None
    for key, original in zip(("primary", "secondary_pass"), originals, strict=True):
        raw = merged["raw_data"][key]
        assert raw["inventoryObservedAt"] == original["inventoryObservedAt"]
        assert raw["detailObservedAt"] == original["detailObservedAt"]
        assert raw["freshnessProvenance"]["generationId"] == GENERATION
        assert (
            raw["freshnessProvenance"]["cacheDisposition"]
            == (original["freshnessProvenance"]["cacheDisposition"])
        )
        assert raw["jllPriceWithheld"] is True
        assert "arbitraryProse" not in raw["freshnessProvenance"]
    assert "$1,250,000" not in json.dumps(merged["raw_data"])
    assert ordered == originals


def test_visible_price_row_is_not_subject_to_hidden_projection():
    payload = listing(False)
    staged = row(payload)
    assert staged["sale_price_usd"] == 1_250_000
    assert staged["raw_data"] == payload


def test_missing_freshness_is_not_filled_from_ingest_time():
    payload = listing()
    del payload["inventoryObservedAt"]
    del payload["freshnessProvenance"]["generationId"]
    raw = row(payload)["raw_data"]
    assert "inventoryObservedAt" not in raw
    assert "generationId" not in raw["freshnessProvenance"]
