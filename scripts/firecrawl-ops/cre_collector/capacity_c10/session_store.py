"""FD-safe durable C10 arm claims for the library-only coordinator.

The runtime lock serializes execution.  This private ledger additionally makes
an arm claim survive a process crash or quarantine recovery, so a recovered
operator cannot accidentally replay an arm from a stale in-memory session.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

from .contracts import (
    C10Error,
    claim_next_arm,
    new_session,
    require_sha256,
    sha256,
    validate_plan,
    validate_session,
)

SCHEMA_VERSION = 1
STATE_KIND = "cre_capacity_c10_v1_durable_session"
ROOT_MODE = 0o700
FILE_MODE = 0o600
MAX_STATE_BYTES = 1024 * 1024


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _canonical(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError) as exc:
        raise C10Error("C10 durable session is not canonical JSON") from exc


class DurableArmSessionStore:
    """Own one private session file through a retained, no-follow root fd."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute() or not path.name or path.name.startswith("."):
            raise C10Error("C10 durable session path must be an absolute named file")
        self.path = path
        self.root = path.parent
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=ROOT_MODE)
            named = self.root.lstat()
            if (
                not stat.S_ISDIR(named.st_mode)
                or named.st_uid != os.geteuid()
                or stat.S_IMODE(named.st_mode) != ROOT_MODE
            ):
                raise C10Error(
                    "C10 durable session root must be an owner-only directory"
                )
            self._root_fd = os.open(
                self.root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
            )
            opened = os.fstat(self._root_fd)
            if _identity(named) != _identity(opened):
                raise C10Error("C10 durable session root changed while opening")
            self._root_identity = _identity(opened)
        except OSError as exc:
            raise C10Error("C10 durable session root is unavailable") from exc

    def close(self) -> None:
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def _assert_root_identity(self) -> None:
        if self._root_fd < 0:
            raise C10Error("C10 durable session store is closed")
        try:
            named = self.root.lstat()
            opened = os.fstat(self._root_fd)
        except OSError as exc:
            raise C10Error("C10 durable session root is unavailable") from exc
        if (
            not stat.S_ISDIR(named.st_mode)
            or named.st_uid != os.geteuid()
            or stat.S_IMODE(named.st_mode) != ROOT_MODE
            or _identity(named) != self._root_identity
            or _identity(opened) != self._root_identity
        ):
            raise C10Error("C10 durable session root was replaced")

    def _lock(self) -> int:
        self._assert_root_identity()
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            try:
                descriptor = os.open(
                    ".c10-arm-session.lock", flags, dir_fd=self._root_fd
                )
            except FileNotFoundError:
                try:
                    descriptor = os.open(
                        ".c10-arm-session.lock",
                        flags | os.O_CREAT | os.O_EXCL,
                        FILE_MODE,
                        dir_fd=self._root_fd,
                    )
                except FileExistsError:
                    descriptor = os.open(
                        ".c10-arm-session.lock", flags, dir_fd=self._root_fd
                    )
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) != FILE_MODE
                or opened.st_nlink != 1
            ):
                raise C10Error("C10 durable session lock is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self._assert_root_identity()
            return descriptor
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise C10Error("C10 durable session lock is unavailable") from exc
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            raise

    @staticmethod
    def _unlock(descriptor: int) -> None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _read_state(self) -> dict[str, Any] | None:
        try:
            descriptor = os.open(
                self.path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise C10Error("C10 durable session file is unavailable") from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) != FILE_MODE
                or opened.st_nlink != 1
                or opened.st_size < 1
                or opened.st_size > MAX_STATE_BYTES
            ):
                raise C10Error("C10 durable session file is unsafe")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, MAX_STATE_BYTES):
                chunks.append(chunk)
                if sum(map(len, chunks)) > MAX_STATE_BYTES:
                    raise C10Error("C10 durable session file is too large")
            value = json.loads(b"".join(chunks))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise C10Error("C10 durable session file is invalid") from exc
        finally:
            os.close(descriptor)
        if not isinstance(value, dict):
            raise C10Error("C10 durable session file is invalid")
        return value

    def _write_state(self, state: Mapping[str, Any]) -> None:
        encoded = _canonical(state) + b"\n"
        if len(encoded) > MAX_STATE_BYTES:
            raise C10Error("C10 durable session file would exceed its size limit")
        temporary = f".{self.path.name}.tmp-{uuid.uuid4().hex}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(temporary, flags, FILE_MODE, dir_fd=self._root_fd)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) != FILE_MODE
                or opened.st_nlink != 1
            ):
                raise C10Error("C10 durable session temporary file is unsafe")
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise C10Error("C10 durable session write was short")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            self._assert_root_identity()
            os.rename(
                temporary,
                self.path.name,
                src_dir_fd=self._root_fd,
                dst_dir_fd=self._root_fd,
            )
            os.fsync(self._root_fd)
            self._assert_root_identity()
        except OSError as exc:
            raise C10Error("C10 durable session write failed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=self._root_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    @staticmethod
    def _validate_state(plan: Mapping[str, Any], state: Mapping[str, Any]) -> None:
        required = {"schema_version", "kind", "plan_sha256", "session", "arms"}
        if (
            set(state) != required
            or state.get("schema_version") != SCHEMA_VERSION
            or state.get("kind") != STATE_KIND
            or state.get("plan_sha256") != plan["plan_sha256"]
            or not isinstance(state.get("session"), Mapping)
            or not isinstance(state.get("arms"), list)
        ):
            raise C10Error("C10 durable session is not bound to this immutable plan")
        session = state["session"]
        validate_session(plan, session)
        arms = state["arms"]
        if len(arms) != len(session["consumed_arm_indexes"]):
            raise C10Error("C10 durable session arm records are incomplete")
        for index, arm in enumerate(arms):
            if not isinstance(arm, Mapping) or arm.get("index") != index:
                raise C10Error("C10 durable session arm record is malformed")
            state_name = arm.get("state")
            if state_name == "claimed" and set(arm) == {"index", "state"}:
                continue
            if state_name == "terminal" and set(arm) == {
                "index",
                "state",
                "result",
                "result_sha256",
            }:
                result = arm.get("result")
                result_sha256 = require_sha256(
                    arm.get("result_sha256"), "C10 terminal result"
                )
                if not isinstance(result, Mapping) or sha256(result) != result_sha256:
                    raise C10Error("C10 terminal result evidence is invalid")
                continue
            raise C10Error("C10 durable session arm record is malformed")

    def _state_for(
        self, plan: Mapping[str, Any], supplied: Mapping[str, Any]
    ) -> dict[str, Any]:
        validate_plan(plan)
        validate_session(plan, supplied)
        stored = self._read_state()
        if stored is None:
            if dict(supplied) != new_session(plan):
                raise C10Error(
                    "C10 new durable session must start from the empty arm ledger"
                )
            return {
                "schema_version": SCHEMA_VERSION,
                "kind": STATE_KIND,
                "plan_sha256": plan["plan_sha256"],
                "session": dict(supplied),
                "arms": [],
            }
        self._validate_state(plan, stored)
        if stored["session"] != dict(supplied):
            raise C10Error("C10 durable session disagrees with the caller ledger")
        return stored

    def claim(
        self, plan: Mapping[str, Any], supplied: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Persist one claim before preflight; unresolved claims permanently stop the plan."""
        descriptor = self._lock()
        try:
            state = self._state_for(plan, supplied)
            if any(arm["state"] == "claimed" for arm in state["arms"]):
                raise C10Error("C10 durable session has an unresolved claimed arm")
            claimed = claim_next_arm(plan, state["session"])
            state["session"] = claimed["session"]
            state["arms"] = [
                *state["arms"],
                {"index": claimed["arm"]["index"], "state": "claimed"},
            ]
            self._write_state(state)
            return claimed
        finally:
            self._unlock(descriptor)

    def mark_terminal(
        self, plan: Mapping[str, Any], arm: Mapping[str, Any], result: Mapping[str, Any]
    ) -> None:
        """Atomically commit a completed arm after all settlement and rollback checks."""
        descriptor = self._lock()
        try:
            state = self._read_state()
            if state is None:
                raise C10Error("C10 durable session claim is missing")
            self._validate_state(plan, state)
            index = arm.get("index")
            if (
                type(index) is not int
                or not state["arms"]
                or state["arms"][-1] != {"index": index, "state": "claimed"}
            ):
                raise C10Error(
                    "C10 durable session cannot terminalize an unclaimed arm"
                )
            canonical_result = json.loads(_canonical(result))
            state["arms"][-1] = {
                "index": index,
                "state": "terminal",
                "result": canonical_result,
                "result_sha256": sha256(canonical_result),
            }
            self._write_state(state)
        finally:
            self._unlock(descriptor)

    def session(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        """Read the current durable ledger for an operator-visible recovery decision."""
        descriptor = self._lock()
        try:
            state = self._read_state()
            if state is None:
                raise C10Error("C10 durable session claim is missing")
            self._validate_state(plan, state)
            return dict(state["session"])
        finally:
            self._unlock(descriptor)

    def terminal_results(self, plan: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Return recoverable, hash-validated terminal evidence in arm order."""
        descriptor = self._lock()
        try:
            state = self._read_state()
            if state is None:
                raise C10Error("C10 durable session claim is missing")
            self._validate_state(plan, state)
            return [
                json.loads(_canonical(arm["result"]))
                for arm in state["arms"]
                if arm["state"] == "terminal"
            ]
        finally:
            self._unlock(descriptor)
