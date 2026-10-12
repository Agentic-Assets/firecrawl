"""Bounded settlement of process groups created by this foreground owner.

Only use from_spawn immediately after Popen(start_new_session=True). The
retained group ID remains reserved while any member exists. Once disappearance
is observed it is never signalled again. Descendants that create a new session
are outside this boundary; local settlement says nothing about remote work.
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
import time
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
    return True


def settle_owned_groups(
    groups: list[OwnedProcessGroup],
    *,
    grace_seconds: float,
    initial_signal: int = signal.SIGINT,
    kill_seconds: float = 5.0,
) -> Settlement:
    """One monotonic deadline covers all groups, escalation and final reaping."""
    if not all(math.isfinite(v) and v >= 0 for v in (grace_seconds, kill_seconds)):
        raise ValueError("cleanup deadlines must be finite and nonnegative")
    errors: list[str] = []
    start = time.monotonic()
    graceful_end = start + grace_seconds
    deadline = graceful_end + kill_seconds

    def probe(group: OwnedProcessGroup) -> None:
        if group.gone:
            return
        try:
            group.gone = not _group_alive(group.pgid)
        except Exception:  # noqa: BLE001
            errors.append("group_probe_failed")

    def send(group: OwnedProcessGroup, signum: int) -> None:
        if group.gone:
            return
        try:
            os.killpg(group.pgid, signum)
        except ProcessLookupError:
            group.gone = True
        except Exception:  # noqa: BLE001
            errors.append("group_signal_failed")

    def reap(group: OwnedProcessGroup, timeout: float = 0) -> None:
        if group.reaped:
            return
        try:
            group.process.wait(timeout=max(0, timeout))
            group.reaped = True
        except subprocess.TimeoutExpired:
            pass
        except Exception:  # noqa: BLE001
            errors.append("leader_reap_failed")

    for group in groups:
        # Popen's new-session contract supplies identity, not arbitrary PIDs.
        if group.pgid != group.process.pid or group.pgid == os.getpgrp():
            errors.append("group_identity_changed")
            continue
        send(group, initial_signal)
    valid = [g for g in groups if g.pgid == g.process.pid and g.pgid != os.getpgrp()]
    while True:
        for group in valid:
            reap(group)
            probe(group)
        if all(g.gone and g.reaped for g in valid) or time.monotonic() >= graceful_end:
            break
        time.sleep(min(0.05, max(0, graceful_end - time.monotonic())))
    for group in valid:
        send(group, signal.SIGKILL)
    while True:
        for group in valid:
            reap(group)
            probe(group)
        if all(g.gone and g.reaped for g in valid) or time.monotonic() >= deadline:
            break
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    details = tuple(
        {"pgid": g.pgid, "leader_reaped": g.reaped, "group_gone": g.gone}
        for g in groups
    )
    return Settlement(
        not errors and all(g.gone and g.reaped for g in groups),
        tuple(dict.fromkeys(errors)),
        details,
    )
