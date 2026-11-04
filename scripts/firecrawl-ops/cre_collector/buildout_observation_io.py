"""Bounded file reads with change detection; not source authentication.

Only the final path component is protected against symlinks. Parent directories
remain caller-controlled, and hostile filesystems or privileged writers may be
able to conceal changes. Callers must keep returned bytes, not reopen the path.
"""
import os
import stat

READ_CHUNK_BYTES = 65_536


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def read_consistent(path, limit):
    """Read at most limit+1 bytes from one unchanged regular-file descriptor."""
    if type(limit) is not int or limit < 0:
        raise ValueError("invalid read limit")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        raise RuntimeError("safe no-follow, nonblocking file opening is unavailable")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("input must be a regular file")
        chunks = []
        remaining = limit + 1
        while remaining:
            part = os.read(descriptor, min(remaining, READ_CHUNK_BYTES))
            if not part:
                break
            chunks.append(part)
            remaining -= len(part)
        after = os.fstat(descriptor)
        try:
            current = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise ValueError("input changed during read") from exc
        if _identity(before) != _identity(after) or _identity(after) != _identity(current):
            raise ValueError("input changed during read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)
