#!/usr/bin/env python3
"""Read bounded CRE performance evidence without network or database access."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

from cre_ingest import SOURCE_TO_BROKERAGE
from cre_performance_compare import compare_reports, source_stage_timing
from cre_run_insights import build_trial_report, overlap, resource_report
from cre_series_status import (
    CHILD_STATES,
    _contained_path,
    _read_regular_file,
    resolve_series_path,
)

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_JOURNAL_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 4096
MAX_SNAPSHOT_BYTES = 128 * 1024
MAX_CPU_BYTES = 4 * 1024 * 1024
MAX_FILES_PER_SOURCE = 1024
MAX_TOTAL_JOURNAL_BYTES = 16 * 1024 * 1024
MAX_VALIDATION_UNITS = 1024
MAX_TOTAL_VALIDATION_BYTES = 16 * 1024 * 1024
MAX_LOG_DIRECTORY_ENTRIES = 4096
MAX_COMMANDS_PER_SOURCE = 2048
RUN_ID = re.compile(r"\d{4}-\d{2}-\d{2}T\d{6}Z(?:-[0-9a-f]{12})?\Z")
COMMAND_ID = re.compile(r"[0-9a-f]{32}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
SAFE_FILE = re.compile(r"[a-zA-Z0-9_.-]{1,200}\Z")
DIRECT_PROVIDER_ROLES = {"current_archive", "closed_archive", "sitemap", "detail"}
DIRECT_PROVIDER_ERRORS = (
    "timeout",
    "http_4xx",
    "http_5xx",
    "transport",
    "empty_response",
    "response_too_large",
    "unknown",
)
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
    "artifact_validation",
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
    "additive_scope_complete_current_inventory",
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


def _direct_provider_metrics(value: object) -> list[dict[str, Any]]:
    records = _object(value).get("by_source_transaction_role")
    if not isinstance(records, list) or len(records) > 12:
        return []
    result = []
    seen: set[tuple[str, str, str]] = set()
    counters = (
        "attempts_started",
        "attempts_completed",
        "succeeded",
        "failed",
        "active_locally_awaited",
        "max_active_locally_awaited",
        "summed_latency_ms",
        "other_valid_statuses",
        "pacing_waits_started",
        "pacing_waits_completed",
        "pacing_waits_active",
        "pacing_wait_ms_scheduled",
        "pacing_wait_ms_elapsed",
    )
    for item in records:
        record = _object(item)
        source, transaction, role = (
            record.get("source"),
            record.get("transaction"),
            record.get("role"),
        )
        if (
            source != "essex-realty"
            or not isinstance(transaction, str)
            or transaction not in {"sale", "lease", "unknown"}
            or not isinstance(role, str)
            or role not in DIRECT_PROVIDER_ROLES
        ):
            continue
        key = (source, transaction, role)
        if key in seen:
            continue
        seen.add(key)
        statuses = _object(record.get("status_counts"))
        error_categories = _object(record.get("error_categories"))
        histogram = _object(record.get("latency_histogram"))
        bounds, counts = histogram.get("bounds_ms"), histogram.get("counts")
        clean_histogram = None
        if (
            isinstance(bounds, list)
            and 0 < len(bounds) <= 32
            and isinstance(counts, list)
            and len(counts) == len(bounds) + 1
            and all(_number(entry) is not None for entry in [*bounds, *counts])
            and all(left < right for left, right in pairwise(bounds))
        ):
            clean_histogram = {"bounds_ms": bounds, "counts": counts}
        result.append(
            {
                "source": source,
                "transaction": transaction,
                "role": role,
                **_numeric_fields(record, counters),
                "latency_histogram": clean_histogram,
                "error_categories": _numeric_fields(
                    error_categories, DIRECT_PROVIDER_ERRORS
                ),
                "status_counts": {
                    code: _number(count)
                    for code, count in list(statuses.items())[:32]
                    if isinstance(code, str) and re.fullmatch(r"[1-5][0-9]{2}", code)
                },
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
    direct_provider_requests = _direct_provider_metrics(
        metrics.get("direct_provider_requests")
    )
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
        "direct_provider_requests": direct_provider_requests,
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
            "direct_provider_requests": (
                "essex_only_other_direct_sources_unknown"
                if direct_provider_requests
                else "unknown_uninstrumented"
            ),
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
    journal_bytes = 0
    for name in sorted(journals):
        remaining = MAX_TOTAL_JOURNAL_BYTES - journal_bytes
        if remaining <= 0:
            warnings.append("journal_total_byte_limit_exceeded")
            break
        try:
            raw = _read_regular_file(
                logs / name, limit=min(MAX_JOURNAL_BYTES, remaining)
            )
        except (OSError, ValueError):
            warnings.append("journal_unreadable_or_oversized")
            continue
        journal_bytes += len(raw)
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
                "source": value.get("source")
                if isinstance(value.get("source"), str)
                and value["source"] in SOURCE_TO_BROKERAGE
                else None,
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
    settings["pipeline"] = config.get("pipeline") is True
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
        project, service, name = (
            record.get("project"),
            record.get("service"),
            record.get("name"),
        )
        legacy_name = (
            _member(name, {"firecrawl-api-1", "firecrawl-playwright-service-1"})
            and project is None
            and service is None
        )
        bound_name = (
            isinstance(project, str)
            and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", project) is not None
            and project == value.get("project")
            and service in ("api", "playwright-service")
            and isinstance(name, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,191}", name) is not None
            and type(record.get("running")) is bool
        )
        if (
            not (legacy_name or bound_name)
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
        if bound_name:
            projected.update(
                project=project, service=service, running=record["running"]
            )
        if (
            type(record.get("memory_swap_limit_bytes")) is int
            and record["memory_swap_limit_bytes"] == -1
        ):
            projected["memory_swap_limit_bytes"] = -1
        result["containers"].append(projected)
    return result


def _validation_timings(run_dir: Path, manifest: Mapping[str, Any]) -> list[dict]:
    """Read numeric query timing projections, never expose SQL/results/env paths."""
    from cre_validate import QUERIES

    allowed = set(QUERIES) | {"artifact_run_jobs", "historical_primary_child_conflicts"}
    pipeline_mode = _object(manifest.get("config")).get("pipeline") is True
    files = []
    before = _object(manifest.get("preflight")).get("validation_path")
    if isinstance(before, str) or not pipeline_mode:
        files.append(("pre_validation", before or "pre-validation.json", None, False))
    if not pipeline_mode:
        after = _object(manifest.get("validation")).get("path")
        files.append(("post_validation", after or "validation.json", None, False))
    units = _object(manifest.get("pipeline")).get("units")
    unit_budget_exhausted = (
        isinstance(units, list) and len(units) > MAX_VALIDATION_UNITS
    )
    if isinstance(units, list):
        for unit in units[:MAX_VALIDATION_UNITS]:
            unit = _object(unit)
            name = unit.get("unit_id", unit.get("id"))
            if isinstance(name, str) and re.fullmatch(r"unit-[0-9]{4,6}", name):
                for stage, path_key, hash_key in (
                    (name + "-before", "before_path", "before_sha256"),
                    (name, "validation_path", "validation_sha256"),
                ):
                    relative = unit.get(path_key)
                    if (
                        path_key == "before_path"
                        and unit.get("before_reused") is True
                        and unit.get("before_reuse_kind") == "initial_preflight"
                    ):
                        relative = unit.get("before_source_path")
                    if isinstance(relative, str):
                        files.append((stage, relative, unit.get(hash_key), True))
    result = []
    if unit_budget_exhausted:
        result.append(
            {
                "run_id": run_dir.name,
                "stage": "pipeline",
                "queries": {},
                "unavailable_code": "unit_timing_budget_exhausted",
            }
        )
    bound_files = {}
    for stage, relative, expected_hash, required in files:
        path = _contained_path(run_dir, relative)
        if path is None:
            continue
        previous = bound_files.get(path)
        if previous is None or (required and not previous[2]):
            bound_files[path] = (stage, expected_hash, required)
        elif required and previous[2] and previous[1] != expected_hash:
            bound_files[path] = (stage, None, True)
    validation_bytes = 0
    for path, (stage, expected_hash, required) in bound_files.items():
        remaining = MAX_TOTAL_VALIDATION_BYTES - validation_bytes
        if remaining <= 0:
            result.append(
                {
                    "run_id": run_dir.name,
                    "stage": "pipeline",
                    "queries": {},
                    "unavailable_code": "query_timing_byte_budget_exhausted",
                }
            )
            break
        try:
            raw = _read_regular_file(path, limit=min(MAX_MANIFEST_BYTES, remaining))
            validation_bytes += len(raw)
            if required and (
                not isinstance(expected_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
                or hashlib.sha256(raw).hexdigest() != expected_hash
            ):
                raise ValueError("invalid report binding")
            data = _object(json.loads(raw))
            if pipeline_mode:
                observed = _timestamp(data.get("generated_at"))
                start, end = (
                    _timestamp(manifest.get("started_at")),
                    _timestamp(manifest.get("finished_at"))
                    or _timestamp(manifest.get("updated_at"))
                    or datetime.now(timezone.utc),
                )
                if (
                    observed is None
                    or start is None
                    or end is None
                    or not start <= observed <= end
                ):
                    raise ValueError("invalid report generation")
        except (OSError, ValueError, UnicodeError, RecursionError):
            if required:
                result.append(
                    {
                        "run_id": run_dir.name,
                        "stage": stage,
                        "queries": {},
                        "unavailable_code": "report_binding_invalid",
                    }
                )
            continue
        timings = _object(data.get("query_timings"))
        result.append(
            {
                "run_id": run_dir.name,
                "stage": stage,
                "validation_profile": data.get("validation_profile")
                if data.get("validation_profile") in {"full", "checkpoint-preflight"}
                else "unknown",
                "queries": {
                    name: {
                        "status": "available"
                        if _object(timings[name]).get("status") == "available"
                        and _number(_object(timings[name]).get("elapsed_ms"))
                        is not None
                        else "unavailable",
                        "elapsed_ms": _number(_object(timings[name]).get("elapsed_ms"))
                        if _object(timings[name]).get("status") == "available"
                        else None,
                    }
                    for name in sorted(allowed & timings.keys())
                },
            }
        )
    return result


def _source_report(
    run_dir: Path,
    manifest: Mapping[str, Any],
    source: str,
    *,
    now: datetime,
    verified_current_inventory: bool = False,
    command_evidence: tuple | None = None,
) -> dict[str, Any]:
    commands, warnings = (
        command_evidence
        if command_evidence is not None
        else _commands(run_dir, now=now)
    )
    multi_source = len(_object(manifest.get("config")).get("sources", [])) > 1
    if multi_source:
        commands = [command for command in commands if command["source"] == source]
    checkpoint = _object(_object(manifest.get("sources")).get(source))
    artifact = _object(checkpoint.get("artifact"))
    stages = source_stage_timing(
        _object(_object(manifest.get("pipeline")).get("source_timings")).get(source)
    )
    timing: dict[str, float] = {}
    for command in commands:
        seconds = command["elapsed_seconds"]
        if command["event"] != "started" and seconds is not None:
            phase = command["phase"]
            timing[phase] = round(timing.get(phase, 0.0) + seconds, 3)
    source_elapsed = (
        stages["source_elapsed_seconds"]
        if multi_source or _object(manifest.get("config")).get("pipeline") is True
        else _duration(manifest.get("started_at"), manifest.get("finished_at"))
    )
    verified_count = (
        artifact.get("staged_unique")
        if verified_current_inventory
        and type(artifact.get("staged_unique")) is int
        and artifact["staged_unique"] >= 0
        else None
    )
    return {
        "source": source,
        "run_id": run_dir.name,
        "status": (
            checkpoint.get("state")
            if _member(checkpoint.get("state"), CHILD_STATES)
            else "unknown"
        )
        if multi_source
        else (
            manifest.get("status")
            if _member(manifest.get("status"), STATUSES)
            else "unknown"
        ),
        "stage_wall_seconds": stages,
        "source_elapsed_seconds": source_elapsed,
        "timed_command_seconds": timing,
        "collection_seconds": timing.get("collection"),
        "raw_emitted_per_minute": round(
            artifact["flat_listings"] * 60 / timing["collection"], 3
        )
        if _number(artifact.get("flat_listings")) is not None
        and timing.get("collection", 0) > 0
        else None,
        "staged_unique_per_minute": round(
            artifact["staged_unique"] * 60 / timing["collection"], 3
        )
        if _number(artifact.get("staged_unique")) is not None
        and timing.get("collection", 0) > 0
        else None,
        "staged_unique": _number(artifact.get("staged_unique")),
        "verified_current_inventory_unique": verified_count,
        "verified_current_inventory_per_minute": round(
            verified_count * 60 / source_elapsed, 3
        )
        if verified_count is not None
        and source_elapsed is not None
        and source_elapsed > 0
        else None,
        "verified_current_inventory_basis": "pipeline_source_unit_full_readback"
        if verified_current_inventory
        else None,
        "raw_emitted_rows": _number(artifact.get("flat_listings")),
        "fresh_qualified_unique": None,
        "ingested_rows": None,
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
        "quality_no_regression": True
        if _object(
            _object(_object(manifest.get("pipeline")).get("source_results")).get(source)
        ).get("state")
        == "verified"
        else _object(manifest.get("validation")).get("quality_no_regression")
        if type(_object(manifest.get("validation")).get("quality_no_regression"))
        is bool
        else None,
        "settings": _settings(manifest, source),
        "host_cpu": {"scope": "shared_coordinator", "state": "see_run_resources"}
        if multi_source
        else _cpu(run_dir),
        "runtime_configuration": {
            "scope": "shared_coordinator",
            "state": "see_run_resources",
        }
        if multi_source
        else _runtime(run_dir, now=now),
        "resources": {"scope": "shared_coordinator", "state": "see_run_resources"}
        if multi_source
        else resource_report(run_dir, now=now),
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
    pipeline_unit_caches = {}
    bound_runs = {}

    def source_row(run_dir, child, source, **kwargs):
        if run_dir not in bound_runs:
            bound_runs[run_dir] = (child, _commands(run_dir, now=now))
        return _source_report(
            run_dir,
            child,
            source,
            now=now,
            command_evidence=bound_runs[run_dir][1],
            **kwargs,
        )

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
                child = (
                    bound_runs[child_root][0]
                    if child_root in bound_runs
                    else _read_json(
                        child_root / "manifest.json", limit=MAX_MANIFEST_BYTES
                    )
                )
                expected_sources = (
                    _object(manifest.get("config")).get("sources")
                    if _object(manifest.get("config")).get("pipeline") is True
                    else [source]
                )
                if (
                    not _schema(child.get("schema_version"), 2)
                    or child.get("run_id") != child_root.name
                    or child.get("collector_git_sha") != sha
                    or _object(child.get("config")).get("sources") != expected_sources
                    or (
                        _object(manifest.get("config")).get("pipeline") is True
                        and _object(child.get("config")).get("pipeline") is not True
                    )
                ):
                    raise ValueError("child identity mismatch")
                verified_current_inventory = False
                parent_target = manifest.get("database_target")
                child_target = _object(child.get("preflight")).get("database_target")
                if (
                    _object(manifest.get("config")).get("pipeline") is True
                    and _object(child.get("config")).get("pipeline") is True
                    and _object(checkpoint).get("checkpoint_run")
                    == f"runs/{child_root.name}"
                    and isinstance(parent_target, Mapping)
                    and bool(parent_target)
                    and child_target == parent_target
                ):
                    from cre_checkpoint_series import pipeline_source_qualified

                    attempts = _object(checkpoint).get("attempts")
                    parent_finished_at = manifest.get("finished_at") or now.isoformat()
                    if isinstance(attempts, list) and attempts:
                        cache = pipeline_unit_caches.setdefault(child_root, {})
                        verified_current_inventory = pipeline_source_qualified(
                            child,
                            source,
                            run_dir=child_root,
                            parent_started_at=manifest.get("started_at"),
                            parent_finished_at=parent_finished_at,
                            unit_cache=cache,
                            parent_attempts=attempts,
                        )
                rows.append(
                    source_row(
                        child_root,
                        child,
                        source,
                        verified_current_inventory=verified_current_inventory,
                    )
                )
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
            or not 1 <= len(sources) <= len(SOURCE_TO_BROKERAGE)
            or any(
                not isinstance(source, str) or source not in SOURCE_TO_BROKERAGE
                for source in sources
            )
            or len(set(sources)) != len(sources)
            or generation != root.name
        ):
            raise ValueError("expected a checkpoint generation")
        rows.extend(source_row(root, manifest, source) for source in sources)
    if not isinstance(generation, str) or not RUN_ID.fullmatch(generation):
        raise ValueError("invalid generation identity")
    collection_intervals = []
    for row in rows:
        for command in row.get("commands", []):
            start, end = (
                _timestamp(command.get("started_at")),
                _timestamp(command.get("observed_at")),
            )
            if (
                command.get("phase") == "collection"
                and command.get("event") in {"finished", "interrupted"}
                and start is not None
                and end is not None
                and end >= start
            ):
                collection_intervals.append((start.timestamp(), end.timestamp()))
    collection_overlap = overlap(collection_intervals)
    collection_overlap["basis"] = (
        "terminal_collection_subprocess_wall_timestamps_not_request_or_server_concurrency"
    )
    return {
        "schema_version": 1,
        "kind": "cre_performance_report",
        "resource_coverage": "current_bound_checkpoint_per_source_prior_failed_or_recovery_attempts_not_aggregated"
        if "series_id" in manifest
        else "current_checkpoint_sessions",
        "generation_id": generation,
        "collector_git_sha": sha,
        "run_status": manifest.get("status")
        if _member(manifest.get("status"), STATUSES)
        else "unknown",
        "run_scope": {
            "transactions": _object(manifest.get("config")).get("transactions")
            if _object(manifest.get("config")).get("transactions")
            in (["sale"], ["lease"], ["sale", "lease"])
            else None,
            "max_items": _number(_object(manifest.get("config")).get("max_items")),
            "pipeline": _object(manifest.get("config")).get("pipeline") is True,
            "freshness_policy": "checkpoint_generation_bound",
        },
        "computed_at": now.isoformat(),
        "sources": rows,
        "run_elapsed_seconds": _duration(
            manifest.get("started_at"), manifest.get("finished_at")
        ),
        "resources": resource_report(root, now=now),
        "collection_overlap": collection_overlap,
        "shared_commands": [
            command
            for _, evidence in bound_runs.values()
            for command in evidence[0]
            if command["source"] is None
        ],
        "validation_query_timings": [
            item
            for run_dir, (child, _) in bound_runs.items()
            for item in _validation_timings(run_dir, child)
        ],
        "checkpoint_resources": [
            {
                "run_id": run_dir.name,
                "runtime_configuration": _runtime(run_dir, now=now),
                "resources": resource_report(run_dir, now=now),
            }
            for run_dir in bound_runs
        ]
        if "series_id" in manifest
        and _object(manifest.get("config")).get("pipeline") is True
        else [],
        "shared_host_cpu": _cpu(root) if "series_id" not in manifest else None,
        "shared_runtime_configuration": _runtime(root, now=now)
        if "series_id" not in manifest
        else None,
        "comparison": {
            "state": "unavailable",
            "speedup_percent": None,
            "reason": "no_matched_quality_valid_baseline",
        },
        "completeness_is_not_established_by_performance_metrics": True,
    }


def render(report: Mapping[str, Any]) -> str:
    if report.get("kind") == "cre_saved_trial_performance_report":
        return _render_trial(report)
    lines = [
        f"CRE performance  {report['generation_id']}",
        f"Collector SHA    {report['collector_git_sha']}",
        f"Run elapsed     {_display(report.get('run_elapsed_seconds'))}s (wall time; overlapping commands are not summed)",
        "",
        "Source                Status                     Raw  Staged  Verified-current  Rejected  Readback   Timed commands",
    ]
    pending = 0
    if report.get("resource_coverage", "").startswith("current_bound_checkpoint"):
        lines.append(
            "Coverage: current bound checkpoint per source; prior failed/recovery attempts are not aggregated. No full-series resource total is claimed."
        )
    for row in report["sources"]:
        if row.get("coverage") == "not_started":
            pending += 1
            continue
        timings = row["timed_command_seconds"]
        staged = row.get("staged_unique")
        timed = f"{sum(timings.values()):.1f}s" if timings else "unknown"
        lines.append(
            f"{row['source']:<21} {row.get('status', 'unknown'):<26} "
            f"{_display(row.get('raw_emitted_rows')):>4} {_display(staged):>7} "
            f"{_display(row.get('verified_current_inventory_unique')):>16} "
            f"{_display(row.get('rejected_by_ingest')):>9} {_display(row.get('database_readback_ok')):>9} {timed:>15}"
        )
        verified_rate = row.get("verified_current_inventory_per_minute")
        if row.get("verified_current_inventory_unique") is not None:
            lines.append(
                "  Verified current staged inventory: "
                f"{row['verified_current_inventory_unique']} unique identities; "
                "collection-to-verified rate="
                f"{_display(verified_rate)}/min. This is not a new-row or strict-detail count."
            )
        if timings:
            lines.append(
                "  Command steps: "
                + ", ".join(
                    f"{name}={seconds:.3f}s"
                    for name, seconds in sorted(timings.items())
                )
            )
        stage = _object(row.get("stage_wall_seconds"))
        if stage.get("awaiting_validation_seconds") is not None:
            lines.append(
                f"  Dispatch wait={_display(stage.get('dispatch_wait_seconds'))}s; awaiting validation={stage['awaiting_validation_seconds']}s; collection-to-verified={_display(stage.get('source_elapsed_seconds'))}s."
            )
        collection = [item for item in row["commands"] if item.get("scrape")]
        if collection:
            scrape = collection[-1]["scrape"]
            requests = scrape["requests"]
            lines.append(
                f"  Latest scrape snapshot: {scrape['state']}; "
                f"successful attempts/s={scrape['successful_client_attempts_per_second'] if scrape['successful_client_attempts_per_second'] is not None else '?'}; "
                f"per-command histogram p95 upper-bound ms={requests['approximate_p95_ms'] if requests['approximate_p95_ms'] is not None else '?'}; "
                f"max locally awaited={requests['max_active_locally_awaited'] if requests['max_active_locally_awaited'] is not None else '?'}"
            )
            for direct in scrape.get("direct_provider_requests", []):
                lines.append(
                    f"  Essex direct {direct['transaction']}/{direct['role']}: "
                    f"started={_display(direct['attempts_started'])}, "
                    f"completed={_display(direct['attempts_completed'])}, "
                    f"failed={_display(direct['failed'])}, "
                    f"active={_display(direct['active_locally_awaited'])}, "
                    f"latency sum={_display(direct['summed_latency_ms'])}ms; "
                    f"pacing scheduled/elapsed="
                    f"{_display(direct['pacing_wait_ms_scheduled'])}/"
                    f"{_display(direct['pacing_wait_ms_elapsed'])}ms; "
                    f"HTTP statuses={direct['status_counts']}"
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
    measured_overlap = _object(report.get("collection_overlap"))
    if measured_overlap.get("peak_overlapping_source_collections") is not None:
        lines.append(
            "Collection subprocess overlap: peak="
            + _display(measured_overlap["peak_overlapping_source_collections"])
            + "; seconds by active subprocesses="
            + ", ".join(
                f"{width}={seconds}s"
                for width, seconds in measured_overlap.get(
                    "seconds_by_active_sources", {}
                ).items()
            )
            + ". This is subprocess wall-time overlap, not request concurrency."
        )
    lines.extend(_render_resources(_object(report.get("resources"))))
    for item in report.get("validation_query_timings", []):
        queries = _object(item.get("queries"))
        measured = [
            (name, row["elapsed_ms"])
            for name, row in queries.items()
            if row.get("elapsed_ms") is not None
        ]
        if measured:
            slowest = sorted(measured, key=lambda pair: pair[1], reverse=True)[:3]
            lines.append(
                f"Validation {item['stage']} slowest client queries: "
                + ", ".join(
                    f"{name}={milliseconds:.3f}ms" for name, milliseconds in slowest
                )
                + "; all measured families in JSON."
            )
    if _object(report.get("resources")).get("state") != "observed":
        for row in report["sources"]:
            if _object(row.get("resources")).get("state") == "observed":
                lines.append(f"Checkpoint resource evidence for {row['source']}:")
                lines.extend(_render_resources(row["resources"]))
    lines.extend(
        [
            "",
            "Staged rows are not proof of ingestion. Request success is not listing completion.",
            "Firecrawl and direct-provider requests are separate; only Essex direct requests are instrumented, and other providers remain unknown.",
            "CPU is from bounded guard evidence; Node RSS is sampled, not a true peak.",
            "Use --json for command IDs, settings, retries/cache counters and evidence gaps.",
            "No matched quality-valid baseline: speedup is unknown. Shared coordinator metrics are not source costs."
            if _object(report.get("comparison")).get("state") == "unavailable"
            else "Saved-run comparison is observational; code, provider, cache and host changes can affect results.",
        ]
    )
    comparison = _object(report.get("comparison"))
    if comparison.get("state") != "unavailable":
        elapsed = _object(comparison.get("elapsed"))
        lines.extend(
            [
                "",
                f"Baseline generation {comparison.get('baseline_generation_id')} / SHA {comparison.get('baseline_collector_git_sha')}",
                f"Run wall: {_display(elapsed.get('baseline_seconds'))}s -> {_display(elapsed.get('current_seconds'))}s; matched-work elapsed reduction={_display(elapsed.get('reduction_percent'))}%.",
            ]
        )
        for item in comparison.get("sources", []):
            duration, rate = item["elapsed_seconds"], item["staged_per_minute"]
            verified_rate = item["verified_current_inventory_per_minute"]
            lines.append(
                f"  {item['source']}: elapsed {_display(duration['baseline'])}s -> {_display(duration['current'])}s; staged/min {_display(rate['baseline'])} -> {_display(rate['current'])}; verified-current/min {_display(verified_rate['baseline'])} -> {_display(verified_rate['current'])}; recorded readbacks={item['readbacks_recorded_ok']}."
            )
        if comparison.get("context_differences"):
            lines.append(
                "Context differences: " + ", ".join(comparison["context_differences"])
            )
    return "\n".join(lines)


def _display(value: object) -> str:
    return "unknown" if value is None else str(value)


def _gib(value: object) -> str:
    numeric = _number(value)
    return f"{numeric / 1024**3:.2f} GiB" if numeric is not None else "unknown"


def _cpu_line(label: str, summary: Mapping[str, Any]) -> str:
    return f"  {label}: mean/p50/p95={_display(summary.get('mean'))}/{_display(summary.get('p50'))}/{_display(summary.get('p95'))}%; sampled peak={_display(summary.get('sampled_max'))}%; duration-weighted estimate={_display(summary.get('duration_weighted_estimate'))}%; samples={_display(summary.get('sample_count'))}, window={_display(summary.get('window_seconds'))}s, covered={_display(summary.get('covered_seconds'))}s, gaps={_display(summary.get('uncovered_gap_seconds'))}s."


def _render_resources(resources: Mapping[str, Any]) -> list[str]:
    lines = ["", "Shared resources (sampled; no inferred speedup)"]
    if resources.get("state") != "observed":
        return lines + ["  Resource usage unknown: saved telemetry unavailable."]
    sessions = resources.get("sessions", [])
    groups = []
    for index, session in enumerate(sessions):
        label = f"session {index + 1}"
        metrics = _object(session.get("metrics"))
        groups.append((label, metrics))
        lines.append(
            f"  {label}: {session['sample_count']} observations; memory pressure samples={session.get('memory_pressure_samples', {})}; runtime availability samples={session.get('runtime_availability_samples', {})}; runtime gap codes={session.get('runtime_gap_code_samples', {})}."
        )
        lines.append(
            f"  Host RAM={_gib(_object(metrics.get('memory_total_bytes')).get('last_observed'))}; Docker VM capacity={_gib(_object(metrics.get('docker_vm_memory_total_bytes')).get('last_observed'))}; compressed memory sampled peak={_gib(_object(metrics.get('vm_compressor_bytes')).get('sampled_max'))}."
        )
        swap = _object(metrics.get("swap_used_bytes"))
        first, last = (
            _number(swap.get("first_observed")),
            _number(swap.get("last_observed")),
        )
        change = (
            f"{(last - first) / 1024**3:+.2f} GiB"
            if first is not None and last is not None
            else "unknown"
        )
        lines.append(
            f"  Swap used {_gib(first)} -> {_gib(last)} (observed change {change}; not attributed to this run); owned-process RSS sampled peak={_gib(_object(metrics.get('owned_processes_rss_bytes')).get('sampled_max'))}."
        )
        for container in session.get("containers", []):
            restarts = _object(_object(container.get("metrics")).get("restart_count"))
            lines.append(
                f"  {label} {container['role']}: OOMKilled true snapshots={_display(container.get('oom_killed_true_snapshots'))} of {_display(container.get('oom_killed_observed_bool_snapshots'))} observed bool fields; restart counter {_display(restarts.get('first_observed'))} -> {_display(restarts.get('last_observed'))} (container-lifetime snapshots, not run-caused event counts)."
            )
            groups.append(
                (
                    f"session {index + 1} {container['role']}",
                    _object(container.get("metrics")),
                )
            )
    if not sessions:
        groups.append(("trial", _object(resources.get("metrics"))))
    for label, metrics in groups:
        for key in (
            "cpu_percent",
            "host_cpu_percent",
            "browser_cpu_percent",
            "api_cpu_percent",
            "cpu_percent_of_verified_quota",
        ):
            if key in metrics:
                basis = (
                    "total-machine CPU"
                    if key == "host_cpu_percent"
                    or (
                        key == "cpu_percent"
                        and not label.endswith((" api", " browser"))
                    )
                    else "verified-quota utilization"
                    if key == "cpu_percent_of_verified_quota"
                    else "Docker CPU (100%=one core)"
                )
                role = (
                    key.split("_", 1)[0]
                    if key in ("browser_cpu_percent", "api_cpu_percent")
                    else ""
                )
                lines.append(
                    _cpu_line(
                        f"{label} {role + ' ' if role else ''}{basis}", metrics[key]
                    )
                )
        for prefix in ("", "browser_", "api_"):
            memory = _object(metrics.get(prefix + "memory_current_bytes"))
            limit = _object(metrics.get(prefix + "memory_limit_bytes"))
            utilization = _object(metrics.get(prefix + "memory_percent_of_limit"))
            if memory or limit:
                lines.append(
                    f"  {label} {prefix}memory sampled peak={_gib(memory.get('sampled_max'))} / observed limit={_gib(limit.get('last_observed'))}; mean/p50/p95/peak limit utilization={_display(utilization.get('mean'))}/{_display(utilization.get('p50'))}/{_display(utilization.get('p95'))}/{_display(utilization.get('sampled_max'))}%; PID sampled peak={_display(_object(metrics.get(prefix + 'pids')).get('sampled_max'))}."
                )
        pages = _object(metrics.get("browser_active_pages"))
        if pages:
            lines.append(
                f"  Browser pages: duration-weighted estimate={_display(pages.get('duration_weighted_estimate'))}; sampled peak={_display(pages.get('sampled_max'))}; observed capacity={_display(_object(metrics.get('browser_page_capacity')).get('last_observed'))}. Queue active/waiting sampled peaks={_display(_object(metrics.get('api_active_jobs')).get('sampled_max'))}/{_display(_object(metrics.get('api_waiting_jobs')).get('sampled_max'))}."
            )
    lines.append(
        "  Duration weighting covers measured intervals within each session, excludes long gaps and does not span resume downtime."
    )
    lines.append(
        "  Host CPU is total-machine 0–100%; Docker CPU uses 100%=one core; quota utilization is explicit. Owned-process CPU is a ps lifetime average. Sampled headroom does not establish a bottleneck."
    )
    return lines


def _render_trial(report: Mapping[str, Any]) -> str:
    lines = [
        "CRE saved trial performance",
        f"Recorded collector SHA {report['collector_git_sha']}",
        f"Trial wall (includes bootstrap/polling/settlement): {_display(report.get('trial_wall_seconds'))}s",
        "",
        "Source                Quality              Raw  Staged  Collection s  Poll-observed process s",
    ]
    for row in report["sources"]:
        lines.append(
            f"{row['source']:<21} {row['status']:<20} {_display(row.get('raw_emitted_rows')):>4} {_display(row.get('staged_unique')):>7} {_display(row.get('collection_seconds')):>13} {_display(row.get('observed_process_seconds')):>24}"
        )
        if row.get("status") == "failed":
            lines.append(
                f"  Collector rc={_display(row.get('collector_returncode'))}; dry-run rc={_display(row.get('dry_run_returncode'))}; detail errors={_display(row.get('detail_errors'))}; failed transactions={','.join(row.get('failed_transactions', [])) or 'none recorded'}."
            )
    shared = report["overlap"]
    lines.append(
        f"Collection overlap: peak={_display(shared.get('peak_overlapping_source_collections'))}; window={_display(shared.get('collection_window_seconds'))}s; summed source time={_display(shared.get('summed_source_collection_seconds'))}s (not trial wall)."
    )
    lines.append(
        "Observed collection overlap seconds by active sources: "
        + ", ".join(
            f"{width}={seconds}s"
            for width, seconds in shared.get("seconds_by_active_sources", {}).items()
        )
    )
    lines.extend(_render_resources(report["resources"]))
    lines.extend(
        [
            "",
            "Historical host memory, request latency and verified CPU quotas: unknown.",
            "Collector exit 0 is not quality proof. Raw/staged rows are not proof of ingestion.",
            "No matched quality-valid baseline: speedup is unknown.",
        ]
    )
    if report.get("warnings"):
        lines.append("Evidence: " + ", ".join(report["warnings"]))
    return "\n".join(lines)


def write_report(report: Mapping[str, Any], out_dir: Path) -> None:
    """Atomically publish only the bounded, sanitized projection."""
    if any(parent.is_symlink() for parent in (out_dir, *out_dir.parents)):
        raise ValueError("report directory alias rejected")
    out_dir.mkdir(mode=0o700, exist_ok=True)
    if not out_dir.is_dir():
        raise ValueError("report directory unavailable")
    for name, payload in (
        (
            "performance-report.json",
            json.dumps(report, sort_keys=True, allow_nan=False) + "\n",
        ),
        ("performance-report.md", render(report) + "\n"),
    ):
        data = payload.encode()
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("report exceeds size bound")
        descriptor, temporary = tempfile.mkstemp(
            prefix=".performance-report-", dir=out_dir
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, out_dir / name)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def save_report(run_dir: Path) -> bool:
    """Best-effort refresh finalizer; reporting never changes acquisition outcome."""
    try:
        write_report(build_report(Path(run_dir)), Path(run_dir))
        return True
    except Exception:  # noqa: BLE001 - diagnostic finalizer must preserve acquisition outcomes
        try:
            print(
                "warning: CRE performance report unavailable; acquisition outcome unchanged",
                file=sys.stderr,
            )
        except Exception:  # noqa: BLE001 - even a broken diagnostic stream must not change outcomes
            return False
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "run", nargs="?", help="series/checkpoint directory; default newest series"
    )
    parser.add_argument(
        "--trial-dir", type=Path, help="read saved bounded trial evidence offline"
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        help="save sanitized JSON and Markdown; create a private output directory if absent",
    )
    parser.add_argument(
        "--json", action="store_true", help="print a structured one-shot report"
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="compare another saved checkpoint/series offline; never queries providers or the database",
    )
    args = parser.parse_args(argv)
    try:
        if args.trial_dir is not None:
            if args.run:
                raise ValueError("choose trial or checkpoint evidence")
            report = build_trial_report(args.trial_dir.expanduser())
        else:
            path = (
                Path(args.run).expanduser() if args.run else resolve_series_path(None)
            )
            report = build_report(path)
        if args.baseline is not None:
            report["comparison"] = compare_reports(
                report, build_report(args.baseline.expanduser())
            )
        if args.out_dir is not None:
            write_report(report, args.out_dir.expanduser())
    except (OSError, ValueError, UnicodeError, TypeError, RecursionError):
        print(
            "Performance evidence is unavailable or invalid; no database was queried."
        )
        return 2
    print(json.dumps(report, sort_keys=True) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
