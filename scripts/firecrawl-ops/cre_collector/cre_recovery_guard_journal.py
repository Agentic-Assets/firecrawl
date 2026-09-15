"""Private, FD-bound journal for governed CRE quarantine recovery guards."""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cre_capacity_errors import RuntimeAdmissionError

JOURNAL_VERSION = 1
JOURNAL_MAX_BYTES = 65536


@dataclass
class GuardJournal:
    """One no-follow, identity-bound, append-only guard journal."""

    path: Path
    descriptor: int
    identity: tuple[int, int]
    state: dict[str, Any]
    sequence: int
    record_sha256: str | None
    valid_size: int


_ACTIVE_JOURNAL: contextvars.ContextVar[GuardJournal | None] = contextvars.ContextVar(
    "active_cre_quarantine_guard_journal", default=None
)


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _parent_is_safe(path: Path) -> None:
    try:
        parent_stat = path.parent.lstat()
    except OSError as exc:
        raise RuntimeAdmissionError(
            "quarantine recovery parent is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.geteuid()
        or stat.S_IMODE(parent_stat.st_mode) & 0o022
    ):
        raise RuntimeAdmissionError("quarantine recovery parent is unsafe")


def write_all(descriptor: int, raw: bytes, *, message: str) -> None:
    payload = memoryview(raw)
    while payload:
        written = os.write(descriptor, payload)
        if written <= 0:
            raise OSError(message)
        payload = payload[written:]


def guard_identity(descriptor: int, path: Path) -> tuple[int, int]:
    observed = os.fstat(descriptor)
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or observed.st_uid != os.geteuid()
        or stat.S_IMODE(observed.st_mode) != 0o600
    ):
        raise RuntimeAdmissionError("quarantine recovery guard is unsafe")
    try:
        named = path.lstat()
    except OSError as exc:
        raise RuntimeAdmissionError("quarantine recovery guard changed") from exc
    if (
        not stat.S_ISREG(named.st_mode)
        or named.st_nlink != 1
        or named.st_uid != os.geteuid()
        or stat.S_IMODE(named.st_mode) != 0o600
        or (named.st_dev, named.st_ino) != (observed.st_dev, observed.st_ino)
    ):
        raise RuntimeAdmissionError("quarantine recovery guard changed")
    return (observed.st_dev, observed.st_ino)


def guard_record(
    state: Mapping[str, Any], sequence: int, previous: str | None
) -> tuple[bytes, str]:
    unsigned = {
        "journal_version": JOURNAL_VERSION,
        "sequence": sequence,
        "previous_sha256": previous,
        "state": dict(state),
    }
    record_sha256 = digest(unsigned)
    return canonical(
        {**unsigned, "record_sha256": record_sha256}
    ) + b"\n", record_sha256


def _read(descriptor: int, path: Path) -> tuple[dict[str, Any], int, str, int]:
    identity = guard_identity(descriptor, path)
    observed = os.fstat(descriptor)
    if observed.st_size <= 0 or observed.st_size > JOURNAL_MAX_BYTES:
        raise RuntimeAdmissionError("quarantine recovery guard is malformed")
    raw = os.pread(descriptor, observed.st_size, 0)
    if len(raw) != observed.st_size:
        raise RuntimeAdmissionError("quarantine recovery guard changed")
    state: dict[str, Any] | None = None
    sequence = 0
    previous: str | None = None
    valid_size = 0
    for index, line in enumerate(raw.splitlines(keepends=True)):
        if not line.endswith(b"\n"):
            if index == len(raw.splitlines(keepends=True)) - 1:
                break
            raise RuntimeAdmissionError("quarantine recovery guard is malformed")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeAdmissionError(
                "quarantine recovery guard is malformed"
            ) from exc
        if not isinstance(record, dict):
            raise RuntimeAdmissionError("quarantine recovery guard is malformed")
        supplied = record.get("record_sha256")
        unsigned = {
            key: value for key, value in record.items() if key != "record_sha256"
        }
        candidate = unsigned.get("state")
        if (
            unsigned.get("journal_version") != JOURNAL_VERSION
            or unsigned.get("sequence") != sequence + 1
            or unsigned.get("previous_sha256") != previous
            or not isinstance(candidate, dict)
            or not isinstance(supplied, str)
            or supplied != digest(unsigned)
        ):
            raise RuntimeAdmissionError("quarantine recovery guard is malformed")
        state = dict(candidate)
        sequence += 1
        previous = supplied
        valid_size += len(line)
    if (
        state is None
        or previous is None
        or guard_identity(descriptor, path) != identity
    ):
        raise RuntimeAdmissionError("quarantine recovery guard is malformed")
    return state, sequence, previous, valid_size


def open_journal(path: Path) -> GuardJournal:
    _parent_is_safe(path)
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except OSError as exc:
        raise RuntimeAdmissionError("quarantine recovery guard is unsafe") from exc
    try:
        identity = guard_identity(descriptor, path)
        state, sequence, previous, valid_size = _read(descriptor, path)
        return GuardJournal(
            path, descriptor, identity, state, sequence, previous, valid_size
        )
    except BaseException:
        os.close(descriptor)
        raise


def append(journal: GuardJournal, state: Mapping[str, Any]) -> None:
    """Append a bounded fsynced record through the retained no-follow FD."""
    if guard_identity(journal.descriptor, journal.path) != journal.identity:
        raise RuntimeAdmissionError("quarantine recovery guard changed")
    observed = os.fstat(journal.descriptor)
    if observed.st_size < journal.valid_size:
        raise RuntimeAdmissionError("quarantine recovery guard changed")
    if observed.st_size != journal.valid_size:
        os.ftruncate(journal.descriptor, journal.valid_size)
        os.fsync(journal.descriptor)
        if guard_identity(journal.descriptor, journal.path) != journal.identity:
            raise RuntimeAdmissionError("quarantine recovery guard changed")
    raw, record_sha256 = guard_record(
        state, journal.sequence + 1, journal.record_sha256
    )
    if journal.valid_size + len(raw) > JOURNAL_MAX_BYTES:
        raise RuntimeAdmissionError("quarantine recovery guard journal is full")
    os.lseek(journal.descriptor, 0, os.SEEK_END)
    write_all(
        journal.descriptor, raw, message="quarantine recovery guard write was short"
    )
    os.fsync(journal.descriptor)
    if guard_identity(journal.descriptor, journal.path) != journal.identity:
        raise RuntimeAdmissionError("quarantine recovery guard changed")
    journal.state = dict(state)
    journal.sequence += 1
    journal.record_sha256 = record_sha256
    journal.valid_size += len(raw)


def create(
    path: Path, state: Mapping[str, Any], *, fsync_parent: Callable[[Path], None]
) -> GuardJournal:
    _parent_is_safe(path)
    try:
        descriptor = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
    except FileExistsError as exc:
        raise RuntimeAdmissionError("quarantine recovery guard already exists") from exc
    try:
        os.fchmod(descriptor, 0o600)
        identity = guard_identity(descriptor, path)
        journal = GuardJournal(path, descriptor, identity, {}, 0, None, 0)
        append(journal, state)
        fsync_parent(path.parent)
        return journal
    except BaseException:
        os.close(descriptor)
        raise


def close(journal: GuardJournal | None) -> None:
    if journal is not None and journal.descriptor >= 0:
        os.close(journal.descriptor)
        journal.descriptor = -1


def activate(journal: GuardJournal) -> contextvars.Token[GuardJournal | None]:
    return _ACTIVE_JOURNAL.set(journal)


def deactivate(token: contextvars.Token[GuardJournal | None]) -> None:
    _ACTIVE_JOURNAL.reset(token)


def active() -> GuardJournal | None:
    return _ACTIVE_JOURNAL.get()


def clear_active() -> None:
    """Drop a fault-leaked operation context after closing its descriptor."""
    _ACTIVE_JOURNAL.set(None)


def completed_allows_acquire(
    path: Path,
    lock_path: Path,
    *,
    validate_completed: Callable[[Mapping[str, Any], Path], bool],
) -> bool:
    """Admit only an exact completed state while retaining its journal."""
    try:
        journal = open_journal(path)
    except RuntimeAdmissionError:
        return False
    try:
        authority = lock_path.with_name(f"{lock_path.name}.authority")
        return (
            validate_completed(journal.state, lock_path)
            and not lock_path.exists()
            and not authority.exists()
        )
    finally:
        close(journal)


def write(
    path: Path,
    state: Mapping[str, Any],
    *,
    create_record: bool,
    fsync_parent: Callable[[Path], None],
) -> None:
    """Use the retained operation journal, or a safe isolated fixture journal."""
    current = active()
    if current is not None:
        if current.path != path:
            raise RuntimeAdmissionError("quarantine recovery guard changed")
        if not create_record:
            append(current, state)
        return
    if create_record:
        journal = create(path, state, fsync_parent=fsync_parent)
    else:
        journal = open_journal(path)
        try:
            append(journal, state)
        finally:
            close(journal)
        return
    close(journal)
