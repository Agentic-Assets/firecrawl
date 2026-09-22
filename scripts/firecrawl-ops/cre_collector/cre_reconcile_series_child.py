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
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
) -> tuple[dict[str, Any], dict[str, Any]]:
    parent = _load_object(series_dir / "manifest.json")
    if (
        parent.get("schema_version") != 1
        or parent.get("collector_git_sha") != expected_sha
    ):
        raise ReconciliationError("parent schema or collector SHA differs")
    if parent.get("status") != "failed":
        raise ReconciliationError("parent must be failed")
    if source not in (parent.get("config") or {}).get("sources", []):
        raise ReconciliationError("source is not in the exact series scope")
    checkpoint = (parent.get("sources") or {}).get(source)
    if not isinstance(checkpoint, dict) or checkpoint.get("state") != "failed_global":
        raise ReconciliationError("parent source is not failed_global")
    child_relative = Path("runs") / expected_child_run
    if checkpoint.get("checkpoint_run") != str(child_relative):
        raise ReconciliationError("parent is not bound to the expected child")
    attempts = checkpoint.get("attempts")
    if (
        not isinstance(attempts, list)
        or not attempts
        or attempts[-1].get("rc") in (None, 0)
    ):
        raise ReconciliationError("parent lacks a recorded failed child attempt")

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
    validation = child.get("validation") or {}
    if validation.get("rc") != 0 or validation.get("readback_ok") is not True:
        raise ReconciliationError("child final validation is incomplete")
    if (child.get("config") or {}).get("sources") != [source]:
        raise ReconciliationError("child source scope differs")
    child_checkpoint = (child.get("sources") or {}).get(source)
    if (
        not isinstance(child_checkpoint, dict)
        or child_checkpoint.get("state") != "ingested"
    ):
        raise ReconciliationError("child source is not ingested")
    recovery = child_checkpoint.get("ingest_recovery") or {}
    if (
        recovery.get("outcome") != "exact_rollback"
        or recovery.get("replay_safe") is not True
    ):
        raise ReconciliationError("child lacks exact rollback evidence")
    ingest = child_checkpoint.get("ingest") or {}
    if ingest.get("rc") != 0 or ingest.get("finished_at") is None:
        raise ReconciliationError("child lacks a completed live ingest")
    readback = child_checkpoint.get("readback") or {}
    if (
        readback.get("ok") is not True
        or readback.get("generation_id") != expected_child_run
    ):
        raise ReconciliationError("child lacks exact generation readback")
    artifact = child_checkpoint.get("artifact") or {}
    if readback.get("expected_staged_unique") != artifact.get("staged_unique"):
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series-dir", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--expected-collector-sha", required=True)
    parser.add_argument("--expected-child-run", required=True)
    parser.add_argument("--expected-artifact-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    series_dir = args.series_dir.resolve()
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
    )
    print(
        f"validated exact child completion: source={args.source} "
        f"child={args.expected_child_run} status={child['status']} "
        f"artifact_sha256={args.expected_artifact_sha256}"
    )
    if not args.apply:
        print("dry run; parent remains failed")
        return 0
    if _sha256(parent_path) != parent_before or _sha256(child_path) != child_before:
        raise ReconciliationError("manifest changed during review")
    checkpoint = parent["sources"][args.source]
    checkpoint["state"] = "complete"
    checkpoint["checkpoint_status"] = child["status"]
    checkpoint["error"] = None
    checkpoint["reconciliation"] = {
        "kind": "exact_rollback_child_completion_v1",
        "child_run": str(Path("runs") / args.expected_child_run),
        "artifact_sha256": args.expected_artifact_sha256,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    parent["updated_at"] = checkpoint["reconciliation"]["recorded_at"]
    _atomic_write_json(parent_path, parent)
    print(
        "parent source reconciled; resume the pinned series with its original configuration"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
