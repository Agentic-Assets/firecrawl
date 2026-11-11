"""Read-only, conservative snapshot of the persistent CRE lock authority."""

import os
import stat
import sys
from pathlib import Path

from cre_checkpoint_refresh import LockHeldError, SharedLock


def observe(path: Path) -> str:
    """Classify one stable read without creating or locking any runtime file."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        return "unsafe"
    try:
        fd = os.open(path, os.O_RDONLY | nofollow | os.O_NONBLOCK)
    except FileNotFoundError:
        return "unsafe" if os.path.lexists(path) else "absent"
    except OSError:
        return "unsafe"
    try:
        before = os.fstat(fd)
        named = os.lstat(path)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)):
            return "unsafe"
        raw = SharedLock._safe_authority_bytes(fd)
        reclaim = SharedLock._authority_reclaim_state_bytes(raw)
        fields = None if reclaim is not None else SharedLock._authority_fields_bytes(raw)
        after = os.fstat(fd)
        named_after = os.lstat(path)
        if (raw != SharedLock._safe_authority_bytes(fd)
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or (after.st_dev, after.st_ino) != (named_after.st_dev, named_after.st_ino)):
            return "unsafe"
        if reclaim is not None:
            return "reclaiming"
        if fields is None:
            return "unsafe"
        pid, _token, _generation, armed = fields
        if pid == 0:
            return "neutral"
        try:
            os.kill(pid, 0)
            owner = "visible"
        except ProcessLookupError:
            owner = "not-visible"
        except OSError:
            owner = "unknown"
        return ("armed-" if armed else "normal-") + owner
    except (OSError, LockHeldError, ValueError):
        return "unsafe"
    finally:
        os.close(fd)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(2)
    print(observe(Path(sys.argv[1])))
