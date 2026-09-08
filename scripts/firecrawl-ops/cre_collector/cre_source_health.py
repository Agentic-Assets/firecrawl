#!/usr/bin/env python3
"""Build the redaction-safe producer freshness receipt for CRE listings.

The checkpoint series remains the collection system of record. This module
projects its manifests into a small handoff contract that GetCREdata can
publish. A failed, partial, or interrupted attempt updates attempt health but
never erases or moves backward the last complete successful observation.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


CONTRACT_VERSION = "producer-freshness-v1"
PRODUCER_ID = "firecrawl-cre-listings"
CADENCE_SECONDS = 7 * 24 * 60 * 60
GRACE_SECONDS = 3 * 24 * 60 * 60
MAX_CLOCK_SKEW_SECONDS = 5 * 60
TERMINAL_SUCCESS = "supported_scope_complete"
STOPPED_STATES = {"interrupted", "resource_guard_interrupted"}
FAILED_STATES = {"failed_source", "failed_global"}

_SAFE_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TOP_LEVEL_FIELDS = {
    "contractVersion",
    "producerId",
    "producerComputedAt",
    "seriesId",
    "seriesStatus",
    "sources",
}
_SOURCE_FIELDS = {
    "sourceId",
    "state",
    "lastSuccessfulObservationAt",
    "lastAttemptObservationAt",
    "sourceVintage",
    "collectedAt",
    "producerComputedAt",
    "publishedAt",
    "cadenceSeconds",
    "graceSeconds",
    "completeness",
    "lastAttemptAt",
    "attemptStatus",
    "failureClass",
    "publicationStatus",
    "backlogCount",
    "retryCount",
    "deadLetterCount",
    "deterministicFailureCount",
    "transientFailureCount",
    "unclassifiedFailureCount",
}
_COUNT_FIELDS = {
    "backlogCount",
    "retryCount",
    "deadLetterCount",
    "deterministicFailureCount",
    "transientFailureCount",
    "unclassifiedFailureCount",
}
_TIMESTAMP_FIELDS = {
    "lastSuccessfulObservationAt",
    "lastAttemptObservationAt",
    "collectedAt",
    "producerComputedAt",
    "publishedAt",
    "lastAttemptAt",
}
_KNOWN_STATES = {"fresh", "stale", "unknown"}
_KNOWN_ATTEMPT_STATUSES = {
    "not_attempted",
    "running",
    "stopped",
    "failed",
    "succeeded",
}
_KNOWN_PUBLICATION_STATUSES = {
    "not_published",
    "failed",
    "partial",
    "complete",
    "unknown",
}
_KNOWN_FAILURE_CLASSES = {
    "transient_resource_or_operator_stop",
    "source_collection_failure",
    "infrastructure_failure",
}
_KNOWN_LIMITATIONS = {"whole_source_coverage_not_established"}


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _newer_timestamp(current: object, previous: object) -> object:
    current_parsed = _parse_timestamp(current)
    previous_parsed = _parse_timestamp(previous)
    if current_parsed is None:
        return previous
    if previous_parsed is None or current_parsed >= previous_parsed:
        return current
    return previous


def _is_newer_or_equal(current: object, previous: object) -> bool:
    current_parsed = _parse_timestamp(current)
    previous_parsed = _parse_timestamp(previous)
    return current_parsed is not None and (
        previous_parsed is None or current_parsed >= previous_parsed
    )


def _within_clock_skew(observation_at: object, computed_at: object) -> bool:
    observed = _parse_timestamp(observation_at)
    computed = _parse_timestamp(computed_at)
    return (
        observed is not None
        and computed is not None
        and observed <= computed + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS)
    )


def freshness_state(
    observation_at: object,
    *,
    computed_at: object,
    cadence_seconds: object = CADENCE_SECONDS,
    grace_seconds: object = GRACE_SECONDS,
) -> str:
    """Classify freshness from observation time, never publication time."""
    observed = _parse_timestamp(observation_at)
    computed = _parse_timestamp(computed_at)
    if observed is None or computed is None:
        return "unknown"
    if observed > computed + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
        return "unknown"
    if (
        not isinstance(cadence_seconds, int)
        or isinstance(cadence_seconds, bool)
        or cadence_seconds <= 0
    ):
        return "unknown"
    if (
        not isinstance(grace_seconds, int)
        or isinstance(grace_seconds, bool)
        or grace_seconds < 0
    ):
        return "unknown"
    boundary = observed + timedelta(seconds=cadence_seconds + grace_seconds)
    return "stale" if computed > boundary else "fresh"


def summarize_queue_rows(rows: object) -> dict[str, dict[str, int | None]]:
    """Classify a redaction-safe enrichment queue snapshot by source."""
    result: dict[str, dict[str, int | None]] = {}
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        source = row.get("source_key")
        attempts = row.get("attempts")
        if not isinstance(source, str) or not _SAFE_TOKEN.fullmatch(source):
            continue
        try:
            attempt_count = int(attempts)
        except (TypeError, ValueError):
            continue
        if attempt_count < 0:
            continue
        summary = result.setdefault(
            source,
            {
                "backlogCount": 0,
                "retryCount": 0,
                "deadLetterCount": 0,
                "deterministicFailureCount": 0,
                "transientFailureCount": 0,
                "unclassifiedFailureCount": 0,
            },
        )
        summary["backlogCount"] += 1
        if attempt_count >= 5:
            summary["deadLetterCount"] += 1
        else:
            summary["retryCount"] += 1
        failure_class = row.get("failure_class")
        if failure_class == "deterministic":
            summary["deterministicFailureCount"] += 1
        elif failure_class == "transient":
            summary["transientFailureCount"] += 1
        elif attempt_count > 0:
            summary["unclassifiedFailureCount"] += 1
    return result


def _child_queue_health(
    child: Mapping[str, Any] | None, source: str
) -> dict[str, int | None] | None:
    """Project the validator's aggregate queue readback into public counts."""
    if child is None:
        return None
    raw = _mapping(_source_checkpoint(child, source).get("readback")).get(
        "queue_health"
    )
    if not isinstance(raw, Mapping) or raw.get("source_key") != source:
        return None
    mapping = {
        "backlogCount": "backlog_count",
        "retryCount": "retry_count",
        "deadLetterCount": "dead_letter_count",
        "deterministicFailureCount": "deterministic_failure_count",
        "transientFailureCount": "transient_failure_count",
        "unclassifiedFailureCount": "unclassified_failure_count",
    }
    result: dict[str, int | None] = {}
    for public_field, raw_field in mapping.items():
        try:
            value = int(raw.get(raw_field))
        except (TypeError, ValueError):
            return None
        if value < 0:
            return None
        result[public_field] = value
    if result["backlogCount"] != (
        result["retryCount"] + result["deadLetterCount"]
    ) or (
        result["deterministicFailureCount"]
        + result["transientFailureCount"]
        + result["unclassifiedFailureCount"]
        > result["backlogCount"]
    ):
        return None
    return result


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_identifier(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_ID.fullmatch(value) else None


def _safe_token(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_TOKEN.fullmatch(value) else None


def _source_checkpoint(child: Mapping[str, Any], source: str) -> Mapping[str, Any]:
    return _mapping(_mapping(child.get("sources")).get(source))


def _whole_source_observation(
    child: Mapping[str, Any], source: str, *, computed_at: object
) -> str | None:
    """Return the earliest required watermark for a source observation."""
    readback = _mapping(_source_checkpoint(child, source).get("readback"))
    if readback.get("ok") is False:
        return None

    required: list[tuple[str, datetime]] = []
    clocks: list[datetime] = []

    def validate(raw: object, *, select: bool) -> bool:
        parsed = _parse_timestamp(raw)
        if parsed is None:
            return False
        clocks.append(parsed)
        if select:
            required.append((raw, parsed))
        return True

    if not validate(readback.get("earliest_inventory_observed_at"), select=True):
        return None
    if not validate(readback.get("latest_inventory_observed_at"), select=False):
        return None

    detail_earliest = readback.get("earliest_detail_observed_at")
    detail_latest = readback.get("latest_detail_observed_at")
    if detail_earliest is not None or detail_latest is not None:
        if not validate(detail_earliest, select=True) or not validate(
            detail_latest, select=False
        ):
            return None

    inventory_only = _mapping(readback.get("inventory_only"))
    enumerated = inventory_only.get("latest_enumerated_at")
    expected_active = inventory_only.get("expected_active")
    inventory_enumeration_required = (
        isinstance(expected_active, int)
        and not isinstance(expected_active, bool)
        and expected_active > 0
    )
    if inventory_enumeration_required:
        if not validate(enumerated, select=True):
            return None
    elif enumerated not in {None, ""} and not validate(enumerated, select=True):
        return None
    scope_watermark = inventory_only.get("scope_watermark_at")
    if scope_watermark is not None and not validate(scope_watermark, select=True):
        return None

    computed = _parse_timestamp(computed_at)
    if computed is None or any(
        clock > computed + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS) for clock in clocks
    ):
        return None

    return min(required, key=lambda item: item[1])[0]


def _current_attempt(checkpoint: Mapping[str, Any]) -> tuple[str | None, str | None]:
    attempts = checkpoint.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return None, None
    attempt = attempts[-1]
    if not isinstance(attempt, Mapping):
        return None, None
    started = attempt.get("started_at")
    finished = attempt.get("finished_at")
    return (
        started if _parse_timestamp(started) is not None else None,
        finished if _parse_timestamp(finished) is not None else None,
    )


def _load_child(
    series_dir: Path, checkpoint: Mapping[str, Any]
) -> dict[str, Any] | None:
    relative = checkpoint.get("checkpoint_run")
    if not isinstance(relative, str) or not relative:
        return None
    candidate = (series_dir / relative / "manifest.json").resolve()
    try:
        candidate.relative_to(series_dir.resolve())
    except ValueError:
        return None
    return _read_json(candidate)


def _attempt_status(checkpoint: Mapping[str, Any]) -> str:
    state = checkpoint.get("state")
    if state == "complete":
        return "succeeded"
    if state in STOPPED_STATES:
        return "stopped"
    if state in FAILED_STATES:
        return "failed"
    if state == "running":
        return "running"
    return "not_attempted"


def _publication_status(
    checkpoint: Mapping[str, Any],
    child: Mapping[str, Any] | None,
    observation: object,
    *,
    computed_at: object,
) -> str:
    state = checkpoint.get("state")
    if state in FAILED_STATES:
        return "failed"
    if state in STOPPED_STATES or state in {"pending", "running"}:
        return "not_published"
    if (
        state != "complete"
        or child is None
        or child.get("status") != TERMINAL_SUCCESS
        or not _within_clock_skew(observation, computed_at)
    ):
        return "unknown"
    scope = _mapping(child.get("scope"))
    return "partial" if scope.get("whole_source_coverage") is False else "complete"


def _successful_values(
    source: str,
    checkpoint: Mapping[str, Any],
    child: Mapping[str, Any] | None,
    observation: object,
    *,
    computed_at: object,
) -> dict[str, Any] | None:
    if (
        checkpoint.get("state") != "complete"
        or child is None
        or child.get("status") != TERMINAL_SUCCESS
        or _mapping(child.get("scope")).get("whole_source_coverage") is False
        or not _within_clock_skew(observation, computed_at)
    ):
        return None
    child_checkpoint = _source_checkpoint(child, source)
    artifact = _mapping(child_checkpoint.get("artifact"))
    observed_count = artifact.get("staged_unique")
    inventory_only = artifact.get("inventory_only")
    if (
        isinstance(observed_count, int)
        and not isinstance(observed_count, bool)
        and isinstance(inventory_only, int)
        and not isinstance(inventory_only, bool)
    ):
        observed_count += inventory_only
    else:
        observed_count = artifact.get("flat_listings")
    if not isinstance(observed_count, int) or isinstance(observed_count, bool):
        observed_count = None
    scope = _mapping(child.get("scope"))
    observed_at = _parse_timestamp(observation)
    return {
        "lastSuccessfulObservationAt": observation,
        "sourceVintage": observed_at.date().isoformat() if observed_at else None,
        "collectedAt": observation,
        "completeness": {
            "runId": _safe_identifier(child.get("run_id")),
            "artifactSha256": (
                artifact.get("sha256")
                if isinstance(artifact.get("sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
                else None
            ),
            "expectedCount": None,
            "observedCount": observed_count,
            "coverageScope": (_safe_token(scope.get("kind"))),
            "limitations": [],
        },
    }


def _safe_count(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _sanitize_completeness(value: object) -> dict[str, Any] | None:
    raw = _mapping(value)
    if not raw:
        return None
    run_id = raw.get("runId")
    artifact_sha = raw.get("artifactSha256")
    scope = raw.get("coverageScope")
    limitations = raw.get("limitations")
    return {
        "runId": _safe_identifier(run_id),
        "artifactSha256": (
            artifact_sha
            if isinstance(artifact_sha, str)
            and re.fullmatch(r"[0-9a-f]{64}", artifact_sha)
            else None
        ),
        "expectedCount": _safe_count(raw.get("expectedCount")),
        "observedCount": _safe_count(raw.get("observedCount")),
        "coverageScope": (_safe_token(scope)),
        "limitations": (
            [item for item in limitations if item in _KNOWN_LIMITATIONS]
            if isinstance(limitations, list)
            else []
        ),
    }


def _sanitize_previous_source(source: str, value: object) -> dict[str, Any]:
    raw = _mapping(value)
    result: dict[str, Any] = {}
    for field in _TIMESTAMP_FIELDS:
        candidate = raw.get(field)
        if candidate is None or _parse_timestamp(candidate) is not None:
            result[field] = candidate
    for field in _COUNT_FIELDS:
        result[field] = _safe_count(raw.get(field))
    if raw.get("state") in _KNOWN_STATES:
        result["state"] = raw["state"]
    if raw.get("attemptStatus") in _KNOWN_ATTEMPT_STATUSES:
        result["attemptStatus"] = raw["attemptStatus"]
    if raw.get("publicationStatus") in _KNOWN_PUBLICATION_STATUSES:
        result["publicationStatus"] = raw["publicationStatus"]
    if (
        raw.get("failureClass") in _KNOWN_FAILURE_CLASSES
        or raw.get("failureClass") is None
    ):
        result["failureClass"] = raw.get("failureClass")
    source_vintage = raw.get("sourceVintage")
    result["sourceVintage"] = _safe_identifier(source_vintage)
    result["sourceId"] = source
    result["cadenceSeconds"] = CADENCE_SECONDS
    result["graceSeconds"] = GRACE_SECONDS
    result["completeness"] = _sanitize_completeness(raw.get("completeness"))
    return result


def build_receipt(
    series_dir: Path,
    manifest: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None = None,
    queue_rows: object = None,
) -> dict[str, Any]:
    current_computed = manifest.get("updated_at")
    previous_mapping = _mapping(previous)
    receipt_computed = _newer_timestamp(
        current_computed, previous_mapping.get("producerComputedAt")
    )
    previous_sources = _mapping(previous_mapping.get("sources"))
    queue = summarize_queue_rows(queue_rows)
    sources: dict[str, dict[str, Any]] = {
        key: _sanitize_previous_source(key, value)
        for key, value in previous_sources.items()
        if isinstance(key, str) and _SAFE_TOKEN.fullmatch(key)
    }
    manifest_sources = _mapping(manifest.get("sources"))
    for source, checkpoint_value in manifest_sources.items():
        if (
            not isinstance(source, str)
            or not _SAFE_TOKEN.fullmatch(source)
            or not isinstance(checkpoint_value, Mapping)
        ):
            continue
        checkpoint = checkpoint_value
        child = _load_child(series_dir, checkpoint)
        previous_source = _mapping(previous_sources.get(source))
        retained = _sanitize_previous_source(source, previous_source)
        observation = (
            _whole_source_observation(
                child,
                source,
                computed_at=current_computed,
            )
            if child
            else None
        )
        success = _successful_values(
            source,
            checkpoint,
            child,
            observation,
            computed_at=current_computed,
        )
        if success is not None and _is_newer_or_equal(
            success["lastSuccessfulObservationAt"],
            retained.get("lastSuccessfulObservationAt"),
        ):
            retained.update(success)

        source_computed = _newer_timestamp(
            current_computed, retained.get("producerComputedAt")
        )
        successful_observation = retained.get("lastSuccessfulObservationAt")
        retained.update(
            {
                "sourceId": source,
                "state": freshness_state(
                    successful_observation, computed_at=source_computed
                ),
                "lastSuccessfulObservationAt": successful_observation,
                "sourceVintage": retained.get("sourceVintage"),
                "collectedAt": retained.get("collectedAt"),
                "producerComputedAt": source_computed,
                "publishedAt": None,
                "cadenceSeconds": CADENCE_SECONDS,
                "graceSeconds": GRACE_SECONDS,
                "completeness": retained.get("completeness"),
            }
        )

        attempt_started, attempt_finished = _current_attempt(checkpoint)
        attempt_at = attempt_finished or attempt_started
        should_update_attempt = checkpoint.get(
            "state"
        ) != "pending" and _is_newer_or_equal(attempt_at, retained.get("lastAttemptAt"))
        if should_update_attempt:
            attempt_status = _attempt_status(checkpoint)
            failure_class = None
            if attempt_status == "stopped":
                failure_class = "transient_resource_or_operator_stop"
            elif checkpoint.get("state") == "failed_source":
                failure_class = "source_collection_failure"
            elif checkpoint.get("state") == "failed_global":
                failure_class = "infrastructure_failure"
            retained.update(
                {
                    "lastAttemptAt": attempt_at,
                    "lastAttemptObservationAt": observation,
                    "attemptStatus": attempt_status,
                    "failureClass": failure_class,
                    "publicationStatus": _publication_status(
                        checkpoint,
                        child,
                        observation,
                        computed_at=current_computed,
                    ),
                }
            )

        queue_health = queue.get(source) or _child_queue_health(child, source)
        if queue_health is not None:
            retained.update(queue_health)
        else:
            for field in _COUNT_FIELDS:
                retained.setdefault(field, None)
        retained.setdefault("lastAttemptAt", None)
        retained.setdefault("lastAttemptObservationAt", None)
        retained.setdefault("attemptStatus", "not_attempted")
        retained.setdefault("failureClass", None)
        retained.setdefault("publicationStatus", "not_published")
        sources[source] = retained

    previous_computed = previous_mapping.get("producerComputedAt")
    current_is_newest = _is_newer_or_equal(current_computed, previous_computed)
    return {
        "contractVersion": CONTRACT_VERSION,
        "producerId": PRODUCER_ID,
        "producerComputedAt": receipt_computed,
        "seriesId": (
            _safe_identifier(manifest.get("series_id"))
            if current_is_newest
            else _safe_identifier(previous_mapping.get("seriesId"))
        ),
        "seriesStatus": (
            _safe_token(manifest.get("status"))
            if current_is_newest
            else _safe_token(previous_mapping.get("seriesStatus"))
        ),
        "sources": dict(sorted(sources.items())),
    }


def _prior_receipt_is_valid(value: Mapping[str, Any]) -> bool:
    if set(value) != _TOP_LEVEL_FIELDS:
        return False
    if (
        value.get("contractVersion") != CONTRACT_VERSION
        or value.get("producerId") != PRODUCER_ID
        or _parse_timestamp(value.get("producerComputedAt")) is None
        or _safe_identifier(value.get("seriesId")) is None
        or _safe_token(value.get("seriesStatus")) is None
    ):
        return False
    sources = value.get("sources")
    if not isinstance(sources, Mapping):
        return False
    for source, source_value in sources.items():
        if (
            not isinstance(source, str)
            or not _SAFE_TOKEN.fullmatch(source)
            or not isinstance(source_value, Mapping)
            or source_value.get("sourceId") != source
            or set(source_value) - _SOURCE_FIELDS
            or (_SOURCE_FIELDS - {"lastAttemptObservationAt"}) - set(source_value)
        ):
            return False
        sanitized = _sanitize_previous_source(source, source_value)
        for field in _TIMESTAMP_FIELDS:
            if field in source_value and source_value.get(field) != sanitized.get(
                field
            ):
                return False
        if source_value.get("state") not in _KNOWN_STATES:
            return False
        if source_value.get("attemptStatus") not in _KNOWN_ATTEMPT_STATUSES:
            return False
        if source_value.get("publicationStatus") not in _KNOWN_PUBLICATION_STATUSES:
            return False
        if (
            source_value.get("failureClass") not in _KNOWN_FAILURE_CLASSES
            and source_value.get("failureClass") is not None
        ):
            return False
        if (
            source_value.get("cadenceSeconds") != CADENCE_SECONDS
            or source_value.get("graceSeconds") != GRACE_SECONDS
            or source_value.get("sourceVintage") != sanitized.get("sourceVintage")
            or source_value.get("completeness") != sanitized.get("completeness")
        ):
            return False
        for field in _COUNT_FIELDS:
            if source_value.get(field) != sanitized.get(field):
                return False
    return True


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:  # pragma: no cover - defensive OS boundary
                raise OSError("short write while publishing source health")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


@contextmanager
def _canonical_lock(canonical: Path) -> Iterator[None]:
    lock_path = canonical.with_name(f".{canonical.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_publication_status(
    series_dir: Path,
    manifest: Mapping[str, Any],
    *,
    status: str,
    reason: str | None,
) -> None:
    """Best-effort, code-only publication health with no exception details."""
    try:
        _atomic_write(
            series_dir / "source-health-publication.json",
            {
                "schemaVersion": 1,
                "status": status,
                "reason": reason,
                "producerComputedAt": (
                    manifest.get("updated_at")
                    if _parse_timestamp(manifest.get("updated_at")) is not None
                    else None
                ),
            },
        )
    except Exception:
        pass


def publish_series_health(series_dir: Path, manifest: Mapping[str, Any]) -> Path:
    """Serialize monotonic per-series and canonical receipt publication."""
    canonical = series_dir.parent / "producer-source-health.json"
    try:
        with _canonical_lock(canonical):
            previous = _read_json(canonical) if canonical.exists() else None
            prior_invalid = canonical.exists() and (
                previous is None or not _prior_receipt_is_valid(previous)
            )
            receipt = build_receipt(
                series_dir,
                manifest,
                previous=None if prior_invalid else previous,
            )
            _atomic_write(series_dir / "source-health.json", receipt)
            if prior_invalid:
                _write_publication_status(
                    series_dir,
                    manifest,
                    status="degraded",
                    reason="canonical_receipt_invalid",
                )
                return canonical
            _atomic_write(canonical, receipt)
            _write_publication_status(series_dir, manifest, status="ok", reason=None)
    except Exception:
        _write_publication_status(
            series_dir,
            manifest,
            status="degraded",
            reason="receipt_output_failed",
        )
        raise RuntimeError("producer source-health publication failed") from None
    return canonical
