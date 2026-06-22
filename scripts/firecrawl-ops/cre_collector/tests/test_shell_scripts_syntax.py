"""
test_shell_scripts_syntax.py

Robustness guard (2026-06-14): every shell script shipped with the collector
must parse under `bash -n`. The pipeline's scheduling and run-health surface is
bash (cre_run_tier.sh dispatcher, cre_daily_update.sh runner, cre_status.sh
heartbeat, cre_setup.sh doctor, launchd/install_launchd.sh generator), so a
syntax slip in any of them silently breaks scheduled or manual runs in ways the
Python unit tests cannot see.

Discovery is dynamic: any *.sh added under cre_collector/ is covered
automatically (node_modules/ and out/ are excluded). Skips cleanly if bash is
unavailable on the runner.
"""

import shutil
import subprocess

import pytest

from pathlib import Path

_COLLECTOR = Path(__file__).resolve().parent.parent
_EXCLUDE_DIRS = {"node_modules", "out", "__pycache__", ".git"}


def _shell_scripts():
    scripts = []
    for p in sorted(_COLLECTOR.rglob("*.sh")):
        if any(part in _EXCLUDE_DIRS for part in p.relative_to(_COLLECTOR).parts):
            continue
        scripts.append(p)
    return scripts


_SCRIPTS = _shell_scripts()


def test_at_least_the_core_scripts_are_discovered():
    """Sanity: the known core scripts must be present (guards a broken glob)."""
    names = {p.name for p in _SCRIPTS}
    for required in (
        "cre_status.sh",
        "cre_daily_update.sh",
        "cre_setup.sh",
        "cre_run_tier.sh",
        "install_launchd.sh",
    ):
        assert required in names, f"{required} not discovered under {_COLLECTOR}"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
@pytest.mark.parametrize("script", _SCRIPTS, ids=[str(p.relative_to(_COLLECTOR)) for p in _SCRIPTS])
def test_shell_script_parses(script):
    """`bash -n <script>` must succeed (no syntax errors)."""
    result = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"bash -n failed for {script}:\n{result.stderr}"
