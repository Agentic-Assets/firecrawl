"""Bounded signals target isolated test processes and temporary leases only."""

import signal
import subprocess
import sys
from pathlib import Path

import cre_checkpoint_refresh as refresh
import pytest


@pytest.mark.parametrize("resource", [False, True])
def test_sigterm_during_main_thread_trip_lock_releases_lease(tmp_path, resource):
    script = """
import os, signal, sys
from pathlib import Path
import cre_checkpoint_refresh as refresh
from cre_memory_guard import HostMemoryGuard
root = Path(sys.argv[1])
resource = sys.argv[2] == 'True'
cpu = refresh.HostCpuGuard(log_path=root / 'cpu.jsonl')
memory = HostMemoryGuard(log_path=root / 'memory.jsonl', on_trip=lambda **_: None)
signal.signal(signal.SIGTERM, refresh.checkpoint_sigterm_handler)
if resource:
    refresh._set_cpu_guard_trip('synthetic memory veto',
        reason_code='host_memory_critical', host_memory_pressure_level=4)
try:
    with refresh.resource_guard_boundary(memory, cpu):
        with refresh.SharedLock(root / '.cre.lock'):
            with refresh._CPU_GUARD_TRIP_LOCK:
                os.kill(os.getpid(), signal.SIGTERM)
except refresh.CpuGuardTrip:
    details = refresh._peek_cpu_guard_trip_details()
    assert resource and details.reason_code == 'host_memory_critical'
    assert details.host_memory_pressure_level == 4
    raise SystemExit(75)
except KeyboardInterrupt:
    assert not resource
    raise SystemExit(130)
raise AssertionError('signal did not interrupt test process')
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), str(resource)],
        cwd=Path(refresh.__file__).parent,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == (75 if resource else 130), completed.stderr
    assert not (tmp_path / ".cre.lock").exists()
    # The persistent authority is reusable after the interrupted owner exits.
    with refresh.SharedLock(tmp_path / ".cre.lock"):
        pass


def test_sigterm_handler_does_not_classify_or_access_trip_lock(monkeypatch):
    def forbidden():
        raise AssertionError("handler must not inspect trip state")

    monkeypatch.setattr(refresh, "_peek_cpu_guard_trip", forbidden)
    with pytest.raises(KeyboardInterrupt) as caught:
        refresh.checkpoint_sigterm_handler(signal.SIGTERM, None)
    assert type(caught.value) is KeyboardInterrupt


@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.parametrize("reason_code", ["host_cpu_sustained", "host_memory_critical"])
def test_logged_command_classifies_recorded_trip_before_cleanup(
    tmp_path, monkeypatch, cleanup_fails, reason_code
):
    class Command:
        pid = 424242

        def __init__(self):
            self.waits = []

        def wait(self, timeout=None):
            self.waits.append(timeout)
            if timeout is None:
                refresh.checkpoint_sigterm_handler(signal.SIGTERM, None)
            return 0

    command = Command()
    signals = []
    refresh._set_cpu_guard_trip("synthetic resource stop", reason_code=reason_code)

    def terminate(*values):
        signals.append(values)
        if cleanup_fails:
            raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(refresh.subprocess, "Popen", lambda *_a, **_kw: command)
    monkeypatch.setattr(refresh.os, "killpg", terminate)
    try:
        with pytest.raises(refresh.CpuGuardTrip, match="synthetic resource stop"):
            refresh._run_logged_command(["synthetic"], tmp_path / "command.log")
        assert signals == [(command.pid, signal.SIGINT)]
        assert command.waits == [None, refresh.COMMAND_INTERRUPT_GRACE_SECONDS]
        details = refresh._peek_cpu_guard_trip_details()
        assert details.owned_processes_reaped
        assert details.evidence_valid is not cleanup_fails
        assert details.reason_code == (
            reason_code
            if not cleanup_fails
            else "host_memory_evidence_failed"
            if reason_code.startswith("host_memory_")
            else "host_cpu_evidence_failed"
        )
    finally:
        refresh._clear_cpu_guard_trip()
