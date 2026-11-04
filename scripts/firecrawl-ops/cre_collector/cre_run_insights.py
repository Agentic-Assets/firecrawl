"""Bounded offline projections and sampled resource statistics for CRE runs."""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

from cre_ingest import SOURCE_TO_BROKERAGE
from cre_series_status import _read_regular_file

MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_RECORD_BYTES = 64 * 1024
MAX_SAMPLES = 20000
MAX_GAP_SECONDS = 30.0


def number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) and 0 <= result <= 1e18 else None


def obj(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def timestamp(value: object) -> float | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


def duration(start: object, end: object) -> float | None:
    left, right = timestamp(start), timestamp(end)
    return (
        round(right - left, 3)
        if left is not None and right is not None and right >= left
        else None
    )


def read_json(
    root: Path, name: str, *, limit: int = MAX_EVIDENCE_BYTES
) -> Mapping[str, Any]:
    if root.is_symlink() or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("unsafe evidence path")
    return obj(json.loads(_read_regular_file(root / name, limit=limit)))


def read_records(root: Path, name: str) -> tuple[list[Mapping[str, Any]], list[str]]:
    warnings: list[str] = []
    try:
        if root.is_symlink():
            raise ValueError("unsafe evidence directory")
        raw = _read_regular_file(root / name, limit=MAX_EVIDENCE_BYTES)
    except (OSError, ValueError):
        return [], ["evidence_unavailable_or_oversized"]
    records = []
    for index, line in enumerate(raw.splitlines()):
        if index >= MAX_SAMPLES:
            warnings.append("sample_limit_exceeded")
            break
        if len(line) > MAX_RECORD_BYTES:
            warnings.append("record_oversized")
            continue
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TypeError("not an object")
            records.append(value)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            warnings.append("record_invalid")
    return records, sorted(set(warnings))


def sampled_stats(
    points: Sequence[tuple[float, float]], *, max_gap: float = MAX_GAP_SECONDS
) -> dict[str, Any]:
    """Left-hold duration estimate over measured intervals, never across long gaps."""
    clean: list[tuple[float, float]] = []
    invalid = 0
    for clock, value in points:
        if (
            number(clock) is None
            or number(value) is None
            or (clean and clock <= clean[-1][0])
        ):
            invalid += 1
            continue
        clean.append((clock, value))
    ordered = sorted(value for _, value in clean)
    spans = [(right[0] - left[0], left[1]) for left, right in pairwise(clean)]
    measured = [(seconds, value) for seconds, value in spans if seconds <= max_gap]
    covered = sum(seconds for seconds, _ in measured)
    wall = clean[-1][0] - clean[0][0] if len(clean) > 1 else 0

    def quantile(p: float) -> float | None:
        return round(ordered[math.ceil(len(ordered) * p) - 1], 3) if ordered else None

    return {
        "sample_count": len(clean),
        "first_observed": clean[0][1] if clean else None,
        "last_observed": clean[-1][1] if clean else None,
        "mean": round(sum(ordered) / len(ordered), 3) if ordered else None,
        "p50": quantile(0.50),
        "p95": quantile(0.95),
        "sampled_max": ordered[-1] if ordered else None,
        "duration_weighted_estimate": round(
            sum(seconds * value for seconds, value in measured) / covered, 3
        )
        if covered
        else None,
        "covered_seconds": round(covered, 3),
        "window_seconds": round(wall, 3),
        "uncovered_gap_seconds": round(wall - covered, 3),
        "max_sample_gap_seconds": round(
            max((seconds for seconds, _ in spans), default=0), 3
        ),
        "invalid_or_nonmonotonic_samples": invalid,
        "method": "sample_nearest_rank_percentiles_left_hold_weighting_no_final_extrapolation",
        "gap_ceiling_seconds": max_gap,
    }


def overlap(intervals: Sequence[tuple[float, float]]) -> dict[str, Any]:
    events: list[tuple[float, int]] = []
    for start, end in intervals:
        if end >= start:
            events.extend(((start, 1), (end, -1)))
    events.sort()
    widths: dict[int, float] = defaultdict(float)
    active, peak = 0, 0
    previous = events[0][0] if events else 0
    for clock, delta in events:
        widths[active] += clock - previous
        active += delta
        peak = max(peak, active)
        previous = clock
    wall = events[-1][0] - events[0][0] if events else 0
    return {
        "collection_window_seconds": round(wall, 3) if events else None,
        "summed_source_collection_seconds": round(
            sum(end - start for start, end in intervals), 3
        )
        if events
        else None,
        "peak_overlapping_source_collections": peak if events else None,
        "source_worker_duration_estimate": round(
            sum(width * seconds for width, seconds in widths.items()) / wall, 3
        )
        if wall
        else None,
        "seconds_by_active_sources": {
            str(width): round(seconds, 3)
            for width, seconds in sorted(widths.items())
            if seconds
        },
        "basis": "artifact_collection_timestamps_not_server_request_concurrency",
    }


HOST_FIELDS = ("cpu_percent", "memory_total_bytes", "load_1m", "load_5m", "load_15m")
CONTAINER_FIELDS = (
    "cpu_quota_cores",
    "cpu_percent",
    "cpu_cores",
    "memory_current_bytes",
    "memory_limit_bytes",
    "pids",
    "restart_count",
)


def resource_report(run_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    records, warnings = read_records(run_dir, "resource-usage.jsonl")
    sessions: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    future = (now or datetime.now(timezone.utc)).timestamp() + 300
    for record in records:
        identity = record.get("session_id")
        observed = timestamp(record.get("observed_at"))
        if (
            type(record.get("schema_version")) is not int
            or record.get("schema_version") != 1
            or record.get("kind") != "cre_resource_usage"
            or record.get("run_id") != run_dir.name
            or not isinstance(identity, str)
            or not re.fullmatch(r"[0-9a-f]{32}", identity)
            or observed is None
            or observed > future
            or number(record.get("elapsed_seconds")) is None
        ):
            warnings.append("resource_identity_or_clock_invalid")
            continue
        sessions[identity].append(record)
    result = []
    for identity, items in sessions.items():
        metrics: dict[str, list[tuple[float, float]]] = defaultdict(list)
        containers: dict[tuple[str, str], dict[str, list[tuple[float, float]]]] = (
            defaultdict(lambda: defaultdict(list))
        )
        invalid_samples = 0
        pressures: Counter[str] = Counter()
        oom_snapshots: Counter[tuple[str, str]] = Counter()
        oom_observed: Counter[tuple[str, str]] = Counter()
        runtime_availability: Counter[str] = Counter()
        runtime_gaps: Counter[str] = Counter()
        previous = -1.0
        for item in items:
            clock = number(item.get("elapsed_seconds"))
            assert clock is not None
            if clock <= previous:
                invalid_samples += 1
                continue
            previous = clock
            host = obj(item.get("host"))
            pressure = host.get("memory_pressure")
            if pressure in ("normal", "warning", "critical"):
                pressures[pressure] += 1
            for key in ("sample_duration_seconds", "sample_seconds"):
                value = number(item.get(key))
                if value is not None:
                    metrics[key].append((clock, value))
            for key in HOST_FIELDS:
                value = number(host.get(key))
                if value is not None:
                    metrics[key].append((clock, value))
            for group, keys in (
                ("swap", ("total_bytes", "used_bytes", "free_bytes")),
                ("disk", ("total_bytes", "used_bytes", "free_bytes")),
                (
                    "vm",
                    (
                        "free_bytes",
                        "active_bytes",
                        "inactive_bytes",
                        "wired_bytes",
                        "compressor_bytes",
                        "pageins",
                        "pageouts",
                        "swapins",
                        "swapouts",
                    ),
                ),
            ):
                for key in keys:
                    value = number(obj(host.get(group)).get(key))
                    if value is not None:
                        metrics[f"{group}_{key}"].append((clock, value))
            for key in ("count", "rss_bytes", "cpu_percent_lifetime"):
                value = number(obj(item.get("processes")).get(key))
                if value is not None:
                    metrics[f"owned_processes_{key}"].append((clock, value))
            runtime = obj(item.get("runtime"))
            availability = runtime.get("availability")
            runtime_availability[
                availability
                if availability in ("available", "unavailable")
                else "unknown"
            ] += 1
            if availability != "available":
                code = runtime.get("unavailable_code")
                known_codes = {
                    "invalid_api_endpoint",
                    "compose_selection_failed",
                    "invalid_container_selection",
                    "inspect_failed",
                    "invalid_output",
                    "project_mismatch",
                    "container_not_running",
                    "api_port_mismatch",
                    "inspect_timeout",
                    "docker_unavailable",
                    "diagnostic_failed",
                }
                runtime_gaps[
                    code if isinstance(code, str) and code in known_codes else "unknown"
                ] += 1
            capacity = number(runtime.get("docker_vm_memory_total_bytes"))
            if capacity is not None:
                metrics["docker_vm_memory_total_bytes"].append((clock, capacity))
            raw_containers = (
                runtime.get("containers") if availability == "available" else []
            )
            for container in (
                raw_containers[:16] if isinstance(raw_containers, list) else []
            ):
                container = obj(container)
                role, cid = container.get("role"), container.get("id")
                if (
                    role not in ("api", "browser")
                    or not isinstance(cid, str)
                    or not re.fullmatch(r"[0-9a-f]{12,64}", cid)
                ):
                    warnings.append("container_identity_invalid")
                    continue
                values = containers[(role, cid)]
                if type(container.get("oom_killed")) is bool:
                    oom_observed[(role, cid)] += 1
                if container.get("oom_killed") is True:
                    oom_snapshots[(role, cid)] += 1
                for key in CONTAINER_FIELDS:
                    value = number(container.get(key))
                    if value is not None:
                        values[key].append((clock, value))
                quota = number(container.get("cpu_quota_cores"))
                cpu = number(container.get("cpu_percent"))
                if quota and cpu is not None and container.get("running") is True:
                    values["cpu_percent_of_verified_quota"].append((clock, cpu / quota))
                memory, limit = (
                    number(container.get("memory_current_bytes")),
                    number(container.get("memory_limit_bytes")),
                )
                if memory is not None and limit:
                    values["memory_percent_of_limit"].append(
                        (clock, memory * 100 / limit)
                    )
        result.append(
            {
                "session_id": identity,
                "started_observation_at": items[0]["observed_at"],
                "last_observation_at": items[-1]["observed_at"],
                "sample_count": len(items),
                "invalid_samples": invalid_samples,
                "memory_pressure_samples": dict(pressures),
                "runtime_availability_samples": dict(runtime_availability),
                "runtime_gap_code_samples": dict(runtime_gaps),
                "elapsed_first_sample_seconds": number(items[0].get("elapsed_seconds")),
                "elapsed_last_sample_seconds": number(items[-1].get("elapsed_seconds")),
                "missing_metrics": [
                    key
                    for key in (
                        "cpu_percent",
                        "memory_total_bytes",
                        "swap_used_bytes",
                        "owned_processes_rss_bytes",
                        "docker_vm_memory_total_bytes",
                    )
                    if key not in metrics
                ],
                "metrics": {
                    key: sampled_stats(value) for key, value in sorted(metrics.items())
                },
                "containers": [
                    {
                        "role": role,
                        "id": cid,
                        "oom_killed_observed_bool_snapshots": oom_observed[(role, cid)],
                        "oom_killed_true_snapshots": oom_snapshots[(role, cid)]
                        if oom_observed[(role, cid)]
                        else None,
                        "counter_basis": "container_lifetime_snapshots_not_run_event_counts",
                        "metrics": {
                            key: sampled_stats(value)
                            for key, value in sorted(values.items())
                        },
                    }
                    for (role, cid), values in sorted(containers.items())
                ],
            }
        )
    return {
        "state": "observed" if result else "unavailable",
        "scope": "coordinator_shared_not_attributed_to_individual_sources",
        "sessions": result,
        "warnings": sorted(set(warnings)),
        "interpretation": "sampled_headroom_does_not_establish_bottleneck_or_speedup; sessions_not_averaged_across_resume_downtime; process_CPU_is_lifetime_average",
    }


def _optional_json(
    root: Path, name: str, warnings: list[str], *, limit: int = MAX_EVIDENCE_BYTES
) -> Mapping[str, Any]:
    try:
        return read_json(root, name, limit=limit)
    except (OSError, ValueError, UnicodeError, RecursionError):
        warnings.append("optional_evidence_unavailable")
        return {}


def build_trial_report(root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Import the legacy saved trial layout, without promoting it to a checkpoint."""
    warnings: list[str] = []
    result = read_json(root, "result.json")
    sha = result.get("git_sha")
    sources = obj(result.get("sources"))
    if (
        not isinstance(sha, str)
        or not re.fullmatch(r"[0-9a-f]{40}", sha)
        or not sources
        or len(sources) > 8
        or any(key not in SOURCE_TO_BROKERAGE for key in sources)
    ):
        raise ValueError("trial identity unavailable")
    dry = _optional_json(root, "dry-run-results.json", warnings)
    rows, intervals = [], []
    for source, process_value in sources.items():
        process = obj(process_value)
        artifact = _optional_json(
            root, f"{source}.json", warnings, limit=MAX_ARTIFACT_BYTES
        )
        meta = obj(artifact.get("runMeta"))
        start, end = timestamp(meta.get("startedAt")), timestamp(meta.get("finishedAt"))
        seconds = duration(meta.get("startedAt"), meta.get("finishedAt"))
        bound_start, bound_end = (
            timestamp(process.get("started_at")),
            timestamp(process.get("finished_at")),
        )
        identity_ok = (
            isinstance(meta.get("transactions"), list)
            and bool(meta["transactions"])
            and all(x in ("sale", "lease") for x in meta["transactions"])
            and seconds is not None
            and bound_start is not None
            and bound_end is not None
            and bound_start <= start <= end <= bound_end
        )
        transaction_rows = []
        counts = Counter()
        raw_sources = artifact.get("sources")
        if not isinstance(raw_sources, list) or len(raw_sources) > 2:
            identity_ok = False
            raw_sources = []
        for record in raw_sources:
            record = obj(record)
            transaction = record.get("transaction")
            if record.get("sourceKey") != source or transaction not in (
                "sale",
                "lease",
            ):
                identity_ok = False
                continue
            fresh = obj(record.get("freshness"))
            transaction_rows.append(
                {
                    "transaction": transaction,
                    "collected": number(record.get("listingsCollected")),
                    "truncated": record.get("truncated")
                    if type(record.get("truncated")) is bool
                    else None,
                    "collection_error": bool(record.get("error")),
                    "freshness": {
                        key: number(fresh.get(key))
                        for key in (
                            "inventoryObserved",
                            "detailObserved",
                            "authoritativeInventoryFeed",
                            "detailErrors",
                            "staleDetailObservations",
                            "childPreservationRows",
                        )
                    },
                }
            )
            for key in ("detailErrors", "staleDetailObservations"):
                if number(fresh.get(key)) is not None:
                    counts[key] += fresh[key]
        if identity_ok and (
            len(transaction_rows) != len(meta["transactions"])
            or {row["transaction"] for row in transaction_rows}
            != set(meta["transactions"])
        ):
            identity_ok = False
        if identity_ok:
            intervals.append((start, end))
        else:
            warnings.append("trial_artifact_binding_or_timing_invalid")
        raw = (
            len(artifact["listings"])
            if isinstance(artifact.get("listings"), list) and identity_ok
            else None
        )
        dry_rc = obj(dry.get(source)).get("returncode")
        dry_rc = dry_rc if type(dry_rc) is int else None
        staged = None
        if dry_rc == 0:
            try:
                raw_log = _read_regular_file(
                    root / f"{source}-dry-run.log", limit=1024 * 1024
                )
                matches = re.findall(rb"(?m)^staged listings: ([0-9]{1,9});", raw_log)
                if len(matches) == 1:
                    staged = int(matches[0])
            except (OSError, ValueError):
                pass
        rc = process.get("rc") if type(process.get("rc")) is int else None
        failed = (
            rc not in (None, 0)
            or dry_rc not in (None, 0)
            or counts["detailErrors"] > 0
            or any(x["collection_error"] for x in transaction_rows)
        )
        quality = (
            "failed"
            if failed
            else "dry_run_passed"
            if identity_ok and dry_rc == 0
            else "unknown"
        )
        rows.append(
            {
                "source": source,
                "status": quality,
                "collector_returncode": rc,
                "dry_run_returncode": dry_rc,
                "raw_emitted_rows": raw,
                "staged_unique": staged if identity_ok else None,
                "rejected_by_ingest": None,
                "fresh_qualified_unique": None,
                "ingested_rows": None,
                "database_readback_ok": None,
                "collection_seconds": seconds if identity_ok else None,
                "observed_process_seconds": duration(
                    process.get("started_at"), process.get("finished_at")
                ),
                "process_timing_basis": "poll_observed_finish_includes_bootstrap_and_poll_delay",
                "transactions": transaction_rows,
                "raw_emitted_per_minute": round(raw * 60 / seconds, 3)
                if raw is not None and seconds
                else None,
                "staged_unique_per_minute": round(staged * 60 / seconds, 3)
                if staged is not None and identity_ok and seconds
                else None,
                "quality_is_not_proven_by_collector_exit_zero": True,
                "detail_errors": counts["detailErrors"],
                "failed_transactions": [
                    row["transaction"]
                    for row in transaction_rows
                    if row["collection_error"]
                ],
            }
        )
    runtime, runtime_warnings = read_records(root, "runtime-samples.jsonl")
    warnings.extend(runtime_warnings)
    metrics: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for item in runtime:
        clock = timestamp(item.get("at"))
        if clock is None:
            continue
        for key, value in (
            ("browser_active_pages", obj(item.get("browser")).get("activePages")),
            ("active_source_processes", item.get("active_source_processes")),
            (
                "browser_page_capacity",
                obj(item.get("browser")).get("maxConcurrentPages"),
            ),
            ("api_max_concurrency", obj(item.get("queue")).get("maxConcurrency")),
            ("api_jobs_in_queue", obj(item.get("queue")).get("jobsInQueue")),
            ("api_active_jobs", obj(item.get("queue")).get("activeJobsInQueue")),
            ("api_waiting_jobs", obj(item.get("queue")).get("waitingJobsInQueue")),
        ):
            if number(value) is not None:
                metrics[key].append((clock, value))
        containers = item.get("containers")
        for container in containers[:16] if isinstance(containers, list) else []:
            container = obj(container)
            name = container.get("Name")
            role = (
                "browser"
                if isinstance(name, str) and name.endswith("-playwright-service-1")
                else "api"
                if isinstance(name, str) and name.endswith("-api-1")
                else None
            )
            if role is None:
                continue
            memory = container.get("MemUsage")
            if isinstance(memory, str) and len(memory) <= 100:
                parts = memory.split("/")
                if len(parts) == 2:
                    for key, part in zip(
                        ("memory_current_bytes", "memory_limit_bytes"),
                        parts,
                        strict=True,
                    ):
                        parsed = docker_bytes(part.strip())
                        if parsed is not None:
                            metrics[f"{role}_{key}"].append((clock, parsed))
            for key, raw_value in (
                ("cpu_percent", container.get("CPUPerc")),
                ("memory_percent_of_limit", container.get("MemPerc")),
                ("pids", container.get("PIDs")),
            ):
                if isinstance(raw_value, str) and re.fullmatch(
                    r"[0-9]+(?:\.[0-9]+)?%?", raw_value
                ):
                    metrics[f"{role}_{key}"].append(
                        (clock, float(raw_value.rstrip("%")))
                    )
    guard, guard_warnings = read_records(root, "host-cpu-guard.jsonl")
    warnings.extend(guard_warnings)
    for item in guard:
        clock, value = (
            timestamp(item.get("observed_at")),
            number(item.get("host_cpu_percent")),
        )
        if clock is not None and value is not None and value <= 100:
            metrics["host_cpu_percent"].append((clock, value))
    return {
        "schema_version": 1,
        "kind": "cre_saved_trial_performance_report",
        "collector_git_sha": sha,
        "computed_at": (now or datetime.now(timezone.utc)).isoformat(),
        "trial_wall_seconds": number(result.get("elapsed_seconds")),
        "wall_timing_basis": "coordinator_including_bootstrap_polling_settlement",
        "sources": rows,
        "overlap": overlap(intervals),
        "resources": {
            "state": "observed" if metrics else "unavailable",
            "scope": "shared_trial",
            "metrics": {
                key: sampled_stats(value) for key, value in sorted(metrics.items())
            },
            "host_memory": None,
            "verified_cpu_quota": None,
            "request_latency": None,
            "coverage": "historical_samples_only; missing_memory_latency_and_quota_remain_unknown",
        },
        "settled": result.get("settled")
        if type(result.get("settled")) is bool
        else None,
        "database_writes_reported": number(result.get("database_writes")),
        "comparison": {
            "state": "unavailable",
            "speedup_percent": None,
            "reason": "no_matched_quality_valid_baseline",
        },
        "warnings": sorted(set(warnings)),
        "completeness_is_not_established_by_performance_metrics": True,
    }


def docker_bytes(value: str) -> float | None:
    match = re.fullmatch(
        r"([0-9]+(?:\.[0-9]+)?)\s*(B|kB|KB|MB|GB|TB|KiB|MiB|GiB|TiB)", value
    )
    if match is None:
        return None
    powers = {
        "B": 1,
        "kB": 1000,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
        "TiB": 1024**4,
    }
    return number(float(match[1]) * powers[match[2]])
