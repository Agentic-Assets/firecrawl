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
import hashlib
import json
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cre_checkpoint_refresh import (
    COLLECTOR_DIR,
    DEFAULT_CPU_SAMPLE_SECONDS,
    DEFAULT_CPU_SUSTAIN_SECONDS,
    DEFAULT_MAX_HOST_CPU_PERCENT,
    DEFAULT_MAX_RESUME_AGE_HOURS,
    FRESHNESS_EVIDENCE_CONTRACT,
    SOURCE_KEYS,
    ProcessCleanupUnknown,
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
from cre_memory_guard import (
    DEFAULT_MEMORY_SAMPLE_SECONDS,
    DEFAULT_MEMORY_SUSTAIN_SECONDS,
    memory_guard_config,
)
from cre_owned_processes import (
    OwnedProcessGroup,
    _defer_cleanup_signals,
    settle_owned_groups,
)
from cre_performance_report import save_report
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
from cre_series_control import SeriesControlError, SeriesWorkStopped

_CLI_CONTROL_CAPABILITY = None

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
    return datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")


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
    memory_sustain_seconds: float = DEFAULT_MEMORY_SUSTAIN_SECONDS,
    memory_sample_seconds: float = DEFAULT_MEMORY_SAMPLE_SECONDS,
    admit_current_inventory_source: str | Sequence[str] | None = None,
    admit_daum_verified_subset: bool = False,
    daum_subset_policy: str = "tenure-only-v1",
    pipeline: bool = False,
    source_workers: int | None = None,
    admit_baseline_hold_additively: bool = False,
    automatic_source_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    import cre_current_inventory_admission as current_inventory

    admissions = ([admit_current_inventory_source] if isinstance(admit_current_inventory_source, str)
                  else list(admit_current_inventory_source or ()))
    current_inventory.validate_options(admissions, sources, ("sale", "lease"), pipeline=pipeline, daum_verified_subset=admit_daum_verified_subset, daum_subset_policy=daum_subset_policy)
    if admit_baseline_hold_additively and (list(sources) != ["savills"] or pipeline or admissions):
        raise ValueError("baseline-hold admission requires a Savills-only serial series without other admissions")
    workers = (2 if pipeline else 1) if source_workers is None else source_workers
    if type(workers) is not int or not 1 <= workers <= 4 or (not pipeline and workers != 1):
        raise ValueError("source-workers must be 1–4 in pipeline mode; serial series requires 1")

    recovery_config = recovery or RecoveryConfig()
    recovery_config.validate()
    if (
        recovery_config.max_recoveries_per_source > 0
        and recovery_config.low_cpu_percent >= max_host_cpu_percent
    ):
        raise ValueError(
            "recovery low CPU percent must be below the host CPU watchdog ceiling"
        )
    config = {
        "sources": list(sources),
        "transactions": ["sale", "lease"],
        "page_cap": page_cap,
        "concurrency": concurrency,
        "source_workers": workers,
        "attempts_per_source": attempts_per_source,
        "max_resume_age_hours": max_resume_age_hours,
        "host_cpu_guard": {
            "max_host_cpu_percent": max_host_cpu_percent,
            "sustain_seconds": cpu_sustain_seconds,
            "sample_seconds": cpu_sample_seconds,
        },
        "host_memory_guard": memory_guard_config(
            memory_sustain_seconds, memory_sample_seconds
        ),
        "resource_recovery": recovery_config.as_dict(),
        "nice": nice,
        "continue_source_local_failures": True,
    }
    if admit_baseline_hold_additively:
        config["admit_baseline_hold_additively"] = True
    if automatic_source_evidence is not None:
        config["automatic_source_evidence"] = dict(automatic_source_evidence)
    if pipeline:
        config["pipeline"] = True
        config["current_inventory_admissions"] = current_inventory.configuration_map(admissions, sources, daum_verified_subset=admit_daum_verified_subset, daum_subset_policy=daum_subset_policy)
        if recovery_config.max_recoveries_per_source:
            raise ValueError("pipeline resource failures require a hard stop; automatic recovery is disabled")
    elif admit_daum_verified_subset:
        config["current_inventory_admission"] = current_inventory.subset_configuration(daum_subset_policy)
    elif admissions:
        config["current_inventory_admission"] = current_inventory.configuration(admissions[0])
    return config


def current_subset(config: Mapping[str, Any]) -> bool:
    import cre_current_inventory_admission as current_inventory
    return current_inventory.is_verified_subset(config)


def new_manifest(
    series_dir: Path,
    *,
    git_sha: str,
    config: Mapping[str, Any],
    database_target: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    now = utc_now()
    result = {
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
    if config.get("current_inventory_admission") or config.get("current_inventory_admissions"):
        result["scope"] = {
            "kind": "collector_registry_additive_current_inventory",
            "whole_source_coverage": False,
        }
    if current_subset(config):
        result["scope"] = {"kind": "collector_registry_verified_subset", "whole_source_coverage": False}
    if config.get("admit_baseline_hold_additively") is True:
        result["scope"] = {"kind": "collector_registry_additive_coverage_hold", "whole_source_coverage": False}
    return result


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
    if isinstance(value, dict) and "cleanup_unknown" in str(value.get("error", "")):
        raise SeriesError(
            "manual owned-process settlement required before series resume"
        )
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
        str(config["source_workers"]),
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
        "--memory-sustain-seconds",
        str(config["host_memory_guard"]["warning_sustain_seconds"]),
        "--memory-sample-seconds",
        str(config["host_memory_guard"]["sample_seconds"]),
    ]
    if config.get("pipeline"):
        argv.append("--pipeline")
        for key, entry in config.get("current_inventory_admissions", {}).items():
            if entry.get("admission_scope") in {"daum_verified_subset_v1", "daum_verified_subset_v2"}:
                argv.append("--admit-daum-verified-subset")
            else:
                argv.extend(["--admit-current-inventory-source", key])
    if resume_run is not None:
        argv.extend(["--resume", str(resume_run)])
    elif fresh_run_id is not None:
        argv.extend(["--run-id", fresh_run_id])
    if env_file:
        argv.extend(["--env-file", env_file])
    evidence = config.get("automatic_source_evidence")
    if evidence:
        argv.extend(["--source-evidence-policy", evidence["policy_path"],
                     "--source-evidence-policy-sha256", evidence["policy_sha256"],
                     "--source-evidence-env-file", evidence["env_file"],
                     "--source-evidence-target-sha256", evidence["target_sha256"]])
    if current_subset(config) and not config.get("pipeline"):
        argv.append("--admit-daum-verified-subset")
    elif "current_inventory_admission" in config:
        argv.extend(
            ["--admit-current-inventory-source", config["current_inventory_admission"]["source_key"]]
        )
    if config.get("admit_baseline_hold_additively") is True:
        argv.append("--admit-baseline-hold-additively")
    if current_subset(config):
        from cre_current_inventory_admission import subset_policy
        if subset_policy(config) != "tenure-only-v1":
            argv.extend(["--daum-subset-policy", subset_policy(config)])
    return argv


def _expected_child_config(
    source: str,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    guard = config["host_cpu_guard"]
    expected = {
        "sources": source.split(",") if config.get("pipeline") else [source],
        "transactions": ["sale", "lease"],
        "max_items": 0,
        "page_cap": config["page_cap"],
        "concurrency": config["concurrency"],
        "source_workers": config["source_workers"],
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
        "admit_baseline_hold_additively": config.get("admit_baseline_hold_additively", False),
    }
    if config.get("pipeline"):
        expected["pipeline"] = True
        expected["current_inventory_admissions"] = config.get("current_inventory_admissions", {})
    if config.get("automatic_source_evidence") is not None:
        expected["automatic_source_evidence"] = config["automatic_source_evidence"]
    if "current_inventory_admission" in config:
        expected["current_inventory_admission"] = config["current_inventory_admission"]
    # Reconciliation also inspects completed historical parent/child pairs.
    # Preserve their recorded policy shape rather than inventing a guard that
    # did not run. New series creation/resume still requires today's full config.
    if "host_memory_guard" in config:
        memory_policy = config["host_memory_guard"]
        if not isinstance(memory_policy, Mapping):
            raise TypeError("host memory guard policy must be an object")
        try:
            canonical = memory_guard_config(
                memory_policy["warning_sustain_seconds"],
                memory_policy["sample_seconds"],
            )
        except (KeyError, ValueError) as exc:
            raise TypeError("host memory guard policy is malformed") from exc
        if not _exact_json_equal(dict(memory_policy), canonical):
            raise TypeError("host memory guard policy is not canonical")
        expected["host_memory_guard"] = canonical
    return expected


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
    return parsed.astimezone(UTC)


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
    current = (now or datetime.now(UTC)).astimezone(UTC)
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


def evidence_reconciliation_required(child_manifest: Mapping[str, Any]) -> bool:
    if child_manifest.get("automatic_source_evidence_paused"):
        return True
    sources = child_manifest.get("sources") or {}
    config = (child_manifest.get("config") or {}).get("automatic_source_evidence") or {}
    allowed = config.get("source_keys") or []
    return any(
        ((attempt.get("source_evidence") or {}).get("state") in {"awaiting_artifact", "pending", "appending", "outcome_unknown", "refused"}
         or (source in allowed and (attempt.get("source_evidence") or {}).get("state") not in ("appended", "no_artifact"))
         or (source in (config.get("native_source_formats") or {}) and
             ((attempt.get("native_source_evidence") or {}).get("state") not in ("settled", "no_data")
              or any(page.get("state") not in ("appended", "no_data")
                     for page in (attempt.get("native_source_evidence") or {}).get("pages", [])))))
        for source, checkpoint in sources.items() if isinstance(checkpoint, Mapping)
        for attempt in (checkpoint.get("attempts") or []) if isinstance(attempt, Mapping)
    )


def source_local_failure(child_manifest: Mapping[str, Any]) -> bool:
    if evidence_reconciliation_required(child_manifest):
        return False
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


def _child_succeeded(
    child_manifest: Mapping[str, Any], config: Mapping[str, Any]
) -> bool:
    """Bind the additive terminal result to its explicit parent admission."""
    if (evidence_reconciliation_required(child_manifest)
            or (config.get("automatic_source_evidence") is not None
                and child_manifest.get("automatic_source_evidence_status") != "settled")):
        return False
    if config.get("admit_baseline_hold_additively") is True:
        from cre_checkpoint_refresh import subset_gate_can_admit

        checkpoint = (child_manifest.get("sources") or {}).get("savills") or {}
        gate = checkpoint.get("gate") or {}
        aggregate = child_manifest.get("aggregate_gate") or {}
        scope = child_manifest.get("scope") or {}
        bound = (
            config.get("sources") == ["savills"]
            and config.get("transactions") == ["sale", "lease"]
            and not config.get("pipeline")
            and config.get("source_workers") == 1
            and not config.get("current_inventory_admission")
            and not config.get("current_inventory_admissions")
            and _exact_json_equal(child_manifest.get("config"), _expected_child_config("savills", config))
            and aggregate.get("hold_sources") == []
            and aggregate.get("non_ok_sources") == []
            and type(aggregate.get("rc")) is int and aggregate["rc"] in (0, 2)
        )
        if not bound:
            return False
        # The fresh source may also clear its historical baseline naturally.
        if child_manifest.get("status") == SUCCESS_STATUS:
            return gate.get("verdict") == "ok" and aggregate.get("baseline_advisory_holds") == []
        return (
            child_manifest.get("status") == "additive_scope_complete_coverage_hold"
            and scope.get("kind") == "collector_registry_additive_coverage_hold"
            and scope.get("whole_source_coverage") is False
            and gate.get("verdict") == "ok_additive_coverage_hold"
            and gate.get("raw_verdict") == "hold"
            and subset_gate_can_admit({"verdict": gate.get("raw_verdict"), "reason": gate.get("raw_reason")})
            and gate.get("mark_missing_safe") is False
            and gate.get("admission_scope") == "additive_coverage_hold"
            and aggregate.get("baseline_advisory_holds") == ["savills"]
            and aggregate.get("mark_missing_safe_brokerages") == []
        )
    if current_subset(config):
        scope = child_manifest.get("scope")
        return (
            isinstance(scope, Mapping)
            and child_manifest.get("status") == "qualified_partial_subset"
            and scope.get("kind") == "collector_registry_verified_subset"
            and scope.get("whole_source_coverage") is False
            and _exact_json_equal(child_manifest.get("config"), _expected_child_config("daum-commercial", config))
        )
    admission = config.get("current_inventory_admission")
    if admission is None:
        return child_manifest.get("status") == SUCCESS_STATUS
    import cre_current_inventory_admission as current_inventory

    try:
        if not isinstance(admission, Mapping) or not isinstance(
            child_manifest.get("scope"), Mapping
        ):
            return False
        current_inventory.validate_option(
            admission["source_key"], config["sources"], config["transactions"]
        )
        expected = current_inventory.configuration(admission["source_key"])
        source = admission["source_key"]
        return (
            _exact_json_equal(admission, expected)
            and child_manifest.get("status")
            == "additive_scope_complete_current_inventory"
            and _exact_json_equal(
                child_manifest.get("config"), _expected_child_config(source, config)
            )
            and (child_manifest.get("scope") or {}).get("kind")
            == "collector_registry_additive_current_inventory"
            and (child_manifest.get("scope") or {}).get("whole_source_coverage")
            is False
        )
    except (KeyError, TypeError, ValueError):
        return False


def _reconciled_state(
    child_manifest: Mapping[str, Any], config: Mapping[str, Any] | None = None
) -> str:
    if evidence_reconciliation_required(child_manifest):
        return "failed_global"
    status = child_manifest.get("status")
    if _child_succeeded(child_manifest, config or {}):
        return "qualified_partial_subset" if current_subset(config or {}) else "complete"
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
    if manifest["config"].get("pipeline"):
        return reconcile_pipeline_series(series_dir, manifest)
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
                "additive_scope_complete_current_inventory",
        "qualified_partial_subset",
                "additive_scope_complete_coverage_hold",
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
                and not manifest["config"].get("current_inventory_admission")
                and not manifest["config"].get("admit_baseline_hold_additively")
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
        checkpoint["state"] = _reconciled_state(child, manifest["config"])
        checkpoint["error"] = child.get("error")
        attempts[-1]["finished_at"] = child.get("finished_at") or utc_now()
        save_manifest(series_dir, manifest)


def _terminate_child(proc: subprocess.Popen[Any]) -> None:
    owned = getattr(proc, "_cre_owned_group", None) or OwnedProcessGroup.from_spawn(
        proc
    )
    settlement = settle_owned_groups(
        [owned],
        grace_seconds=SERIES_INTERRUPT_GRACE_SECONDS,
        initial_signal=signal.SIGTERM,
    )
    if not settlement.settled:
        raise RefreshError("owned checkpoint process cleanup_unknown")


def checkpoint_series_sigterm_handler(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


def _now_utc() -> datetime:
    return datetime.now(UTC)


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
    manifest["error"] = "resource guard stop was not admitted for automatic recovery"
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
    except KeyboardInterrupt as exc:
        if isinstance(exc, SeriesWorkStopped):
            raise
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


def local_git_output(
    arguments: Sequence[str], *, timeout: float = 5.0, control=None
) -> str:
    """Bound local inspection bytes/time; never fetch, persist stderr or use a shell."""
    limit = 65536
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_NO_LAZY_FETCH"] = "1"
    try:
        with control.defer_stops() if control is not None else _defer_cleanup_signals():
            if control is not None:
                control.spawn_uncertain = True
            process = subprocess.Popen(
                [
                    "git",
                    "--no-optional-locks",
                    "-c",
                    "core.fsmonitor=false",
                    *arguments,
                ],
                cwd=COLLECTOR_DIR,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                group = OwnedProcessGroup.from_spawn(process)
                if control is not None:
                    control.register_group(group)
            except (Exception, KeyboardInterrupt):
                raise ProcessCleanupUnknown(
                    "local Git ownership registration unavailable"
                ) from None
    except OSError:
        raise SeriesError("local Git inspection failed to start") from None
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        try:
            if control is not None:
                control.check()
            with selectors.DefaultSelector() as selector:
                os.set_blocking(process.stdout.fileno(), False)
                selector.register(process.stdout, selectors.EVENT_READ)
                eof = False
                while not eof:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SeriesError("local Git inspection timeout")
                    for key, _events in selector.select(remaining):
                        chunk = os.read(key.fd, min(8192, limit + 1 - len(output)))
                        if not chunk:
                            eof = True
                            break
                        output.extend(chunk)
                        if len(output) > limit:
                            raise SeriesError(
                                "local Git inspection output exceeded bound"
                            )
                try:
                    rc = process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    raise SeriesError("local Git inspection timeout") from None
        except BaseException as error:
            settled = settle_owned_groups([group], grace_seconds=0.5, kill_seconds=0.5)
            if control is not None:
                control.settled(group, settled.settled)
            if not settled.settled:
                raise ProcessCleanupUnknown(
                    "local Git cleanup_unknown; manual settlement required"
                ) from None
            if isinstance(error, KeyboardInterrupt):
                raise
            if isinstance(error, SeriesError):
                raise error from None
            raise SeriesError("local Git inspection failed") from None
        settled = settle_owned_groups([group], grace_seconds=0, kill_seconds=0.1)
        if control is not None:
            control.settled(group, settled.settled)
        if not settled.settled:
            raise ProcessCleanupUnknown(
                "local Git cleanup_unknown; manual settlement required"
            ) from None
        if rc != 0:
            raise SeriesError("local Git inspection failed")
        try:
            return output.decode("utf-8").strip()
        except UnicodeError:
            raise SeriesError("local Git inspection output invalid") from None
    finally:
        process.stdout.close()


def verify_reviewed_checkout(expected_sha: str, *, control=None) -> None:
    """Pin clean local code; a matching tracking tip is not remote freshness."""
    if (
        not isinstance(expected_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", expected_sha) is None
    ):
        raise SeriesError("expected SHA must be a lowercase full40 revision")
    kwargs = {"control": control} if control is not None else {}
    if local_git_output(["rev-parse", "HEAD"], **kwargs) != expected_sha:
        raise SeriesError("checkout SHA differs from the reviewed revision")
    if local_git_output(
        ["status", "--porcelain", "--untracked-files=normal"], **kwargs
    ):
        raise SeriesError("reviewed checkout is not clean")
    if (
        expected_sha
        not in local_git_output(
            [
                "for-each-ref",
                "--format=%(objectname)",
                "refs/remotes/origin/",
            ],
            **kwargs,
        ).splitlines()
    ):
        raise SeriesError("reviewed revision has no matching local origin tracking tip")


def admit_series_invocation(
    expected_sha: str | None, windows_unix_ms: int | None
) -> dict | None:
    """Current invocation only; optional Windows evidence is startup-only."""
    if expected_sha is None:
        if windows_unix_ms is not None:
            raise SeriesError("Windows startup admission requires an expected SHA")
        return None
    # Reject malformed invocation identity before local inspection.
    if (
        not isinstance(expected_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", expected_sha) is None
    ):
        raise SeriesError("expected SHA must be a lowercase full40 revision")
    verify_reviewed_checkout(expected_sha)
    if windows_unix_ms is not None:
        from cre_guarded_tier import GuardedTierError, validate_windows_admission

        try:
            validate_windows_admission(windows_unix_ms)
        except (GuardedTierError, OverflowError, TypeError, ValueError):
            raise SeriesError("Windows startup admission is stale or invalid") from None
    return {
        "expected_sha": expected_sha,
        "checked_at": utc_now(),
        "origin_tracking": "matching_local_tip_not_remote_freshness",
        "windows_admission_unix_ms": windows_unix_ms,
        "windows_scope": "startup_only"
        if windows_unix_ms is not None
        else "not_supplied",
    }


def reconcile_pipeline_series(series_dir: Path, manifest: dict) -> None:
    """Recover observations from the same exact terminal owner, never replay."""
    sources = manifest["config"]["sources"]
    refs = {manifest["sources"][key].get("checkpoint_run") for key in sources}
    if len(refs) != 1 or not isinstance(next(iter(refs)), str):
        raise SeriesError("pipeline reconciliation requires one exact shared owner")
    relative = Path(next(iter(refs)))
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != "runs"
        or ".." in relative.parts
    ):
        raise SeriesError("invalid pipeline owner path")
    _, child = _load_child_manifest(
        series_dir / "runs",
        before=set(),
        source=",".join(sources),
        git_sha=manifest["collector_git_sha"],
        expected_path=series_dir / relative / "manifest.json",
        expected_config=_expected_child_config(",".join(sources), manifest["config"]),
    )
    if child.get("status") not in {
        SUCCESS_STATUS,
        "additive_scope_complete_current_inventory",
        "qualified_partial_subset",
        "failed",
        "interrupted",
        RESOURCE_GUARD_STATUS,
    } or child.get("preflight", {}).get("database_target") != manifest.get(
        "database_target"
    ):
        raise SeriesError(
            "pipeline reconciliation requires exact target and terminal owner"
        )
    finished = _parse_utc_timestamp(
        child.get("finished_at"), field="pipeline finished_at"
    )
    started = _parse_utc_timestamp(child.get("started_at"), field="pipeline started_at")
    for key in sources:
        attempts = manifest["sources"][key].get("attempts")
        if (
            not attempts
            or finished
            < _parse_utc_timestamp(
                attempts[-1].get("started_at"), field="attempt started_at"
            )
            or finished < started
        ):
            raise SeriesError(
                "pipeline reconciliation lacks exact terminal attempt timing"
            )
    unit_cache = {}
    for key in sources:
        row = manifest["sources"][key]
        row.update(
            state=("qualified_partial_subset" if current_subset(manifest["config"]) and key == "daum-commercial" else "complete")
            if pipeline_source_qualified(
                child,
                key,
                run_dir=series_dir / relative,
                parent_started_at=manifest["started_at"],
                parent_finished_at=utc_now(),
                unit_cache=unit_cache,
                parent_attempts=row["attempts"],
            )
            else "interrupted",
            checkpoint_status=child["status"],
            error=child.get("error"),
        )
        row["attempts"][-1]["finished_at"] = child["finished_at"]
    save_manifest(series_dir, manifest)


def _pipeline_member_report_matches(
    report: dict, checkpoint: dict, source: str, run_id: str
) -> bool:
    """Cross-bind saved exact readback fields to the hashed full query output."""
    artifact = checkpoint["artifact"]
    readback = checkpoint["readback"]
    staged = artifact["staged_unique"]
    inventory_only = artifact.get("inventory_only", 0)
    if (
        type(staged) is not int
        or staged < 0
        or type(inventory_only) is not int
        or inventory_only < 0
        or readback["expected_staged_unique"] != staged
    ):
        return False
    rows = report["queries"]["freshness_generations"]
    generation_rows = [
        row
        for row in rows
        if row.get("source_key") == source and row.get("generation_id") == run_id
    ]
    if readback.get("generation_id") == run_id:
        if len(generation_rows) != 1:
            return False
        generation = generation_rows[0]
        if (
            int(generation["active"]) != staged
            or int(generation["persisted_inventory_observed"]) != staged
            or readback["latest_inventory_batch_active"] != staged
            or generation["evidence_class"] != readback["evidence_class"]
        ):
            return False
        contract = FRESHNESS_EVIDENCE_CONTRACT.get(generation["evidence_class"])
        if contract is None:
            return False
        for name in (
            "earliest_inventory_observed_at",
            "latest_inventory_observed_at",
            "earliest_detail_observed_at",
            "latest_detail_observed_at",
        ):
            saved, queried = readback[name], generation[name]
            # psql serializes SQL NULL as an empty cell. Only feed detail
            # clocks are optional, and both bound reports must agree on absence.
            if (
                not contract["requires_detail"]
                and name in {"earliest_detail_observed_at", "latest_detail_observed_at"}
                and saved in (None, "")
                and queried in (None, "")
            ):
                continue
            if _parse_utc_timestamp(queried, field=name) != _parse_utc_timestamp(
                saved, field=name
            ):
                return False
    elif (
        staged != 0 or inventory_only <= 0 or readback.get("generation_id") is not None
    ):
        return False
    if readback.get("generation_id") is None:
        from cre_source_policy import load_source_policy

        if load_source_policy()[source]["inventory_only_namespace"] is None:
            return False
    if inventory_only:
        inventory = [
            row
            for row in report["queries"]["inventory_only_index"]
            if row.get("source_key") == source
        ]
        saved = readback["inventory_only"]
        if (
            len(inventory) != 1
            or int(inventory[0]["active"]) != inventory_only
            or int(inventory[0]["latest_batch_active"]) != inventory_only
            or saved["expected_active"] != inventory_only
        ):
            return False
    fingerprint_rows = [
        row
        for row in report["queries"]["inventory_generation_fingerprints"]
        if row.get("source_key") == source
    ]
    fingerprint = readback["inventory_fingerprint"]
    if (
        len(fingerprint_rows) != 1
        or fingerprint["sourceId"] != source
        or fingerprint["publicationStatus"] != "complete"
        or type(fingerprint["rowCount"]) is not int
        or int(fingerprint_rows[0]["row_count"]) != fingerprint["rowCount"]
    ):
        return False
    return all(
        _parse_utc_timestamp(fingerprint_rows[0][raw], field=raw)
        == _parse_utc_timestamp(fingerprint[saved], field=saved)
        for raw, saved in (
            ("max_row_updated_at", "maxRowUpdatedAt"),
            ("max_observation_at", "maxObservationAt"),
        )
    )


def pipeline_source_qualified(
    child: Mapping[str, Any],
    source: str,
    *,
    run_dir: Path | None = None,
    parent_started_at: str | None = None,
    parent_finished_at: str | None = None,
    unit_cache: dict | None = None,
    allow_running: bool = False,
    parent_attempts: Sequence[Mapping[str, Any]] | None = None,
) -> bool:
    """Qualify original bound audit bytes; matching manifest strings are insufficient.

    A caller owns one cache for a single qualification pass. It never persists
    cache entries across reads or generations, and each sealed report is read once.
    """
    from cre_series_status import _contained_path, _read_regular_file
    from cre_validate import (
        FULL_VALIDATION_PROFILE,
        VALIDATION_AUDIT_SCOPES,
        require_full_validation_report,
    )

    if run_dir is None or parent_started_at is None or not parent_attempts:
        return False
    cache = unit_cache if unit_cache is not None else {}
    try:
        run_id = child["run_id"]
        if not isinstance(run_id, str) or validate_run_id(run_id) != run_dir.name:
            return False
        parent_start = _parse_utc_timestamp(
            parent_started_at, field="parent started_at"
        )
        parent_end = _parse_utc_timestamp(
            parent_finished_at or utc_now(), field="parent finished_at"
        )
        child_start = _parse_utc_timestamp(
            child["started_at"], field="child started_at"
        )
        child_finished = child.get("finished_at")
        if child_finished is None and not (
            allow_running and child.get("status") == "running"
        ):
            return False
        child_end = _parse_utc_timestamp(
            child_finished or parent_end.isoformat(), field="child finished_at"
        )
        if not parent_start <= child_start <= child_end <= parent_end:
            return False
        results = child["pipeline"]["source_results"]
        result = results[source]
        unit_id = result["unit_id"]
        if (
            not isinstance(unit_id, str)
            or not re.fullmatch(r"unit-[0-9]{4,6}", unit_id)
            or int(unit_id[5:]) <= 0
        ):
            return False
        units = child["pipeline"]["units"]
        matching = [unit for unit in units if unit.get("unit_id") == unit_id]
        if len(matching) != 1:
            return False
        unit = matching[0]
        members = unit["sources"]
        if (
            unit["state"] != "verified"
            or not isinstance(members, list)
            or not 1 <= len(members) <= 5
            or len(set(members)) != len(members)
            or source not in members
            or any(key not in child["sources"] for key in members)
        ):
            return False
        unit_start = _parse_utc_timestamp(unit["started_at"], field="unit started_at")
        unit_end = _parse_utc_timestamp(unit["finished_at"], field="unit finished_at")
        if not child_start <= unit_start <= unit_end <= child_end:
            return False
        in_parent_attempt = False
        for attempt in parent_attempts:
            attempt_start = _parse_utc_timestamp(
                attempt.get("started_at"), field="parent attempt started_at"
            )
            attempt_end = _parse_utc_timestamp(
                attempt.get("finished_at") or parent_end.isoformat(),
                field="parent attempt finished_at",
            )
            if (
                parent_start
                <= attempt_start
                <= unit_start
                <= unit_end
                <= attempt_end
                <= parent_end
            ):
                in_parent_attempt = True
        if not in_parent_attempt:
            return False
        path = unit["validation_path"]
        digest = unit["validation_sha256"]
        expected_path = f"pipeline/{unit_id}/validation.json"
        if (
            path != expected_path
            or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
        ):
            return False
        evidence = unit["validation"]
        if (
            evidence["path"] != path
            or type(evidence["rc"]) is not int
            or evidence["rc"] != 0
            or evidence["query_execution_ok"] is not True
            or evidence["quality_no_regression"] is not True
            or evidence["quality_failures"] != []
            or evidence["readback_ok"] is not True
            or evidence["failed_readback_sources"] != []
            or type(evidence["absolute_quality_ok"]) is not bool
            or not isinstance(evidence["absolute_quality_failures"], list)
        ):
            return False
        cache_key = (str(run_dir), unit_id, path, digest)
        if cache_key not in cache:
            cache[cache_key] = None
            bound = _contained_path(run_dir, path)
            original = run_dir / path
            if bound is None or any(
                part.is_symlink()
                for part in (run_dir, original.parent.parent, original.parent, original)
            ):
                return False
            raw = _read_regular_file(original, limit=32 * 1024 * 1024)
            if hashlib.sha256(raw).hexdigest() != digest:
                return False
            report = json.loads(raw)
            if (
                report.get("validation_profile") != FULL_VALIDATION_PROFILE
                or report.get("audit_scope")
                != VALIDATION_AUDIT_SCOPES[FULL_VALIDATION_PROFILE]
                or report.get("ok") is not True
            ):
                return False
            require_full_validation_report(report)
            report_time = _parse_utc_timestamp(
                report["generated_at"], field="validation generated_at"
            )
            if not unit_start <= report_time <= unit_end:
                return False
            cache[cache_key] = report
        if cache[cache_key] is None:
            return False
        report = cache[cache_key]
        for key in members:
            checkpoint = child["sources"][key]
            proof = results[key]
            if current_subset(child.get("config") or {}) and key == "daum-commercial":
                if (proof.get("qualification") != "qualified_partial_subset"
                    or proof.get("partial") is not True
                    or proof.get("whole_source_coverage") is not False
                    or child.get("scope", {}).get("kind") != "collector_registry_verified_subset"):
                    return False
            elif proof.get("partial") is True:
                return False
            artifact = checkpoint["artifact"]
            artifact_digest = artifact["sha256"]
            if (
                not isinstance(artifact_digest, str)
                or not re.fullmatch(r"[a-f0-9]{64}", artifact_digest)
                or type(artifact["bytes"]) is not int
                or artifact["bytes"] <= 0
                or proof["state"] != "verified"
                or proof["unit_id"] != unit_id
                or proof["readback_ok"] is not True
                or proof["generation_id"] != run_id
                or proof["artifact_sha256"] != artifact_digest
                or proof["validation_path"] != expected_path
                or proof["validation_sha256"] != digest
                or checkpoint["readback"]["ok"] is not True
                or not _pipeline_member_report_matches(report, checkpoint, key, run_id)
                or sum(
                    key in candidate.get("sources", [])
                    for candidate in units
                    if candidate.get("state") == "verified"
                )
                != 1
            ):
                return False
        return True
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        SeriesError,
    ):
        return False


def run_pipeline_series(
    series_dir: Path,
    manifest: dict[str, Any],
    *,
    env_file: str | None,
    expected_sha: str | None = None,
    control=None,
) -> int:
    """Forward the finite selection once; the refresh owns all stage overlap."""
    config = manifest["config"]
    sources = config["sources"]
    root = series_dir / "runs"
    root.mkdir(parents=True, exist_ok=True)
    logs = series_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    if current_subset(config) and all(
        manifest["sources"][key].get("state") in {"complete", "qualified_partial_subset"}
        for key in sources
    ):
        # A handled partial is not another collection/apply opportunity.
        # Reuse the same independent bound report qualification as reconciliation.
        refs = {manifest["sources"][key].get("checkpoint_run") for key in sources}
        if len(refs) != 1 or not isinstance(next(iter(refs)), str):
            raise SeriesError("handled subset has no exact shared owner")
        relative = Path(next(iter(refs)))
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 2 or relative.parts[0] != "runs":
            raise SeriesError("handled subset owner path is invalid")
        _, child = _load_child_manifest(root, before=set(), source=",".join(sources),
            git_sha=manifest["collector_git_sha"], expected_path=series_dir/relative/"manifest.json",
            expected_config=_expected_child_config(",".join(sources), config))
        cache = {}
        if (child.get("status") != "qualified_partial_subset"
            or evidence_reconciliation_required(child)
            or (config.get("automatic_source_evidence") is not None
                and child.get("automatic_source_evidence_status") != "settled") or any(
            not pipeline_source_qualified(child, key, run_dir=series_dir/relative,
                parent_started_at=manifest["started_at"],parent_finished_at=manifest["finished_at"],
                unit_cache=cache,parent_attempts=manifest["sources"][key]["attempts"])
            for key in sources
        )):
            raise SeriesError("handled subset evidence no longer qualifies")
        return 0
    prior_runs = {manifest["sources"][key].get("checkpoint_run") for key in sources}
    resume = None
    if prior_runs != {None}:
        if len(prior_runs) != 1 or None in prior_runs:
            raise SeriesError(
                "pipeline sources must share exactly one bound refresh owner"
            )
        prior = next(iter(prior_runs))
        if not isinstance(prior, str):
            raise SeriesError("pipeline owner binding is malformed")
        resume = series_dir / prior
        _, prior_child = _load_child_manifest(
            root,
            before=set(),
            source=",".join(sources),
            git_sha=manifest["collector_git_sha"],
            expected_path=resume / "manifest.json",
            expected_config=_expected_child_config(",".join(sources), config),
        )
        if evidence_reconciliation_required(prior_child):
            raise SeriesError("automatic source evidence requires exact prior-run reconciliation")
    if expected_sha is not None:
        verify_reviewed_checkout(
            expected_sha, **({"control": control} if control else {})
        )
    run_id = None if resume else allocate_run_id()
    child_run = resume or root / run_id
    attempt = {
        "number": 1
        + max(
            (len(manifest["sources"][key]["attempts"]) for key in sources), default=0
        ),
        "started_at": utc_now(),
        "finished_at": None,
        "rc": None,
        "log": "logs/pipeline.log",
        "checkpoint_run": str(child_run.relative_to(series_dir)),
    }
    for key in sources:
        row = manifest["sources"][key]
        row.update(
            state="running",
            checkpoint_run=str(child_run.relative_to(series_dir)),
            checkpoint_status=None,
            error=None,
        )
        row["attempts"].append(dict(attempt))
    save_manifest(series_dir, manifest)
    argv = build_checkpoint_argv(
        ",".join(sources),
        child_out_root=root,
        env_file=env_file,
        config=config,
        resume_run=resume,
        fresh_run_id=run_id,
    )
    if control is not None:
        control.prepare_checkpoint(child_run, manifest["collector_git_sha"])
        argv.extend(
            [
                "--series-control-invocation-id",
                control.invocation_id,
                "--series-control-child-id",
                control.child_invocation_id,
            ]
        )
        for key in sources:
            manifest["sources"][key]["attempts"][-1]["series_control_child_id"] = (
                control.child_invocation_id
            )
        save_manifest(series_dir, manifest)
    proc = None
    try:
        with (logs / "pipeline.log").open("a", encoding="utf-8") as log:
            spawn = control.spawn if control is not None else None
            kwargs = {
                "cwd": COLLECTOR_DIR,
                "stdout": log,
                "stderr": subprocess.STDOUT,
                "text": True,
                "start_new_session": True,
            }
            with _defer_cleanup_signals():
                proc = (
                    spawn(subprocess.Popen, argv, **kwargs)
                    if spawn
                    else subprocess.Popen(argv, **kwargs)
                )
                if control is None:
                    proc._cre_owned_group = OwnedProcessGroup.from_spawn(proc)
            if control is not None:
                control.check()
            rc = proc.wait()
            if control is not None:
                control.observe_exit(proc)
            else:
                _terminate_child(proc)
    except BaseException:
        if proc is not None and control is None:
            _terminate_child(proc)
        raise
    _, child = _load_child_manifest(
        root,
        before=set(),
        source=",".join(sources),
        git_sha=manifest["collector_git_sha"],
        expected_path=child_run / "manifest.json",
        expected_config=_expected_child_config(",".join(sources), config),
    )
    if child.get("preflight", {}).get("database_target") != manifest.get(
        "database_target"
    ):
        raise SeriesError("pipeline checkpoint database target changed")
    qualified = set()
    unit_cache = {}
    for key in sources:
        row = manifest["sources"][key]
        row["attempts"][-1].update(finished_at=utc_now(), rc=rc)
        if pipeline_source_qualified(child, key, run_dir=child_run,
                parent_started_at=manifest["started_at"], parent_finished_at=utc_now(), unit_cache=unit_cache,
                parent_attempts=row["attempts"]):
            qualified.add(key)
            row.update(state="qualified_partial_subset" if current_subset(config) and key == "daum-commercial" else "complete", error=None)
            row["pipeline_result"] = child["pipeline"]["source_results"][key]
        else:
            state = child.get("sources", {}).get(key, {}).get("state")
            if state in {
                "collect_failed",
                "artifact_rejected",
                "gate_failed",
                "gate_blocked",
                "baseline_seed_required",
                "dry_run_failed",
            }:
                outcome = "failed_source"
            elif state in {
                "pending",
                "collecting",
                "candidate",
                "validated",
                "gated",
                "dry_run_passed",
            }:
                outcome = "pending"
            else:
                outcome = "failed_global"
            row.update(
                state=outcome, error="pipeline source lacks qualified unit readback"
            )
        row["checkpoint_status"] = child.get("status")
    success = rc == 0 and len(qualified) == len(sources)
    manifest.update(
        status=("qualified_partial_subset" if current_subset(config) else "complete") if success else "failed",
        finished_at=utc_now(),
        error=None
        if success
        else "pipeline incomplete; inspect exact member and owner evidence",
    )
    save_manifest(series_dir, manifest)
    print(series_dir)
    return 0 if success else (rc or 1)


def run_series(
    series_dir: Path,
    manifest: dict[str, Any],
    *,
    env_file: str | None,
    retry_failed: bool,
    expected_sha: str | None = None,
    control=None,
) -> int:
    config = manifest["config"]
    if config.get("pipeline"):
        return run_pipeline_series(
            series_dir,
            manifest,
            env_file=env_file,
            expected_sha=expected_sha,
            control=control,
        )
    git_sha = manifest["collector_git_sha"]
    child_out_root = series_dir / "runs"
    child_out_root.mkdir(parents=True, exist_ok=True)
    log_root = series_dir / "logs"
    log_root.mkdir(parents=True, exist_ok=True)

    for index, source in enumerate(config["sources"], start=1):
        checkpoint = manifest["sources"][source]
        prior_state = checkpoint["state"]
        if config.get("automatic_source_evidence") and prior_state not in {"complete", "qualified_partial_subset"} and checkpoint.get("checkpoint_run"):
            try:
                _, prior_child = _load_child_manifest(
                    child_out_root, before=set(), source=source, git_sha=git_sha,
                    expected_path=series_dir / checkpoint["checkpoint_run"] / "manifest.json",
                    expected_config=_expected_child_config(source, config),
                )
            except (OSError, json.JSONDecodeError, SeriesError):
                raise SeriesError("cannot verify prior automatic evidence outcome") from None
            if evidence_reconciliation_required(prior_child):
                manifest["status"] = "failed"
                manifest["error"] = "automatic source evidence requires exact prior-run reconciliation"
                manifest["finished_at"] = utc_now()
                save_manifest(series_dir, manifest)
                return 1
        if prior_state in {"complete", "qualified_partial_subset"}:
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
            if expected_sha is not None:
                try:
                    verify_reviewed_checkout(
                        expected_sha,
                        **({"control": control} if control is not None else {}),
                    )
                except (SeriesError, ProcessCleanupUnknown) as error:
                    checkpoint["state"] = "failed_global"
                    checkpoint["error"] = (
                        "local Git cleanup_unknown; manual settlement required"
                        if isinstance(error, ProcessCleanupUnknown)
                        else "reviewed checkout refused before child dispatch"
                    )
                    manifest["status"] = "failed"
                    manifest["error"] = checkpoint["error"]
                    manifest["finished_at"] = utc_now()
                    save_manifest(series_dir, manifest)
                    return 1
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
            if control is not None:
                control.prepare_checkpoint(child_run, git_sha)
                argv.extend(
                    [
                        "--series-control-invocation-id",
                        control.invocation_id,
                        "--series-control-child-id",
                        control.child_invocation_id,
                    ]
                )
                attempt["series_control_child_id"] = control.child_invocation_id
                save_manifest(series_dir, manifest)
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"[{utc_now()}] command: {' '.join(argv)}\n")
                log.flush()
                try:
                    if control is not None:
                        proc = control.spawn(
                            subprocess.Popen,
                            argv,
                            cwd=COLLECTOR_DIR,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            text=True,
                            start_new_session=True,
                        )
                    else:
                        proc = subprocess.Popen(
                            argv,
                            cwd=COLLECTOR_DIR,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            text=True,
                            start_new_session=True,
                        )
                except KeyboardInterrupt as exc:
                    if isinstance(exc, SeriesWorkStopped):
                        raise
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
                if control is None:
                    proc._cre_owned_group = OwnedProcessGroup.from_spawn(proc)
                try:
                    if control is not None:
                        control.check()
                    rc = proc.wait()
                    if control is not None:
                        control.observe_exit(proc)
                except KeyboardInterrupt as exc:
                    if isinstance(exc, SeriesWorkStopped):
                        raise
                    try:
                        _terminate_child(proc)
                    except RefreshError:
                        attempt["finished_at"] = utc_now()
                        attempt["rc"] = 130
                        checkpoint["state"] = "failed_global"
                        checkpoint["error"] = "owned process cleanup_unknown"
                        manifest["status"] = "failed"
                        manifest["error"] = (
                            "owned process cleanup_unknown; manual settlement required"
                        )
                        manifest["finished_at"] = utc_now()
                        save_manifest(series_dir, manifest)
                        return 1
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

            if evidence_reconciliation_required(child_manifest):
                checkpoint["state"] = "failed_global"
                manifest["status"] = "failed"
                manifest["error"] = "automatic source evidence requires reconciliation"
                manifest["finished_at"] = utc_now()
                save_manifest(series_dir, manifest)
                return 1
            if rc == 0 and _child_succeeded(child_manifest, config):
                checkpoint["state"] = "qualified_partial_subset" if current_subset(config) else "complete"
                save_manifest(series_dir, manifest)
                break
            if rc == 75 or child_manifest.get("status") == RESOURCE_GUARD_STATUS:
                checkpoint["state"] = "resource_guard_interrupted"
                manifest["status"] = RESOURCE_GUARD_STATUS
                stop_code = (child_manifest.get("resource_stop") or {}).get(
                    "reason_code", ""
                )
                manifest["error"] = (
                    "host memory pressure guard interrupted a checkpoint child"
                    if str(stop_code).startswith("host_memory_")
                    else "host CPU guard interrupted a checkpoint child"
                )
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
        "complete_with_source_failures" if failed_sources else ("qualified_partial_subset" if current_subset(config) else "complete")
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


def run_controlled_series(
    series_dir, manifest, series_lock, args, *, _cli_control_capability=None
):
    """Own one explicit invocation; never infer remote/SQL settlement."""
    from cre_series_control import SeriesControl

    if (
        __name__ != "__main__"
        or _CLI_CONTROL_CAPABILITY is None
        or _cli_control_capability is not _CLI_CONTROL_CAPABILITY
    ):
        raise SeriesError("controlled series require the one-shot CLI entrypoint")
    control = SeriesControl(series_dir, series_lock, args.work_deadline_seconds)
    rc = 1
    with control.handlers():
        control.publish_running()
        manifest["latest_invocation_control"] = {
            "invocation_id": control.invocation_id,
            "work_deadline_seconds": control.seconds,
            "receipt": f".series-control/{control.invocation_id}.json",
        }
        try:
            save_manifest(series_dir, manifest)
            # Locator only: the bounded native observer does not parse the JSON handshake.
            print(series_dir, flush=True)
            signal.setitimer(signal.ITIMER_REAL, control.seconds)
            rc = run_series(
                series_dir,
                manifest,
                env_file=args.env_file,
                retry_failed=args.retry_failed,
                control=control,
                **(
                    {"expected_sha": args.expected_sha}
                    if args.expected_sha is not None
                    else {}
                ),
            )
            control.check()
        except SeriesWorkStopped as exc:
            from cre_checkpoint_refresh import _cleanup_unknown_in_chain

            control.cleanup_unknown |= _cleanup_unknown_in_chain(exc)
            rc = 124 if control.stop_reason == "deadline" else 130
            manifest["status"] = "interrupted"
            manifest["error"] = (
                "series work deadline expired" if rc == 124 else "operator interruption"
            )
            for checkpoint in manifest["sources"].values():
                if checkpoint["state"] == "running":
                    checkpoint["state"] = "interrupted"
                    checkpoint["error"] = manifest["error"]
                    checkpoint["attempts"][-1].update(finished_at=utc_now(), rc=rc)
        except BaseException as exc:
            from cre_checkpoint_refresh import _cleanup_unknown_in_chain

            control.cleanup_unknown |= _cleanup_unknown_in_chain(exc)
            manifest["status"] = "failed"
            manifest["error"] = "controlled series failed; inspect invocation receipt"
            rc = 1
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            with control.defer_stops():
                control.settle_active()
                manifest["finished_at"] = utc_now()
                save_manifest(series_dir, manifest)
                status = "deadline_interrupted" if rc == 124 else manifest["status"]
                save_report(series_dir)
                if not control.finish(status):
                    manifest["status"] = "failed"
                    manifest["error"] = (
                        "owned process cleanup_unknown; manual settlement required"
                    )
                    save_manifest(series_dir, manifest)
                    rc = 1
    return rc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", default=None)
    parser.add_argument("--work-deadline-seconds", type=int, default=None)
    parser.add_argument("--expected-sha", default=None)
    parser.add_argument("--windows-admission-unix-ms", type=int, default=None)
    parser.add_argument(
        "--reconcile-stale-series",
        action="store_true",
        help="explicitly repair a stale running parent from exactly one terminal child before resume",
    )
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--source-evidence-policy", default=None)
    parser.add_argument("--source-evidence-policy-sha256", default=None)
    parser.add_argument("--source-evidence-env-file", default=None)
    parser.add_argument("--source-evidence-target-sha256", default=None)
    parser.add_argument("--sources", default="all")
    parser.add_argument("--admit-current-inventory-source", action="append", default=None)
    parser.add_argument("--admit-baseline-hold-additively", action="store_true",
        help="Savills-only serial additive refresh retaining its historical coverage hold")
    parser.add_argument("--pipeline", action="store_true")
    parser.add_argument("--source-workers", type=int, default=None)
    parser.add_argument("--page-cap", type=int, default=400)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--attempts-per-source", type=int, default=3)
    parser.add_argument(
        "--memory-sustain-seconds", type=float, default=DEFAULT_MEMORY_SUSTAIN_SECONDS
    )
    parser.add_argument(
        "--memory-sample-seconds", type=float, default=DEFAULT_MEMORY_SAMPLE_SECONDS
    )
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
    parser.add_argument("--admit-daum-verified-subset", action="store_true")
    parser.add_argument("--daum-subset-policy", choices=("tenure-only-v1", "tenure-and-empty-title-v2"), default="tenure-only-v1")
    parser.add_argument("--nice", type=int, default=10)
    args = parser.parse_args(argv)

    if args.work_deadline_seconds is not None:
        from cre_series_control import validate_control_budget

        try:
            validate_control_budget(args.work_deadline_seconds)
        except SeriesControlError as exc:
            parser.error(str(exc))
        if __name__ != "__main__" or _CLI_CONTROL_CAPABILITY is None:
            raise SeriesError("controlled series require the one-shot CLI entrypoint")

    try:
        sources = parse_sources(args.sources)
        import cre_current_inventory_admission as current_inventory

        current_inventory.validate_options(
            args.admit_current_inventory_source, sources, ("sale", "lease"), pipeline=args.pipeline,
            daum_verified_subset=args.admit_daum_verified_subset,
            daum_subset_policy=args.daum_subset_policy
        )
        if args.admit_baseline_hold_additively and (sources != ("savills",) or args.pipeline or args.admit_current_inventory_source):
            raise ValueError("baseline-hold admission requires a Savills-only serial series without other admissions")
        if args.source_workers is not None and (not 1 <= args.source_workers <= 4 or
                (not args.pipeline and args.source_workers != 1)):
            raise ValueError("source-workers must be 1–4 in pipeline mode; serial series requires 1")
        if args.pipeline and args.max_resource_recoveries_per_source:
            raise ValueError("pipeline automatic resource recovery is disabled")
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
    try:
        memory_guard_config(args.memory_sustain_seconds, args.memory_sample_seconds)
    except ValueError as exc:
        parser.error(str(exc))
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

    invocation_admission = admit_series_invocation(
        args.expected_sha, args.windows_admission_unix_ms
    )
    git_sha, git_dirty = (
        (args.expected_sha, False) if args.expected_sha is not None else git_identity()
    )
    if git_dirty:
        raise SeriesError("refusing checkpoint series from a dirty checkout")
    evidence_options = (args.source_evidence_policy, args.source_evidence_policy_sha256,
                        args.source_evidence_env_file, args.source_evidence_target_sha256)
    automatic_source_evidence = None
    if any(evidence_options):
        from source_evidence.automatic import configuration
        automatic_source_evidence = configuration(*evidence_options, git_sha, sources)
    config = series_config(
        sources=sources,
        page_cap=args.page_cap,
        concurrency=args.concurrency,
        attempts_per_source=args.attempts_per_source,
        max_resume_age_hours=args.max_resume_age_hours,
        max_host_cpu_percent=args.max_host_cpu_percent,
        cpu_sustain_seconds=args.cpu_sustain_seconds,
        cpu_sample_seconds=args.cpu_sample_seconds,
        memory_sustain_seconds=args.memory_sustain_seconds,
        memory_sample_seconds=args.memory_sample_seconds,
        nice=args.nice,
        recovery=recovery_config,
        admit_current_inventory_source=args.admit_current_inventory_source,
        admit_daum_verified_subset=args.admit_daum_verified_subset,
        daum_subset_policy=args.daum_subset_policy,
        admit_baseline_hold_additively=args.admit_baseline_hold_additively,
        pipeline=args.pipeline,
        source_workers=args.source_workers,
        automatic_source_evidence=automatic_source_evidence,
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
        with SeriesOwnershipLock(series_dir / ".series.lock") as series_lock:
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
            # This is a replaceable latest-invocation observation, never config/history.
            manifest.pop("latest_invocation_admission", None)
            if invocation_admission is not None:
                manifest["latest_invocation_admission"] = invocation_admission
            manifest.pop("latest_invocation_control", None)
            save_manifest(series_dir, manifest)

            if args.work_deadline_seconds is not None:
                return run_controlled_series(
                    series_dir,
                    manifest,
                    series_lock,
                    args,
                    _cli_control_capability=_CLI_CONTROL_CAPABILITY,
                )

            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, checkpoint_series_sigterm_handler)
            try:
                return run_series(
                    series_dir,
                    manifest,
                    env_file=args.env_file,
                    retry_failed=args.retry_failed,
                    **(
                        {"expected_sha": args.expected_sha}
                        if args.expected_sha is not None
                        else {}
                    ),
                )
            finally:
                signal.signal(signal.SIGTERM, previous_sigterm)
                save_report(series_dir)
    except RecoveryOwnershipError as exc:
        raise SeriesError(str(exc)) from exc


if __name__ == "__main__":
    _CLI_CONTROL_CAPABILITY = object()
    raise SystemExit(main())
