"""
test_ingest_mark_missing.py

Locks down the ingest-layer mark-missing folded-coverage guard in cre_ingest.py.

This guard is the last line of defense before the ingestor emits a destructive
  UPDATE ... SET deleted_at = now(), status = 'inactive'
for an entire brokerage.  It must refuse to mark-missing a multi-source
brokerage when one of its sub-sources is absent from the artifact's sources[].

Distinct from test_gate.py / test_cre_gate.py, which cover the cre_gate.py
code path (coverage baseline; `mark_missing_safe` rollup).  This file tests
the analogous guard that lives inside main() of cre_ingest.py, gated by
`has_complete_folded_coverage`.

All cases run via subprocess --dry-run --keep-artifacts so no DB connection
is required and no environment file is needed.  The generated ingest.sql is
inspected for the presence or absence of `deleted_at = now()`.

SOURCE_TO_BROKERAGE pairings verified against cre_ingest.py:
  cbre + cbre-dealflow  -> brokerage slug "cbre"   (prefix "dealflow:")
  jll  + jll-investor   -> brokerage slug "jll"    (prefix "investor:")
  colliers + colliers-main -> brokerage slug "colliers" (prefix "main:")
  svn                   -> brokerage slug "svn"    (singleton, no sibling)

Tested brokerage: "cbre" (cbre + cbre-dealflow).  All three multi-source
brokerages share identical guard logic; cbre is chosen because its external_id
prefix ("dealflow:") makes the two sources visually distinct in assertions.
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

# A synthetic finishedAt timestamp used across all artifacts.
_FINISHED_AT = "2026-06-14T00:00:00.000Z"
_STARTED_AT = "2026-06-14T00:00:00.000Z"

# Enough rows above --mark-missing-floor 1.
_LISTING_COUNT = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source_entry(source_key, count=_LISTING_COUNT, error=None, truncated=False):
    """Build one element for the artifact sources[] array."""
    entry = {
        "sourceKey": source_key,
        "transaction": "sale",
        "listingsCollected": count,
    }
    if error:
        entry["error"] = error
    if truncated:
        entry["truncated"] = True
    return entry


def _listing(source_key, idx=0):
    """
    Build a minimal listing dict that passes to_row's source_url guard.
    cre_ingest.py requires url.startswith("http") and a recognized sourceKey.
    The id field provides a stable external_id so rows deduplicate cleanly.
    """
    return {
        "sourceKey": source_key,
        "url": f"https://www.cbre.com/p/test-{source_key}-{idx:04d}",
        "id": f"test-{source_key}-{idx:04d}",
        "transactionMode": "sale",
    }


def _artifact(source_keys_for_entries, source_keys_for_listings):
    """
    Build a minimal artifact dict.

    source_keys_for_entries: list of source keys to include in sources[].
    source_keys_for_listings: list of (source_key, count) tuples for listings[].
    """
    sources = [_source_entry(sk) for sk in source_keys_for_entries]
    listings = []
    for sk, count in source_keys_for_listings:
        for i in range(count):
            listings.append(_listing(sk, i))
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
    """
    Run cre_ingest.py --dry-run --mark-missing --mark-missing-floor 1
    with --keep-artifacts pointing at a sub-dir of tmp_path.

    Returns (returncode, stderr_text, sql_text).
    sql_text is None when the SQL file is absent.
    """
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
    """
    Return True when the SQL contains the mark-missing UPDATE scoped to slug.

    The ingestor writes:
      SET deleted_at = now(), status = 'inactive', updated_at = now()
      ...
      AND b.slug IN ('slug')
    We check for the slug appearing inside a slug IN (...) block that is
    preceded by `deleted_at = now()` to avoid false positives from the
    summary SELECT that appears unconditionally in the SQL.
    """
    if sql_text is None:
        return False
    # Locate the mark-missing UPDATE block.
    block_start = sql_text.find("deleted_at = now()")
    if block_start == -1:
        return False
    # Within that block find the slug IN clause.
    block = sql_text[block_start: block_start + 2000]
    return f"'{slug}'" in block and "b.slug IN" in block


# ---------------------------------------------------------------------------
# Case 1: guard HOLDS when folded sub-source is absent
# ---------------------------------------------------------------------------


class TestMarkMissingHeldWhenFoldedSubsourceAbsent:
    """
    Artifact contains only `cbre` (primary source) but NOT `cbre-dealflow`.
    The ingestor knows the `cbre` brokerage slug has two source keys:
      cbre  and  cbre-dealflow
    Because cbre-dealflow is absent from sources[], has_complete_folded_coverage
    is False, and the mark-missing UPDATE must NOT be emitted for the cbre slug.
    """

    def test_mark_missing_held_when_folded_subsource_absent(self, tmp_path):
        payload = _artifact(
            source_keys_for_entries=["cbre"],             # only cbre, no cbre-dealflow
            source_keys_for_listings=[("cbre", _LISTING_COUNT)],
        )
        art = _write_artifact(payload, tmp_path)
        rc, stderr, sql = _run_dry(art, tmp_path)

        assert rc == 0, f"ingestor exited {rc}. stderr:\n{stderr}"
        assert sql is not None, "SQL file was not written"

        # The guard should have blocked mark-missing for the cbre brokerage.
        assert not _mark_missing_present_for_slug(sql, "cbre"), (
            "mark-missing UPDATE for 'cbre' was emitted even though "
            "cbre-dealflow was absent from sources[]. Guard did not hold.\n"
            f"Relevant SQL:\n{sql[:3000]}"
        )

        # Verify the guard note appears in stderr (confirms the guard path fired).
        assert "folded source coverage incomplete" in stderr or \
               "mark-missing skipped" in stderr, (
            f"Expected guard note in stderr. Got:\n{stderr}"
        )

    def test_no_mark_missing_sql_block_at_all_when_all_brokerages_blocked(self, tmp_path):
        """
        When the sole brokerage in the artifact is blocked by the guard, the
        SQL file must contain no `deleted_at = now()` statement at all.
        """
        payload = _artifact(
            source_keys_for_entries=["cbre"],
            source_keys_for_listings=[("cbre", _LISTING_COUNT)],
        )
        art = _write_artifact(payload, tmp_path)
        _, _, sql = _run_dry(art, tmp_path)

        assert sql is not None
        assert "deleted_at = now()" not in sql, (
            "SQL contained `deleted_at = now()` even though cbre-dealflow was absent.\n"
            f"SQL snippet:\n{sql[:3000]}"
        )


# ---------------------------------------------------------------------------
# Case 2: guard PASSES when both folded sources are present
# ---------------------------------------------------------------------------


class TestMarkMissingPassesWhenBothFoldedSourcesPresent:
    """
    Artifact contains BOTH `cbre` and `cbre-dealflow` in sources[].
    has_complete_folded_coverage is True, no errors, above floor.
    The mark-missing UPDATE IS expected for the cbre brokerage.
    """

    def test_mark_missing_fires_when_complete_folded_coverage(self, tmp_path):
        payload = _artifact(
            source_keys_for_entries=["cbre", "cbre-dealflow"],
            source_keys_for_listings=[
                ("cbre", _LISTING_COUNT),
                ("cbre-dealflow", _LISTING_COUNT),
            ],
        )
        art = _write_artifact(payload, tmp_path)
        rc, stderr, sql = _run_dry(art, tmp_path)

        assert rc == 0, f"ingestor exited {rc}. stderr:\n{stderr}"
        assert sql is not None

        assert _mark_missing_present_for_slug(sql, "cbre"), (
            "mark-missing UPDATE for 'cbre' was NOT emitted even though both "
            "cbre and cbre-dealflow were present in sources[]. Guard should pass.\n"
            f"Relevant SQL:\n{sql[:3000]}"
        )

    def test_stderr_reports_mark_missing_active_for_cbre(self, tmp_path):
        """Stderr must confirm mark-missing is active for the cbre slug."""
        payload = _artifact(
            source_keys_for_entries=["cbre", "cbre-dealflow"],
            source_keys_for_listings=[
                ("cbre", _LISTING_COUNT),
                ("cbre-dealflow", _LISTING_COUNT),
            ],
        )
        art = _write_artifact(payload, tmp_path)
        _, stderr, _ = _run_dry(art, tmp_path)

        assert "mark-missing active for" in stderr and "cbre" in stderr, (
            f"Expected mark-missing confirmation in stderr. Got:\n{stderr}"
        )


# ---------------------------------------------------------------------------
# Case 3: singleton brokerage (no folded sub-source) fires normally
# ---------------------------------------------------------------------------


class TestMarkMissingFiresForSingletonBrokerage:
    """
    `svn` is a singleton: SOURCE_KEYS_BY_SLUG['svn'] == {'svn'}.
    has_complete_folded_coverage is True by definition (len(known_keys) == 1).
    Mark-missing must fire normally for svn when error-free and above floor.

    Note: svn uses Buildout URL-based external_ids. Providing a ?propertyId=
    query param in the URL ensures to_row assigns a stable id rather than the
    sha1 fallback, but either form is acceptable; what matters is that the
    row count satisfies --mark-missing-floor 1 and the brokerage slug 'svn'
    appears in the mark-missing UPDATE.
    """

    def test_mark_missing_fires_for_svn_singleton(self, tmp_path):
        listings = []
        for i in range(_LISTING_COUNT):
            listings.append({
                "sourceKey": "svn",
                "url": f"https://www.svn.com/property?propertyId=svn-{i:04d}-sale",
                "id": f"svn-{i:04d}",
                "transactionMode": "sale",
            })
        payload = {
            "runMeta": {
                "startedAt": _STARTED_AT,
                "finishedAt": _FINISHED_AT,
            },
            "brokers": [],
            "sources": [_source_entry("svn")],
            "listings": listings,
        }
        art = _write_artifact(payload, tmp_path)
        rc, stderr, sql = _run_dry(art, tmp_path)

        assert rc == 0, f"ingestor exited {rc}. stderr:\n{stderr}"
        assert sql is not None

        assert _mark_missing_present_for_slug(sql, "svn"), (
            "mark-missing UPDATE for singleton 'svn' was NOT emitted. "
            "Singleton brokerages must always pass the folded-coverage guard.\n"
            f"Relevant SQL:\n{sql[:3000]}"
        )

    def test_svn_no_folded_coverage_note_in_stderr(self, tmp_path):
        """Stderr must NOT contain the folded coverage note for a singleton."""
        listings = []
        for i in range(_LISTING_COUNT):
            listings.append({
                "sourceKey": "svn",
                "url": f"https://www.svn.com/property?propertyId=svn-{i:04d}-sale",
                "id": f"svn-{i:04d}",
                "transactionMode": "sale",
            })
        payload = {
            "runMeta": {
                "startedAt": _STARTED_AT,
                "finishedAt": _FINISHED_AT,
            },
            "brokers": [],
            "sources": [_source_entry("svn")],
            "listings": listings,
        }
        art = _write_artifact(payload, tmp_path)
        _, stderr, _ = _run_dry(art, tmp_path)

        assert "folded source coverage incomplete" not in stderr, (
            f"Unexpected folded coverage note for singleton svn. stderr:\n{stderr}"
        )


# ---------------------------------------------------------------------------
# Case 4: error on one sub-source also blocks mark-missing (belt-and-suspenders)
# ---------------------------------------------------------------------------


class TestMarkMissingHeldWhenSubsourceHasError:
    """
    Both cbre and cbre-dealflow appear in sources[], but cbre-dealflow has an
    error.  The error guard (st['errors'] > 0) fires before the folded-coverage
    guard, so mark-missing must still be blocked.
    """

    def test_mark_missing_held_when_subsource_errors(self, tmp_path):
        sources = [
            _source_entry("cbre"),
            _source_entry("cbre-dealflow", error="connection timeout"),
        ]
        listings = []
        for i in range(_LISTING_COUNT):
            listings.append(_listing("cbre", i))
        payload = {
            "runMeta": {
                "startedAt": _STARTED_AT,
                "finishedAt": _FINISHED_AT,
            },
            "brokers": [],
            "sources": sources,
            "listings": listings,
        }
        art = _write_artifact(payload, tmp_path)
        rc, stderr, sql = _run_dry(art, tmp_path)

        assert rc == 0, f"ingestor exited {rc}. stderr:\n{stderr}"
        assert sql is not None

        assert not _mark_missing_present_for_slug(sql, "cbre"), (
            "mark-missing UPDATE for 'cbre' was emitted even though "
            "cbre-dealflow reported an error. Error guard did not hold.\n"
            f"Relevant SQL:\n{sql[:3000]}"
        )


# ---------------------------------------------------------------------------
# Case 5: truncated source pass also blocks mark-missing
# ---------------------------------------------------------------------------


class TestMarkMissingHeldWhenSourceIsTruncated:
    """
    A source can collect rows and still know it only saw partial coverage. That
    must block mark-missing, including for singleton brokerages, because missing
    rows are ambiguous on a truncated run.
    """

    def test_mark_missing_held_when_singleton_source_truncated(self, tmp_path):
        listings = []
        for i in range(_LISTING_COUNT):
            listings.append({
                "sourceKey": "svn",
                "url": f"https://www.svn.com/property?propertyId=svn-truncated-{i:04d}-sale",
                "id": f"svn-truncated-{i:04d}",
                "transactionMode": "sale",
            })
        payload = {
            "runMeta": {
                "startedAt": _STARTED_AT,
                "finishedAt": _FINISHED_AT,
            },
            "brokers": [],
            "sources": [_source_entry("svn", truncated=True)],
            "listings": listings,
        }
        art = _write_artifact(payload, tmp_path)
        rc, stderr, sql = _run_dry(art, tmp_path)

        assert rc == 0, f"ingestor exited {rc}. stderr:\n{stderr}"
        assert sql is not None

        assert not _mark_missing_present_for_slug(sql, "svn"), (
            "mark-missing UPDATE for singleton 'svn' was emitted even though "
            "the source reported truncated=true.\n"
            f"Relevant SQL:\n{sql[:3000]}"
        )
        assert "mark-missing skipped" in stderr and "svn" in stderr, (
            f"Expected skipped brokerage note in stderr. Got:\n{stderr}"
        )
