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


def test_actual_nai_cli_psql_inherits_lock_after_owner_death(tmp_path):
    """Exercise CLI and psql subprocess with synthetic admission and SQL."""
    import hashlib
    import json
    from pathlib import Path

    collector = Path(__file__).resolve().parent.parent
    lock = tmp_path / ".cre.lock"
    root = tmp_path / "generation"
    (root / "sources").mkdir(parents=True)
    (root / "private").mkdir(mode=0o700)
    artifact = root / "sources" / "nai-global.json"
    artifact.write_text("{}")
    claims = root / "private" / "claims.json"
    binding = {"artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
    claims.write_text(json.dumps({"manifest": {"collector_git_sha": "a" * 40}, "binding": binding}))
    claims.chmod(0o600)
    attempt = "d" * 32
    manifest = {"run_id": root.name, "collector_git_sha": "a" * 40,
        "preflight": {"shared_lock": str(lock), "database_target": {"algorithm": "sha256", "value": "b" * 64}},
        "sources": {"nai-global": {"state": "ingesting", "subset_ingest_attempt_id": attempt,
            "ingest": {"attempt_id": attempt, "rc": None},
            "artifact": {"path": "sources/nai-global.json", "sha256": binding["artifact_sha256"]},
            "gate": {"current_inventory_proof": binding},
            "nai_post_claims": {"path": str(claims), "sha256": hashlib.sha256(claims.read_bytes()).hexdigest(),
                                "attempt_id": attempt}}}}
    (root / "manifest.json").write_text(json.dumps(manifest))
    ready, stop, done = (tmp_path / name for name in ("ready", "stop", "done"))
    shim = tmp_path / "psql"
    shim.write_text("""#!/usr/bin/env python3
import os,time
from pathlib import Path
for name in ('CRE_TIER_LOCK_AUTHORITY_FD', 'CRE_TIER_LOCK_SYNC_FD'): os.fstat(int(os.environ[name]))
Path(os.environ['NAI_TEST_READY']).touch()
while not Path(os.environ['NAI_TEST_STOP']).exists(): time.sleep(.02)
""")
    shim.chmod(0o700)
    cli = """
import os,runpy,sys
from pathlib import Path
import cre_checkpoint_refresh as r, cre_tier_dispatch as d
import cre_ingest as i, cre_nai_post_admission as a, cre_nai_post_partial as p
r.canonical_shared_lock_dir = d.canonical_shared_lock_dir = lambda: Path(os.environ['NAI_TEST_LOCK'])
r.git_identity = lambda: ('a'*40, False)
a.ingest_subset_context = lambda *x,**y: dict(staged=[],held=[],preimage=[],job_id='j',started_at='s',finished_at='f')
p.build_partial_sql = lambda *x: 'SELECT 1;'
i.load_db_url = lambda *x: ('postgresql://localhost/synthetic',None)
i.assert_expected_database_target = lambda *x: None
i.find_psql = lambda: os.environ['NAI_TEST_PSQL']
sys.argv = [os.environ['NAI_TEST_ENTRY'], '--in', os.environ['NAI_TEST_ARTIFACT'],
 '--nai-post-claims', os.environ['NAI_TEST_CLAIMS'], '--checkpoint-ingest-attempt', 'd'*32,
 '--expected-db-target-sha256', 'b'*64]
try:
    runpy.run_path(sys.argv[0],run_name='__main__')
except SystemExit as exc:
    Path(os.environ['NAI_TEST_DONE']).write_text(str(exc))
    raise
else:
    Path(os.environ['NAI_TEST_DONE']).write_text('unexpected success')
"""
    coordinator = """
import os,subprocess,sys
from pathlib import Path
import cre_checkpoint_refresh as r
lock=r.SharedLock(Path(os.environ['NAI_TEST_LOCK']),recovery_required=True)
lock.acquire()
env=dict(os.environ,CRE_TIER_LOCK_AUTHORITY_FD=str(lock.authority_fd),
 CRE_TIER_LOCK_SYNC_FD=str(lock.recovery_sync_fd),CRE_TIER_LOCK_TOKEN=lock.authority_token,
 CRE_TIER_LOCK_GENERATION=lock.authority_generation,CRE_TIER_LOCK_OWNER_PID=str(os.getpid()))
subprocess.Popen([sys.executable,'-c',os.environ['NAI_TEST_CLI']],env=env,
 pass_fds=(lock.authority_fd,lock.recovery_sync_fd)).wait()
"""
    env = {**os.environ, "NAI_TEST_LOCK": str(lock), "NAI_TEST_READY": str(ready), "NAI_TEST_STOP": str(stop),
           "NAI_TEST_DONE": str(done), "NAI_TEST_PSQL": str(shim), "NAI_TEST_CLI": cli,
           "NAI_TEST_ENTRY": str(collector / "cre_ingest.py"), "NAI_TEST_ARTIFACT": str(artifact),
           "NAI_TEST_CLAIMS": str(claims), "CRE_ACTIVATE_STATUS": "0"}
    parent = subprocess.Popen([sys.executable, "-c", coordinator], cwd=collector, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert parent.poll() is None and time.monotonic() < deadline, "CLI did not reach psql"
            time.sleep(.02)
        parent.kill()
        parent.wait(timeout=10)
        authority = lock.with_name(lock.name + refresh.LOCK_AUTHORITY_SUFFIX)
        probe = os.open(authority, os.O_RDWR | os.O_NOFOLLOW)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            stop.touch()
            deadline = time.monotonic() + 20
            while not done.exists():
                assert time.monotonic() < deadline, "NAI child did not settle"
                time.sleep(.02)
            assert "refusing NAI post partial ingest" in done.read_text()
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
