"""Explicit operator recovery of one exact, known-settled dead CRE owner.

Inspection is read-only. This tool never decides database/remote settlement,
replays work, adopts a lease, or automatically resumes an interrupted archive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path

import cre_checkpoint_refresh as cp
import cre_quarantine_recovery as q
from cre_posix import fcntl

EVIDENCE_ROLES = {"known_outcome", "remote_settlement", "descendant_settlement"}
ARCHIVE_ROOT = ".cre-owner-recovery-forensics"


class RecoveryAmbiguous(RuntimeError):
    """Neutral publication began; failure does not prove a retained fence."""


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _open_private(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o600:
            raise ValueError("not a private owned singleton file")
        if (path.lstat().st_dev, path.lstat().st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("private file pathname changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _file(path, *, record=False):
    descriptor = _open_private(path)
    try:
        opened = os.fstat(descriptor)
        digest = hashlib.sha256()
        size = 0
        raw = b""
        while block := os.read(descriptor, 65536):
            size += len(block)
            digest.update(block)
            if record:
                if size >= 512:
                    raise ValueError("canonical lock record oversized")
                raw += block
        named = path.lstat()
        after = os.fstat(descriptor)
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino) or (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
            raise ValueError("private file changed while reading")
        return {"identity": [opened.st_dev, opened.st_ino], "sha256": digest.hexdigest(), "size": size}, raw
    finally:
        os.close(descriptor)


def _state(lock):
    directory = q._private_directory(lock, message="canonical lock directory unsafe")
    if set(os.listdir(lock)) != {"pid", "lease"}:
        raise ValueError("unknown or interlocked lock entries")
    for marker in (cp.QUARANTINE_RECOVERY_GUARD, lock.name + ".reclaim"):
        if os.path.lexists(lock.parent / marker):
            raise ValueError("recovery/reclaim guard present")
    authority = lock.with_name(lock.name + cp.LOCK_AUTHORITY_SUFFIX)
    authority_info, raw = _file(authority, record=True)
    descriptor = _open_private(authority)
    try:
        fields = cp.SharedLock._authority_fields(descriptor)
        if cp.SharedLock._safe_authority_bytes(descriptor) != raw:
            raise ValueError("authority changed during inspection")
    finally:
        os.close(descriptor)
    if fields is None or not raw.startswith(b"v1 ") or not fields[3] or not fields[2].startswith(cp.OPERATOR_RECOVERY_LEASE_PREFIX):
        raise ValueError("not a canonical armed recovery authority")
    pid_info, pid = _file(lock / "pid", record=True)
    lease_info, lease = _file(lock / "lease", record=True)
    parts = pid.decode("utf-8").split()
    if len(parts) != 2 or not all(part.isdecimal() for part in parts) or int(parts[0]) != fields[0] or lease.decode("utf-8").strip() != fields[2]:
        raise ValueError("authority/pid/lease binding differs")
    if cp._pid_alive(fields[0]):
        raise cp.LockHeldError("owner is live; no recovery")
    named = lock.lstat()
    if (named.st_dev, named.st_ino) != (directory.st_dev, directory.st_ino):
        raise ValueError("canonical directory changed")
    state = {"schema": "cre-known-settled-owner-v1", "lock_path": str(lock),
             "directory_identity": [directory.st_dev, directory.st_ino],
             "authority": authority_info, "pid": pid_info, "lease": lease_info,
             "owner_pid": fields[0], "generation": fields[2]}
    state["fingerprint"] = hashlib.sha256(_canonical(state)).hexdigest()
    return state, raw


def inspect_state():
    """Observe only: never create synchronizers, archives, or other files."""
    return _state(cp.canonical_shared_lock_dir())[0]


def _evidence(value):
    if not isinstance(value, dict) or set(value) != EVIDENCE_ROLES:
        raise ValueError("all three immutable settlement evidence bindings required")
    checked = {}
    for role, row in value.items():
        if not isinstance(row, dict) or set(row) != {"path", "sha256"} or not isinstance(row["path"], str) or not Path(row["path"]).is_absolute() or not isinstance(row["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is None:
            raise ValueError("invalid immutable evidence binding")
        path = Path(row["path"])
        q._private_directory(path.parent, message="evidence directory unsafe")
        info, _raw = _file(path)
        if info["sha256"] != row["sha256"]:
            raise ValueError("settlement evidence bytes changed")
        checked[role] = {"path": str(path), **info}
    return checked


def _write_private(path, raw):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(raw)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("private forensic write was short")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_proof(source, target, expected):
    source_fd = _open_private(source)
    target_fd = -1
    try:
        target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.fchmod(target_fd, 0o600)
        digest = hashlib.sha256()
        while block := os.read(source_fd, 65536):
            digest.update(block)
            remaining = memoryview(block)
            while remaining:
                written = os.write(target_fd, remaining)
                if written <= 0:
                    raise OSError("forensic evidence copy was short")
                remaining = remaining[written:]
        if digest.hexdigest() != expected:
            raise ValueError("copied proof differs")
        os.fsync(target_fd)
    finally:
        os.close(source_fd)
        if target_fd >= 0:
            os.close(target_fd)


def _neutralize(descriptor, path, identity):
    opened = os.fstat(descriptor)
    if [opened.st_dev, opened.st_ino] != identity or not q._same_identity(path, identity):
        raise ValueError("persistent authority inode changed")
    os.ftruncate(descriptor, 0)
    os.lseek(descriptor, 0, os.SEEK_SET)
    remaining = memoryview(b"v1 neutral\n")
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("neutral authority write was short")
        remaining = remaining[written:]
    os.fsync(descriptor)
    if not q._same_identity(path, identity) or os.pread(descriptor, 512, 0) != b"v1 neutral\n":
        raise ValueError("neutral authority readback differs")


def recover(expected, *, evidence, operator, reference, confirmation, execute=False):
    """Explicitly archive exact dead armed owner, then neutralize same inode.

    The operator attests review of known DB/remote outcomes and fresh complete
    descendant settlement. File hashes bind that review; they do not infer it.
    """
    if operator not in {"cayman", "stace"} or not isinstance(reference, str) or re.fullmatch(r"AGENTIC-[0-9]+", reference) is None:
        raise ValueError("explicit configured operator/reference required")
    observed = inspect_state()
    if expected != observed or confirmation != "RECOVER CRE OWNER " + observed["fingerprint"]:
        raise ValueError("exact expected state/confirmation differs")
    proofs = _evidence(evidence)
    if not execute:
        return {"executed": False, "state": observed, "evidence": proofs}
    lock = cp.canonical_shared_lock_dir()
    synchronizer = cp.acquire_quarantine_recovery_sync(lock)
    descriptor = -1
    neutral_started = False
    try:
        authority = lock.with_name(lock.name + cp.LOCK_AUTHORITY_SUFFIX)
        descriptor = os.open(authority, os.O_RDWR | os.O_NOFOLLOW)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        opened = os.fstat(descriptor)
        if [opened.st_dev, opened.st_ino] != expected["authority"]["identity"]:
            raise ValueError("authority descriptor differs")
        current, authority_raw = _state(lock)
        if current != expected or _evidence(evidence) != proofs:
            raise ValueError("recovery CAS changed under boundary")
        root = lock.parent / ARCHIVE_ROOT
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        root_stat = q._private_directory(root, message="forensic root unsafe")
        if root_stat.st_dev != opened.st_dev:
            raise ValueError("forensic archive crosses filesystem")
        archive = root / expected["fingerprint"]
        archive.mkdir(mode=0o700)  # No overwrite, resume, or old-prefix adoption.
        _write_private(archive / "authority-before", authority_raw)
        for role, proof in proofs.items():
            _copy_proof(Path(proof["path"]), archive / (role + ".evidence"), proof["sha256"])
        prepared = {"status": "PREPARED_EXACT_KNOWN_SETTLED_RECOVERY", "expected": expected,
                    "operator": operator, "reference": reference, "evidence": proofs}
        _write_private(archive / "prepared.json", _canonical(prepared) + b"\n")
        q.fsync_directory(archive)
        q.fsync_directory(root)
        q.fsync_directory(lock.parent)
        if _state(lock)[0] != expected or _evidence(evidence) != proofs:
            raise ValueError("recovery CAS changed before archive")
        q._rename_exact_to_empty_target(lock, archive / "lock", expected["directory_identity"], message="exact recovery archive failed")
        q._private_directory(archive / "lock", message="archived lock unsafe")
        if set(os.listdir(archive / "lock")) != {"pid", "lease"} or any(_file(archive / "lock" / name, record=True)[0] != expected[name] for name in ("pid", "lease")):
            raise ValueError("archived originals changed")
        q.fsync_directory(archive)
        q.fsync_directory(lock.parent)
        authority_info, raw = _file(authority, record=True)
        if authority_info != expected["authority"] or raw != authority_raw or os.path.lexists(lock):
            raise ValueError("post-archive authority/source changed")
        neutral_started = True
        _neutralize(descriptor, authority, expected["authority"]["identity"])
        q.fsync_directory(lock.parent)
        result = {"status": "KNOWN_SETTLED_OWNER_RECOVERED", "executed": True,
                  "archive": str(archive), "expected_fingerprint": expected["fingerprint"],
                  "authority_identity": expected["authority"]["identity"], "evidence": proofs,
                  "operator": operator, "reference": reference}
        _write_private(archive / "completed.json", _canonical(result) + b"\n")
        q.fsync_directory(archive)
        return result
    except Exception as exc:
        if neutral_started:
            raise RecoveryAmbiguous("neutral publication began; inspect exact archive/authority, no automatic retry") from exc
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        cp.release_quarantine_recovery_sync(synchronizer)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-state", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--operator", choices=("cayman", "stace"))
    parser.add_argument("--reference")
    parser.add_argument("--confirm")
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps(inspect_state(), sort_keys=True))
        return
    if not all((args.expected_state, args.evidence, args.operator, args.reference, args.confirm)):
        parser.error("execute requires exact state, evidence, operator, reference and confirmation")
    # Read only safe singleton private metadata; never follow an input symlink.
    with os.fdopen(_open_private(args.expected_state), encoding="utf-8") as handle:
        expected = json.load(handle)
    with os.fdopen(_open_private(args.evidence), encoding="utf-8") as handle:
        evidence = json.load(handle)
    print(json.dumps(recover(expected, evidence=evidence, operator=args.operator,
        reference=args.reference, confirmation=args.confirm, execute=True), sort_keys=True))


if __name__ == "__main__":
    main()
