#!/usr/bin/env python3
"""Render a read-only terminal view of a checkpoint-series manifest."""

from __future__ import annotations

import argparse
import json
import os
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
    value = json.loads(path.read_text(encoding="utf-8"))
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
    started = _parse_timestamp(manifest.get("started_at"))
    updated = _parse_timestamp(manifest.get("updated_at"))
    finished = _parse_timestamp(manifest.get("finished_at"))
    current_source = running[0] if running else None
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
                    attempt_log = str((manifest_path.parent / relative_log).resolve())

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
        "percent_complete": (complete / total * 100) if total else 0.0,
        "percent_handled": (handled / total * 100) if total else 0.0,
        "elapsed_seconds": (end - started).total_seconds() if started else None,
        "updated_age_seconds": (now - updated).total_seconds() if updated else None,
        "current_source": current_source,
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
        character if ord(character) >= 32 and not 127 <= ord(character) <= 159 else "?"
        for character in str(value)
    )


def render(snapshot: Mapping[str, Any], *, color: bool) -> str:
    def paint(code: str, value: object) -> str:
        text = _safe_text(value)
        return f"\033[{code}m{text}\033[0m" if color else text

    status = _safe_text(snapshot["status"])
    status_color = "32" if status in {"complete", "supported_scope_complete"} else "36"
    if snapshot["failed"] or status == "failed":
        status_color = "31"
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

    if snapshot["current_source"]:
        lines.extend(
            [
                "",
                f"Recorded source        {paint('33', snapshot['current_source'])}",
                f"Outer attempt          {paint('0', snapshot['current_attempt'])}",
            ]
        )
        if snapshot["attempt_log"]:
            lines.append(
                f"Attempt log            {paint('0', snapshot['attempt_log'])}"
            )

    if snapshot["error_recorded"]:
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
        help="refresh in place every SECONDS (default: 5)",
    )
    value.add_argument("--json", action="store_true", help="print one JSON snapshot")
    value.add_argument("--no-color", action="store_true", help="disable ANSI color")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.watch is not None and args.watch < 0.5:
        raise SystemExit("--watch interval must be at least 0.5 seconds")
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
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        print(f"cannot render series status: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
