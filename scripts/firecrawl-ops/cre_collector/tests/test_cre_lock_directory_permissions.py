"""Owned temporary locks and synthetic workers only; no provider or database."""

import contextlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import cre_checkpoint_refresh as checkpoint
import cre_stop_guarded_tier as stop
from test_cre_stop_guarded_tier import OWNER_SCRIPT


@contextlib.contextmanager
def temporary_umask(value):
    previous = os.umask(value)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.mark.parametrize("mask", [0o002, 0o022, 0o077])
def test_fresh_nested_lock_and_immediate_parent_are_private(tmp_path, mask):
    lock_path = tmp_path / "new-outer" / "daily" / "private.lock"
    with temporary_umask(mask), checkpoint.SharedLock(lock_path):
        assert stat.S_IMODE(lock_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o700


@pytest.mark.parametrize("mask", [0o002, 0o022, 0o077])
def test_existing_safe_parent_keeps_mode_and_inode(tmp_path, mask):
    parent = tmp_path / "existing"
    parent.mkdir()
    parent.chmod(0o755)
    before = parent.stat()
    with temporary_umask(mask), checkpoint.SharedLock(parent / "private.lock"):
        assert stat.S_IMODE(parent.stat().st_mode) == 0o755
        assert parent.stat().st_ino == before.st_ino
        assert stat.S_IMODE((parent / "private.lock").stat().st_mode) == 0o700
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755


@pytest.mark.parametrize("kind", ["group_writable", "symlink", "wrong_owner"])
def test_unsafe_existing_parent_refuses_before_authority_mutation(
    tmp_path, monkeypatch, kind
):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    parent = target
    if kind == "group_writable":
        target.chmod(0o775)
    elif kind == "symlink":
        parent = tmp_path / "alias"
        parent.symlink_to(target, target_is_directory=True)
    else:
        original = type(target).lstat

        def observed(path):
            result = original(path)
            if path == target:
                return SimpleNamespace(st_mode=result.st_mode, st_uid=os.geteuid() + 1)
            return result

        monkeypatch.setattr(type(target), "lstat", observed)
    before = target.stat()
    with pytest.raises(checkpoint.LockHeldError):
        with checkpoint.SharedLock(parent / "private.lock"):
            pass
    assert list(target.iterdir()) == []
    after = target.stat()
    assert (after.st_ino, stat.S_IMODE(after.st_mode)) == (
        before.st_ino,
        stat.S_IMODE(before.st_mode),
    )
    if kind == "symlink":
        assert parent.is_symlink()


def test_existing_unsafe_held_lock_is_never_chmodded_or_admitted_for_stop(tmp_path):
    lock_path = tmp_path / "private.lock"
    with checkpoint.SharedLock(lock_path, recovery_required=True):
        lock_path.chmod(0o775)  # Explicit unsafe fixture, created by this test owner.
        before = lock_path.stat()
        with pytest.raises(stop.StopRefused, match="unsafe local evidence"):
            stop._authority(lock_path)
        with pytest.raises(checkpoint.LockHeldError):
            with checkpoint.SharedLock(lock_path):
                pass
        after = lock_path.stat()
        assert (after.st_ino, stat.S_IMODE(after.st_mode)) == (before.st_ino, 0o775)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux pidfd stop contract")
@pytest.mark.parametrize("mask", [0o002, 0o022, 0o077])
def test_real_cooperative_stop_works_with_inherited_umask(tmp_path, mask):
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        pytest.skip("pidfd APIs unavailable")
    # Reuse the real one-shot CLI fixture; only its process-local umask changes.
    script = "import os; os.umask(" + repr(mask) + ")\n" + OWNER_SCRIPT
    owner = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "child-ready").exists():
            if owner.poll() is not None or time.monotonic() >= deadline:
                pytest.fail("synthetic private owner did not become ready")
            time.sleep(0.01)
        (run_dir,) = (tmp_path / "runs").iterdir()
        rc, result = stop.stop_run(
            run_dir.name,
            40,
            _out_root=tmp_path / "runs",
            _lock_path=tmp_path / "private.lock",
        )
        assert rc == 0 and result["signal_sent"]
        assert result["recovery_fence"] == "retained"
        assert owner.wait(timeout=3) == 130
        receipt = json.loads((run_dir / "receipt.json").read_text())
        assert receipt["guards_stopped"] is True
        assert receipt["local_settlement"]["state"] == "confirmed"
        assert receipt["recovery_clear_eligible"] is False
        assert all(
            group["leader_reaped"] and group["group_gone"]
            for group in receipt["local_settlement"]["groups"]
        )
        assert stat.S_IMODE((tmp_path / "private.lock").stat().st_mode) == 0o700
    finally:
        if owner.poll() is None:
            # This Popen is the actual parent of the unreaped synthetic owner.
            # Cooperatively settle it even when the pre-fix stop correctly refused.
            owner.send_signal(signal.SIGTERM)
        owner.communicate(timeout=30)
