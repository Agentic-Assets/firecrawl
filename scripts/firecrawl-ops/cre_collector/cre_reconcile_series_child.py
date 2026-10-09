#!/usr/bin/env python3
"""Reconcile a failed series parent after its exact child completed on review.

This is an operator command for the narrow case where a live ingest was
interrupted, the child proved an exact rollback, and a reviewed child resume
subsequently completed. It does not collect, ingest, or change a child run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cre_checkpoint_series import _exact_json_equal, _expected_child_config
from cre_resource_recovery import RecoveryOwnershipError, SeriesOwnershipLock

SUCCESS_STATUS = "supported_scope_complete"


class ReconciliationError(Exception):
    pass


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReconciliationError(
            f"cannot read {path.name}: {type(exc).__name__}"
        ) from exc
    if not isinstance(value, dict):
        raise ReconciliationError(f"{path.name} must contain an object")
    return value


def _object_field(value: Any, name: str) -> dict[str, Any]:
    """Return a manifest sub-object, treating only an absent one as empty."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ReconciliationError(f"child {name} is malformed")
    return value


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ReconciliationError(f"child {field} is missing")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ReconciliationError(f"child {field} is invalid") from exc
    if parsed.tzinfo is None:
        raise ReconciliationError(f"child {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _require_resume_age_within_limit(
    parent_config: dict[str, Any], child: dict[str, Any]
) -> None:
    """Refuse a child whose run outlived the series resume-age limit.

    The child refuses to resume a generation older than its
    ``--max-resume-age-hours`` measured from the manifest ``started_at``. A
    child that completed later than that limit after ``started_at`` can only
    have been resumed under an age override, which needs founder sign-off.
    """
    if "max_resume_age_hours" not in parent_config:
        return
    limit = parent_config["max_resume_age_hours"]
    if (
        isinstance(limit, bool)
        or not isinstance(limit, (int, float))
        or not math.isfinite(limit)
        or limit <= 0
    ):
        raise ReconciliationError("parent max_resume_age_hours is malformed")
    started_at = _parse_timestamp(child.get("started_at"), "started_at")
    finished_at = _parse_timestamp(child.get("finished_at"), "finished_at")
    if finished_at < started_at:
        raise ReconciliationError("child run timestamps are inconsistent")
    if finished_at - started_at > timedelta(hours=limit):
        raise ReconciliationError(
            f"child run span exceeds the series max_resume_age_hours ({limit:g}); "
            "an age override needs founder sign-off on AGENTIC-3045, then "
            "--acknowledge-resume-age-override"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_clean_checkout(series_dir: Path, expected_sha: str) -> None:
    if (
        series_dir.parent.name != "checkpoint-series"
        or series_dir.parent.parent.name != "out"
    ):
        raise ReconciliationError("series directory is outside the checkpoint layout")
    checkout = series_dir.parents[2]
    try:
        head = subprocess.check_output(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
        ).strip()
        dirty = subprocess.check_output(
            ["git", "-C", str(checkout), "status", "--porcelain"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReconciliationError("cannot verify collector checkout") from exc
    if head != expected_sha or dirty:
        raise ReconciliationError("collector checkout SHA or cleanliness differs")


def validate_reconciliation(
    series_dir: Path,
    *,
    source: str,
    expected_sha: str,
    expected_child_run: str,
    expected_artifact_sha256: str,
    acknowledge_resume_age_override: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if Path(expected_child_run).name != expected_child_run or expected_child_run in {
        ".",
        "..",
    }:
        raise ReconciliationError("child run must be one exact directory name")
    parent = _load_object(series_dir / "manifest.json")
    if (
        parent.get("schema_version") != 1
        or parent.get("collector_git_sha") != expected_sha
    ):
        raise ReconciliationError("parent schema or collector SHA differs")
    if parent.get("status") != "failed":
        raise ReconciliationError("parent must be failed")
    parent_config = parent.get("config")
    parent_sources = parent.get("sources")
    if not isinstance(parent_config, dict) or not isinstance(parent_sources, dict):
        raise ReconciliationError("parent manifest is malformed")
    scope = parent_config.get("sources")
    if not isinstance(scope, list) or source not in scope:
        raise ReconciliationError("source is not in the exact series scope")
    checkpoint = parent_sources.get(source)
    if not isinstance(checkpoint, dict) or checkpoint.get("state") != "failed_global":
        raise ReconciliationError("parent source is not failed_global")
    child_relative = Path("runs") / expected_child_run
    if checkpoint.get("checkpoint_run") != str(child_relative):
        raise ReconciliationError("parent is not bound to the expected child")
    attempts = checkpoint.get("attempts")
    if (
        not isinstance(attempts, list)
        or not attempts
        or not isinstance(attempts[-1], dict)
        or attempts[-1].get("rc") in (None, 0)
    ):
        raise ReconciliationError("parent lacks a recorded failed child attempt")
    try:
        expected_child_config = _expected_child_config(source, parent_config)
    except (KeyError, TypeError) as exc:
        raise ReconciliationError("parent manifest is malformed") from exc

    child_dir = series_dir / child_relative
    if child_dir.resolve().parent != (series_dir / "runs").resolve():
        raise ReconciliationError("child path escapes the series")
    child = _load_object(child_dir / "manifest.json")
    if (
        child.get("schema_version") != 2
        or child.get("collector_git_sha") != expected_sha
    ):
        raise ReconciliationError("child schema or collector SHA differs")
    if (
        child.get("run_id") != expected_child_run
        or child.get("status") != SUCCESS_STATUS
    ):
        raise ReconciliationError("expected child has not completed")
    validation = _object_field(child.get("validation"), "validation")
    if validation.get("rc") != 0 or validation.get("readback_ok") is not True:
        raise ReconciliationError("child final validation is incomplete")
    if not _exact_json_equal(child.get("config"), expected_child_config):
        raise ReconciliationError("child configuration differs from the series")
    if not acknowledge_resume_age_override:
        _require_resume_age_within_limit(parent_config, child)
    parent_target = parent.get("database_target")
    preflight = child.get("preflight")
    child_target = (
        preflight.get("database_target") if isinstance(preflight, dict) else None
    )
    if parent_target is not None and not _exact_json_equal(child_target, parent_target):
        raise ReconciliationError("child database target differs from the series")
    child_checkpoint = _object_field(child.get("sources"), "sources").get(source)
    if (
        not isinstance(child_checkpoint, dict)
        or child_checkpoint.get("state") != "ingested"
    ):
        raise ReconciliationError("child source is not ingested")
    recovery = _object_field(child_checkpoint.get("ingest_recovery"), "ingest_recovery")
    if (
        recovery.get("outcome") != "exact_rollback"
        or recovery.get("replay_safe") is not True
    ):
        raise ReconciliationError("child lacks exact rollback evidence")
    ingest = _object_field(child_checkpoint.get("ingest"), "ingest")
    if ingest.get("rc") != 0 or ingest.get("finished_at") is None:
        raise ReconciliationError("child lacks a completed live ingest")
    readback = _object_field(child_checkpoint.get("readback"), "readback")
    if (
        readback.get("ok") is not True
        or readback.get("generation_id") != expected_child_run
    ):
        raise ReconciliationError("child lacks exact generation readback")
    artifact = _object_field(child_checkpoint.get("artifact"), "artifact")
    staged_unique = artifact.get("staged_unique")
    if (
        type(staged_unique) is not int
        or staged_unique < 0
        or type(readback.get("expected_staged_unique")) is not int
    ):
        raise ReconciliationError("child readback count is missing or malformed")
    if readback.get("expected_staged_unique") != staged_unique:
        raise ReconciliationError("child readback count differs from the artifact")
    if artifact.get("sha256") != expected_artifact_sha256:
        raise ReconciliationError("artifact digest differs from expected")
    relative_artifact = Path(str(artifact.get("path") or ""))
    artifact_path = child_dir / relative_artifact
    if (
        relative_artifact.is_absolute()
        or artifact_path.resolve().parent != (child_dir / "sources").resolve()
    ):
        raise ReconciliationError("artifact path escapes the child")
    if (
        not artifact_path.is_file()
        or _sha256(artifact_path) != expected_artifact_sha256
    ):
        raise ReconciliationError("immutable artifact bytes differ")
    return parent, child


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main() -> int:
    try:
        return _run()
    except (OSError, ReconciliationError, RecoveryOwnershipError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


def _run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series-dir", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--expected-collector-sha", required=True)
    parser.add_argument("--expected-child-run", required=True)
    parser.add_argument("--expected-artifact-sha256", required=True)
    parser.add_argument(
        "--acknowledge-resume-age-override",
        action="store_true",
        help=(
            "admit a child whose run span exceeds the series max_resume_age_hours; "
            "requires founder sign-off recorded on AGENTIC-3045"
        ),
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    series_dir = args.series_dir.resolve()
    if Path(
        args.expected_child_run
    ).name != args.expected_child_run or args.expected_child_run in {".", ".."}:
        raise ReconciliationError("child run must be one exact directory name")
    _require_clean_checkout(series_dir, args.expected_collector_sha)
    parent_path = series_dir / "manifest.json"
    child_path = series_dir / "runs" / args.expected_child_run / "manifest.json"
    parent_before = _sha256(parent_path)
    child_before = _sha256(child_path)
    parent, child = validate_reconciliation(
        series_dir,
        source=args.source,
        expected_sha=args.expected_collector_sha,
        expected_child_run=args.expected_child_run,
        expected_artifact_sha256=args.expected_artifact_sha256,
        acknowledge_resume_age_override=args.acknowledge_resume_age_override,
    )
    print(
        f"validated exact child completion: source={args.source} "
        f"child={args.expected_child_run} status={child['status']} "
        f"artifact_sha256={args.expected_artifact_sha256}"
    )
    if not args.apply:
        print("dry run; parent remains failed")
        return 0
    with SeriesOwnershipLock(series_dir / ".series.lock"):
        _require_clean_checkout(series_dir, args.expected_collector_sha)
        if _sha256(parent_path) != parent_before or _sha256(child_path) != child_before:
            raise ReconciliationError("manifest changed during review")
        parent, child = validate_reconciliation(
            series_dir,
            source=args.source,
            expected_sha=args.expected_collector_sha,
            expected_child_run=args.expected_child_run,
            expected_artifact_sha256=args.expected_artifact_sha256,
            acknowledge_resume_age_override=args.acknowledge_resume_age_override,
        )
        checkpoint = parent["sources"][args.source]
        previous = {
            "previous_state": checkpoint.get("state"),
            "previous_error": checkpoint.get("error"),
            "previous_checkpoint_status": checkpoint.get("checkpoint_status"),
        }
        checkpoint["state"] = "complete"
        checkpoint["checkpoint_status"] = child["status"]
        checkpoint["error"] = None
        checkpoint["reconciliation"] = {
            "kind": "exact_rollback_child_completion_v1",
            "child_run": str(Path("runs") / args.expected_child_run),
            "artifact_sha256": args.expected_artifact_sha256,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            **previous,
            "resume_age_override_acknowledged": args.acknowledge_resume_age_override,
        }
        parent["updated_at"] = checkpoint["reconciliation"]["recorded_at"]
        _atomic_write_json(parent_path, parent)
    print(
        "parent source reconciled; resume the pinned series with its original configuration"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
