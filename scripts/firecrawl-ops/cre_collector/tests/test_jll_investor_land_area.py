"""Projection coverage for the JLL Investor source-owned lot acreage field."""

from datetime import datetime, timezone

import cre_ingest as ci


def test_jll_investor_lot_acres_project_to_lot_square_feet_only():
    row = ci.to_row(
        {
            "sourceKey": "jll-investor",
            "url": "https://invest.jll.com/us/en/listings/retail/example",
            "id": "006P500000vz5tCIAQ",
            "assetType": "Retail",
            "lotSizeAcres": 3.71,
            "sizeText": "90 units",
            "jllInvestorDetail": {
                "customAttributes": [{"label": "Land Area", "value": "3.71 Acres"}]
            },
        },
        {},
        datetime(2026, 10, 20, tzinfo=timezone.utc).isoformat(),
    )

    assert row["lot_size_sf"] == 3.71 * ci.SQFT_PER_ACRE
    assert row["size_sf"] is None
