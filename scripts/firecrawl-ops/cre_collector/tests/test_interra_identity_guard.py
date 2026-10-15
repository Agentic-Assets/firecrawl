"""Offline generated SQL boundary; disposable PostgreSQL proof has its own runner."""

import cre_ingest as ingest


def test_identity_guard_precedes_listing_upsert_under_existing_lifecycle_lock():
    sql = ingest.build_sql([], [], "2026-10-15T00:00:00Z", set())
    guard = sql.index("-- Interra switched from legacy")
    assert sql.index(ingest.lifecycle_transaction_lock_sql()) < guard
    assert guard < sql.index("INSERT INTO credeals.cre_listings AS t")
    block = sql[guard : sql.index("-- The transaction-wide lifecycle", guard)]
    assert "t.brokerage_id = s.brokerage_id" in block
    assert "t.source_url = s.source_url" in block
    assert "t.external_id IS DISTINCT FROM s.external_id" in block
    assert "t.deleted_at IS NULL" in block
    assert '$.**.sourceKey ? (@ == "interra-realty")' in block
    assert "RAISE EXCEPTION" in block
    assert "UPDATE " not in block and "DELETE " not in block


def test_native_interra_identity_is_preserved_for_guarded_ingestion():
    row = ingest.to_row(
        {
            "sourceKey": "interra-realty",
            "id": "12345",
            "url": "https://interrarealty.com/listing/synthetic/",
            "canonicalUrl": "https://interrarealty.com/listing/synthetic/",
            "name": "Synthetic",
            "transactionType": "Sale",
        },
        {},
        "2026-10-15T00:00:00Z",
    )
    assert row["external_id"] == "12345"
    assert row["slug"] == "interra-realty"


def test_identity_guard_rejects_two_incoming_ids_for_the_same_interra_url():
    sql = ingest.build_sql([], [], "2026-10-15T00:00:00Z", set())
    guard = sql.index("-- Interra switched from legacy")
    block = sql[guard : sql.index("-- The transaction-wide lifecycle", guard)]
    assert "incoming_interra AS MATERIALIZED" in block
    assert "JOIN incoming_interra other" in block
    assert "other.brokerage_id = s.brokerage_id" in block
    assert "other.source_url = s.source_url" in block
    assert "other.external_id IS DISTINCT FROM s.external_id" in block
    assert "Interra incoming inventory contains conflicting identities" in block
