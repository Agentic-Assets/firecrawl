#!/usr/bin/env python3
"""Run one explicitly selected incremental tier with checkpoint safety.

Monitor defaults to inventory acquisition plus a database-free transform;
explicit --apply admits the existing bound monitor transaction.
Enrichment is SQL-only unless --apply is explicit. This foreground entrypoint
does not install schedules, run weekly/daily backstops, or recover failed runs.
Linux resource observations describe its kernel/VM, not the Windows host.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import cre_checkpoint_refresh as checkpoint
from cre_ingest import SOURCE_TO_BROKERAGE
from cre_memory_guard import HostMemoryGuard

COLLECTOR_DIR = Path(__file__).resolve().parent
DEFAULT_OUT_ROOT = COLLECTOR_DIR / "out" / "guarded-tiers"
MONITOR_EXCLUDED = frozenset({"jll", "jll-investor", "cbre-dealflow", "colliers"})
MAX_MONITOR_ARTIFACT_BYTES = 128 * 1024 * 1024
# Set only by this module's one-shot process entrypoint, never by main/library
# defaults. Identity comparison prevents a caller flag from granting the role.
_CLI_STOP_CAPABILITY = None


class GuardedTierError(RuntimeError):
    """A fixed, credential-free refusal of incremental work."""


class PreflightRefused(GuardedTierError):
    """An exact non-provider admission refusal after known local settlement."""


class TierDeadline(KeyboardInterrupt):
    """The foreground run exhausted its wall-clock work budget."""


def validate_windows_admission(admitted_unix_ms: int | None) -> None:
    """Reject stale Windows startup evidence; this is not a continuous guard."""
    if admitted_unix_ms is None:
        return
    if type(admitted_unix_ms) is not int:
        raise GuardedTierError("Windows host admission is stale or invalid")
    age = time.time() * 1000 - admitted_unix_ms
    if not math.isfinite(age) or not 0 <= age <= 15000:
        raise GuardedTierError("Windows host admission is stale or invalid")


@contextmanager
def work_deadline(seconds: float):
    """Interrupt owned work, then disarm and drain our timer before cleanup.

    Cleanup has the checkpoint owner's separate bounded settlement/join budgets.
    This is a process work deadline, not preemptive cancellation of kernel I/O.
    An existing process timer is refused rather than overwritten.
    """
    if not math.isfinite(seconds) or seconds <= 0:
        raise GuardedTierError("invalid tier deadline")
    if (
        signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0)
        or signal.SIGALRM in signal.sigpending()
        or signal.SIGALRM in signal.pthread_sigmask(signal.SIG_BLOCK, set())
    ):
        raise GuardedTierError("an existing process timer prevents admission")
    previous = signal.getsignal(signal.SIGALRM)
    previous_stops = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}

    def expired(_signum: int, _frame: Any) -> None:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        raise TierDeadline

    def stopped(_signum: int, _frame: Any) -> None:
        # An operator/guard stop must not encounter a second alarm inside
        # checkpoint's settlement routine, which defers INT/TERM, not ALRM.
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        raise KeyboardInterrupt

    signal.signal(signal.SIGALRM, expired)
    try:
        for signum in previous_stops:
            signal.signal(signum, stopped)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        # Never deliver our pending alarm to a restored caller handler. Blocking
        # precedes disarming; sigwait consumes only an already-pending SIGALRM.
        mask = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGALRM, signal.SIGINT, signal.SIGTERM}
        )
        try:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            if signal.SIGALRM in signal.sigpending():
                signal.sigwait({signal.SIGALRM})
            signal.signal(signal.SIGALRM, previous)
            for signum, handler in previous_stops.items():
                signal.signal(signum, handler)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)


def build_commands(args: argparse.Namespace, run_dir: Path, target: str | None):
    """Fixed direct workers; no legacy shell, dispatcher, or lifecycle flags."""
    if args.tier == "monitor":
        artifact = run_dir / "monitor.json"
        monitor = [
            sys.executable,
            str(COLLECTOR_DIR / "cre_monitor.py"),
            "--in",
            str(artifact),
            "--quiet",
            "--out",
            str(run_dir / "monitor-summary.json"),
        ]
        if args.apply:
            if target is None or not re.fullmatch(r"[0-9a-f]{64}", target):
                raise GuardedTierError("database target binding is unavailable")
            monitor.extend(["--apply", "--expected-db-target-sha256", target])
            if args.env_file:
                monitor.extend(["--env-file", args.env_file])
        else:
            monitor.append("--dry-run")
        return [
            (
                "collect",
                [
                    "npx",
                    "--no-install",
                    "tsx",
                    "collect.ts",
                    f"--source={args.source}",
                    "--monitor",
                    "--concurrency=1",
                    f"--out={artifact}",
                ],
            ),
            (
                "monitor_apply" if args.apply else "monitor_dry_run",
                monitor,
            ),
        ]
    argv = [
        sys.executable,
        str(COLLECTOR_DIR / "cre_enrich.py"),
        "--source",
        args.source,
        "--batch",
        str(args.batch),
    ]
    if args.apply:
        if target is None or not re.fullmatch(r"[0-9a-f]{64}", target):
            raise GuardedTierError("database target binding is unavailable")
        argv.extend(["--expected-db-target-sha256", target])
        if args.source == "matthews":
            if sys.platform != "linux" or args.deadline_seconds > 600:
                raise GuardedTierError("targeted Matthews requires Linux and a deadline at most 600 seconds")
            argv.extend(["--matthews-targeted-refresh", "--guarded-run-id", run_dir.name,
                         "--work-seconds", str(args.deadline_seconds)])
        if args.env_file:
            argv.extend(["--env-file", args.env_file])
    else:
        argv.append("--dry-run")
    return [("enrich_apply" if args.apply else "enrich_dry_run", argv)]


def _command_output(path: Path) -> list[str]:
    # run_command owns this fresh log and brackets stdout with fixed records.
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or not lines[-1].endswith(" rc=0"):
        raise PreflightRefused("checkout identity could not be verified")
    return lines[1:-1]


def validate_monitor_artifact(path: Path, source: str) -> None:
    """A zero exit alone does not prove both inventory passes completed."""
    with path.open("rb") as handle:
        raw = handle.read(MAX_MONITOR_ARTIFACT_BYTES + 1)
    if len(raw) > MAX_MONITOR_ARTIFACT_BYTES:
        raise GuardedTierError("monitor artifact exceeds the bounded evidence policy")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise GuardedTierError("monitor artifact is invalid")
    meta = data.get("runMeta")
    sources = data.get("sources")
    listings = data.get("listings")
    if (
        not isinstance(meta, dict)
        or meta.get("mode") != "monitor"
        or meta.get("transactions") != list(checkpoint.TRANSACTIONS)
        or not isinstance(sources, list)
        or len(sources) != len(checkpoint.TRANSACTIONS)
        or not isinstance(listings, list)
        or type(data.get("totalListings")) is not int
        or data["totalListings"] != len(listings)
    ):
        raise GuardedTierError("monitor artifact scope is incomplete")
    counts = {}
    for entry in sources:
        if (
            not isinstance(entry, dict)
            or entry.get("sourceKey") != source
            or entry.get("supported") is not True
            or entry.get("transaction") not in checkpoint.TRANSACTIONS
            or entry["transaction"] in counts
            or entry.get("error") is not None
            or entry.get("truncated") is not False
            or type(entry.get("listingsCollected")) is not int
            or entry["listingsCollected"] < 0
        ):
            raise GuardedTierError("monitor source pass did not complete")
        counts[entry["transaction"]] = entry["listingsCollected"]
    observed = {tx: 0 for tx in checkpoint.TRANSACTIONS}
    for listing in listings:
        if (
            not isinstance(listing, dict)
            or listing.get("sourceKey") != source
            or listing.get("transactionMode") not in observed
        ):
            raise GuardedTierError("monitor listing scope is invalid")
        observed[listing["transactionMode"]] += 1
    if observed != counts:
        raise GuardedTierError("monitor source counts do not match its artifact")


def verify_checkout(
    expected_sha: str, execute: Callable[[str, Sequence[str]], None], run_dir: Path
):
    """Require clean exact HEAD and a matching local origin tracking tip.

    This does not fetch or claim fresh remote state. Remote tracking references
    must have been refreshed by the operator before admitting this invocation.
    """
    execute("git_head", ["git", "rev-parse", "HEAD"])
    if _command_output(run_dir / "logs/git_head.log") != [expected_sha]:
        raise PreflightRefused("checkout SHA differs from the expected revision")
    execute("git_clean", ["git", "status", "--porcelain", "--untracked-files=normal"])
    if _command_output(run_dir / "logs/git_clean.log"):
        raise PreflightRefused("checkout is not clean")
    execute(
        "git_pushed",
        ["git", "for-each-ref", "--format=%(objectname)", "refs/remotes/origin/"],
    )
    if expected_sha not in _command_output(run_dir / "logs/git_pushed.log"):
        raise PreflightRefused("expected revision has no matching origin tracking tip")


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _create_run_dir(out_root: Path) -> Path:
    missing = []
    parent = out_root
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        _fsync_directory(directory.parent)
    run_dir = out_root / checkpoint.allocate_run_id()
    run_dir.mkdir(mode=0o700)
    _fsync_directory(out_root)
    return run_dir


def run_guarded_tier(
    args: argparse.Namespace,
    *,
    _lock_path: Path | None = None,
    _out_root: Path | None = None,
    _runner: Callable[..., int] | None = None,
    _cpu_sampler: Callable[[], float] | None = None,
    _memory_sampler: Callable[[], int] | None = None,
    _verify_checkout: Callable[..., None] | None = None,
    _deadline_seconds: float | None = None,
    _cli_stop_capability: object | None = None,
) -> int:
    """Hidden Python seams support isolated, synthetic process fault tests."""
    if args.tier not in {"monitor", "enrich"} or args.source not in SOURCE_TO_BROKERAGE:
        raise GuardedTierError(
            "an exact supported source and incremental tier are required"
        )
    if not 1 <= args.batch <= 50 or not 30 <= args.deadline_seconds <= 14400:
        raise GuardedTierError("batch or deadline exceeds the bounded tier policy")
    if not re.fullmatch(r"[0-9a-f]{40}", args.expected_sha):
        raise GuardedTierError("a full expected checkout SHA is required")
    if args.tier == "monitor" and args.source in MONITOR_EXCLUDED:
        raise GuardedTierError("monitor-excluded sources are not admitted")
    if args.tier == "enrich" and args.source == "matthews" and args.apply:
        if sys.platform != "linux" or args.deadline_seconds > 600:
            raise GuardedTierError("targeted Matthews requires Linux and a deadline at most 600 seconds")
    if args.env_file and any(ord(c) < 32 or ord(c) == 127 for c in args.env_file):
        raise GuardedTierError("invalid environment file path")
    windows_admission = getattr(args, "windows_admission_unix_ms", None)
    validate_windows_admission(windows_admission)

    runner = checkpoint.run_command if _runner is None else _runner
    verifier = verify_checkout if _verify_checkout is None else _verify_checkout
    lock = checkpoint.SharedLock(
        checkpoint.canonical_shared_lock_dir() if _lock_path is None else _lock_path,
        recovery_required=True,
    )
    # No native child or original-run evidence is touched before admission.
    with lock:
        run_dir = _create_run_dir(DEFAULT_OUT_ROOT if _out_root is None else _out_root)
        receipt = {
            "schema_version": 1,
            "tier": args.tier,
            "source": args.source,
            "apply": args.apply,
            "batch": args.batch,
            "expected_sha": args.expected_sha,
            "deadline_seconds": args.deadline_seconds,
            "windows_admission_unix_ms": windows_admission,
            "started_at": checkpoint.utc_now(),
            "status": "running",
            "phase": "preflight",
            "recovery_fence": "armed_until_terminal_owner_clear",
            "local_settlement": {"state": "not_started"},
        }
        terminal_saved = False
        success = False
        native_work_started = False
        native_command_active = False
        tier_worker_started = False
        admission_refused = False
        pre_provider_refused = False
        settlement_unknown = False
        cpu = checkpoint.HostCpuGuard(log_path=run_dir / "logs/host-cpu-guard.jsonl")
        if _cpu_sampler is not None:
            cpu.sampler = _cpu_sampler
        memory = HostMemoryGuard(
            log_path=run_dir / "logs/host-memory-guard.jsonl",
            sampler=_memory_sampler,
            context_provider=lambda: dict(cpu.incident_context),
            on_trip=lambda **values: checkpoint._trip_host_memory_guard(
                **values, context=cpu.incident_context
            ),
        )
        receipt["cpu_guard"] = cpu.config()
        receipt["memory_guard"] = memory.config()
        checkpoint._clear_cpu_guard_trip()
        previous_sigterm = signal.signal(
            signal.SIGTERM, checkpoint.checkpoint_sigterm_handler
        )

        def persist() -> None:
            # Strict directory fsync; a failed terminal publication stays fenced.
            checkpoint.atomic_write_text(
                run_dir / "receipt.json", json.dumps(receipt, sort_keys=True) + "\n"
            )

        def execute(phase: str, argv: Sequence[str]) -> None:
            nonlocal native_work_started, native_command_active, tier_worker_started
            receipt["phase"] = phase
            checkpoint.set_cpu_guard_context(
                cpu, phase=phase, source=args.source, active_operation=phase
            )
            persist()
            if not phase.startswith("git_") and not tier_worker_started:
                validate_windows_admission(windows_admission)
                tier_worker_started = True
            native_work_started = True
            native_command_active = True
            receipt["local_settlement"] = {"state": "unknown", "phase": phase}
            try:
                command_rc = runner(
                    argv,
                    run_dir / "logs" / f"{phase}.log",
                    env=checkpoint.safe_process_env(),
                )
            except BaseException as exc:
                settled = checkpoint.command_local_settlement(exc)
                if settled is not None:
                    receipt["local_settlement"] = {**settled, "phase": phase}
                raise
            # The production runner returns only after its owned group is
            # settled. Exceptions deliberately leave this active witness set.
            native_command_active = False
            receipt["local_settlement"] = {"state": "confirmed", "phase": phase}
            if command_rc != 0:
                if phase.startswith("git_"):
                    raise PreflightRefused("checkout identity could not be verified")
                raise GuardedTierError("tier command did not complete successfully")

        try:
            if (
                __name__ == "__main__"
                and _CLI_STOP_CAPABILITY is not None
                and _cli_stop_capability is _CLI_STOP_CAPABILITY
                and sys.platform == "linux"
            ):
                from cre_stop_guarded_tier import publish_stop_identity

                owner = publish_stop_identity(lock, run_dir)
                if owner is not None:
                    receipt["stop_owner"] = owner
            persist()
            if receipt.get("stop_owner") is not None:
                print(run_dir, flush=True)
            with checkpoint.resource_guard_boundary(memory, cpu):
                with work_deadline(
                    args.deadline_seconds
                    if _deadline_seconds is None
                    else _deadline_seconds
                ):
                    memory.preflight()
                    if memory.tripped:
                        raise checkpoint.CpuGuardTrip
                    memory.start()
                    if checkpoint.run_cpu_guard_preflight(cpu) >= cpu.max_percent:
                        checkpoint._set_cpu_guard_trip(
                            "host CPU prevented incremental tier admission",
                            reason_code="host_cpu_start_blocked",
                            telemetry_valid=True,
                            context=cpu.incident_context,
                        )
                        raise checkpoint.CpuGuardTrip
                    cpu.start()
                    verifier(args.expected_sha, execute, run_dir)
                    try:
                        target = (
                            checkpoint.database_target_fingerprint(args.env_file)[
                                "value"
                            ]
                            if args.apply
                            else None
                        )
                    except SystemExit as exc:
                        raise PreflightRefused(
                            "database environment is unavailable"
                        ) from exc
                    if target is not None:
                        receipt["database_target_sha256"] = target
                    for phase, argv in build_commands(args, run_dir, target):
                        execute(phase, argv)
                        if phase == "collect":
                            validate_monitor_artifact(
                                run_dir / "monitor.json", args.source
                            )
            success = True
            receipt["status"] = "complete"
            rc = 0
        except TierDeadline as exc:
            settlement_unknown = checkpoint._cleanup_unknown_in_chain(exc)
            receipt["status"] = "deadline_interrupted"
            rc = 124
        except (checkpoint.CpuGuardTrip, KeyboardInterrupt) as exc:
            settlement_unknown = checkpoint._cleanup_unknown_in_chain(exc)
            receipt["status"] = (
                "resource_guard_interrupted"
                if checkpoint._peek_cpu_guard_trip() is not None
                else "interrupted"
            )
            rc = 75 if receipt["status"] == "resource_guard_interrupted" else 130
            admission_refused = rc == 75 and not native_work_started
        except PreflightRefused as exc:
            settlement_unknown = checkpoint._cleanup_unknown_in_chain(exc)
            pre_provider_refused = not tier_worker_started and not native_command_active
            receipt["status"] = "failed_recovery_required"
            rc = 1
        except (Exception, SystemExit) as exc:
            settlement_unknown = checkpoint._cleanup_unknown_in_chain(exc)
            receipt["status"] = "failed_recovery_required"
            rc = 1
        finally:
            try:
                memory.stop()
                cpu.stop()
                guards_stopped = all(
                    guard.thread is None or not guard.thread.is_alive()
                    for guard in (memory, cpu)
                )
                receipt["finished_at"] = checkpoint.utc_now()
                receipt["guards_stopped"] = guards_stopped
                if settlement_unknown:
                    receipt["local_settlement"] = {"state": "unknown"}
                details = checkpoint._peek_cpu_guard_trip_details()
                clear_eligible = (
                    (success or admission_refused or pre_provider_refused)
                    and guards_stopped
                    and not native_command_active
                    and not settlement_unknown
                    and (
                        details is None
                        or (details.owned_processes_reaped and details.evidence_valid)
                    )
                )
                receipt["native_work_started"] = native_work_started
                receipt["native_command_active"] = native_command_active
                receipt["tier_worker_started"] = tier_worker_started
                receipt["recovery_clear_eligible"] = clear_eligible
                if pre_provider_refused and clear_eligible:
                    receipt["status"] = "preflight_refused"
                persist()
                terminal_saved = True
                if clear_eligible and terminal_saved:
                    lock.clear_recovery_requirement()
            finally:
                checkpoint._clear_cpu_guard_trip(reset_signals=False)
                signal.signal(signal.SIGTERM, previous_sigterm)
        print(run_dir)
        return rc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tier", choices=("monitor", "enrich"))
    parser.add_argument("--source", required=True, choices=sorted(SOURCE_TO_BROKERAGE))
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--batch", type=int, default=10)
    parser.add_argument("--deadline-seconds", type=int, default=1800)
    parser.add_argument("--env-file")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--windows-admission-unix-ms", type=int)
    args = parser.parse_args(argv)
    try:
        if __name__ == "__main__" and _CLI_STOP_CAPABILITY is not None:
            return run_guarded_tier(args, _cli_stop_capability=_CLI_STOP_CAPABILITY)
        return run_guarded_tier(args)
    except checkpoint.LockHeldError:
        print("CRE tier is blocked by active ownership or recovery", file=sys.stderr)
        return 73
    except (Exception, KeyboardInterrupt):
        print("CRE guarded tier refused or requires operator recovery", file=sys.stderr)
        return 1


if __name__ == "__main__":
    _CLI_STOP_CAPABILITY = object()
    raise SystemExit(main())
