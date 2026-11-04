"""Normalized sale prices respect acceptance while retaining raw source evidence."""

import copy
import json
import re

import cre_ingest as ingest
import cre_validate as validate
import pytest


@pytest.mark.parametrize("path", ["numeric", "nai_currency_label"])
@pytest.mark.parametrize(
    "amount,expected",
    [
        (1_250_000, 1_250_000),
        (20_000_000_000, 20_000_000_000),
        (20_000_000_001, None),
        (100_000_000_000, None),
    ],
)
def test_incoming_sale_price_ceiling_and_emitted_sql_preserve_raw(
    path, amount, expected
):
    listing = {
        "id": "synthetic-price-bound",
        "sourceKey": "nai-global" if path == "nai_currency_label" else "cbre",
        "url": "https://example.invalid/synthetic-price-bound",
        "name": "Synthetic price boundary",
        "salePriceUsd": None if path == "nai_currency_label" else amount,
        "salePriceText": f"POUND {amount:,}" if path == "nai_currency_label" else None,
        "transactionMode": "sale",
        "providerAudit": {"kind": "synthetic"},
    }
    original = copy.deepcopy(listing)
    row = ingest.to_row(listing, {}, "2026-10-14T00:00:00Z")
    assert row["sale_price_usd"] == expected
    assert listing == original
    assert row["raw_data"] == original

    sql = ingest.build_sql([row], [], "2026-10-14T00:00:00Z", set())
    stage = re.search(r"COPY _stage \(([^)]+)\) FROM stdin;\n([^\n]+)\n\\\.", sql)
    assert stage is not None
    columns = stage.group(1).split(", ")
    cells = dict(zip(columns, stage.group(2).split("\t"), strict=True))
    assert cells["sale_price_usd"] == ("\\N" if expected is None else str(expected))
    assert json.loads(cells["raw_data"]) == original
    assert "sale_price_usd > 20000000000" in validate.QUERIES["quality_by_source"]
