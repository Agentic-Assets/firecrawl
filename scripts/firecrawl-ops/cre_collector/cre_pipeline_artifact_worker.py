#!/usr/bin/env python3
"""Pure local artifact inspection for an already authenticated foreground owner.

This process has no lock, database, network or manifest-writing entrypoint.
Its request is scoped data, never transferable execution authority.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import cre_checkpoint_refresh as refresh


def file_identity(path: Path) -> list[int]:
    value = path.stat()
    return [
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    ]


def compute(action: str, arguments: dict, response: Path) -> dict:
    path = Path(arguments["path"])
    if path.is_symlink() or not path.is_file():
        raise refresh.ArtifactValidationError(
            "artifact must be a regular immutable file"
        )
    before = file_identity(path)
    expected_identity = arguments.get("file_identity")
    if expected_identity is not None and before != expected_identity:
        raise refresh.GlobalStageError("published candidate identity changed")
    if action == "hash":
        result = {"sha256": refresh.sha256_file(path), "bytes": before[2]}
    elif action == "validate":
        kwargs = dict(arguments["kwargs"])
        if kwargs.get("now") is not None:
            kwargs["now"] = datetime.fromisoformat(kwargs["now"])
        stats = refresh.validate_source_artifact(
            path,
            arguments["source"],
            arguments["attempt_started_at"],
            **kwargs,
        )
        result = {**stats, "bytes": before[2]}
    elif action == "admit":
        report = arguments["report"]
        refresh._admit_current_inventory(
            Path(arguments["run_dir"]),
            arguments["manifest"],
            report,
            path,
            arguments["phase"],
            source=arguments["source"],
        )
        evidence = response.with_suffix(".admission.json")
        refresh.atomic_write_json(evidence, report)
        result = {
            "report_path": str(evidence),
            "sha256": refresh.sha256_file(evidence),
            "scope": arguments["manifest"]["scope"],
        }
    else:
        raise ValueError("unknown artifact computation")
    if file_identity(path) != before:
        raise refresh.GlobalStageError("artifact changed during inspection")
    if (
        arguments.get("sha256") not in (None, result.get("sha256"))
        and action != "admit"
    ):
        raise refresh.ArtifactValidationError("artifact hash changed")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    args = parser.parse_args()
    if args.request.is_symlink() or args.request.stat().st_size > 8 * 1024**2:
        raise ValueError("invalid compute request")
    request = json.loads(args.request.read_text(encoding="utf-8"))
    result = {"request_id": request["request_id"], "ok": False}
    try:
        result["value"] = compute(
            request["action"], request["arguments"], args.response
        )
        result["ok"] = True
    except refresh.ArtifactValidationError as exc:
        result.update(artifact_rejected=True, error=str(exc))
    except Exception:
        result["error"] = "artifact computation failed"
    encoded = json.dumps(result)
    if len(encoded.encode()) > 64 * 1024:
        raise ValueError("compute response exceeds metadata bound")
    refresh.atomic_write_json(args.response, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
