"""Private local due intents with a single, non-replaying dispatcher owner."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

from cre_stop_guarded_tier import _read_at, _safe_stat


class DispatchRefused(ValueError):
    """An intent or its prior execution cannot safely be advanced."""


class LocalPreflightRefused(ValueError):
    """This source's request was refused before any native process started."""


def _directory(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    _safe_stat(fd, directory=True, private=True)
    return fd


def _slot(slot: str) -> str:
    if (not isinstance(slot, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,112}(?:\.continuation[1-9][0-9]*)?", slot) is None):
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
            or value["state"] not in {"queued", "dispatch_uncertain", "settled", "held_no_work"}):
        raise DispatchRefused("invalid durable intent")
    if "policy" in value["request"] and value["request"].get("slot") != value["slot"]:
        raise DispatchRefused("durable intent slot differs")
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


def hold_invalid_policy(path: Path, slot: str, policy_sha256: str | None,
                        *, reason: str = "invalid_source_policy") -> None:
    """Keep a no-work due slot without persisting raw policy bytes."""
    if (reason not in {"invalid_source_policy", "source_policy_unreadable", "shared_preflight_unavailable"}
            or (policy_sha256 is None and reason != "source_policy_unreadable")
            or (policy_sha256 is not None and re.fullmatch(r"[a-f0-9]{64}", policy_sha256) is None)):
        raise DispatchRefused("invalid rejected policy hash")
    name = _slot(slot)
    directory = _directory(path)
    try:
        request = {"slot": slot, "held_policy_sha256": policy_sha256}
        record = {"schema": "cre-local-dispatch/v1", "slot": slot, "request": request,
                  "state": "held_no_work", "result": {"outcome": "held_no_work", "reason": reason}}
        try:
            _write(directory, name, record, create=True)
        except FileExistsError:
            old = _read(directory, name)
            if _encode(old["request"]) != _encode(request):
                raise DispatchRefused("due slot already has a different request")
    finally:
        os.close(directory)


def record_unknown_history(path: Path, task: str, policy_sha256: str, observed_unix_ms: int) -> int:
    """Durably note an unverified historical policy interval without replaying it."""
    if (not isinstance(task, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,72}", task) is None
            or not isinstance(policy_sha256, str) or re.fullmatch(r"[a-f0-9]{64}", policy_sha256) is None
            or type(observed_unix_ms) is not int or observed_unix_ms < 0):
        raise DispatchRefused("invalid historical policy observation")
    name = ".history-" + hashlib.sha256((task + ":" + policy_sha256).encode()).hexdigest() + ".audit"
    directory = _directory(path)
    try:
        value = {"schema": "cre-dispatch-history-unknown/v1", "task": task,
                 "policy_sha256": policy_sha256, "observed_unix_ms": observed_unix_ms,
                 "historical_intervals": "unknown_not_replayed"}
        first_observed = observed_unix_ms
        try:
            _write(directory, name, value, create=True)
        except FileExistsError:
            raw, _ = _read_at(directory, name, limit=4096)
            old = json.loads(raw)
            if (not isinstance(old, dict) or old.get("schema") != value["schema"]
                    or old.get("task") != task or old.get("policy_sha256") != policy_sha256
                    or old.get("historical_intervals") != value["historical_intervals"]
                    or type(old.get("observed_unix_ms")) is not int or old["observed_unix_ms"] < 0):
                raise DispatchRefused("historical policy observation differs")
            first_observed = old["observed_unix_ms"]
        return first_observed
    finally:
        os.close(directory)


def drain(path: Path, *, launch, qualify, successor=None, prepare=None, wait=True) -> bool:
    """Run queued intents under one lock; uncertain attempts are never replayed.

    ``launch`` must invoke the real foreground launcher using the request's
    preallocated locator. ``qualify`` must independently verify all settlement
    and write fences; an exception preserves the uncertain intent and stops.
    Submissions wait for a busy owner after enqueue. A periodic wake can instead
    return False when busy; a subsequent wake must retry any pending intents.
    """
    directory = _directory(path)
    lock = os.open(".owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        _safe_stat(lock, private=True)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            return False
        while True:
            names = sorted(name for name in os.listdir(directory) if name.endswith(".json"))
            records = [(name, _read(directory, name)) for name in names]
            records = [item for item in records if item[1]["state"] not in {"settled", "held_no_work"}]
            for _, item in records:
                due = item["request"].get("due_unix_ms")
                if "policy" in item["request"] and (type(due) is not int or due < 0):
                    raise DispatchRefused("dispatch due time is invalid")
                eligible = item["request"].get("eligible_unix_ms", due)
                if "policy" in item["request"] and (type(eligible) is not int or eligible < due):
                    raise DispatchRefused("dispatch eligibility is invalid")
            now_ms = int(time.time() * 1000)
            records = [item for item in records if item[1]["state"] == "dispatch_uncertain"
                       or item[1]["request"].get("eligible_unix_ms", item[1]["request"].get("due_unix_ms", 0)) <= now_ms]
            if not records:
                break
            # Uncertain writes fence everything; fresh due work precedes continuations.
            records.sort(key=lambda item: (item[1]["state"] != "dispatch_uncertain",
                bool(item[1]["request"].get("continuation_depth", 0)), item[0]))
            name, record = records[0]
            if record["state"] == "settled":
                continue
            if record["state"] == "queued":
                prepared = None
                if prepare is not None:
                    try:
                        prepared = prepare(record["request"])
                    except LocalPreflightRefused as exc:
                        record.update(state="held_no_work", result={"outcome": "held_no_work",
                            "reason": "source_preflight_refused", "error_type": type(exc).__name__})
                        _write(directory, name, record, create=False)
                        continue
                    except Exception as exc:
                        # Nothing capable of launching the native process has
                        # run. Preserve this intent for a later safe retry.
                        record["result"] = {"outcome": "pre_spawn_refused", "error_type": type(exc).__name__}
                        _write(directory, name, record, create=False)
                        raise
                record["state"] = "dispatch_uncertain"
                record["result"] = None
                _write(directory, name, record, create=False)
                launch_result = (launch(record["request"], prepared) if prepare is not None
                                 else launch(record["request"]))
            else:
                launch_result = None
            result = (launch_result if isinstance(launch_result, dict)
                      and launch_result.get("outcome") == "settled_no_work"
                      else qualify(record["request"]))
            if not isinstance(result, dict) or result.get("outcome") not in {"qualified", "settled_no_work"}:
                raise DispatchRefused("dispatch settlement was not qualified")
            if successor is not None:
                next_intents = successor(record["slot"], record["request"], result)
                if isinstance(next_intents, tuple):
                    next_intents = [next_intents]
                for next_intent in next_intents or []:
                    enqueue(path, *next_intent)
            record.update(state="settled", result=result)
            _write(directory, name, record, create=False)
            if result.get("scope") == "shared":
                raise DispatchRefused("shared native preflight refused; due peers remain queued")
        return True
    finally:
        os.close(lock)
        os.close(directory)
