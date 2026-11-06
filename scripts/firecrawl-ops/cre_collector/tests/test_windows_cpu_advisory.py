"""WSL CPU observations must not prevent checkpoint work."""
import json
from contextlib import nullcontext

import pytest
import cre_checkpoint_refresh as refresh
from cre_memory_guard import HostMemoryGuard


@pytest.mark.parametrize("failure", [99.0, float("nan"), refresh.CpuTelemetryError("stale")])
def test_wsl_checkpoint_bypasses_cpu_veto_but_starts_memory(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(refresh, "windows_wsl", lambda: True)
    monkeypatch.setattr(refresh, "git_identity", lambda: ("b" * 40, False))
    monkeypatch.setattr(refresh, "checkpoint_lock_dir", lambda _: tmp_path / "lock")
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _: {"algorithm": "sha256", "value": "a" * 64})
    monkeypatch.setattr(refresh, "observe_resource_usage", lambda *a, **kw: nullcontext())
    memory_started = []
    monkeypatch.setattr(HostMemoryGuard, "preflight", lambda _: 1)
    monkeypatch.setattr(HostMemoryGuard, "start", lambda _: memory_started.append(True))
    def forbidden(_):
        if isinstance(failure, Exception):
            raise failure
        return failure
    monkeypatch.setattr(refresh, "run_cpu_guard_preflight", forbidden)
    monkeypatch.setattr(refresh.HostCpuGuard, "start", lambda _: pytest.fail("CPU veto started"))
    commands = []
    def health(argv, *_args, **_kwargs):
        commands.append(argv)
        return 7
    monkeypatch.setattr(refresh, "run_command", health)
    with pytest.raises(refresh.GlobalStageError, match="healthcheck failed"):
        refresh.main(["--out-root", str(tmp_path / "runs"), "--sources", "cbre"])
    assert commands and memory_started == [True]
    manifest = json.loads(next((tmp_path / "runs").glob("*/manifest.json")).read_text())
    assert manifest["config"]["host_cpu_guard"]["action"] == "diagnostic_only"
    assert manifest["preflight"]["host_cpu_guard"]["log"] == "resource-usage.jsonl"


def test_non_wsl_guard_retains_interrupt_policy(monkeypatch):
    monkeypatch.setattr(refresh, "windows_wsl", lambda: False)
    assert refresh.cpu_guard_actions() == {"action": "interrupt_and_checkpoint", "telemetry_failure_action": "interrupt_and_checkpoint"}
