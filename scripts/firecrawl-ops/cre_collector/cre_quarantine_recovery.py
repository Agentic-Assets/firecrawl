"""Crash-replayable archive of the exact governed CRE quarantine residue.

This module owns the forensic archive/replay state machine.  It is deliberately
separate from the runtime controller: callers inject the live observation
callbacks while this module owns only filesystem authority, recovery state, and
receipt evidence.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

import cre_checkpoint_refresh as checkpoint_refresh
import cre_recovery_guard_journal as guard_journal
from cre_capacity_errors import RuntimeAdmissionError
from cre_checkpoint_refresh import LockHeldError

SCHEMA_VERSION = 1
QUARANTINE_ARCHIVE_DIR = ".cre-quarantine-forensics"
QUARANTINE_RECOVERY_KIND = "cre_capacity_quarantine_recovery"
QUARANTINE_RECOVERY_GUARD = checkpoint_refresh.QUARANTINE_RECOVERY_GUARD
LEGACY_AUTHORITY_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
GUARD_JOURNAL_VERSION = guard_journal.JOURNAL_VERSION
GUARD_JOURNAL_MAX_BYTES = guard_journal.JOURNAL_MAX_BYTES


@dataclass(frozen=True)
class QuarantineRecoveryConfig:
    """Exact filesystem and schema bindings for one operator recovery target."""

    lock_path: Path
    schema_version: int = SCHEMA_VERSION
    archive_dir_name: str = QUARANTINE_ARCHIVE_DIR
    recovery_kind: str = QUARANTINE_RECOVERY_KIND


class QuarantineRecoveryError(RuntimeAdmissionError):
    """Public admission failure for an unsafe quarantine recovery operation."""


class QuarantineEvidence(TypedDict, total=False):
    """Identity-bound operator evidence retained in a recovery guard or receipt."""

    lock_identity: list[int]
    authority_identity: list[int]
    active_identity: list[int]
    quarantine_identity: list[int]
    active_sha256: str
    quarantine_sha256: str
    authority_sha256: str
    active_evidence: dict[str, Any]
    quarantine_evidence: dict[str, Any]
    authority_evidence: dict[str, Any]
    owner: int


class QuarantineRecoveryResult(TypedDict, total=False):
    """Public dry-run or completed forensic-archive outcome."""

    schema_version: int
    kind: str
    lock_path: str
    archive: str
    phase: str
    pair: QuarantineEvidence
    executed: bool


@dataclass(frozen=True)
class QuarantineRecoveryCallbacks:
    """Read-only runtime observations injected by the thin runtime wrapper."""

    runner: Callable[..., Any]
    compose_endpoints: Callable[[Callable[..., Any]], Mapping[str, str]]
    settlement: Callable[[Callable[..., Any], Mapping[str, str]], Mapping[str, Any]]
    load_baseline_profile: Callable[[], tuple[Mapping[str, Any], Any]]
    capture_runtime: Callable[[Callable[..., Any]], Any]
    evaluate_state: Callable[
        [Mapping[str, Any], Mapping[str, Any], str], Mapping[str, bool]
    ]
    cpu_evidence: Callable[[], Mapping[str, Any]]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def fsync_directory(path: Path) -> None:
    """Fsync one owner-controlled directory without following substitutions."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        observed = os.fstat(descriptor)
        named = path.lstat()
        if (
            not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or stat.S_IMODE(observed.st_mode) & 0o022
            or observed.st_nlink < 2
            or (named.st_dev, named.st_ino) != (observed.st_dev, observed.st_ino)
        ):
            raise RuntimeAdmissionError(
                "directory changed before durability acknowledgement"
            )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _regular_file_evidence(
    path: Path, *, limit: int = 65536
) -> tuple[bytes, dict[str, Any]]:
    """Read a private singleton file while binding its complete identity."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or observed.st_uid != os.geteuid()
            or stat.S_IMODE(observed.st_mode) != 0o600
        ):
            raise RuntimeAdmissionError(
                "quarantine evidence is not a private regular file"
            )
        raw = os.read(descriptor, limit + 1)
        if len(raw) > limit:
            raise RuntimeAdmissionError("quarantine evidence is oversized")
        named = path.lstat()
        if (named.st_dev, named.st_ino) != (observed.st_dev, observed.st_ino):
            raise RuntimeAdmissionError("quarantine evidence changed during inspection")
        return raw, {
            "identity": [observed.st_dev, observed.st_ino],
            "mode": stat.S_IMODE(observed.st_mode),
            "uid": observed.st_uid,
            "nlink": observed.st_nlink,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    finally:
        os.close(descriptor)


def _regular_bytes(path: Path, *, limit: int = 65536) -> tuple[bytes, tuple[int, int]]:
    raw, evidence = _regular_file_evidence(path, limit=limit)
    return raw, tuple(evidence["identity"])


def _private_directory(path: Path, *, message: str) -> os.stat_result:
    try:
        observed = path.lstat()
    except OSError as exc:
        raise RuntimeAdmissionError(message) from exc
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or stat.S_IMODE(observed.st_mode) != 0o700
    ):
        raise RuntimeAdmissionError(message)
    return observed


def _empty_private_directory(path: Path, *, message: str) -> None:
    _private_directory(path, message=message)
    try:
        if any(path.iterdir()):
            raise RuntimeAdmissionError(message)
    except OSError as exc:
        raise RuntimeAdmissionError(message) from exc


def _quarantine_pair(lock_path: Path) -> QuarantineEvidence:
    """Recognize only the historic pre-persistent-authority pytest residue."""
    authority = lock_path.with_name(f"{lock_path.name}.authority")
    try:
        lock_stat = lock_path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeAdmissionError("canonical quarantine lock is absent") from exc
    _private_directory(lock_path, message="canonical quarantine lock is unsafe")
    entries = sorted(item.name for item in lock_path.iterdir())
    expected = sorted(
        [
            checkpoint_refresh.BENCHMARK_ACTIVE_MARKER,
            checkpoint_refresh.BENCHMARK_QUARANTINE_MARKER,
        ]
    )
    if entries != expected:
        raise RuntimeAdmissionError("quarantine lock has ambiguous entries")
    active_raw, active_evidence = _regular_file_evidence(lock_path / expected[0])
    quarantine_raw, quarantine_evidence = _regular_file_evidence(
        lock_path / expected[1]
    )
    authority_raw, authority_evidence = _regular_file_evidence(authority, limit=512)
    try:
        active = json.loads(active_raw)
        quarantine = json.loads(quarantine_raw)
        authority_parts = authority_raw.decode("utf-8").strip().split()
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeAdmissionError("quarantine evidence is malformed") from exc
    if (
        not isinstance(active, Mapping)
        or not isinstance(quarantine, Mapping)
        or active.get("kind") != "cre_capacity_candidate_pair_active"
        or active.get("state") != "active"
        or active.get("profile") != "bold-jll-128"
        or quarantine.get("kind") != "cre_capacity_benchmark_lock_quarantine"
        or quarantine.get("state") != "quarantined"
        or quarantine.get("reason") != "candidate_baseline_rollback_failed"
        or quarantine.get("lock_path") != str(lock_path)
        or active.get("result_path") != quarantine.get("result_path")
        or "/pytest-of-" not in str(active.get("result_path", ""))
        or len(authority_parts) != 2
        or not authority_parts[0].isdigit()
        or not LEGACY_AUTHORITY_TOKEN.fullmatch(authority_parts[1])
    ):
        raise RuntimeAdmissionError(
            "quarantine residue is not the exact recoverable legacy form"
        )
    owner = int(authority_parts[0])
    if owner <= 0 or checkpoint_refresh._pid_alive(owner):
        raise RuntimeAdmissionError("quarantine authority owner is live or invalid")
    if active.get("pid") != owner:
        raise RuntimeAdmissionError("quarantine marker and authority owner differ")
    return {
        "lock_identity": [lock_stat.st_dev, lock_stat.st_ino],
        "authority_identity": authority_evidence["identity"],
        "active_identity": active_evidence["identity"],
        "quarantine_identity": quarantine_evidence["identity"],
        "active_sha256": active_evidence["sha256"],
        "quarantine_sha256": quarantine_evidence["sha256"],
        "authority_sha256": authority_evidence["sha256"],
        "active_evidence": active_evidence,
        "quarantine_evidence": quarantine_evidence,
        "authority_evidence": authority_evidence,
        "owner": owner,
    }


def recovery_cpu_evidence(
    *,
    sampler: Callable[[], float] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Require a fresh full 90%-for-30s observation before manual recovery."""
    read = sampler or checkpoint_refresh.DarwinCpuSampler()
    samples: list[float] = []
    for index in range(16):
        value = read()
        if not isinstance(value, (int, float)) or not 0 <= value <= 100:
            raise RuntimeAdmissionError("quarantine recovery CPU telemetry is invalid")
        samples.append(float(value))
        if index < 15:
            sleep(2)
    if any(value >= 90 for value in samples):
        raise RuntimeAdmissionError("quarantine recovery CPU guard is not idle")
    return {
        "threshold_percent": 90,
        "sample_seconds": 2,
        "sustain_seconds": 30,
        "samples": [round(value, 2) for value in samples],
    }


# Keep the narrow test seam stable while runtime callers use the public API.
_recovery_cpu_evidence = recovery_cpu_evidence


def _recovery_guard_path(lock_path: Path) -> Path:
    return lock_path.parent / QUARANTINE_RECOVERY_GUARD


_GuardJournal = guard_journal.GuardJournal
_guard_identity = guard_journal.guard_identity
_guard_record = guard_journal.guard_record
_close_guard_journal = guard_journal.close


def _append_guard_state(journal: _GuardJournal, state: Mapping[str, Any]) -> None:
    guard_journal.append(journal, state)


def _open_guard_journal(path: Path) -> _GuardJournal:
    return guard_journal.open_journal(path, max_bytes=GUARD_JOURNAL_MAX_BYTES)


def _create_guard_journal(path: Path, state: Mapping[str, Any]) -> _GuardJournal:
    return guard_journal.create(
        path,
        state,
        fsync_parent=fsync_directory,
        max_bytes=GUARD_JOURNAL_MAX_BYTES,
    )


def _write_recovery_guard(
    path: Path, value: Mapping[str, Any], *, create: bool
) -> None:
    guard_journal.write(
        path,
        value,
        create_record=create,
        fsync_parent=fsync_directory,
        max_bytes=GUARD_JOURNAL_MAX_BYTES,
    )


def _read_recovery_guard(path: Path) -> dict[str, Any]:
    journal = _open_guard_journal(path)
    try:
        return dict(journal.state)
    finally:
        _close_guard_journal(journal)


def _same_identity(path: Path, expected: Sequence[int]) -> bool:
    try:
        observed = path.lstat()
    except OSError:
        return False
    return [observed.st_dev, observed.st_ino] == list(expected)


def _path_is_absent(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _require_phase_source_absence(phase: str, lock_path: Path, authority: Path) -> None:
    """Fail closed if a completed handoff's canonical source reappears."""
    if phase in {
        "lock-archived",
        "authority-renaming",
        "pair-archived",
        "receipt-written",
    }:
        if not _path_is_absent(lock_path):
            raise RuntimeAdmissionError("quarantine lock source reappeared")
    if phase in {"pair-archived", "receipt-written"}:
        if not _path_is_absent(authority):
            raise RuntimeAdmissionError("quarantine authority source reappeared")


def _rename_exact_to_empty_target(
    source: Path,
    target: Path,
    expected: Sequence[int],
    *,
    message: str,
) -> None:
    """Move only the expected source to a target proven absent at handoff.

    Replay accepts an already-archived expected inode, but an occupied target
    is always forensic evidence, not a destination we may replace. The
    authority flock serializes cooperative recovery participants, while the
    operating-system no-replace primitive closes the check-to-rename window
    against a foreign target appearing between the identity check and handoff.
    """
    if not _same_identity(source, expected):
        raise RuntimeAdmissionError(message)
    if not _path_is_absent(target):
        raise RuntimeAdmissionError(message)
    _atomic_rename_noreplace(source, target, message=message)
    if not _same_identity(target, expected) or not _path_is_absent(source):
        raise RuntimeAdmissionError(message)


def _atomic_rename_noreplace(source: Path, target: Path, *, message: str) -> None:
    """Atomically rename only when ``target`` does not exist on Darwin/Linux.

    POSIX ``rename`` clobbers a destination.  A preflight ``lstat`` cannot make
    that safe, so this uses Darwin's ``renameatx_np(RENAME_EXCL)`` or Linux's
    ``renameat2(RENAME_NOREPLACE)``. Unsupported runtimes deliberately stop
    rather than falling back to clobbering rename semantics.
    """
    library = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    if sys.platform == "darwin":
        operation = getattr(library, "renameatx_np", None)
        directory_fd = -2  # AT_FDCWD on Darwin.
        flags = 0x0004  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        operation = getattr(library, "renameat2", None)
        directory_fd = -100  # AT_FDCWD on Linux.
        flags = 0x0001  # RENAME_NOREPLACE
    else:
        operation = None
        directory_fd = 0
        flags = 0
    if operation is None:
        raise RuntimeAdmissionError("atomic no-replace rename is unavailable")
    operation.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    operation.restype = ctypes.c_int
    ctypes.set_errno(0)
    if operation(directory_fd, source_bytes, directory_fd, target_bytes, flags) == 0:
        return
    failure = ctypes.get_errno()
    if failure in {errno.EEXIST, errno.ENOTEMPTY}:
        raise RuntimeAdmissionError(message)
    raise OSError(failure, os.strerror(failure), target)


def _archive_entries_are_exact(archive: Path, expected: Sequence[str]) -> bool:
    """Bind the archive root itself, not just its nested lock contents."""
    try:
        _private_directory(archive, message="quarantine forensic archive is unsafe")
        return sorted(item.name for item in archive.iterdir()) == sorted(expected)
    except (OSError, RuntimeAdmissionError):
        return False


def _archive_pair_is_exact(
    archive: Path, pair: Mapping[str, Any], *, receipt: bool | None = False
) -> bool:
    """Verify the complete archive root and every evidence-bearing member.

    ``None`` accepts the narrow crash prefix after receipt creation but before
    the guard phase advances; any receipt in that prefix is still separately
    schema/hash validated by the caller before it can be trusted.
    """
    lock = archive / ".cre.lock"
    authority = archive / ".cre.lock.authority"
    root_pair = [lock.name, authority.name]
    root_with_receipt = [*root_pair, "recovery-receipt.json"]
    if receipt is True:
        expected_roots = [root_with_receipt]
    elif receipt is False:
        expected_roots = [root_pair]
    else:
        expected_roots = [root_pair, root_with_receipt]
    if not any(
        _archive_entries_are_exact(archive, entries) for entries in expected_roots
    ):
        return False
    try:
        _private_directory(lock, message="quarantine forensic lock is unsafe")
    except RuntimeAdmissionError:
        return False
    if not _same_identity(lock, pair.get("lock_identity", [])) or not _same_identity(
        authority, pair.get("authority_identity", [])
    ):
        return False
    try:
        if sorted(item.name for item in lock.iterdir()) != sorted(
            [
                checkpoint_refresh.BENCHMARK_ACTIVE_MARKER,
                checkpoint_refresh.BENCHMARK_QUARANTINE_MARKER,
            ]
        ):
            return False
    except OSError:
        return False
    try:
        _active, active_evidence = _regular_file_evidence(
            lock / checkpoint_refresh.BENCHMARK_ACTIVE_MARKER
        )
        _quarantine, quarantine_evidence = _regular_file_evidence(
            lock / checkpoint_refresh.BENCHMARK_QUARANTINE_MARKER
        )
        _authority, authority_evidence = _regular_file_evidence(authority, limit=512)
    except (OSError, RuntimeAdmissionError):
        return False
    return (
        active_evidence == pair.get("active_evidence")
        and quarantine_evidence == pair.get("quarantine_evidence")
        and authority_evidence == pair.get("authority_evidence")
    )


def _archive_phase_entries_are_exact(archive: Path, phase: str) -> bool:
    """Accept only the durable namespace prefixes for one recovery phase."""
    lock = ".cre.lock"
    authority = ".cre.lock.authority"
    receipt = "recovery-receipt.json"
    allowed: dict[str, tuple[Sequence[str], ...]] = {
        "prepared": ((),),
        # A crash may occur after the durable intent but before/after rename.
        "lock-renaming": ((), (lock,)),
        "lock-archived": ((lock,),),
        "authority-renaming": ((lock,), (lock, authority)),
        # Receipt creation is durable before the phase guard advances.
        "pair-archived": ((lock, authority), (lock, authority, receipt)),
        "receipt-written": ((lock, authority, receipt),),
        "completed": ((lock, authority, receipt),),
    }
    return phase in allowed and any(
        _archive_entries_are_exact(archive, entries) for entries in allowed[phase]
    )


def _recovery_receipt_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    completed = {**result, "phase": "archived", "executed": True}
    completed["receipt_sha256"] = _hash(completed)
    return completed


def _write_recovery_receipt(path: Path, result: Mapping[str, Any]) -> dict[str, Any]:
    """Create one no-follow receipt, file-fsync it, then fsync its parent."""
    _private_directory(path.parent, message="quarantine forensic archive is unsafe")
    receipt = _recovery_receipt_payload(result)
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
    except FileExistsError as exc:
        raise RuntimeAdmissionError(
            "quarantine recovery receipt already exists"
        ) from exc
    try:
        os.fchmod(descriptor, 0o600)
        identity = _guard_identity(descriptor, path)
        guard_journal.write_all(
            descriptor,
            _canonical(receipt) + b"\n",
            message="quarantine recovery receipt write was short",
        )
        os.fsync(descriptor)
        if _guard_identity(descriptor, path) != identity:
            raise RuntimeAdmissionError("quarantine recovery receipt changed")
    finally:
        os.close(descriptor)
    fsync_directory(path.parent)
    return receipt


def _validate_recovery_receipt(
    path: Path,
    *,
    lock_path: Path,
    archive: Path,
    pair: Mapping[str, Any],
) -> dict[str, Any]:
    """Accept only a complete receipt bound to this exact archived pair."""
    raw, _ = _regular_bytes(path, limit=8192)
    try:
        receipt = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeAdmissionError("quarantine recovery receipt is malformed") from exc
    if not isinstance(receipt, dict):
        raise RuntimeAdmissionError("quarantine recovery receipt is malformed")
    supplied_hash = receipt.get("receipt_sha256")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if (
        receipt.get("schema_version") != SCHEMA_VERSION
        or receipt.get("kind") != QUARANTINE_RECOVERY_KIND
        or receipt.get("phase") != "archived"
        or receipt.get("executed") is not True
        or receipt.get("lock_path") != str(lock_path)
        or receipt.get("archive") != str(archive)
        or receipt.get("pair") != dict(pair)
        or not isinstance(supplied_hash, str)
        or supplied_hash != _hash(unsigned)
    ):
        raise RuntimeAdmissionError("quarantine recovery receipt is not bound")
    return receipt


def completed_guard_evidence_is_valid(
    state: Mapping[str, Any], lock_path: Path
) -> bool:
    """Validate a terminal journal state without touching a later lock cycle."""
    try:
        if (
            state.get("phase") != "completed"
            or state.get("kind") != QUARANTINE_RECOVERY_KIND
            or state.get("lock_path") != str(lock_path)
            or not isinstance(state.get("pair"), Mapping)
            or not isinstance(state.get("archive"), str)
            or not isinstance(state.get("completed_receipt_sha256"), str)
        ):
            return False
        archive = Path(state["archive"])
        pair = dict(state["pair"])
        if archive.parent != lock_path.parent / QUARANTINE_ARCHIVE_DIR:
            return False
        if not _archive_pair_is_exact(archive, pair, receipt=True):
            return False
        receipt_path = archive / "recovery-receipt.json"
        _validate_recovery_receipt(
            receipt_path, lock_path=lock_path, archive=archive, pair=pair
        )
        _raw, evidence = _regular_file_evidence(receipt_path, limit=8192)
        return evidence["sha256"] == state["completed_receipt_sha256"]
    except (OSError, RuntimeAdmissionError, TypeError, ValueError):
        return False


def completed_guard_allows_acquire(path: Path, lock_path: Path) -> bool:
    """Compatibility wrapper around the explicit journal admission API."""
    return guard_journal.completed_allows_acquire(
        path, lock_path, validate_completed=completed_guard_evidence_is_valid
    )


def recover_quarantine(
    *,
    execute: bool,
    config: QuarantineRecoveryConfig,
    callbacks: QuarantineRecoveryCallbacks,
) -> QuarantineRecoveryResult:
    """Serialize the explicit paired archive against every normal acquire."""
    lock_path = config.lock_path
    try:
        synchronizer = checkpoint_refresh.acquire_quarantine_recovery_sync(lock_path)
    except LockHeldError as exc:
        raise QuarantineRecoveryError(
            "quarantine recovery is already in progress"
        ) from exc
    try:
        try:
            return _recover_quarantine_while_synchronized(
                execute=execute, config=config, callbacks=callbacks
            )
        except RuntimeAdmissionError as exc:
            raise QuarantineRecoveryError(str(exc)) from exc
    finally:
        # A fault before the namespace-operation try/finally still must not
        # leak a retained descriptor or contaminate a later same-process
        # operator invocation.
        active = guard_journal.active()
        if active is not None:
            _close_guard_journal(active)
            guard_journal.clear_active()
        checkpoint_refresh.release_quarantine_recovery_sync(synchronizer)


def _recover_quarantine_while_synchronized(
    *,
    execute: bool,
    config: QuarantineRecoveryConfig,
    callbacks: QuarantineRecoveryCallbacks,
) -> QuarantineRecoveryResult:
    """Durably archive one exact historic residue as a matched forensic pair."""
    lock_path = config.lock_path
    authority = lock_path.with_name(f"{lock_path.name}.authority")
    guard_path = _recovery_guard_path(lock_path)
    guard = _read_recovery_guard(guard_path) if guard_path.exists() else None
    if guard is not None and (
        guard.get("kind") != QUARANTINE_RECOVERY_KIND
        or guard.get("lock_path") != str(lock_path)
        or guard.get("phase")
        not in {
            "prepared",
            "lock-renaming",
            "lock-archived",
            "authority-renaming",
            "pair-archived",
            "receipt-written",
            "completed",
        }
        or not isinstance(guard.get("pair"), Mapping)
        or not isinstance(guard.get("archive"), str)
    ):
        raise RuntimeAdmissionError("quarantine recovery guard is not replay-safe")
    if guard is None:
        pair = _quarantine_pair(lock_path)
        operation = hashlib.sha256(_canonical(pair)).hexdigest()
        archive = lock_path.parent / config.archive_dir_name / operation
        result: dict[str, Any] = {
            "schema_version": config.schema_version,
            "kind": config.recovery_kind,
            "lock_path": str(lock_path),
            "pair": pair,
            "archive": str(archive),
            "operation_id": secrets.token_hex(16),
            "phase": "prepared",
        }
    else:
        pair = dict(guard["pair"])
        archive = Path(guard["archive"])
        if archive.parent != lock_path.parent / config.archive_dir_name:
            raise RuntimeAdmissionError("quarantine recovery archive target is invalid")
        result = dict(guard)
    # Before the first durable guard, require all live safety evidence.  A
    # resumed guarded operation still rechecks the runtime before it can clear
    # the guard, but does not reinterpret the historical source artifact.
    cpu = dict(callbacks.cpu_evidence())
    endpoints = callbacks.compose_endpoints(callbacks.runner)
    settlement = callbacks.settlement(callbacks.runner, endpoints)
    profile, _ = callbacks.load_baseline_profile()
    capture = callbacks.capture_runtime(callbacks.runner)
    checks = callbacks.evaluate_state(capture.public, profile, "baseline")
    if not all(checks.values()):
        raise RuntimeAdmissionError(
            "quarantine recovery requires an exact idle baseline runtime"
        )
    result.update({"cpu": cpu, "checks": checks, "settlement": settlement})
    if not execute:
        return {**result, "executed": False}

    archive_root = lock_path.parent / config.archive_dir_name
    archive_root.mkdir(mode=0o700, exist_ok=True)
    _private_directory(archive_root, message="quarantine forensic archive is unsafe")
    journal: _GuardJournal | None = None
    journal_context: object | None = None
    if guard is None:
        # The guard is the first durable operation artifact.  A crash before
        # archive creation can therefore be replayed by creating the exact
        # deterministic directory, rather than stranding the source pair.
        journal = _create_guard_journal(guard_path, result)
        journal_context = guard_journal.activate(journal)
        _write_recovery_guard(guard_path, result, create=True)
        archive.mkdir(mode=0o700, exist_ok=False)
        fsync_directory(archive_root)
    else:
        # Reopen the retained journal only after read-only admission succeeds.
        # Any replaced or torn guard fails before the first namespace mutation.
        journal = _open_guard_journal(guard_path)
        journal_context = guard_journal.activate(journal)
        result = dict(journal.state)
        pair = dict(result["pair"])
        archive = Path(str(result["archive"]))
        if archive.parent != lock_path.parent / config.archive_dir_name:
            _close_guard_journal(journal)
            raise RuntimeAdmissionError("quarantine recovery archive target is invalid")
        result.update({"cpu": cpu, "checks": checks, "settlement": settlement})
    assert journal is not None
    if result.get("phase") == "completed":
        if not completed_guard_evidence_is_valid(result, lock_path):
            _close_guard_journal(journal)
            raise RuntimeAdmissionError("quarantine recovery guard is not replay-safe")
        if _path_is_absent(lock_path) and _path_is_absent(authority):
            try:
                return {**result, "executed": True, "already_completed": True}
            finally:
                _close_guard_journal(journal)
        # A completed prior recovery remains immutable evidence.  A subsequent
        # exact quarantined pair starts a distinct append-only journal cycle.
        pair = _quarantine_pair(lock_path)
        operation_id = secrets.token_hex(16)
        operation = hashlib.sha256(
            _canonical({"pair": pair, "operation_id": operation_id})
        ).hexdigest()
        archive = archive_root / operation
        result = {
            "schema_version": config.schema_version,
            "kind": config.recovery_kind,
            "lock_path": str(lock_path),
            "pair": pair,
            "archive": str(archive),
            "operation_id": operation_id,
            "phase": "prepared",
            "cpu": cpu,
            "checks": checks,
            "settlement": settlement,
        }
        _write_recovery_guard(guard_path, result, create=False)
        archive.mkdir(mode=0o700, exist_ok=False)
        fsync_directory(archive_root)
        guard = None
    if guard is not None and not archive.exists():
        if result.get("phase") != "prepared":
            _close_guard_journal(journal)
            raise RuntimeAdmissionError("quarantine recovery archive is missing")
        archive.mkdir(mode=0o700, exist_ok=False)
        fsync_directory(archive_root)
    elif guard is not None:
        try:
            _private_directory(archive, message="quarantine recovery archive changed")
        except RuntimeAdmissionError:
            _close_guard_journal(journal)
            raise RuntimeAdmissionError("quarantine recovery archive changed")
    # An interrupted first creation can leave the deterministic archive name
    # visible but its parent-entry durability unknown.  A replay must repair
    # that acknowledgement before it advances the guard or moves either
    # canonical artifact into the archive.
    fsync_directory(archive_root)

    phase = str(result["phase"])
    if not _archive_phase_entries_are_exact(archive, phase):
        raise RuntimeAdmissionError("quarantine recovery archive entries changed")
    archived_lock = archive / lock_path.name
    archived_authority = archive / authority.name
    held_authority = authority
    if phase in {"pair-archived", "receipt-written"}:
        held_authority = archived_authority
    elif phase == "authority-renaming" and not authority.exists():
        held_authority = archived_authority
    descriptor = -1
    try:
        descriptor = os.open(held_authority, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeAdmissionError("quarantine authority is held") from exc
        if phase == "prepared":
            # Revalidate the exact pair immediately before mutation.  No generic
            # lock, malformed authority, or a live owner can enter this path.
            _empty_private_directory(
                archive, message="quarantine recovery archive changed"
            )
            if _quarantine_pair(lock_path) != pair:
                raise RuntimeAdmissionError("quarantine source changed before archive")
            if not _same_identity(authority, pair["authority_identity"]):
                raise RuntimeAdmissionError(
                    "quarantine authority changed before archive"
                )
            result["phase"] = "lock-renaming"
            _write_recovery_guard(guard_path, result, create=False)
            phase = "lock-renaming"
        if phase == "lock-renaming":
            source_present = _same_identity(lock_path, pair["lock_identity"])
            archived_present = _same_identity(archived_lock, pair["lock_identity"])
            if source_present and not archived_present:
                _rename_exact_to_empty_target(
                    lock_path,
                    archived_lock,
                    pair["lock_identity"],
                    message="quarantine lock handoff changed",
                )
            elif not source_present and archived_present:
                # An archived expected inode is replayable only when its
                # canonical source name is genuinely absent.  A foreign or
                # reappeared source is forensic evidence, never absence.
                if not _path_is_absent(lock_path):
                    raise RuntimeAdmissionError("quarantine lock handoff changed")
            else:
                raise RuntimeAdmissionError("quarantine lock handoff changed")
            if not _archive_entries_are_exact(archive, [archived_lock.name]):
                raise RuntimeAdmissionError("quarantine lock archive entries changed")
            # A cross-directory rename must first make the archive entry
            # durable.  Fsyncing the source parent first could persist the
            # removal while losing the only forensic destination on power loss.
            fsync_directory(archive)
            fsync_directory(lock_path.parent)
            result["phase"] = "lock-archived"
            _write_recovery_guard(guard_path, result, create=False)
            phase = "lock-archived"
        if phase == "lock-archived":
            _require_phase_source_absence(phase, lock_path, authority)
            if not _archive_entries_are_exact(archive, [archived_lock.name]):
                raise RuntimeAdmissionError("quarantine lock archive entries changed")
            if not _same_identity(archived_lock, pair["lock_identity"]):
                raise RuntimeAdmissionError("quarantine lock archive changed")
            if not _same_identity(authority, pair["authority_identity"]):
                raise RuntimeAdmissionError(
                    "quarantine authority changed before archive"
                )
            result["phase"] = "authority-renaming"
            _write_recovery_guard(guard_path, result, create=False)
            phase = "authority-renaming"
        if phase == "authority-renaming":
            _require_phase_source_absence(phase, lock_path, authority)
            source_present = _same_identity(authority, pair["authority_identity"])
            archived_present = _same_identity(
                archived_authority, pair["authority_identity"]
            )
            if source_present and not archived_present:
                _rename_exact_to_empty_target(
                    authority,
                    archived_authority,
                    pair["authority_identity"],
                    message="quarantine authority handoff changed",
                )
            elif not source_present and archived_present:
                # Apply the same strict absence rule to the authority sidecar
                # before any further paired-archive mutation can occur.
                if not _path_is_absent(authority):
                    raise RuntimeAdmissionError("quarantine authority handoff changed")
            else:
                raise RuntimeAdmissionError("quarantine authority handoff changed")
            if not _archive_entries_are_exact(
                archive, [archived_lock.name, archived_authority.name]
            ):
                raise RuntimeAdmissionError(
                    "quarantine authority archive entries changed"
                )
            # Keep the paired authority handoff in the same destination-first
            # durability order as the lock directory above.
            fsync_directory(archive)
            fsync_directory(lock_path.parent)
            result["phase"] = "pair-archived"
            _write_recovery_guard(guard_path, result, create=False)
            phase = "pair-archived"
        if phase == "pair-archived":
            _require_phase_source_absence(phase, lock_path, authority)
            if not _archive_pair_is_exact(archive, pair, receipt=None):
                raise RuntimeAdmissionError("quarantine forensic pair changed")
            receipt = archive / "recovery-receipt.json"
            if receipt.exists():
                _validate_recovery_receipt(
                    receipt, lock_path=lock_path, archive=archive, pair=pair
                )
            else:
                _write_recovery_receipt(receipt, result)
            # A receipt may have been file-fsynced just before its containing
            # archive directory fsync failed.  Replaying must repair that
            # directory-entry durability before it advances the phase/guard.
            fsync_directory(archive)
            if not _archive_pair_is_exact(archive, pair, receipt=True):
                raise RuntimeAdmissionError("quarantine forensic pair changed")
            _validate_recovery_receipt(
                receipt, lock_path=lock_path, archive=archive, pair=pair
            )
            result["phase"] = "receipt-written"
            _write_recovery_guard(guard_path, result, create=False)
            phase = "receipt-written"
        if phase != "receipt-written" or not _archive_pair_is_exact(
            archive, pair, receipt=True
        ):
            raise RuntimeAdmissionError("quarantine recovery did not complete safely")
        _require_phase_source_absence(phase, lock_path, authority)
        _validate_recovery_receipt(
            archive / "recovery-receipt.json",
            lock_path=lock_path,
            archive=archive,
            pair=pair,
        )
        receipt_raw, receipt_evidence = _regular_file_evidence(
            archive / "recovery-receipt.json", limit=8192
        )
        if not receipt_raw:
            raise RuntimeAdmissionError("quarantine recovery receipt changed")
        result["phase"] = "completed"
        result["completed_receipt_sha256"] = receipt_evidence["sha256"]
        _write_recovery_guard(guard_path, result, create=False)
        return {**result, "executed": True, "archive": str(archive)}
    finally:
        if descriptor >= 0:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        if journal_context is not None:
            guard_journal.deactivate(journal_context)
        _close_guard_journal(journal)
