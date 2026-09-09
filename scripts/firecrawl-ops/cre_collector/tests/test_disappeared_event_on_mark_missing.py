"""
test_disappeared_event_on_mark_missing.py

Locks down the M3 'disappeared' event emission in cre_ingest.py build_sql().

Pre-fix: the mark-missing block was a single UPDATE ... SET deleted_at = now().
No cre_listing_events row was written, so a listing retired by the ingestor had
no ledger entry marking when/why it went inactive.

Post-fix: the mark-missing block uses a _retired temp table to capture the
about-to-be-retired listings' prior status, runs the soft-delete UPDATE, then
INSERTs a 'disappeared' event per retired listing with source_value='mark_missing'
and old_value=prior status.

Also includes the L4a flip-metric assertion per spec section 4.7.

All subprocess cases use --dry-run --keep-artifacts; pure build_sql() calls for
the static SQL shape assertion.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from cre_ingest import build_sql

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_COLLECTOR_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FINISHED_AT = "2026-06-15T00:00:00.000Z"
_STARTED_AT = "2026-06-15T00:00:00.000Z"
_LISTING_COUNT = 3
_SCRAPED_AT = datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source_entry(source_key, count=_LISTING_COUNT, error=None):
    entry = {
        "sourceKey": source_key,
        "transaction": "sale",
        "listingsCollected": count,
    }
    if error:
        entry["error"] = error
    return entry


def _svn_listing(idx=0):
    return {
        "sourceKey": "svn",
        "url": f"https://www.svn.com/property?propertyId=svn-{idx:04d}-sale",
        "id": f"svn-{idx:04d}",
        "transactionMode": "sale",
    }


def _write_artifact(payload, tmp_path, name="artifact.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


def _run_dry(artifact_path, tmp_path):
    artifacts_dir = str(tmp_path / "artifacts")
    os.makedirs(artifacts_dir, exist_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            "cre_ingest.py",
            "--in", artifact_path,
            "--mark-missing",
            "--dry-run",
            "--keep-artifacts", artifacts_dir,
            "--mark-missing-floor", "1",
        ],
        cwd=_COLLECTOR_DIR,
        capture_output=True,
        text=True,
    )
    sql_path = os.path.join(artifacts_dir, "ingest.sql")
    sql_text = None
    if os.path.isfile(sql_path):
        with open(sql_path) as f:
            sql_text = f.read()
    return result.returncode, result.stderr, sql_text


# ---------------------------------------------------------------------------
# Positive case: singleton svn that clears the floor fires the disappeared event
# ---------------------------------------------------------------------------


class TestDisappearedEventEmittedOnMarkMissing:
    """
    Singleton svn with rows clears the mark-missing floor.
    The generated SQL must include _retired, INSERT INTO cre_listing_events,
    'disappeared', 'mark_missing', r.prior_status, and 'inactive'.
    """

    def _make_artifact(self):
        return {
            "runMeta": {"startedAt": _STARTED_AT, "finishedAt": _FINISHED_AT},
            "brokers": [],
            "sources": [_source_entry("svn")],
            "listings": [_svn_listing(i) for i in range(_LISTING_COUNT)],
        }

    def test_retired_temp_table_present(self, tmp_path):
        payload = self._make_artifact()
        art = _write_artifact(payload, tmp_path)
        rc, stderr, sql = _run_dry(art, tmp_path)

        assert rc == 0, f"ingestor exited {rc}. stderr:\n{stderr}"
        assert sql is not None
        assert "CREATE TEMP TABLE _retired" in sql, (
            "Expected _retired temp table in mark-missing SQL.\n"
            f"SQL snippet:\n{sql[:3000]}"
        )

    def test_disappeared_event_insert_present(self, tmp_path):
        payload = self._make_artifact()
        art = _write_artifact(payload, tmp_path)
        _, _, sql = _run_dry(art, tmp_path)

        assert sql is not None
        assert "INSERT INTO credeals.cre_listing_events" in sql, (
            "Expected INSERT INTO cre_listing_events in mark-missing SQL."
        )

    def test_disappeared_event_type_in_sql(self, tmp_path):
        payload = self._make_artifact()
        art = _write_artifact(payload, tmp_path)
        _, _, sql = _run_dry(art, tmp_path)

        assert sql is not None
        assert "'disappeared'" in sql, (
            "Expected event_type 'disappeared' in mark-missing SQL."
        )

    def test_mark_missing_source_value(self, tmp_path):
        payload = self._make_artifact()
        art = _write_artifact(payload, tmp_path)
        _, _, sql = _run_dry(art, tmp_path)

        assert sql is not None
        assert "'mark_missing'" in sql, (
            "Expected source_value 'mark_missing' in mark-missing SQL."
        )

    def test_prior_status_captured_from_retired(self, tmp_path):
        payload = self._make_artifact()
        art = _write_artifact(payload, tmp_path)
        _, _, sql = _run_dry(art, tmp_path)

        assert sql is not None
        assert "r.prior_status" in sql, (
            "Expected r.prior_status (from _retired) as old_value in event INSERT."
        )

    def test_new_value_inactive_in_sql(self, tmp_path):
        payload = self._make_artifact()
        art = _write_artifact(payload, tmp_path)
        _, _, sql = _run_dry(art, tmp_path)

        assert sql is not None
        assert "'inactive'" in sql, (
            "Expected 'inactive' as new_value in disappeared event INSERT."
        )


# ---------------------------------------------------------------------------
# Negative case: no mark-missing block when brokerage is blocked
# ---------------------------------------------------------------------------


class TestNoDisappearedEventWhenNoMarkMissingEligible:
    """
    A cbre artifact with only the primary source (no cbre-dealflow) should be
    blocked by the folded-coverage guard: no _retired table, no event INSERT.
    """

    def test_no_retired_table_when_blocked(self, tmp_path):
        # cbre-dealflow is absent; the guard blocks mark-missing for cbre.
        listings = [
            {
                "sourceKey": "cbre",
                "url": f"https://www.cbre.com/p/test-cbre-{i:04d}",
                "id": f"test-cbre-{i:04d}",
                "transactionMode": "sale",
            }
            for i in range(_LISTING_COUNT)
        ]
        payload = {
            "runMeta": {"startedAt": _STARTED_AT, "finishedAt": _FINISHED_AT},
            "brokers": [],
            "sources": [_source_entry("cbre")],
            "listings": listings,
        }
        art = _write_artifact(payload, tmp_path)
        rc, stderr, sql = _run_dry(art, tmp_path)

        assert rc == 0
        assert sql is not None
        assert "CREATE TEMP TABLE _retired" not in sql, (
            "SQL contained _retired table even though mark-missing was blocked.\n"
            f"SQL snippet:\n{sql[:3000]}"
        )
        # The always-on present/revive lifecycle sync has its own event INSERT,
        # but the blocked mark-missing path must not emit a disappeared event.
        assert "'disappeared'" not in sql, (
            "SQL contained disappeared event even though mark-missing was blocked."
        )


# ---------------------------------------------------------------------------
# L4a: flip-breaker metric widened to any non-active reclassification
# (added here per spec section 4.7; same SQL shape test as in folded coverage)
# ---------------------------------------------------------------------------


def test_flip_breaker_metric_counts_any_non_active_reclassification():
    """
    L4a: leaving_active must count any reclassification to a non-active status,
    not only departures from 'active'. Assert against build_sql output.
    """
    sql = build_sql([], [], _SCRAPED_AT, set())

    assert "AND s.status <> 'active'" in sql, (
        "Widened leaving_active predicate 'AND s.status <> 'active'' not found."
    )
    assert "leaving_active" in sql, (
        "leaving_active alias missing."
    )
    old_narrow_form = "WHERE s.status IS NOT NULL AND t.status = 'active'\n               ) AS leaving_active"
    assert old_narrow_form not in sql, (
        "Old narrow leaving_active filter (t.status = 'active' only) still present."
    )
