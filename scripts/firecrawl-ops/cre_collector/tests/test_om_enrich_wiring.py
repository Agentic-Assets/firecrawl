"""
test_om_enrich_wiring.py

Asserts the OPT-IN OM-parse step wired into cre_enrich.py is ADDITIVE-ONLY and
default-OFF. Per tests/CLAUDE.md, all assertions are pure / no-DB: the worker's
DB + subprocess boundary is monkeypatched (reusing the harness style from
test_cre_enrich.py) so nothing connects.

Contract pinned here:
  - the OM step is OFF unless --om-parse / CRE_OM_PARSE=1 (default enrich flow is
    byte-identical without it);
  - when ON, it shells out to om_parse.py with NO destructive flags, and om_parse
    itself re-ingests with ["--in", path] only (never --activate-status /
    --mark-missing), so the OM step can never soft-delete or activate status;
  - an OM-step failure does NOT fail the enrich run (the detail enrich committed);
  - re-running the enrich is idempotent (the OM step is invoked the same way).
"""

import json
import os

import cre_enrich
import om_parse
from cre_enrich import build_om_parse_argv, om_parse_enabled

DB_URL_SENTINEL = "postgres://user:secret@db.example.com:5432/postgres"


# --- om_parse_enabled gate (opt-in, default-off) ---------------------------


class _FlagArgs:
    def __init__(self, om_parse=False):
        self.om_parse = om_parse


def test_om_parse_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CRE_OM_PARSE", raising=False)
    assert om_parse_enabled(_FlagArgs(om_parse=False)) is False


def test_om_parse_enabled_by_flag(monkeypatch):
    monkeypatch.delenv("CRE_OM_PARSE", raising=False)
    assert om_parse_enabled(_FlagArgs(om_parse=True)) is True


def test_om_parse_enabled_by_env(monkeypatch):
    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv("CRE_OM_PARSE", truthy)
        assert om_parse_enabled(_FlagArgs(om_parse=False)) is True
    for falsy in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("CRE_OM_PARSE", falsy)
        assert om_parse_enabled(_FlagArgs(om_parse=False)) is False


# --- build_om_parse_argv: additive, no destructive flags -------------------


def test_build_om_parse_argv_shape_and_no_destructive_flags():
    argv = build_om_parse_argv(["colliers-main", "jll-investor"], apply=True)
    assert any(str(a).endswith("om_parse.py") for a in argv)
    assert "--sources" in argv
    # sources are sorted + de-duplicated for a stable argv.
    src = argv[argv.index("--sources") + 1]
    assert src == "colliers-main,jll-investor"
    assert "--apply" in argv
    joined = " ".join(str(a) for a in argv)
    for banned in ("--activate-status", "--mark-missing", "--no-mark-missing"):
        assert banned not in joined


def test_build_om_parse_argv_dedupes_and_sorts_sources():
    argv = build_om_parse_argv(["svn", "svn", "lee-associates"], apply=False)
    src = argv[argv.index("--sources") + 1]
    assert src == "lee-associates,svn"
    assert "--apply" not in argv  # dry posture when apply=False


def test_om_parse_underlying_ingest_is_in_path_only():
    # The OM step's real DB write goes through om_parse.build_ingest_argv, which
    # is the SAME ["--in", path] safety guard as the enrich detail ingest.
    argv = om_parse.build_ingest_argv("/tmp/om.json")
    assert argv == ["--in", "/tmp/om.json"]
    for banned in ("--activate-status", "--mark-missing", "--no-mark-missing"):
        assert banned not in argv


# --- run() wiring: OM step OFF by default does not fire --------------------


def test_run_does_not_invoke_om_parse_when_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("CRE_OM_PARSE", raising=False)
    calls = _wire_run(monkeypatch, tmp_path,
                      claimed=[("id-1", "cbre", "x1", "https://x/a", "new", "0")],
                      collect_rc=0,
                      write_enriched={"listings": [{"url": "https://x/a"}]})
    rc = cre_enrich.run(_Args(env_file=None, om_parse=False))
    assert rc == 0
    assert calls["om_parse_called"] is False  # default enrich flow unchanged


# --- run() wiring: OM step ON fires om_parse.py additively ------------------


def test_run_invokes_om_parse_when_enabled_with_batch_sources(monkeypatch, tmp_path):
    monkeypatch.delenv("CRE_OM_PARSE", raising=False)
    calls = _wire_run(monkeypatch, tmp_path,
                      claimed=[("id-1", "cbre", "x1", "https://x/a", "new", "0"),
                               ("id-2", "jll", "x2", "https://x/b", "new", "0")],
                      collect_rc=0,
                      write_enriched={"listings": [{"url": "https://x/a"},
                                                   {"url": "https://x/b"}]})
    rc = cre_enrich.run(_Args(env_file=None, om_parse=True))
    assert rc == 0
    assert calls["om_parse_called"] is True
    om_argv = calls["om_parse_argv"]
    # sources are the batch's source keys (sorted, deduped).
    src = om_argv[om_argv.index("--sources") + 1]
    assert src == "cbre,jll"
    joined = " ".join(str(a) for a in om_argv)
    for banned in ("--activate-status", "--mark-missing", "--no-mark-missing"):
        assert banned not in joined


def test_om_step_failure_does_not_fail_enrich_run(monkeypatch, tmp_path):
    monkeypatch.delenv("CRE_OM_PARSE", raising=False)
    calls = _wire_run(monkeypatch, tmp_path,
                      claimed=[("id-1", "cbre", "x1", "https://x/a", "new", "0")],
                      collect_rc=0,
                      write_enriched={"listings": [{"url": "https://x/a"}]},
                      om_rc=1)  # OM step fails
    rc = cre_enrich.run(_Args(env_file=None, om_parse=True))
    # the enrich run still SUCCEEDS (detail enrich already committed; OM facts are
    # additive and retry-safe).
    assert rc == 0
    assert calls["om_parse_called"] is True


def test_om_step_idempotent_on_rerun(monkeypatch, tmp_path):
    # Two identical enrich runs invoke the OM step with identical argv (the step
    # is a pure function of the batch's source keys). om_parse re-ingest is
    # idempotent (ON CONFLICT DO UPDATE on the om_facts unique key).
    monkeypatch.delenv("CRE_OM_PARSE", raising=False)
    seen = []
    for _ in range(2):
        calls = _wire_run(monkeypatch, tmp_path,
                          claimed=[("id-1", "cbre", "x1", "https://x/a", "new", "0")],
                          collect_rc=0,
                          write_enriched={"listings": [{"url": "https://x/a"}]})
        cre_enrich.run(_Args(env_file=None, om_parse=True))
        seen.append(calls["om_parse_argv"])
    assert seen[0] == seen[1]


def test_om_step_enabled_via_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("CRE_OM_PARSE", "1")
    calls = _wire_run(monkeypatch, tmp_path,
                      claimed=[("id-1", "cbre", "x1", "https://x/a", "new", "0")],
                      collect_rc=0,
                      write_enriched={"listings": [{"url": "https://x/a"}]})
    cre_enrich.run(_Args(env_file=None, om_parse=False))  # flag off, env on
    assert calls["om_parse_called"] is True


def test_om_step_skips_when_no_source_keys(monkeypatch, tmp_path):
    monkeypatch.delenv("CRE_OM_PARSE", raising=False)
    # claimed rows carry empty source_key -> OM step has nothing to parse.
    calls = _wire_run(monkeypatch, tmp_path,
                      claimed=[("id-1", "", "x1", "https://x/a", "new", "0")],
                      collect_rc=0,
                      write_enriched={"listings": [{"url": "https://x/a"}]})
    rc = cre_enrich.run(_Args(env_file=None, om_parse=True))
    assert rc == 0
    assert calls["om_parse_called"] is False


# ---------------------------------------------------------------------------
# Harness: monkeypatch the worker's DB + subprocess boundary (mirrors
# test_cre_enrich._wire_run, extended to capture the om_parse.py invocation).
# ---------------------------------------------------------------------------


class _Args:
    def __init__(self, env_file=None, batch=200, dry_run=False, om_parse=False):
        self.env_file = env_file
        self.batch = batch
        self.dry_run = dry_run
        self.om_parse = om_parse


def _wire_run(monkeypatch, tmp_path, *, claimed, collect_rc=0,
              write_enriched="__unset__", om_rc=0):
    calls = {
        "exec_sql": [],
        "ingest_called": False,
        "om_parse_called": False,
        "om_parse_argv": None,
    }

    monkeypatch.setattr(cre_enrich, "OUT_ENRICH_DIR", str(tmp_path))
    monkeypatch.setattr(cre_enrich, "load_db_url",
                        lambda env_file: (DB_URL_SENTINEL, "/fake/.env.local"))
    monkeypatch.setattr(cre_enrich, "_psql_query", lambda db_url, sql: list(claimed))
    monkeypatch.setattr(cre_enrich, "_psql_exec",
                        lambda db_url, sql: calls["exec_sql"].append(sql))

    claimed_urls = [t[3] for t in claimed]

    class _Proc:
        def __init__(self, rc):
            self.returncode = rc

    def _fake_subprocess_run(argv, **kwargs):
        if "collect.ts" in argv:
            out_path = argv[argv.index("--out") + 1]
            payload = ({"listings": [{"url": u} for u in claimed_urls]}
                       if write_enriched == "__unset__" else write_enriched)
            if payload is not None:
                with open(out_path, "w") as f:
                    json.dump(payload, f)
            return _Proc(collect_rc)
        if any(str(a).endswith("om_parse.py") for a in argv):
            calls["om_parse_called"] = True
            calls["om_parse_argv"] = list(argv)
            return _Proc(om_rc)
        if any(str(a).endswith("cre_ingest.py") for a in argv):
            calls["ingest_called"] = True
            return _Proc(0)
        return _Proc(0)

    monkeypatch.setattr(cre_enrich.subprocess, "run", _fake_subprocess_run)
    return calls
