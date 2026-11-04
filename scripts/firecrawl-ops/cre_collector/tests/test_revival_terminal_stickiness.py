"""
test_revival_terminal_stickiness.py

Locks down the M5 revival terminal-stickiness fix in cre_ingest.py build_sql().

Pre-fix: the upsert revival CASE reset ANY soft-deleted row to 'active' on
reappearance, even a row that held a real terminal (sold/leased/off_market)
before it was soft-deleted. This would cause a sold listing that flickered back
into a feed to lose its terminal label.

Post-fix: revival resets to 'active' only when the prior status was 'inactive'
(the mark-missing soft-delete marker). A real terminal-bearing soft-deleted row
that reappears keeps its terminal label. The un-delete (deleted_at = NULL) still
happens in both cases.

Pure Python, no DB connection. Asserts against build_sql([], [], scraped_at, set()).

The disposable PostgreSQL transaction fixture also checks full-inventory revival
and terminal status preservation alongside detail-only lifecycle preservation.
"""

from datetime import datetime, timezone

from cre_ingest import build_sql

_SCRAPED_AT = datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc).isoformat()


def _sql():
    return build_sql([], [], _SCRAPED_AT, set())


# ---------------------------------------------------------------------------
# Assert the new tightened CASE is present
# ---------------------------------------------------------------------------


def test_revival_case_checks_inactive_status():
    sql = _sql()
    assert "WHEN t.deleted_at IS NOT NULL AND t.status = 'inactive'" in sql, (
        "Expected M5 tightened revival CASE "
        "(... AND t.status = 'inactive') not found in generated SQL.\n"
        "Old unconditional revival form may still be present."
    )


def test_revival_case_then_active():
    sql = _sql()
    # The THEN 'active' branch must be present as part of the tightened CASE.
    assert "THEN 'active' ELSE t.status END" in sql, (
        "THEN 'active' ELSE t.status END not found; revival CASE may be malformed."
    )


# ---------------------------------------------------------------------------
# Assert the old unconditional revival form is GONE
# ---------------------------------------------------------------------------


def test_old_unconditional_revival_gone():
    sql = _sql()
    old_form = "status            = CASE WHEN t.deleted_at IS NOT NULL THEN 'active' ELSE t.status END,"
    assert old_form not in sql, (
        "Old unconditional revival CASE (without t.status = 'inactive' guard) "
        "still present. M5 fix was not applied."
    )


# ---------------------------------------------------------------------------
# Assert un-delete still happens (deleted_at = NULL preserved)
# ---------------------------------------------------------------------------


def test_deleted_at_null_still_set():
    sql = _sql()
    assert "THEN t.deleted_at ELSE NULL END" in sql, (
        "Full inventory must clear deleted_at while pure SVN detail preserves it."
    )
