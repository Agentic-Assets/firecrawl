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
    reverse = ci.merge_rows(row(False, "lease"), row())
    assert reverse["sale_price_usd"] == 1000000

def test_v2_foreign_currency_lease_is_not_promoted_to_usd():
    listing = {"sourceKey": "jll", "jllPublicFactsRetention": {"schema": "jll-public-facts-retention/v2"},
               "jllDetail": {"pricing": {"visibility": "withheld", "lease": {
                   "normalization": "available", "currency": "CAD"}}}}
    assert ci._jll_pricing_has_foreign_lease_currency(listing)


def test_legacy_visible_pass_survives_withheld_sibling_in_both_orders():
    for reverse in (False, True):
        visible = ci.to_row({"sourceKey": "jll", "id": "test-123", "url": "https://property.jll.com/listings/test-123",
                             "transactionMode": "sale", "salePriceUsd": 1000000,
                             "description": "Visible public offering", "hidePrice": False}, {},
                            "2026-11-04T00:00:00Z")
        withheld = ci.to_row({"sourceKey": "jll", "id": "test-123", "url": "https://property.jll.com/listings/test-123",
                              "transactionMode": "lease", "hidePrice": True, "leaseRateText": "$99/SF"}, {},
                             "2026-11-04T00:00:00Z")
        merged = ci.merge_rows(withheld, visible) if reverse else ci.merge_rows(visible, withheld)
        assert merged["sale_price_usd"] == 1000000
        assert merged["description"] == "Visible public offering"
        assert merged["lease_rate_min"] is None
        assert merged["raw_data"]["jllPriceWithheld"] is True
        raw_visible = merged["raw_data"]["secondary_pass" if reverse else "primary"]
        raw_withheld = merged["raw_data"]["primary" if reverse else "secondary_pass"]
        assert raw_visible["salePriceUsd"] == 1000000
        assert raw_withheld["jllPriceWithheld"] is True
        assert raw_withheld["jllLegacySourceControls"]["listing"] == {"hidePrice": True}
        assert "leaseRateText" not in raw_withheld
