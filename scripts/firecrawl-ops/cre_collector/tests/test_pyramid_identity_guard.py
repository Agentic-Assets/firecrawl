"""The exact generated pre-DML boundary has a separate local PostgreSQL replay."""

import re

import cre_ingest as ingest


def test_pyramid_guard_is_literal_source_bound_and_before_any_listing_write():
    sql = ingest.build_sql([], [], "2026-10-16T00:00:00Z", set())
    begin = sql.index("-- Pyramid preserves historical literal path identities.")
    end = sql.index("END $$;", begin) + len("END $$;")
    block = sql[begin:end]
    assert sql.index(ingest.lifecycle_transaction_lock_sql()) < begin
    listing_writes = list(re.finditer(
        r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+credeals\.cre_listings\b",
        sql, re.IGNORECASE,
    ))
    assert listing_writes
    assert all(end < write.start() for write in listing_writes)
    assert '$.**.sourceKey ? (@ == "pyramid-brokerage")' in block
    assert "t.brokerage_id = s.brokerage_id" in block
    assert "t.source_url = s.source_url" in block
    assert "t.external_id IS DISTINCT FROM s.external_id" in block
    assert "t.external_id = s.external_id" in block
    assert "t.source_url IS DISTINCT FROM s.source_url" in block
    assert "JOIN incoming_pyramid other" in block
    assert "deleted_at IS NULL" not in block
    assert "lower(" not in block and "regexp_replace(" not in block
    assert "UPDATE " not in block and "DELETE " not in block
