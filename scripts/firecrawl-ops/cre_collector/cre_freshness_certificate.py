#!/usr/bin/env python3
"""Read-only certificate for a completed union of CRE source checkpoints.

The checkpoint runner remains the only mutating orchestrator.  This tool only
reads explicit run directories (or their manifest.json files), validates their
durable terminal evidence, and writes one JSON certificate to stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from cre_checkpoint_refresh import (
    DEFAULT_MAX_OBSERVATION_AGE_HOURS,
    MAX_FUTURE_CLOCK_SKEW,
    TRANSACTIONS,
    parse_iso8601,
)
from cre_ingest import SOURCE_TO_BROKERAGE
from cre_source_policy import SourcePolicyValidationError, load_source_policy


CERTIFICATE_VERSION = 2
SOURCE_KEYS = tuple(SOURCE_TO_BROKERAGE)
TERMINAL_STATE = "ingested"
TERMINAL_RUN_STATUS = "supported_scope_complete"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _failure(
    failures: list[dict[str, str]],
    code: str,
    message: str,
    *,
    run_path: Path | None = None,
    source_key: str | None = None,
) -> None:
    entry = {"code": code, "message": message}
    if run_path is not None:
        entry["run_path"] = str(run_path)
    if source_key is not None:
        entry["source_key"] = source_key
    failures.append(entry)


def _manifest_path(run_path: str | Path) -> Path:
    path = Path(run_path).expanduser().resolve()
    return path if path.name == "manifest.json" else path / "manifest.json"


def _load_manifest(path: Path) -> Mapping[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("manifest root must be an object")
    return value


def _is_exact_source_list(value: Any, expected: list[str]) -> bool:
    return isinstance(value, list) and value == expected and len(value) == len(set(value))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_artifact_evidence(
    artifact_path: Path,
    source_key: str,
    evidence: Mapping[str, object],
    failures: list[dict[str, str]],
    *,
    run_path: Path,
    reported_inventory_only: object,
    observation_cutoff: datetime,
    latest_allowed: datetime,
) -> None:
    """Validate the on-disk collection proof, not just its manifest summary."""
    try:
        with artifact_path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _failure(failures, "artifact_evidence", f"cannot parse source artifact: {exc}", run_path=run_path, source_key=source_key)
        return
    if not isinstance(payload, dict):
        _failure(failures, "artifact_evidence", "source artifact root must be an object", run_path=run_path, source_key=source_key)
        return
    freshness = (payload.get("runMeta") or {}).get("freshness") if isinstance(payload.get("runMeta"), dict) else None
    listings = payload.get("listings")
    if not isinstance(listings, list) or not listings:
        _failure(failures, "artifact_evidence", "source artifact lacks listings", run_path=run_path, source_key=source_key)
        return
    if any(not isinstance(row, dict) or row.get("sourceKey") != source_key for row in listings):
        _failure(failures, "artifact_evidence", "source artifact listing identity is inconsistent", run_path=run_path, source_key=source_key)
        return

    evidence_class = evidence["evidence_class"]
    child_contract = evidence["child_contract"]
    inventory_namespace = evidence["inventory_only_namespace"]
    supported_evidence_classes = {
        "strict_detail",
        "property_detail",
        "authoritative_inventory_feed",
        "inventory_only_namespace",
    }
    if evidence_class not in supported_evidence_classes:
        _failure(failures, "artifact_evidence", f"unsupported policy evidence class {evidence_class!r}", run_path=run_path, source_key=source_key)
        return
    provisional_rows: list[dict[str, Any]] = []
    canonical_rows: list[dict[str, Any]] = []
    for row in listings:
        if isinstance(row.get("inventoryOnly"), dict) or isinstance(row.get("provisionalIdentity"), dict):
            provisional_rows.append(row)
        else:
            canonical_rows.append(row)
    if inventory_namespace is None and provisional_rows:
        _failure(failures, "artifact_evidence", "artifact has provisional rows without a policy source-index namespace", run_path=run_path, source_key=source_key)
        return
    if inventory_namespace is not None:
        if not _is_int(reported_inventory_only) or reported_inventory_only != len(provisional_rows):
            _failure(failures, "artifact_evidence", "artifact provisional-row count does not match source-index accounting", run_path=run_path, source_key=source_key)
            return
        for row in provisional_rows:
            if not isinstance(row.get("inventoryOnly"), dict) or not isinstance(row.get("provisionalIdentity"), dict):
                _failure(failures, "artifact_evidence", "provisional row lacks explicit index and identity markers", run_path=run_path, source_key=source_key)
                return
            provenance = row.get("freshnessProvenance")
            if row.get("detailObservedAt") or (
                isinstance(provenance, dict) and provenance.get("detailScope") == "detail_page"
            ):
                _failure(failures, "artifact_evidence", "provisional source-index row must not claim canonical detail evidence", run_path=run_path, source_key=source_key)
                return
    if evidence_class == "inventory_only_namespace":
        if canonical_rows:
            _failure(failures, "artifact_evidence", "pure inventory-only policy cannot certify canonical rows", run_path=run_path, source_key=source_key)
        return
    if not canonical_rows:
        _failure(failures, "artifact_evidence", "canonical policy source has no canonical artifact rows", run_path=run_path, source_key=source_key)
        return
    if not isinstance(freshness, dict):
        _failure(failures, "artifact_evidence", "canonical artifact rows lack freshness metadata", run_path=run_path, source_key=source_key)
        return
    if evidence_class in {"strict_detail", "authoritative_inventory_feed"}:
        if freshness.get("requireFreshDetails") is not True:
            _failure(failures, "artifact_evidence", "artifact lacks strict freshness assertion", run_path=run_path, source_key=source_key)
            return
    elif evidence_class == "property_detail":
        if freshness.get("requireFreshDetails") is not False or freshness.get("requireFreshPropertyDetails") is not True:
            _failure(failures, "artifact_evidence", "artifact lacks property-detail freshness assertion", run_path=run_path, source_key=source_key)
            return

    for row in canonical_rows:
        provenance = row.get("freshnessProvenance")
        if not isinstance(provenance, dict):
            _failure(failures, "artifact_evidence", "listing lacks freshness provenance", run_path=run_path, source_key=source_key)
            return
        if evidence_class == "authoritative_inventory_feed":
            if provenance.get("detailScope") != "authoritative_inventory_feed" or not row.get("inventoryObservedAt"):
                _failure(failures, "artifact_evidence", "inventory-feed listing lacks current inventory proof", run_path=run_path, source_key=source_key)
                return
            try:
                inventory_observed = parse_iso8601(
                    row.get("inventoryObservedAt"),
                    field="inventoryObservedAt",
                )
            except Exception as exc:
                _failure(failures, "observation_age", str(exc), run_path=run_path, source_key=source_key)
                return
            if inventory_observed > latest_allowed:
                _failure(failures, "observation_age", "inventory observation exceeds certificate clock-skew allowance", run_path=run_path, source_key=source_key)
                return
            if inventory_observed < observation_cutoff:
                _failure(failures, "observation_age", "inventory observation exceeds certificate freshness SLO", run_path=run_path, source_key=source_key)
                return
            preserves = row.get("preserveChildCollections") is True
            if (child_contract == "preserve_existing_children") != preserves:
                _failure(failures, "artifact_evidence", "inventory-feed child contract conflicts with policy", run_path=run_path, source_key=source_key)
                return
        elif evidence_class in {"strict_detail", "property_detail"}:
            if provenance.get("detailScope") != "detail_page" or not row.get("detailObservedAt"):
                _failure(failures, "artifact_evidence", "detail listing lacks current detail proof", run_path=run_path, source_key=source_key)
                return
            try:
                inventory_observed = parse_iso8601(
                    row.get("inventoryObservedAt"),
                    field="inventoryObservedAt",
                )
                detail_value = row.get("detailObservedAt")
                if (
                    provenance.get("cacheDisposition") == "source_revision_cache"
                    and provenance.get("validatedAt")
                ):
                    detail_value = provenance.get("validatedAt")
                detail_observed = parse_iso8601(
                    detail_value,
                    field="detailObservedAt",
                )
            except Exception as exc:
                _failure(failures, "observation_age", str(exc), run_path=run_path, source_key=source_key)
                return
            if (
                inventory_observed > latest_allowed
                or detail_observed > latest_allowed
            ):
                _failure(failures, "observation_age", "canonical observation exceeds certificate clock-skew allowance", run_path=run_path, source_key=source_key)
                return
            if (
                inventory_observed < observation_cutoff
                or detail_observed < observation_cutoff
            ):
                _failure(failures, "observation_age", "canonical detail observation exceeds certificate freshness SLO", run_path=run_path, source_key=source_key)
                return
            if evidence_class == "strict_detail" and row.get("preserveChildCollections") is True:
                _failure(failures, "artifact_evidence", "strict-detail listing cannot preserve child collections", run_path=run_path, source_key=source_key)
                return
            if row.get("preserveChildCollections") is True and row.get("detailObservedWithChildPreservation") is not True:
                _failure(failures, "artifact_evidence", "preserved property-detail listing lacks current-detail preservation proof", run_path=run_path, source_key=source_key)
                return


def _validate_run(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    policy: Mapping[str, Mapping[str, object]],
    failures: list[dict[str, str]],
    source_records: dict[str, dict[str, Any]],
    *,
    max_source_age_hours: float,
    max_observation_age_hours: float,
    now: datetime,
) -> None:
    run_path = manifest_path.parent
    config = manifest.get("config")
    scope = manifest.get("scope")
    checkpoints = manifest.get("sources")
    if not isinstance(config, dict) or not isinstance(scope, dict) or not isinstance(checkpoints, dict):
        _failure(failures, "manifest_shape", "manifest lacks config, scope, or sources object", run_path=run_path)
        return

    configured_sources = config.get("sources")
    if not isinstance(configured_sources, list) or not all(isinstance(key, str) for key in configured_sources):
        _failure(failures, "source_scope", "config.sources must be a source-key list", run_path=run_path)
        return
    if len(configured_sources) != len(set(configured_sources)):
        _failure(failures, "duplicate_configured_source", "config.sources contains a duplicate key", run_path=run_path)
    if set(configured_sources) - set(SOURCE_KEYS):
        _failure(failures, "unknown_source", "config.sources contains an unknown source key", run_path=run_path)
    if set(checkpoints) != set(configured_sources):
        _failure(failures, "source_scope", "manifest sources do not exactly match config.sources", run_path=run_path)
    if not _is_exact_source_list(scope.get("source_keys"), configured_sources):
        _failure(failures, "source_scope", "scope.source_keys must exactly match config.sources", run_path=run_path)
    if scope.get("kind") != "collector_registry":
        _failure(failures, "subset_scope", "certificate rejects a non-registry source scope", run_path=run_path)
    if config.get("transactions") != list(TRANSACTIONS):
        _failure(failures, "subset_scope", "certificate requires both sale and lease transactions", run_path=run_path)
    if config.get("max_items") != 0:
        _failure(failures, "subset_scope", "certificate requires an unlimited source collection", run_path=run_path)
    if config.get("additive") is not True:
        _failure(failures, "non_additive", "certificate requires additive ingest", run_path=run_path)
    if config.get("status_activation") is not False:
        _failure(failures, "status_activation", "certificate rejects status activation", run_path=run_path)
    if config.get("mark_missing") is not False:
        _failure(failures, "mark_missing", "certificate rejects mark-missing", run_path=run_path)
    if config.get("admit_baseline_hold_additively") is True:
        _failure(failures, "baseline_hold", "certificate rejects additive baseline-hold admission", run_path=run_path)
    if manifest.get("status") != TERMINAL_RUN_STATUS:
        _failure(failures, "run_not_terminal", f"run status must be {TERMINAL_RUN_STATUS}", run_path=run_path)
    if manifest.get("error") not in (None, ""):
        _failure(failures, "run_error", "terminal run has a recorded error", run_path=run_path)

    aggregate = manifest.get("aggregate_gate")
    validation = manifest.get("validation")
    preflight = manifest.get("preflight")
    if not isinstance(aggregate, dict) or not isinstance(validation, dict) or not isinstance(preflight, dict):
        _failure(failures, "integrity_failure", "manifest lacks aggregate, validation, or preflight evidence", run_path=run_path)
    else:
        if aggregate.get("rc") != 0 or aggregate.get("non_ok_sources") not in ([], None):
            _failure(failures, "integrity_failure", "aggregate gate is not clean", run_path=run_path)
        if aggregate.get("hold_sources") not in ([], None) or aggregate.get("baseline_advisory_holds") not in ([], None):
            _failure(failures, "baseline_hold", "aggregate gate records a hold", run_path=run_path)
        if preflight.get("healthcheck_rc") != 0 or preflight.get("validation_rc") != 0:
            _failure(failures, "integrity_failure", "preflight healthcheck or validation failed", run_path=run_path)
        for field in ("query_execution_ok", "quality_no_regression", "readback_ok"):
            if validation.get(field) is not True:
                _failure(failures, "integrity_failure", f"validation {field} is not true", run_path=run_path)
        if validation.get("absolute_quality_ok") is not True or validation.get("absolute_quality_failures") not in ([], None):
            _failure(failures, "absolute_quality", "final validation lacks clean absolute-quality proof", run_path=run_path)
        if validation.get("failed_readback_sources") not in ([], None) or validation.get("quality_failures") not in ([], None):
            _failure(failures, "integrity_failure", "final validation records source or quality failures", run_path=run_path)

    for source_key in configured_sources:
        if source_key not in policy:
            _failure(failures, "unknown_source", "source has no policy evidence", run_path=run_path, source_key=source_key)
            continue
        checkpoint = checkpoints.get(source_key)
        if not isinstance(checkpoint, dict):
            _failure(failures, "source_checkpoint", "source checkpoint must be an object", run_path=run_path, source_key=source_key)
            continue
        if checkpoint.get("state") != TERMINAL_STATE:
            _failure(failures, "source_not_terminal", "source state must be ingested", run_path=run_path, source_key=source_key)
            continue
        if source_key in source_records:
            _failure(failures, "duplicate_terminal_source", "source occurs in more than one terminal checkpoint", run_path=run_path, source_key=source_key)
            continue
        artifact = checkpoint.get("artifact")
        gate = checkpoint.get("gate")
        ingest = checkpoint.get("ingest")
        readback = checkpoint.get("readback")
        if not all(isinstance(value, dict) for value in (artifact, gate, ingest, readback)):
            _failure(failures, "source_integrity", "source lacks artifact, gate, ingest, or readback evidence", run_path=run_path, source_key=source_key)
            continue
        artifact_path_value = artifact.get("path")
        artifact_sha = artifact.get("sha256")
        artifact_path = None
        if isinstance(artifact_path_value, str):
            candidate = (run_path / artifact_path_value).resolve()
            if candidate.is_relative_to(run_path.resolve()):
                artifact_path = candidate
            else:
                _failure(failures, "artifact_path", "source artifact must remain inside its run directory", run_path=run_path, source_key=source_key)
        if not artifact_path or not artifact_path.is_file():
            _failure(failures, "artifact_missing", "source artifact is missing", run_path=run_path, source_key=source_key)
        else:
            if not isinstance(artifact_sha, str) or sha256_file(artifact_path) != artifact_sha:
                _failure(failures, "artifact_sha256_mismatch", "source artifact digest does not match manifest", run_path=run_path, source_key=source_key)
            if not _is_int(artifact.get("bytes")) or artifact["bytes"] != artifact_path.stat().st_size:
                _failure(failures, "artifact_size_mismatch", "source artifact size does not match manifest", run_path=run_path, source_key=source_key)
        if not _is_int(artifact.get("staged_unique")) or artifact["staged_unique"] < 0:
            _failure(failures, "source_integrity", "artifact staged_unique must be a nonnegative integer", run_path=run_path, source_key=source_key)
        if gate.get("rc") != 0 or gate.get("verdict") != "ok" or gate.get("raw_verdict") in {"hold", "first_seen"}:
            _failure(failures, "source_gate", "source coverage gate is not an unheld ok verdict", run_path=run_path, source_key=source_key)
        dry_run = checkpoint.get("dry_run")
        if not isinstance(dry_run, dict) or dry_run.get("rc") != 0:
            _failure(failures, "source_integrity", "source dry-run evidence is not successful", run_path=run_path, source_key=source_key)
        if ingest.get("rc") != 0 or ingest.get("additive") is not True:
            _failure(failures, "non_additive", "source ingest is not a successful additive write", run_path=run_path, source_key=source_key)
        if ingest.get("status_activation") is not False:
            _failure(failures, "status_activation", "source ingest enabled status activation", run_path=run_path, source_key=source_key)
        if ingest.get("mark_missing") is not False:
            _failure(failures, "mark_missing", "source ingest enabled mark-missing", run_path=run_path, source_key=source_key)
        if readback.get("ok") is not True:
            _failure(failures, "source_readback", "source readback is not ok", run_path=run_path, source_key=source_key)
        try:
            completed_at = parse_iso8601(ingest.get("finished_at"), field=f"{source_key}.ingest.finished_at")
        except Exception as exc:
            _failure(failures, "source_age", str(exc), run_path=run_path, source_key=source_key)
            continue
        age_hours = (now - completed_at).total_seconds() / 3600
        if age_hours < 0 or age_hours > max_source_age_hours:
            _failure(failures, "source_age", f"source age {age_hours:.3f}h exceeds certificate limit", run_path=run_path, source_key=source_key)

        evidence = policy[source_key]
        if artifact_path and artifact_path.is_file() and isinstance(artifact_sha, str) and sha256_file(artifact_path) == artifact_sha:
            _validate_artifact_evidence(
                artifact_path,
                source_key,
                evidence,
                failures,
                run_path=run_path,
                reported_inventory_only=artifact.get("inventory_only"),
                observation_cutoff=now - timedelta(hours=max_observation_age_hours),
                latest_allowed=now + MAX_FUTURE_CLOCK_SKEW,
            )
        evidence_class = evidence["evidence_class"]
        if evidence_class in {"strict_detail", "authoritative_inventory_feed"} and artifact.get("strict_freshness") is not True:
            _failure(failures, "evidence_contract", f"{evidence_class} source lacks strict freshness proof", run_path=run_path, source_key=source_key)
        if evidence_class == "property_detail" and artifact.get("property_detail_freshness") is not True:
            _failure(failures, "evidence_contract", "property-detail source lacks current detail proof", run_path=run_path, source_key=source_key)
        if evidence_class == "inventory_only_namespace" and not _is_int(artifact.get("inventory_only")):
            _failure(failures, "evidence_contract", "inventory-only source lacks inventory-only accounting", run_path=run_path, source_key=source_key)
        source_records[source_key] = {
            "source_key": source_key,
            "run_path": str(run_path),
            "completed_at": completed_at.isoformat(),
            "age_hours": round(age_hours, 6),
            "evidence": dict(evidence),
            "provisional_source_index_count": artifact.get("inventory_only"),
        }


def build_freshness_certificate(
    run_paths: Sequence[str | Path],
    *,
    max_source_age_hours: float,
    max_observation_age_hours: float = DEFAULT_MAX_OBSERVATION_AGE_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a read-only certificate for explicit checkpoint-run directories."""
    current = (now or utc_now()).astimezone(timezone.utc)
    failures: list[dict[str, str]] = []
    source_records: dict[str, dict[str, Any]] = {}
    if not math.isfinite(max_source_age_hours) or max_source_age_hours <= 0:
        _failure(failures, "argument", "max_source_age_hours must be finite and positive")
    if (
        not math.isfinite(max_observation_age_hours)
        or max_observation_age_hours <= 0
    ):
        _failure(failures, "argument", "max_observation_age_hours must be finite and positive")
    if not run_paths:
        _failure(failures, "argument", "at least one explicit checkpoint run path is required")
    try:
        policy = load_source_policy()
    except SourcePolicyValidationError as exc:
        _failure(failures, "policy_integrity", str(exc))
        policy = {}
    seen_paths: set[Path] = set()
    for raw_path in run_paths:
        manifest_path = _manifest_path(raw_path)
        if manifest_path in seen_paths:
            _failure(failures, "duplicate_run_path", "checkpoint run path was supplied more than once", run_path=manifest_path.parent)
            continue
        seen_paths.add(manifest_path)
        try:
            manifest = _load_manifest(manifest_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            _failure(failures, "manifest_read", str(exc), run_path=manifest_path.parent)
            continue
        if policy:
            _validate_run(
                manifest_path,
                manifest,
                policy,
                failures,
                source_records,
                max_source_age_hours=max_source_age_hours,
                max_observation_age_hours=max_observation_age_hours,
                now=current,
            )

    observed = set(source_records)
    expected = set(SOURCE_KEYS)
    missing = sorted(expected - observed)
    extra = sorted(observed - expected)
    if missing or extra:
        _failure(failures, "source_union", f"certificate requires exact 51-source union; missing={missing} extra={extra}")
    sources = [source_records[key] for key in SOURCE_KEYS if key in source_records]
    return {
        "certificate_version": CERTIFICATE_VERSION,
        "status": "valid" if not failures and len(sources) == len(SOURCE_KEYS) else "invalid",
        "generated_at": current.isoformat(),
        "max_source_age_hours": max_source_age_hours,
        "max_observation_age_hours": max_observation_age_hours,
        "expected_source_count": len(SOURCE_KEYS),
        "certified_source_count": len(sources),
        "sources": sources,
        "failures": failures,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint_runs", nargs="+", help="explicit checkpoint run directories or manifest.json files")
    parser.add_argument("--max-source-age-hours", type=float, required=True)
    parser.add_argument(
        "--max-observation-age-hours",
        type=float,
        default=DEFAULT_MAX_OBSERVATION_AGE_HOURS,
        help="maximum age of every canonical source observation at certification time",
    )
    args = parser.parse_args(argv)
    certificate = build_freshness_certificate(
        args.checkpoint_runs,
        max_source_age_hours=args.max_source_age_hours,
        max_observation_age_hours=args.max_observation_age_hours,
    )
    print(json.dumps(certificate, indent=2, sort_keys=True))
    return 0 if certificate["status"] == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())
