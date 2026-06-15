"""
test_folded_coverage_count_aware.py

Locks down the M1 count-aware folded-coverage fix in cre_ingest.py main().

The pre-fix check was presence-only: a folded source key in sources[] with
listingsCollected=0 satisfied has_complete_folded_coverage and the brokerage
would be mark-missing'd even though one of its sub-sources contributed nothing.
The fix requires every folded key to have a nonzero discovered count this run.

All cases run via subprocess --dry-run --mark-missing --mark-missing-floor 1
--keep-artifacts. The generated ingest.sql is inspected for the presence or
absence of the _retired temp table and related mark-missing SQL.

Also includes the L4a flip-metric assertion (added here per spec section 4.7
to stay within the 6 named test files):
  test_flip_breaker_metric_counts_any_non_active_reclassification
"""

import json
import os
import subprocess
import sys

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_COLLECTOR_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FINISHED_AT = "2026-06-15T00:00:00.000Z"
_STARTED_AT = "2026-06-15T00:00:00.000Z"
_LISTING_COUNT = 3


# ---------------------------------------------------------------------------
# Helpers (local copies; do not import from other test files)
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


def _listing(source_key, idx=0):
    return {
        "sourceKey": source_key,
        "url": f"https://www.colliers.com/p/test-{source_key}-{idx:04d}",
        "id": f"test-{source_key}-{idx:04d}",
        "transactionMode": "sale",
    }


def _svn_listing(idx=0):
    return {
        "sourceKey": "svn",
        "url": f"https://www.svn.com/property?propertyId=svn-{idx:04d}-sale",
        "id": f"svn-{idx:04d}",
        "transactionMode": "sale",
    }


def _artifact(sources, listings):
    return {
        "runMeta": {
            "startedAt": _STARTED_AT,
            "finishedAt": _FINISHED_AT,
        },
        "brokers": [],
        "sources": sources,
        "listings": listings,
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


def _mark_missing_present_for_slug(sql_text, slug):
    """Return True when the SQL contains a _retired table scoped to slug."""
    if sql_text is None:
        return False
    # The new mark-missing block uses _retired and slug IN (...)
    block_start = sql_text.find("CREATE TEMP TABLE _retired")
    if block_start == -1:
        # Fall back to the deleted_at marker from the UPDATE
        block_start = sql_text.find("deleted_at = now()")
        if block_start == -1:
            return False
    block = sql_text[block_start: block_start + 2000]
    return f"'{slug}'" in block and ("b.slug IN" in block or "slug_list" in block.lower())


# ---------------------------------------------------------------------------
# CASE A: bug scenario -- colliers-main present in sources[] with count=0
# ---------------------------------------------------------------------------


class TestFoldedCoverageBugScenario:
    """
    Artifact lists BOTH colliers and colliers-main in sources[], but
    colliers-main has listingsCollected=0 and contributes NO listings.
    The count-aware guard must block mark-missing for the colliers brokerage.
    """

    def test_mark_missing_blocked_when_folded_source_has_zero_count(self, tmp_path):
        sources = [
            _source_entry("colliers", count=_LISTING_COUNT),
            _source_entry("colliers-main", count=0),  # present but zero rows
        ]
        listings = [_listing("colliers", i) for i in range(_LISTING_COUNT)]
        payload = _artifact(sources, listings)
        art = _write_artifact(payload, tmp_path)
        rc, stderr, sql = _run_dry(art, tmp_path)

        assert rc == 0, f"ingestor exited {rc}. stderr:\n{stderr}"
        assert sql is not None

        # The count-aware guard should block mark-missing for colliers.
        assert not _mark_missing_present_for_slug(sql, "colliers"), (
            "mark-missing UPDATE for 'colliers' was emitted even though "
            "colliers-main had listingsCollected=0. Count-aware guard did not fire.\n"
            f"SQL snippet:\n{sql[:3000]}"
        )

    def test_stderr_notes_zero_count_key(self, tmp_path):
        """Stderr should mention the skip due to folded source coverage."""
        sources = [
            _source_entry("colliers", count=_LISTING_COUNT),
            _source_entry("colliers-main", count=0),
        ]
        listings = [_listing("colliers", i) for i in range(_LISTING_COUNT)]
        payload = _artifact(sources, listings)
        art = _write_artifact(payload, tmp_path)
        _, stderr, _ = _run_dry(art, tmp_path)

        assert "folded source coverage incomplete" in stderr or \
               "mark-missing skipped" in stderr, (
            f"Expected guard note in stderr. Got:\n{stderr}"
        )

    def test_no_deleted_at_when_folded_source_zero_count(self, tmp_path):
        """No deleted_at assignment should appear when the guard blocks."""
        sources = [
            _source_entry("colliers", count=_LISTING_COUNT),
            _source_entry("colliers-main", count=0),
        ]
        listings = [_listing("colliers", i) for i in range(_LISTING_COUNT)]
        payload = _artifact(sources, listings)
        art = _write_artifact(payload, tmp_path)
        _, _, sql = _run_dry(art, tmp_path)

        assert sql is not None
        # The _retired table should not appear when blocked.
        assert "CREATE TEMP TABLE _retired" not in sql, (
            "SQL contained _retired table even though colliers-main had zero count.\n"
            f"SQL snippet:\n{sql[:3000]}"
        )


# ---------------------------------------------------------------------------
# CASE B: control -- both colliers and colliers-main have nonzero counts
# ---------------------------------------------------------------------------


class TestFoldedCoveragePassesWithNonzeroCounts:
    """
    Both colliers and colliers-main have nonzero listingsCollected AND listings.
    Mark-missing IS expected for the colliers brokerage.
    """

    def test_mark_missing_fires_when_both_folded_sources_nonzero(self, tmp_path):
        sources = [
            _source_entry("colliers", count=_LISTING_COUNT),
            _source_entry("colliers-main", count=_LISTING_COUNT),
        ]
        listings = (
            [_listing("colliers", i) for i in range(_LISTING_COUNT)]
            + [_listing("colliers-main", i) for i in range(_LISTING_COUNT)]
        )
        payload = _artifact(sources, listings)
        art = _write_artifact(payload, tmp_path)
        rc, stderr, sql = _run_dry(art, tmp_path)

        assert rc == 0, f"ingestor exited {rc}. stderr:\n{stderr}"
        assert sql is not None

        assert "CREATE TEMP TABLE _retired" in sql or "deleted_at = now()" in sql, (
            "mark-missing SQL was NOT emitted even though both colliers and "
            "colliers-main were present with nonzero counts.\n"
            f"SQL snippet:\n{sql[:3000]}"
        )


# ---------------------------------------------------------------------------
# CASE C: singleton svn unaffected by count-aware gate
# ---------------------------------------------------------------------------


class TestSingletonUnaffectedByCountAwareGate:
    """
    svn is a singleton (SOURCE_KEYS_BY_SLUG['svn'] == {'svn'}).
    The count-aware gate only applies to multi-key brokerages.
    Singleton mark-missing must fire normally.
    """

    def test_singleton_svn_fires_normally(self, tmp_path):
        sources = [_source_entry("svn", count=_LISTING_COUNT)]
        listings = [_svn_listing(i) for i in range(_LISTING_COUNT)]
        payload = _artifact(sources, listings)
        art = _write_artifact(payload, tmp_path)
        rc, stderr, sql = _run_dry(art, tmp_path)

        assert rc == 0, f"ingestor exited {rc}. stderr:\n{stderr}"
        assert sql is not None

        assert "CREATE TEMP TABLE _retired" in sql or "deleted_at = now()" in sql, (
            "mark-missing SQL was NOT emitted for singleton svn. "
            "Count-aware gate must not block singletons.\n"
            f"SQL snippet:\n{sql[:3000]}"
        )

    def test_singleton_no_folded_coverage_note_in_stderr(self, tmp_path):
        sources = [_source_entry("svn", count=_LISTING_COUNT)]
        listings = [_svn_listing(i) for i in range(_LISTING_COUNT)]
        payload = _artifact(sources, listings)
        art = _write_artifact(payload, tmp_path)
        _, stderr, _ = _run_dry(art, tmp_path)

        assert "folded source coverage incomplete" not in stderr, (
            f"Unexpected folded coverage note for singleton svn. stderr:\n{stderr}"
        )


# ---------------------------------------------------------------------------
# L4a: flip-breaker metric widened to any non-active reclassification
# ---------------------------------------------------------------------------


def test_flip_breaker_metric_counts_any_non_active_reclassification():
    """
    L4a: the leaving_active FILTER in the status-flip pre-flight DO block must
    count ANY reclassification to a non-active status (e.g. under_contract ->
    sold), not only departures from 'active'.

    Assert against build_sql([], [], scraped_at, set()) (no DB needed).
    """
    from datetime import datetime, timezone
    from cre_ingest import build_sql

    scraped_at = datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc).isoformat()
    sql = build_sql([], [], scraped_at, set())

    # The widened predicate must be present.
    assert "AND s.status <> 'active'" in sql, (
        "Expected widened leaving_active predicate 'AND s.status <> 'active'' "
        f"not found in generated SQL.\nRelevant SQL:\n{sql[sql.find('leaving_active')-200:sql.find('leaving_active')+500]}"
    )

    # The leaving_active alias must still be present (trip condition references it).
    assert "leaving_active" in sql, (
        "leaving_active alias missing from generated SQL."
    )

    # The OLD narrow form (only counting t.status = 'active') must be gone.
    old_narrow_form = "WHERE s.status IS NOT NULL AND t.status = 'active'\n               ) AS leaving_active"
    assert old_narrow_form not in sql, (
        "Old narrow leaving_active filter still present in generated SQL. "
        "L4a widened predicate was not applied."
    )
