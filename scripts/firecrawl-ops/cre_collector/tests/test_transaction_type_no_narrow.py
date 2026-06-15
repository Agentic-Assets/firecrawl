"""
test_transaction_type_no_narrow.py

Locks down the transaction_type no-narrow guard in cre_ingest.py build_sql().

Pre-fix: the DO UPDATE SET block did `transaction_type = EXCLUDED.transaction_type`
unconditionally. The generic enricher (lib/enrich.ts parseGenericJsonLd) emits NO
transactionType, and the enrichment queue has no transaction column, so cre_enrich
tags every claimed row "sale". A generic-enrich re-ingest of an existing 'lease' or
'sale_or_lease' row therefore overwrote transaction_type to 'sale', silently
dropping the row off v_cre_active_for_lease (which filters transaction_type IN
('lease','sale_or_lease')). That is board-visible data corruption.

Post-fix: transaction_type NEVER narrows a known type. The CASE upgrades a
sale-vs-lease collision to 'sale_or_lease' (still on both boards), keeps an
existing 'sale_or_lease', takes incoming when the existing is NULL, and otherwise
keeps the existing value. A row can still be promoted to 'sale_or_lease' but is
never demoted off a board.

Pure Python, no DB connection. Asserts against build_sql([], [], scraped_at, set()).
"""

from datetime import datetime, timezone

from cre_ingest import build_sql

_SCRAPED_AT = datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc).isoformat()


def _sql():
    return build_sql([], [], _SCRAPED_AT, set())


# ---------------------------------------------------------------------------
# Old unconditional overwrite form is GONE
# ---------------------------------------------------------------------------


def test_unconditional_transaction_type_overwrite_gone():
    sql = _sql()
    # The DO UPDATE SET assignment line that overwrote unconditionally must be
    # gone. (The INSERT column list and the SELECT still reference EXCLUDED-less
    # `transaction_type`; we only forbid the bare DO-UPDATE assignment line.)
    assert "transaction_type  = EXCLUDED.transaction_type,\n" not in sql, (
        "Old unconditional `transaction_type = EXCLUDED.transaction_type` form "
        "still present in the DO UPDATE SET block; the no-narrow guard was not applied."
    )


# ---------------------------------------------------------------------------
# New no-narrow CASE is present, with each required branch
# ---------------------------------------------------------------------------


def test_no_narrow_case_present():
    sql = _sql()
    assert "transaction_type  = CASE" in sql, (
        "Expected a CASE expression for transaction_type in the DO UPDATE SET block."
    )


def test_existing_sale_or_lease_is_kept():
    # Existing 'sale_or_lease' must never narrow to a single mode.
    assert "WHEN t.transaction_type = 'sale_or_lease' THEN 'sale_or_lease'" in _sql()


def test_incoming_sale_or_lease_upgrades():
    # An incoming 'sale_or_lease' wins (promotion is allowed).
    assert "WHEN EXCLUDED.transaction_type = 'sale_or_lease' THEN 'sale_or_lease'" in _sql()


def test_existing_null_takes_incoming():
    # A row with no prior transaction_type takes the incoming value.
    assert "WHEN t.transaction_type IS NULL THEN EXCLUDED.transaction_type" in _sql()


def test_sale_vs_lease_collision_promotes_to_sale_or_lease():
    sql = _sql()
    # A disagreement (existing 'lease' vs incoming 'sale', or vice versa) promotes
    # to 'sale_or_lease' so the row stays on BOTH boards instead of flipping off one.
    assert "EXCLUDED.transaction_type IS DISTINCT FROM t.transaction_type" in sql
    assert "THEN 'sale_or_lease'" in sql


def test_fallback_keeps_existing():
    # Same single mode, or incoming NULL: keep the existing transaction_type.
    assert "ELSE t.transaction_type" in _sql()
