"""Crash, replay, concurrency, and forensic contracts for quarantine recovery."""

from __future__ import annotations

import inspect
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import cre_capacity_runtime as runtime
import cre_checkpoint_refresh as checkpoint_refresh
import cre_quarantine_recovery as recovery
import pytest
from cre_capacity_runtime_test_support import capture, profile


@pytest.fixture(autouse=True)
def _recovery_tests_require_an_explicit_temporary_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirect the runtime wrapper's canonical lock resolver per test."""
    isolated = tmp_path / "isolated" / "out" / "daily" / ".cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda *_args: isolated)


def _historic_quarantine_pair(lock_path: Path) -> None:
    lock_path.mkdir(mode=0o700, parents=True)
    lock_path.chmod(0o700)
    owner = 99999999
    result_path = "/private/var/folders/test/pytest-of-operator/pytest-1/result.json"
    active = lock_path / "capacity-benchmark-active.json"
    active.write_text(
        json.dumps(
            {
                "kind": "cre_capacity_candidate_pair_active",
                "state": "active",
                "profile": "bold-jll-128",
                "pid": owner,
                "result_path": result_path,
            }
        ),
        encoding="utf-8",
    )
    active.chmod(0o600)
    quarantine = lock_path / "capacity-benchmark-quarantine.json"
    quarantine.write_text(
        json.dumps(
            {
                "kind": "cre_capacity_benchmark_lock_quarantine",
                "state": "quarantined",
                "reason": "candidate_baseline_rollback_failed",
                "lock_path": str(lock_path),
                "result_path": result_path,
            }
        ),
        encoding="utf-8",
    )
    quarantine.chmod(0o600)
    authority = lock_path.with_name(f"{lock_path.name}.authority")
    authority.write_text(f"{owner} {'x' * 32}\n", encoding="utf-8")
    authority.chmod(0o600)


def _guard_state(path: Path) -> dict[str, object]:
    """Read the latest valid append-only recovery journal record."""
    return recovery._read_recovery_guard(path)


def test_quarantine_recovery_state_machine_is_owned_by_its_dedicated_module() -> None:
    """Keep the runtime CLI wrapper thin and recovery mechanics independently testable."""
    runtime_source = Path(runtime.__file__).read_text(encoding="utf-8")
    recovery_source = Path(recovery.__file__).read_text(encoding="utf-8")

    assert "def _recover_quarantine_while_synchronized" not in runtime_source
    assert "def _recover_quarantine_while_synchronized" in recovery_source
    assert "class QuarantineRecoveryConfig" in recovery_source
    assert "class QuarantineRecoveryCallbacks" in recovery_source


def test_public_recovery_config_and_callbacks_support_offline_dry_run(
    tmp_path: Path,
) -> None:
    """The extracted module accepts observations without importing runtime control."""
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    _historic_quarantine_pair(lock_path)
    baseline = capture()
    baseline_profile, _ = profile()
    callbacks = recovery.QuarantineRecoveryCallbacks(
        runner=lambda *_args, **_kwargs: None,
        compose_endpoints=lambda _runner: baseline.public["endpoints"],
        settlement=lambda _runner, _endpoints: baseline.public["settlement"],
        load_baseline_profile=lambda: (baseline_profile, "offline-digest"),
        capture_runtime=lambda _runner: baseline,
        evaluate_state=runtime.evaluate_state,
        cpu_evidence=lambda: {"ok": True},
    )

    result = recovery.recover_quarantine(
        execute=False,
        config=recovery.QuarantineRecoveryConfig(lock_path=lock_path),
        callbacks=callbacks,
    )

    assert result["executed"] is False
    assert result["lock_path"] == str(lock_path)
    assert lock_path.is_dir()


def test_quarantine_recovery_dry_run_and_exact_pair_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    lock_path = tmp_path / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda _root: lock_path)
    _historic_quarantine_pair(lock_path)
    baseline = capture()
    monkeypatch.setattr(recovery, "recovery_cpu_evidence", lambda: {"ok": True})
    monkeypatch.setattr(
        runtime, "_compose_loopback_endpoints", lambda _r: baseline.public["endpoints"]
    )
    monkeypatch.setattr(
        runtime, "_settlement", lambda *_args: baseline.public["settlement"]
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda _r: baseline)
    dry = runtime.recover_quarantine(execute=False)
    assert dry["executed"] is False
    assert lock_path.is_dir()
    recovered = runtime.recover_quarantine(execute=True)
    archive = Path(str(recovered["archive"]))
    assert not lock_path.exists()
    assert not lock_path.with_name(f"{lock_path.name}.authority").exists()
    assert (archive / ".cre.lock").is_dir()
    assert (archive / ".cre.lock.authority").is_file()
    assert (
        json.loads((archive / "recovery-receipt.json").read_text())["phase"]
        == "archived"
    )
    guard = lock_path.parent / recovery.QUARANTINE_RECOVERY_GUARD
    assert _guard_state(guard)["phase"] == "completed"
    # A valid completed journal is retained as forensic evidence, but it does
    # not permanently wedge the next normal cooperative acquisition.
    with runtime.SharedLock(lock_path):
        assert lock_path.is_dir()
    # A later exact residue does not overwrite the completed evidence.  It
    # appends a distinct operation and retains both forensic archives.
    _historic_quarantine_pair(lock_path)
    later = runtime.recover_quarantine(execute=True)
    later_archive = Path(str(later["archive"]))
    assert later_archive != archive
    assert archive.is_dir()
    assert later_archive.is_dir()
    assert _guard_state(guard)["phase"] == "completed"


def test_guard_journal_rejects_replacement_after_append_before_path_recheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Appending through the retained FD cannot clobber a substituted guard."""
    guard = tmp_path / recovery.QUARANTINE_RECOVERY_GUARD
    first = {"kind": "test", "phase": "prepared"}
    journal = recovery._create_guard_journal(guard, first)
    foreign = b'{"foreign":true}\n'
    real_fsync = recovery.os.fsync
    replaced = False

    def replace_before_fsync(descriptor: int) -> None:
        nonlocal replaced
        if not replaced and descriptor == journal.descriptor:
            replaced = True
            guard.unlink()
            guard.write_bytes(foreign)
            guard.chmod(0o600)
        real_fsync(descriptor)

    monkeypatch.setattr(recovery.os, "fsync", replace_before_fsync)
    try:
        with pytest.raises(
            runtime.RuntimeAdmissionError, match="guard (changed|is unsafe)"
        ):
            recovery._append_guard_state(
                journal, {"kind": "test", "phase": "lock-renaming"}
            )
    finally:
        recovery._close_guard_journal(journal)
    assert guard.read_bytes() == foreign


def test_guard_journal_discards_only_a_torn_final_record_before_replay(
    tmp_path: Path,
) -> None:
    """A crash-torn suffix resumes from the longest checksum-valid prefix."""
    guard = tmp_path / recovery.QUARANTINE_RECOVERY_GUARD
    recovery._write_recovery_guard(
        guard, {"kind": "test", "phase": "prepared"}, create=True
    )
    with guard.open("ab") as handle:
        handle.write(b'{"journal_version":1')
        handle.flush()
        os.fsync(handle.fileno())
    recovery._write_recovery_guard(
        guard, {"kind": "test", "phase": "lock-renaming"}, create=False
    )
    assert _guard_state(guard)["phase"] == "lock-renaming"
    assert guard.read_bytes().endswith(b"\n")


def test_completed_guard_substitution_blocks_acquire_without_deleting_foreign_path(
    tmp_path: Path,
) -> None:
    """A replaced terminal journal is a stop, never a pathname cleanup target."""
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    guard = lock_path.parent / recovery.QUARANTINE_RECOVERY_GUARD
    lock_path.parent.mkdir(parents=True)
    recovery._write_recovery_guard(
        guard,
        {"kind": "test", "phase": "completed"},
        create=True,
    )
    original = guard.with_name("guard-original")
    guard.rename(original)
    foreign = b'{"foreign":true}\n'
    guard.write_bytes(foreign)
    guard.chmod(0o600)
    with pytest.raises(runtime.LockHeldError, match="operator completion"):
        runtime.SharedLock(lock_path).acquire()
    assert guard.read_bytes() == foreign
    assert original.exists()


def test_actual_quarantine_recovery_sync_blocks_tier_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launchd worker cannot enter while recover_quarantine owns its sync."""
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    lock_path = tmp_path / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda _root: lock_path)
    _historic_quarantine_pair(lock_path)
    baseline = capture()
    entered = threading.Event()
    proceed = threading.Event()
    outcome: dict[str, object] = {}

    def pause_after_sync() -> dict[str, bool]:
        entered.set()
        assert proceed.wait(timeout=10)
        return {"ok": True}

    monkeypatch.setattr(recovery, "recovery_cpu_evidence", pause_after_sync)
    monkeypatch.setattr(
        runtime, "_compose_loopback_endpoints", lambda _r: baseline.public["endpoints"]
    )
    monkeypatch.setattr(
        runtime, "_settlement", lambda *_args: baseline.public["settlement"]
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda _r: baseline)

    def recover() -> None:
        try:
            outcome["result"] = runtime.recover_quarantine(execute=True)
        except Exception as exc:  # noqa: BLE001 - surface thread failure below
            outcome["error"] = exc

    recovery_thread = threading.Thread(target=recover)
    recovery_thread.start()
    assert entered.wait(timeout=5)
    tier_attempt = (
        "from pathlib import Path; import cre_tier_dispatch as d; "
        f"raise SystemExit(d.run_tier('monitor', lock_path=Path({str(lock_path)!r}), "
        "command=['/bin/true']))"
    )
    blocked = subprocess.run(
        [
            sys.executable,
            "-c",
            tier_attempt,
        ],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert blocked.returncode == 0
    assert "tier/recovery is active; skipping monitor" in blocked.stderr
    assert lock_path.is_dir()
    assert lock_path.with_name(f"{lock_path.name}.authority").is_file()

    proceed.set()
    recovery_thread.join(timeout=10)
    assert not recovery_thread.is_alive()
    assert "error" not in outcome
    assert outcome["result"]


def test_actual_quarantine_recovery_refuses_while_tier_dispatch_holds_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery never archives or reclaims a namespace held by a tier worker."""
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    lock_path = tmp_path / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda _root: lock_path)
    ready = tmp_path / "tier-ready"
    child_code = (
        f"import pathlib, time; pathlib.Path({str(ready)!r}).write_text('ready'); "
        "time.sleep(30)"
    )
    runner_code = (
        "from pathlib import Path; import sys; import cre_tier_dispatch as d; "
        f"raise SystemExit(d.run_tier('monitor', lock_path=Path({str(lock_path)!r}), "
        f"command=[sys.executable, '-c', {child_code!r}]))"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            runner_code,
        ],
        cwd=Path(__file__).resolve().parent.parent,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        with pytest.raises(runtime.RuntimeAdmissionError, match="already in progress"):
            runtime.recover_quarantine(execute=True)
    finally:
        process.terminate()
        process.wait(timeout=10)

    assert process.returncode == 143


def test_quarantine_recovery_refuses_live_or_ambiguous_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    lock_path = tmp_path / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda _root: lock_path)
    _historic_quarantine_pair(lock_path)
    (lock_path / "unexpected").write_text("x", encoding="utf-8")
    with pytest.raises(runtime.RuntimeAdmissionError, match="ambiguous"):
        runtime.recover_quarantine(execute=False)
    assert lock_path.is_dir()


def test_quarantine_recovery_replays_an_interrupted_paired_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between paired renames remains an operator stop, then resumes."""
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    lock_path = tmp_path / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda _root: lock_path)
    _historic_quarantine_pair(lock_path)
    baseline = capture()
    monkeypatch.setattr(recovery, "recovery_cpu_evidence", lambda: {"ok": True})
    monkeypatch.setattr(
        runtime, "_compose_loopback_endpoints", lambda _r: baseline.public["endpoints"]
    )
    monkeypatch.setattr(
        runtime, "_settlement", lambda *_args: baseline.public["settlement"]
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda _r: baseline)
    real_rename = recovery._atomic_rename_noreplace
    authority = lock_path.with_name(f"{lock_path.name}.authority")

    def interrupt_authority_rename(source: Path, target: Path, *, message: str) -> None:
        if source == authority:
            raise OSError("simulated interruption before authority archive")
        real_rename(source, target, message=message)

    monkeypatch.setattr(
        recovery, "_atomic_rename_noreplace", interrupt_authority_rename
    )
    with pytest.raises(OSError, match="simulated interruption"):
        runtime.recover_quarantine(execute=True)
    guard = lock_path.parent / recovery.QUARANTINE_RECOVERY_GUARD
    assert guard.is_file()
    assert not lock_path.exists()
    assert authority.is_file()
    with pytest.raises(runtime.LockHeldError, match="operator completion"):
        runtime.SharedLock(lock_path).acquire()

    monkeypatch.setattr(recovery, "_atomic_rename_noreplace", real_rename)
    replayed = runtime.recover_quarantine(execute=True)
    assert replayed["executed"] is True
    assert _guard_state(guard)["phase"] == "completed"
    assert not authority.exists()
    assert not lock_path.exists()


def test_quarantine_recovery_replays_guard_before_archive_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first durable guard prevents a stranded deterministic archive name."""
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    lock_path = tmp_path / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda _root: lock_path)
    _historic_quarantine_pair(lock_path)
    baseline = capture()
    monkeypatch.setattr(recovery, "recovery_cpu_evidence", lambda: {"ok": True})
    monkeypatch.setattr(
        runtime, "_compose_loopback_endpoints", lambda _r: baseline.public["endpoints"]
    )
    monkeypatch.setattr(
        runtime, "_settlement", lambda *_args: baseline.public["settlement"]
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda _r: baseline)
    real_fsync = recovery.fsync_directory
    archive_root = lock_path.parent / recovery.QUARANTINE_ARCHIVE_DIR

    def interrupt_archive_fsync(path: Path) -> None:
        if path == archive_root:
            raise OSError("simulated archive-create power loss")
        real_fsync(path)

    monkeypatch.setattr(recovery, "fsync_directory", interrupt_archive_fsync)
    with pytest.raises(OSError, match="archive-create"):
        runtime.recover_quarantine(execute=True)
    assert (lock_path.parent / recovery.QUARANTINE_RECOVERY_GUARD).is_file()
    with pytest.raises(runtime.LockHeldError, match="operator completion"):
        runtime.SharedLock(lock_path).acquire()

    archive_root_fsynced = False
    real_rename = recovery._atomic_rename_noreplace

    def record_replay_archive_fsync(path: Path) -> None:
        nonlocal archive_root_fsynced
        if path == archive_root:
            archive_root_fsynced = True
        real_fsync(path)

    def require_archive_fsync_before_handoff(
        source: Path, target: Path, *, message: str
    ) -> None:
        if source == lock_path and not archive_root_fsynced:
            raise AssertionError("replay moved the lock before archive-root fsync")
        real_rename(source, target, message=message)

    monkeypatch.setattr(recovery, "fsync_directory", record_replay_archive_fsync)
    monkeypatch.setattr(
        recovery, "_atomic_rename_noreplace", require_archive_fsync_before_handoff
    )
    replayed = runtime.recover_quarantine(execute=True)
    assert replayed["executed"] is True
    assert archive_root_fsynced
    assert not lock_path.exists()


def test_quarantine_recovery_refuses_tampered_receipt_before_guard_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A torn/tampered receipt can never be treated as a completed archive."""
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    lock_path = tmp_path / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda _root: lock_path)
    _historic_quarantine_pair(lock_path)
    baseline = capture()
    monkeypatch.setattr(recovery, "recovery_cpu_evidence", lambda: {"ok": True})
    monkeypatch.setattr(
        runtime, "_compose_loopback_endpoints", lambda _r: baseline.public["endpoints"]
    )
    monkeypatch.setattr(
        runtime, "_settlement", lambda *_args: baseline.public["settlement"]
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda _r: baseline)
    real_guard_write = recovery._write_recovery_guard

    def interrupt_receipt_phase(
        path: Path, value: dict[str, object], *, create: bool
    ) -> None:
        if value.get("phase") == "receipt-written":
            raise OSError("simulated crash after receipt")
        real_guard_write(path, value, create=create)

    monkeypatch.setattr(recovery, "_write_recovery_guard", interrupt_receipt_phase)
    with pytest.raises(OSError, match="after receipt"):
        runtime.recover_quarantine(execute=True)
    guard = lock_path.parent / recovery.QUARANTINE_RECOVERY_GUARD
    guard_value = _guard_state(guard)
    archive = Path(guard_value["archive"])
    receipt = archive / "recovery-receipt.json"
    receipt.write_text('{"tampered":true}\n', encoding="utf-8")
    receipt.chmod(0o600)

    monkeypatch.setattr(recovery, "_write_recovery_guard", real_guard_write)
    with pytest.raises(runtime.RuntimeAdmissionError, match="receipt"):
        runtime.recover_quarantine(execute=True)
    assert guard.is_file()
    assert receipt.read_text(encoding="utf-8") == '{"tampered":true}\n'


def test_quarantine_recovery_replays_existing_receipt_after_archive_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A receipt entry is re-fsynced before replay advances its guard phase."""
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    lock_path = tmp_path / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda _root: lock_path)
    _historic_quarantine_pair(lock_path)
    baseline = capture()
    monkeypatch.setattr(recovery, "recovery_cpu_evidence", lambda: {"ok": True})
    monkeypatch.setattr(
        runtime, "_compose_loopback_endpoints", lambda _r: baseline.public["endpoints"]
    )
    monkeypatch.setattr(
        runtime, "_settlement", lambda *_args: baseline.public["settlement"]
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda _r: baseline)
    real_receipt_write = recovery._write_recovery_receipt
    real_fsync = recovery.fsync_directory
    archive: Path | None = None
    receipt_write_started = False

    def arm_receipt_fsync_failure(path: Path, value: dict[str, object]) -> None:
        nonlocal archive, receipt_write_started
        archive = path.parent
        receipt_write_started = True
        real_receipt_write(path, value)

    def fail_receipt_archive_fsync(path: Path) -> None:
        if receipt_write_started and archive is not None and path == archive:
            raise OSError("simulated receipt archive fsync failure")
        real_fsync(path)

    monkeypatch.setattr(recovery, "_write_recovery_receipt", arm_receipt_fsync_failure)
    monkeypatch.setattr(recovery, "fsync_directory", fail_receipt_archive_fsync)
    with pytest.raises(OSError, match="receipt archive fsync"):
        runtime.recover_quarantine(execute=True)
    guard = lock_path.parent / recovery.QUARANTINE_RECOVERY_GUARD
    guard_value = _guard_state(guard)
    archive = Path(guard_value["archive"])
    assert guard_value["phase"] == "pair-archived"
    assert (archive / "recovery-receipt.json").is_file()
    with pytest.raises(runtime.LockHeldError, match="operator completion"):
        runtime.SharedLock(lock_path).acquire()

    archive_fsynced = False

    def record_archive_fsync(path: Path) -> None:
        nonlocal archive_fsynced
        if path == archive:
            archive_fsynced = True
        real_fsync(path)

    real_guard_write = recovery._write_recovery_guard

    def require_receipt_fsync_before_advance(
        path: Path, value: dict[str, object], *, create: bool
    ) -> None:
        if value.get("phase") == "receipt-written" and not archive_fsynced:
            raise AssertionError("replay advanced receipt phase before archive fsync")
        real_guard_write(path, value, create=create)

    monkeypatch.setattr(recovery, "fsync_directory", record_archive_fsync)
    monkeypatch.setattr(
        recovery, "_write_recovery_guard", require_receipt_fsync_before_advance
    )
    assert runtime.recover_quarantine(execute=True)["executed"] is True
    assert archive_fsynced
    assert _guard_state(guard)["phase"] == "completed"


def test_recovery_directory_fsync_is_unique_strict_and_refuses_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery cannot silently use a later weak fsync helper or a symlink."""
    source = inspect.getsource(recovery)
    assert source.count("def fsync_directory(") == 1
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    observed_flags: list[int] = []
    real_open = runtime.os.open

    def record_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        observed_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(runtime.os, "open", record_open)
    recovery.fsync_directory(directory)
    assert any(
        flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW for flags in observed_flags
    )

    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    substituted = tmp_path / "substituted"
    substituted.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        recovery.fsync_directory(substituted)


@pytest.mark.parametrize(
    ("member", "phase"),
    [
        (".cre.lock", "lock-renaming"),
        (".cre.lock.authority", "authority-renaming"),
    ],
)
def test_quarantine_recovery_refuses_occupied_archive_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    member: str,
    phase: str,
) -> None:
    """An unexpected archive target is forensic evidence, never rename input."""
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    lock_path = tmp_path / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda _root: lock_path)
    _historic_quarantine_pair(lock_path)
    baseline = capture()
    monkeypatch.setattr(recovery, "recovery_cpu_evidence", lambda: {"ok": True})
    monkeypatch.setattr(
        runtime, "_compose_loopback_endpoints", lambda _r: baseline.public["endpoints"]
    )
    monkeypatch.setattr(
        runtime, "_settlement", lambda *_args: baseline.public["settlement"]
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda _r: baseline)
    real_guard_write = recovery._write_recovery_guard
    foreign = b"foreign authority\n"

    def occupy_target(path: Path, value: dict[str, object], *, create: bool) -> None:
        real_guard_write(path, value, create=create)
        if value.get("phase") != phase:
            return
        archive = Path(str(value["archive"]))
        target = archive / member
        if member == ".cre.lock":
            target.mkdir(mode=0o700)
        else:
            target.write_bytes(foreign)
            target.chmod(0o600)

    monkeypatch.setattr(recovery, "_write_recovery_guard", occupy_target)
    with pytest.raises(runtime.RuntimeAdmissionError, match="handoff changed"):
        runtime.recover_quarantine(execute=True)

    guard = lock_path.parent / recovery.QUARANTINE_RECOVERY_GUARD
    archive = Path(str(_guard_state(guard)["archive"]))
    target = archive / member
    assert target.exists()
    if member == ".cre.lock.authority":
        assert target.read_bytes() == foreign
    assert guard.exists()


def test_quarantine_recovery_refuses_byte_identical_marker_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Receipt finalization binds marker identity, not merely its digest."""
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    lock_path = tmp_path / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda _root: lock_path)
    _historic_quarantine_pair(lock_path)
    baseline = capture()
    monkeypatch.setattr(recovery, "recovery_cpu_evidence", lambda: {"ok": True})
    monkeypatch.setattr(
        runtime, "_compose_loopback_endpoints", lambda _r: baseline.public["endpoints"]
    )
    monkeypatch.setattr(
        runtime, "_settlement", lambda *_args: baseline.public["settlement"]
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda _r: baseline)
    real_guard_write = recovery._write_recovery_guard

    def interrupt_receipt_phase(
        path: Path, value: dict[str, object], *, create: bool
    ) -> None:
        if value.get("phase") == "receipt-written":
            raise OSError("simulated crash after receipt")
        real_guard_write(path, value, create=create)

    monkeypatch.setattr(recovery, "_write_recovery_guard", interrupt_receipt_phase)
    with pytest.raises(OSError, match="after receipt"):
        runtime.recover_quarantine(execute=True)
    guard = lock_path.parent / recovery.QUARANTINE_RECOVERY_GUARD
    archive = Path(str(_guard_state(guard)["archive"]))
    active = archive / ".cre.lock" / checkpoint_refresh.BENCHMARK_ACTIVE_MARKER
    replacement = active.with_name(f".{active.name}.replacement")
    replacement.write_bytes(active.read_bytes())
    replacement.chmod(0o600)
    replacement.replace(active)

    monkeypatch.setattr(recovery, "_write_recovery_guard", real_guard_write)
    with pytest.raises(
        runtime.RuntimeAdmissionError, match="archive entries|forensic pair"
    ):
        runtime.recover_quarantine(execute=True)
    assert guard.exists()
    assert active.exists()


@pytest.mark.parametrize("member", [".cre.lock", ".cre.lock.authority"])
def test_atomic_archive_handoff_refuses_a_target_created_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, member: str
) -> None:
    """The no-replace syscall closes the lstat-to-rename foreign-target race."""
    source = tmp_path / f"source-{member.removeprefix('.')}"
    target = tmp_path / f"target-{member.removeprefix('.')}"
    if member == ".cre.lock":
        source.mkdir(mode=0o700)
    else:
        source.write_bytes(b"owned authority\n")
        source.chmod(0o600)
    source_stat = source.lstat()
    expected = [source_stat.st_dev, source_stat.st_ino]
    real_atomic = recovery._atomic_rename_noreplace
    foreign = b"foreign authority\n"

    def insert_foreign_then_rename(
        original: Path, destination: Path, *, message: str
    ) -> None:
        if member == ".cre.lock":
            destination.mkdir(mode=0o700)
        else:
            destination.write_bytes(foreign)
            destination.chmod(0o600)
        real_atomic(original, destination, message=message)

    monkeypatch.setattr(
        recovery, "_atomic_rename_noreplace", insert_foreign_then_rename
    )
    with pytest.raises(runtime.RuntimeAdmissionError, match="handoff changed"):
        recovery._rename_exact_to_empty_target(
            source, target, expected, message="handoff changed"
        )
    assert source.exists()
    assert target.exists()
    if member == ".cre.lock.authority":
        assert target.read_bytes() == foreign


def test_atomic_archive_handoff_refuses_an_unsupported_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_bytes(b"owned\n")
    source.chmod(0o600)
    monkeypatch.setattr(recovery.sys, "platform", "unsupported")
    with pytest.raises(runtime.RuntimeAdmissionError, match="no-replace"):
        recovery._atomic_rename_noreplace(source, target, message="handoff changed")
    assert source.exists()
    assert not target.exists()


def test_quarantine_recovery_refuses_an_unbound_archived_lock_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay cannot clear the guard after any unbound archive mutation."""
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    lock_path = tmp_path / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda _root: lock_path)
    _historic_quarantine_pair(lock_path)
    baseline = capture()
    monkeypatch.setattr(recovery, "recovery_cpu_evidence", lambda: {"ok": True})
    monkeypatch.setattr(
        runtime, "_compose_loopback_endpoints", lambda _r: baseline.public["endpoints"]
    )
    monkeypatch.setattr(
        runtime, "_settlement", lambda *_args: baseline.public["settlement"]
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda _r: baseline)
    real_guard_write = recovery._write_recovery_guard

    def interrupt_receipt_phase(
        path: Path, value: dict[str, object], *, create: bool
    ) -> None:
        if value.get("phase") == "receipt-written":
            raise OSError("simulated crash after receipt")
        real_guard_write(path, value, create=create)

    monkeypatch.setattr(recovery, "_write_recovery_guard", interrupt_receipt_phase)
    with pytest.raises(OSError, match="after receipt"):
        runtime.recover_quarantine(execute=True)
    guard = lock_path.parent / recovery.QUARANTINE_RECOVERY_GUARD
    archive = Path(str(_guard_state(guard)["archive"]))
    foreign = archive / ".cre.lock" / "foreign"
    foreign.write_bytes(b"unbound forensic content\n")
    foreign.chmod(0o600)

    monkeypatch.setattr(recovery, "_write_recovery_guard", real_guard_write)
    with pytest.raises(
        runtime.RuntimeAdmissionError, match="archive entries|forensic pair"
    ):
        runtime.recover_quarantine(execute=True)
    assert guard.exists()
    assert foreign.exists()


def test_quarantine_recovery_refuses_an_unbound_archive_root_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreign top-level archive entry is never hidden by a valid pair."""
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    lock_path = tmp_path / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda _root: lock_path)
    _historic_quarantine_pair(lock_path)
    baseline = capture()
    monkeypatch.setattr(recovery, "recovery_cpu_evidence", lambda: {"ok": True})
    monkeypatch.setattr(
        runtime, "_compose_loopback_endpoints", lambda _r: baseline.public["endpoints"]
    )
    monkeypatch.setattr(
        runtime, "_settlement", lambda *_args: baseline.public["settlement"]
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda _r: baseline)
    real_guard_write = recovery._write_recovery_guard

    def interrupt_receipt_phase(
        path: Path, value: dict[str, object], *, create: bool
    ) -> None:
        if value.get("phase") == "receipt-written":
            raise OSError("simulated crash after receipt")
        real_guard_write(path, value, create=create)

    monkeypatch.setattr(recovery, "_write_recovery_guard", interrupt_receipt_phase)
    with pytest.raises(OSError, match="after receipt"):
        runtime.recover_quarantine(execute=True)
    guard = lock_path.parent / recovery.QUARANTINE_RECOVERY_GUARD
    archive = Path(str(_guard_state(guard)["archive"]))
    foreign = archive / "foreign-root"
    foreign.write_bytes(b"unbound archive content\n")
    foreign.chmod(0o600)

    monkeypatch.setattr(recovery, "_write_recovery_guard", real_guard_write)
    with pytest.raises(
        runtime.RuntimeAdmissionError, match="archive entries|forensic pair"
    ):
        runtime.recover_quarantine(execute=True)
    assert guard.exists()
    assert foreign.exists()


@pytest.mark.parametrize(
    "phase",
    [
        "prepared",
        "lock-renaming",
        "lock-archived",
        "authority-renaming",
        "pair-archived",
        "receipt-written",
    ],
)
def test_quarantine_recovery_blocks_new_shared_lock_at_every_durable_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """The persistent synchronizer spans every archive phase, not just guard I/O."""
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    lock_path = tmp_path / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda _root: lock_path)
    _historic_quarantine_pair(lock_path)
    baseline = capture()
    monkeypatch.setattr(recovery, "recovery_cpu_evidence", lambda: {"ok": True})
    monkeypatch.setattr(
        runtime, "_compose_loopback_endpoints", lambda _r: baseline.public["endpoints"]
    )
    monkeypatch.setattr(
        runtime, "_settlement", lambda *_args: baseline.public["settlement"]
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda _r: baseline)
    real_guard_write = recovery._write_recovery_guard
    observations: list[subprocess.CompletedProcess[str]] = []

    def assert_new_lock_is_blocked(
        path: Path, value: dict[str, object], *, create: bool
    ) -> None:
        real_guard_write(path, value, create=create)
        if value.get("phase") != phase:
            return
        script = """
import sys
from pathlib import Path
import cre_checkpoint_refresh as refresh
try:
    refresh.SharedLock(Path(sys.argv[1])).acquire()
except refresh.LockHeldError as exc:
    print(exc)
    raise SystemExit(73)
raise SystemExit(0)
"""
        observations.append(
            subprocess.run(
                [sys.executable, "-c", script, str(lock_path)],
                cwd=Path(checkpoint_refresh.__file__).parent,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        )

    monkeypatch.setattr(recovery, "_write_recovery_guard", assert_new_lock_is_blocked)
    assert runtime.recover_quarantine(execute=True)["executed"] is True
    assert len(observations) == 1
    assert observations[0].returncode == 73, observations[0].stderr
    assert "authority is held" in observations[0].stdout
    assert not lock_path.exists()


def test_quarantine_recovery_guard_create_is_atomic_and_never_clobbers(
    tmp_path: Path,
) -> None:
    """A second operation cannot replace the first operation's durable guard."""
    guard = tmp_path / recovery.QUARANTINE_RECOVERY_GUARD
    original = {"kind": "test", "phase": "prepared"}
    recovery._write_recovery_guard(guard, original, create=True)
    with pytest.raises(runtime.RuntimeAdmissionError, match="already exists"):
        recovery._write_recovery_guard(
            guard, {"kind": "test", "phase": "lock-renaming"}, create=True
        )
    assert recovery._read_recovery_guard(guard) == original


def test_quarantine_recovery_refuses_a_simultaneous_operation_before_guard_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stable flock rejects a peer before either can publish a guard."""
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    lock_path = tmp_path / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda _root: lock_path)
    synchronizer = checkpoint_refresh.acquire_quarantine_recovery_sync(lock_path)
    try:
        with pytest.raises(runtime.RuntimeAdmissionError, match="already in progress"):
            runtime.recover_quarantine(execute=True)
        assert not (lock_path.parent / recovery.QUARANTINE_RECOVERY_GUARD).exists()
    finally:
        checkpoint_refresh.release_quarantine_recovery_sync(synchronizer)


def test_quarantine_recovery_rejects_a_simultaneous_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second operator process loses the stable flock before guard mutation."""
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    lock_path = tmp_path / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda _root: lock_path)
    _historic_quarantine_pair(lock_path)
    baseline = capture()
    monkeypatch.setattr(recovery, "recovery_cpu_evidence", lambda: {"ok": True})
    monkeypatch.setattr(
        runtime, "_compose_loopback_endpoints", lambda _r: baseline.public["endpoints"]
    )
    monkeypatch.setattr(
        runtime, "_settlement", lambda *_args: baseline.public["settlement"]
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda _r: baseline)
    real_guard_write = recovery._write_recovery_guard
    observations: list[subprocess.CompletedProcess[str]] = []

    def invoke_second_operator(
        path: Path, value: dict[str, object], *, create: bool
    ) -> None:
        real_guard_write(path, value, create=create)
        if value.get("phase") != "prepared":
            return
        script = """
import sys
from pathlib import Path
import cre_capacity_runtime as runtime
runtime.REPO_ROOT = Path(sys.argv[1])
runtime.canonical_shared_lock_dir = lambda _root: Path(sys.argv[2])
try:
    runtime.recover_quarantine(execute=True)
except runtime.RuntimeAdmissionError as exc:
    print(exc)
    raise SystemExit(73)
raise SystemExit(0)
"""
        observations.append(
            subprocess.run(
                [sys.executable, "-c", script, str(tmp_path), str(lock_path)],
                cwd=Path(runtime.__file__).parent,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        )

    monkeypatch.setattr(recovery, "_write_recovery_guard", invoke_second_operator)
    assert runtime.recover_quarantine(execute=True)["executed"] is True
    assert len(observations) == 1
    assert observations[0].returncode == 73, observations[0].stderr
    assert "already in progress" in observations[0].stdout


@pytest.mark.parametrize(
    ("member", "expected_phase", "failure_target"),
    [
        (".cre.lock", "lock-renaming", "archive"),
        (".cre.lock", "lock-renaming", "source-parent"),
        (".cre.lock.authority", "authority-renaming", "archive"),
        (".cre.lock.authority", "authority-renaming", "source-parent"),
    ],
)
def test_quarantine_recovery_replays_rename_before_durable_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    member: str,
    expected_phase: str,
    failure_target: str,
) -> None:
    """Both destination/source fsync prefixes retain an exact replay intent."""
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    lock_path = tmp_path / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda _root: lock_path)
    _historic_quarantine_pair(lock_path)
    baseline = capture()
    monkeypatch.setattr(recovery, "recovery_cpu_evidence", lambda: {"ok": True})
    monkeypatch.setattr(
        runtime, "_compose_loopback_endpoints", lambda _r: baseline.public["endpoints"]
    )
    monkeypatch.setattr(
        runtime, "_settlement", lambda *_args: baseline.public["settlement"]
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda _r: baseline)
    real_rename = recovery._atomic_rename_noreplace
    real_fsync = recovery.fsync_directory
    renamed = False
    failed = False
    archive: Path | None = None
    handoff_fsync_events: list[Path] = []

    def record_rename(source: Path, target: Path, *, message: str) -> None:
        nonlocal archive, renamed
        if source.name == member:
            renamed = True
            archive = target.parent
        real_rename(source, target, message=message)

    def fail_durable_handoff_after_rename(path: Path) -> None:
        nonlocal failed
        if renamed:
            handoff_fsync_events.append(path)
        target = archive if failure_target == "archive" else lock_path.parent
        if renamed and not failed and path == target:
            failed = True
            raise OSError(f"simulated {failure_target} fsync power loss")
        real_fsync(path)

    monkeypatch.setattr(recovery, "_atomic_rename_noreplace", record_rename)
    monkeypatch.setattr(recovery, "fsync_directory", fail_durable_handoff_after_rename)
    with pytest.raises(OSError, match=f"{failure_target} fsync"):
        runtime.recover_quarantine(execute=True)
    assert failed
    assert archive is not None
    assert handoff_fsync_events[0] == archive
    if failure_target == "archive":
        assert handoff_fsync_events == [archive]
    else:
        assert handoff_fsync_events[:2] == [archive, lock_path.parent]
    guard = lock_path.parent / recovery.QUARANTINE_RECOVERY_GUARD
    assert _guard_state(guard)["phase"] == expected_phase
    with pytest.raises(runtime.LockHeldError, match="operator completion"):
        runtime.SharedLock(lock_path).acquire()

    monkeypatch.setattr(recovery, "_atomic_rename_noreplace", real_rename)
    monkeypatch.setattr(recovery, "fsync_directory", real_fsync)
    assert runtime.recover_quarantine(execute=True)["executed"] is True
    assert _guard_state(guard)["phase"] == "completed"


@pytest.mark.parametrize(
    ("member", "expected_phase"),
    [(".cre.lock", "lock-renaming"), (".cre.lock.authority", "authority-renaming")],
)
def test_quarantine_recovery_refuses_reappeared_source_before_any_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    member: str,
    expected_phase: str,
) -> None:
    """An archived member cannot make a reappeared canonical path look absent."""
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    lock_path = tmp_path / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda _root: lock_path)
    _historic_quarantine_pair(lock_path)
    baseline = capture()
    monkeypatch.setattr(recovery, "recovery_cpu_evidence", lambda: {"ok": True})
    monkeypatch.setattr(
        runtime, "_compose_loopback_endpoints", lambda _r: baseline.public["endpoints"]
    )
    monkeypatch.setattr(
        runtime, "_settlement", lambda *_args: baseline.public["settlement"]
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda _r: baseline)
    real_rename = recovery._atomic_rename_noreplace
    real_fsync = recovery.fsync_directory
    archive: Path | None = None

    def record_member_rename(source: Path, target: Path, *, message: str) -> None:
        nonlocal archive
        if source.name == member:
            archive = target.parent
        real_rename(source, target, message=message)

    def fail_archive_fsync_after_member_rename(path: Path) -> None:
        if archive is not None and path == archive:
            raise OSError("simulated archive durability interruption")
        real_fsync(path)

    monkeypatch.setattr(recovery, "_atomic_rename_noreplace", record_member_rename)
    monkeypatch.setattr(
        recovery, "fsync_directory", fail_archive_fsync_after_member_rename
    )
    with pytest.raises(OSError, match="archive durability interruption"):
        runtime.recover_quarantine(execute=True)
    assert archive is not None
    guard = lock_path.parent / recovery.QUARANTINE_RECOVERY_GUARD
    assert _guard_state(guard)["phase"] == expected_phase

    authority = lock_path.with_name(f"{lock_path.name}.authority")
    source = lock_path if member == lock_path.name else authority
    if member == lock_path.name:
        source.mkdir(mode=0o700)
    else:
        source.write_text("foreign authority\n", encoding="utf-8")
        source.chmod(0o600)

    def snapshot_tree(path: Path) -> list[tuple[object, ...]]:
        result: list[tuple[object, ...]] = []
        for entry in [path, *sorted(path.rglob("*"))]:
            observed = entry.lstat()
            result.append(
                (
                    entry.relative_to(path),
                    observed.st_dev,
                    observed.st_ino,
                    stat.S_IMODE(observed.st_mode),
                    observed.st_uid,
                    observed.st_nlink,
                    entry.read_bytes() if stat.S_ISREG(observed.st_mode) else None,
                )
            )
        return result

    guard_before = guard.read_bytes()
    archive_before = snapshot_tree(archive)
    source_before = snapshot_tree(source) if source.is_dir() else source.read_bytes()
    fsync_paths: list[Path] = []

    def record_replay_fsync(path: Path) -> None:
        fsync_paths.append(path)
        real_fsync(path)

    monkeypatch.setattr(recovery, "fsync_directory", record_replay_fsync)
    monkeypatch.setattr(
        recovery,
        "_atomic_rename_noreplace",
        lambda *_args, **_kwargs: pytest.fail("reappeared source must not be renamed"),
    )
    monkeypatch.setattr(
        recovery,
        "_write_recovery_guard",
        lambda *_args, **_kwargs: pytest.fail(
            "reappeared source must not advance guard"
        ),
    )
    with pytest.raises(runtime.RuntimeAdmissionError, match="handoff changed"):
        runtime.recover_quarantine(execute=True)

    assert guard.read_bytes() == guard_before
    assert snapshot_tree(archive) == archive_before
    if source.is_dir():
        assert snapshot_tree(source) == source_before
    else:
        assert source.read_bytes() == source_before
    assert not (archive / "recovery-receipt.json").exists()
    assert fsync_paths == [archive.parent]


@pytest.mark.parametrize(
    ("phase", "reappeared_member"),
    [
        ("lock-archived", ".cre.lock"),
        ("authority-renaming", ".cre.lock"),
        ("pair-archived", ".cre.lock"),
        ("pair-archived", ".cre.lock.authority"),
        ("receipt-written", ".cre.lock"),
        ("receipt-written", ".cre.lock.authority"),
    ],
)
def test_quarantine_recovery_later_phase_reappearance_stops_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    reappeared_member: str,
) -> None:
    """Every later phase fences all sources that its recorded handoffs removed."""
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    lock_path = tmp_path / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda _root: lock_path)
    _historic_quarantine_pair(lock_path)
    baseline = capture()
    monkeypatch.setattr(recovery, "recovery_cpu_evidence", lambda: {"ok": True})
    monkeypatch.setattr(
        runtime, "_compose_loopback_endpoints", lambda _r: baseline.public["endpoints"]
    )
    monkeypatch.setattr(
        runtime, "_settlement", lambda *_args: baseline.public["settlement"]
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda _r: baseline)
    authority = lock_path.with_name(f"{lock_path.name}.authority")
    guard = lock_path.parent / recovery.QUARANTINE_RECOVERY_GUARD
    real_guard_write = recovery._write_recovery_guard

    real_rename = recovery._atomic_rename_noreplace
    real_receipt_write = recovery._write_recovery_receipt

    def interrupt_at_phase(
        path: Path, value: dict[str, object], *, create: bool
    ) -> None:
        if phase == "lock-archived" and value.get("phase") == "authority-renaming":
            raise OSError("simulated lock-archived interruption")
        real_guard_write(path, value, create=create)
        if phase == "receipt-written" and value.get("phase") == "receipt-written":
            raise OSError("simulated receipt-written interruption")

    def interrupt_authority_rename(source: Path, target: Path, *, message: str) -> None:
        if phase == "authority-renaming" and source == authority:
            raise OSError("simulated authority-renaming interruption")
        real_rename(source, target, message=message)

    def interrupt_receipt_write(path: Path, value: dict[str, object]) -> None:
        if phase == "pair-archived":
            raise OSError("simulated pair-archived interruption")
        real_receipt_write(path, value)

    monkeypatch.setattr(recovery, "_write_recovery_guard", interrupt_at_phase)
    monkeypatch.setattr(
        recovery, "_atomic_rename_noreplace", interrupt_authority_rename
    )
    monkeypatch.setattr(recovery, "_write_recovery_receipt", interrupt_receipt_write)
    with pytest.raises(OSError, match=f"{phase} interruption"):
        runtime.recover_quarantine(execute=True)
    assert _guard_state(guard)["phase"] == phase

    source = lock_path if reappeared_member == lock_path.name else authority
    if source == lock_path:
        source.mkdir(mode=0o700)
    else:
        source.write_text("foreign authority\n", encoding="utf-8")
        source.chmod(0o600)
    guard_before = guard.read_bytes()

    def snapshot_tree(path: Path) -> list[tuple[object, ...]]:
        result: list[tuple[object, ...]] = []
        for entry in [path, *sorted(path.rglob("*"))]:
            observed = entry.lstat()
            result.append(
                (
                    entry.relative_to(path),
                    observed.st_dev,
                    observed.st_ino,
                    stat.S_IMODE(observed.st_mode),
                    observed.st_uid,
                    observed.st_nlink,
                    entry.read_bytes() if stat.S_ISREG(observed.st_mode) else None,
                )
            )
        return result

    archive = Path(str(_guard_state(guard)["archive"]))
    archive_before = snapshot_tree(archive)
    source_before = snapshot_tree(source) if source.is_dir() else source.read_bytes()
    fsync_paths: list[Path] = []
    real_fsync = recovery.fsync_directory

    def record_replay_fsync(path: Path) -> None:
        fsync_paths.append(path)
        real_fsync(path)

    monkeypatch.setattr(recovery, "fsync_directory", record_replay_fsync)
    monkeypatch.setattr(
        recovery,
        "_atomic_rename_noreplace",
        lambda *_args, **_kwargs: pytest.fail("reappeared source must not be renamed"),
    )
    monkeypatch.setattr(
        recovery,
        "_write_recovery_guard",
        lambda *_args, **_kwargs: pytest.fail(
            "reappeared source must not advance guard"
        ),
    )
    monkeypatch.setattr(
        recovery,
        "_write_recovery_receipt",
        lambda *_args, **_kwargs: pytest.fail(
            "reappeared source must not write receipt"
        ),
    )
    with pytest.raises(runtime.RuntimeAdmissionError, match="source reappeared"):
        runtime.recover_quarantine(execute=True)

    assert guard.read_bytes() == guard_before
    assert snapshot_tree(archive) == archive_before
    if source.is_dir():
        assert snapshot_tree(source) == source_before
    else:
        assert source.read_bytes() == source_before
    assert fsync_paths == [archive.parent]


def test_quarantine_recovery_post_fence_reappearance_is_a_retained_intent_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A noncooperating post-fence write cannot reach the next mutation.

    A pathname writer outside the recovery flock can always race an absence
    observation.  The guard is consequently a durable next-operation intent,
    not a perpetual assertion about that pathname: the next phase must reject
    the reappearance before it can archive the authority, write a receipt, or
    clear the guard.
    """
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    lock_path = tmp_path / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
    monkeypatch.setattr(runtime, "canonical_shared_lock_dir", lambda _root: lock_path)
    _historic_quarantine_pair(lock_path)
    baseline = capture()
    monkeypatch.setattr(recovery, "recovery_cpu_evidence", lambda: {"ok": True})
    monkeypatch.setattr(
        runtime, "_compose_loopback_endpoints", lambda _r: baseline.public["endpoints"]
    )
    monkeypatch.setattr(
        runtime, "_settlement", lambda *_args: baseline.public["settlement"]
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda _r: baseline)

    authority = lock_path.with_name(f"{lock_path.name}.authority")
    guard = lock_path.parent / recovery.QUARANTINE_RECOVERY_GUARD
    real_guard_write = recovery._write_recovery_guard

    def snapshot_tree(path: Path) -> list[tuple[object, ...]]:
        result: list[tuple[object, ...]] = []
        for entry in [path, *sorted(path.rglob("*"))]:
            observed = entry.lstat()
            result.append(
                (
                    entry.relative_to(path),
                    observed.st_dev,
                    observed.st_ino,
                    stat.S_IMODE(observed.st_mode),
                    observed.st_uid,
                    observed.st_nlink,
                    entry.read_bytes() if stat.S_ISREG(observed.st_mode) else None,
                )
            )
        return result

    def interrupt_after_lock_archive_intent(
        path: Path, value: dict[str, object], *, create: bool
    ) -> None:
        if value.get("phase") == "authority-renaming":
            raise OSError("simulated lock-archived interruption")
        real_guard_write(path, value, create=create)

    monkeypatch.setattr(
        recovery, "_write_recovery_guard", interrupt_after_lock_archive_intent
    )
    with pytest.raises(OSError, match="lock-archived interruption"):
        runtime.recover_quarantine(execute=True)
    assert _guard_state(guard)["phase"] == "lock-archived"

    monkeypatch.setattr(recovery, "_write_recovery_guard", real_guard_write)
    real_fence = recovery._require_phase_source_absence
    injected = False
    injected_lock_before: list[tuple[object, ...]] | None = None

    def inject_after_lock_fence(
        phase: str, observed_lock: Path, observed_authority: Path
    ) -> None:
        nonlocal injected, injected_lock_before
        real_fence(phase, observed_lock, observed_authority)
        if phase == "lock-archived" and not injected:
            injected = True
            observed_lock.mkdir(mode=0o700)
            injected_lock_before = snapshot_tree(observed_lock)

    monkeypatch.setattr(
        recovery, "_require_phase_source_absence", inject_after_lock_fence
    )
    archived_lock = Path(str(_guard_state(guard)["archive"])) / lock_path.name
    archived_lock_before = snapshot_tree(archived_lock)
    authority_before = (
        authority.lstat().st_dev,
        authority.lstat().st_ino,
        stat.S_IMODE(authority.lstat().st_mode),
        authority.lstat().st_uid,
        authority.lstat().st_nlink,
        authority.read_bytes(),
    )
    guard_writes: list[str] = []

    def record_guard_intent(
        path: Path, value: dict[str, object], *, create: bool
    ) -> None:
        guard_writes.append(str(value["phase"]))
        real_guard_write(path, value, create=create)

    monkeypatch.setattr(recovery, "_write_recovery_guard", record_guard_intent)
    monkeypatch.setattr(
        recovery,
        "_atomic_rename_noreplace",
        lambda *_args, **_kwargs: pytest.fail(
            "post-fence source must stop authority rename"
        ),
    )
    monkeypatch.setattr(
        recovery,
        "_write_recovery_receipt",
        lambda *_args, **_kwargs: pytest.fail(
            "post-fence source must stop receipt write"
        ),
    )
    with pytest.raises(runtime.RuntimeAdmissionError, match="lock source reappeared"):
        runtime.recover_quarantine(execute=True)

    assert injected
    assert injected_lock_before is not None
    assert guard_writes == ["authority-renaming"]
    assert _guard_state(guard)["phase"] == "authority-renaming"
    assert lock_path.is_dir()
    assert snapshot_tree(lock_path) == injected_lock_before
    assert snapshot_tree(archived_lock) == archived_lock_before
    assert (
        authority.lstat().st_dev,
        authority.lstat().st_ino,
        stat.S_IMODE(authority.lstat().st_mode),
        authority.lstat().st_uid,
        authority.lstat().st_nlink,
        authority.read_bytes(),
    ) == authority_before
    archive = archived_lock.parent
    assert not (archive / "recovery-receipt.json").exists()

    guard_before_replay = guard.read_bytes()
    with pytest.raises(runtime.RuntimeAdmissionError, match="lock source reappeared"):
        runtime.recover_quarantine(execute=True)
    assert guard.read_bytes() == guard_before_replay
    assert snapshot_tree(lock_path) == injected_lock_before
    assert snapshot_tree(archived_lock) == archived_lock_before
    assert authority.read_bytes() == authority_before[-1]
    assert not (archive / "recovery-receipt.json").exists()
