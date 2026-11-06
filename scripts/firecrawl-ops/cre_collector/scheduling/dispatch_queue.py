"""Private local due intents with a single, non-replaying dispatcher owner."""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
from pathlib import Path

from cre_stop_guarded_tier import _read_at, _safe_stat


class DispatchRefused(ValueError):
    """An intent or its prior execution cannot safely be advanced."""


def _directory(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    _safe_stat(fd, directory=True, private=True)
    return fd


def _slot(slot: str) -> str:
    if (not isinstance(slot, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,112}(?:\.continuation1)?", slot) is None):
        raise DispatchRefused("invalid due slot")
    return slot + ".json"


def _encode(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _write(directory: int, name: str, value: dict, *, create: bool) -> None:
    data = _encode(value)
    temp = ".pending-" + secrets.token_hex(16)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            os.link(temp, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            os.unlink(temp, dir_fd=directory)
        else:
            os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temp, dir_fd=directory)
        except FileNotFoundError:
            pass


def _read(directory: int, name: str) -> dict:
    raw, _ = _read_at(directory, name, limit=1024 * 1024)
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {"schema", "slot", "request", "state", "result"}
            or value["schema"] != "cre-local-dispatch/v1"
            or _slot(value["slot"]) != name or not isinstance(value["request"], dict)
            or value["state"] not in {"queued", "dispatch_uncertain", "settled"}):
        raise DispatchRefused("invalid durable intent")
    return value


def enqueue(path: Path, slot: str, request: dict) -> None:
    """Publish the exact intent once; a conflicting duplicate is refused."""
    if not isinstance(request, dict):
        raise DispatchRefused("invalid dispatch request")
    name = _slot(slot)
    directory = _directory(path)
    try:
        record = {"schema": "cre-local-dispatch/v1", "slot": slot, "request": request,
                  "state": "queued", "result": None}
        try:
            _write(directory, name, record, create=True)
        except FileExistsError:
            old = _read(directory, name)
            if _encode(old["request"]) != _encode(request):
                raise DispatchRefused("due slot already has a different request")
    finally:
        os.close(directory)


def drain(path: Path, *, launch, qualify, successor=None) -> bool:
    """Run queued intents under one lock; uncertain attempts are never replayed.

    ``launch`` must invoke the real foreground launcher using the request's
    preallocated locator. ``qualify`` must independently verify all settlement
    and write fences; an exception preserves the uncertain intent and stops.
    A busy owner waits after enqueue, so an intent arriving during owner exit
    cannot be stranded by a lost wakeup. This can leave a scheduled task
    instance waiting; it does not reconstruct triggers suppressed by Windows.
    """
    directory = _directory(path)
    lock = os.open(".owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        _safe_stat(lock, private=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        while True:
            names = sorted(name for name in os.listdir(directory) if name.endswith(".json"))
            records = [(name, _read(directory, name)) for name in names]
            records = [item for item in records if item[1]["state"] != "settled"]
            if not records:
                break
            # Uncertain writes fence everything; fresh due work precedes continuations.
            records.sort(key=lambda item: (item[1]["state"] != "dispatch_uncertain",
                bool(item[1]["request"].get("continuation_depth", 0)), item[0]))
            name, record = records[0]
            if record["state"] == "settled":
                continue
            if record["state"] == "queued":
                record["state"] = "dispatch_uncertain"
                _write(directory, name, record, create=False)
                launch_result = launch(record["request"])
            else:
                launch_result = None
            result = (launch_result if isinstance(launch_result, dict)
                      and launch_result.get("outcome") == "settled_no_work"
                      else qualify(record["request"]))
            if not isinstance(result, dict) or result.get("outcome") not in {"qualified", "settled_no_work"}:
                raise DispatchRefused("dispatch settlement was not qualified")
            if successor is not None:
                next_intent = successor(record["slot"], record["request"], result)
                if next_intent is not None:
                    enqueue(path, *next_intent)
            record.update(state="settled", result=result)
            _write(directory, name, record, create=False)
        return True
    finally:
        os.close(lock)
        os.close(directory)
