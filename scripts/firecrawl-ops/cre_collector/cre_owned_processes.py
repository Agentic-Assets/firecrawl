"""Bounded settlement of process groups created by this foreground owner.

Only use from_spawn immediately after Popen(start_new_session=True). The
direct leader must remain unreaped through the last signal, reserving its PID
and process-group ID. An already-reaped leader with a live group is ambiguous
and is never signalled. Once disappearance is observed it is never signalled again. Descendants that create a new session
are outside this boundary; local settlement says nothing about remote work.
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


@dataclass
class OwnedProcessGroup:
    process: Any
    pgid: int
    gone: bool = False
    reaped: bool = False

    @classmethod
    def from_spawn(cls, process: Any) -> OwnedProcessGroup:
        pgid = process.pid
        if type(pgid) is not int or pgid <= 1 or pgid == os.getpgrp():
            raise ValueError("ambiguous owned process-group identity")
        return cls(process, pgid)


@dataclass(frozen=True)
class Settlement:
    settled: bool
    errors: tuple[str, ...]
    groups: tuple[dict[str, Any], ...]


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin can report EPERM for an existing group containing zombies.
        # This proves presence only, never ownership or successful cleanup.
        return True
    return True


def _leader_owned(group: OwnedProcessGroup) -> bool:
    # The new-session Popen contract fixes PGID=PID. This object is the sole
    # reaper: returncode=None means its PID remains reserved, even if the OS
    # process has exited. Darwin may reject getpgid/getsid for such a zombie,
    # so those calls cannot replace the unreaped-child ownership witness.
    return group.process.returncode is None


@contextmanager
def _defer_cleanup_signals():
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, lambda _s, _f: None)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def settle_owned_groups(
    groups: list[OwnedProcessGroup],
    *,
    grace_seconds: float,
    initial_signal: int = signal.SIGINT,
    kill_seconds: float = 5.0,
) -> Settlement:
    """Reserve leader IDs through all signals; bound every final reap.

    Main-thread handlers defer additional operator signals during this bounded
    cleanup. Resource watchdogs still record their sticky trip state. Native
    calls are assumed to return; this does not claim a native syscall deadline.
    """
    if not all(math.isfinite(v) and v >= 0 for v in (grace_seconds, kill_seconds)):
        raise ValueError("cleanup deadlines must be finite and nonnegative")
    errors: list[str] = []
    start = time.monotonic()
    graceful_end = start + grace_seconds
    deadline = graceful_end + kill_seconds

    def probe(group: OwnedProcessGroup) -> None:
        if not group.gone:
            try:
                group.gone = not _group_alive(group.pgid)
            except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
                errors.append(f"group_probe_failed:{type(exc).__name__}")

    def send(group: OwnedProcessGroup, signum: int) -> None:
        if not group.gone:
            try:
                os.killpg(group.pgid, signum)
            except ProcessLookupError:
                group.gone = True
            except PermissionError:
                errors.append("group_signal_denied")
            except (Exception, KeyboardInterrupt):  # noqa: BLE001
                errors.append("group_signal_failed")

    def reap(group: OwnedProcessGroup) -> None:
        if not group.reaped:
            try:
                group.process.wait(timeout=0)
                group.reaped = True
            except subprocess.TimeoutExpired:
                pass
            except (Exception, KeyboardInterrupt):  # noqa: BLE001
                errors.append("leader_reap_failed")

    def pause(until: float) -> bool:
        try:
            time.sleep(min(0.05, max(0, until - time.monotonic())))
            return True
        except (Exception, KeyboardInterrupt):  # noqa: BLE001
            errors.append("cleanup_wait_interrupted")
            return False

    with _defer_cleanup_signals():
        valid = []
        admissible = []
        for group in groups:
            if group.pgid != group.process.pid or group.pgid == os.getpgrp():
                errors.append("group_identity_changed")
                continue
            probe(group)
            if group.gone:
                admissible.append(group)
                continue
            try:
                if not _leader_owned(group):
                    errors.append("group_ownership_unknown")
                    continue
            except (Exception, KeyboardInterrupt):  # noqa: BLE001
                errors.append("group_ownership_unknown")
                continue
            valid.append(group)
            admissible.append(group)
            send(group, initial_signal)
        # Do not wait/poll here: preserving the unreaped leader reserves the
        # numeric group ID, including after its OS exit, until the last signal.
        while valid and time.monotonic() < graceful_end:
            for group in valid:
                probe(group)
            if all(g.gone for g in valid) or not pause(graceful_end):
                break
        for group in valid:
            send(group, signal.SIGKILL)
        # No signals after this point, so reuse after reaping cannot harm a
        # different group. A live group at the deadline remains unknown.
        while True:
            for group in admissible:
                reap(group)
                probe(group)
            if (
                all(g.gone and g.reaped for g in admissible)
                or time.monotonic() >= deadline
            ):
                break
            if not pause(deadline):
                break
    details = tuple(
        {"pgid": g.pgid, "leader_reaped": g.reaped, "group_gone": g.gone}
        for g in groups
    )
    return Settlement(
        not any(error != "group_signal_denied" for error in errors)
        and all(g.gone and g.reaped for g in groups),
        tuple(dict.fromkeys(errors)),
        details,
    )
