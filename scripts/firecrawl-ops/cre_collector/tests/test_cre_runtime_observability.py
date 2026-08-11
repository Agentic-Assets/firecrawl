"""Pure contracts for bounded CRE CPU incident evidence."""

from __future__ import annotations

import cre_runtime_observability as observability


def test_parse_ps_sorts_and_never_exposes_command_arguments():
    rows = observability.parse_ps(
        "10 1 2.5 100 00:01 /usr/bin/python\n"
        "11 1 9.5 200 00:02 /Applications/Codex.app/Contents/MacOS/Codex\n"
        "malformed\n"
    )
    assert [row["pid"] for row in rows] == [11, 10]
    assert rows[0]["command"] == "Codex"


def test_sanitize_context_drops_urls_tokens_and_unknown_phases():
    assert observability.sanitize_context(
        {
            "phase": "collect",
            "source": "jll",
            "child_pid": 55,
            "url": "https://user:token@example.test",
            "authorization": "Bearer secret",
        }
    ) == {"phase": "collect", "source": "jll", "child_pid": 55}
    assert observability.sanitize_context({"phase": "https://secret.test"}) == {}


def test_parse_docker_stats_accepts_only_expected_json_rows():
    rows = observability.parse_docker_stats(
        '{"Name":"firecrawl-api-1","CPUPerc":"2.00%","MemUsage":"1MiB / 8GiB","PIDs":"12"}\nnot-json\n'
    )
    assert rows == [{"name": "firecrawl-api-1", "cpu_percent": "2.00%", "mem_usage": "1MiB / 8GiB", "pids": "12"}]
