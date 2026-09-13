#!/usr/bin/env python3
"""Read bounded CRE performance evidence without network or database access."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

from cre_ingest import SOURCE_TO_BROKERAGE
from cre_series_status import _contained_path, _read_regular_file, resolve_series_path

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_JOURNAL_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 4096
MAX_SNAPSHOT_BYTES = 128 * 1024
MAX_CPU_BYTES = 4 * 1024 * 1024
MAX_FILES_PER_SOURCE = 256
MAX_LOG_DIRECTORY_ENTRIES = 4096
MAX_COMMANDS_PER_SOURCE = 2048
RUN_ID = re.compile(r"\d{4}-\d{2}-\d{2}T\d{6}Z(?:-[0-9a-f]{12})?\Z")
COMMAND_ID = re.compile(r"[0-9a-f]{32}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
SAFE_FILE = re.compile(r"[a-zA-Z0-9_.-]{1,200}\Z")
PHASES = {
    "collection",
    "healthcheck",
    "pre_validation",
    "source_gate",
    "dry_run",
    "aggregate_gate",
    "ingest",
    "ingest_recovery",
    "readback",
    "unknown",
}
OUTCOMES = {"running", "success", "failed", "interrupted", "exception"}
STATUSES = {
    "pending",
    "running",
    "complete",
    "supported_scope_complete",
    "selected_transaction_scope_complete",
    "additive_scope_complete_coverage_hold",
    "failed",
    "failed_source",
    "failed_global",
    "interrupted",
    "resource_guard_interrupted",
    "cooling_down",
    "complete_with_failures",
}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        converted = float(value)
    except OverflowError:
        return None
    return converted if math.isfinite(converted) and 0 <= converted <= 1e18 else None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _object(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _member(value: object, choices: set[str]) -> bool:
    return isinstance(value, str) and value in choices


def _schema(value: object, expected: int) -> bool:
    return type(value) is int and value == expected


def _read_json(path: Path, *, limit: int) -> Mapping[str, Any]:
    return _object(json.loads(_read_regular_file(path, limit=limit)))


def _duration(start: object, end: object) -> float | None:
    first, last = _timestamp(start), _timestamp(end)
    if first is None or last is None or last < first:
        return None
    return round((last - first).total_seconds(), 3)


def _safe_filename(value: object) -> str | None:
    if not isinstance(value, str) or not SAFE_FILE.fullmatch(value):
        return None
    return value if value not in {".", ".."} else None


def _numeric_fields(value: object, names: tuple[str, ...]) -> dict[str, Any]:
    record = _object(value)
    return {name: _number(record.get(name)) for name in names}


def _source_metrics(value: object, names: tuple[str, ...]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 2 * len(SOURCE_TO_BROKERAGE):
        return []
    result = []
    for item in value:
        record = _object(item)
        source, transaction = record.get("source"), record.get("transaction")
        if not isinstance(source, str) or source not in SOURCE_TO_BROKERAGE:
            continue
        if not _member(transaction, {"sale", "lease"}):
            continue
        result.append(
            {
                "source": source,
                "transaction": transaction,
                **_numeric_fields(record, names),
            }
        )
    return result


def _snapshot(
    logs: Path, record: Mapping[str, Any], *, now: datetime
) -> dict[str, Any] | None:
    name = _safe_filename(record.get("metrics_file"))
    if name is None or not name.endswith(".scrape-performance.json"):
        return None
    try:
        value = _read_json(logs / name, limit=MAX_SNAPSHOT_BYTES)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None
    updated = _timestamp(value.get("updated_at"))
    started = _timestamp(value.get("started_at"))
    if (
        not _schema(value.get("schema_version"), 1)
        or value.get("kind") != "cre_scrape_performance"
        or value.get("run_id") != record.get("run_id")
        or value.get("command_id") != record.get("command_id")
        or updated is None
        or started is None
        or updated < started
        or (updated - now).total_seconds() > 300
    ):
        return None
    metrics = _object(value.get("metrics"))
    requests = _object(metrics.get("requests"))
    names = (
        "attempts_started",
        "attempts_completed",
        "succeeded",
        "failed",
        "active_locally_awaited",
        "max_active_locally_awaited",
        "summed_latency_ms",
        "approximate_p50_ms",
        "approximate_p95_ms",
        "fresh_requested",
        "other_valid_statuses",
        "timed_out_remote_settlement_unknown",
    )
    clean_requests = {name: _number(requests.get(name)) for name in names}
    for kind in ("http_helper", "json_parse"):
        retry = _object(_object(requests.get("retry")).get(kind))
        clean_requests[kind] = {
            name: _number(retry.get(name))
            for name in ("retry_attempts", "backoff_ms", "terminal_backoff_ms")
        }
    age = max(0.0, (now - updated).total_seconds())
    terminal = value.get("terminal") is True
    monotonic_ms = _number(metrics.get("elapsed_ms"))
    elapsed = monotonic_ms / 1000 if monotonic_ms is not None else None
    successful = clean_requests["succeeded"]
    request_rate = (
        round(successful / elapsed, 3)
        if successful is not None and elapsed is not None and elapsed > 0
        else None
    )
    cache = _object(_object(metrics.get("cache")).get("jll_detail"))
    resources = _object(metrics.get("resources"))
    memory = _object(resources.get("node_rss_bytes"))
    cpu = _object(resources.get("process_cpu_microseconds"))
    histogram = _object(requests.get("latency_histogram"))
    bounds, counts = histogram.get("bounds_ms"), histogram.get("counts")
    clean_histogram = None
    if (
        isinstance(bounds, list)
        and 0 < len(bounds) <= 32
        and isinstance(counts, list)
        and len(counts) == len(bounds) + 1
        and all(_number(item) is not None for item in [*bounds, *counts])
        and all(left < right for left, right in pairwise(bounds))
    ):
        clean_histogram = {"bounds_ms": bounds, "counts": counts}
    clean_requests["latency_histogram"] = clean_histogram
    clean_requests["error_categories"] = _numeric_fields(
        requests.get("error_categories"),
        ("timeout", "http_4xx", "http_5xx", "transport", "empty_response", "unknown"),
    )
    statuses = _object(requests.get("status_counts"))
    clean_requests["status_counts"] = {
        key: _number(value)
        for key, value in list(statuses.items())[:32]
        if isinstance(key, str) and re.fullmatch(r"[1-5][0-9]{2}", key)
    }
    clean_requests["by_source_transaction"] = _source_metrics(
        requests.get("by_source_transaction"),
        (
            "attempts_started",
            "attempts_completed",
            "succeeded",
            "failed",
            "fresh_requested",
        ),
    )
    return {
        "updated_at": updated.isoformat(),
        "age_seconds": round(age, 1),
        "terminal": terminal,
        "state": (
            "degraded"
            if value.get("degraded") is True
            else "terminal"
            if terminal
            else "stale_or_stalled"
            if age > 30
            else "partial"
        ),
        "elapsed_seconds": elapsed,
        "successful_client_attempts_per_second": request_rate,
        "requests": clean_requests,
        "logical_scrape_calls": _numeric_fields(
            metrics.get("logical_scrape_calls"), ("raw", "doc", "json")
        ),
        "source_runs": _source_metrics(
            metrics.get("source_runs"),
            (
                "started",
                "completed",
                "succeeded",
                "failed",
                "listings_emitted",
                "elapsed_ms",
            ),
        ),
        "jll_detail_cache": {
            name: _number(cache.get(name))
            for name in ("hits", "misses", "refresh_bypasses")
        },
        "collector_process": {
            "rss_bytes": _number(memory.get("current")),
            "max_sampled_rss_bytes": _number(memory.get("max_sampled")),
            "cpu_user_us": _number(cpu.get("user_cumulative")),
            "cpu_system_us": _number(cpu.get("system_cumulative")),
        },
        "coverage": {
            "requests": "shared_firecrawl_helpers_only",
            "concurrency": "locally_awaited_attempts_not_server_concurrency",
            "latency": "approximate_histogram_attempt_latency_excludes_backoff",
            "api_queue_wait": "unknown_uninstrumented",
            "direct_provider_requests": "unknown_uninstrumented",
            "other_caches": "unknown_uninstrumented",
            "listing_quality": "not_established_by_request_metrics",
        },
    }


def _commands(
    run_dir: Path, *, now: datetime
) -> tuple[list[dict[str, Any]], list[str]]:
    logs = _contained_path(run_dir, "logs")
    if logs is None:
        return [], ["logs_unavailable"]
    warnings: list[str] = []
    journals: list[str] = []
    try:
        with os.scandir(logs) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_LOG_DIRECTORY_ENTRIES:
                    return [], ["log_directory_entry_limit_exceeded"]
                if entry.name.endswith(".performance.jsonl"):
                    journals.append(entry.name)
                    if len(journals) > MAX_FILES_PER_SOURCE:
                        return [], ["journal_file_limit_exceeded"]
    except (OSError, ValueError):
        return [], ["logs_unavailable"]
    commands: dict[str, dict[str, Any]] = {}
    for name in sorted(journals):
        try:
            raw = _read_regular_file(logs / name, limit=MAX_JOURNAL_BYTES)
        except (OSError, ValueError):
            warnings.append("journal_unreadable_or_oversized")
            continue
        for line in raw.splitlines():
            if len(line) > MAX_RECORD_BYTES:
                warnings.append("journal_record_oversized")
                continue
            try:
                value = _object(json.loads(line))
            except (ValueError, UnicodeError, RecursionError):
                warnings.append("journal_record_invalid")
                continue
            identity = value.get("command_id")
            command_log = _safe_filename(value.get("command_log"))
            observed = _timestamp(value.get("observed_at"))
            if (
                not _schema(value.get("schema_version"), 1)
                or value.get("kind") != "cre_command_performance"
                or value.get("run_id") != run_dir.name
                or not isinstance(identity, str)
                or not COMMAND_ID.fullmatch(identity)
                or command_log is None
                or Path(command_log).with_suffix(".performance.jsonl").name != name
                or not _member(value.get("phase"), PHASES)
                or not _member(
                    value.get("event"), {"started", "finished", "interrupted"}
                )
                or not _member(value.get("outcome"), OUTCOMES)
                or observed is None
                or (observed - now).total_seconds() > 300
            ):
                warnings.append("journal_identity_or_schema_invalid")
                continue
            previous = commands.get(identity)
            if previous and previous["command_log"] != command_log:
                warnings.append("journal_invocation_conflict")
                continue
            if len(commands) >= MAX_COMMANDS_PER_SOURCE and identity not in commands:
                warnings.append("command_limit_exceeded")
                break
            if previous and previous["observed_at"] > observed.isoformat():
                continue
            elapsed = _number(value.get("elapsed_seconds"))
            commands[identity] = {
                "run_id": run_dir.name,
                "command_id": identity,
                "command_log": command_log,
                "metrics_file": _safe_filename(value.get("metrics_file")),
                "phase": value["phase"],
                "event": value["event"],
                "outcome": value["outcome"],
                "observed_at": observed.isoformat(),
                "started_at": (
                    observed.isoformat()
                    if value["event"] == "started"
                    else previous.get("started_at")
                    if previous
                    else None
                ),
                "elapsed_seconds": elapsed,
                "configured": _numeric_fields(
                    value,
                    (
                        "configured_concurrency",
                        "configured_page_cap",
                        "configured_max_items",
                    ),
                ),
                "returncode": (
                    value["returncode"]
                    if type(value.get("returncode")) is int
                    and -(2**31) <= value["returncode"] < 2**31
                    else None
                ),
            }
    result = []
    for record in sorted(commands.values(), key=lambda item: item["observed_at"]):
        record["measurement_state"] = (
            "running_or_abruptly_stopped"
            if record["event"] == "started"
            else "start_record_missing"
            if record["started_at"] is None
            else "terminal"
        )
        record["scrape"] = _snapshot(logs, record, now=now)
        if record["phase"] == "collection" and record["scrape"] is None:
            warnings.append("scrape_snapshot_unavailable")
        result.append(record)
    if not journals:
        warnings.append("command_timing_unavailable")
    return result, sorted(set(warnings))


def _cpu(run_dir: Path) -> dict[str, Any]:
    path = _contained_path(run_dir, "logs/host-cpu-guard.jsonl")
    values: list[float] = []
    states: Counter[str] = Counter()
    malformed = 0
    try:
        if path is None:
            raise OSError
        raw = _read_regular_file(path, limit=MAX_CPU_BYTES, tail=True)
    except (OSError, ValueError):
        return {"state": "unavailable", "sample_count": 0}
    for line in raw.splitlines():
        if len(line) > MAX_RECORD_BYTES:
            malformed += 1
            continue
        try:
            record = _object(json.loads(line))
        except (ValueError, UnicodeError, RecursionError):
            malformed += 1
            continue
        value = _number(record.get("host_cpu_percent"))
        if value is not None and value <= 100:
            values.append(value)
        if _member(record.get("state"), {"ok", "high", "tripped"}):
            states[record["state"]] += 1
    values.sort()
    return {
        "state": "observed" if values else "no_valid_samples",
        "coverage": "at_most_last_4MiB_of_guard_log",
        "sample_count": len(values),
        "mean_percent": round(sum(values) / len(values), 2) if values else None,
        "p95_percent": values[math.ceil(len(values) * 0.95) - 1] if values else None,
        "max_percent": values[-1] if values else None,
        "high_samples": states["high"],
        "tripped_records": states["tripped"],
        "invalid_records": malformed,
        "high_samples_are_not_contiguous_duration": True,
    }


def _settings(manifest: Mapping[str, Any], source: str) -> dict[str, Any]:
    config = _object(manifest.get("config"))
    settings = {
        key: _number(config.get(key))
        for key in ("concurrency", "source_workers", "page_cap")
    }
    guard = _object(config.get("host_cpu_guard"))
    settings["host_cpu_guard"] = {
        key: _number(guard.get(key))
        for key in ("max_host_cpu_percent", "sustain_seconds", "sample_seconds")
    }
    source_record = _object(_object(manifest.get("sources")).get(source))
    attempts = source_record.get("attempts")
    if isinstance(attempts, list) and attempts:
        overrides = _object(_object(attempts[-1]).get("freshness_overrides"))
        knobs = {}
        for key, value in overrides.items():
            if (
                isinstance(key, str)
                and re.fullmatch(
                    r"[A-Z_]{1,64}(?:CONCURRENCY|WAIT_MS|TIMEOUT_MS|INTERVAL_MS|COOLDOWN_MS)",
                    key,
                )
                and isinstance(value, str)
                and re.fullmatch(r"[0-9]{1,10}", value)
            ):
                knobs[key] = int(value)
        settings["recorded_source_knobs"] = knobs
    return settings


def _runtime(run_dir: Path, *, now: datetime) -> dict[str, Any]:
    """Project only initial-run resource configuration, never current capacity."""
    try:
        value = _read_json(run_dir / "runtime-performance.json", limit=64 * 1024)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {"state": "unavailable"}
    observed = _timestamp(value.get("observed_at"))
    if (
        not _schema(value.get("schema_version"), 1)
        or value.get("kind") != "cre_runtime_performance"
        or value.get("run_id") != run_dir.name
        or observed is None
        or (observed - now).total_seconds() > 300
    ):
        return {"state": "invalid"}
    result: dict[str, Any] = {
        "state": value.get("availability")
        if _member(value.get("availability"), {"available", "unavailable"})
        else "unknown",
        "observed_at": observed.isoformat(),
        "coverage": "initial_generation_configuration_not_live_usage_or_resume_configuration",
        "hardware": _numeric_fields(
            value.get("hardware"), ("logical_cpu_count", "memory_bytes")
        ),
        "containers": [],
    }
    containers = value.get("containers")
    if not isinstance(containers, list) or len(containers) > 2:
        result["state"] = "invalid"
        return result
    for item in containers:
        record = _object(item)
        if (
            not _member(
                record.get("name"),
                {"firecrawl-api-1", "firecrawl-playwright-service-1"},
            )
            or not isinstance(record.get("id"), str)
            or not re.fullmatch(r"[0-9a-f]{12,64}", record["id"])
            or not isinstance(record.get("image"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", record["image"])
        ):
            result["state"] = "invalid"
            continue
        ports = record.get("ports")
        started = _timestamp(record.get("started_at"))
        projected = {
            "name": record["name"],
            "id": record["id"],
            "image": record["image"],
            "started_at": started.isoformat() if started else None,
            **_numeric_fields(
                record,
                (
                    "cpu_limit",
                    "memory_limit_bytes",
                    "memory_swap_limit_bytes",
                    "pids_limit",
                    "shm_size_bytes",
                ),
            ),
            "ports": [
                port
                for port in ports
                if isinstance(port, str)
                and re.fullmatch(r"[0-9]{1,5}/(?:tcp|udp|sctp)->[0-9]{0,5}", port)
            ]
            if isinstance(ports, list) and len(ports) <= 256
            else [],
        }
        if (
            type(record.get("memory_swap_limit_bytes")) is int
            and record["memory_swap_limit_bytes"] == -1
        ):
            projected["memory_swap_limit_bytes"] = -1
        result["containers"].append(projected)
    return result


def _source_report(
    run_dir: Path, manifest: Mapping[str, Any], source: str, *, now: datetime
) -> dict[str, Any]:
    commands, warnings = _commands(run_dir, now=now)
    checkpoint = _object(_object(manifest.get("sources")).get(source))
    artifact = _object(checkpoint.get("artifact"))
    timing: dict[str, float] = {}
    for command in commands:
        seconds = command["elapsed_seconds"]
        if command["event"] != "started" and seconds is not None:
            phase = command["phase"]
            timing[phase] = round(timing.get(phase, 0.0) + seconds, 3)
    return {
        "source": source,
        "run_id": run_dir.name,
        "status": manifest.get("status")
        if _member(manifest.get("status"), STATUSES)
        else "unknown",
        "source_elapsed_seconds": _duration(
            manifest.get("started_at"), manifest.get("finished_at")
        ),
        "timed_command_seconds": timing,
        "staged_unique": _number(artifact.get("staged_unique")),
        "rejected_by_ingest": _number(artifact.get("rejected_by_ingest")),
        "acquisition_quality_signals": _numeric_fields(
            artifact,
            (
                "flat_listings",
                "detail_errors",
                "detail_unavailable",
                "provisional_identities",
                "inventory_only",
            ),
        ),
        "database_readback_ok": (
            _object(checkpoint.get("readback")).get("ok")
            if type(_object(checkpoint.get("readback")).get("ok")) is bool
            else None
        ),
        "settings": _settings(manifest, source),
        "host_cpu": _cpu(run_dir),
        "runtime_configuration": _runtime(run_dir, now=now),
        "commands": commands,
        "warnings": warnings,
        "coverage": "command_subprocesses_not_all_inline_python_work",
        "field_completeness": "not_established_by_performance_metrics",
    }


def build_report(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if path.is_dir():
        path = path / "manifest.json"
    root = path.parent.resolve()
    manifest = _read_json(path, limit=MAX_MANIFEST_BYTES)
    sha = manifest.get("collector_git_sha")
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ValueError("manifest collector identity is unavailable")
    rows = []
    if "series_id" in manifest:
        if not _schema(manifest.get("schema_version"), 1):
            raise ValueError("unsupported series schema")
        generation = manifest.get("series_id")
        if generation != root.name:
            raise ValueError("series directory identity mismatch")
        for source, checkpoint in _object(manifest.get("sources")).items():
            if source not in SOURCE_TO_BROKERAGE:
                raise ValueError("unrecognized source")
            checkpoint = _object(checkpoint)
            if (
                checkpoint.get("state") == "pending"
                and checkpoint.get("checkpoint_run") is None
                and checkpoint.get("attempts") == []
            ):
                rows.append(
                    {
                        "source": source,
                        "status": "pending",
                        "commands": [],
                        "timed_command_seconds": {},
                        "warnings": [],
                        "coverage": "not_started",
                    }
                )
                continue
            child_root = _contained_path(
                root, _object(checkpoint).get("checkpoint_run")
            )
            try:
                if child_root is None or child_root.parent != (root / "runs").resolve():
                    raise ValueError("child unavailable")
                child = _read_json(
                    child_root / "manifest.json", limit=MAX_MANIFEST_BYTES
                )
                if (
                    not _schema(child.get("schema_version"), 2)
                    or child.get("run_id") != child_root.name
                    or child.get("collector_git_sha") != sha
                    or _object(child.get("config")).get("sources") != [source]
                ):
                    raise ValueError("child identity mismatch")
                rows.append(_source_report(child_root, child, source, now=now))
            except (OSError, ValueError, UnicodeError, RecursionError):
                rows.append(
                    {
                        "source": source,
                        "status": "unknown",
                        "warnings": ["bound_child_evidence_unavailable"],
                        "commands": [],
                        "timed_command_seconds": {},
                    }
                )
    else:
        generation = manifest.get("run_id")
        sources = _object(manifest.get("config")).get("sources")
        if (
            not _schema(manifest.get("schema_version"), 2)
            or not isinstance(sources, list)
            or len(sources) != 1
            or not isinstance(sources[0], str)
            or sources[0] not in SOURCE_TO_BROKERAGE
            or generation != root.name
        ):
            raise ValueError("expected a single-source checkpoint generation")
        rows.append(_source_report(root, manifest, sources[0], now=now))
    if not isinstance(generation, str) or not RUN_ID.fullmatch(generation):
        raise ValueError("invalid generation identity")
    return {
        "schema_version": 1,
        "kind": "cre_performance_report",
        "generation_id": generation,
        "collector_git_sha": sha,
        "computed_at": now.isoformat(),
        "sources": rows,
        "completeness_is_not_established_by_performance_metrics": True,
    }


def render(report: Mapping[str, Any]) -> str:
    lines = [
        f"CRE performance  {report['generation_id']}",
        f"Collector SHA    {report['collector_git_sha']}",
        "",
        "Source                Staged   Timed commands   Slowest measured command phase",
    ]
    pending = 0
    for row in report["sources"]:
        if row.get("coverage") == "not_started":
            pending += 1
            continue
        timings = row["timed_command_seconds"]
        slowest = max(timings, key=timings.get) if timings else None
        staged = row.get("staged_unique")
        timed = f"{sum(timings.values()):.1f}s" if timings else "unknown"
        lines.append(
            f"{row['source']:<21} {int(staged) if staged is not None else '?':>6}"
            f"   {timed:>11}   "
            + (f"{slowest}: {timings[slowest]:.1f}s" if slowest else "unknown")
        )
        collection = [item for item in row["commands"] if item.get("scrape")]
        if collection:
            scrape = collection[-1]["scrape"]
            requests = scrape["requests"]
            lines.append(
                f"  Latest scrape snapshot: {scrape['state']}; "
                f"successful attempts/s={scrape['successful_client_attempts_per_second'] if scrape['successful_client_attempts_per_second'] is not None else '?'}; "
                f"p95 upper-bound ms={requests['approximate_p95_ms'] if requests['approximate_p95_ms'] is not None else '?'}; "
                f"max locally awaited={requests['max_active_locally_awaited'] if requests['max_active_locally_awaited'] is not None else '?'}"
            )
        if row.get("warnings"):
            lines.append("  Evidence: " + ", ".join(row["warnings"]))
    if pending:
        lines.extend(
            [
                "",
                f"{pending} pending sources have not started; no performance measurements yet.",
            ]
        )
    lines.extend(
        [
            "",
            "Staged rows are not proof of ingestion. Request success is not listing completion.",
            "Latency is approximate; queue wait and direct-provider/cache coverage may be unknown.",
            "CPU is from bounded guard evidence; Node RSS is sampled, not a true peak.",
            "Use --json for command IDs, settings, retries/cache counters and evidence gaps.",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "run", nargs="?", help="series/checkpoint directory; default newest series"
    )
    parser.add_argument(
        "--json", action="store_true", help="print a structured one-shot report"
    )
    args = parser.parse_args(argv)
    try:
        path = Path(args.run).expanduser() if args.run else resolve_series_path(None)
        report = build_report(path)
    except (OSError, ValueError, UnicodeError, TypeError, RecursionError):
        print(
            "Performance evidence is unavailable or invalid; no database was queried."
        )
        return 2
    print(json.dumps(report, sort_keys=True) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
