"""
test_price_history_snapshot.py

Locks down the H4a price history snapshot in cre_ingest.py build_sql().

The ingestor now maintains an append-only cre_listing_price_history table.
Before the upsert, it captures prior watched values into _prior_vals. After
the upsert and status activation UPDATE, it INSERTs a history row for any
listing whose watched field (sale_price_usd, sale_price_per_sf, lease_rate_min,
lease_rate_max, status, cap_rate) changed vs the prior value.

Key contracts tested:
- _prior_vals is created BEFORE _up in the SQL (spec ordering requirement).
- The INSERT column list matches the exact contract in spec section 2.1.
- All six IS DISTINCT FROM predicates are present.
- history_guard=True (apply) wraps the INSERT in a to_regclass existence guard.
- history_guard=False (dry-run) emits the INSERT unconditionally (no guard).

Pure Python, no DB connection. Asserts against build_sql() output.
"""

from datetime import datetime, timezone

from cre_ingest import build_sql

_SCRAPED_AT = datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc).isoformat()

# The exact column list from spec section 2.1 (Owner B's CREATE TABLE column order).
_EXPECTED_COL_LIST = (
    "(listing_id, observed_at, sale_price_usd, sale_price_per_sf,\n"
    "     lease_rate_min, lease_rate_max, status, cap_rate, source_lastmod, transaction_type)"
)


# ---------------------------------------------------------------------------
# Dry-run form (history_guard=False): INSERT is a plain top-level statement
# ---------------------------------------------------------------------------


def test_history_insert_present_in_dry_run_form():
    sql = build_sql([], [], _SCRAPED_AT, set(), history_guard=False)
    assert "INSERT INTO credeals.cre_listing_price_history" in sql, (
        "Expected INSERT INTO cre_listing_price_history in dry-run SQL."
    )


def test_history_column_list_exact_match_dry_run():
    sql = build_sql([], [], _SCRAPED_AT, set(), history_guard=False)
    assert _EXPECTED_COL_LIST in sql, (
        f"Expected exact column list:\n{_EXPECTED_COL_LIST!r}\nnot found in SQL."
    )


def test_history_six_is_distinct_from_predicates_dry_run():
    sql = build_sql([], [], _SCRAPED_AT, set(), history_guard=False)
    watched_fields = [
        "t.sale_price_usd    IS DISTINCT FROM p.sale_price_usd",
        "t.sale_price_per_sf IS DISTINCT FROM p.sale_price_per_sf",
        "t.lease_rate_min    IS DISTINCT FROM p.lease_rate_min",
        "t.lease_rate_max    IS DISTINCT FROM p.lease_rate_max",
        "t.status            IS DISTINCT FROM p.status",
        "t.cap_rate          IS DISTINCT FROM p.cap_rate",
    ]
    for field_pred in watched_fields:
        assert field_pred in sql, (
            f"Expected IS DISTINCT FROM predicate not found: {field_pred!r}"
        )


def test_prior_vals_referenced_in_dry_run():
    sql = build_sql([], [], _SCRAPED_AT, set(), history_guard=False)
    assert "_prior_vals" in sql, (
        "Expected _prior_vals temp table reference in SQL."
    )


def test_no_regclass_guard_in_dry_run_form():
    sql = build_sql([], [], _SCRAPED_AT, set(), history_guard=False)
    assert "to_regclass('credeals.cre_listing_price_history')" not in sql, (
        "to_regclass guard should NOT appear in dry-run (history_guard=False) form."
    )


# ---------------------------------------------------------------------------
# Apply form (history_guard=True): INSERT is wrapped in existence guard
# ---------------------------------------------------------------------------


def test_regclass_guard_present_in_apply_form():
    sql = build_sql([], [], _SCRAPED_AT, set(), history_guard=True)
    assert "to_regclass('credeals.cre_listing_price_history')" in sql, (
        "Expected to_regclass guard in apply (history_guard=True) SQL."
    )


def test_history_insert_still_present_inside_apply_form():
    sql = build_sql([], [], _SCRAPED_AT, set(), history_guard=True)
    assert "INSERT INTO credeals.cre_listing_price_history" in sql, (
        "INSERT INTO cre_listing_price_history must be present inside the guard."
    )


# ---------------------------------------------------------------------------
# Ordering: _prior_vals is created BEFORE _up
# ---------------------------------------------------------------------------


def test_prior_vals_created_before_up():
    sql = build_sql([], [], _SCRAPED_AT, set())
    assert "_prior_vals" in sql, "_prior_vals not in SQL at all."
    assert "CREATE TEMP TABLE _up" in sql, "_up not in SQL at all."
    # _prior_vals must appear before _up (H4a spec ordering requirement).
    idx_prior = sql.index("_prior_vals")
    idx_up = sql.index("CREATE TEMP TABLE _up")
    assert idx_prior < idx_up, (
        f"_prior_vals (at {idx_prior}) must be created before _up (at {idx_up}). "
        "History snapshot must read prior values before the upsert mutates them."
    )


# ---------------------------------------------------------------------------
# default signature: history_guard defaults to True (apply form)
# ---------------------------------------------------------------------------


def test_default_history_guard_is_true():
    """Calling build_sql without history_guard must produce the guarded form."""
    sql_default = build_sql([], [], _SCRAPED_AT, set())
    sql_explicit = build_sql([], [], _SCRAPED_AT, set(), history_guard=True)
    # Both should contain the regclass guard.
    assert "to_regclass('credeals.cre_listing_price_history')" in sql_default, (
        "Default build_sql() should produce the guarded (apply) form."
    )
    # The guarded and default forms should be identical.
    assert sql_default == sql_explicit, (
        "Default build_sql() output differs from explicit history_guard=True."
    )
