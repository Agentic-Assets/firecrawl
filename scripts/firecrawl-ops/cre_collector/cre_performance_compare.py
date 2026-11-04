"""Small numeric projections for offline CRE timing comparisons; no I/O."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime

from cre_ingest import SOURCE_TO_BROKERAGE

_SUCCESS = {
    "ingested",
    "complete",
    "supported_scope_complete",
    "selected_transaction_scope_complete",
    "additive_scope_complete_current_inventory",
    "additive_scope_complete_coverage_hold",
}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) and 0 <= result <= 1e18 else None


def _duration(first: object, last: object) -> float | None:
    if not isinstance(first, str) or not isinstance(last, str):
        return None
    if max(len(first), len(last)) > 40:
        return None
    try:
        start = datetime.fromisoformat(first.replace("Z", "+00:00"))
        end = datetime.fromisoformat(last.replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None or end < start:
            return None
        return round((end - start).total_seconds(), 3)
    except (ValueError, OverflowError):
        return None


def source_stage_timing(value: object) -> dict:
    """Derive timestamp wall intervals; command journals retain monotonic costs."""
    record = value if isinstance(value, Mapping) else {}
    pairs = {
        "dispatch_wait_seconds": ("queued_at", "collection_started_at"),
        "collection_wall_seconds": ("collection_started_at", "collection_finished_at"),
        "awaiting_validation_seconds": (
            "collection_finished_at",
            "validation_started_at",
        ),
        "validation_to_ingested_seconds": ("validation_started_at", "ingested_at"),
        "post_ingest_to_verified_seconds": ("ingested_at", "verified_at"),
        "source_elapsed_seconds": ("collection_started_at", "verified_at"),
        "queued_to_verified_seconds": ("queued_at", "verified_at"),
    }
    return {
        name: _duration(record.get(first), record.get(last))
        for name, (first, last) in pairs.items()
    }


def _rows(report: Mapping) -> dict[str, Mapping]:
    values = report.get("sources")
    if not isinstance(values, list) or len(values) > len(SOURCE_TO_BROKERAGE):
        raise ValueError("invalid comparison source set")
    rows = {}
    for row in values:
        if not isinstance(row, Mapping):
            raise ValueError("invalid comparison source")
        source = row.get("source")
        if (
            not isinstance(source, str)
            or source not in SOURCE_TO_BROKERAGE
            or source in rows
        ):
            raise ValueError("invalid comparison source")
        rows[source] = row
    return rows


def _rate(count: object, seconds: object) -> float | None:
    quantity, elapsed = _number(count), _number(seconds)
    return (
        round(quantity * 60 / elapsed, 3) if quantity is not None and elapsed else None
    )


def _measurement(current: object, baseline: object) -> dict:
    now, before = _number(current), _number(baseline)
    return {
        "current": now,
        "baseline": before,
        "change": round(now - before, 3)
        if now is not None and before is not None
        else None,
    }


def _runtime_context(report: Mapping) -> object:
    runtime = report.get("shared_runtime_configuration")
    if not isinstance(runtime, Mapping) or runtime.get("state") != "available":
        children = report.get("checkpoint_resources")
        if isinstance(children, list) and len(children) == 1:
            runtime = children[0].get("runtime_configuration")
    if not isinstance(runtime, Mapping) or runtime.get("state") != "available":
        return None
    containers = runtime.get("containers")
    if not isinstance(containers, list) or len(containers) > 2:
        return None
    return {
        "hardware": runtime.get("hardware"),
        "containers": [
            {
                name: row.get(name)
                for name in (
                    "service",
                    "image",
                    "cpu_limit",
                    "memory_limit_bytes",
                    "memory_swap_limit_bytes",
                    "shm_size_bytes",
                    "pids_limit",
                )
            }
            for row in containers
            if isinstance(row, Mapping)
        ],
    }


def compare_reports(current: Mapping, baseline: Mapping) -> dict:
    """Compare observed saved work, without promoting it to causal quality proof."""
    if any(
        report.get("kind") != "cre_performance_report" for report in (current, baseline)
    ):
        raise ValueError("comparison requires checkpoint or series reports")
    now_rows, before_rows = _rows(current), _rows(baseline)
    same_sources = bool(now_rows) and set(now_rows) == set(before_rows)
    current_scope, baseline_scope = current.get("run_scope"), baseline.get("run_scope")
    scope_known = all(
        isinstance(scope, Mapping)
        and isinstance(scope.get("transactions"), list)
        and bool(scope["transactions"])
        for scope in (current_scope, baseline_scope)
    )
    same_scope = scope_known and all(
        current_scope.get(key) == baseline_scope.get(key)
        for key in ("transactions", "max_items", "freshness_policy")
    )
    differences = []
    if current.get("run_status") != baseline.get("run_status"):
        differences.append("run_outcome_changed")
    if current.get("collector_git_sha") != baseline.get("collector_git_sha"):
        differences.append("collector_code_changed")
    if not same_sources:
        differences.append("source_cohort_changed")
    if not same_scope:
        differences.append("scope_changed_or_unknown")
    current_runtime, baseline_runtime = (
        _runtime_context(current),
        _runtime_context(baseline),
    )
    if (
        current_runtime is None
        or baseline_runtime is None
        or current_runtime != baseline_runtime
    ):
        differences.append("runtime_context_changed_or_unavailable")
    if current.get("run_scope", {}).get("pipeline") != baseline.get(
        "run_scope", {}
    ).get("pipeline"):
        differences.append("pipeline_mode_changed")
    rows, qualified, same_work, same_quality = (
        [],
        all(report.get("run_status") in _SUCCESS for report in (current, baseline)),
        True,
        True,
    )
    for source in sorted(now_rows.keys() & before_rows.keys()):
        now, before = now_rows[source], before_rows[source]
        if now.get("status") != before.get("status"):
            differences.append(f"{source}:outcome_changed")
        if now.get("status") not in _SUCCESS or before.get("status") not in _SUCCESS:
            qualified = False
            differences.append(f"{source}:outcome_not_successful")
        current_cache, baseline_cache = _cache_retry(now), _cache_retry(before)
        if current_cache != baseline_cache:
            differences.append(f"{source}:cache_retry_context_changed")
        for name in ("cache_hits", "cache_misses", "cache_refresh_bypasses"):
            if current_cache.get(name) != baseline_cache.get(name):
                same_work = False
        if now.get("settings") != before.get("settings"):
            differences.append(f"{source}:settings_changed")
        if now.get("staged_unique") != before.get("staged_unique"):
            differences.append(f"{source}:staged_count_changed")
            same_work = False
        if now.get("acquisition_quality_signals") != before.get(
            "acquisition_quality_signals"
        ):
            differences.append(f"{source}:quality_signals_changed")
            same_quality = False
        if now.get("rejected_by_ingest") != before.get("rejected_by_ingest"):
            differences.append(f"{source}:rejected_count_changed")
            same_quality = False
        readback = (
            now.get("database_readback_ok") is True
            and before.get("database_readback_ok") is True
        )
        qualified = qualified and readback
        if not readback:
            differences.append(f"{source}:readback_not_qualified")
        if (
            now.get("quality_no_regression") is not True
            or before.get("quality_no_regression") is not True
        ):
            qualified = False
            differences.append(f"{source}:quality_not_qualified")
        phases = set(now.get("timed_command_seconds", {})) | set(
            before.get("timed_command_seconds", {})
        )
        rows.append(
            {
                "source": source,
                "readbacks_recorded_ok": readback,
                "outcomes": {
                    "current": now.get("status"),
                    "baseline": before.get("status"),
                },
                "cache_retry": {"current": current_cache, "baseline": baseline_cache},
                "staged_unique": _measurement(
                    now.get("staged_unique"), before.get("staged_unique")
                ),
                "verified_current_inventory_unique": _measurement(
                    now.get("verified_current_inventory_unique"),
                    before.get("verified_current_inventory_unique"),
                ),
                "verified_current_inventory_per_minute": _measurement(
                    now.get("verified_current_inventory_per_minute"),
                    before.get("verified_current_inventory_per_minute"),
                ),
                "elapsed_seconds": _measurement(
                    now.get("source_elapsed_seconds"),
                    before.get("source_elapsed_seconds"),
                ),
                "staged_per_minute": _measurement(
                    _rate(now.get("staged_unique"), now.get("source_elapsed_seconds")),
                    _rate(
                        before.get("staged_unique"),
                        before.get("source_elapsed_seconds"),
                    ),
                ),
                "phase_command_seconds": {
                    phase: _measurement(
                        now.get("timed_command_seconds", {}).get(phase),
                        before.get("timed_command_seconds", {}).get(phase),
                    )
                    for phase in sorted(phases)
                },
            }
        )
    now_wall, before_wall = (
        _number(current.get("run_elapsed_seconds")),
        _number(baseline.get("run_elapsed_seconds")),
    )
    matched = same_sources and same_scope and same_work and same_quality and qualified
    reduction = (
        round((before_wall - now_wall) * 100 / before_wall, 3)
        if matched and now_wall and before_wall
        else None
    )
    return {
        "state": "matched_scope_observations"
        if matched
        else "unmatched_or_unqualified_observations",
        "baseline_generation_id": baseline.get("generation_id"),
        "baseline_collector_git_sha": baseline.get("collector_git_sha"),
        "run_outcomes": {
            "current": current.get("run_status"),
            "baseline": baseline.get("run_status"),
        },
        "elapsed": {
            "current_seconds": now_wall,
            "baseline_seconds": before_wall,
            "reduction_percent": reduction,
        },
        "sources": rows,
        "query_client_timings": _compare_queries(current, baseline),
        "context_differences": differences,
        "causal_improvement_established": False,
        "speedup_percent": None,
        "basis": "observed_wall_and_staged_work_not_sum_of_parallel_commands_or_independent_quality_acceptance",
        "cache_provider_and_host_variation_may_differ": True,
    }


def _cache_retry(row: Mapping) -> dict:
    totals = {}
    commands = row.get("commands")
    if not isinstance(commands, list) or len(commands) > 2048:
        return {"state": "unavailable"}
    for command in commands:
        if not isinstance(command, Mapping) or command.get("phase") != "collection":
            continue
        scrape = command.get("scrape")
        if not isinstance(scrape, Mapping):
            continue
        cache = scrape.get("jll_detail_cache")
        cache = cache if isinstance(cache, Mapping) else {}
        requests = scrape.get("requests")
        requests = requests if isinstance(requests, Mapping) else {}
        fields = {
            "cache_" + name: cache.get(name)
            for name in ("hits", "misses", "refresh_bypasses")
        }
        for kind, prefix in (("http_helper", "http"), ("json_parse", "json")):
            retry = requests.get(kind)
            if isinstance(retry, Mapping):
                fields.update(
                    {
                        prefix + "_" + name: retry.get(name)
                        for name in (
                            "retry_attempts",
                            "backoff_ms",
                            "terminal_backoff_ms",
                        )
                    }
                )
        for name, value in fields.items():
            number = _number(value)
            if number is not None:
                totals[name] = totals.get(name, 0) + number
    return {"state": "observed", **totals} if totals else {"state": "unavailable"}


def _compare_queries(current: Mapping, baseline: Mapping) -> list[dict]:
    from cre_validate import QUERIES

    allowed = set(QUERIES) | {"artifact_run_jobs", "historical_primary_child_conflicts"}
    totals = []
    for report in (current, baseline):
        by_query = {}
        entries = report.get("validation_query_timings")
        if isinstance(entries, list) and len(entries) <= 512:
            for entry in entries:
                queries = entry.get("queries") if isinstance(entry, Mapping) else None
                if not isinstance(queries, Mapping):
                    continue
                for name in allowed & queries.keys():
                    row = queries[name]
                    elapsed = (
                        _number(row.get("elapsed_ms"))
                        if isinstance(row, Mapping)
                        else None
                    )
                    if elapsed is not None:
                        record = by_query.setdefault(name, {"calls": 0, "ms": 0})
                        record["calls"] += 1
                        record["ms"] += elapsed
        totals.append(by_query)
    now, before = totals
    return [
        {
            "query": name,
            "calls": _measurement(
                now.get(name, {}).get("calls"), before.get(name, {}).get("calls")
            ),
            "summed_client_ms": _measurement(
                now.get(name, {}).get("ms"), before.get(name, {}).get("ms")
            ),
            "basis": "numeric_timing_frames_only_not_server_execution_or_run_wall",
        }
        for name in sorted(now.keys() | before.keys())
    ]
