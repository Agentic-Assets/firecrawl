"""Regression guards for the retired Firecrawl OM writer.

GetCREdata is the sole production OM extraction writer. These tests prove that
the collector cannot reactivate its old write path through a CLI flag, legacy
environment variable, or direct call to the OM runner.
"""

import inspect
from types import SimpleNamespace

import pytest

import cre_enrich
import om_parse


def test_cre_enrich_contains_no_om_parser_subprocess_path():
    run_source = inspect.getsource(cre_enrich.run)
    assert "om_parse.py" not in run_source
    assert "CRE_OM_PARSE" not in run_source
    assert not hasattr(cre_enrich, "build_om_parse_argv")


def test_cre_enrich_cli_rejects_the_retired_om_parse_flag(monkeypatch):
    monkeypatch.setattr("sys.argv", ["cre_enrich.py", "--om-parse"])
    with pytest.raises(SystemExit) as exc_info:
        cre_enrich.main()
    assert exc_info.value.code == 2


def test_om_parse_apply_returns_a_fail_closed_exit_code(capsys):
    assert om_parse.run(SimpleNamespace(apply=True)) == om_parse.RETIRED_WRITER_EXIT_CODE
    assert "sole production OM extraction writer" in capsys.readouterr().err


def test_om_parse_apply_cli_returns_a_fail_closed_exit_code(monkeypatch):
    monkeypatch.setattr("sys.argv", ["om_parse.py", "--apply"])
    with pytest.raises(SystemExit) as exc_info:
        om_parse.main()
    assert exc_info.value.code == om_parse.RETIRED_WRITER_EXIT_CODE


def test_om_parse_dry_run_can_still_show_candidate_sql_without_connecting(capsys):
    args = SimpleNamespace(
        apply=False,
        sources="cbre,jll",
        limit=1,
        dry_run=True,
        show_sql=True,
    )
    assert om_parse.run(args) == 0
    captured = capsys.readouterr()
    assert "dry-run (--show-sql): not connecting" in captured.err
    assert "SELECT" in captured.out
