"""Bounded, sanitized diagnostics for CRE host-resource incidents.

This module intentionally contains no collector, network, or database logic.
It is called only when the existing Mach CPU guard first enters a high window.
Failures are evidence-only: they must never change the guard's fail-closed
termination behavior.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCHEMA_VERSION = 1
MAX_OUTPUT_BYTES = 32_768
MAX_PROCESSES = 16
SAFE_CONTEXT_FIELDS = {"source", "phase", "attempt", "child_pid", "process_group"}
SAFE_PHASES = {
    "preflight",
    "healthcheck",
    "pre_validation",
    "collect",
    "artifact_validate",
    "source_gate",
    "ingest_dry_run",
    "aggregate_gate",
    "ingest",
    "readback",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_context(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep only structured, non-secret stage identifiers."""
    if not value:
        return {}
    out: dict[str, Any] = {}
    for key in SAFE_CONTEXT_FIELDS:
        item = value.get(key)
        if item is None:
            continue
        if key == "phase":
            if item in SAFE_PHASES:
                out[key] = item
        elif key == "source":
            if isinstance(item, str) and item.replace("-", "").isalnum() and len(item) <= 64:
                out[key] = item
        elif isinstance(item, int) and item > 0:
            out[key] = item
    return out


def _run(argv: Sequence[str]) -> tuple[str, str | None]:
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            check=False,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", f"{type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        return "", f"exit {completed.returncode}"
    raw = completed.stdout[:MAX_OUTPUT_BYTES]
    if len(completed.stdout.encode("utf-8", errors="ignore")) > MAX_OUTPUT_BYTES:
        return raw, "output_truncated"
    return raw, None


def parse_ps(output: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split(None, 5)
        if len(parts) != 6:
            continue
        try:
            rows.append(
                {
                    "pid": int(parts[0]),
                    "ppid": int(parts[1]),
                    "cpu_percent": round(float(parts[2]), 2),
                    "rss_kb": int(parts[3]),
                    "elapsed": parts[4],
                    # `comm`, unlike command/args, excludes URLs, headers, and env.
                    "command": Path(parts[5]).name,
                }
            )
        except ValueError:
            continue
    return sorted(rows, key=lambda row: row["cpu_percent"], reverse=True)[:MAX_PROCESSES]


def parse_docker_stats(output: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        name = value.get("Name")
        if not isinstance(name, str):
            continue
        rows.append(
            {
                "name": name,
                "cpu_percent": value.get("CPUPerc"),
                "mem_usage": value.get("MemUsage"),
                "pids": value.get("PIDs"),
            }
        )
    return rows


def capture_incident(context: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a bounded diagnostic snapshot.  Never raises."""
    errors: list[str] = []
    ps_output, ps_error = _run(["/bin/ps", "-Ao", "pid=,ppid=,%cpu=,rss=,etime=,comm="])
    if ps_error:
        errors.append(f"ps: {ps_error}")
    stats_output, stats_error = _run(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{json .}}",
            "firecrawl-api-1",
            "firecrawl-playwright-service-1",
        ]
    )
    if stats_error:
        errors.append(f"docker_stats: {stats_error}")
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": utc_now(),
        "context": sanitize_context(context),
        "host_processes": parse_ps(ps_output),
        "containers": parse_docker_stats(stats_output),
        "snapshot_errors": errors,
    }


def append_incident(
    path: Path,
    *,
    host_cpu_percent: float,
    max_host_cpu_percent: float,
    context: Mapping[str, Any] | None,
    snapshotter: Callable[[Mapping[str, Any] | None], dict[str, Any]] = capture_incident,
) -> dict[str, Any] | None:
    """Persist one best-effort high-window snapshot and return its metadata."""
    try:
        record = snapshotter(context)
        record.update(
            {
                "trigger": "high_started",
                "host_cpu_percent": round(host_cpu_percent, 2),
                "max_host_cpu_percent": max_host_cpu_percent,
            }
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
        return {"log": str(path), "observed_at": record["observed_at"]}
    except Exception:
        # Incident evidence is deliberately non-fatal. The Mach guard remains
        # the source of truth for interrupt-and-checkpoint behavior.
        return None
