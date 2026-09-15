"""Governed launchd dispatcher for the CRE collector tiers.

The shell tier worker retains the established logging, marker, alert, and
command semantics.  This module owns its lifetime lock: it takes the same
``SharedLock`` and persistent recovery synchronizer used by checkpoint,
benchmark, and operator recovery.  The worker receives inherited descriptors,
so an unexpected dispatcher-parent exit cannot release the authority while the
worker is still running.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import signal
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from cre_checkpoint_refresh import (
    LOCK_AUTHORITY_SUFFIX,
    LockHeldError,
    SharedLock,
    canonical_shared_lock_dir,
    quarantine_recovery_sync_path,
)

COLLECTOR_DIR = Path(__file__).resolve().parent
RUNNER = COLLECTOR_DIR / "launchd" / "cre_run_tier.sh"
TIERS = frozenset({"monitor", "enrich", "weekly", "daily"})


class TierDispatchError(RuntimeError):
    """The launchd tier cannot safely enter or retain the governed lock."""


def _strict_private_regular(descriptor: int, path: Path, label: str) -> None:
    """Require an inherited descriptor to still name one private regular file."""
    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
    except OSError as exc:
        raise TierDispatchError(f"CRE tier {label} descriptor is unavailable") from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or opened.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) != 0o600
        or not stat.S_ISREG(named.st_mode)
        or named.st_nlink != 1
        or named.st_uid != os.geteuid()
        or stat.S_IMODE(named.st_mode) != 0o600
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise TierDispatchError(f"CRE tier {label} descriptor changed")


def _inherited_descriptor(value: str | None, label: str) -> int:
    try:
        descriptor = int(value or "")
    except ValueError as exc:
        raise TierDispatchError(f"CRE tier {label} descriptor is missing") from exc
    if descriptor < 0:
        raise TierDispatchError(f"CRE tier {label} descriptor is missing")
    return descriptor


def verify_inherited_lock(environ: Mapping[str, str] | None = None) -> None:
    """Prove the shell worker inherited the dispatcher's canonical authority.

    This is intentionally a cooperative-process boundary.  The descriptors are
    never accepted from a public CLI flag; the dispatcher forks while holding
    the canonical authority and recovery-sync flocks, then execs the shell with
    these exact descriptors.  A direct shell invocation lacks those FDs and
    fails before it can run a tier.
    """
    env = os.environ if environ is None else environ
    lock_path = canonical_shared_lock_dir()
    authority_fd = _inherited_descriptor(
        env.get("CRE_TIER_LOCK_AUTHORITY_FD"), "authority"
    )
    sync_fd = _inherited_descriptor(env.get("CRE_TIER_LOCK_SYNC_FD"), "recovery sync")
    _strict_private_regular(
        authority_fd,
        lock_path.with_name(f"{lock_path.name}{LOCK_AUTHORITY_SUFFIX}"),
        "authority",
    )
    _strict_private_regular(
        sync_fd,
        quarantine_recovery_sync_path(lock_path),
        "recovery sync",
    )
    # A pathname/inode match alone is not ownership: another same-UID process
    # could open these files while the real dispatcher holds their flocks.  An
    # inherited descriptor shares the dispatcher's open file description, so
    # this nonblocking reassertion succeeds without changing its lifetime lock.
    # A separately opened descriptor fails while either real owner is active.
    for descriptor, label in (
        (authority_fd, "authority"),
        (sync_fd, "recovery sync"),
    ):
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise TierDispatchError(
                    f"CRE tier {label} descriptor does not own the dispatcher flock"
                ) from exc
            raise TierDispatchError(
                f"CRE tier {label} descriptor flock is unavailable"
            ) from exc
    token = env.get("CRE_TIER_LOCK_TOKEN")
    expected_generation = env.get("CRE_TIER_LOCK_GENERATION")
    owner = env.get("CRE_TIER_LOCK_OWNER_PID")
    fields = SharedLock._authority_fields(authority_fd)
    if fields is None or token is None or expected_generation is None or owner is None:
        raise TierDispatchError("CRE tier inherited authority is incomplete")
    pid, recorded_token, generation, recovery_required = fields
    if (
        recovery_required
        or recorded_token != token
        or generation != expected_generation
        or owner != str(pid)
        or pid <= 0
    ):
        raise TierDispatchError(
            "CRE tier inherited authority does not match dispatcher"
        )
    try:
        lease = (lock_path / "lease").read_text(encoding="utf-8").strip()
        lock_owner = (lock_path / "pid").read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError) as exc:
        raise TierDispatchError("CRE tier inherited lock is incomplete") from exc
    if lease != expected_generation or lock_owner != owner:
        raise TierDispatchError("CRE tier inherited lock ownership changed")


def _default_command(tier: str) -> list[str]:
    return ["/bin/bash", str(RUNNER), "--already-locked", tier]


def _wait_for_child(child_pid: int, forwarded_signal: list[int]) -> int:
    while True:
        try:
            _pid, status = os.waitpid(child_pid, 0)
            return os.waitstatus_to_exitcode(status)
        except InterruptedError:
            continue
        except ChildProcessError as exc:
            raise TierDispatchError("CRE tier worker disappeared before wait") from exc


def run_tier(
    tier: str,
    *,
    command: Sequence[str] | None = None,
    lock_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run one tier while holding the canonical authority for its full lifetime.

    ``command`` and ``lock_path`` are test seams only.  The public CLI always
    uses the primary checkout's canonical lock and the checked-in shell worker.
    """
    if tier not in TIERS:
        raise TierDispatchError(f"unknown CRE tier: {tier}")
    selected_lock = canonical_shared_lock_dir() if lock_path is None else lock_path
    selected_command = list(_default_command(tier) if command is None else command)
    if not selected_command:
        raise TierDispatchError("CRE tier worker command is empty")

    lock = SharedLock(selected_lock)
    try:
        lock.acquire()
    except LockHeldError as exc:
        print(
            f"[cre_tier_dispatch] CRE tier/recovery is active; skipping {tier}: {exc}",
            file=sys.stderr,
        )
        return 0

    if (
        lock.authority_fd < 0
        or lock.recovery_sync_fd < 0
        or lock.authority_token is None
        or lock.authority_generation is None
        or lock.lease_token != lock.authority_generation
    ):
        lock.release()
        raise TierDispatchError("CRE tier authority was not fully established")

    parent_env = dict(os.environ if environ is None else environ)
    parent_env.update(
        {
            "CRE_TIER_LOCK_AUTHORITY_FD": str(lock.authority_fd),
            "CRE_TIER_LOCK_SYNC_FD": str(lock.recovery_sync_fd),
            "CRE_TIER_LOCK_TOKEN": lock.authority_token,
            "CRE_TIER_LOCK_GENERATION": lock.authority_generation,
            "CRE_TIER_LOCK_OWNER_PID": str(os.getpid()),
        }
    )
    original_inheritable = {
        descriptor: os.get_inheritable(descriptor)
        for descriptor in (lock.authority_fd, lock.recovery_sync_fd)
    }
    for descriptor in original_inheritable:
        os.set_inheritable(descriptor, True)

    child_pid = -1
    forwarded_signal: list[int] = []
    previous_handlers: dict[int, object] = {}

    def forward_signal(signum: int, _frame: object) -> None:
        if signum not in forwarded_signal:
            forwarded_signal.append(signum)
        if child_pid > 0:
            try:
                os.kill(child_pid, signum)
            except ProcessLookupError:
                pass

    try:
        child_pid = os.fork()
        if child_pid == 0:
            os.execvpe(selected_command[0], selected_command, parent_env)
            raise AssertionError("unreachable after exec")
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, forward_signal)
        child_rc = _wait_for_child(child_pid, forwarded_signal)
        if child_rc < 0:
            return 128 + -child_rc
        if forwarded_signal and child_rc == 0:
            return 128 + forwarded_signal[-1]
        return child_rc
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        for descriptor, inheritable in original_inheritable.items():
            try:
                os.set_inheritable(descriptor, inheritable)
            except OSError:
                pass
        lock.release()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tier", nargs="?", choices=sorted(TIERS))
    parser.add_argument("--verify-inherited-lock", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.verify_inherited_lock:
            if args.tier is not None:
                parser.error("--verify-inherited-lock takes no tier")
            verify_inherited_lock()
            return 0
        if args.tier is None:
            parser.error("tier is required")
        return run_tier(args.tier)
    except TierDispatchError as exc:
        print(f"[cre_tier_dispatch] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
