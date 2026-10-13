"""Cooperatively stop one exact Linux CLI owner; never recover its fence."""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import signal
import stat
import sys
import time
from pathlib import Path

import cre_checkpoint_refresh as checkpoint

OUT_ROOT = Path(__file__).resolve().parent / "out" / "guarded-tiers"
RUN_ID = re.compile(r"\d{4}-\d{2}-\d{2}T\d{6}Z-[0-9a-f]{12}")


class StopRefused(RuntimeError):
    """Untrusted, changed, or unavailable local ownership evidence."""


def _identity(observed):
    return [observed.st_dev, observed.st_ino]


def _safe_stat(descriptor, *, directory=False, private=False):
    observed = os.fstat(descriptor)
    valid_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not valid_type(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or observed.st_mode & (0o077 if private else 0o022)
        or (not directory and observed.st_nlink != 1)
    ):
        raise StopRefused("unsafe local evidence")
    return observed


def _read_at(directory_fd, name, limit=16384):
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
    )
    try:
        observed = _safe_stat(descriptor, private=True)
        data = os.read(descriptor, limit + 1)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            len(data) > limit
            or len(data) != observed.st_size
            or _identity(named) != _identity(observed)
        ):
            raise StopRefused("changed or oversized local evidence")
        return data, observed
    finally:
        os.close(descriptor)


def _read_receipt(root, run_id):
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _safe_stat(root_fd, directory=True)
        directory_fd = os.open(
            run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd
        )
        try:
            observed = _safe_stat(directory_fd, directory=True, private=True)
            raw, _ = _read_at(directory_fd, "receipt.json")
            receipt = json.loads(raw)
            if not isinstance(receipt, dict):
                raise StopRefused("invalid receipt")
            owner = receipt.get("stop_owner")
            if (
                not isinstance(owner, dict)
                or type(owner.get("schema_version")) is not int
                or owner["schema_version"] != 1
                or owner.get("owner_role") != "guarded_tier_one_shot_cli"
                or owner.get("run_id") != run_id
                or owner.get("run_directory_identity") != _identity(observed)
                or receipt.get("tier") not in {"monitor", "enrich"}
            ):
                raise StopRefused("receipt has no supported stop capability")
            return receipt
        finally:
            os.close(directory_fd)
    finally:
        os.close(root_fd)


def _proc_text(path, limit=16384):
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise StopRefused("oversized process evidence")
    return data.decode("ascii")


def _process_identity(pid):
    # comm can contain spaces and ')'; the final ')' precedes field 3.
    prefix, separator, tail = _proc_text(f"/proc/{pid}/stat").rpartition(")")
    fields = tail.split()
    if (
        not separator
        or not prefix.startswith(f"{pid} (")
        or len(fields) < 20
        or len(fields[0]) != 1
        or fields[0] not in "RSDTtKWPI"
    ):
        raise StopRefused("process identity is unavailable")
    start_ticks = int(fields[19])
    status = _proc_text(f"/proc/{pid}/status")
    uid_lines = [
        line.split()[1:] for line in status.splitlines() if line.startswith("Uid:")
    ]
    if len(uid_lines) != 1 or [int(v) for v in uid_lines[0]] != [os.geteuid()] * 4:
        raise StopRefused("process user differs")
    boot = _proc_text("/proc/sys/kernel/random/boot_id", 128).strip()
    if (
        start_ticks <= 0
        or re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot) is None
    ):
        raise StopRefused("invalid process birth identity")
    return {
        "pid": pid,
        "start_ticks": start_ticks,
        "boot_id": boot,
        "uid": os.geteuid(),
        "pid_namespace_identity": _identity(os.stat(f"/proc/{pid}/ns/pid")),
    }


def _authority(lock_path):
    parent_fd = os.open(lock_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _safe_stat(parent_fd, directory=True)
        name = lock_path.name + checkpoint.LOCK_AUTHORITY_SUFFIX
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd
        )
        try:
            observed = _safe_stat(descriptor, private=True)
            fields = checkpoint.SharedLock._authority_fields(descriptor)
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                fields is None
                or not fields[3]
                or _identity(named) != _identity(observed)
            ):
                raise StopRefused("authority is not recovery fenced")
            pid_owner, token, generation, _ = fields
        finally:
            os.close(descriptor)
        directory_fd = os.open(
            lock_path.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        try:
            directory = _safe_stat(directory_fd, directory=True)
            lease, _ = _read_at(directory_fd, "lease", 256)
            pid, _ = _read_at(directory_fd, "pid", 128)
            pid_fields = pid.split()
            if (
                lease.decode("ascii").strip() != generation
                or len(pid_fields) != 2
                or int(pid_fields[0]) != pid_owner
                or int(pid_fields[1]) <= 0
                or not generation.startswith(checkpoint.OPERATOR_RECOVERY_LEASE_PREFIX)
            ):
                raise StopRefused("authority and directory disagree")
            return {
                "authority_identity": _identity(observed),
                "authority_token": token,
                "authority_generation": generation,
                "lease_token": lease.decode("ascii").strip(),
                "lock_directory_identity": _identity(directory),
                "pid": pid_owner,
            }
        finally:
            os.close(directory_fd)
    finally:
        os.close(parent_fd)


def _held_flock(owner):
    pid, descriptor = owner["pid"], owner["authority_fd"]
    if type(descriptor) is not int or descriptor < 0:
        raise StopRefused("invalid authority descriptor")
    observed = os.stat(f"/proc/{pid}/fd/{descriptor}")
    if (
        not stat.S_ISREG(observed.st_mode)
        or _identity(observed) != owner["authority_identity"]
    ):
        raise StopRefused("owner authority descriptor changed")
    text = _proc_text(f"/proc/{pid}/fdinfo/{descriptor}")
    witness = re.compile(
        r"^lock:\s+\d+: FLOCK\s+ADVISORY\s+WRITE\s+(\d+)\s+([0-9a-f]+):([0-9a-f]+):(\d+)\s+0 EOF$",
        re.MULTILINE,
    )
    expected = (
        pid,
        os.major(observed.st_dev),
        os.minor(observed.st_dev),
        observed.st_ino,
    )
    found = [
        (int(p), int(major, 16), int(minor, 16), int(ino))
        for p, major, minor, ino in witness.findall(text)
    ]
    if found != [expected]:
        raise StopRefused("held authority flock is unavailable")


def publish_stop_identity(lock, run_dir):
    """Only the one-shot main CLI calls this after installing its handler."""
    if sys.platform != "linux":
        return None
    owner = {
        "schema_version": 1,
        "owner_role": "guarded_tier_one_shot_cli",
        "run_id": run_dir.name,
        "run_directory_identity": _identity(run_dir.stat()),
        **_process_identity(os.getpid()),
        "authority_fd": lock.authority_fd,
        "authority_identity": list(lock.authority_identity),
        "authority_token": lock.authority_token,
        "authority_generation": lock.authority_generation,
        "lease_token": lock.lease_token,
        "lock_directory_identity": list(lock.directory_identity),
    }
    _held_flock(owner)
    return owner


def _validate_process(owner):
    pid = owner.get("pid")
    if type(pid) is not int or not 1 < pid <= 2147483647 or pid == os.getpid():
        raise StopRefused("invalid stop owner")
    for key, value in _process_identity(pid).items():
        if owner.get(key) != value:
            raise StopRefused("process birth identity changed")
    if owner["pid_namespace_identity"] != _identity(os.stat("/proc/self/ns/pid")):
        raise StopRefused("process namespace differs")


def _validate_owner(owner, receipt, lock_path):
    if receipt.get("stop_owner") != owner:
        raise StopRefused("owner binding changed")
    _validate_process(owner)
    for key, value in _authority(lock_path).items():
        if owner.get(key) != value:
            raise StopRefused("canonical ownership changed")
    _held_flock(owner)


def _exited(pidfd, milliseconds=0):
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    events = poller.poll(milliseconds)
    if any(flags & (select.POLLERR | select.POLLNVAL) for _, flags in events):
        raise StopRefused("process handle is unavailable")
    return bool(events)


def stop_run(run_id, wait_seconds=45, *, _out_root=None, _lock_path=None):
    """Hidden path seams are for private-clone tests; CLI paths are fixed."""
    result = {
        "run_id": run_id
        if isinstance(run_id, str) and RUN_ID.fullmatch(run_id)
        else None,
        "outcome": "refused",
        "signal_sent": False,
        "owner_exited": False,
        "local_settlement": "unknown",
        "recovery_fence": "not_checked",
        "terminal_status": None,
    }
    pidfd = None
    try:
        if (
            sys.platform != "linux"
            or type(wait_seconds) is not int
            or not 1 <= wait_seconds <= 120
            or not isinstance(run_id, str)
            or RUN_ID.fullmatch(run_id) is None
            or not hasattr(os, "pidfd_open")
            or not hasattr(signal, "pidfd_send_signal")
        ):
            raise StopRefused("unsupported stop invocation")
        root = OUT_ROOT if _out_root is None else _out_root
        lock_path = (
            checkpoint.canonical_shared_lock_dir() if _lock_path is None else _lock_path
        )
        receipt = _read_receipt(root, run_id)
        owner = receipt["stop_owner"]
        pid = owner.get("pid")
        if type(pid) is not int or not 1 < pid <= 2147483647 or pid == os.getpid():
            raise StopRefused("invalid stop owner")
        # No identity check grants permission to signal a bare numeric PID.
        pidfd = os.pidfd_open(pid, 0)
        deadline = time.monotonic() + wait_seconds
        if _exited(pidfd):
            raise StopRefused("owner already exited")
        _validate_process(owner)
        if receipt.get("status") == "running":
            _validate_owner(owner, _read_receipt(root, run_id), lock_path)
            fresh = _read_receipt(root, run_id)
            if fresh.get("status") == "running":
                _validate_owner(owner, fresh, lock_path)
                if _exited(pidfd):
                    raise StopRefused("owner exited before stop")
                signal.pidfd_send_signal(pidfd, signal.SIGTERM, None, 0)
                result["signal_sent"] = True
        elif receipt.get("status") != "complete":
            raise StopRefused("run is already terminal")
        remaining = max(0, int((deadline - time.monotonic()) * 1000))
        if not _exited(pidfd, remaining):
            result["outcome"] = "stop_outcome_unverified"
            return 2, result
        result["owner_exited"] = True
        terminal = _read_receipt(root, run_id)
        local = terminal.get("local_settlement")
        if (
            terminal.get("stop_owner") != owner
            or terminal.get("status")
            not in {
                "complete",
                "interrupted",
                "resource_guard_interrupted",
                "deadline_interrupted",
                "failed_recovery_required",
                "preflight_refused",
            }
            or terminal.get("guards_stopped") is not True
            or not isinstance(terminal.get("finished_at"), str)
            or not terminal["finished_at"]
            or not isinstance(local, dict)
            or local.get("state") not in {"confirmed", "not_started"}
        ):
            result["outcome"] = "stop_outcome_unverified"
            return 2, result
        result["local_settlement"] = local["state"]
        result["terminal_status"] = terminal["status"]
        if terminal["status"] == "complete":
            result["outcome"] = "completed"
            return 0, result
        retained = _authority(lock_path)
        for key in (
            "pid",
            "authority_identity",
            "authority_token",
            "authority_generation",
            "lease_token",
            "lock_directory_identity",
        ):
            if retained[key] != owner[key]:
                raise StopRefused("terminal fence ownership changed")
        result["recovery_fence"] = "retained"
        result["outcome"] = "terminal_local_settlement_confirmed"
        return 0, result
    except (
        OSError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        OverflowError,
        NotImplementedError,
        checkpoint.LockHeldError,
        StopRefused,
    ):
        if result["signal_sent"] or result["owner_exited"]:
            result["outcome"] = "stop_outcome_unverified"
            return 2, result
        return 1, result
    finally:
        if pidfd is not None:
            os.close(pidfd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--wait-seconds", type=int, default=45)
    args = parser.parse_args(argv)
    rc, result = stop_run(args.run_id, args.wait_seconds)
    print(json.dumps(result, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
