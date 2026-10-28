"""Publish a small named file set into a new private POSIX directory."""
import os
import re
import stat
from pathlib import Path


def write(outdir, files):
    """Create exclusively; on failure remove only files in our opened directory."""
    if (not isinstance(files, (list, tuple)) or not files or len(files) > 16
            or any(not isinstance(entry, (list, tuple)) or len(entry) != 2
                   or not isinstance(entry[0], str) or len(entry[0]) > 255
                   or not re.fullmatch(r"[a-z][a-z0-9._-]*", entry[0])
                   or not isinstance(entry[1], bytes) for entry in files)
            or len({entry[0] for entry in files}) != len(files)):
        raise ValueError("invalid private export file set")
    if not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "getuid")):
        raise RuntimeError("private export requires POSIX filesystem support")
    path = Path(outdir)
    if not path.name or path.name in (".", ".."):
        raise ValueError("invalid export directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(path.parent, flags)
    directory = None
    created = False
    published = []

    def same_directory():
        if directory is None:
            return False
        try:
            named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(directory)
            return (stat.S_ISDIR(named.st_mode) and
                    (named.st_dev, named.st_ino, named.st_uid, named.st_mode) ==
                    (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_mode))
        except OSError:
            return False

    try:
        os.mkdir(path.name, 0o700, dir_fd=parent)
        created = True
        directory = os.open(path.name, flags, dir_fd=parent)
        info = os.fstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("export directory is not private")
        for name, payload in files:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            published.append(name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        os.fsync(directory)
        if not same_directory():
            raise ValueError("export directory changed during publication")
        os.fsync(parent)
    except BaseException:
        if directory is not None:
            for name in published:
                try:
                    os.unlink(name, dir_fd=directory)
                except OSError:
                    pass
        if created and same_directory():
            try:
                os.rmdir(path.name, dir_fd=parent)
            except OSError:
                pass
        raise
    finally:
        if directory is not None:
            os.close(directory)
        os.close(parent)
