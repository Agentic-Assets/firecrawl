"""Single outstanding targeted evidence receipt, serialized by the CRE owner lock.

This is a recovery fence, not a queue. Callers must already hold SharedLock.
"""
import json
import os
import stat
import tempfile
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
    """Atomically publish complete bytes while the caller owns the CRE lock.

    Cooperating publishers are serialized by SharedLock. The private temporary
    file is fsynced before rename, and the final name must be absent immediately
    before installation. A crash before rename leaves only an inert temp file.
    """
    marker = path(lock_path)
    raw = events.encoded(binding) + b"\n"
    if len(raw) > LIMIT:
        raise ValueError("targeted evidence recovery fence is oversized")
    try:
        marker.lstat()
    except FileNotFoundError:
        pass
    else:
        raise ValueError("targeted evidence recovery fence exists")
    fd, temp_name = tempfile.mkstemp(prefix=NAME + ".", suffix=".tmp", dir=marker.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(raw)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short fence write")
            view = view[count:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        try:
            marker.lstat()
        except FileNotFoundError:
            pass
        else:
            raise ValueError("targeted evidence recovery fence exists")
        os.rename(temp, marker)
        _sync(marker.parent)
        if read(lock_path) != binding:
            raise ValueError("targeted evidence fence publication changed")
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


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
