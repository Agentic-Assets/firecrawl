"""Validate a prospective Avison standalone spool receipt.

Read-only evidence only: a caller must separately authenticate the canonical
manifest, code, target, owner lock, process-group settlement and write fence.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cre_colliers_partial import _time
from cre_partial_page_custody import expected_path
from source_evidence.automatic import _read_private

_DENIED_OR_UNSETTLED = {73, 74, 87, 88}


def verify_receipt(run_dir, attempt, *, now=None):
    """Return private receipt metadata or None without authorizing a write."""
    root = Path(run_dir)
    number = attempt.get("number") if isinstance(attempt, dict) else None
    rc = attempt.get("rc") if isinstance(attempt, dict) else None
    if (type(number) is not int or number < 1 or type(rc) is not int
            or rc == 0 or rc in _DENIED_OR_UNSETTLED or not attempt.get("finished_at")):
        return None
    spool = expected_path(root, number, "avison-young")
    state = attempt.get("avison-young_partial")
    if not isinstance(state, dict) or state.get("path") != str(spool.relative_to(root)):
        return None
    receipt_path = spool.parent / f"attempt-{number}.receipt.json"
    try:
        directory = spool.parent.lstat()
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
                or stat.S_IMODE(directory.st_mode) != 0o700):
            return None
        receipt_info = receipt_path.lstat()
        if receipt_info.st_size > 16384:
            return None
        receipt_raw = _read_private(receipt_path, receipt_info.st_size)
        if len(receipt_raw) != receipt_info.st_size:
            return None
        receipt = json.loads(receipt_raw)
        if not isinstance(receipt, dict) or set(receipt) != {
            "schema", "sourceKey", "generation", "attempt", "sourceOutcome", "coverage",
            "sourceStartedAt", "sourceFinishedAt", "spool",
        }:
            return None
        if (receipt["schema"] != "avison-young-settled-spool/v1"
                or receipt["sourceKey"] != "avison-young" or receipt["generation"] != root.name
                or type(receipt["attempt"]) is not int or receipt["attempt"] != number
                or receipt["sourceOutcome"] not in {"record_local_error", "unclassified_source_error",
                                                    "terminal_detail_not_found", "mixed_source_errors"}
                or (rc in {78, 82} and receipt["sourceOutcome"] not in
                    {"terminal_detail_not_found", "mixed_source_errors"})
                or receipt["coverage"] != "unverified"):
            return None
        current = (now or datetime.now(UTC)).astimezone(UTC)
        started, finished = _time(receipt["sourceStartedAt"]), _time(receipt["sourceFinishedAt"])
        if (started < _time(attempt["started_at"]) or finished < started
                or finished - _time(attempt["finished_at"]) > timedelta(minutes=5)
                or finished > current + timedelta(minutes=5)):
            return None
        declared = receipt["spool"]
        if (not isinstance(declared, dict)
                or set(declared) != {"state", "bytes", "observationRecords", "sha256"}
                or declared["state"] not in {"sealed", "sealed_partial"}
                or type(declared["bytes"]) is not int or declared["bytes"] < 1
                or type(declared["observationRecords"]) is not int or declared["observationRecords"] < 1
                or not isinstance(declared["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", declared["sha256"])):
            return None
        fd = os.open(spool, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600):
                return None
            digest = hashlib.sha256()
            size = lines = 0
            header, last = b"", b""
            while chunk := os.read(fd, 1024 * 1024):
                if b"\n" not in header and len(header) < 65536:
                    header += chunk[:65536 - len(header)]
                digest.update(chunk)
                size += len(chunk)
                lines += chunk.count(b"\n")
                last = chunk[-1:]
            after, named = os.fstat(fd), spool.lstat()
            identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns,
                                     item.st_ctime_ns, item.st_mode, item.st_uid, item.st_nlink)
            if (identity(before) != identity(after) or identity(after) != identity(named)
                    or size != declared["bytes"] or lines != declared["observationRecords"] + 1
                    or last != b"\n" or digest.hexdigest() != declared["sha256"]
                    or json.loads(header.split(b"\n", 1)[0]) != {
                        "schema": "avison-young-partial-pages/v1", "sourceKey": "avison-young",
                        "generation": root.name, "attempt": number}):
                return None
        finally:
            os.close(fd)
        after_dir = spool.parent.lstat()
        if ((directory.st_dev, directory.st_ino, directory.st_mode, directory.st_uid)
                != (after_dir.st_dev, after_dir.st_ino, after_dir.st_mode, after_dir.st_uid)):
            return None
        return {"state": "sealed_standalone", "coverage": "unverified",
                "source_outcome": receipt["sourceOutcome"], "path": str(spool.relative_to(root)),
                "sha256": declared["sha256"], "bytes": size,
                "observationRecords": declared["observationRecords"],
                "receipt_path": str(receipt_path.relative_to(root)),
                "receipt_sha256": hashlib.sha256(receipt_raw).hexdigest(),
                "source_started_at": receipt["sourceStartedAt"],
                "source_finished_at": receipt["sourceFinishedAt"]}
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None
