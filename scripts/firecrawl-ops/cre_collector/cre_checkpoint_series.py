#!/usr/bin/env python3
"""Run the full CRE registry as bounded, serial checkpoint generations.

Each source gets its own `cre_checkpoint_refresh.py` run so no source's
observation window is extended by unrelated slow providers. Source-local
collection or coverage failures are recorded and the series continues. CPU,
database, validation, infrastructure, and operator interruptions stop the
series immediately. Every live write remains inside the checkpoint runner.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cre_checkpoint_refresh import (
    COLLECTOR_DIR,
    DEFAULT_CPU_SAMPLE_SECONDS,
    DEFAULT_CPU_SUSTAIN_SECONDS,
    DEFAULT_MAX_HOST_CPU_PERCENT,
    DEFAULT_MAX_RESUME_AGE_HOURS,
    SOURCE_KEYS,
    RefreshError,
    allocate_run_id,
    atomic_write_json,
    database_target_fingerprint,
    git_identity,
    read_host_cpu_percent,
    utc_now,
    validate_run_id,
)
from cre_inventory_generation import publish_series_health
from cre_resource_recovery import (
    DEFAULT_LOW_CPU_PERCENT,
    DEFAULT_LOW_CPU_SECONDS,
    DEFAULT_MAX_COOLDOWN_SECONDS,
    DEFAULT_MAX_RECOVERIES_PER_SOURCE,
    DEFAULT_MAX_SERIES_COOLDOWN_SECONDS,
    DEFAULT_SAMPLE_SECONDS,
    MAX_COOLDOWN_SECONDS,
    MAX_RECOVERIES_PER_SOURCE,
    MAX_SERIES_COOLDOWN_SECONDS,
    CooldownProgress,
    RecoveryBudgetExhausted,
    RecoveryCancelled,
    RecoveryConfig,
    RecoveryEvidenceError,
    RecoveryOwnershipError,
    RecoveryTelemetryError,
    SeriesOwnershipLock,
    wait_for_cpu_recovery,
)

SCHEMA_VERSION = 1
DEFAULT_OUT_ROOT = COLLECTOR_DIR / "out" / "checkpoint-series"
SOURCE_LOCAL_FAILURE_PREFIXES = (
    "RefreshError: source checkpoints remain incomplete:",
    "RefreshError: aggregate coverage gate is not established for source(s):",
)
SUCCESS_STATUS = "supported_scope_complete"
RESOURCE_GUARD_STATUS = "resource_guard_interrupted"
# The child can need 30s to supervise a cohort worker whose inner grace is15s.
SERIES_INTERRUPT_GRACE_SECONDS = 45
RECOVERABLE_CPU_REASON_CODES = {
    "host_cpu_sustained",
    "host_cpu_start_blocked",
}
RECOVERABLE_RESOURCE_PHASES = {"preflight", "collection"}
RECOVERY_PROGRESS_STATES = {"idle", "cooling_down", "resuming", "failed", "exhausted"}
RECOVERY_PROGRESS_EVENTS = {
    "cooldown_started",
    "cooldown_stopped",
    "cooldown_complete",
    "child_resume_started",
    "recovery_not_admitted",
}
RECOVERY_PROGRESS_REASON_CODES = RECOVERABLE_CPU_REASON_CODES | {
    "cpu_cooldown_complete",
    "operator_cancelled",
    "recovery_budget_exhausted",
    "recovery_telemetry_failed",
    "recovery_evidence_failed",
    "generation_expired",
    "recovery_admission_changed",
    "collector_identity_changed",
    "database_target_changed",
    "recovery_checkpoint_missing",
    "recovery_ownership_mismatch",
    "recovery_disabled",
    "recovery_not_admitted",
}
RECOVERY_PROGRESS_OPERATIONS = {
    "host_cpu_admission",
    "healthcheck",
    "pre_validation",
    "source_collection_preflight",
    "source_collection",
}


class SeriesError(RuntimeError):
    """The bounded checkpoint series cannot proceed safely."""


def _exact_json_equal(left: Any, right: Any) -> bool:
    """Compare persisted JSON without Python's bool/number equality aliasing."""
    try:
        return json.dumps(
            left, allow_nan=False, separators=(",", ":"), sort_keys=True
        ) == json.dumps(right, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        return False


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def parse_sources(value: str) -> tuple[str, ...]:
    if value.strip().lower() == "all":
        return SOURCE_KEYS
    sources = tuple(item.strip() for item in value.split(",") if item.strip())
    if not sources:
        raise ValueError("at least one source is required")
    unknown = [source for source in sources if source not in SOURCE_KEYS]
    if unknown:
        raise ValueError("unknown source(s): " + ", ".join(unknown))
    if len(set(sources)) != len(sources):
        raise ValueError("duplicate sources are not allowed")
    return sources


def series_config(
    *,
    sources: Sequence[str],
    page_cap: int,
    concurrency: int,
    attempts_per_source: int,
    max_resume_age_hours: float,
    max_host_cpu_percent: float,
    cpu_sustain_seconds: float,
    cpu_sample_seconds: float,
    nice: int,
    recovery: RecoveryConfig | None = None,
) -> dict[str, Any]:
    recovery_config = recovery or RecoveryConfig()
    recovery_config.validate()
    if (
        recovery_config.max_recoveries_per_source > 0
        and recovery_config.low_cpu_percent >= max_host_cpu_percent
    ):
        raise ValueError(
            "recovery low CPU percent must be below the host CPU watchdog ceiling"
        )
    return {
        "sources": list(sources),
        "transactions": ["sale", "lease"],
        "page_cap": page_cap,
        "concurrency": concurrency,
        "source_workers": 1,
        "attempts_per_source": attempts_per_source,
        "max_resume_age_hours": max_resume_age_hours,
        "host_cpu_guard": {
            "max_host_cpu_percent": max_host_cpu_percent,
            "sustain_seconds": cpu_sustain_seconds,
            "sample_seconds": cpu_sample_seconds,
        },
        "resource_recovery": recovery_config.as_dict(),
        "nice": nice,
        "continue_source_local_failures": True,
    }


def new_manifest(
    series_dir: Path,
    *,
    git_sha: str,
    config: Mapping[str, Any],
    database_target: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "series_id": series_dir.name,
        "status": "running",
        "started_at": now,
        "updated_at": now,
        "finished_at": None,
        "collector_git_sha": git_sha,
        "collector_git_dirty": False,
        "database_target": (
            dict(database_target) if database_target is not None else None
        ),
        "config": dict(config),
        "sources": {
            source: {
                "state": "pending",
                "attempts": [],
                "checkpoint_run": None,
                "checkpoint_status": None,
                "error": None,
            }
            for source in config["sources"]
        },
        "resource_recovery": {
            "schema_version": 1,
            "state": "idle",
            "total_recoveries": 0,
            "recoveries_by_source": {source: 0 for source in config["sources"]},
            "cumulative_wait_seconds": 0.0,
            "active": None,
            "last_event": None,
        },
        "error": None,
    }


def save_manifest(series_dir: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = utc_now()
    atomic_write_json(series_dir / "manifest.json", manifest)
    recovery = manifest.get("resource_recovery") or {}
    active = recovery.get("active")
    source_names = set(manifest.get("sources") or {})

    def safe_number(value: Any) -> int | float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            return value if math.isfinite(value) else None
        except OverflowError:
            return None

    def safe_timestamp(value: Any) -> str | None:
        if not isinstance(value, str) or len(value) > 40:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return value if parsed.tzinfo is not None else None

    if isinstance(active, Mapping):
        safe_active = {
            "source": (
                active.get("source") if active.get("source") in source_names else None
            ),
            "reason_code": (
                active.get("reason_code")
                if active.get("reason_code") in RECOVERY_PROGRESS_REASON_CODES
                else None
            ),
            "phase": (
                active.get("phase")
                if active.get("phase") in RECOVERABLE_RESOURCE_PHASES
                else None
            ),
            "active_operation": (
                active.get("active_operation")
                if active.get("active_operation") in RECOVERY_PROGRESS_OPERATIONS
                else None
            ),
            "started_at": safe_timestamp(active.get("started_at")),
            "updated_at": safe_timestamp(active.get("updated_at")),
        }
        for key in (
            "waited_seconds",
            "current_host_cpu_percent",
            "low_cpu_seconds",
            "required_low_cpu_seconds",
            "remaining_cooldown_seconds",
            "source_recoveries",
            "max_source_recoveries",
            "remaining_series_wait_seconds",
            "preserved_detail_count",
        ):
            safe_active[key] = safe_number(active.get(key))
        terminal_reason = active.get("terminal_reason_code")
        if terminal_reason in RECOVERY_PROGRESS_REASON_CODES:
            safe_active["terminal_reason_code"] = terminal_reason
    else:
        safe_active = None
    last_event = recovery.get("last_event")
    if isinstance(last_event, Mapping):
        safe_last_event = {
            "event": (
                last_event.get("event")
                if last_event.get("event") in RECOVERY_PROGRESS_EVENTS
                else None
            ),
            "source": (
                last_event.get("source")
                if last_event.get("source") in source_names
                else None
            ),
            "reason_code": (
                last_event.get("reason_code")
                if last_event.get("reason_code") in RECOVERY_PROGRESS_REASON_CODES
                else None
            ),
            "phase": (
                last_event.get("phase")
                if last_event.get("phase") in RECOVERABLE_RESOURCE_PHASES
                else None
            ),
            "occurred_at": safe_timestamp(last_event.get("occurred_at")),
        }
    else:
        safe_last_event = None
    safe_counts = {
        source: safe_number((recovery.get("recoveries_by_source") or {}).get(source))
        for source in source_names
    }
    atomic_write_json(
        series_dir / "resource-recovery-progress.json",
        {
            "schema_version": 1,
            "series_id": manifest.get("series_id"),
            "series_status": manifest.get("status"),
            "updated_at": manifest.get("updated_at"),
            "state": (
                recovery.get("state")
                if recovery.get("state") in RECOVERY_PROGRESS_STATES
                else None
            ),
            "total_recoveries": safe_number(recovery.get("total_recoveries")),
            "recoveries_by_source": safe_counts,
            "cumulative_wait_seconds": safe_number(
                recovery.get("cumulative_wait_seconds")
            ),
            "active": safe_active,
            "last_event": safe_last_event,
        },
    )
    try:
        publish_series_health(series_dir, manifest)
    except (OSError, RuntimeError) as exc:
        # This redaction-safe handoff is a sidecar, never write-admission state.
        # Report only the exception class: messages can contain URLs or secrets.
        print(
            "warning: producer source-health publication failed "
            f"({type(exc).__name__})",
            file=sys.stderr,
        )


def _validate_persisted_resource_recovery(
    recovery: Any,
    *,
    config: Mapping[str, Any],
    checkpoints: Mapping[str, Any],
) -> None:
    if not isinstance(recovery, Mapping) or recovery.get("schema_version") != 1:
        raise SeriesError("series resource recovery state is malformed")
    configured_sources = set(config["sources"])
    counts = recovery.get("recoveries_by_source")
    if not isinstance(counts, Mapping) or set(counts) != configured_sources:
        raise SeriesError("series resource recovery counters are malformed")
    recovery_config = RecoveryConfig.from_mapping(config["resource_recovery"])
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > recovery_config.max_recoveries_per_source
        for value in counts.values()
    ):
        raise SeriesError("series resource recovery counters are malformed")
    total = recovery.get("total_recoveries")
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or total < 0
        or total != sum(counts.values())
    ):
        raise SeriesError("series resource recovery total is malformed")
    cumulative_wait = recovery.get("cumulative_wait_seconds")
    if (
        isinstance(cumulative_wait, bool)
        or not isinstance(cumulative_wait, (int, float))
        or not 0 <= cumulative_wait <= recovery_config.max_series_cooldown_seconds
    ):
        raise SeriesError("series resource recovery wait is malformed")
    state = recovery.get("state")
    if state not in RECOVERY_PROGRESS_STATES:
        raise SeriesError("series resource recovery state is malformed")
    active = recovery.get("active")
    if state == "idle" and active is not None:
        raise SeriesError("idle resource recovery cannot have an active cooldown")
    if state in {"cooling_down", "resuming"} and not isinstance(active, Mapping):
        raise SeriesError("active resource recovery state is malformed")
    if not isinstance(active, Mapping):
        return
    source = active.get("source")
    checkpoint_run = active.get("checkpoint_run")
    waited = active.get("waited_seconds")
    relative = Path(checkpoint_run) if isinstance(checkpoint_run, str) else None
    if (
        source not in configured_sources
        or relative is None
        or relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != "runs"
        or ".." in relative.parts
        or active.get("reason_code") not in RECOVERABLE_CPU_REASON_CODES
        or active.get("phase") not in RECOVERABLE_RESOURCE_PHASES
        or active.get("active_operation") not in RECOVERY_PROGRESS_OPERATIONS
        or isinstance(waited, bool)
        or not isinstance(waited, (int, float))
        or not 0 <= waited <= recovery_config.max_cooldown_seconds
        or active.get("source_recoveries") != counts[source]
    ):
        raise SeriesError("active resource recovery state is malformed")
    if state in {"cooling_down", "resuming"}:
        checkpoint = checkpoints.get(source)
        if (
            not isinstance(checkpoint, Mapping)
            or checkpoint.get("state") != "resource_guard_interrupted"
            or checkpoint.get("checkpoint_run") != checkpoint_run
        ):
            raise SeriesError("active resource recovery checkpoint is inconsistent")


def load_resume_manifest(
    manifest_path: Path,
    *,
    git_sha: str,
    config: Mapping[str, Any],
    database_target: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SeriesError(f"cannot read series manifest: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise SeriesError("unsupported checkpoint-series manifest")
    if value.get("collector_git_sha") != git_sha:
        raise SeriesError("cannot resume series with a different collector Git SHA")
    if not _exact_json_equal(value.get("config"), dict(config)):
        raise SeriesError("resume configuration differs from the series manifest")
    if database_target is not None and not _exact_json_equal(
        value.get("database_target"), dict(database_target)
    ):
        raise SeriesError("cannot resume series against a different database target")
    checkpoints = value.get("sources")
    if not isinstance(checkpoints, dict) or set(checkpoints) != set(config["sources"]):
        raise SeriesError("series source checkpoints do not match configuration")
    try:
        _validate_persisted_resource_recovery(
            value.get("resource_recovery"),
            config=config,
            checkpoints=checkpoints,
        )
    except ValueError as exc:
        raise SeriesError("series resource recovery state is malformed") from exc
    return value


def build_checkpoint_argv(
    source: str,
    *,
    child_out_root: Path,
    env_file: str | None,
    config: Mapping[str, Any],
    resume_run: Path | None = None,
    fresh_run_id: str | None = None,
) -> list[str]:
    if resume_run is not None and fresh_run_id is not None:
        raise SeriesError("fresh child run-id cannot be combined with resume")
    if fresh_run_id is not None:
        try:
            validate_run_id(fresh_run_id)
        except ValueError as exc:
            raise SeriesError(str(exc)) from exc
    guard = config["host_cpu_guard"]
    argv = [
        "/usr/bin/nice",
        "-n",
        str(config["nice"]),
        sys.executable,
        str(COLLECTOR_DIR / "cre_checkpoint_refresh.py"),
        "--out-root",
        str(child_out_root),
        "--sources",
        source,
        "--transactions",
        "both",
        "--page-cap",
        str(config["page_cap"]),
        "--concurrency",
        str(config["concurrency"]),
        "--source-workers",
        "1",
        "--attempts-per-source",
        str(config["attempts_per_source"]),
        "--max-resume-age-hours",
        str(config["max_resume_age_hours"]),
        "--max-host-cpu-percent",
        str(guard["max_host_cpu_percent"]),
        "--cpu-sustain-seconds",
        str(guard["sustain_seconds"]),
        "--cpu-sample-seconds",
        str(guard["sample_seconds"]),
    ]
    if resume_run is not None:
        argv.extend(["--resume", str(resume_run)])
    elif fresh_run_id is not None:
        argv.extend(["--run-id", fresh_run_id])
    if env_file:
        argv.extend(["--env-file", env_file])
    return argv


def _expected_child_config(
    source: str,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    guard = config["host_cpu_guard"]
    return {
        "sources": [source],
        "transactions": ["sale", "lease"],
        "max_items": 0,
        "page_cap": config["page_cap"],
        "concurrency": config["concurrency"],
        "source_workers": 1,
        "host_cpu_guard": {
            "max_host_cpu_percent": guard["max_host_cpu_percent"],
            "sustain_seconds": guard["sustain_seconds"],
            "sample_seconds": guard["sample_seconds"],
            "action": "interrupt_and_checkpoint",
            "telemetry_failure_action": "interrupt_and_checkpoint",
        },
        "additive": True,
        "status_activation": False,
        "mark_missing": False,
        "admit_baseline_hold_additively": False,
    }


def _parse_utc_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SeriesError(f"{field} is missing")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SeriesError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise SeriesError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_resource_recovery_admission(
    series_dir: Path,
    manifest: Mapping[str, Any],
    child_manifest_path: Path,
    child_manifest: Mapping[str, Any],
    *,
    source: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Admit only an exact, terminal, confirmed CPU stop before writes."""
    if child_manifest.get("status") != RESOURCE_GUARD_STATUS:
        raise SeriesError("child checkpoint is not a terminal resource stop")
    resolved_path = child_manifest_path.resolve()
    expected_root = (series_dir / "runs").resolve()
    if (
        resolved_path.name != "manifest.json"
        or resolved_path.parent.parent != expected_root
    ):
        raise SeriesError("child checkpoint path is outside the series run root")
    if child_manifest.get("run_id") != resolved_path.parent.name:
        raise SeriesError(
            "child checkpoint generation identity does not match its path"
        )
    if child_manifest.get("collector_git_sha") != manifest.get("collector_git_sha"):
        raise SeriesError(
            "child checkpoint collector Git SHA does not match the series"
        )
    config = manifest.get("config") or {}
    if not _exact_json_equal(
        child_manifest.get("config"), _expected_child_config(source, config)
    ):
        raise SeriesError("child checkpoint configuration does not match the series")
    database_target = manifest.get("database_target")
    if not isinstance(database_target, Mapping):
        raise SeriesError("series database target is not bound")
    child_preflight = child_manifest.get("preflight")
    if not isinstance(child_preflight, Mapping) or not _exact_json_equal(
        child_preflight.get("database_target"), dict(database_target)
    ):
        raise SeriesError("child checkpoint database target does not match the series")

    started_at = _parse_utc_timestamp(
        child_manifest.get("started_at"), field="child started_at"
    )
    finished_at = _parse_utc_timestamp(
        child_manifest.get("finished_at"), field="child finished_at"
    )
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    future_limit = current + timedelta(minutes=5)
    if (
        started_at > future_limit
        or finished_at > future_limit
        or finished_at < started_at
    ):
        raise SeriesError("child checkpoint timestamps are inconsistent")
    max_age = float(config["max_resume_age_hours"])
    if current - started_at > timedelta(hours=max_age):
        raise SeriesError("child checkpoint generation_expired")

    stop = child_manifest.get("resource_stop")
    if not isinstance(stop, dict) or stop.get("schema_version") != 1:
        raise SeriesError("child checkpoint lacks a typed resource stop")
    if stop.get("reason_code") not in RECOVERABLE_CPU_REASON_CODES:
        raise SeriesError("child resource stop is not recoverable CPU saturation")
    if stop.get("phase") not in RECOVERABLE_RESOURCE_PHASES:
        raise SeriesError(
            "child resource stop occurred outside preflight or collection"
        )
    if stop.get("source") != source:
        raise SeriesError(
            "child resource stop source does not match the series checkpoint"
        )
    if stop.get("generation_id") != child_manifest.get("run_id"):
        raise SeriesError("child resource stop generation identity does not match")
    occurred_at = _parse_utc_timestamp(
        stop.get("occurred_at"), field="resource stop occurred_at"
    )
    recorded_at = _parse_utc_timestamp(
        stop.get("recorded_at"), field="resource stop recorded_at"
    )
    if not started_at <= occurred_at <= recorded_at <= finished_at:
        raise SeriesError("child resource stop timestamps are inconsistent")
    if (
        stop.get("telemetry_valid") is not True
        or stop.get("evidence_valid") is not True
        or stop.get("owned_processes_reaped") is not True
    ):
        raise SeriesError("child resource stop lacks valid terminal CPU evidence")
    observed_cpu = stop.get("host_cpu_percent")
    if isinstance(observed_cpu, bool):
        raise SeriesError("child resource stop CPU sample is invalid")
    try:
        observed_cpu = float(observed_cpu)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SeriesError("child resource stop CPU sample is invalid") from exc
    ceiling = float(config["host_cpu_guard"]["max_host_cpu_percent"])
    if (
        not math.isfinite(observed_cpu)
        or not 0 <= observed_cpu <= 100
        or observed_cpu < ceiling
    ):
        raise SeriesError("child resource stop does not prove CPU saturation")

    operation = stop.get("active_operation")
    allowed_operations = {
        "preflight": {
            "host_cpu_admission",
            "healthcheck",
            "pre_validation",
            "source_collection_preflight",
        },
        "collection": {"source_collection"},
    }
    if operation not in allowed_operations[stop["phase"]]:
        raise SeriesError("child resource stop active operation is not recoverable")
    child_sources = child_manifest.get("sources")
    if not isinstance(child_sources, Mapping) or set(child_sources) != {source}:
        raise SeriesError("child resource stop source checkpoint is malformed")
    checkpoint = child_sources[source]
    if not isinstance(checkpoint, Mapping):
        raise SeriesError("child resource stop source checkpoint is malformed")
    if stop["phase"] == "collection" and checkpoint.get("state") != "collecting":
        raise SeriesError("child resource stop does not prove active collection")
    if stop["phase"] == "preflight" and checkpoint.get("state") not in {
        "pending",
        "collecting",
    }:
        raise SeriesError("child resource stop cannot resume a post-collection stage")
    forbidden_states = {"ingesting", "ingest_recovery_required", "ingested"}
    if (
        checkpoint.get("state") in forbidden_states
        or checkpoint.get("ingest") is not None
    ):
        raise SeriesError("child resource stop crossed the live-write boundary")
    if checkpoint.get("readback") is not None:
        raise SeriesError("child resource stop crossed the readback boundary")
    if (
        child_manifest.get("aggregate_gate") is not None
        or child_manifest.get("validation") is not None
    ):
        raise SeriesError("child resource stop crossed collection-only admission")
    return stop


def _preserved_detail_count(child_run: Path, source: str) -> int | None:
    """Return only counts with a source-specific durable detail-cache meaning."""
    try:
        if source == "jll":
            cache = child_run / "cache" / "jll-detail"
            return sum(1 for path in cache.glob("*.json") if path.is_file())
        if source == "colliers-main":
            cache = child_run / "cache" / "colliers-main" / "detail-cache.jsonl"
            if not cache.is_file():
                return 0
            return sum(
                1
                for line in cache.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
    except OSError:
        return None
    return None


def source_local_failure(child_manifest: Mapping[str, Any]) -> bool:
    if child_manifest.get("status") != "failed":
        return False
    error = child_manifest.get("error")
    return isinstance(error, str) and error.startswith(SOURCE_LOCAL_FAILURE_PREFIXES)


def _load_child_manifest(
    child_out_root: Path,
    *,
    before: set[Path],
    source: str,
    git_sha: str,
    expected_path: Path | None = None,
    expected_config: Mapping[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    if expected_path is not None:
        resolved_root = child_out_root.resolve()
        resolved_path = expected_path.resolve()
        if (
            resolved_path.name != "manifest.json"
            or resolved_path.parent.parent != resolved_root
        ):
            raise SeriesError(
                f"bound checkpoint manifest escaped runs root for {source}"
            )
        value = json.loads(resolved_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise SeriesError("checkpoint manifest root must be an object")
        if expected_config is not None and (
            value.get("run_id") != resolved_path.parent.name
            or value.get("collector_git_sha") != git_sha
            or not _exact_json_equal(value.get("config"), expected_config)
        ):
            raise SeriesError(
                f"bound checkpoint manifest identity mismatch for {source}"
            )
        return resolved_path, value

    candidates: list[tuple[Path, dict[str, Any]]] = []
    for manifest_path in child_out_root.glob("*/manifest.json"):
        if manifest_path in before:
            continue
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if value.get("collector_git_sha") == git_sha and (
            value.get("config") or {}
        ).get("sources") == [source]:
            candidates.append((manifest_path, value))
    if len(candidates) != 1:
        raise SeriesError(
            f"expected exactly one new checkpoint manifest for {source}; "
            f"found {len(candidates)}"
        )
    return candidates[0]


def _reconciled_state(child_manifest: Mapping[str, Any]) -> str:
    status = child_manifest.get("status")
    if status == SUCCESS_STATUS:
        return "complete"
    if status == RESOURCE_GUARD_STATUS:
        return "resource_guard_interrupted"
    if status == "interrupted":
        return "interrupted"
    if source_local_failure(child_manifest):
        return "failed_source"
    return "failed_global"


def reconcile_stale_running_sources(series_dir: Path, manifest: dict[str, Any]) -> None:
    """Repair only explicitly requested, provable parent/child interruption drift.

    A terminal child can outlive an interrupted series driver.  Never guess at
    ownership: each stale parent must have exactly one child from its current
    outer attempt, on the same collector SHA and one-source configuration.
    """
    child_root = series_dir / "runs"
    git_sha = manifest["collector_git_sha"]
    for source, checkpoint in manifest["sources"].items():
        if checkpoint.get("state") != "running":
            continue
        attempts = checkpoint.get("attempts") or []
        if not attempts or not isinstance(attempts[-1], Mapping):
            raise SeriesError(
                f"cannot reconcile stale {source}: missing current attempt"
            )
        attempt_started = attempts[-1].get("started_at")
        if not isinstance(attempt_started, str):
            raise SeriesError(f"cannot reconcile stale {source}: missing attempt start")
        attempt_started_at = _parse_utc_timestamp(
            attempt_started, field="outer attempt started_at"
        )
        recorded_run = checkpoint.get("checkpoint_run")
        exact_recorded_child = isinstance(recorded_run, str) and bool(recorded_run)
        if exact_recorded_child:
            relative = Path(recorded_run)
            if (
                relative.is_absolute()
                or len(relative.parts) != 2
                or relative.parts[0] != "runs"
                or ".." in relative.parts
            ):
                raise SeriesError(
                    f"cannot reconcile stale {source}: invalid recorded checkpoint run"
                )
            paths = [(series_dir / relative / "manifest.json").resolve()]
            if paths[0].parent.parent != child_root.resolve():
                raise SeriesError(
                    f"cannot reconcile stale {source}: checkpoint run escaped series"
                )
        else:
            paths = list(child_root.glob("*/manifest.json"))

        matches: list[tuple[Path, dict[str, Any]]] = []
        for path in paths:
            try:
                child = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(child, dict):
                    continue
                finished_at = _parse_utc_timestamp(
                    child.get("finished_at"), field="child finished_at"
                )
                child_started_at = _parse_utc_timestamp(
                    child.get("started_at"), field="child started_at"
                )
            except (OSError, json.JSONDecodeError, SeriesError):
                continue
            if finished_at < attempt_started_at or finished_at < child_started_at:
                continue
            terminal = child.get("status") in {
                SUCCESS_STATUS,
                RESOURCE_GUARD_STATUS,
                "interrupted",
                "failed",
            }
            exact_identity = (
                child.get("collector_git_sha") == git_sha
                and child.get("run_id") == path.parent.name
                and _exact_json_equal(
                    child.get("config"),
                    _expected_child_config(source, manifest["config"]),
                )
                and isinstance(child.get("finished_at"), str)
            )
            parent_database_target = manifest.get("database_target")
            child_preflight = child.get("preflight")
            exact_target = not isinstance(parent_database_target, Mapping) or (
                isinstance(child_preflight, Mapping)
                and _exact_json_equal(
                    child_preflight.get("database_target"), parent_database_target
                )
            )
            legacy_identity = (
                not exact_recorded_child
                and (child.get("config") or {}).get("sources") == [source]
                and isinstance(child.get("started_at"), str)
                and child_started_at >= attempt_started_at
            )
            if (
                child.get("collector_git_sha") == git_sha
                and terminal
                and (
                    (exact_recorded_child and exact_identity and exact_target)
                    or legacy_identity
                )
            ):
                matches.append((path, child))
        if len(matches) != 1:
            raise SeriesError(
                f"cannot reconcile stale {source}: expected exactly one terminal child, found {len(matches)}"
            )
        path, child = matches[0]
        checkpoint["checkpoint_run"] = str(path.parent.relative_to(series_dir))
        checkpoint["checkpoint_status"] = child.get("status")
        checkpoint["state"] = _reconciled_state(child)
        checkpoint["error"] = child.get("error")
        attempts[-1]["finished_at"] = child.get("finished_at") or utc_now()
        save_manifest(series_dir, manifest)


def _terminate_child(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=SERIES_INTERRUPT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def checkpoint_series_sigterm_handler(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _resource_recovery_state(manifest: Mapping[str, Any]) -> dict[str, Any]:
    recovery = manifest.get("resource_recovery")
    if not isinstance(recovery, dict) or recovery.get("schema_version") != 1:
        raise SeriesError("series resource recovery state is malformed")
    return recovery


def _start_or_continue_cooldown(
    series_dir: Path,
    manifest: dict[str, Any],
    *,
    source: str,
    child_manifest_path: Path,
    stop: Mapping[str, Any],
) -> dict[str, Any]:
    recovery = _resource_recovery_state(manifest)
    config = RecoveryConfig.from_mapping(manifest["config"]["resource_recovery"])
    relative_child_run = str(child_manifest_path.parent.relative_to(series_dir))
    checkpoint = manifest["sources"].get(source)
    if (
        not isinstance(checkpoint, Mapping)
        or checkpoint.get("state") != "resource_guard_interrupted"
        or checkpoint.get("checkpoint_run") != relative_child_run
    ):
        raise SeriesError("resource cooldown checkpoint ownership is inconsistent")
    active = recovery.get("active")
    if recovery.get("state") == "cooling_down":
        if (
            recovery.get("state") != "cooling_down"
            or not isinstance(active, dict)
            or active.get("source") != source
            or active.get("checkpoint_run") != relative_child_run
            or active.get("reason_code") != stop.get("reason_code")
            or active.get("phase") != stop.get("phase")
        ):
            raise SeriesError("persisted resource cooldown ownership is inconsistent")
        manifest["status"] = "cooling_down"
        manifest["finished_at"] = None
        manifest["error"] = None
        save_manifest(series_dir, manifest)
        return active
    if recovery.get("state") == "resuming":
        raise SeriesError("resource recovery is already admitted for child resume")

    source_recoveries = int(recovery["recoveries_by_source"].get(source, 0))
    cumulative_wait = float(recovery.get("cumulative_wait_seconds") or 0.0)
    if config.max_recoveries_per_source <= 0:
        raise RecoveryBudgetExhausted("resource recovery is disabled")
    if source_recoveries >= config.max_recoveries_per_source:
        raise RecoveryBudgetExhausted("source recovery count is exhausted")
    if cumulative_wait >= config.max_series_cooldown_seconds:
        raise RecoveryBudgetExhausted("series recovery wait budget is exhausted")

    source_recoveries += 1
    recovery["recoveries_by_source"][source] = source_recoveries
    recovery["total_recoveries"] = int(recovery.get("total_recoveries") or 0) + 1
    active = {
        "source": source,
        "checkpoint_run": relative_child_run,
        "reason_code": stop["reason_code"],
        "phase": stop["phase"],
        "active_operation": stop["active_operation"],
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "waited_seconds": 0.0,
        "current_host_cpu_percent": stop["host_cpu_percent"],
        "low_cpu_seconds": 0.0,
        "required_low_cpu_seconds": config.low_cpu_seconds,
        "remaining_cooldown_seconds": config.max_cooldown_seconds,
        "source_recoveries": source_recoveries,
        "max_source_recoveries": config.max_recoveries_per_source,
        "remaining_series_wait_seconds": max(
            0.0, config.max_series_cooldown_seconds - cumulative_wait
        ),
        "preserved_detail_count": _preserved_detail_count(
            child_manifest_path.parent, source
        ),
    }
    recovery["state"] = "cooling_down"
    recovery["active"] = active
    recovery["last_event"] = {
        "event": "cooldown_started",
        "source": source,
        "reason_code": stop["reason_code"],
        "phase": stop["phase"],
        "occurred_at": active["started_at"],
    }
    manifest["status"] = "cooling_down"
    manifest["finished_at"] = None
    manifest["error"] = None
    save_manifest(series_dir, manifest)
    return active


def _finish_cooldown_failure(
    series_dir: Path,
    manifest: dict[str, Any],
    *,
    source: str,
    reason_code: str,
    cancelled: bool = False,
) -> int:
    recovery = _resource_recovery_state(manifest)
    recovery["state"] = "failed" if cancelled else "exhausted"
    active = recovery.get("active")
    if isinstance(active, dict):
        active["updated_at"] = utc_now()
        active["terminal_reason_code"] = reason_code
    recovery["last_event"] = {
        "event": "cooldown_stopped",
        "source": source,
        "reason_code": reason_code,
        "occurred_at": utc_now(),
    }
    checkpoint = manifest["sources"][source]
    checkpoint["state"] = "interrupted" if cancelled else "resource_guard_interrupted"
    if cancelled:
        manifest["status"] = "interrupted"
        manifest["error"] = "operator interruption during resource cooldown"
        rc = 130
    else:
        manifest["status"] = RESOURCE_GUARD_STATUS
        manifest["error"] = "bounded host CPU recovery stopped without resume"
        rc = 75
    manifest["finished_at"] = utc_now()
    save_manifest(series_dir, manifest)
    return rc


def _record_recovery_nonadmission(
    series_dir: Path,
    manifest: dict[str, Any],
    *,
    source: str,
    reason_code: str,
    exhausted: bool = False,
) -> int:
    recovery = _resource_recovery_state(manifest)
    recovery["state"] = "exhausted" if exhausted else "failed"
    recovery["active"] = None
    recovery["last_event"] = {
        "event": "recovery_not_admitted",
        "source": source,
        "reason_code": reason_code,
        "occurred_at": utc_now(),
    }
    manifest["sources"][source]["state"] = "resource_guard_interrupted"
    manifest["status"] = RESOURCE_GUARD_STATUS
    manifest["error"] = "host CPU stop was not admitted for automatic recovery"
    manifest["finished_at"] = utc_now()
    save_manifest(series_dir, manifest)
    return 75


def _cool_down_for_child_resume(
    series_dir: Path,
    manifest: dict[str, Any],
    *,
    source: str,
    child_manifest_path: Path,
    child_manifest: Mapping[str, Any],
    env_file: str | None,
) -> int | None:
    """Persist, wait, and revalidate one exact child before `--resume`."""
    stop = validate_resource_recovery_admission(
        series_dir,
        manifest,
        child_manifest_path,
        child_manifest,
        source=source,
        now=_now_utc(),
    )
    active = _start_or_continue_cooldown(
        series_dir,
        manifest,
        source=source,
        child_manifest_path=child_manifest_path,
        stop=stop,
    )
    recovery = _resource_recovery_state(manifest)
    config = RecoveryConfig.from_mapping(manifest["config"]["resource_recovery"])
    initial_active_wait = float(active.get("waited_seconds") or 0.0)
    initial_series_wait = float(recovery.get("cumulative_wait_seconds") or 0.0)

    def persist_progress(progress: CooldownProgress) -> None:
        validate_resource_recovery_admission(
            series_dir,
            manifest,
            child_manifest_path,
            child_manifest,
            source=source,
            now=_now_utc(),
        )
        elapsed_this_process = max(0.0, progress.waited_seconds - initial_active_wait)
        recovery["cumulative_wait_seconds"] = round(
            initial_series_wait + elapsed_this_process, 3
        )
        active.update(progress.as_dict())
        active["updated_at"] = progress.observed_at
        save_manifest(series_dir, manifest)

    try:
        result = wait_for_cpu_recovery(
            config,
            already_waited_seconds=initial_active_wait,
            series_waited_seconds=initial_series_wait,
            sampler=read_host_cpu_percent,
            monotonic=time.monotonic,
            sleep=time.sleep,
            utc_now=utc_now,
            on_progress=persist_progress,
        )
    except KeyboardInterrupt:
        return _finish_cooldown_failure(
            series_dir,
            manifest,
            source=source,
            reason_code="operator_cancelled",
            cancelled=True,
        )
    except RecoveryCancelled:
        return _finish_cooldown_failure(
            series_dir,
            manifest,
            source=source,
            reason_code="operator_cancelled",
            cancelled=True,
        )
    except RecoveryBudgetExhausted:
        return _finish_cooldown_failure(
            series_dir,
            manifest,
            source=source,
            reason_code="recovery_budget_exhausted",
        )
    except RecoveryTelemetryError:
        return _finish_cooldown_failure(
            series_dir,
            manifest,
            source=source,
            reason_code="recovery_telemetry_failed",
        )
    except RecoveryEvidenceError:
        try:
            return _finish_cooldown_failure(
                series_dir,
                manifest,
                source=source,
                reason_code="recovery_evidence_failed",
            )
        except OSError:
            return 1
    except SeriesError as exc:
        reason_code = (
            "generation_expired"
            if "generation_expired" in str(exc)
            else "recovery_admission_changed"
        )
        return _finish_cooldown_failure(
            series_dir,
            manifest,
            source=source,
            reason_code=reason_code,
        )

    current_sha, current_dirty = git_identity()
    if current_dirty or current_sha != manifest["collector_git_sha"]:
        return _finish_cooldown_failure(
            series_dir,
            manifest,
            source=source,
            reason_code="collector_identity_changed",
        )
    try:
        current_database_target = database_target_fingerprint(env_file)
    except (OSError, RefreshError, SystemExit):
        current_database_target = None
    if current_database_target != manifest.get("database_target"):
        return _finish_cooldown_failure(
            series_dir,
            manifest,
            source=source,
            reason_code="database_target_changed",
        )
    try:
        reloaded = json.loads(child_manifest_path.read_text(encoding="utf-8"))
        if not isinstance(reloaded, dict):
            raise SeriesError("child checkpoint manifest root is invalid")
        validate_resource_recovery_admission(
            series_dir,
            manifest,
            child_manifest_path,
            reloaded,
            source=source,
            now=_now_utc(),
        )
    except (OSError, json.JSONDecodeError, SeriesError):
        return _finish_cooldown_failure(
            series_dir,
            manifest,
            source=source,
            reason_code="recovery_admission_changed",
        )

    recovery["state"] = "resuming"
    active.update(
        {
            "updated_at": result.observed_at,
            "waited_seconds": round(result.waited_seconds, 3),
            "current_host_cpu_percent": round(result.final_host_cpu_percent, 2),
            "low_cpu_seconds": round(result.low_cpu_seconds, 3),
            "remaining_cooldown_seconds": max(
                0.0, config.max_cooldown_seconds - result.waited_seconds
            ),
        }
    )
    recovery["last_event"] = {
        "event": "cooldown_complete",
        "source": source,
        "reason_code": stop["reason_code"],
        "phase": stop["phase"],
        "occurred_at": result.observed_at,
    }
    manifest["status"] = "running"
    manifest["finished_at"] = None
    manifest["error"] = None
    save_manifest(series_dir, manifest)
    return None


def run_series(
    series_dir: Path,
    manifest: dict[str, Any],
    *,
    env_file: str | None,
    retry_failed: bool,
) -> int:
    config = manifest["config"]
    git_sha = manifest["collector_git_sha"]
    child_out_root = series_dir / "runs"
    child_out_root.mkdir(parents=True, exist_ok=True)
    log_root = series_dir / "logs"
    log_root.mkdir(parents=True, exist_ok=True)

    for index, source in enumerate(config["sources"], start=1):
        checkpoint = manifest["sources"][source]
        prior_state = checkpoint["state"]
        if prior_state == "complete":
            continue
        if prior_state == "failed_source" and not retry_failed:
            continue

        resume_run: Path | None = None
        if prior_state in {
            "resource_guard_interrupted",
            "interrupted",
        } and checkpoint.get("checkpoint_run"):
            resume_run = series_dir / checkpoint["checkpoint_run"]
        if prior_state == "interrupted":
            missing_child_error = (
                "interrupted child startup lacks a readable bound manifest; "
                "refusing automatic replay"
            )
            try:
                if resume_run is None:
                    raise SeriesError(missing_child_error)
                _load_child_manifest(
                    child_out_root,
                    before=set(),
                    source=source,
                    git_sha=git_sha,
                    expected_path=resume_run / "manifest.json",
                )
            except (OSError, json.JSONDecodeError, SeriesError):
                checkpoint["error"] = missing_child_error
                manifest["status"] = "interrupted"
                manifest["error"] = missing_child_error
                manifest["finished_at"] = utc_now()
                save_manifest(series_dir, manifest)
                return 130

        recovery = _resource_recovery_state(manifest)
        active = recovery.get("active")
        if recovery.get("state") == "cooling_down":
            if not isinstance(active, Mapping) or active.get("source") != source:
                return _record_recovery_nonadmission(
                    series_dir,
                    manifest,
                    source=source,
                    reason_code="recovery_ownership_mismatch",
                )
            if resume_run is None:
                return _record_recovery_nonadmission(
                    series_dir,
                    manifest,
                    source=source,
                    reason_code="recovery_checkpoint_missing",
                )
            child_manifest_path = resume_run / "manifest.json"
            try:
                child_manifest = json.loads(
                    child_manifest_path.read_text(encoding="utf-8")
                )
                if not isinstance(child_manifest, dict):
                    raise SeriesError("checkpoint manifest root must be an object")
                cooldown_rc = _cool_down_for_child_resume(
                    series_dir,
                    manifest,
                    source=source,
                    child_manifest_path=child_manifest_path,
                    child_manifest=child_manifest,
                    env_file=env_file,
                )
            except (OSError, json.JSONDecodeError, SeriesError):
                return _record_recovery_nonadmission(
                    series_dir,
                    manifest,
                    source=source,
                    reason_code="recovery_admission_changed",
                )
            if cooldown_rc is not None:
                return cooldown_rc

        while True:
            attempt_number = len(checkpoint["attempts"]) + 1
            log_path = log_root / f"{index:02d}-{source}-attempt-{attempt_number}.log"
            fresh_run_id: str | None = None
            if resume_run is None:
                fresh_run_id = allocate_run_id()
                try:
                    validate_run_id(fresh_run_id)
                except ValueError as exc:
                    raise SeriesError(str(exc)) from exc
                child_run = child_out_root / fresh_run_id
            else:
                child_run = resume_run
            child_manifest_path = child_run / "manifest.json"
            attempt = {
                "number": attempt_number,
                "started_at": utc_now(),
                "finished_at": None,
                "rc": None,
                "log": str(log_path.relative_to(series_dir)),
            }
            checkpoint["attempts"].append(attempt)
            checkpoint["state"] = "running"
            checkpoint["checkpoint_run"] = str(child_run.relative_to(series_dir))
            checkpoint["checkpoint_status"] = None
            checkpoint["error"] = None
            recovery = _resource_recovery_state(manifest)
            if recovery.get("state") == "resuming":
                recovery["state"] = "idle"
                recovery["active"] = None
                recovery["last_event"] = {
                    "event": "child_resume_started",
                    "source": source,
                    "reason_code": "cpu_cooldown_complete",
                    "occurred_at": utc_now(),
                }
            save_manifest(series_dir, manifest)

            argv = build_checkpoint_argv(
                source,
                child_out_root=child_out_root,
                env_file=env_file,
                config=config,
                resume_run=resume_run,
                fresh_run_id=fresh_run_id,
            )
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"[{utc_now()}] command: {' '.join(argv)}\n")
                log.flush()
                try:
                    proc = subprocess.Popen(
                        argv,
                        cwd=COLLECTOR_DIR,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        text=True,
                        start_new_session=True,
                    )
                except KeyboardInterrupt:
                    attempt["finished_at"] = utc_now()
                    attempt["rc"] = 130
                    checkpoint["state"] = "interrupted"
                    checkpoint["error"] = "operator interruption before child start"
                    manifest["status"] = "interrupted"
                    manifest["error"] = f"interrupted while starting {source}"
                    manifest["finished_at"] = utc_now()
                    save_manifest(series_dir, manifest)
                    return 130
                except OSError:
                    attempt["finished_at"] = utc_now()
                    checkpoint["state"] = "failed_global"
                    checkpoint["error"] = "checkpoint child process failed to start"
                    manifest["status"] = "failed"
                    manifest["error"] = f"failed to start checkpoint child for {source}"
                    manifest["finished_at"] = utc_now()
                    save_manifest(series_dir, manifest)
                    return 1
                try:
                    rc = proc.wait()
                except KeyboardInterrupt:
                    _terminate_child(proc)
                    attempt["finished_at"] = utc_now()
                    attempt["rc"] = 130
                    checkpoint["state"] = "interrupted"
                    checkpoint["error"] = "operator interruption"
                    manifest["status"] = "interrupted"
                    manifest["error"] = f"interrupted while running {source}"
                    manifest["finished_at"] = utc_now()
                    save_manifest(series_dir, manifest)
                    return 130
                log.write(f"[{utc_now()}] rc={rc}\n")

            attempt["finished_at"] = utc_now()
            attempt["rc"] = rc
            try:
                child_manifest_path, child_manifest = _load_child_manifest(
                    child_out_root,
                    before=set(),
                    source=source,
                    git_sha=git_sha,
                    expected_path=child_manifest_path,
                    expected_config=(
                        _expected_child_config(source, config)
                        if fresh_run_id is not None
                        else None
                    ),
                )
            except (OSError, json.JSONDecodeError, SeriesError) as exc:
                checkpoint["state"] = "failed_global"
                checkpoint["error"] = str(exc)
                manifest["status"] = "failed"
                manifest["error"] = str(exc)
                manifest["finished_at"] = utc_now()
                save_manifest(series_dir, manifest)
                return rc or 1

            checkpoint["checkpoint_status"] = child_manifest.get("status")
            checkpoint["error"] = child_manifest.get("error")

            if rc == 0 and child_manifest.get("status") == SUCCESS_STATUS:
                checkpoint["state"] = "complete"
                save_manifest(series_dir, manifest)
                break
            if rc == 75 or child_manifest.get("status") == RESOURCE_GUARD_STATUS:
                checkpoint["state"] = "resource_guard_interrupted"
                manifest["status"] = RESOURCE_GUARD_STATUS
                manifest["error"] = "host CPU guard interrupted a checkpoint child"
                manifest["finished_at"] = utc_now()
                save_manifest(series_dir, manifest)
                recovery_config = RecoveryConfig.from_mapping(
                    config["resource_recovery"]
                )
                if recovery_config.max_recoveries_per_source <= 0:
                    return _record_recovery_nonadmission(
                        series_dir,
                        manifest,
                        source=source,
                        reason_code="recovery_disabled",
                        exhausted=True,
                    )
                try:
                    cooldown_rc = _cool_down_for_child_resume(
                        series_dir,
                        manifest,
                        source=source,
                        child_manifest_path=child_manifest_path,
                        child_manifest=child_manifest,
                        env_file=env_file,
                    )
                except RecoveryBudgetExhausted:
                    return _record_recovery_nonadmission(
                        series_dir,
                        manifest,
                        source=source,
                        reason_code="recovery_budget_exhausted",
                        exhausted=True,
                    )
                except (SeriesError, ValueError):
                    return _record_recovery_nonadmission(
                        series_dir,
                        manifest,
                        source=source,
                        reason_code="recovery_not_admitted",
                    )
                if cooldown_rc is not None:
                    return cooldown_rc
                resume_run = child_manifest_path.parent
                continue
            if source_local_failure(child_manifest):
                checkpoint["state"] = "failed_source"
                save_manifest(series_dir, manifest)
                break

            checkpoint["state"] = "failed_global"
            manifest["status"] = "failed"
            manifest["error"] = (
                f"global checkpoint failure for {source}: "
                f"{child_manifest.get('error') or f'exit {rc}'}"
            )
            manifest["finished_at"] = utc_now()
            save_manifest(series_dir, manifest)
            return rc or 1

    failed_sources = [
        source
        for source, checkpoint in manifest["sources"].items()
        if checkpoint["state"] == "failed_source"
    ]
    manifest["status"] = (
        "complete_with_source_failures" if failed_sources else "complete"
    )
    manifest["error"] = (
        "source-local failures: " + ", ".join(failed_sources)
        if failed_sources
        else None
    )
    manifest["finished_at"] = utc_now()
    save_manifest(series_dir, manifest)
    print(series_dir)
    return 2 if failed_sources else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", default=None)
    parser.add_argument(
        "--reconcile-stale-series",
        action="store_true",
        help="explicitly repair a stale running parent from exactly one terminal child before resume",
    )
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--sources", default="all")
    parser.add_argument("--page-cap", type=int, default=400)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--attempts-per-source", type=int, default=3)
    parser.add_argument(
        "--max-resume-age-hours",
        type=float,
        default=DEFAULT_MAX_RESUME_AGE_HOURS,
    )
    parser.add_argument(
        "--max-host-cpu-percent",
        type=float,
        default=DEFAULT_MAX_HOST_CPU_PERCENT,
    )
    parser.add_argument(
        "--cpu-sustain-seconds",
        type=float,
        default=DEFAULT_CPU_SUSTAIN_SECONDS,
    )
    parser.add_argument(
        "--cpu-sample-seconds",
        type=float,
        default=DEFAULT_CPU_SAMPLE_SECONDS,
    )
    parser.add_argument(
        "--max-resource-recoveries-per-source",
        type=int,
        default=DEFAULT_MAX_RECOVERIES_PER_SOURCE,
        help=(
            "opt-in foreground CPU recoveries per source (default: 0 disabled; "
            f"maximum: {MAX_RECOVERIES_PER_SOURCE})"
        ),
    )
    parser.add_argument(
        "--recovery-low-cpu-percent",
        type=float,
        default=DEFAULT_LOW_CPU_PERCENT,
    )
    parser.add_argument(
        "--recovery-low-cpu-seconds",
        type=float,
        default=DEFAULT_LOW_CPU_SECONDS,
    )
    parser.add_argument(
        "--recovery-cpu-sample-seconds",
        type=float,
        default=DEFAULT_SAMPLE_SECONDS,
    )
    parser.add_argument(
        "--max-recovery-cooldown-seconds",
        type=float,
        default=DEFAULT_MAX_COOLDOWN_SECONDS,
        help=f"bounded cooldown time (maximum: {MAX_COOLDOWN_SECONDS:g}s)",
    )
    parser.add_argument(
        "--max-series-recovery-seconds",
        type=float,
        default=DEFAULT_MAX_SERIES_COOLDOWN_SECONDS,
        help=(
            "cumulative series cooldown budget "
            f"(maximum: {MAX_SERIES_COOLDOWN_SECONDS:g}s)"
        ),
    )
    parser.add_argument("--nice", type=int, default=10)
    args = parser.parse_args(argv)

    try:
        sources = parse_sources(args.sources)
    except ValueError as exc:
        parser.error(str(exc))
    if args.page_cap < 1 or not 1 <= args.concurrency <= 6:
        parser.error(
            "page-cap must be positive and concurrency must be between 1 and 6"
        )
    if args.attempts_per_source < 1:
        parser.error("attempts-per-source must be positive")
    if not math.isfinite(args.max_resume_age_hours) or args.max_resume_age_hours <= 0:
        parser.error("max-resume-age-hours must be finite and positive")
    if (
        not math.isfinite(args.max_host_cpu_percent)
        or not 0 < args.max_host_cpu_percent < 100
    ):
        parser.error("max-host-cpu-percent must be finite and between 0 and 100")
    if not math.isfinite(args.cpu_sustain_seconds) or args.cpu_sustain_seconds <= 0:
        parser.error("cpu-sustain-seconds must be finite and positive")
    if (
        not math.isfinite(args.cpu_sample_seconds)
        or args.cpu_sample_seconds <= 0
        or args.cpu_sample_seconds > args.cpu_sustain_seconds
    ):
        parser.error(
            "cpu-sample-seconds must be positive and no greater than cpu-sustain-seconds"
        )
    if not 0 <= args.nice <= 20:
        parser.error("nice must be between 0 and 20")
    recovery_config = RecoveryConfig(
        max_recoveries_per_source=args.max_resource_recoveries_per_source,
        low_cpu_percent=args.recovery_low_cpu_percent,
        low_cpu_seconds=args.recovery_low_cpu_seconds,
        sample_seconds=args.recovery_cpu_sample_seconds,
        max_cooldown_seconds=args.max_recovery_cooldown_seconds,
        max_series_cooldown_seconds=args.max_series_recovery_seconds,
    )
    try:
        recovery_config.validate()
    except ValueError as exc:
        parser.error(str(exc))
    if (
        recovery_config.max_recoveries_per_source > 0
        and recovery_config.low_cpu_percent >= args.max_host_cpu_percent
    ):
        parser.error(
            "recovery-low-cpu-percent must be below max-host-cpu-percent "
            "when recovery is enabled"
        )

    git_sha, git_dirty = git_identity()
    if git_dirty:
        raise SeriesError("refusing checkpoint series from a dirty checkout")
    config = series_config(
        sources=sources,
        page_cap=args.page_cap,
        concurrency=args.concurrency,
        attempts_per_source=args.attempts_per_source,
        max_resume_age_hours=args.max_resume_age_hours,
        max_host_cpu_percent=args.max_host_cpu_percent,
        cpu_sustain_seconds=args.cpu_sustain_seconds,
        cpu_sample_seconds=args.cpu_sample_seconds,
        nice=args.nice,
        recovery=recovery_config,
    )
    database_target = database_target_fingerprint(args.env_file)

    if args.resume:
        supplied = Path(args.resume).expanduser().resolve()
        manifest_path = (
            supplied if supplied.name == "manifest.json" else supplied / "manifest.json"
        )
        series_dir = manifest_path.parent
    else:
        series_dir = Path(args.out_root).expanduser().resolve() / _run_id()
        if series_dir.exists():
            raise SeriesError(f"series directory already exists: {series_dir}")
        series_dir.mkdir(parents=True)
    try:
        with SeriesOwnershipLock(series_dir / ".series.lock"):
            if args.resume:
                manifest = load_resume_manifest(
                    manifest_path,
                    git_sha=git_sha,
                    config=config,
                    database_target=database_target,
                )
                if args.reconcile_stale_series:
                    reconcile_stale_running_sources(series_dir, manifest)
                recovery = _resource_recovery_state(manifest)
                manifest["status"] = (
                    "cooling_down"
                    if recovery.get("state") == "cooling_down"
                    else "running"
                )
                manifest["error"] = None
                manifest["finished_at"] = None
            else:
                if (series_dir / "manifest.json").exists():
                    raise SeriesError(
                        f"series manifest already exists: {series_dir / 'manifest.json'}"
                    )
                manifest = new_manifest(
                    series_dir,
                    git_sha=git_sha,
                    config=config,
                    database_target=database_target,
                )
            save_manifest(series_dir, manifest)

            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, checkpoint_series_sigterm_handler)
            try:
                return run_series(
                    series_dir,
                    manifest,
                    env_file=args.env_file,
                    retry_failed=args.retry_failed,
                )
            finally:
                signal.signal(signal.SIGTERM, previous_sigterm)
    except RecoveryOwnershipError as exc:
        raise SeriesError(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
