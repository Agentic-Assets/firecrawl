"""Contention types report the flock outcome, not writer identity or retry safety."""

from __future__ import annotations

import os
import select
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

import cre_checkpoint_refresh as refresh

COLLECTOR = Path(__file__).resolve().parent.parent


@contextmanager
def holder(code: str, path: Path):
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(path)],
        cwd=COLLECTOR, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready, "lock holder did not start"
        assert process.stdout.readline().strip() == "ready"
        yield process
    finally:
        if process.poll() is None:
            assert process.stdin is not None
            process.stdin.write("q")
            process.stdin.flush()
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        assert process.returncode == 0, (stdout, stderr)


@pytest.mark.parametrize("kind", ["writer", "recovery_synchronizer"])
def test_live_writer_and_recovery_holder_both_report_only_synchronizer_busy(tmp_path, kind):
    lock_path = tmp_path / ".cre.lock"
    setup = (
        "owner = r.SharedLock(p); owner.acquire()"
        if kind == "writer"
        else "fd = r.acquire_quarantine_recovery_sync(p)"
    )
    cleanup = (
        "owner.release()"
        if kind == "writer"
        else "r.release_quarantine_recovery_sync(fd)"
    )
    code = f'''
import sys
from pathlib import Path
import cre_checkpoint_refresh as r
p = Path(sys.argv[1])
{setup}
print("ready", flush=True)
sys.stdin.read(1)
{cleanup}
'''
    with holder(code, lock_path):
        with pytest.raises(refresh.LockFlockBusyError) as outcome:
            refresh.SharedLock(lock_path).acquire()
        assert isinstance(outcome.value, refresh.LockHeldError)
        assert outcome.value.lock_object == "recovery_synchronizer"
        assert str(outcome.value) == "CRE lock authority is held by another process"

    # The classified refusal neither claimed the lock nor left a new fence.
    successor = refresh.SharedLock(lock_path)
    successor.acquire()
    successor.release()


def test_authority_flock_busy_is_distinct_from_synchronizer_busy(tmp_path):
    lock_path = tmp_path / ".cre.lock"
    authority = refresh.SharedLock(lock_path).authority_path
    code = '''
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("ready", flush=True)
sys.stdin.read(1)
os.close(fd)
'''
    with holder(code, authority):
        with pytest.raises(refresh.LockFlockBusyError) as outcome:
            refresh.SharedLock(lock_path).acquire()
        assert outcome.value.lock_object == "authority"
        assert not lock_path.exists()


def test_crashed_recovery_owner_is_not_classified_as_busy(tmp_path):
    lock_path = tmp_path / ".cre.lock"
    code = '''
import os, sys
from pathlib import Path
import cre_checkpoint_refresh as r
owner = r.SharedLock(Path(sys.argv[1]), recovery_required=True)
owner.acquire()
os._exit(0)
'''
    process = subprocess.run([sys.executable, "-c", code, str(lock_path)], cwd=COLLECTOR,
                             capture_output=True, text=True, timeout=10, check=False)
    assert process.returncode == 0, process.stderr
    with pytest.raises(refresh.LockHeldError, match="requires operator recovery") as outcome:
        refresh.SharedLock(lock_path).acquire()
    assert not isinstance(outcome.value, refresh.LockFlockBusyError)
    assert refresh._lock_requires_operator_recovery(lock_path)


def test_malformed_authority_is_not_classified_as_busy(tmp_path):
    lock_path = tmp_path / ".cre.lock"
    authority = refresh.SharedLock(lock_path).authority_path
    authority.write_text("malformed authority\n")
    os.chmod(authority, 0o600)
    with pytest.raises(refresh.LockHeldError, match="malformed") as outcome:
        refresh.SharedLock(lock_path).acquire()
    assert not isinstance(outcome.value, refresh.LockFlockBusyError)
    assert not lock_path.exists()


def test_busy_synchronizer_does_not_hide_or_clear_recovery_fence(tmp_path):
    lock_path = tmp_path / ".cre.lock"
    owner = refresh.SharedLock(lock_path, recovery_required=True)
    owner.acquire()
    owner.release()
    assert refresh._lock_requires_operator_recovery(lock_path)
    code = '''
import sys
from pathlib import Path
import cre_checkpoint_refresh as r
fd = r.acquire_quarantine_recovery_sync(Path(sys.argv[1]))
print("ready", flush=True)
sys.stdin.read(1)
r.release_quarantine_recovery_sync(fd)
'''
    with holder(code, lock_path):
        with pytest.raises(refresh.LockFlockBusyError) as outcome:
            refresh.SharedLock(lock_path).acquire()
        assert outcome.value.lock_object == "recovery_synchronizer"
        assert refresh._lock_requires_operator_recovery(lock_path)
    with pytest.raises(refresh.LockHeldError, match="requires operator recovery") as outcome:
        refresh.SharedLock(lock_path).acquire()
    assert not isinstance(outcome.value, refresh.LockFlockBusyError)
    assert refresh._lock_requires_operator_recovery(lock_path)
