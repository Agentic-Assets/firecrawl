"""Fault tests for exact foreground process-group settlement, never live owners."""

import os
import signal
import subprocess
import sys
import time

import cre_owned_processes as owned
import pytest


class Process:
    pid = 987654

    def __init__(self):
        self.returncode = None
        self.waits = []

    def wait(self, timeout=None):
        assert timeout is not None
        self.waits.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fixture", timeout)
        return self.returncode


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(owned.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        owned.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    return now


def test_leader_exit_does_not_certify_group_settlement(monkeypatch, clock):
    proc = Process()
    proc.returncode = 0
    live = [True]
    signals = []
    monkeypatch.setattr(owned, "_group_alive", lambda pgid: live[0])

    def kill(pgid, sent):
        signals.append((pgid, sent))
        if sent == signal.SIGKILL:
            live[0] = False

    monkeypatch.setattr(owned.os, "killpg", kill)
    result = owned.settle_owned_groups(
        [owned.OwnedProcessGroup.from_spawn(proc)], grace_seconds=0.2
    )
    assert result.settled
    assert signals == [(proc.pid, signal.SIGINT), (proc.pid, signal.SIGKILL)]
    assert 0.2 <= clock[0] < 0.3
    assert result.groups[0]["group_gone"]


def test_all_groups_share_one_deadline_and_final_wait_is_bounded(monkeypatch, clock):
    processes = [Process(), Process()]
    processes[1].pid += 1
    monkeypatch.setattr(owned, "_group_alive", lambda pgid: True)
    monkeypatch.setattr(owned.os, "killpg", lambda *_args: None)
    result = owned.settle_owned_groups(
        [owned.OwnedProcessGroup.from_spawn(p) for p in processes],
        grace_seconds=0.2,
        kill_seconds=0.1,
    )
    assert not result.settled
    assert clock[0] == pytest.approx(0.3)
    assert all(p.waits and all(v == 0 for v in p.waits) for p in processes)
    assert all(not item["leader_reaped"] for item in result.groups)


def test_already_gone_group_is_never_signalled_again(monkeypatch, clock):
    proc = Process()
    proc.returncode = 0
    signals = []

    def vanished(pgid, sent):
        signals.append(sent)
        raise ProcessLookupError

    monkeypatch.setattr(owned.os, "killpg", vanished)
    result = owned.settle_owned_groups(
        [owned.OwnedProcessGroup.from_spawn(proc)], grace_seconds=0.2
    )
    assert result.settled
    assert signals == [signal.SIGINT]


def test_identity_change_stays_unknown_and_never_signals(monkeypatch, clock):
    proc = Process()
    group = owned.OwnedProcessGroup.from_spawn(proc)
    proc.pid += 1
    monkeypatch.setattr(
        owned.os,
        "killpg",
        lambda *_args: pytest.fail("identity ambiguity must not signal"),
    )
    result = owned.settle_owned_groups([group], grace_seconds=0.2)
    assert not result.settled
    assert result.errors == ("group_identity_changed",)


def test_reap_exception_cannot_skip_remaining_owned_group(monkeypatch, clock):
    one, two = Process(), Process()
    two.pid += 1
    two.returncode = 0
    one.wait = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("broken adapter"))
    monkeypatch.setattr(owned, "_group_alive", lambda pgid: False)
    monkeypatch.setattr(owned.os, "killpg", lambda *_args: None)
    result = owned.settle_owned_groups(
        [owned.OwnedProcessGroup.from_spawn(p) for p in (one, two)],
        grace_seconds=0,
        kill_seconds=0,
    )
    assert not result.settled
    assert result.groups[1]["leader_reaped"]
    assert result.errors == ("leader_reap_failed",)


def test_real_leader_first_stubborn_grandchild_and_unrelated_sentinel(tmp_path):
    marker = tmp_path / "child.pid"
    child_code = "import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); time.sleep(60)"
    leader_code = (
        "import subprocess,sys; from pathlib import Path; "
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        f"Path({str(marker)!r}).write_text(str(p.pid))"
    )
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code], start_new_session=True
    )
    group = owned.OwnedProcessGroup.from_spawn(leader)
    try:
        leader.wait(timeout=3)
        assert marker.exists()
        start = time.monotonic()
        result = owned.settle_owned_groups([group], grace_seconds=0.15, kill_seconds=2)
        assert time.monotonic() - start < 2.5
        assert result.settled, result
        assert sentinel.poll() is None
        with pytest.raises(ProcessLookupError):
            os.killpg(group.pgid, 0)
    finally:
        for process in (leader, sentinel):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)


@pytest.mark.parametrize("bad", [0, 1, -1, True])
def test_reject_ambiguous_group_identity(bad):
    proc = Process()
    proc.pid = bad
    with pytest.raises(ValueError, match="ambiguous"):
        owned.OwnedProcessGroup.from_spawn(proc)


def test_unknown_cleanup_preserves_operator_recovery_lease(tmp_path):
    import cre_checkpoint_refresh as refresh

    lock_path = tmp_path / ".cre.lock"
    refresh._clear_cpu_guard_trip()
    with pytest.raises(refresh.ProcessCleanupUnknown), refresh.SharedLock(lock_path):
        raise refresh.ProcessCleanupUnknown("owned process cleanup_unknown")
    assert lock_path.exists()
    with pytest.raises(refresh.LockHeldError), refresh.SharedLock(lock_path):
        pytest.fail("uncertain owned work admitted another owner")


def test_unknown_cleanup_manifest_cannot_resume(tmp_path):
    import json

    import cre_checkpoint_refresh as refresh

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"error": "owned process cleanup_unknown"}))
    with pytest.raises(refresh.ProcessCleanupUnknown, match="manual"):
        refresh.load_resume_manifest(
            manifest, git_sha="fake", sources=("cbre",), page_cap=400, concurrency=2
        )


def test_real_series_cleanup_probes_descendants_after_leader_exits(
    tmp_path, monkeypatch
):
    import cre_checkpoint_series as series

    monkeypatch.setattr(series, "SERIES_INTERRUPT_GRACE_SECONDS", 0.1)
    marker = tmp_path / "ready"
    child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    code = (
        "import subprocess,sys; from pathlib import Path; "
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
        f"Path({str(marker)!r}).write_text(str(p.pid))"
    )
    leader = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    leader._cre_owned_group = owned.OwnedProcessGroup.from_spawn(leader)
    try:
        leader.wait(timeout=3)
        assert marker.exists()
        series._terminate_child(leader)
        with pytest.raises(ProcessLookupError):
            os.killpg(leader.pid, 0)
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        leader.wait(timeout=3)


def test_unknown_series_cleanup_cannot_resume(tmp_path):
    import json

    import cre_checkpoint_series as series

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"error": "owned process cleanup_unknown"}))
    with pytest.raises(series.SeriesError, match="manual"):
        series.load_resume_manifest(manifest, git_sha="fake", config={})
