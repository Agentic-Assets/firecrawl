"""Pure scope contract for the generated optional institutional UPDATE."""

import re

import pytest

import cre_ingest


@pytest.mark.parametrize("history_guard", [False, True])
def test_institutional_update_binds_canonical_source_brokerage(history_guard):
    sql = cre_ingest.build_sql(
        [], [], "2026-10-12T00:00:00Z", set(), history_guard=history_guard
    )
    start = sql.index("-- Phase-2 data-lift: write the new institutional columns")
    end = sql.index("END $$;", start)
    statement = sql[start:end]

    assert "column_name = 'building_class'" in statement
    assert "s.raw_data->>'jllPriceWithheld' = 'true'" in statement
    assert "b.slug" in statement
    # The canonical source predicate reaches into brokerage metadata. Its alias
    # must belong to THIS UPDATE, rather than another statement in the script.
    assert re.search(
        r"FROM _src s\s+JOIN credeals\.cre_brokerages b "
        r"ON b\.id = s\.brokerage_id\s+WHERE t\.brokerage_id = s\.brokerage_id",
        statement,
    )
