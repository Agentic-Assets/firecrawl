from datetime import datetime, timezone

import cre_ingest as ci


def row(retained=True, mode="sale"):
    listing = {
        "sourceKey": "jll",
        "id": "test-123",
        "url": "https://property.jll.com/listings/test-123",
        "transactionMode": mode,
        "salePriceUsd": 1000000,
        "description": "Source-reported asking price is $1,000,000.",
        "jllSearchResult": {"priceWithholdingControl": "withheld"},
    }
    if retained:
        listing["jllPublicFactsRetention"] = {"schema": "jll-public-facts-retention/v2"}
    return ci.to_row(listing, {}, datetime.now(timezone.utc).isoformat())


def test_new_format_retains_reported_price_and_control_while_legacy_replays():
    current = row()
    legacy = row(False)
    assert current["sale_price_usd"] == 1000000
    assert current["description"] == "Source-reported asking price is $1,000,000."
    assert current["raw_data"]["jllSearchResult"]["priceWithholdingControl"] == "withheld"
    assert legacy["sale_price_usd"] is None
    assert legacy["raw_data"]["jllPriceWithheld"] is True


def test_mixed_legacy_and_v2_passes_keep_v2_fact():
    merged = ci.merge_rows(row(), row(False, "lease"))
    assert merged["sale_price_usd"] == 1000000
    assert merged["raw_data"]["primary"]["jllPublicFactsRetention"]["schema"] == "jll-public-facts-retention/v2"
    merged_again = ci.merge_rows(merged, row(False, "lease"))
    assert merged_again["sale_price_usd"] == 1000000

def test_v2_foreign_currency_lease_is_not_promoted_to_usd():
    listing = {"sourceKey": "jll", "jllPublicFactsRetention": {"schema": "jll-public-facts-retention/v2"},
               "jllDetail": {"pricing": {"visibility": "withheld", "lease": {
                   "normalization": "available", "currency": "CAD"}}}}
    assert ci._jll_pricing_has_foreign_lease_currency(listing)