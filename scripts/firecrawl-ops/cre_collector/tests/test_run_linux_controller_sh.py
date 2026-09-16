"""Contracts for the generic C10 Linux-runner wrapper script.

These run the real script (no mocking) but only ever exercise its argument
parsing and usage output: no subcommand here touches Docker or the network,
so the tests are safe on any machine, Linux or not.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "capacity_c10"
    / "tools"
    / "run_linux_controller.sh"
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_script_exists_and_is_executable():
    assert _SCRIPT.is_file()
    assert _SCRIPT.stat().st_mode & 0o111, "run_linux_controller.sh must be executable"


@pytest.mark.parametrize("flag", ["-h", "--help", "help"])
def test_help_prints_usage_without_touching_docker(flag):
    result = _run(flag)
    assert result.returncode == 0
    assert "run_linux_controller.sh" in result.stdout
    assert "build" in result.stdout
    assert "exec CMD" in result.stdout


def test_no_subcommand_prints_usage_and_fails_closed():
    result = _run()
    assert result.returncode == 1
    assert "run_linux_controller.sh" in result.stdout


def test_unknown_subcommand_is_rejected_without_touching_docker():
    result = _run("definitely-not-a-real-subcommand")
    assert result.returncode == 1
    assert "unknown subcommand" in result.stderr


def test_exec_without_a_command_is_rejected():
    # `exec` alone must fail its own argument check before it ever reaches
    # `docker compose ... exec` (which would otherwise hang or error later).
    result = _run("exec")
    assert result.returncode == 1
    assert "missing command" in result.stderr


def test_script_only_references_the_c10_runner_compose_overlay():
    text = _SCRIPT.read_text(encoding="utf-8")
    assert "docker-compose.c10-runner.yaml" in text
    # Never the C10 sidecar overlay directly, and never the ordinary dev
    # stack: this wrapper owns exactly one compose file.
    assert "docker-compose.c10.yaml" not in text
    assert "docker-compose.yaml" not in text
