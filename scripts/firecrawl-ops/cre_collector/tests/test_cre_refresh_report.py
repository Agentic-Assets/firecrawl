import json
import sys

import pytest

import cre_refresh_report as report


def test_validate_since_normalizes_z_suffix():
    assert report.validate_since("2026-07-18T13:00:00Z") == "2026-07-18T13:00:00+00:00"


def test_validate_since_rejects_naive_timestamp():
    with pytest.raises(ValueError, match="timezone"):
        report.validate_since("2026-07-18T13:00:00")


def test_build_queries_keeps_since_parameter_out_of_sql_literal():
    queries = report.build_queries()
    assert ":since" in queries["inventory"]
    assert "2026-" not in "\n".join(queries.values())
    assert "supported_active" in queries["registry_coverage"]
    assert "'cbre-dealflow'" in queries["registry_coverage"]
    assert "$.**.detailError" in queries["inventory_by_source"]


def test_render_markdown_includes_inventory_and_queue():
    data = {
        "since": "2026-07-18T13:00:00+00:00",
        "generated_at": "2026-07-18T14:00:00+00:00",
        "inventory": [{"active_total": "10", "created_since": "2"}],
        "inventory_by_source": [{"source_key": "svn", "active": "7"}],
        "registry_coverage": [{"supported_active": "7", "unsupported_active": "3"}],
        "events_by_type": [{"event_type": "new", "count": "2"}],
        "source_index": [{"seen_since": "8"}],
        "queue_by_source": [{"source_key": "svn", "pending": "4"}],
        "details": [{"contacts": "5"}],
        "ownership_invariants": [{"om_facts": "12", "market_index": "3"}],
    }
    text = report.render_markdown(data)
    assert "## Inventory" in text
    assert "| 10 | 2 |" in text
    assert "| svn | 4 |" in text


def test_run_query_uses_read_only_and_quotes_since(monkeypatch):
    calls = {}

    class Proc:
        returncode = 0
        stdout = "active_total\n10\n"
        stderr = ""

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        calls["input"] = kwargs["input"]
        calls["env"] = kwargs["env"]
        return Proc()

    monkeypatch.setattr(report.subprocess, "run", fake_run)
    rows = report.run_query(
        "psql",
        "postgres://user:secret@db.test/cre",
        "SELECT :since AS x;",
        "2026-07-18T13:00:00+00:00",
    )
    assert rows == [{"active_total": "10"}]
    assert "BEGIN READ ONLY" in calls["input"]
    assert "'2026-07-18T13:00:00+00:00'::timestamptz" in calls["input"]
    assert ":since" not in calls["input"]
    assert all("secret" not in arg for arg in calls["argv"])
    assert calls["env"]["PGHOST"] == "db.test"
    assert calls["env"]["PGDATABASE"] == "cre"
    assert calls["env"]["PGPASSWORD"] == "secret"


def test_main_writes_json_without_credentials(monkeypatch, tmp_path):
    out = tmp_path / "report.json"
    monkeypatch.setattr(report, "load_db_url", lambda _path: ("postgres://secret", "/tmp/env"))
    monkeypatch.setattr(report, "find_psql", lambda: "psql")
    monkeypatch.setattr(report, "run_query", lambda *_args: [])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cre_refresh_report.py",
            "--since",
            "2026-07-18T13:00:00Z",
            "--out",
            str(out),
        ],
    )
    report.main()
    payload = json.loads(out.read_text())
    assert payload["since"] == "2026-07-18T13:00:00+00:00"
    assert "secret" not in out.read_text()
