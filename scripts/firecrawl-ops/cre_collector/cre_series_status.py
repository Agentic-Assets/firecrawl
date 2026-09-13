#!/usr/bin/env python3
"""Render a read-only terminal view of a checkpoint-series manifest."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import sys
import time
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

COLLECTOR_DIR = Path(__file__).resolve().parent
DEFAULT_SERIES_ROOT = COLLECTOR_DIR / "out" / "checkpoint-series"
SCHEMA_VERSION = 1
HANDLED_STATES = {"complete", "failed_source"}
SOURCE_STATES = {
    "pending",
    "running",
    "complete",
    "failed_source",
    "failed_global",
    "interrupted",
    "resource_guard_interrupted",
}
SERIES_STATES = {
    "running",
    "cooling_down",
    "complete",
    "complete_with_source_failures",
    "failed",
    "interrupted",
    "resource_guard_interrupted",
}
FAILURE_STATES = {
    "failed_source",
    "failed_global",
    "interrupted",
    "resource_guard_interrupted",
}
RESOURCE_REASONS = {
    "host_cpu_sustained": "Sustained host CPU pressure",
    "host_cpu_start_blocked": "Host CPU above the start limit",
}
RESOURCE_PHASES = {"preflight", "collect", "collection"}
CHILD_STATES = {
    "pending",
    "collecting",
    "collected",
    "validated",
    "gated",
    "dry_run_passed",
    "ingesting",
    "ingest_recovery_required",
    "ingested",
    "complete",
    "failed",
    "collect_infrastructure_failed",
    "collect_failed",
    "artifact_rejected",
    "gate_failed",
    "gate_blocked",
    "baseline_seed_required",
    "dry_run_failed",
}
JLL_DETAIL_PROGRESS = re.compile(
    r"^\s*jll/(sale|lease): detail enriched (\d{1,9})/(\d{1,9})\s*$"
)


def _read_regular_file(path: Path, *, limit: int, tail: bool = False) -> bytes:
    """Bound reads and reject special files without blocking on a FIFO open."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("status artifact is not a regular file")
        if tail:
            os.lseek(descriptor, max(0, info.st_size - limit), os.SEEK_SET)
        elif info.st_size > limit:
            raise OSError("status artifact exceeds the read limit")
        return os.read(descriptor, limit)
    finally:
        os.close(descriptor)


def _contained_path(root: Path, value: object) -> Path | None:
    if not isinstance(value, str) or Path(value).is_absolute():
        return None
    try:
        target = (root / value).resolve()
        if target.is_symlink():
            # Non-strict resolution can return the original path for a cycle.
            return None
        return target if target.is_relative_to(root.resolve()) else None
    except (OSError, ValueError, RuntimeError):
        return None


def child_progress(
    manifest: Mapping[str, Any], path: Path, source: str
) -> dict[str, Any] | None:
    """Read bounded local evidence, never execute a child or query the database."""
    checkpoint = manifest["sources"][source]
    run_dir = _contained_path(path.parent, checkpoint.get("checkpoint_run"))
    if run_dir is None or not run_dir.is_relative_to((path.parent / "runs").resolve()):
        return None
    try:
        child_manifest = _contained_path(run_dir, "manifest.json")
        if child_manifest is None:
            return None
        raw = _read_regular_file(child_manifest, limit=4 * 1024 * 1024)
        child = json.loads(raw)
        if (
            not isinstance(child, Mapping)
            or child.get("schema_version") != 2
            or child.get("collector_git_sha") != manifest.get("collector_git_sha")
            or child.get("run_id") != run_dir.name
            or not isinstance(child.get("sources"), Mapping)
            or set(child["sources"]) != {source}
        ):
            return None
        record = child["sources"][source]
        if not isinstance(record, Mapping) or record.get("state") not in CHILD_STATES:
            return None
        progress = {"state": record["state"], "detail_pass": None}
        attempts = record.get("attempts")
        if source != "jll" or not isinstance(attempts, list) or not attempts:
            return progress
        for attempt in reversed(attempts[-8:]):
            log = (
                _contained_path(run_dir, attempt.get("log"))
                if isinstance(attempt, Mapping)
                else None
            )
            if log is None:
                continue
            try:
                lines = (
                    _read_regular_file(log, limit=65_536, tail=True)
                    .decode("utf-8", errors="replace")
                    .splitlines()
                )
            except OSError:
                continue
            for line in reversed(lines):
                match = JLL_DETAIL_PROGRESS.fullmatch(line)
                if match:
                    completed, total = int(match[2]), int(match[3])
                    if 0 <= completed <= total and total > 0:
                        progress["detail_pass"] = {
                            "transaction": match[1],
                            "completed": completed,
                            "total": total,
                            "attempt": _finite_nonnegative(attempt.get("number")),
                        }
                        return progress
        return progress
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def _finite_nonnegative(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        converted = float(value)
    except OverflowError:
        return None
    return converted if math.isfinite(converted) and converted >= 0 else None


def recovery_snapshot(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project only defined, body-free recovery fields from the parent."""
    recovery = manifest.get("resource_recovery")
    if not isinstance(recovery, Mapping):
        return None
    active = recovery.get("active")
    if not isinstance(active, Mapping):
        return None
    source = active.get("source")
    sources = manifest.get("sources")
    if (
        not isinstance(source, str)
        or not isinstance(sources, Mapping)
        or source not in sources
    ):
        return None
    config = manifest.get("config")
    recovery_config = (
        config.get("resource_recovery") if isinstance(config, Mapping) else None
    )
    low_target = (
        recovery_config.get("low_cpu_percent")
        if isinstance(recovery_config, Mapping)
        else None
    )
    reason_code = active.get("reason_code")
    phase = active.get("phase")
    numeric = {
        name: _finite_nonnegative(active.get(name))
        for name in (
            "waited_seconds",
            "current_host_cpu_percent",
            "low_cpu_seconds",
            "required_low_cpu_seconds",
            "remaining_cooldown_seconds",
            "source_recoveries",
            "max_source_recoveries",
            "remaining_series_wait_seconds",
            "preserved_detail_count",
        )
    }
    # Unknown enum values or arbitrary messages never reach the terminal/JSON.
    return {
        **numeric,
        "source": source,
        "reason": RESOURCE_REASONS.get(reason_code)
        if isinstance(reason_code, str)
        else None,
        "phase": phase if isinstance(phase, str) and phase in RESOURCE_PHASES else None,
        "low_cpu_percent": _finite_nonnegative(low_target),
        "cumulative_wait_seconds": _finite_nonnegative(
            recovery.get("cumulative_wait_seconds")
        ),
    }


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def resolve_series_path(value: str | None, root: Path = DEFAULT_SERIES_ROOT) -> Path:
    if value:
        supplied = Path(value).expanduser().resolve()
        manifest = (
            supplied if supplied.name == "manifest.json" else supplied / "manifest.json"
        )
        if not manifest.is_file():
            raise FileNotFoundError(f"series manifest not found: {manifest}")
        return manifest

    candidates = [path for path in root.glob("*/manifest.json") if path.is_file()]
    if not candidates:
        raise FileNotFoundError(f"no checkpoint series found under {root}")
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.parent.name))


def load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(_read_regular_file(path, limit=4 * 1024 * 1024).decode("utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise TypeError(f"unsupported checkpoint-series manifest: {path}")
    sources = value.get("sources")
    config = value.get("config")
    configured_sources = config.get("sources") if isinstance(config, Mapping) else None
    if (
        not isinstance(sources, dict)
        or not isinstance(configured_sources, list)
        or len(configured_sources) != len(sources)
        or set(configured_sources) != set(sources)
    ):
        raise TypeError(f"invalid checkpoint-series manifest: {path}")
    states = []
    for checkpoint in sources.values():
        if not isinstance(checkpoint, Mapping):
            raise TypeError(f"invalid source checkpoint in series manifest: {path}")
        state = checkpoint.get("state")
        if state not in SOURCE_STATES:
            raise TypeError(f"invalid source state in series manifest: {path}")
        states.append(state)
    series_state = value.get("status")
    if series_state not in SERIES_STATES:
        raise TypeError(f"invalid series state in manifest: {path}")
    inconsistent = (
        (series_state == "complete" and any(state != "complete" for state in states))
        or (
            series_state == "complete_with_source_failures"
            and (
                "failed_source" not in states
                or any(state not in HANDLED_STATES for state in states)
            )
        )
        or (series_state == "failed" and "failed_global" not in states)
        or (series_state == "interrupted" and "interrupted" not in states)
        or (
            series_state == "resource_guard_interrupted"
            and "resource_guard_interrupted" not in states
        )
    )
    if inconsistent:
        raise TypeError(f"inconsistent checkpoint-series manifest: {path}")
    if series_state == "cooling_down":
        recovery = recovery_snapshot(value)
        if (
            recovery is None
            or value["resource_recovery"].get("state") != "cooling_down"
            or sources[recovery["source"]].get("state") != "resource_guard_interrupted"
        ):
            raise TypeError(f"inconsistent cooling-down manifest: {path}")
    return value


def build_snapshot(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    sources = manifest["sources"]
    counts = Counter(
        str(value.get("state", "unknown"))
        for value in sources.values()
        if isinstance(value, Mapping)
    )
    total = len(sources)
    complete = counts["complete"]
    handled = sum(counts[state] for state in HANDLED_STATES)
    running = [
        name
        for name, value in sources.items()
        if isinstance(value, Mapping) and value.get("state") == "running"
    ]
    failed = sum(counts[state] for state in FAILURE_STATES)
    recovery = recovery_snapshot(manifest)
    cooling = (
        manifest.get("status") == "cooling_down"
        and recovery is not None
        and manifest["resource_recovery"].get("state") == "cooling_down"
        and isinstance(sources[recovery["source"]], Mapping)
        and sources[recovery["source"]].get("state") == "resource_guard_interrupted"
    )
    if cooling:
        failed = max(0, failed - 1)
    started = _parse_timestamp(manifest.get("started_at"))
    updated = _parse_timestamp(manifest.get("updated_at"))
    finished = _parse_timestamp(manifest.get("finished_at"))
    stopped = [
        name
        for name, value in sources.items()
        if isinstance(value, Mapping)
        and value.get("state")
        in {"failed_global", "interrupted", "resource_guard_interrupted"}
    ]
    current_source = (
        running[0]
        if running
        else recovery["source"]
        if cooling
        else stopped[-1]
        if stopped
        else None
    )
    current_attempt = None
    attempt_log = None

    if current_source:
        source_state = sources[current_source]
        attempts = source_state.get("attempts")
        if isinstance(attempts, list) and attempts:
            latest_attempt = attempts[-1]
            if isinstance(latest_attempt, Mapping):
                current_attempt = latest_attempt.get("number")
                relative_log = latest_attempt.get("log")
                if isinstance(relative_log, str):
                    log_path = _contained_path(manifest_path.parent, relative_log)
                    if log_path is not None and log_path.is_relative_to(
                        (manifest_path.parent / "logs").resolve()
                    ):
                        attempt_log = str(log_path)

    end = finished or now
    return {
        "series_id": manifest.get("series_id") or manifest_path.parent.name,
        "series_path": str(manifest_path.parent.resolve()),
        "status": str(manifest.get("status", "unknown")),
        "collector_sha": manifest.get("collector_git_sha"),
        "total": total,
        "complete": complete,
        "handled": handled,
        "failed": failed,
        "running": counts["running"],
        "pending": counts["pending"],
        "cooling_down": int(cooling),
        "recovery": recovery,
        "percent_complete": (complete / total * 100) if total else 0.0,
        "percent_handled": (handled / total * 100) if total else 0.0,
        "elapsed_seconds": (end - started).total_seconds() if started else None,
        "updated_age_seconds": (now - updated).total_seconds() if updated else None,
        "current_source": current_source,
        "child_progress": child_progress(manifest, manifest_path, current_source)
        if current_source
        else None,
        "current_attempt": current_attempt,
        "attempt_log": attempt_log,
        # Exception text can contain request URLs or other provider context.
        # The viewer reports its presence without echoing raw artifact text.
        "error_recorded": bool(manifest.get("error")),
    }


def _bar(value: int, total: int, width: int = 30) -> str:
    filled = round(width * value / total) if total else 0
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _safe_text(value: object) -> str:
    return "".join(
        character if character.isprintable() else "?" for character in str(value)
    )


def render(snapshot: Mapping[str, Any], *, color: bool) -> str:
    def paint(code: str, value: object) -> str:
        text = _safe_text(value)
        return f"\033[{code}m{text}\033[0m" if color else text

    status = _safe_text(snapshot["status"])
    status_color = "32" if status in {"complete", "supported_scope_complete"} else "36"
    if snapshot["failed"] or status == "failed":
        status_color = "31"
    elif status == "cooling_down":
        status_color = "33"
    lines = [
        f"CRE checkpoint series  {paint('1', snapshot['series_id'])}",
        f"Recorded status        {paint(status_color, status)}",
        "",
        (
            f"{_bar(int(snapshot['complete']), int(snapshot['total']))} "
            f"{snapshot['complete']}/{snapshot['total']} complete "
            f"({snapshot['percent_complete']:.1f}%)"
        ),
        (
            f"Handled                {snapshot['handled']}/{snapshot['total']} "
            f"({snapshot['percent_handled']:.1f}%)"
        ),
        (
            f"Pending / running      {snapshot['pending']} / {snapshot['running']}"
            f"    Failed/interrupted {snapshot['failed']}"
        ),
    ]
    if snapshot["elapsed_seconds"] is not None:
        lines.append(f"Elapsed                {_duration(snapshot['elapsed_seconds'])}")
    if snapshot["updated_age_seconds"] is not None:
        lines.append(
            f"Manifest updated       {_duration(snapshot['updated_age_seconds'])} ago"
        )
    lines.append(f"Collector SHA          {paint('0', snapshot['collector_sha'])}")

    recovery = snapshot.get("recovery")
    if snapshot.get("cooling_down") and isinstance(recovery, Mapping):
        lines.extend(["", "Recovery               Waiting for host CPU to cool"])
        if recovery.get("reason"):
            lines.append(f"Reason                 {paint('33', recovery['reason'])}")
        if recovery.get("phase"):
            lines.append(f"Interrupted phase      {paint('0', recovery['phase'])}")
        cpu, target = (
            recovery.get("current_host_cpu_percent"),
            recovery.get("low_cpu_percent"),
        )
        if cpu is not None and target is not None:
            lines.append(
                f"Host CPU               {cpu:.1f}%  (resume below {target:.1f}%)"
            )
        low, required = (
            recovery.get("low_cpu_seconds"),
            recovery.get("required_low_cpu_seconds"),
        )
        if low is not None and required is not None:
            lines.append(
                f"Stable cool window     {_duration(low)} / {_duration(required)}"
            )
        waited, remaining = (
            recovery.get("waited_seconds"),
            recovery.get("remaining_cooldown_seconds"),
        )
        if waited is not None and remaining is not None:
            lines.append(
                f"Cooldown               {_duration(waited)} elapsed; {_duration(remaining)} left"
            )
        series_remaining = recovery.get("remaining_series_wait_seconds")
        if series_remaining is not None:
            lines.append(
                f"Series recovery budget {_duration(series_remaining)} remaining"
            )
        count, limit = (
            recovery.get("source_recoveries"),
            recovery.get("max_source_recoveries"),
        )
        if count is not None and limit is not None:
            lines.append(f"Source recoveries      {int(count)} / {int(limit)}")
        preserved = recovery.get("preserved_detail_count")
        if preserved is not None:
            lines.append(
                f"Preserved details      {int(preserved):,}  (original observation times)"
            )

    if snapshot["current_source"]:
        lines.extend(
            [
                "",
                f"Recorded source        {paint('33', snapshot['current_source'])}",
                f"Outer attempt          {paint('0', snapshot['current_attempt'])}",
            ]
        )
        progress = snapshot.get("child_progress")
        if isinstance(progress, Mapping):
            lines.append(f"Checkpoint phase       {paint('0', progress['state'])}")
            detail = progress.get("detail_pass")
            if isinstance(detail, Mapping):
                lines.append(
                    f"Last detail counter    {paint('0', detail['transaction'])}: "
                    f"{detail['completed']:,}/{detail['total']:,} in this pass"
                )
                if detail.get("attempt") is not None:
                    lines.append(
                        f"Detail attempt         {int(detail['attempt'])}  (last recorded counter)"
                    )
                lines.append(
                    "                       Includes cache/error outcomes; not listing completion"
                )
        if snapshot["attempt_log"]:
            lines.append(
                f"Attempt log            {paint('0', snapshot['attempt_log'])}"
            )

    if snapshot["error_recorded"] and not snapshot.get("cooling_down"):
        lines.extend(
            [
                "",
                f"Latest error           {paint('31', 'recorded in manifest')}",
            ]
        )
    lines.extend(["", f"Artifacts              {paint('0', snapshot['series_path'])}"])
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Show progress from durable CRE checkpoint-series artifacts."
    )
    value.add_argument(
        "series",
        nargs="?",
        help="series directory or manifest.json (default: newest series)",
    )
    value.add_argument(
        "--watch",
        nargs="?",
        const=5.0,
        type=float,
        metavar="SECONDS",
        help="refresh every 0.5-3600 seconds (default: 5; incompatible with --json)",
    )
    value.add_argument("--json", action="store_true", help="print one JSON snapshot")
    value.add_argument("--no-color", action="store_true", help="disable ANSI color")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.watch is not None and (
        not math.isfinite(args.watch) or not 0.5 <= args.watch <= 3600
    ):
        raise SystemExit(
            "--watch interval must be finite and between 0.5 and 3600 seconds"
        )
    if args.json and args.watch is not None:
        raise SystemExit("--json cannot be combined with --watch")
    try:
        manifest_path = resolve_series_path(args.series)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2

    ansi = (
        sys.stdout.isatty()
        and os.environ.get("TERM", "") != "dumb"
        and not args.no_color
        and "NO_COLOR" not in os.environ
    )
    try:
        while True:
            manifest = load_manifest(manifest_path)
            snapshot = build_snapshot(manifest, manifest_path)
            if args.json:
                print(json.dumps(snapshot, sort_keys=True))
                return 0
            if args.watch is not None and ansi:
                print("\033[2J\033[H", end="")
            print(render(snapshot, color=ansi), flush=True)
            if args.watch is None:
                return 0
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0
    except (OSError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"cannot render series status: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
