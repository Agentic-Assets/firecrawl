"""The Investor guard adds no identity normalization or source admission waiver."""

import pytest

import cre_ingest as ingest


def test_investor_collision_guard_precedes_upsert_under_existing_lock():
    sql = ingest.build_sql([], [], "2026-10-17T00:00:00Z", set())
    guard = sql.index("-- JLL Investor prospective URL collision pairs")
    end = sql.index("-- Interra switched", guard)
    assert sql.index(ingest.lifecycle_transaction_lock_sql()) < guard
    assert guard < sql.index("INSERT INTO credeals.cre_listings AS t")
    block = sql[guard:end]
    assert "{staged_source_key_sql}" not in block
    assert "EXCEPT" in block
    assert "t.deleted_at IS NULL" in block
    assert "UPDATE " not in block and "DELETE " not in block
    assert "RAISE EXCEPTION" in block


@pytest.mark.parametrize("url", [None, "", "not-a-url"])
def test_investor_missing_or_invalid_url_is_not_staged(url):
    assert (
        ingest.to_row(
            {"sourceKey": "jll-investor", "id": "006P500000wGZejIAG", "url": url},
            {},
            "2026-10-17T00:00:00Z",
        )
        is None
    )


def test_investor_literal_native_identity_is_unchanged():
    row = ingest.to_row(
        {
            "sourceKey": "jll-investor",
            "id": "006P500000wGZejIAG",
            "url": "https://invest.jll.com/us/en/listings/retail/7-eleven-memphis-in",
            "transactionType": "Sale",
        },
        {},
        "2026-10-17T00:00:00Z",
    )
    assert row["external_id"] == "investor:006P500000wGZejIAG"
    assert row["slug"] == "jll"
