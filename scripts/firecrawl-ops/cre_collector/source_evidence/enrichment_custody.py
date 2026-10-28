"""Private custody markers for one already-owned enrichment worker.

This records local files only; it does not admit evidence or authorize writes.
The caller must establish worker settlement before calling settle.
"""

import os
import re
import stat
from pathlib import Path

from source_evidence import doc_fetch_process, events

SCHEMA = "cre-enrichment-custody/v1"
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_CLAIMS_BYTES = 16 * 1024 * 1024


def _directory(root):
    path = Path(root)
    if not path.is_absolute():
        raise ValueError("enrichment custody requires an absolute directory")
    fd = doc_fetch_process._private_directory(path)
    return path, fd


def preflight(root):
    """Require an existing private parent and two unused output names."""
    path, fd = _directory(root)
    try:
        for name in ("enriched.json", "claims.json"):
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ValueError("enrichment custody destination already exists")
    finally:
        os.close(fd)
    return path / "claims.json", path / "enriched.json"


def mark(root, source):
    """Create a marker before dispatch; the caller persists it in its receipt."""
    if not isinstance(source, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", source):
        raise ValueError("invalid enrichment source")
    preflight(root)
    return {"schema_version": SCHEMA, "source_key": source, "state": "awaiting_worker",
            "artifact_path": "enriched.json", "claims_path": "claims.json",
            "database_retention": "not_performed"}


def settle(root, marker):
    """Return metadata for private files after the owner confirms settlement."""
    if not isinstance(marker, dict):
        raise ValueError("invalid enrichment custody marker")
    expected = {"schema_version": SCHEMA, "source_key": marker.get("source_key"),
                "state": "awaiting_worker", "artifact_path": "enriched.json",
                "claims_path": "claims.json", "database_retention": "not_performed"}
    if marker != expected or not isinstance(marker.get("source_key"), str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9_-]{0,127}", marker["source_key"]):
        raise ValueError("invalid enrichment custody marker")
    path, fd = _directory(root)
    try:
        directory_before = os.fstat(fd)
        result = dict(marker)
        absent = []
        for field, name, limit in (("artifact", "enriched.json", MAX_ARTIFACT_BYTES),
                                   ("claims", "claims.json", MAX_CLAIMS_BYTES)):
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                result[field] = {"state": "absent"}
                absent.append(name)
                continue
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                def identity(info):
                    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink,
                            info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                opened = os.fstat(file_fd)
                if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                        or stat.S_IMODE(opened.st_mode) != 0o600 or opened.st_nlink != 1
                        or identity(opened) != identity(os.stat(name, dir_fd=fd, follow_symlinks=False))):
                    raise ValueError("enrichment custody file changed")
                chunks = []
                size = 0
                while True:
                    chunk = os.read(file_fd, min(65536, limit + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("enrichment custody file exceeds bound")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                os.fsync(file_fd)
                if (identity(opened) != identity(os.fstat(file_fd)) or
                        identity(opened) != identity(os.stat(name, dir_fd=fd, follow_symlinks=False))):
                    raise ValueError("enrichment custody file changed")
                result[field] = {"state": "retained", "sha256": events.sha256(raw), "bytes": len(raw)}
            finally:
                os.close(file_fd)
        for name in absent:
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ValueError("enrichment custody directory changed")
        os.fsync(fd)
        named_directory = os.stat(path, follow_symlinks=False)
        directory_after = os.fstat(fd)
        directory_identity = lambda info: (info.st_dev, info.st_ino, info.st_uid, info.st_mode,
                                          info.st_mtime_ns, info.st_ctime_ns)
        if (directory_identity(directory_before) != directory_identity(directory_after) or
                directory_identity(directory_after) != directory_identity(named_directory)):
            raise ValueError("enrichment custody directory changed")
        result["state"] = "settled"
        return result
    finally:
        os.close(fd)
