"""Run the real status command against private artifacts and offline tool stubs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest


@pytest.fixture
def status_host(tmp_path):
    root = tmp_path / "host"
    collector = root / "Documents" / "firecrawl" / "scripts" / "firecrawl-ops" / "cre_collector"
    collector.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "cre_status.sh", collector / "cre_status.sh")
    shutil.copyfile(Path(__file__).resolve().parents[1] / "cre_platform.py", collector / "cre_platform.py")
    (collector / "cre_ingest.py").write_text(
        'STATUS_SOURCE_PATHS = {"cbre": []}\n'
        'def load_db_url(_):\n    return "private-not-printed", "test-env-file"\n',
        encoding="utf-8",
    )
    tools = tmp_path / "bin"
    tools.mkdir()
    scripts = {
        "curl": 'printf \'%s\\n\' \'{"message":"Firecrawl API","documentation_url":"https://docs.firecrawl.dev"}\'\n',
        "git": 'case "$*" in\n'
        '  *--is-inside-work-tree*) echo true ;;\n'
        '  *symbolic-ref*) echo main ;;\n'
        '  *status*) ;;\n'
        '  *) echo 0123456789abcdef ;;\nesac\n',
        "launchctl": 'touch "$HOME/launchctl-called"\n'
        'if [ "$TEST_LAUNCHCTL" = present ]; then echo "- 126 ai.agentic.cre-monitor"; else exit 127; fi\n',
    }
    for name, content in scripts.items():
        path = tools / name
        path.write_text("#!/bin/bash\n" + content, encoding="utf-8")
        path.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(root),
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "TEST_LAUNCHCTL": "present",
        "CRE_ENV_FILE": "",
        "CRE_EXPECTED_SHA": "",
    }
    return collector, root, env


def run_status(status_host, *args):
    collector, _root, env = status_host
    return subprocess.run(
        ["bash", str(collector / "cre_status.sh"), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


@pytest.mark.parametrize("launchctl", ["present", "absent"])
def test_external_ignores_launchd_and_tcc_but_checks_runtime(status_host, launchctl):
    collector, root, env = status_host
    env["TEST_LAUNCHCTL"] = launchctl
    plist_dir = root / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True)
    (plist_dir / "ai.agentic.cre-monitor.plist").write_text("invalid plist", encoding="utf-8")
    daily = collector / "out" / "daily"
    daily.mkdir(parents=True)
    (daily / "cre-monitor.err.log").write_text("Operation not permitted\nprivate-secret", encoding="utf-8")
    result = run_status(status_host, "--scheduler", "external")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no lock held" in result.stdout
    assert "Firecrawl API ready" in result.stdout
    assert "POSTGRES_URL env file found" in result.stdout
    assert "checkout is clean" in result.stdout
    assert "no run artifacts yet" in result.stdout
    assert "launchd" not in result.stdout
    assert "TCC" not in result.stdout
    assert "private-secret" not in result.stdout + result.stderr
    assert not (root / "launchctl-called").exists()


def test_default_keeps_launchd_and_tcc_checks(status_host):
    _collector, root, _env = status_host
    result = run_status(status_host)
    assert result.returncode == 1
    assert "last exit 126" in result.stdout
    assert "clone under ~/Documents" in result.stdout
    assert "installed launchd template drift" in result.stdout
    assert (root / "launchctl-called").exists()


@pytest.mark.parametrize("tier", ["monitor", "enrich", "weekly"])
def test_external_stale_tier_is_a_problem_without_launchd(status_host, tier):
    collector, _root, _env = status_host
    relative = {"monitor": "monitor/monitor_sample.json", "enrich": "enrich/sample.json", "weekly": "daily/run_sample.log"}[tier]
    artifact = collector / "out" / relative
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    old = time.time() - 12 * 86400
    os.utime(artifact, (old, old))
    result = run_status(status_host, "--scheduler", "external")
    assert result.returncode == 1
    assert f"{tier}: STALE" in result.stdout
    assert "syntax error" not in result.stderr


def test_external_reports_failed_run_and_stale_lock(status_host):
    collector, _root, _env = status_host
    daily = collector / "out" / "daily"
    lock = daily / ".cre.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"99999999 {int(time.time())}\n", encoding="utf-8")
    monitor = collector / "out" / "monitor"
    monitor.mkdir()
    (monitor / "monitor_sample.json").write_text("{}", encoding="utf-8")
    (daily / "last_run_monitor.json").write_text(
        json.dumps({"rc": 1, "ok": False, "end": "2026-10-12T00:00:00Z", "consecutive_failures": 2}, separators=(",", ":")),
        encoding="utf-8",
    )
    result = run_status(status_host, "--scheduler", "external")
    assert result.returncode == 1
    assert "monitor: last run FAILED" in result.stdout
    assert "stale lock present" in result.stdout
    assert "2 consecutive failures" in result.stdout
    assert lock.is_dir()


@pytest.mark.parametrize("args", [("--scheduler",), ("--scheduler", "cron")])
def test_scheduler_argument_fails_before_checks(status_host, args):
    _collector, root, _env = status_host
    result = run_status(status_host, *args)
    assert result.returncode == 2
    assert "requires launchd or external" in result.stderr
    assert not (root / "launchctl-called").exists()
