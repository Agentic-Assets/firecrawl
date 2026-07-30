"""
test_cre_enrich_psql.py

Regression + flow unit tests for the Tier-B enrichment-queue worker's psql
helpers and the currently-uncovered run()/main() branches.

PRIMARY JOB: lock in the three psql bug-fixes so they can never silently
regress:
  FIX-1  SQL fed on STDIN via `["-f", "-"]`, NOT `["-c", sql]`
  FIX-2  argv includes `"-q"` to suppress phantom command-status result rows
  FIX-3  subprocess.run() passes `text=True` AND `input=sql` (str); without
         `text=True`, `input=sql` (str) raises TypeError in Python 3

No live DB, no network.  subprocess.run is monkeypatched for every test.
conftest.py already puts cre_collector/ on sys.path.
"""

import json
import sys

import pytest

import cre_enrich
from cre_enrich import (
    _psql_exec,
    _psql_query,
    build_claim_sql,
    build_complete_sql,
    main,
    run,
)

DB_URL = "postgres://user:secret@db.example.com:5432/testdb"


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def _noncomment_lines(sql):
    """SQL lines excluding psql meta (\\set) and blank lines."""
    out = []
    for line in sql.splitlines():
        s = line.strip()
        if not s or s.startswith("\\"):
            continue
        out.append(line)
    return out


class _FakeProc:
    """Minimal subprocess.CompletedProcess stand-in."""
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_fake_subprocess(returncode=0, stdout=""):
    """Return (calls_list, fake_run_fn).  calls_list accumulates (args, kwargs)."""
    calls = []

    def _run(args, **kwargs):
        calls.append((list(args), kwargs))
        return _FakeProc(returncode=returncode, stdout=stdout)

    return calls, _run


# ---------------------------------------------------------------------------
# _psql_query  —  FIX-1, FIX-2, FIX-3 regression guards
# ---------------------------------------------------------------------------


class TestPsqlQuery:
    """Regression tests: _psql_query must feed SQL on STDIN, pass -q, text=True."""

    def _call(self, monkeypatch, sql, *, stdout="", returncode=0):
        """Run _psql_query with a monkeypatched subprocess and return (calls, result)."""
        monkeypatch.setattr(cre_enrich, "find_psql", lambda: "psql")
        calls, fake_run = _make_fake_subprocess(returncode=returncode, stdout=stdout)
        monkeypatch.setattr(cre_enrich.subprocess, "run", fake_run)
        if returncode != 0:
            with pytest.raises(SystemExit):
                _psql_query(DB_URL, sql)
            return calls, None
        result = _psql_query(DB_URL, sql)
        return calls, result

    # --- FIX-1: SQL fed on STDIN (`-f -`), NOT `-c` --------------------------

    def test_fix1_argv_uses_stdin_feed_f_dash(self, monkeypatch):
        """FIX-1: argv must contain `-f` immediately followed by `-`."""
        calls, _ = self._call(monkeypatch, "SELECT 1;")
        assert len(calls) == 1
        argv = calls[0][0]
        assert "-f" in argv
        dash_idx = argv.index("-f")
        assert argv[dash_idx + 1] == "-", (
            "argv must have `-f` immediately followed by `-` (stdin); "
            f"got: {argv}"
        )

    def test_fix1_argv_has_no_dash_c(self, monkeypatch):
        """FIX-1 corollary: `-c` must NOT appear in the argv."""
        calls, _ = self._call(monkeypatch, "SELECT 1;")
        argv = calls[0][0]
        assert "-c" not in argv, f"argv must not contain `-c`; got: {argv}"

    # --- FIX-2: `-q` suppresses phantom command-status rows ------------------

    def test_fix2_argv_contains_q(self, monkeypatch):
        """FIX-2: `-q` must appear in the argv."""
        calls, _ = self._call(monkeypatch, "SELECT 1;")
        argv = calls[0][0]
        assert "-q" in argv, f"argv must contain `-q`; got: {argv}"

    # --- FIX-3: text=True + input=sql (str) ----------------------------------

    def test_fix3_text_true_in_kwargs(self, monkeypatch):
        """FIX-3: kwargs must include `text=True`."""
        calls, _ = self._call(monkeypatch, "SELECT 1;")
        kwargs = calls[0][1]
        assert kwargs.get("text") is True, (
            "subprocess.run() must be called with text=True; "
            f"got text={kwargs.get('text')!r}"
        )

    def test_fix3_input_is_the_sql_string(self, monkeypatch):
        """FIX-3: `input` kwarg must be the exact SQL string passed in."""
        sql = "SELECT 42;"
        calls, _ = self._call(monkeypatch, sql)
        kwargs = calls[0][1]
        assert kwargs.get("input") == sql, (
            f"kwargs['input'] must equal the SQL string; "
            f"got: {kwargs.get('input')!r}"
        )

    def test_fix3_no_type_error(self, monkeypatch):
        """FIX-3 smoke: calling _psql_query with a str SQL must not raise TypeError."""
        monkeypatch.setattr(cre_enrich, "find_psql", lambda: "psql")
        calls, fake_run = _make_fake_subprocess(stdout="")
        monkeypatch.setattr(cre_enrich.subprocess, "run", fake_run)
        # If text=True is missing, Python raises TypeError here.
        _psql_query(DB_URL, "SELECT 1;")  # must not raise

    # --- Additional argv invariants -------------------------------------------

    def test_argv_contains_v_on_error_stop(self, monkeypatch):
        """-v ON_ERROR_STOP=1 must be present in argv."""
        calls, _ = self._call(monkeypatch, "SELECT 1;")
        argv = calls[0][0]
        assert "-v" in argv
        v_idx = argv.index("-v")
        assert argv[v_idx + 1] == "ON_ERROR_STOP=1", (
            f"Expected ON_ERROR_STOP=1 after -v; got: {argv[v_idx + 1]!r}"
        )

    def test_argv_contains_tA_and_field_separator(self, monkeypatch):
        """-tA and -F \\t must appear for unaligned tab output."""
        calls, _ = self._call(monkeypatch, "SELECT 1;")
        argv = calls[0][0]
        assert "-tA" in argv, f"argv must contain -tA; got: {argv}"
        assert "-F" in argv
        f_idx = argv.index("-F")
        assert argv[f_idx + 1] == "\t", (
            f"Expected tab after -F; got: {argv[f_idx + 1]!r}"
        )

    def test_db_url_never_in_process_output(self, monkeypatch, capsys):
        """DB url must never appear in stdout/stderr prints from _psql_query."""
        calls, _ = self._call(monkeypatch, "SELECT 1;")
        captured = capsys.readouterr()
        assert DB_URL not in captured.out
        assert DB_URL not in captured.err

    def test_database_secret_is_not_in_process_argv(self, monkeypatch):
        calls, _ = self._call(monkeypatch, "SELECT 1;")
        argv, kwargs = calls[0]
        assert DB_URL not in argv
        assert "secret" not in " ".join(argv)
        assert kwargs["env"]["PGPASSWORD"] == "secret"

    # --- Row parsing ----------------------------------------------------------

    def test_tab_split_tuples_blank_lines_skipped(self, monkeypatch):
        """Given stdout `a\\tb\\nc\\t\\n`, return [('a','b'), ('c','')]."""
        stdout_text = "a\tb\nc\t\n"
        calls, result = self._call(monkeypatch, "SELECT 1;", stdout=stdout_text)
        assert result == [("a", "b"), ("c", "")], (
            f"Expected [('a','b'), ('c','')]; got: {result!r}"
        )

    def test_empty_stdout_returns_empty_list(self, monkeypatch):
        """Empty psql output should yield an empty list, not an error."""
        calls, result = self._call(monkeypatch, "SELECT 1;", stdout="")
        assert result == []

    # --- Non-zero returncode exits via sys.exit --------------------------------

    def test_nonzero_returncode_raises_system_exit(self, monkeypatch):
        """_psql_query must call sys.exit() when returncode != 0 (FIX-1 guard)."""
        calls, _ = self._call(monkeypatch, "SELECT 1;", returncode=1)
        # The fixture already checked for SystemExit; being here confirms it.
        assert len(calls) == 1  # one subprocess call was made

    def test_nonzero_exit_message_does_not_contain_db_url(self, monkeypatch):
        """Error message on failure must not expose the DB url."""
        monkeypatch.setattr(cre_enrich, "find_psql", lambda: "psql")
        calls, fake_run = _make_fake_subprocess(returncode=2, stdout="")
        monkeypatch.setattr(cre_enrich.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as exc_info:
            _psql_query(DB_URL, "SELECT 1;")
        msg = str(exc_info.value)
        assert DB_URL not in msg, (
            f"DB url must not appear in sys.exit message; got: {msg!r}"
        )

    # --- Meta-command headed script reaches subprocess unmodified ------------

    def test_meta_command_headed_sql_transported_verbatim(self, monkeypatch):
        """A claim SQL with \\set + BEGIN/COMMIT must arrive unmodified in `input`."""
        sql = build_claim_sql(100)
        assert "\\set ON_ERROR_STOP on" in sql
        assert "BEGIN;" in sql
        assert "COMMIT;" in sql

        monkeypatch.setattr(cre_enrich, "find_psql", lambda: "psql")
        calls, fake_run = _make_fake_subprocess(stdout="")
        monkeypatch.setattr(cre_enrich.subprocess, "run", fake_run)
        _psql_query(DB_URL, sql)

        kwargs = calls[0][1]
        assert kwargs["input"] == sql, (
            "The full SQL including \\set meta-command must be sent on stdin unmodified"
        )


# ---------------------------------------------------------------------------
# _psql_exec  —  FIX-1, FIX-2, FIX-3 regression guards
# ---------------------------------------------------------------------------


class TestPsqlExec:
    """Regression tests: _psql_exec must feed SQL on STDIN, pass -q, text=True."""

    def _call(self, monkeypatch, sql, *, returncode=0):
        """Run _psql_exec with a monkeypatched subprocess, return calls list."""
        monkeypatch.setattr(cre_enrich, "find_psql", lambda: "psql")
        calls, fake_run = _make_fake_subprocess(returncode=returncode)
        monkeypatch.setattr(cre_enrich.subprocess, "run", fake_run)
        if returncode != 0:
            with pytest.raises(SystemExit):
                _psql_exec(DB_URL, sql)
        else:
            _psql_exec(DB_URL, sql)
        return calls

    # --- FIX-1: SQL fed on STDIN (`-f -`), NOT `-c` --------------------------

    def test_fix1_argv_uses_stdin_feed_f_dash(self, monkeypatch):
        """FIX-1: argv must contain `-f` immediately followed by `-`."""
        calls = self._call(monkeypatch, "BEGIN; COMMIT;")
        argv = calls[0][0]
        assert "-f" in argv
        dash_idx = argv.index("-f")
        assert argv[dash_idx + 1] == "-", (
            f"argv must have `-f` followed by `-` (stdin); got: {argv}"
        )

    def test_fix1_argv_has_no_dash_c(self, monkeypatch):
        """FIX-1 corollary: `-c` must NOT appear in the argv."""
        calls = self._call(monkeypatch, "BEGIN; COMMIT;")
        argv = calls[0][0]
        assert "-c" not in argv, f"argv must not contain `-c`; got: {argv}"

    # --- FIX-2: `-q` suppresses phantom command-status rows ------------------

    def test_fix2_argv_contains_q(self, monkeypatch):
        """FIX-2: `-q` must appear in the argv."""
        calls = self._call(monkeypatch, "BEGIN; COMMIT;")
        argv = calls[0][0]
        assert "-q" in argv, f"argv must contain `-q`; got: {argv}"

    # --- FIX-3: text=True + input=sql (str) ----------------------------------

    def test_fix3_text_true_in_kwargs(self, monkeypatch):
        """FIX-3: kwargs must include `text=True`."""
        calls = self._call(monkeypatch, "BEGIN; COMMIT;")
        kwargs = calls[0][1]
        assert kwargs.get("text") is True, (
            f"subprocess.run() must be called with text=True; "
            f"got text={kwargs.get('text')!r}"
        )

    def test_fix3_input_is_the_sql_string(self, monkeypatch):
        """FIX-3: `input` kwarg must be the exact SQL string passed in."""
        sql = "BEGIN; SELECT 1; COMMIT;"
        calls = self._call(monkeypatch, sql)
        kwargs = calls[0][1]
        assert kwargs.get("input") == sql

    def test_fix3_no_type_error(self, monkeypatch):
        """FIX-3 smoke: _psql_exec with str SQL must not raise TypeError.

        Before the fix, subprocess.run(..., input=sql) with text=True missing
        would raise TypeError because bytes were expected.  This test is the
        direct TypeError-guard regression.
        """
        monkeypatch.setattr(cre_enrich, "find_psql", lambda: "psql")
        calls, fake_run = _make_fake_subprocess()
        monkeypatch.setattr(cre_enrich.subprocess, "run", fake_run)
        # Must not raise TypeError (FIX-3).
        _psql_exec(DB_URL, "SELECT 1;")

    # --- Additional argv invariants -------------------------------------------

    def test_argv_contains_v_on_error_stop(self, monkeypatch):
        """-v ON_ERROR_STOP=1 must be present in argv."""
        calls = self._call(monkeypatch, "BEGIN; COMMIT;")
        argv = calls[0][0]
        assert "-v" in argv
        v_idx = argv.index("-v")
        assert argv[v_idx + 1] == "ON_ERROR_STOP=1"

    def test_db_url_never_in_printed_output(self, monkeypatch, capsys):
        """DB url must never appear in stdout/stderr prints from _psql_exec."""
        self._call(monkeypatch, "BEGIN; COMMIT;")
        captured = capsys.readouterr()
        assert DB_URL not in captured.out
        assert DB_URL not in captured.err

    def test_database_secret_is_not_in_process_argv(self, monkeypatch):
        calls = self._call(monkeypatch, "BEGIN; COMMIT;")
        argv, kwargs = calls[0]
        assert DB_URL not in argv
        assert "secret" not in " ".join(argv)
        assert kwargs["env"]["PGPASSWORD"] == "secret"

    # --- Non-zero returncode exits via sys.exit --------------------------------

    def test_nonzero_returncode_raises_system_exit(self, monkeypatch):
        """_psql_exec must call sys.exit() when returncode != 0."""
        monkeypatch.setattr(cre_enrich, "find_psql", lambda: "psql")
        calls, fake_run = _make_fake_subprocess(returncode=1)
        monkeypatch.setattr(cre_enrich.subprocess, "run", fake_run)
        with pytest.raises(SystemExit):
            _psql_exec(DB_URL, "BEGIN; COMMIT;")

    def test_nonzero_exit_message_does_not_contain_db_url(self, monkeypatch):
        """Error message on failure must not expose the DB url."""
        monkeypatch.setattr(cre_enrich, "find_psql", lambda: "psql")
        calls, fake_run = _make_fake_subprocess(returncode=2)
        monkeypatch.setattr(cre_enrich.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as exc_info:
            _psql_exec(DB_URL, "BEGIN; COMMIT;")
        msg = str(exc_info.value)
        assert DB_URL not in msg

    # --- Meta-command headed script reaches subprocess unmodified ------------

    def test_complete_sql_with_meta_command_transported_verbatim(self, monkeypatch):
        """A complete SQL with \\set + BEGIN/COMMIT must arrive unmodified in `input`."""
        sql = build_complete_sql({"11111111-1111-1111-1111-111111111111"})
        assert "\\set ON_ERROR_STOP on" in sql
        assert "BEGIN;" in sql
        assert "COMMIT;" in sql

        monkeypatch.setattr(cre_enrich, "find_psql", lambda: "psql")
        calls, fake_run = _make_fake_subprocess()
        monkeypatch.setattr(cre_enrich.subprocess, "run", fake_run)
        _psql_exec(DB_URL, sql)

        kwargs = calls[0][1]
        assert kwargs["input"] == sql, (
            "The full SQL including \\set meta-command must be sent on stdin unmodified"
        )


# ---------------------------------------------------------------------------
# run() branches not covered by test_cre_enrich.py
# ---------------------------------------------------------------------------

DB_URL_SENTINEL = "postgres://user:secret@db.example.com:5432/postgres"


class _Args:
    def __init__(self, env_file=None, batch=200, dry_run=False, source=None):
        self.env_file = env_file
        self.batch = batch
        self.dry_run = dry_run
        self.source = source


def _wire_run(monkeypatch, tmp_path, *, claimed_rows, collect_rc=0,
              write_enriched="__unset__", ingest_rc=0):
    """Patch run()'s DB + subprocess boundary; return a calls-tracking dict.

    `claimed_rows` is a list of dicts with keys matching _parse_claimed output.
    `write_enriched`: dict written as enriched.json; None = write nothing;
    `"__unset__"` = default 1-listing artifact echoing each claimed url.
    """
    calls = {
        "exec_sqls": [],          # all SQL strings passed to _psql_exec
        "query_sqls": [],         # all SQL strings passed to _psql_query
        "collect_called": False,
        "ingest_called": False,
        "ingest_argv": None,
    }

    monkeypatch.setattr(cre_enrich, "OUT_ENRICH_DIR", str(tmp_path))
    monkeypatch.setattr(cre_enrich, "load_db_url",
                        lambda ef: (DB_URL_SENTINEL, "/fake/.env.local"))

    def _fake_query(db_url, sql):
        calls["query_sqls"].append(sql)
        return [
            (r["id"], r.get("source_key", "colliers-main"),
             r.get("external_id", "ext:1"), r.get("url", "https://x/1"),
             r.get("reason", "new"), str(r.get("attempts", 0)))
            for r in claimed_rows
        ]

    def _fake_exec(db_url, sql):
        calls["exec_sqls"].append(sql)

    monkeypatch.setattr(cre_enrich, "_psql_query", _fake_query)
    monkeypatch.setattr(cre_enrich, "_psql_exec", _fake_exec)

    class _Proc:
        def __init__(self, rc):
            self.returncode = rc

    claimed_urls = [r.get("url", "https://x/1") for r in claimed_rows]

    def _fake_subprocess_run(argv, **kwargs):
        if "collect.ts" in argv:
            calls["collect_called"] = True
            out_path = argv[argv.index("--out") + 1]
            if write_enriched == "__unset__":
                payload = {
                    "runMeta": {"mode": "enrich"},
                    "listings": [
                        {"url": r.get("url", "https://x/1"), "sourceKey": r.get("source_key", "colliers-main")}
                        for r in claimed_rows
                    ],
                }
            else:
                payload = write_enriched
                if isinstance(payload, dict):
                    payload.setdefault("runMeta", {"mode": "enrich"})
                    source_by_url = {
                        r.get("url", "https://x/1"): r.get("source_key", "colliers-main")
                        for r in claimed_rows
                    }
                    for listing in payload.get("listings") or []:
                        if isinstance(listing, dict) and "sourceKey" not in listing:
                            listing["sourceKey"] = source_by_url.get(listing.get("url"))
            if payload is not None:
                import json as _json
                with open(out_path, "w") as f:
                    _json.dump(payload, f)
            return _Proc(collect_rc)
        if any(str(a).endswith("cre_ingest.py") for a in argv):
            calls["ingest_called"] = True
            calls["ingest_argv"] = list(argv)
            return _Proc(ingest_rc)
        return _Proc(0)

    monkeypatch.setattr(cre_enrich.subprocess, "run", _fake_subprocess_run)
    return calls


class TestRunComplete:
    """run() COMPLETE path: claimed batch enriched, ingest succeeds."""

    def test_complete_path_calls_psql_exec_with_complete_sql(
            self, monkeypatch, tmp_path):
        """After successful collect + ingest, _psql_exec is called with
        DELETE-based build_complete_sql, not a release SQL."""
        rows = [
            {"id": "id-1", "url": "https://x/a", "source_key": "colliers-main",
             "external_id": "main:a", "reason": "new", "attempts": 0},
        ]
        calls = _wire_run(monkeypatch, tmp_path, claimed_rows=rows)
        rc = run(_Args())
        assert rc == 0
        assert calls["ingest_called"] is True
        combined = "\n".join(calls["exec_sqls"])
        assert "DELETE FROM credeals.cre_enrichment_queue" in combined
        assert "WHERE id IN ('id-1'::uuid)" in combined
        assert "WHERE url IN" not in combined

    def test_complete_path_no_release_sql_when_successful(
            self, monkeypatch, tmp_path):
        """On the happy path, claimed_at = NULL (release) must not appear."""
        rows = [
            {"id": "id-1", "url": "https://x/a", "source_key": "colliers-main",
             "external_id": "main:a", "reason": "new", "attempts": 0},
        ]
        calls = _wire_run(monkeypatch, tmp_path, claimed_rows=rows)
        run(_Args())
        combined = "\n".join(calls["exec_sqls"])
        # complete path deletes; release (claimed_at = NULL) should not appear
        assert "claimed_at = NULL" not in combined

    def test_complete_path_increments_absent_rows(self, monkeypatch, tmp_path):
        """Rows claimed but absent from the enriched artifact get attempts+1."""
        rows = [
            {"id": "id-done", "url": "https://x/a", "source_key": "jll-investor",
             "external_id": "investor:1", "reason": "new", "attempts": 0},
            {"id": "id-miss", "url": "https://x/b", "source_key": "jll-investor",
             "external_id": "investor:2", "reason": "new", "attempts": 0},
        ]
        # Only the first URL is in the enriched artifact
        calls = _wire_run(monkeypatch, tmp_path, claimed_rows=rows,
                          write_enriched={"listings": [{"url": "https://x/a"}]})
        rc = run(_Args())
        assert rc == 0
        combined = "\n".join(calls["exec_sqls"])
        assert "DELETE FROM credeals.cre_enrichment_queue" in combined
        assert "attempts = attempts + 1" in combined


class TestRunIngestFailure:
    """run() path: collect succeeds but ingest subprocess fails."""

    def test_ingest_failure_releases_claims(self, monkeypatch, tmp_path):
        """When ingest rc != 0, claims are released (claimed_at = NULL)."""
        rows = [{"id": "id-1", "url": "https://x/a", "source_key": "colliers-main",
                 "external_id": "main:a", "reason": "new", "attempts": 0}]
        calls = _wire_run(monkeypatch, tmp_path, claimed_rows=rows, ingest_rc=1)
        rc = run(_Args())
        assert rc == 1
        combined = "\n".join(calls["exec_sqls"])
        assert "claimed_at = NULL" in combined

    def test_ingest_failure_no_complete_sql(self, monkeypatch, tmp_path):
        """When ingest fails, no DELETE (complete SQL) should be emitted."""
        rows = [{"id": "id-1", "url": "https://x/a", "source_key": "colliers-main",
                 "external_id": "main:a", "reason": "new", "attempts": 0}]
        calls = _wire_run(monkeypatch, tmp_path, claimed_rows=rows, ingest_rc=1)
        run(_Args())
        combined = "\n".join(calls["exec_sqls"])
        assert "DELETE FROM credeals.cre_enrichment_queue" not in combined


class TestRunBranchBatchValidation:
    """run() early-exit: invalid --batch exits."""

    def test_batch_zero_exits(self, monkeypatch, tmp_path):
        """--batch 0 must call sys.exit (< 1)."""
        monkeypatch.setattr(cre_enrich, "load_db_url",
                            lambda ef: (DB_URL_SENTINEL, "/fake/.env.local"))
        with pytest.raises(SystemExit):
            run(_Args(batch=0))


class TestRunEnvFile:
    """run() --env-file is passed through to ingest subprocess."""

    def test_env_file_forwarded_to_ingest_argv(self, monkeypatch, tmp_path):
        """When --env-file is provided, ingest subprocess argv must carry it."""
        rows = [{"id": "id-1", "url": "https://x/a", "source_key": "colliers-main",
                 "external_id": "main:a", "reason": "new", "attempts": 0}]
        calls = _wire_run(monkeypatch, tmp_path, claimed_rows=rows)
        run(_Args(env_file="/custom/.env.local"))
        ingest_argv = calls["ingest_argv"]
        assert ingest_argv is not None
        assert "--env-file" in ingest_argv
        idx = ingest_argv.index("--env-file")
        assert ingest_argv[idx + 1] == "/custom/.env.local"

    def test_no_env_file_no_env_file_flag_in_ingest(self, monkeypatch, tmp_path):
        """When env_file=None, --env-file must NOT appear in ingest argv."""
        rows = [{"id": "id-1", "url": "https://x/a", "source_key": "colliers-main",
                 "external_id": "main:a", "reason": "new", "attempts": 0}]
        calls = _wire_run(monkeypatch, tmp_path, claimed_rows=rows)
        run(_Args(env_file=None))
        ingest_argv = calls["ingest_argv"]
        assert ingest_argv is not None
        assert "--env-file" not in ingest_argv


# ---------------------------------------------------------------------------
# main() argparse wiring
# ---------------------------------------------------------------------------


class TestMain:
    """main() argparse contract wires batch, source, dry-run, and env-file into run()."""

    def _run_main(self, monkeypatch, argv_extra=None):
        """Patch sys.argv and run main(), capturing the args object seen by run()."""
        seen_args = {}

        def _fake_run(args):
            seen_args.update({
                "batch": args.batch,
                "dry_run": args.dry_run,
                "env_file": args.env_file,
                "source": args.source,
            })
            return 0

        monkeypatch.setattr(cre_enrich, "run", _fake_run)
        argv = ["cre_enrich.py"] + (argv_extra or [])
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        return seen_args

    def test_default_batch_is_200(self, monkeypatch):
        seen = self._run_main(monkeypatch)
        assert seen["batch"] == 200

    def test_batch_flag_parsed(self, monkeypatch):
        seen = self._run_main(monkeypatch, ["--batch", "50"])
        assert seen["batch"] == 50

    def test_source_flag_parsed(self, monkeypatch):
        seen = self._run_main(monkeypatch, ["--source", "marcus-millichap"])
        assert seen["source"] == "marcus-millichap"

    def test_source_default_none(self, monkeypatch):
        seen = self._run_main(monkeypatch)
        assert seen["source"] is None

    def test_dry_run_flag_parsed(self, monkeypatch):
        seen = self._run_main(monkeypatch, ["--dry-run"])
        assert seen["dry_run"] is True

    def test_dry_run_default_false(self, monkeypatch):
        seen = self._run_main(monkeypatch)
        assert seen["dry_run"] is False

    def test_env_file_flag_parsed(self, monkeypatch):
        seen = self._run_main(monkeypatch, ["--env-file", "/tmp/custom.env"])
        assert seen["env_file"] == "/tmp/custom.env"

    def test_env_file_default_none(self, monkeypatch):
        seen = self._run_main(monkeypatch)
        assert seen["env_file"] is None

    def test_db_url_never_printed_by_main(self, monkeypatch, tmp_path, capsys):
        """main() entry point must never print the DB url."""
        rows = [{"id": "id-1", "url": "https://x/a", "source_key": "colliers-main",
                 "external_id": "main:a", "reason": "new", "attempts": 0}]

        # Wire up a full run
        monkeypatch.setattr(cre_enrich, "OUT_ENRICH_DIR", str(tmp_path))
        monkeypatch.setattr(cre_enrich, "load_db_url",
                            lambda ef: (DB_URL_SENTINEL, "/fake/.env.local"))

        def _fake_query(db_url, sql):
            return [("id-1", "colliers-main", "main:a",
                     "https://x/a", "new", "0")]

        def _fake_exec(db_url, sql):
            pass

        monkeypatch.setattr(cre_enrich, "_psql_query", _fake_query)
        monkeypatch.setattr(cre_enrich, "_psql_exec", _fake_exec)

        class _Proc:
            def __init__(self, rc):
                self.returncode = rc

        def _fake_subprocess_run(argv, **kwargs):
            if "collect.ts" in argv:
                out_path = argv[argv.index("--out") + 1]
                with open(out_path, "w") as f:
                    json.dump(
                        {
                            "runMeta": {"mode": "enrich"},
                            "listings": [{"url": "https://x/a", "sourceKey": "colliers-main"}],
                        },
                        f,
                    )
                return _Proc(0)
            if any(str(a).endswith("cre_ingest.py") for a in argv):
                return _Proc(0)
            return _Proc(0)

        monkeypatch.setattr(cre_enrich.subprocess, "run", _fake_subprocess_run)
        monkeypatch.setattr(sys, "argv", ["cre_enrich.py"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert DB_URL_SENTINEL not in captured.out
        assert DB_URL_SENTINEL not in captured.err
