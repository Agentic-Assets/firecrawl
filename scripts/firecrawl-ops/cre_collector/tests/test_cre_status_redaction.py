"""Run the real shell stderr-summary block against synthetic private logs."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def test_shell_help_describes_readiness_probe_without_executing_checks():
    script = Path(__file__).resolve().parents[1] / "cre_status.sh"
    result = subprocess.run(
        ["bash", str(script), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "API readiness GET" in result.stdout
    assert "offline status (default)" not in result.stdout
    assert "\nset -uo pipefail\n" not in result.stdout
    assert "== launchd schedules ==" not in result.stdout


@pytest.mark.parametrize("known_signature", [False, True])
def test_shell_status_never_emits_raw_stderr(tmp_path, known_signature):
    script = Path(__file__).resolve().parents[1] / "cre_status.sh"
    block = (
        script.read_text(encoding="utf-8")
        .split('section "recent launchd stderr (redacted summary)"', 1)[1]
        .split('section "disappearance-only signal staleness"', 1)[0]
    )
    secret = "https://user:do-not-print@example.invalid/path?token=private-value"
    content = secret + "\n\x1b]2;spoofed\x07\n"
    if known_signature:
        content += "Operation not permitted\n"
    (tmp_path / "cre-daily.err.log").write_text(content, encoding="utf-8")
    result = subprocess.run(
        [
            "bash",
            "-c",
            'warn() { printf "%s\\n" "$1"; }; note() { printf "%s\\n" "$1"; };' + block,
        ],
        env={**os.environ, "OUT_DAILY": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert "raw contents withheld" in result.stdout
    assert "do-not-print" not in result.stdout + result.stderr
    assert "private-value" not in result.stdout + result.stderr
    assert "spoofed" not in result.stdout + result.stderr
    assert "\x1b" not in result.stdout + result.stderr
    assert ("TCC-126 signature" in result.stdout) == known_signature
