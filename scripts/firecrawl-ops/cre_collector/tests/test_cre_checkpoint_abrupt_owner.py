"""Ordinary checkpoint ownership faults with bounded local children, never a DB.

Run only in a standalone no-hardlinks clone. Native fixtures exit by themselves;
no test signals a reaped numeric process group or an unrelated process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import cre_checkpoint_refresh as refresh
import pytest
from cre_validate import CHECKPOINT_PREFLIGHT_QUERY_NAMES


def configure_main(monkeypatch, tmp_path):
    monkeypatch.setattr(refresh, "git_identity", lambda: ("b" * 40, False))
    monkeypatch.setattr(
        refresh, "checkpoint_lock_dir", lambda _: tmp_path / ".cre.lock"
    )
    monkeypatch.setattr(
        refresh,
        "database_target_fingerprint",
        lambda _: {"algorithm": "sha256", "value": "a" * 64},
    )
    monkeypatch.setattr(refresh, "capture_runtime_configuration", lambda _: None)
    monkeypatch.setattr(
        refresh, "observe_resource_usage", lambda *_a, **_k: nullcontext()
    )
    monkeypatch.setattr(refresh, "save_report", lambda _: True)
    monkeypatch.setattr(refresh.HostMemoryGuard, "preflight", lambda _: 1)
    monkeypatch.setattr(refresh.HostMemoryGuard, "start", lambda _: None)
    monkeypatch.setattr(refresh.HostMemoryGuard, "stop", lambda _: None)
    monkeypatch.setattr(refresh, "run_cpu_guard_preflight", lambda _: 1.0)
    monkeypatch.setattr(refresh.HostCpuGuard, "start", lambda _: None)
    monkeypatch.setattr(refresh.HostCpuGuard, "stop", lambda _: None)
    monkeypatch.setattr(refresh, "record_scope_from_validation", lambda *_: None)
    monkeypatch.setattr(refresh, "prepare_sources_cohort", lambda *_a, **_k: [])
    monkeypatch.setattr(refresh, "run_aggregate_gate", lambda *_: None)
    monkeypatch.setattr(refresh, "ingest_admitted_sources", lambda *_: None)
    monkeypatch.setattr(refresh, "run_final_validation", lambda *_: None)

    def command(argv, _log, **_kwargs):
        if "--out" in argv:
            Path(argv[argv.index("--out") + 1]).write_text(
                json.dumps(
                    {
                        "ok": True,
                        "validation_profile": refresh.CHECKPOINT_PREFLIGHT_PROFILE,
                        "audit_scope": refresh.VALIDATION_AUDIT_SCOPES[
                            refresh.CHECKPOINT_PREFLIGHT_PROFILE
                        ],
                        "queries": {
                            **{name: [] for name in CHECKPOINT_PREFLIGHT_QUERY_NAMES},
                            "lifecycle_schema_contract": [
                                {"contract_item": name, "status": "ok"}
                                for name in refresh.LIFECYCLE_SCHEMA_CONTRACT_ITEMS
                            ],
                        },
                    }
                )
            )
        return 0

    monkeypatch.setattr(refresh, "run_command", command)
    return ["--out-root", str(tmp_path / "runs"), "--sources", "cbre"]


OWNER_PROGRAM = r"""
import json, os, sys, time
from contextlib import nullcontext
from pathlib import Path
import cre_checkpoint_refresh as r
from cre_validate import CHECKPOINT_PREFLIGHT_QUERY_NAMES
root, phase, shape = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
r.git_identity = lambda: ("b" * 40, False)
r.checkpoint_lock_dir = lambda _: root / ".cre.lock"
r.database_target_fingerprint = lambda _: {"algorithm":"sha256", "value":"a" * 64}
r.capture_runtime_configuration = lambda _: None
r.observe_resource_usage = lambda *_a, **_k: nullcontext()
r.save_report = lambda _: True
r.HostMemoryGuard.preflight = lambda _: 1
r.HostMemoryGuard.start = r.HostMemoryGuard.stop = lambda _: None
r.run_cpu_guard_preflight = lambda _: 1.0
r.HostCpuGuard.start = r.HostCpuGuard.stop = lambda _: None
r.record_scope_from_validation = lambda *_: None
r.prepare_sources_cohort = lambda *_a, **_k: []
r.run_aggregate_gate = r.ingest_admitted_sources = r.run_final_validation = lambda *_: None
def pause():
    (root / "ready").write_text("ready")
    time.sleep(5)
if phase == "before_dispatch":
    r.capture_runtime_configuration = lambda _: pause()
if phase == "after_barrier":
    r.SharedLock._acquire_with_authority = lambda _: pause()
if phase == "during_registration":
    original = r.OwnedProcessGroup.from_spawn
    def register(proc):
        until = time.monotonic() + 3
        while not (root / "native-ready").exists() and time.monotonic() < until:
            time.sleep(.01)
        pause()
        return original(proc)
    r.OwnedProcessGroup.from_spawn = register
if phase == "before_terminal":
    original_write = r.atomic_write_text
    def write(path, value):
        if path.name == "report.md": pause()
        return original_write(path, value)
    r.atomic_write_text = write
def command(argv, log, **kwargs):
    if phase in {"during_dispatch", "during_registration"}:
        return r._run_logged_command([sys.executable, str(root / "native.py"), shape], log)
    if "--out" in argv:
        Path(argv[argv.index("--out") + 1]).write_text(json.dumps({
            "ok": True, "validation_profile": r.CHECKPOINT_PREFLIGHT_PROFILE,
            "audit_scope": r.VALIDATION_AUDIT_SCOPES[r.CHECKPOINT_PREFLIGHT_PROFILE],
            "queries": {**{name: [] for name in CHECKPOINT_PREFLIGHT_QUERY_NAMES},
            "lifecycle_schema_contract": [
                {"contract_item": name, "status": "ok"}
                for name in r.LIFECYCLE_SCHEMA_CONTRACT_ITEMS
            ]}}))
    return 0
r.run_command = command
r.main(["--out-root", str(root / "runs"), "--sources", "cbre"])
"""

NATIVE_PROGRAM = r"""
import json, os, subprocess, sys, time
from pathlib import Path
root = Path(__file__).parent
# The psql-shaped fixture is deliberately just Python, with no socket or DB.
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)",
                          "psql-no-database" if sys.argv[1] == "psql" else "collector"])
(root / "native-ready").write_text(json.dumps([os.getpid(), child.pid]))
if not (root / "ready").exists(): (root / "ready").write_text("ready")
time.sleep(3)
child.wait()
"""


def wait_for(path, owner):
    deadline = time.monotonic() + 5
    while not path.exists() and owner.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), f"owner exited {owner.poll()} before fault boundary"


@pytest.mark.parametrize(
    "phase,shape",
    [
        ("before_dispatch", "collector"),
        ("after_barrier", "collector"),
        ("during_dispatch", "collector"),
        ("during_dispatch", "psql"),
        ("during_registration", "collector"),
        ("before_terminal", "collector"),
    ],
)
def test_real_main_abrupt_owner_death_blocks_successor(
    tmp_path, monkeypatch, phase, shape
):
    resume_argv = configure_main(monkeypatch, tmp_path)
    (tmp_path / "native.py").write_text(NATIVE_PROGRAM)
    sentinel = tmp_path / "unrelated"
    sentinel.write_bytes(b"preserve")
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(refresh.COLLECTOR_DIR)}
    owner = subprocess.Popen(
        [sys.executable, "-c", OWNER_PROGRAM, str(tmp_path), phase, shape],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for(tmp_path / "ready", owner)
        if phase in {"during_dispatch", "during_registration"}:
            wait_for(tmp_path / "native-ready", owner)
            for pid in json.loads((tmp_path / "native-ready").read_text()):
                os.kill(pid, 0)
        owner.kill()
        owner.wait(timeout=3)
        if phase in {"during_dispatch", "during_registration"}:
            # These are survivors after coordinator death, not merely children
            # observed before it. Both remain bounded by their natural lifetime.
            for pid in json.loads((tmp_path / "native-ready").read_text()):
                os.kill(pid, 0)
        for manifest in (tmp_path / "runs").glob("*/manifest.json"):
            # Preserve a prior report as well as the original manifest. Its
            # content is intentionally opaque to the refused successor.
            (manifest.parent / "report.md").write_bytes(b"previous report evidence\n")
        evidence = {
            path: path.read_bytes()
            for pattern in ("*/manifest.json", "*/report.md")
            for path in (tmp_path / "runs").glob(pattern)
        }
        successor = refresh.SharedLock(tmp_path / ".cre.lock")
        try:
            with pytest.raises(refresh.LockHeldError, match="recovery"):
                successor.acquire()
        finally:
            successor.release()
        for manifest in (tmp_path / "runs").glob("*/manifest.json"):
            with pytest.raises(refresh.LockHeldError, match="recovery"):
                refresh.main([*resume_argv, "--resume", str(manifest)])
        assert {
            path: path.read_bytes()
            for pattern in ("*/manifest.json", "*/report.md")
            for path in (tmp_path / "runs").glob(pattern)
        } == evidence
        assert sentinel.read_bytes() == b"preserve"
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=4)
        # Only fixture descendants remain after owner death, and they have a
        # fixed natural lifetime. Never send a post-reap numeric PGID signal.
        if (tmp_path / "native-ready").exists():
            time.sleep(3.1)


@pytest.mark.parametrize("performance_report_saved", [True, False])
def test_known_success_clears_fence_after_terminal_evidence(
    tmp_path, monkeypatch, performance_report_saved
):
    argv = configure_main(monkeypatch, tmp_path)
    monkeypatch.setattr(refresh, "save_report", lambda _: performance_report_saved)
    assert refresh.main(argv) == 0
    lock = refresh.SharedLock(tmp_path / ".cre.lock")
    lock.acquire()
    lock.release()


def assert_successor_fenced(tmp_path):
    successor = refresh.SharedLock(tmp_path / ".cre.lock")
    try:
        with pytest.raises(refresh.LockHeldError, match="recovery"):
            successor.acquire()
    finally:
        successor.release()


@pytest.mark.parametrize("target", ["authority", "lease", "parent_fsync"])
def test_publication_failure_never_dispatches_and_keeps_fence(
    tmp_path, monkeypatch, target
):
    argv = configure_main(monkeypatch, tmp_path)
    monkeypatch.setattr(
        refresh,
        "run_command",
        lambda *_a, **_kw: pytest.fail("dispatch before durable ownership"),
    )
    if target == "authority":
        original = refresh.SharedLock._write_owned_authority

        def write(lock):
            original(lock)
            raise OSError("authority acknowledgement failed")

        monkeypatch.setattr(refresh.SharedLock, "_write_owned_authority", write)
    elif target == "lease":
        original = refresh.atomic_write_text

        def write(path, value):
            if path.name == "lease":
                raise OSError("lease publication failed")
            return original(path, value)

        monkeypatch.setattr(refresh, "atomic_write_text", write)
    else:
        original = refresh.os.fsync
        parent_identity = tmp_path.stat().st_ino

        def fsync(descriptor):
            if os.fstat(descriptor).st_ino == parent_identity:
                raise OSError("parent namespace acknowledgement failed")
            original(descriptor)

        monkeypatch.setattr(refresh.os, "fsync", fsync)
    with pytest.raises(OSError, match="failed"):
        refresh.main(argv)
    assert_successor_fenced(tmp_path)


NESTED_WORKER_PROGRAM = r"""
import os, signal, sys, threading, time
from pathlib import Path
import cre_checkpoint_refresh as r
root, mode = Path(sys.argv[1]), sys.argv[2]
r.COMMAND_INTERRUPT_GRACE_SECONDS = .1
r.build_collect_argv = lambda *_a, **_kw: [sys.executable, str(root / "nested.py"), mode]
if mode in {"worker_killed", "known_interrupt"}:
    def fault():
        until = time.monotonic() + 4
        while not (root / "native-ready").exists() and time.monotonic() < until:
            time.sleep(.01)
        os.kill(os.getpid(), signal.SIGKILL if mode == "worker_killed" else signal.SIGINT)
    threading.Thread(target=fault, daemon=True).start()
sys.exit(r.main(sys.argv[3:]))
"""

NESTED_COLLECTOR_PROGRAM = r"""
import json, os, signal, subprocess, sys, time
from pathlib import Path
root, mode = Path(__file__).parent, sys.argv[1]
if mode == "known_failure": sys.exit(7)
if mode in {"known_interrupt", "cleanup_interrupt"}:
    signal.signal(signal.SIGINT, lambda *_: None)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
(root / "native-ready").write_text(json.dumps([os.getpid(), child.pid]))
if mode == "worker_killed":
    time.sleep(3)
    child.wait()
if mode in {"known_interrupt", "cleanup_interrupt"}: child.wait()
# In the other mode, the leader exits normally leaving its group present.
"""


@pytest.mark.parametrize(
    "mode",
    [
        "unknown_inner",
        "worker_killed",
        "known_failure",
        "known_interrupt",
        "cleanup_interrupt",
    ],
)
def test_real_nested_worker_boundary_retains_uncertain_outcome(
    tmp_path, monkeypatch, mode
):
    prepare = refresh.prepare_sources_cohort
    build = refresh.build_cohort_collect_worker_argv
    argv = configure_main(monkeypatch, tmp_path)
    monkeypatch.setattr(refresh, "prepare_sources_cohort", prepare)
    (tmp_path / "nested.py").write_text(NESTED_COLLECTOR_PROGRAM)

    def worker(*args, **kwargs):
        if mode == "known_interrupt":
            # A retry must wait for its own nested dispatch, not attempt one's
            # readiness marker and a signal before the internal entrypoint.
            (tmp_path / "native-ready").unlink(missing_ok=True)
        original = build(*args, **kwargs)
        return [
            sys.executable,
            "-c",
            NESTED_WORKER_PROGRAM,
            str(tmp_path),
            mode,
            *original[2:],
        ]

    monkeypatch.setattr(refresh, "build_cohort_collect_worker_argv", worker)
    if mode == "cleanup_interrupt":
        real_sleep = time.sleep
        interrupted = False

        def sleep(seconds):
            nonlocal interrupted
            if seconds == 0.1 and not interrupted:
                until = time.monotonic() + 4
                while (
                    not (tmp_path / "native-ready").exists()
                    and time.monotonic() < until
                ):
                    real_sleep(0.01)
                assert (tmp_path / "native-ready").exists()
                interrupted = True
                raise KeyboardInterrupt("bounded parent interruption")
            real_sleep(seconds)

        monkeypatch.setattr(refresh.time, "sleep", sleep)
        monkeypatch.setattr(refresh, "COHORT_INTERRUPT_GRACE_SECONDS", 1.0)
    try:
        run_argv = [*argv, "--source-workers", "2", "--attempts-per-source", "2"]
        if mode == "cleanup_interrupt":
            assert refresh.main(run_argv) == 130
        elif mode in {"known_failure", "known_interrupt"}:
            with pytest.raises(refresh.RefreshError):
                refresh.main(run_argv)
        else:
            # Unsafe cleanup evidence remains ineligible for automatic recovery,
            # even when guard shutdown replaces the original unknown exception.
            assert refresh.main(run_argv) == 75
        if mode in {"known_failure", "known_interrupt", "cleanup_interrupt"}:
            manifest = json.loads(
                next((tmp_path / "runs").glob("*/manifest.json")).read_text()
            )
            attempts = manifest["sources"]["cbre"]["attempts"]
            if mode == "cleanup_interrupt":
                assert manifest["status"] == "interrupted"
                assert len(attempts) == 1
            else:
                assert len(attempts) == 2
                expected_rc = (
                    refresh.COHORT_SETTLED_FAILURE_EXIT
                    if mode == "known_failure"
                    else refresh.COHORT_SETTLED_INTERRUPT_EXIT
                )
                assert {item["rc"] for item in attempts} == {expected_rc}
            lock = refresh.SharedLock(tmp_path / ".cre.lock")
            lock.acquire()
            lock.release()
        else:
            pids = json.loads((tmp_path / "native-ready").read_text())
            os.kill(pids[-1], 0)
            assert_successor_fenced(tmp_path)
    finally:
        if mode in {"unknown_inner", "worker_killed"}:
            time.sleep(3.1)


@pytest.mark.parametrize("target", ["manifest", "report", "clear"])
def test_terminal_evidence_or_clear_failure_keeps_fence(tmp_path, monkeypatch, target):
    argv = configure_main(monkeypatch, tmp_path)
    if target == "manifest":
        original = refresh.save_manifest

        def save(run_dir, manifest):
            if manifest["status"] in {"supported_scope_complete", "failed"}:
                raise OSError("terminal manifest failed")
            original(run_dir, manifest)

        monkeypatch.setattr(refresh, "save_manifest", save)
    elif target == "report":
        original = refresh.atomic_write_text

        def write(path, value):
            if path.name == "report.md":
                raise OSError("terminal report failed")
            return original(path, value)

        monkeypatch.setattr(refresh, "atomic_write_text", write)
    else:

        def clear(_lock):
            raise OSError("recovery clear failed")

        monkeypatch.setattr(refresh.SharedLock, "clear_recovery_requirement", clear)
    with pytest.raises(OSError, match="failed"):
        refresh.main(argv)
    assert_successor_fenced(tmp_path)


@pytest.mark.parametrize("target", ["manifest", "report"])
def test_transient_terminal_failure_stays_fenced_after_failed_evidence_saved(
    tmp_path, monkeypatch, target
):
    argv = configure_main(monkeypatch, tmp_path)
    injected = False
    if target == "manifest":
        original = refresh.save_manifest

        def save(run_dir, manifest):
            nonlocal injected
            if manifest["status"] == "supported_scope_complete" and not injected:
                injected = True
                raise OSError("one-shot terminal failure")
            original(run_dir, manifest)

        monkeypatch.setattr(refresh, "save_manifest", save)
    else:
        original = refresh.atomic_write_text

        def write(path, value):
            nonlocal injected
            if path.name == "report.md" and not injected:
                injected = True
                raise OSError("one-shot terminal failure")
            return original(path, value)

        monkeypatch.setattr(refresh, "atomic_write_text", write)
    with pytest.raises(OSError, match="one-shot"):
        refresh.main(argv)
    manifest_path = next((tmp_path / "runs").glob("*/manifest.json"))
    assert json.loads(manifest_path.read_text())["status"] == "failed"
    assert (manifest_path.parent / "report.md").is_file()
    assert_successor_fenced(tmp_path)


@pytest.mark.parametrize("masked_by_cpu", [False, True])
def test_unknown_settlement_keeps_fence_even_when_watchdog_replaces_error(
    tmp_path, monkeypatch, masked_by_cpu
):
    argv = configure_main(monkeypatch, tmp_path)

    def fail(*_a, **_kw):
        raise refresh.ProcessCleanupUnknown("fixture group remains")

    monkeypatch.setattr(refresh, "run_command", fail)
    if masked_by_cpu:

        def quiesce(*_guards):
            raise refresh.CpuGuardTrip("replacement watchdog outcome")

        monkeypatch.setattr(refresh, "quiesce_resource_guards", quiesce)
        assert refresh.main(argv) == 75
    else:
        with pytest.raises(refresh.ProcessCleanupUnknown, match="group remains"):
            refresh.main(argv)
    assert_successor_fenced(tmp_path)


def test_watchdog_still_alive_keeps_fence(tmp_path, monkeypatch):
    argv = configure_main(monkeypatch, tmp_path)
    monkeypatch.setattr(
        refresh.HostCpuGuard,
        "start",
        lambda guard: setattr(guard, "thread", SimpleNamespace(is_alive=lambda: True)),
    )
    assert refresh.main(argv) == 75
    assert_successor_fenced(tmp_path)


@pytest.mark.parametrize("probe", [True, "denied", "invalid_identity", "unreaped"])
def test_post_reap_presence_or_uncertainty_never_signals(monkeypatch, probe):
    group = refresh.OwnedProcessGroup(
        SimpleNamespace(pid=99999999, returncode=0), 99999999
    )
    if probe == "invalid_identity":
        group.pgid = os.getpgrp()
    if probe == "unreaped":
        group.process.returncode = None

    def observe(_pgid):
        if probe == "denied":
            raise PermissionError("presence cannot be proved")
        return True

    monkeypatch.setattr(refresh, "_group_alive", observe)
    monkeypatch.setattr(
        refresh.os,
        "killpg",
        lambda *_: pytest.fail("post-reap PGID signal"),
    )
    with pytest.raises(refresh.ProcessCleanupUnknown):
        refresh._require_completed_group_settlement(group)


def test_real_completed_leader_with_survivor_is_unknown(tmp_path):
    # The original owner is the only leader reaper. Its same-group grandchild
    # exits naturally, so the test never signals an ambiguous, reaped PGID.
    argv = [
        sys.executable,
        "-c",
        (
            "import subprocess,sys; "
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(1)'])"
        ),
    ]
    try:
        with pytest.raises(refresh.ProcessCleanupUnknown, match="group remains"):
            refresh._run_logged_command(argv, tmp_path / "command.log")
    finally:
        time.sleep(1.1)


@pytest.mark.parametrize("interrupt", [False, True])
def test_real_registration_failure_keeps_main_fenced(tmp_path, monkeypatch, interrupt):
    argv = configure_main(monkeypatch, tmp_path)
    (tmp_path / "native.py").write_text(NATIVE_PROGRAM)
    monkeypatch.setattr(
        refresh,
        "run_command",
        lambda _argv, log, **_kw: refresh._run_logged_command(
            [sys.executable, str(tmp_path / "native.py"), "collector"], log
        ),
    )
    spawned = []

    def register(process):
        spawned.append(process)
        wait_for(tmp_path / "native-ready", process)
        if interrupt:
            raise KeyboardInterrupt("registration interruption")
        raise ValueError("registration failed")

    monkeypatch.setattr(refresh.OwnedProcessGroup, "from_spawn", register)
    try:
        if interrupt:
            assert refresh.main(argv) == 130
        else:
            with pytest.raises(refresh.ProcessCleanupUnknown, match="dispatch"):
                refresh.main(argv)
        for pid in json.loads((tmp_path / "native-ready").read_text()):
            os.kill(pid, 0)
        assert_successor_fenced(tmp_path)
    finally:
        # The leader has a bounded natural exit; reap only our actual Popen.
        for process in spawned:
            process.wait(timeout=4)
