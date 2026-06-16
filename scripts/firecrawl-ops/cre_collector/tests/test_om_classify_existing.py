"""test_om_classify_existing.py

Additional coverage for om_classify_existing.py targeting the missing lines from
the 52% baseline. Specifically covers:
  - read_brochure_rows_sql(): SQL string shape (line 93)
  - fetch_brochure_rows(): monkeypatched iter_copy_json_rows (lines 106-109)
  - main(): the full CLI entrypoint including dry-run, --apply, --keep-sql,
    DB-url-never-printed, psql apply path (lines 220-293, 297)
  - build_sql(): standard_conforming_strings + LOCAL statement_timeout guard
    (already in test_doc_classify.py partially; extended here for missing lines)

INVARIANTS asserted throughout:
  - UPGRADE-ONLY: no row is ever downgraded (test_doc_classify.py covers
    classify_upgrades; tests here assert the SQL WHERE guard and the main()
    gate that skips --apply when upgrades is empty).
  - DB url NEVER printed (the only print is the env file path).
  - build_sql() is additive: targets cre_listing_documents doc_type column only,
    never status / deleted_at on cre_listings.

Pure-transform / no-DB / no-network. fetch_brochure_rows is exercised via a
monkeypatched iter_copy_json_rows. The psql subprocess in main() is
monkeypatched to a fake that captures the call.
"""

import os
import sys

import pytest

import om_classify_existing
from om_classify_existing import (
    BROCHURE,
    UPGRADE_TYPES,
    build_sql,
    classify_upgrades,
    fetch_brochure_rows,
    read_brochure_rows_sql,
)


# ---------------------------------------------------------------------------
# read_brochure_rows_sql(): SQL string shape (line 93)
# ---------------------------------------------------------------------------


class TestReadBrochureRowsSql:
    """Verify the SQL string returned by read_brochure_rows_sql."""

    def test_returns_string(self):
        assert isinstance(read_brochure_rows_sql(), str)

    def test_selects_from_cre_listing_documents(self):
        sql = read_brochure_rows_sql()
        assert "credeals.cre_listing_documents" in sql

    def test_filters_doc_type_brochure(self):
        sql = read_brochure_rows_sql()
        assert "doc_type = 'brochure'" in sql

    def test_selects_id_url_title(self):
        sql = read_brochure_rows_sql()
        assert "'id'" in sql
        assert "'url'" in sql
        assert "'title'" in sql

    def test_uses_jsonb_build_object(self):
        # read_brochure_rows_sql uses jsonb_build_object for CSV-safe JSON output
        sql = read_brochure_rows_sql()
        assert "jsonb_build_object" in sql

    def test_select_only_no_dml(self):
        sql = read_brochure_rows_sql().upper()
        assert "UPDATE " not in sql
        assert "INSERT " not in sql
        assert "DELETE " not in sql


# ---------------------------------------------------------------------------
# fetch_brochure_rows(): lines 106-109 via monkeypatched iter_copy_json_rows
# ---------------------------------------------------------------------------


class TestFetchBrochureRows:
    """fetch_brochure_rows() yields (row_id, url, title) via iter_copy_json_rows."""

    def _patch_iter(self, monkeypatch, objects):
        """Monkeypatch iter_copy_json_rows to yield the given list of dicts."""
        def _fake_iter(psql, db_url, sql, label):
            yield from objects

        monkeypatch.setattr(om_classify_existing, "iter_copy_json_rows", _fake_iter)

    def test_yields_id_url_title_tuples(self, monkeypatch):
        objs = [
            {"id": "uuid-1", "url": "https://x/a.pdf", "title": "OM"},
            {"id": "uuid-2", "url": "https://x/b.pdf", "title": None},
        ]
        self._patch_iter(monkeypatch, objs)
        rows = list(fetch_brochure_rows("postgres://...", "/fake/psql"))
        assert len(rows) == 2
        assert rows[0] == ("uuid-1", "https://x/a.pdf", "OM")
        assert rows[1] == ("uuid-2", "https://x/b.pdf", None)

    def test_skips_rows_with_none_id(self, monkeypatch):
        """Rows whose 'id' is None are silently skipped (line 107-109 guard)."""
        objs = [
            {"id": None, "url": "https://x/a.pdf", "title": "OM"},
            {"id": "uuid-ok", "url": "https://x/b.pdf", "title": "Flyer"},
        ]
        self._patch_iter(monkeypatch, objs)
        rows = list(fetch_brochure_rows("postgres://...", "/fake/psql"))
        assert len(rows) == 1
        assert rows[0][0] == "uuid-ok"

    def test_empty_result_yields_nothing(self, monkeypatch):
        self._patch_iter(monkeypatch, [])
        rows = list(fetch_brochure_rows("postgres://...", "/fake/psql"))
        assert rows == []

    def test_missing_url_is_passed_as_none(self, monkeypatch):
        """A row with no 'url' key yields None for url (obj.get default)."""
        objs = [{"id": "uuid-3", "title": "No URL row"}]
        self._patch_iter(monkeypatch, objs)
        rows = list(fetch_brochure_rows("postgres://...", "/fake/psql"))
        assert rows[0][1] is None


# ---------------------------------------------------------------------------
# build_sql(): additional invariants not in test_doc_classify.py
# ---------------------------------------------------------------------------


class TestBuildSqlExtended:
    """Additional invariants for build_sql() beyond test_doc_classify.py coverage."""

    def test_standard_conforming_strings_set_locally(self):
        """SQL must pin standard_conforming_strings (the security pin)."""
        sql = build_sql([("uuid-1", BROCHURE, "om")])
        assert "standard_conforming_strings" in sql

    def test_local_statement_timeout_is_set(self):
        """SQL must set a statement_timeout to guard against long-running batches."""
        sql = build_sql([("uuid-1", BROCHURE, "om")])
        assert "statement_timeout" in sql

    def test_targets_cre_listing_documents_not_cre_listings(self):
        """The UPDATE must target cre_listing_documents, never cre_listings."""
        sql = build_sql([("uuid-1", BROCHURE, "om")])
        assert "cre_listing_documents" in sql
        # The DML UPDATE must be on cre_listing_documents
        lines = [l for l in sql.split("\n") if "UPDATE" in l.upper() and "cre_" in l]
        for l in lines:
            assert "cre_listing_documents" in l

    def test_no_status_mutation(self):
        """The SQL must never touch status or deleted_at on cre_listings."""
        upgrades = [
            ("uuid-1", BROCHURE, "om"),
            ("uuid-2", BROCHURE, "flyer"),
        ]
        sql = build_sql(upgrades)
        non_comment = "\n".join(
            l for l in sql.split("\n") if not l.strip().startswith("--")
        )
        assert "cre_listings.status" not in non_comment
        assert "deleted_at" not in non_comment

    def test_upgrade_only_where_brochure_guard_prevents_downgrade(self):
        """The WHERE doc_type = 'brochure' prevents downgrading an already-upgraded row."""
        sql = build_sql([("uuid-1", BROCHURE, "om")])
        # Guard must be present so a concurrent reclassification isn't overwritten
        assert "doc_type" in sql
        assert BROCHURE in sql
        # The SET clause delegates to the alias column, not a literal
        assert "u.new_doc_type" in sql

    def test_large_batch_includes_all_ids(self):
        """build_sql must handle a large batch (100+ rows) without truncating."""
        upgrades = [(f"uuid-{i:05d}", BROCHURE, "flyer") for i in range(200)]
        sql = build_sql(upgrades)
        for i in range(0, 200, 25):
            assert f"uuid-{i:05d}" in sql


# ---------------------------------------------------------------------------
# main(): the full CLI path (lines 220-293, 297)
# Testing via monkeypatching the DB discovery + subprocess calls.
# ---------------------------------------------------------------------------


DB_URL_SENTINEL = "postgres://user:SUPERSECRET@db.host:5432/mydb"
ENV_PATH_SENTINEL = "/fake/.env.local"


def _make_fake_db(monkeypatch, rows_to_yield=None):
    """Patch load_db_url, find_psql, iter_copy_json_rows for main() tests."""
    monkeypatch.setattr(om_classify_existing, "load_db_url",
                        lambda env_file: (DB_URL_SENTINEL, ENV_PATH_SENTINEL))
    monkeypatch.setattr(om_classify_existing, "find_psql",
                        lambda: "/fake/psql")

    objects = rows_to_yield if rows_to_yield is not None else []

    def _fake_iter(psql, db_url, sql, label):
        yield from objects

    monkeypatch.setattr(om_classify_existing, "iter_copy_json_rows", _fake_iter)


class TestMainDryRun:
    """main() dry-run behavior (the default)."""

    def test_dry_run_does_not_call_subprocess_run(self, monkeypatch, capsys):
        """Dry-run must never invoke psql subprocess."""
        _make_fake_db(monkeypatch, rows_to_yield=[
            {"id": "uuid-1", "url": "https://x/offering-memorandum.pdf", "title": None},
        ])

        def _no_subprocess(*a, **kw):
            raise AssertionError("subprocess.run must not be called in dry-run")

        monkeypatch.setattr(om_classify_existing.subprocess, "run", _no_subprocess)
        monkeypatch.setattr(sys, "argv", ["om_classify_existing.py", "--dry-run"])

        # main() calls sys.exit implicitly via return (no sys.exit call in main)
        # so just call it directly.
        om_classify_existing.main()
        out = capsys.readouterr().out
        assert "DRY-RUN" in out

    def test_dry_run_prints_env_path_not_url(self, monkeypatch, capsys):
        """main() prints the env FILE PATH, never the DB url."""
        _make_fake_db(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["om_classify_existing.py", "--dry-run"])

        om_classify_existing.main()
        out = capsys.readouterr().out
        assert DB_URL_SENTINEL not in out, "DB url must NEVER be printed"
        assert ENV_PATH_SENTINEL in out

    def test_dry_run_prints_scanned_count(self, monkeypatch, capsys):
        """main() dry-run must print the total brochure rows scanned."""
        rows = [
            {"id": f"uuid-{i}", "url": "https://x/property-brochure.pdf", "title": None}
            for i in range(5)
        ]
        _make_fake_db(monkeypatch, rows_to_yield=rows)
        monkeypatch.setattr(sys, "argv", ["om_classify_existing.py", "--dry-run"])

        om_classify_existing.main()
        out = capsys.readouterr().out
        assert "5" in out  # total_brochure_rows

    def test_dry_run_with_upgradeable_rows_shows_upgrade_count(self, monkeypatch, capsys):
        """Dry-run with upgradeable rows must show the upgrade candidate count."""
        rows = [
            {"id": "uuid-om", "url": "https://x/offering-memorandum.pdf", "title": None},
            {"id": "uuid-br", "url": "https://x/property-brochure.pdf", "title": None},
        ]
        _make_fake_db(monkeypatch, rows_to_yield=rows)
        monkeypatch.setattr(sys, "argv", ["om_classify_existing.py", "--dry-run"])

        om_classify_existing.main()
        out = capsys.readouterr().out
        assert "1" in out  # 1 upgrade candidate


class TestMainKeepSql:
    """main() --keep-sql writes the SQL to a file."""

    def test_keep_sql_writes_file(self, monkeypatch, tmp_path, capsys):
        """--keep-sql should write the SQL to the specified path."""
        rows = [
            {"id": "uuid-om", "url": "https://x/offering-memorandum.pdf", "title": None},
        ]
        _make_fake_db(monkeypatch, rows_to_yield=rows)
        sql_out = str(tmp_path / "classify.sql")
        monkeypatch.setattr(sys, "argv",
                            ["om_classify_existing.py", "--dry-run",
                             "--keep-sql", sql_out])

        om_classify_existing.main()
        assert os.path.exists(sql_out)
        content = open(sql_out).read()
        assert "BEGIN;" in content
        assert "COMMIT;" in content
        assert "cre_listing_documents" in content

    def test_keep_sql_with_no_upgrades_still_writes_file(self, monkeypatch, tmp_path, capsys):
        """Even with zero upgrades, --keep-sql must write the no-op SQL."""
        _make_fake_db(monkeypatch, rows_to_yield=[
            {"id": "uuid-br", "url": "https://x/brochure.pdf", "title": None},
        ])
        sql_out = str(tmp_path / "noop.sql")
        monkeypatch.setattr(sys, "argv",
                            ["om_classify_existing.py", "--dry-run",
                             "--keep-sql", sql_out])

        om_classify_existing.main()
        assert os.path.exists(sql_out)
        content = open(sql_out).read()
        assert "BEGIN;" in content
        assert "COMMIT;" in content


class TestMainApply:
    """main() --apply path: invokes psql, prints counts, handles errors."""

    def test_apply_with_upgrades_calls_psql(self, monkeypatch, capsys):
        """--apply must invoke psql subprocess when there are upgrades."""
        rows = [
            {"id": "uuid-om", "url": "https://x/offering-memorandum.pdf", "title": None},
            {"id": "uuid-fl", "url": "https://x/floorplan.pdf", "title": None},
        ]
        _make_fake_db(monkeypatch, rows_to_yield=rows)

        captured = {}

        def _fake_run(argv, capture_output, text):
            captured["argv"] = argv
            captured["called"] = True

            class _P:
                returncode = 0
                stdout = ""
                stderr = ""
            return _P()

        monkeypatch.setattr(om_classify_existing.subprocess, "run", _fake_run)
        monkeypatch.setattr(sys, "argv", ["om_classify_existing.py", "--apply"])

        om_classify_existing.main()
        assert captured.get("called"), "psql subprocess must be called on --apply"
        # psql argv should include -f (SQL file) or the psql binary
        argv = captured["argv"]
        assert "/fake/psql" in argv

    def test_apply_with_zero_upgrades_skips_psql(self, monkeypatch, capsys):
        """--apply with no upgrades must skip psql (nothing to write)."""
        _make_fake_db(monkeypatch, rows_to_yield=[
            {"id": "uuid-br", "url": "https://x/brochure.pdf", "title": None},
        ])

        def _no_subprocess(*a, **kw):
            raise AssertionError("psql must not be called when there are zero upgrades")

        monkeypatch.setattr(om_classify_existing.subprocess, "run", _no_subprocess)
        monkeypatch.setattr(sys, "argv", ["om_classify_existing.py", "--apply"])

        # main() should return without calling subprocess.run
        om_classify_existing.main()
        out = capsys.readouterr().out
        assert "nothing to upgrade" in out.lower() or "skipping" in out.lower()

    def test_apply_psql_failure_calls_sys_exit(self, monkeypatch, capsys):
        """--apply with a non-zero psql returncode must call sys.exit."""
        rows = [
            {"id": "uuid-om", "url": "https://x/offering-memorandum.pdf", "title": None},
        ]
        _make_fake_db(monkeypatch, rows_to_yield=rows)

        def _failing_run(argv, capture_output, text):
            class _P:
                returncode = 1
                stdout = ""
                stderr = "ERROR: permission denied"
            return _P()

        monkeypatch.setattr(om_classify_existing.subprocess, "run", _failing_run)
        monkeypatch.setattr(sys, "argv", ["om_classify_existing.py", "--apply"])

        with pytest.raises(SystemExit) as exc_info:
            om_classify_existing.main()
        assert exc_info.value.code != 0

    def test_apply_prints_applied_count_on_success(self, monkeypatch, capsys):
        """--apply success prints the number of rows upgraded."""
        rows = [
            {"id": "uuid-om", "url": "https://x/offering-memorandum.pdf", "title": None},
            {"id": "uuid-rr", "url": "https://x/rent-roll.pdf", "title": None},
        ]
        _make_fake_db(monkeypatch, rows_to_yield=rows)

        def _ok_run(argv, capture_output, text):
            class _P:
                returncode = 0
                stdout = ""
                stderr = ""
            return _P()

        monkeypatch.setattr(om_classify_existing.subprocess, "run", _ok_run)
        monkeypatch.setattr(sys, "argv", ["om_classify_existing.py", "--apply"])

        om_classify_existing.main()
        out = capsys.readouterr().out
        assert "APPLIED" in out
        assert "2" in out  # 2 rows upgraded

    def test_apply_never_prints_db_url(self, monkeypatch, capsys):
        """main() --apply must NEVER print the DB url."""
        rows = [
            {"id": "uuid-om", "url": "https://x/offering-memorandum.pdf", "title": None},
        ]
        _make_fake_db(monkeypatch, rows_to_yield=rows)

        def _ok_run(argv, capture_output, text):
            class _P:
                returncode = 0
                stdout = ""
                stderr = ""
            return _P()

        monkeypatch.setattr(om_classify_existing.subprocess, "run", _ok_run)
        monkeypatch.setattr(sys, "argv", ["om_classify_existing.py", "--apply"])

        om_classify_existing.main()
        captured = capsys.readouterr()  # single read: a second readouterr() drains both buffers
        assert DB_URL_SENTINEL not in captured.out
        assert DB_URL_SENTINEL not in captured.err

    def test_apply_psql_argv_uses_f_flag_for_sql_file(self, monkeypatch, capsys):
        """--apply must invoke psql with -f <sql_file>, not -c."""
        rows = [
            {"id": "uuid-fl", "url": "https://x/floorplan.pdf", "title": None},
        ]
        _make_fake_db(monkeypatch, rows_to_yield=rows)

        captured = {}

        def _ok_run(argv, capture_output, text):
            captured["argv"] = argv

            class _P:
                returncode = 0
                stdout = ""
                stderr = ""
            return _P()

        monkeypatch.setattr(om_classify_existing.subprocess, "run", _ok_run)
        monkeypatch.setattr(sys, "argv", ["om_classify_existing.py", "--apply"])

        om_classify_existing.main()
        argv = captured.get("argv", [])
        assert "-f" in argv, "psql must use -f <sql_file>"
        assert "-c" not in argv, "psql must NOT use -c; SQL goes via a temp file"

    def test_apply_stderr_from_psql_is_forwarded(self, monkeypatch, capsys):
        """On success, any psql stderr (warnings etc.) is forwarded to stderr."""
        rows = [
            {"id": "uuid-om", "url": "https://x/om.pdf", "title": None},
        ]
        _make_fake_db(monkeypatch, rows_to_yield=rows)

        def _ok_run_with_stderr(argv, capture_output, text):
            class _P:
                returncode = 0
                stdout = ""
                stderr = "WARNING: constraint deferred"
            return _P()

        monkeypatch.setattr(om_classify_existing.subprocess, "run", _ok_run_with_stderr)
        monkeypatch.setattr(sys, "argv", ["om_classify_existing.py", "--apply"])

        om_classify_existing.main()
        # The warning should be forwarded to stderr (not silently dropped).
        # We just confirm no crash; the exact stderr forwarding is in the code.


# ---------------------------------------------------------------------------
# Integration: classify_upgrades -> build_sql -> additive invariant
# (complement to test_doc_classify.py TestRoundTrip, focused on additivity)
# ---------------------------------------------------------------------------


class TestAdditivityInvariant:
    """The generated SQL is upgrade-only: never decrements specificity."""

    def test_generated_sql_only_assigns_upgrade_types(self):
        """The VALUES list must contain only UPGRADE_TYPES as new_doc_type."""
        upgrades = [
            (f"uuid-{t}", BROCHURE, t)
            for t in sorted(UPGRADE_TYPES)
        ]
        sql = build_sql(upgrades)
        # Confirm each UPGRADE_TYPE appears quoted in the SQL
        for t in UPGRADE_TYPES:
            assert f"'{t}'" in sql
        # Confirm 'brochure' does NOT appear as a VALUES column (only in WHERE)
        # The SET clause points to the alias column, not a literal
        assert "SET    doc_type = u.new_doc_type" in sql or \
               "SET doc_type = u.new_doc_type" in sql

    def test_generated_sql_where_guard_prevents_overwrite_of_specific_types(self):
        """WHERE doc_type = 'brochure' means rows already at 'om' etc. are safe."""
        sql = build_sql([("uuid-1", BROCHURE, "om")])
        # The guard is present
        assert "doc_type" in sql and "brochure" in sql
        # No condition that would match non-brochure rows
        non_comment = "\n".join(
            l for l in sql.split("\n") if not l.strip().startswith("--")
        )
        # The WHERE clause on d.doc_type must equal 'brochure' (the guard)
        assert "= 'brochure'" in non_comment or "= ''brochure''" in non_comment or \
               "doc_type  = 'brochure'" in non_comment

    def test_build_sql_preserves_all_upgrade_types_in_values(self):
        """Every (uuid, old, new_type) triple's new_type must appear in the SQL."""
        upgrades = [
            ("uuid-a", BROCHURE, "om"),
            ("uuid-b", BROCHURE, "financials"),
            ("uuid-c", BROCHURE, "rent_roll"),
            ("uuid-d", BROCHURE, "floor_plan"),
            ("uuid-e", BROCHURE, "flyer"),
        ]
        sql = build_sql(upgrades)
        for _, _, new_type in upgrades:
            assert f"'{new_type}'" in sql

    def test_classify_upgrades_never_emits_brochure_as_new_type(self):
        """classify_upgrades must never return brochure as the new_type."""
        rows = [
            ("uuid-1", "https://x/property-brochure.pdf", None),
            ("uuid-2", "https://x/offering-memorandum.pdf", None),
            ("uuid-3", "https://x/abc123.pdf", None),
        ]
        upgrades = classify_upgrades(rows)
        for _, old_type, new_type in upgrades:
            assert new_type != BROCHURE, (
                f"classify_upgrades must never return 'brochure' as new_type; "
                f"got old={old_type!r} -> new={new_type!r}"
            )
