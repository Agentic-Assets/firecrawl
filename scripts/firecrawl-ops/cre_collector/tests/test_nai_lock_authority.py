"""NAI uses inherited canonical lock authority, not claims as write authority."""
import os
import subprocess
import sys
import time

import pytest

import cre_checkpoint_refresh as refresh
import cre_tier_dispatch as dispatch
from cre_posix import fcntl


def authority_env(lock):
    return {"CRE_TIER_LOCK_AUTHORITY_FD": str(lock.authority_fd),
            "CRE_TIER_LOCK_SYNC_FD": str(lock.recovery_sync_fd),
            "CRE_TIER_LOCK_TOKEN": lock.authority_token,
            "CRE_TIER_LOCK_GENERATION": lock.authority_generation,
            "CRE_TIER_LOCK_OWNER_PID": str(os.getpid())}


def test_nai_authority_requires_live_owner_and_direct_parent(tmp_path, monkeypatch):
    path = tmp_path / ".cre.lock"
    lock = refresh.SharedLock(path, recovery_required=True)
    lock.acquire()
    try:
        monkeypatch.setattr(dispatch, "canonical_shared_lock_dir", lambda: path)
        env = authority_env(lock)
        # A live checkpoint uses the recovery bit as its crash fence.
        monkeypatch.setattr(dispatch.os, "getppid", lambda: os.getpid())
        dispatch.verify_inherited_lock(env, allow_recovery_required=True, require_direct_parent=True)
        with pytest.raises(dispatch.TierDispatchError):
            dispatch.verify_inherited_lock(env, require_direct_parent=True)
        monkeypatch.setattr(dispatch.os, "getppid", lambda: os.getpid() + 1)
        with pytest.raises(dispatch.TierDispatchError, match="parent differs"):
            dispatch.verify_inherited_lock(env, allow_recovery_required=True, require_direct_parent=True)
        monkeypatch.setattr(dispatch.os, "getppid", lambda: os.getpid())
        with pytest.raises(dispatch.TierDispatchError):
            dispatch.verify_inherited_lock({**env, "CRE_TIER_LOCK_GENERATION": "stale"},
                                           allow_recovery_required=True, require_direct_parent=True)
        forged = os.open(lock.authority_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            with pytest.raises(dispatch.TierDispatchError):
                dispatch.verify_inherited_lock({**env, "CRE_TIER_LOCK_AUTHORITY_FD": str(forged)},
                                               allow_recovery_required=True, require_direct_parent=True)
        finally:
            os.close(forged)
        with pytest.raises(dispatch.TierDispatchError):
            dispatch.verify_inherited_lock({**env, "CRE_TIER_LOCK_AUTHORITY_FD": str(forged)},
                                           allow_recovery_required=True, require_direct_parent=True)
        original = tmp_path / "original-authority"
        replacement = tmp_path / "replacement-authority"
        replacement.write_text("replacement\n")
        replacement.chmod(0o600)
        os.rename(lock.authority_path, original)
        os.replace(replacement, lock.authority_path)
        try:
            with pytest.raises(dispatch.TierDispatchError, match="changed"):
                dispatch.verify_inherited_lock(env, allow_recovery_required=True, require_direct_parent=True)
        finally:
            os.replace(original, lock.authority_path)
    finally:
        lock.clear_recovery_requirement()
        lock.release()


def test_inherited_lock_survives_abrupt_coordinator_exit(tmp_path):
    """The DB child retains both locked open descriptions after parent death."""
    path = tmp_path / ".cre.lock"
    stop = tmp_path / "stop-child"
    child_script = r'''
import os, time
from pathlib import Path
import cre_tier_dispatch as dispatch
dispatch.canonical_shared_lock_dir = lambda: Path(os.environ['NAI_TEST_LOCK'])
dispatch.verify_inherited_lock(allow_recovery_required=True, require_direct_parent=True)
print('READY ' + str(os.getpid()), flush=True)
while not Path(os.environ['NAI_TEST_STOP']).exists(): time.sleep(.02)
'''
    parent_script = r'''
import os, subprocess, sys
from pathlib import Path
import cre_checkpoint_refresh as refresh
lock = refresh.SharedLock(Path(os.environ['NAI_TEST_LOCK']), recovery_required=True)
lock.acquire()
env = dict(os.environ, CRE_TIER_LOCK_AUTHORITY_FD=str(lock.authority_fd),
           CRE_TIER_LOCK_SYNC_FD=str(lock.recovery_sync_fd), CRE_TIER_LOCK_TOKEN=lock.authority_token,
           CRE_TIER_LOCK_GENERATION=lock.authority_generation, CRE_TIER_LOCK_OWNER_PID=str(os.getpid()))
child = subprocess.Popen([sys.executable, '-c', os.environ['NAI_TEST_CHILD']], env=env,
                         pass_fds=(lock.authority_fd, lock.recovery_sync_fd))
child.wait()
'''
    env = {**os.environ, "NAI_TEST_LOCK": str(path), "NAI_TEST_STOP": str(stop), "NAI_TEST_CHILD": child_script}
    parent = subprocess.Popen([sys.executable, "-c", parent_script], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        line = parent.stdout.readline().strip()
        assert line.startswith("READY "), line
        assert int(line.split()[1]) > 0
        parent.kill()
        parent.wait(timeout=10)
        authority = path.with_name(path.name + refresh.LOCK_AUTHORITY_SUFFIX)
        probe = os.open(authority, os.O_RDWR | os.O_NOFOLLOW)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            stop.touch()
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    assert time.monotonic() < deadline
                    time.sleep(.02)
        finally:
            os.close(probe)
    finally:
        stop.touch(exist_ok=True)
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
