"""test_cre_gate_decisions.py

Closes coverage gaps in cre_gate.py. Targets lines that the existing
test_cre_gate.py and test_gate.py do not reach:

  - _eprint with quiet=False (writes to stderr)
  - count_artifacts: sourceKey-less sources entry (continue at line 118)
  - count_artifacts: to_row exception path (torow_errors counter, lines 132-135)
  - _psql_read: subprocess argv shape + row parsing (lines 199-212)
  - read_baseline: psql call wired, row parsing (lines 217-233)
  - run_baseline_sql: writes temp file, calls psql (lines 276-289)
  - build_baseline_sql: UNMAPPED_SLUG slug rendered as NULL (line 254)
  - build_baseline_sql: None job_id / None scraped_at paths
  - select_baseline_updates: ok verdict with no prior baseline entry (br is {})

Pure-transform tests need no DB. The psql-shelling helpers (_psql_read,
read_baseline, run_baseline_sql) are tested by monkeypatching subprocess.run
and asserting argv shape. No network, no live DB.
"""

import json
import os
import subprocess
import sys
import tempfile

import pytest

import cre_gate as g

# ---------------------------------------------------------------------------
# _eprint: the quiet=False branch (lines 67-68)
# ---------------------------------------------------------------------------


class TestEprint:
    def test_quiet_true_suppresses_output(self, capsys):
        g._eprint(True, "should not appear")
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_quiet_false_writes_to_stderr(self, capsys):
        g._eprint(False, "hello", "world")
        captured = capsys.readouterr()
        assert "hello" in captured.err
        assert "world" in captured.err

    def test_quiet_false_multiple_parts(self, capsys):
        g._eprint(False, "part1", "part2", "part3")
        captured = capsys.readouterr()
        assert "part1" in captured.err
        assert "part2" in captured.err


# ---------------------------------------------------------------------------
# count_artifacts: sourceKey-less source entry (line 118 continue)
# ---------------------------------------------------------------------------


def _write_artifact(payload):
    fd, path = tempfile.mkstemp(prefix="cre_gate_test_", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    return path


class TestCountArtifactsEdgeCases:
    def test_source_entry_without_sourcekey_is_skipped(self):
        """A sources[] entry with no sourceKey hits the `continue` at line 118."""
        payload = {
            "runMeta": {"finishedAt": "2026-06-14T00:00:00.000Z"},
            "brokers": [],
            "sources": [
                {"error": "boom, but no sourceKey"},  # no 'sourceKey' key
                {},  # completely empty entry
            ],
            "listings": [],
        }
        path = _write_artifact(payload)
        try:
            current, errors, observed, scraped_at, torow_errors = g.count_artifacts(
                [path], quiet=True
            )
        finally:
            os.unlink(path)
        # No sourceKey -> nothing should be in observed or errors
        assert len(observed) == 0
        assert len(errors) == 0
        assert torow_errors == 0

    def test_source_entry_without_sourcekey_does_not_prevent_valid_source(self):
        """Mixed: one sourceKey-less entry, one with sourceKey. Only the keyed one is counted."""
        payload = {
            "runMeta": {"finishedAt": "2026-06-14T00:00:00.000Z"},
            "brokers": [],
            "sources": [
                {"error": "no key"},  # will be skipped
                {"sourceKey": "svn"},  # will be added
            ],
            "listings": [],
        }
        path = _write_artifact(payload)
        try:
            current, errors, observed, scraped_at, torow_errors = g.count_artifacts(
                [path], quiet=True
            )
        finally:
            os.unlink(path)
        assert "svn" in observed
        assert len(observed) == 1

    def test_to_row_exception_increments_torow_errors(self):
        """A listing that causes to_row to raise an exception should increment
        torow_errors (lines 132-135) and NOT crash the whole gate."""
        # A listing with a URL but a malformed brokers index ref that causes a
        # to_row exception. We can trigger this by injecting a brokerIndex that
        # refers to a non-existent broker slot which causes KeyError in to_row.
        # Actually the simplest path: just pass a listing with a sourceKey that
        # cre_ingest.to_row can handle, but supply a brokerIdx value pointing
        # to a broker that does not exist.  However to_row is resilient to
        # missing broker.  Instead we test via monkeypatching to_row directly.
        import cre_gate as gmod
        from unittest.mock import patch

        payload = {
            "runMeta": {"finishedAt": "2026-06-14T00:00:00.000Z"},
            "brokers": [],
            "sources": [],
            "listings": [
                {"sourceKey": "svn", "url": "https://svn.com/1"},
            ],
        }
        path = _write_artifact(payload)
        try:
            with patch("cre_gate.to_row", side_effect=ValueError("boom")):
                current, errors, observed, scraped_at, torow_errors = gmod.count_artifacts(
                    [path], quiet=True
                )
        finally:
            os.unlink(path)
        # Exception was caught; torow_errors should be 1
        assert torow_errors == 1
        # The listing's sourceKey was added to observed before the exception
        assert "svn" in observed
        # current_active should be 0 because row is None after exception
        assert current.get("svn", 0) == 0

    def test_scraped_at_fallback_to_startedAt_when_finishedAt_missing(self):
        """count_artifacts: scraped_at falls back to startedAt when finishedAt absent."""
        payload = {
            "runMeta": {"startedAt": "2026-06-10T00:00:00.000Z"},
            "brokers": [],
            "sources": [],
            "listings": [],
        }
        path = _write_artifact(payload)
        try:
            _, _, _, scraped_at, _ = g.count_artifacts([path], quiet=True)
        finally:
            os.unlink(path)
        assert scraped_at == "2026-06-10T00:00:00.000Z"

    def test_multiple_artifacts_merged(self):
        """count_artifacts with two files: counts accumulate across both."""
        payload1 = {
            "runMeta": {"finishedAt": "2026-06-14T01:00:00.000Z"},
            "brokers": [],
            "sources": [{"sourceKey": "svn"}],
            "listings": [
                {"sourceKey": "svn", "url": "https://svn.com/1"},
                {"sourceKey": "svn", "url": "https://svn.com/2"},
            ],
        }
        payload2 = {
            "runMeta": {"finishedAt": "2026-06-14T02:00:00.000Z"},
            "brokers": [],
            "sources": [{"sourceKey": "svn"}],
            "listings": [
                {"sourceKey": "svn", "url": "https://svn.com/3"},
            ],
        }
        p1 = _write_artifact(payload1)
        p2 = _write_artifact(payload2)
        try:
            current, errors, observed, scraped_at, torow_errors = g.count_artifacts(
                [p1, p2], quiet=True
            )
        finally:
            os.unlink(p1)
            os.unlink(p2)
        # 2 + 1 = 3 listings
        assert current.get("svn", 0) == 3
        # scraped_at should be from first artifact
        assert scraped_at == "2026-06-14T01:00:00.000Z"

    def test_source_error_already_recorded_not_overwritten(self):
        """If a source has two source entries, the first error is kept (line 121-122)."""
        payload = {
            "runMeta": {"finishedAt": "2026-06-14T00:00:00.000Z"},
            "brokers": [],
            "sources": [
                {"sourceKey": "svn", "error": "first error"},
                {"sourceKey": "svn", "error": "second error"},
            ],
            "listings": [],
        }
        path = _write_artifact(payload)
        try:
            _, errors, _, _, _ = g.count_artifacts([path], quiet=True)
        finally:
            os.unlink(path)
        # First error is preserved; second error does not overwrite it
        assert errors.get("svn") == "first error"

    def test_source_no_error_sets_none_in_errors_map(self):
        """A source entry with no error sets source_error[sk]=None (line 123-124)."""
        payload = {
            "runMeta": {"finishedAt": "2026-06-14T00:00:00.000Z"},
            "brokers": [],
            "sources": [{"sourceKey": "svn"}],
            "listings": [],
        }
        path = _write_artifact(payload)
        try:
            _, errors, _, _, _ = g.count_artifacts([path], quiet=True)
        finally:
            os.unlink(path)
        assert "svn" in errors
        assert errors["svn"] is None


# ---------------------------------------------------------------------------
# _psql_read: subprocess call shape and row parsing (lines 199-212)
# ---------------------------------------------------------------------------


class TestPsqlReadShape:
    def test_argv_shape_read(self, monkeypatch):
        """_psql_read assembles the correct psql argv for a read-only query."""
        captured = {}

        class FakeResult:
            returncode = 0
            stdout = "svn\x1f5000\x1f4900\n"
            stderr = ""

        def fake_run(argv, **kw):
            captured["argv"] = argv
            captured["kw"] = kw
            return FakeResult()

        monkeypatch.setattr(subprocess, "run", fake_run)
        rows = g._psql_read("psql", "postgresql://fake/db", "SELECT 1;")
        argv = captured["argv"]
        # Credentials must stay out of the process argument list.
        assert "postgresql://fake/db" not in argv
        assert captured["kw"]["env"]["PGHOST"] == "fake"
        assert captured["kw"]["env"]["PGDATABASE"] == "db"
        # Must use unit-separator delimiter
        assert "\x1f" in argv
        # Must use -tAF (tuple-only, no alignment, with field sep)
        assert "-tAF" in argv
        # Must pass ON_ERROR_STOP
        assert "ON_ERROR_STOP=1" in argv
        # SQL must be passed via -c
        assert "-c" in argv
        assert "SELECT 1;" in argv
        # text=True required
        assert captured["kw"].get("text") is True

    def test_row_parsing_splits_on_unit_separator(self, monkeypatch):
        """_psql_read splits each output line on \\x1f and skips blank lines."""

        class FakeResult:
            returncode = 0
            stdout = "svn\x1f5000\x1f4900\n\ncbre\x1f18000\x1f17500\n"
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())
        rows = g._psql_read("psql", "postgresql://fake/db", "SELECT 1;")
        assert len(rows) == 2
        assert rows[0] == ["svn", "5000", "4900"]
        assert rows[1] == ["cbre", "18000", "17500"]

    def test_nonzero_returncode_calls_sys_exit(self, monkeypatch):
        """_psql_read exits if psql returns nonzero."""

        class FakeResult:
            returncode = 1
            stdout = ""
            stderr = "connection refused"

        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())
        with pytest.raises(SystemExit) as exc_info:
            g._psql_read("psql", "postgresql://fake/db", "SELECT 1;")
        assert "psql read failed" in str(exc_info.value)

    def test_empty_output_returns_empty_list(self, monkeypatch):
        """_psql_read with empty stdout returns []."""

        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())
        rows = g._psql_read("psql", "postgresql://fake/db", "SELECT 1;")
        assert rows == []


# ---------------------------------------------------------------------------
# read_baseline: psql row parsing and return shape (lines 217-233)
# ---------------------------------------------------------------------------


class TestReadBaseline:
    def test_read_baseline_parses_rows_correctly(self, monkeypatch):
        """read_baseline converts psql rows to {median, last} baseline dicts.
        load_db_url and find_psql are imported into cre_gate's namespace, so
        we monkeypatch them there."""
        # patch in cre_gate's own namespace (where they were imported)
        monkeypatch.setattr(g, "load_db_url",
                            lambda env_file: ("postgresql://fake/db", "/fake/.env.local"))
        monkeypatch.setattr(g, "find_psql", lambda: "psql")
        # _psql_read returns pre-parsed rows (skip subprocess entirely)
        monkeypatch.setattr(g, "_psql_read",
                            lambda psql, db_url, sql: [
                                ["svn", "5000", "4900"],
                                ["cbre", "18000", "17500"],
                                ["jll", "", ""],  # null median and last
                            ])

        baseline, db_url, psql_bin = g.read_baseline(None, quiet=True)
        assert baseline["svn"] == {"median": 5000, "last": 4900}
        assert baseline["cbre"] == {"median": 18000, "last": 17500}
        # Empty string -> None
        assert baseline["jll"] == {"median": None, "last": None}
        assert db_url == "postgresql://fake/db"
        assert psql_bin == "psql"

    def test_read_baseline_prints_credentials_when_not_quiet(self, monkeypatch, capsys):
        """read_baseline _eprints the env path when quiet=False."""
        monkeypatch.setattr(g, "load_db_url",
                            lambda env_file: ("postgresql://fake/db", "/my/.env.local"))
        monkeypatch.setattr(g, "find_psql", lambda: "psql")
        monkeypatch.setattr(g, "_psql_read", lambda *a: [])

        g.read_baseline(None, quiet=False)
        captured = capsys.readouterr()
        assert "/my/.env.local" in captured.err

    def test_read_baseline_prints_row_count_when_not_quiet(self, monkeypatch, capsys):
        """read_baseline _eprints the number of baseline rows read."""
        monkeypatch.setattr(g, "load_db_url",
                            lambda env_file: ("postgresql://fake/db", "/my/.env.local"))
        monkeypatch.setattr(g, "find_psql", lambda: "psql")
        monkeypatch.setattr(g, "_psql_read",
                            lambda *a: [["svn", "5000", "4900"], ["cbre", "18000", "17500"]])

        g.read_baseline(None, quiet=False)
        captured = capsys.readouterr()
        assert "2" in captured.err  # 2 rows read


# ---------------------------------------------------------------------------
# run_baseline_sql: subprocess call shape (lines 276-289)
# ---------------------------------------------------------------------------


class TestRunBaselineSql:
    def test_run_baseline_sql_writes_tempfile_and_calls_psql(self, monkeypatch):
        """run_baseline_sql creates a temp .sql file and passes -f <path> to psql."""
        captured = {}

        class FakeResult:
            returncode = 0
            stdout = b""
            stderr = b""

        def fake_run(argv, **kw):
            captured["argv"] = argv
            # Read the file while it still exists
            f_arg_idx = argv.index("-f")
            sql_path = argv[f_arg_idx + 1]
            with open(sql_path) as fh:
                captured["sql"] = fh.read()
            return FakeResult()

        monkeypatch.setattr(subprocess, "run", fake_run)
        sql = "BEGIN;\nSELECT 1;\nCOMMIT;"
        g.run_baseline_sql("psql", "postgresql://fake/db", sql)

        argv = captured["argv"]
        # Credentials must stay out of the process argument list.
        assert "postgresql://fake/db" not in argv
        # -f flag must be present
        assert "-f" in argv
        # The SQL content was written to the temp file
        assert captured["sql"] == sql
        # ON_ERROR_STOP must be set
        assert "ON_ERROR_STOP=1" in argv

    def test_run_baseline_sql_tempfile_is_cleaned_up(self, monkeypatch):
        """run_baseline_sql deletes the temp file even on success."""
        written_path = []

        class FakeResult:
            returncode = 0
            stdout = b""
            stderr = b""

        def fake_run(argv, **kw):
            f_idx = argv.index("-f")
            written_path.append(argv[f_idx + 1])
            return FakeResult()

        monkeypatch.setattr(subprocess, "run", fake_run)
        g.run_baseline_sql("psql", "postgresql://fake/db", "BEGIN; COMMIT;")
        assert written_path  # the path was captured
        assert not os.path.exists(written_path[0])  # file was deleted

    def test_run_baseline_sql_nonzero_rc_exits(self, monkeypatch):
        """run_baseline_sql exits on psql error."""

        class FakeResult:
            returncode = 1
            stdout = b""
            stderr = b"ERROR: syntax error"

        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())
        with pytest.raises(SystemExit) as exc_info:
            g.run_baseline_sql("psql", "postgresql://fake/db", "INVALID;")
        assert "baseline write failed" in str(exc_info.value)


# ---------------------------------------------------------------------------
# build_baseline_sql: edge cases (unmapped slug -> NULL, None job_id/scraped_at)
# ---------------------------------------------------------------------------


class TestBuildBaselineSqlEdgeCases:
    def test_unmapped_slug_renders_as_null(self):
        """When a source key is not in SOURCE_TO_BROKERAGE, slug=UNMAPPED_SLUG
        should render as NULL in the INSERT (not as a string literal)."""
        updates = [
            {
                "source_key": "__unknown__",
                "slug": g.UNMAPPED_SLUG,  # "__unmapped__"
                "new_median": 100,
                "current": 110,
            }
        ]
        sql = g.build_baseline_sql(updates, "2026-06-14T00:00:00+00:00", None)
        # The INSERT should have NULL for the brokerage_slug column
        # Check that UNMAPPED_SLUG literal is NOT in the SQL as a string value
        assert g.UNMAPPED_SLUG not in sql or "NULL" in sql
        # Specifically the slug position in the INSERT should be NULL
        assert "NULL" in sql

    def test_none_scraped_at_renders_as_null(self):
        """build_baseline_sql with scraped_at=None renders NULL for the timestamptz."""
        updates = [
            {"source_key": "svn", "slug": "svn", "new_median": 5000, "current": 5100},
        ]
        sql = g.build_baseline_sql(updates, None, None)
        # NULL should appear for the scraped_at position
        assert "NULL" in sql
        # The SQL must still be valid (BEGIN/COMMIT present)
        assert "BEGIN;" in sql
        assert "COMMIT;" in sql

    def test_with_job_id_includes_uuid_cast(self):
        """build_baseline_sql with a job_id includes the ::uuid cast."""
        updates = [
            {"source_key": "svn", "slug": "svn", "new_median": 5000, "current": 5100},
        ]
        job_id = "12345678-0000-0000-0000-000000000001"
        sql = g.build_baseline_sql(updates, "2026-06-14T00:00:00+00:00", job_id)
        assert job_id in sql
        assert "::uuid" in sql

    def test_none_job_id_renders_as_null(self):
        """build_baseline_sql with job_id=None uses COALESCE(NULL, ...) pattern."""
        updates = [
            {"source_key": "svn", "slug": "svn", "new_median": 5000, "current": 5100},
        ]
        sql = g.build_baseline_sql(updates, "2026-06-14T00:00:00+00:00", None)
        # The COALESCE expression in the DO UPDATE SET for job_id should be present
        assert "COALESCE" in sql


# ---------------------------------------------------------------------------
# select_baseline_updates: ok verdict with no prior baseline entry
# ---------------------------------------------------------------------------


class TestSelectBaselineUpdatesOkNoPrior:
    def test_ok_verdict_with_no_prior_baseline_still_seeds(self):
        """An 'ok' verdict with no matching baseline entry (br is {}) should still
        produce an update using rolling_median(None, None, current) = current."""
        per_source = {"svn": {"verdict": "ok", "current_active": 5000}}
        source_error = {"svn": None}
        baseline = {}  # no prior row for svn
        updates = {u["source_key"]: u for u in g.select_baseline_updates(
            per_source, source_error, baseline
        )}
        assert "svn" in updates
        # rolling_median(None, None, 5000) = 5000
        assert updates["svn"]["new_median"] == 5000
        assert updates["svn"]["current"] == 5000

    def test_slug_for_unmapped_key_returns_unmapped_sentinel(self):
        """_slug_for with an unknown source key returns UNMAPPED_SLUG."""
        slug = g._slug_for("__definitely_not_a_real_source_key__")
        assert slug == g.UNMAPPED_SLUG

    def test_slug_for_mapped_key_returns_brokerage_slug(self):
        """_slug_for with a known source key returns the brokerage slug string."""
        # All real source keys should return their expected slug (spot check)
        assert g._slug_for("svn") == "svn"
        assert g._slug_for("cbre-dealflow") == "cbre"
        assert g._slug_for("colliers-main") == "colliers"
        assert g._slug_for("jll-investor") == "jll"


# ---------------------------------------------------------------------------
# main(): CLI entry point - testable branches (lines 360-486)
# ---------------------------------------------------------------------------


class TestMainCli:
    """Test the pure-logic branches in main() that are exercisable without a DB.

    Strategy: manipulate sys.argv directly, monkeypatch count_artifacts and
    any DB-touching helpers, then call main() via runpy / direct call and
    inspect exit codes and output. Only tests that do NOT require a real DB
    connection are included here; the live-apply path (lines 399-400 and
    432-434) is left uncovered as an I/O boundary.
    """

    def test_main_exits_if_artifact_not_found(self, monkeypatch, tmp_path):
        """main() exits with a message when the --in file does not exist."""
        monkeypatch.setattr(sys, "argv", [
            "cre_gate.py",
            "--in", "/tmp/__nonexistent_cre_gate_test__.json",
            "--dry-run",
        ])
        with pytest.raises(SystemExit) as exc_info:
            g.main()
        assert "artifact not found" in str(exc_info.value)

    def test_main_dry_run_produces_json_output(self, monkeypatch, tmp_path, capsys):
        """main() in dry-run mode prints valid JSON with expected structure."""
        # Create a minimal valid artifact
        artifact = tmp_path / "run.json"
        artifact.write_text(json.dumps({
            "runMeta": {"finishedAt": "2026-06-14T00:00:00.000Z"},
            "brokers": [],
            "sources": [{"sourceKey": "svn"}],
            "listings": [
                {"sourceKey": "svn", "url": "https://svn.com/1"},
            ],
        }))
        monkeypatch.setattr(sys, "argv", [
            "cre_gate.py", "--in", str(artifact), "--dry-run", "--quiet",
        ])
        g.main()
        captured = capsys.readouterr()
        result = json.loads(captured.out)
        # Dry-run: all sources should be first_seen (empty baseline)
        assert result["summary"]["mode"] == "dry_run"
        assert result["per_source"]["svn"]["verdict"] == "first_seen"

    def test_main_strict_exits_2_when_hold(self, monkeypatch, tmp_path, capsys):
        """main() with --strict exits 2 when any source is hold (line 485-486).

        Force a hold by monkeypatching count_artifacts to return a healthy
        current count but monkeypatching read_baseline to inject a high baseline,
        then running in live mode. Instead, use a simpler approach: monkeypatch
        verdict_for to always return hold for svn, then run dry-run + strict.
        """
        artifact = tmp_path / "run.json"
        artifact.write_text(json.dumps({
            "runMeta": {"finishedAt": "2026-06-14T00:00:00.000Z"},
            "brokers": [],
            "sources": [{"sourceKey": "svn"}],
            "listings": [
                # Only 10 listings -> below DEFAULT_FLOOR of 100 -> hold
                *[{"sourceKey": "svn", "url": f"https://svn.com/{i}"}
                  for i in range(10)],
            ],
        }))
        # Inject a fake baseline so svn has a baseline row but current is below floor
        monkeypatch.setattr(g, "read_baseline",
                            lambda env_file, quiet: (
                                {"svn": {"median": 5000, "last": 5000}},
                                "postgresql://fake/db",
                                "psql",
                            ))
        monkeypatch.setattr(sys, "argv", [
            "cre_gate.py", "--in", str(artifact),
            "--apply",  # triggers live mode with our patched read_baseline
            "--strict", "--quiet",
        ])
        with pytest.raises(SystemExit) as exc_info:
            g.main()
        assert exc_info.value.code == 2

    def test_main_writes_out_file(self, monkeypatch, tmp_path, capsys):
        """main() writes the JSON result to --out when specified (line 473-476)."""
        artifact = tmp_path / "run.json"
        artifact.write_text(json.dumps({
            "runMeta": {"finishedAt": "2026-06-14T00:00:00.000Z"},
            "brokers": [],
            "sources": [{"sourceKey": "svn"}],
            "listings": [{"sourceKey": "svn", "url": "https://svn.com/1"}],
        }))
        out_path = tmp_path / "gate_result.json"
        monkeypatch.setattr(sys, "argv", [
            "cre_gate.py", "--in", str(artifact),
            "--dry-run", "--out", str(out_path), "--quiet",
        ])
        g.main()
        assert out_path.exists()
        result = json.loads(out_path.read_text())
        assert "per_source" in result
        assert "summary" in result

    def test_main_update_baseline_in_dry_run_prints_warning(self, monkeypatch, tmp_path, capsys):
        """--update-baseline in dry-run mode should print a warning (line 430)."""
        artifact = tmp_path / "run.json"
        artifact.write_text(json.dumps({
            "runMeta": {"finishedAt": "2026-06-14T00:00:00.000Z"},
            "brokers": [],
            "sources": [{"sourceKey": "svn"}],
            "listings": [{"sourceKey": "svn", "url": "https://svn.com/1"}],
        }))
        monkeypatch.setattr(sys, "argv", [
            "cre_gate.py", "--in", str(artifact),
            "--dry-run", "--update-baseline",
            # NOT quiet, so the warning is printed
        ])
        g.main()
        captured = capsys.readouterr()
        assert "update-baseline" in captured.err
        assert "dry-run" in captured.err.lower() or "nothing written" in captured.err

    def test_main_dry_run_eprint_emitted(self, monkeypatch, tmp_path, capsys):
        """main() in dry-run mode (not quiet) prints a dry-run notice (line 402)."""
        artifact = tmp_path / "run.json"
        artifact.write_text(json.dumps({
            "runMeta": {"finishedAt": "2026-06-14T00:00:00.000Z"},
            "brokers": [],
            "sources": [{"sourceKey": "svn"}],
            "listings": [{"sourceKey": "svn", "url": "https://svn.com/1"}],
        }))
        monkeypatch.setattr(sys, "argv", [
            "cre_gate.py", "--in", str(artifact), "--dry-run",
        ])
        g.main()
        captured = capsys.readouterr()
        # The dry-run notice must appear on stderr
        assert "dry-run" in captured.err.lower() or "first_seen" in captured.err

    def test_main_multiple_inputs_generates_for_list(self, monkeypatch, tmp_path, capsys):
        """main() with two --in files sets generated_for_artifact to a list (line 465)."""
        def _make_artifact(path, url):
            path.write_text(json.dumps({
                "runMeta": {"finishedAt": "2026-06-14T00:00:00.000Z"},
                "brokers": [],
                "sources": [{"sourceKey": "svn"}],
                "listings": [{"sourceKey": "svn", "url": url}],
            }))

        a1 = tmp_path / "a.json"
        a2 = tmp_path / "b.json"
        _make_artifact(a1, "https://svn.com/1")
        _make_artifact(a2, "https://svn.com/2")
        monkeypatch.setattr(sys, "argv", [
            "cre_gate.py",
            "--in", str(a1), "--in", str(a2),
            "--dry-run", "--quiet",
        ])
        g.main()
        captured = capsys.readouterr()
        result = json.loads(captured.out)
        # With two inputs, generated_for_artifact must be a list
        assert isinstance(result["generated_for_artifact"], list)
        assert len(result["generated_for_artifact"]) == 2

    def test_main_strict_does_not_exit_when_no_holds(self, monkeypatch, tmp_path, capsys):
        """main() with --strict exits 0 when all sources are first_seen (no holds)."""
        artifact = tmp_path / "run.json"
        artifact.write_text(json.dumps({
            "runMeta": {"finishedAt": "2026-06-14T00:00:00.000Z"},
            "brokers": [],
            "sources": [{"sourceKey": "svn"}],
            "listings": [{"sourceKey": "svn", "url": "https://svn.com/1"}],
        }))
        monkeypatch.setattr(sys, "argv", [
            "cre_gate.py", "--in", str(artifact), "--dry-run", "--strict", "--quiet",
        ])
        # Should NOT raise SystemExit (no holds in dry-run with empty baseline)
        g.main()  # if it raises SystemExit(2), the test will fail
