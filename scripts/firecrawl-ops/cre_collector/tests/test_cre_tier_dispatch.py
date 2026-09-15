"""Subprocess contracts for the launchd tier authority handoff."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_tier_dispatch as dispatch
import pytest

COLLECTOR = Path(__file__).resolve().parent.parent
RUN_TIER = COLLECTOR / "launchd" / "cre_run_tier.sh"


def _dispatch_program(lock_path: Path, command: list[str]) -> str:
    return f"""
from pathlib import Path
import cre_tier_dispatch as dispatch
raise SystemExit(dispatch.run_tier('monitor', lock_path=Path({str(lock_path)!r}), command={command!r}))
"""


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def _sleeping_command(ready: Path) -> list[str]:
    return [
        sys.executable,
        "-c",
        f"import pathlib, time; pathlib.Path({str(ready)!r}).write_text('ready'); time.sleep(30)",
    ]


def test_tier_refuses_while_recovery_sync_is_held(tmp_path):
    lock_path = tmp_path / ".cre.lock"
    sync = refresh.acquire_quarantine_recovery_sync(lock_path)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                _dispatch_program(
                    lock_path, [sys.executable, "-c", "raise SystemExit(9)"]
                ),
            ],
            cwd=COLLECTOR,
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        refresh.release_quarantine_recovery_sync(sync)

    assert result.returncode == 0
    assert "tier/recovery is active; skipping monitor" in result.stderr
    assert not lock_path.exists()


def test_recovery_sync_refuses_while_tier_holds_authority(tmp_path):
    lock_path = tmp_path / ".cre.lock"
    ready = tmp_path / "ready"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _dispatch_program(lock_path, _sleeping_command(ready)),
        ],
        cwd=COLLECTOR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for(ready)
        with pytest.raises(refresh.LockHeldError):
            refresh.acquire_quarantine_recovery_sync(lock_path)
    finally:
        process.terminate()
        process.wait(timeout=10)

    assert process.returncode == 143
    successor = refresh.SharedLock(lock_path)
    successor.acquire()
    successor.release()
    assert not lock_path.exists()


def test_tier_signal_forwards_then_releases_canonical_lock(tmp_path):
    lock_path = tmp_path / ".cre.lock"
    ready = tmp_path / "ready"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _dispatch_program(lock_path, _sleeping_command(ready)),
        ],
        cwd=COLLECTOR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _wait_for(ready)
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)

    assert process.returncode == 143
    successor = refresh.SharedLock(lock_path)
    successor.acquire()
    successor.release()
    assert not lock_path.exists()


def test_shell_private_worker_requires_inherited_descriptor_proof():
    result = subprocess.run(
        ["/bin/bash", str(RUN_TIER), "--already-locked", "monitor"],
        cwd=COLLECTOR,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "governed inherited lock proof failed" in result.stderr


def test_shell_has_no_raw_canonical_lock_acquisition_or_cleanup():
    source = RUN_TIER.read_text(encoding="utf-8")

    assert 'exec python3 "${COLLECTOR_DIR}/cre_tier_dispatch.py" "$@"' in source
    assert "--verify-inherited-lock" in source
    for forbidden in (
        "acquire_lock()",
        "_lock_interlocked()",
        'mkdir "${LOCKDIR}"',
        'rm -rf "${LOCKDIR}"',
        "LOCKDIR=",
        ".cre.lock.reclaim",
    ):
        assert forbidden not in source


def test_launchd_shell_surface_has_no_raw_canonical_lock_mutation():
    raw_lock_mutation = re.compile(
        r"(?:mkdir|rm|mv|cp)\b[^\n]*\.cre\.lock|\.cre\.lock[^\n]*(?:mkdir|rm|mv|cp)\b"
    )

    for script in (COLLECTOR / "launchd").glob("*.sh"):
        assert raw_lock_mutation.search(script.read_text(encoding="utf-8")) is None


def test_inherited_authority_proof_binds_both_private_descriptors(
    tmp_path, monkeypatch
):
    lock_path = tmp_path / ".cre.lock"
    lock = refresh.SharedLock(lock_path)
    lock.acquire()
    try:
        monkeypatch.setattr(dispatch, "canonical_shared_lock_dir", lambda: lock_path)
        dispatch.verify_inherited_lock(
            {
                "CRE_TIER_LOCK_AUTHORITY_FD": str(lock.authority_fd),
                "CRE_TIER_LOCK_SYNC_FD": str(lock.recovery_sync_fd),
                "CRE_TIER_LOCK_TOKEN": str(lock.authority_token),
                "CRE_TIER_LOCK_GENERATION": str(lock.authority_generation),
                "CRE_TIER_LOCK_OWNER_PID": str(os.getpid()),
            }
        )
    finally:
        lock.release()
