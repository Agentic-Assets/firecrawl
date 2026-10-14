"""Linux one-shot series controls; private flock identity is not a CRE fence."""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from cre_owned_processes import OwnedProcessGroup, settle_owned_groups


class SeriesControlError(RuntimeError):
    """Control admission/publication failed; never authorize automatic replay."""


class SeriesWorkStopped(KeyboardInterrupt):
    """Typed control interruption survives legacy Git cleanup."""


def validate_control_budget(seconds):
    if seconds is None:
        return
    if (
        type(seconds) is not int
        or not 30 <= seconds <= 86400
        or sys.platform != "linux"
        or not hasattr(os, "pidfd_open")
        or not hasattr(signal, "pidfd_send_signal")
    ):
        raise SeriesControlError(
            "Linux control requires an integer budget of 30–86400 seconds and pidfd support"
        )


def _evidence():
    # Reuse only safe read/process/flock primitives, never incremental authority.
    import cre_stop_guarded_tier

    return cre_stop_guarded_tier


def _open_directories(series_dir):
    safe = _evidence()
    root = os.open(series_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        root_stat = safe._safe_stat(root, directory=True)
        private = os.open(
            ".series-control", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root
        )
        try:
            private_stat = safe._safe_stat(private, directory=True, private=True)
        except BaseException:
            os.close(private)
            raise
        return root, private, root_stat, private_stat
    except BaseException:
        os.close(root)
        raise


def read_receipt(series_dir, invocation_id):
    if (
        not isinstance(invocation_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None
    ):
        raise SeriesControlError("invalid invocation identity")
    safe = _evidence()
    root, private, root_stat, private_stat = _open_directories(series_dir)
    try:
        raw, _ = _read_private_receipt(private, invocation_id + ".json")
        receipt = json.loads(raw)
        owner = receipt.get("stop_owner") if isinstance(receipt, dict) else None
        if (
            not isinstance(owner, dict)
            or type(owner.get("schema_version")) is not int
            or owner["schema_version"] != 1
            or owner.get("owner_role") != "checkpoint_series_one_shot_cli"
            or owner.get("invocation_id") != invocation_id
            or owner.get("series_directory_identity") != safe._identity(root_stat)
            or owner.get("control_directory_identity") != safe._identity(private_stat)
        ):
            raise SeriesControlError("receipt has no matching series stop capability")
        return receipt
    finally:
        os.close(private)
        os.close(root)


def validate_live_owner(series_dir, receipt):
    safe = _evidence()
    owner = receipt["stop_owner"]
    pid = owner.get("pid")
    if type(pid) is not int or not 1 < pid <= 2147483647:
        raise SeriesControlError("invalid series owner")
    observed = safe._process_identity(pid)
    if any(owner.get(key) != value for key, value in observed.items()):
        raise SeriesControlError("series process birth identity changed")
    root, private, root_stat, private_stat = _open_directories(series_dir)
    try:
        if owner.get("series_directory_identity") != safe._identity(
            root_stat
        ) or owner.get("control_directory_identity") != safe._identity(private_stat):
            raise SeriesControlError("series namespace changed")
        raw, lock_stat = safe._read_at(root, ".series.lock", 128)
        if raw.strip() != str(pid).encode("ascii") or safe._identity(
            lock_stat
        ) != owner.get("series_lock_identity"):
            raise SeriesControlError("series lock identity changed")
        safe._held_flock(
            {
                "pid": pid,
                "authority_fd": owner.get("series_lock_fd"),
                "authority_identity": owner.get("series_lock_identity"),
            }
        )
    finally:
        os.close(private)
        os.close(root)


class SeriesControl:
    def __init__(self, series_dir, lock, seconds):
        validate_control_budget(seconds)
        self.series_dir, self.lock, self.seconds = Path(series_dir), lock, seconds
        self.invocation_id = uuid.uuid4().hex
        self.stop_reason = None
        self.defer_depth = 0
        self.group = None
        self.work_started = False
        self.cleanup_unknown = False
        self.spawn_uncertain = False
        self.receipt = None
        self.handlers_ready = False
        self.checkpoint_run = None
        self.checkpoint_pid = None
        self.checkpoint_sha = None
        self.child_unverified = False
        self.child_invocation_id = None

    def _signal(self, signum, _frame):
        signal.setitimer(signal.ITIMER_REAL, 0)
        if self.stop_reason is None:
            self.stop_reason = "deadline" if signum == signal.SIGALRM else "operator"
        if not self.defer_depth:
            raise SeriesWorkStopped

    def check(self):
        if self.stop_reason:
            raise SeriesWorkStopped

    @contextmanager
    def defer_stops(self):
        # Do not change the mask: Popen children must inherit ordinary signals.
        self.defer_depth += 1
        try:
            yield
        finally:
            self.defer_depth -= 1

    @contextmanager
    def handlers(self):
        if threading.current_thread() is not threading.main_thread():
            raise SeriesControlError("series controls require the main thread")
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        controls = {signal.SIGALRM, signal.SIGINT, signal.SIGTERM}
        if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0) or controls & (
            signal.sigpending() | blocked
        ):
            raise SeriesControlError(
                "existing timer or blocked control signals prevent admission"
            )
        previous = {s: signal.getsignal(s) for s in controls}
        try:
            for s in controls:
                signal.signal(s, self._signal)
            self.handlers_ready = True
            yield
        finally:
            mask = signal.pthread_sigmask(signal.SIG_BLOCK, controls)
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
                if signal.SIGALRM in signal.sigpending():
                    signal.sigwait({signal.SIGALRM})
                for s, handler in previous.items():
                    signal.signal(s, handler)
                self.handlers_ready = False
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)

    def publish_running(self):
        if not self.handlers_ready:
            raise SeriesControlError("handlers are not installed")
        safe = _evidence()
        root = os.open(self.series_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            safe._safe_stat(root, directory=True)
            try:
                os.mkdir(".series-control", 0o700, dir_fd=root)
            except FileExistsError:
                pass
            os.fsync(root)
        finally:
            os.close(root)
        # Persist a newly allocated series's directory entry too.
        parent = os.open(
            self.series_dir.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        root, private, root_stat, private_stat = _open_directories(self.series_dir)
        try:
            owner = {
                "schema_version": 1,
                "owner_role": "checkpoint_series_one_shot_cli",
                "invocation_id": self.invocation_id,
                "series_directory_identity": safe._identity(root_stat),
                "control_directory_identity": safe._identity(private_stat),
                "series_lock_fd": self.lock.fileno(),
                "series_lock_identity": safe._identity(os.fstat(self.lock.fileno())),
                **safe._process_identity(os.getpid()),
            }
        finally:
            os.close(private)
            os.close(root)
        self.receipt = {
            "stop_owner": owner,
            "status": "running",
            "work_deadline_seconds": self.seconds,
            "started_unix_ms": int(time.time() * 1000),
            "finished_unix_ms": None,
            "local_settlement": "unknown",
        }
        validate_live_owner(self.series_dir, self.receipt)
        self.publish()
        print(
            json.dumps(
                {
                    "series_dir": str(self.series_dir),
                    "invocation_id": self.invocation_id,
                }
            ),
            flush=True,
        )

    def publish(self):
        safe = _evidence()
        root, private, root_stat, private_stat = _open_directories(self.series_dir)
        try:
            owner = self.receipt["stop_owner"]
            if owner["series_directory_identity"] != safe._identity(root_stat) or owner[
                "control_directory_identity"
            ] != safe._identity(private_stat):
                raise SeriesControlError("series control namespace changed")
            _write_private_receipt(private, self.invocation_id + ".json", self.receipt)
        finally:
            os.close(private)
            os.close(root)

    def register_group(self, group):
        if self.group is not None:
            raise SeriesControlError("another owned native group remains active")
        self.group = group
        self.work_started = True
        self.spawn_uncertain = False

    def spawn(self, factory, *args, **kwargs):
        self.check()
        with self.defer_stops():
            self.spawn_uncertain = True
            process = factory(*args, **kwargs)
            group = OwnedProcessGroup.from_spawn(process)
            self.register_group(group)
            process._cre_owned_group = group
            self.checkpoint_pid = process.pid
            self.child_unverified = True
        return process

    def settled(self, group, known):
        if not known:
            self.cleanup_unknown = True
        if known and self.group is group:
            self.group = None

    def observe_exit(self, process):
        group = process._cre_owned_group
        # A reaped leader never authorizes a new signal to a surviving group.
        result = settle_owned_groups([group], grace_seconds=0, kill_seconds=0)
        self.settled(group, result.settled)
        if not result.settled:
            raise SeriesControlError("owned process cleanup_unknown")
        self.verify_checkpoint()

    def prepare_checkpoint(self, run_dir, git_sha):
        self.checkpoint_run = Path(run_dir)
        self.checkpoint_pid = None
        self.checkpoint_sha = git_sha
        self.child_invocation_id = uuid.uuid4().hex

    def verify_checkpoint(self):
        if not self.child_unverified:
            return
        root, private, _root_stat, _private_stat = _open_directories(
            self.checkpoint_run
        )
        try:
            raw, _ = _read_private_receipt(private, self.child_invocation_id + ".json")
            child = json.loads(raw)
            if (
                not isinstance(child, dict)
                or type(child.get("schema_version")) is not int
                or child["schema_version"] != 1
                or child.get("invocation_id") != self.invocation_id
                or child.get("child_invocation_id") != self.child_invocation_id
                or child.get("run_id") != self.checkpoint_run.name
                or child.get("collector_git_sha") != self.checkpoint_sha
                or type(child.get("pid")) is not int
                or child["pid"] != self.checkpoint_pid
                or child.get("guards_stopped") is not True
                or child.get("nested_cleanup_confirmed") is not True
                or child.get("terminal_evidence_saved") is not True
            ):
                raise SeriesControlError("checkpoint local settlement unverified")
            self.child_unverified = False
        finally:
            os.close(private)
            os.close(root)

    def settle_active(self):
        signal.setitimer(signal.ITIMER_REAL, 0)
        if self.group is not None:
            result = settle_owned_groups(
                [self.group], grace_seconds=45, initial_signal=signal.SIGTERM
            )
            self.settled(self.group, result.settled)
        if self.child_unverified:
            try:
                self.verify_checkpoint()
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                _evidence().StopRefused,
                SeriesControlError,
            ):
                self.cleanup_unknown = True

    def finish(self, status):
        signal.setitimer(signal.ITIMER_REAL, 0)
        self.defer_depth += 1  # Late operator stops cannot interrupt publication.
        self.settle_active()
        known = not (
            self.cleanup_unknown
            or self.spawn_uncertain
            or self.group is not None
            or self.child_unverified
        )
        if self.child_invocation_id is not None:
            self.receipt["last_checkpoint"] = {
                "run_id": self.checkpoint_run.name,
                "child_invocation_id": self.child_invocation_id,
                "pid": self.checkpoint_pid,
            }
        self.receipt.update(
            status=status if known else "cleanup_unknown",
            local_settlement=("confirmed" if self.work_started else "not_started")
            if known
            else "unknown",
            finished_unix_ms=int(time.time() * 1000),
        )
        self.publish()
        return known


def stop_series(series_dir, invocation_id, wait_seconds=60):
    result = {
        "outcome": "refused",
        "signal_sent": False,
        "owner_exited": False,
        "local_settlement": "unknown",
        "terminal_status": None,
    }
    pidfd = None
    safe = _evidence()
    try:
        if (
            sys.platform != "linux"
            or not hasattr(os, "pidfd_open")
            or not hasattr(signal, "pidfd_send_signal")
            or type(wait_seconds) is not int
            or not 1 <= wait_seconds <= 120
        ):
            raise SeriesControlError("unsupported stop invocation")
        receipt = read_receipt(series_dir, invocation_id)
        owner = receipt["stop_owner"]
        pid = owner.get("pid")
        if type(pid) is not int or not 1 < pid <= 2147483647 or pid == os.getpid():
            raise SeriesControlError("invalid stop owner")
        pidfd = os.pidfd_open(pid, 0)
        validate_live_owner(series_dir, receipt)
        deadline = time.monotonic() + wait_seconds
        fresh = read_receipt(series_dir, invocation_id)
        if fresh["stop_owner"] != owner:
            raise SeriesControlError("stop owner changed")
        if fresh.get("status") == "running":
            validate_live_owner(series_dir, fresh)
            final = read_receipt(series_dir, invocation_id)
            if final["stop_owner"] != owner:
                raise SeriesControlError("stop owner changed")
            if final.get("status") == "running":
                validate_live_owner(series_dir, final)
                if safe._exited(pidfd):
                    raise SeriesControlError("owner exited before stop")
                signal.pidfd_send_signal(pidfd, signal.SIGTERM, None, 0)
                result["signal_sent"] = True
        remaining = max(0, int((deadline - time.monotonic()) * 1000))
        if not safe._exited(pidfd, remaining):
            result["outcome"] = "stop_outcome_unverified"
            return 2, result
        result["owner_exited"] = True
        terminal = read_receipt(series_dir, invocation_id)
        if (
            terminal["stop_owner"] != owner
            or terminal.get("status")
            not in {
                "complete",
                "complete_with_source_failures",
                "failed",
                "interrupted",
                "deadline_interrupted",
                "resource_guard_interrupted",
            }
            or terminal.get("local_settlement") not in {"confirmed", "not_started"}
            or type(terminal.get("finished_unix_ms")) is not int
            or terminal["finished_unix_ms"] <= 0
        ):
            result["outcome"] = "stop_outcome_unverified"
            return 2, result
        result.update(
            local_settlement=terminal["local_settlement"],
            terminal_status=terminal["status"],
            outcome="completed"
            if terminal["status"] in {"complete", "complete_with_source_failures"}
            else "terminal_local_settlement_confirmed",
        )
        return 0, result
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        OverflowError,
        NotImplementedError,
        safe.StopRefused,
        SeriesControlError,
    ):
        if result["signal_sent"] or result["owner_exited"]:
            result["outcome"] = "stop_outcome_unverified"
            return 2, result
        return 1, result
    finally:
        if pidfd is not None:
            os.close(pidfd)


def _write_private_receipt(private, name, value):
    temporary = ".tmp-" + uuid.uuid4().hex
    pending = name + ".pending"
    # A durable pending witness remains on every failed publication. Removing
    # it is the final operation; crash resurrection can only refuse a receipt.
    marker = os.open(
        pending,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=private,
    )
    try:
        os.fsync(marker)
    finally:
        os.close(marker)
    os.fsync(private)
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=private,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=private, dst_dir_fd=private)
        os.fsync(private)
        os.unlink(pending, dir_fd=private)
    finally:
        try:
            os.unlink(temporary, dir_fd=private)
        except FileNotFoundError:
            pass


def validate_child_invocation(invocation_id):
    if invocation_id is not None and (
        sys.platform != "linux"
        or not isinstance(invocation_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None
    ):
        raise SeriesControlError("invalid controlled checkpoint invocation")


def publish_checkpoint_settlement(
    run_dir,
    invocation_id,
    manifest,
    *,
    child_invocation_id,
    guards_stopped,
    nested_cleanup_confirmed,
    terminal_evidence_saved,
):
    """Optional final hook AFTER existing canonical ownership teardown."""
    validate_child_invocation(invocation_id)
    validate_child_invocation(child_invocation_id)
    safe = _evidence()
    root = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        safe._safe_stat(root, directory=True)
        try:
            os.mkdir(".series-control", 0o700, dir_fd=root)
        except FileExistsError:
            pass
        os.fsync(root)
    finally:
        os.close(root)
    root, private, _root_stat, _private_stat = _open_directories(run_dir)
    try:
        _write_private_receipt(
            private,
            child_invocation_id + ".json",
            {
                "schema_version": 1,
                "invocation_id": invocation_id,
                "child_invocation_id": child_invocation_id,
                "run_id": Path(run_dir).name,
                "collector_git_sha": manifest["collector_git_sha"],
                "pid": os.getpid(),
                "guards_stopped": guards_stopped is True,
                "nested_cleanup_confirmed": nested_cleanup_confirmed is True,
                "terminal_evidence_saved": terminal_evidence_saved is True,
            },
        )
    finally:
        os.close(private)
        os.close(root)


def _read_private_receipt(private, name):
    def refuse_pending():
        try:
            os.stat(name + ".pending", dir_fd=private, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise SeriesControlError("receipt publication is incomplete")

    refuse_pending()
    result = _evidence()._read_at(private, name)
    refuse_pending()
    return result
