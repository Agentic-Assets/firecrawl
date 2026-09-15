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
import time
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
TIER_GROUP_TERM_GRACE_SECONDS = 5.0


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


def _escalate_owned_worker_group(worker_pid: int, worker_pgid: int) -> bool:
    """Escalate only while the unreaped session leader proves group identity."""
    if worker_pgid != worker_pid or not _owned_worker_group(worker_pid):
        return False
    return _signal_worker_group(worker_pgid, signal.SIGKILL)


def _wait_for_child(
    child_pid: int,
    forwarded_signal: list[int],
    *,
    worker_pgid: int = -1,
) -> int:
    """Reap the direct worker, escalating only while it is identity-bound."""
    term_deadline: float | None = None
    kill_attempted = False
    while True:
        try:
            waited_pid, status = os.waitpid(child_pid, os.WNOHANG)
        except InterruptedError:
            continue
        except ChildProcessError as exc:
            raise TierDispatchError("CRE tier worker disappeared before wait") from exc
        if waited_pid:
            return os.waitstatus_to_exitcode(status)
        if forwarded_signal and term_deadline is None:
            term_deadline = time.monotonic() + TIER_GROUP_TERM_GRACE_SECONDS
        if (
            term_deadline is not None
            and not kill_attempted
            and time.monotonic() >= term_deadline
        ):
            # The direct child remains unreaped while this loop runs, so its
            # PID cannot be reused.  The session-leader proof binds the KILL to
            # that exact dedicated group; a post-reap numeric PGID is never
            # escalated.
            _escalate_owned_worker_group(child_pid, worker_pgid)
            kill_attempted = True
        time.sleep(0.02)


def _owned_worker_group(worker_pid: int) -> bool:
    """Return whether ``worker_pid`` still leads its dedicated session/group."""
    if worker_pid <= 0 or worker_pid == os.getpgrp():
        raise TierDispatchError("CRE tier worker group is unsafe")
    try:
        return (
            os.getpgid(worker_pid) == worker_pid and os.getsid(worker_pid) == worker_pid
        )
    except ProcessLookupError:
        return False


def _signal_worker_group(worker_pgid: int, signum: int) -> bool:
    """Signal only a known dedicated worker group, never the dispatcher's group."""
    if worker_pgid <= 0 or worker_pgid == os.getpgrp():
        raise TierDispatchError("CRE tier worker group is unsafe")
    try:
        os.killpg(worker_pgid, signum)
    except ProcessLookupError:
        return False
    return True


def _worker_group_exists(worker_pgid: int) -> bool:
    """Probe the dedicated group without signaling the caller's group."""
    return _signal_worker_group(worker_pgid, 0)


def _drain_worker_group(worker_pgid: int) -> None:
    """Keep the caller's lock until every worker-group member is gone.

    A child shell can exit before a foreground descendant.  The group is a
    dedicated session whose ID is not the dispatcher's process group.  Once the
    shell is reaped, only a numeric PGID remains.  It is never signaled: an
    unrelated future group could reuse that number.  Escalation occurs before
    reaping, while the direct session leader still proves the group identity.
    Here the dispatcher merely keeps authority until the group is absent.  A
    reused PGID can therefore make it wait conservatively, but never receive a
    signal or cause an unlocked window.
    """
    while _worker_group_exists(worker_pgid):
        time.sleep(0.1)


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
    worker_pgid = -1
    forwarded_signal: list[int] = []
    previous_handlers: dict[int, object] = {}
    readiness_read, readiness_write = os.pipe()

    def forward_signal(signum: int, _frame: object) -> None:
        if signum not in forwarded_signal:
            forwarded_signal.append(signum)
        if worker_pgid > 0:
            try:
                if _owned_worker_group(worker_pgid):
                    _signal_worker_group(worker_pgid, signum)
            except TierDispatchError:
                # The direct leader is no longer an identity proof.  The reap
                # path keeps authority and waits, but must not signal a bare
                # PGID that might belong to another process group.
                pass

    try:
        child_pid = os.fork()
        if child_pid == 0:
            os.close(readiness_read)
            try:
                os.setsid()
                os.write(readiness_write, b"ready")
                os.close(readiness_write)
                os.execvpe(selected_command[0], selected_command, parent_env)
            except BaseException:
                try:
                    os.write(readiness_write, b"error")
                except OSError:
                    pass
                os._exit(127)
        os.close(readiness_write)
        readiness_write = -1
        if os.read(readiness_read, 16) != b"ready" or not _owned_worker_group(
            child_pid
        ):
            _wait_for_child(child_pid, forwarded_signal)
            raise TierDispatchError("CRE tier worker session could not be established")
        worker_pgid = child_pid
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, forward_signal)
        child_rc = _wait_for_child(child_pid, forwarded_signal, worker_pgid=worker_pgid)
        _drain_worker_group(worker_pgid)
        if child_rc < 0:
            return 128 + -child_rc
        if forwarded_signal and child_rc == 0:
            return 128 + forwarded_signal[-1]
        return child_rc
    finally:
        if readiness_read >= 0:
            try:
                os.close(readiness_read)
            except OSError:
                pass
        if readiness_write >= 0:
            try:
                os.close(readiness_write)
            except OSError:
                pass
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
