"""Single outstanding targeted evidence receipt, serialized by the CRE owner lock.

This is a recovery fence, not a queue. Callers must already hold SharedLock.
"""
import json
import os
import stat
from pathlib import Path

from source_evidence import automatic, events

NAME = ".cre-targeted-evidence-recovery.json"
LIMIT = 16384


def path(lock_path):
    return Path(lock_path).parent / NAME


def _sync(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read(lock_path):
    marker = path(lock_path)
    try:
        raw = automatic._read_private(marker, LIMIT)
    except FileNotFoundError:
        return None
    if len(raw) > LIMIT:
        raise ValueError("targeted evidence recovery fence is oversized")
    obj = json.loads(raw.decode(), object_pairs_hook=automatic.unique_object,
                     parse_constant=automatic.invalid_number)
    if (not isinstance(obj, dict) or set(obj) != {"schema", "receipt", "plan_sha256", "source", "policy_sha256", "target_sha256"}
            or obj["schema"] != "cre-targeted-evidence-fence/v1"
            or not isinstance(obj["receipt"], str) or not Path(obj["receipt"]).is_absolute()):
        raise ValueError("invalid targeted evidence recovery fence")
    return obj


def require_clear(lock_path):
    if read(lock_path) is not None:
        raise ValueError("targeted evidence requires exact receipt reconciliation")


def publish(lock_path, binding):
    if path(lock_path).exists():
        raise ValueError("targeted evidence recovery fence exists")
    raw = events.encoded(binding) + b"\n"
    if len(raw) > LIMIT:
        raise ValueError("targeted evidence recovery fence is oversized")
    fd = os.open(path(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(raw)
        while view:
            n = os.write(fd, view)
            if n <= 0:
                raise OSError("short fence write")
            view = view[n:]
        os.fsync(fd)
    finally:
        os.close(fd)
    _sync(path(lock_path).parent)
    if read(lock_path) != binding:
        raise ValueError("targeted evidence fence publication changed")


def clear(lock_path, binding):
    marker = path(lock_path)
    if read(lock_path) != binding:
        raise ValueError("targeted evidence recovery fence binding changed")
    before = marker.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("unsafe targeted evidence recovery fence")
    if read(lock_path) != binding or marker.lstat() != before:
        raise ValueError("targeted evidence recovery fence changed")
    os.unlink(marker)
    _sync(marker.parent)
