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

NOTE: test_ingest_status_activation.py::test_update_keeps_status_sticky_resetting_only_resurrected
asserts the OLD exact CASE string and will fail after this M5 change. That file
is not owned by Owner A and must not be edited here. The integration owner must
update that one assertion to the new CASE string after merge. This is the ONLY
existing test that Owner A's changes break; it is flagged as a known stale
assertion (spec section 4.3 / 9 cross-owner note).
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
    assert "status            = CASE WHEN t.deleted_at IS NOT NULL AND t.status = 'inactive'" in sql, (
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
    assert "deleted_at        = NULL," in sql, (
        "deleted_at = NULL line missing from DO UPDATE SET. "
        "Un-delete behavior was inadvertently removed."
    )
