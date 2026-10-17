"""Read-only qualification of saved complete serial generation metrics."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shlex
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from cre_series_status import _contained_path, _read_regular_file


def serial_source_qualified(
    run_dir: Path,
    manifest: Mapping,
    source: str,
    *,
    parent: Mapping | None = None,
) -> bool:
    """Re-evaluate original evidence, never query, persist or grant parent approval.

    Serial reports predate persisted validation digests. Their exact contained
    bytes/profile/original clocks/readback are checked; an absent old digest is
    not invented. This is not a freshness certificate or settlement attestation.
    """
    from cre_checkpoint_refresh import (
        sha256_file,
        RefreshError,
        compare_validation_quality,
        manifest_database_target_sha256,
        verify_validation_readback,
    )
    from cre_notification_summary import bound_staged_transaction_breakdown
    from cre_validate import require_full_validation_report
    import cre_current_inventory_admission as admission

    snapshots = []

    def identity(value):
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    def contained(relative):
        path = _contained_path(run_dir, relative)
        if path is None or ".." in Path(relative).parts:
            raise ValueError("unsafe serial evidence path")
        current = run_dir
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("aliased serial evidence path")
        return path

    def read(relative, expected_hash=None):
        path = contained(relative)
        before = path.stat()
        if before.st_nlink != 1:
            raise ValueError("aliased serial evidence")
        raw = _read_regular_file(path, limit=16 * 1024 * 1024)
        if (
            expected_hash is not None
            and hashlib.sha256(raw).hexdigest() != expected_hash
        ):
            raise ValueError("serial evidence digest mismatch")
        snapshots.append((path, identity(before), raw))
        return raw

    def read_json(relative, expected_hash=None):
        value = json.loads(read(relative, expected_hash))
        if not isinstance(value, dict):
            raise ValueError("serial evidence is not an object")
        return value

    def timestamp(value):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("unbound serial evidence clock")
        return result.astimezone(timezone.utc)

    try:
        config = manifest["config"]
        if (
            manifest.get("schema_version") != 2
            or manifest.get("run_id") != run_dir.name
            or not re.fullmatch(r"[a-f0-9]{40}", manifest["collector_git_sha"])
            or manifest.get("collector_git_dirty") is not False
            or manifest.get("error") is not None
            or manifest.get("resource_stop") is not None
            or config.get("pipeline")
            or config["sources"] != [source]
            or config["transactions"] != ["sale", "lease"]
            or type(config.get("max_items")) is not int
            or config["max_items"] != 0
            or type(config.get("source_workers")) is not int
            or config["source_workers"] != 1
            or set(manifest["sources"]) != {source}
            or config.get("additive") is not True
            or config.get("status_activation") is not False
            or config.get("mark_missing") is not False
            or config.get("admit_baseline_hold_additively")
            or admission.is_verified_subset(config, source)
        ):
            return False
        target = manifest_database_target_sha256(manifest)
        if target is None:
            return False
        entry = admission.admission_configuration(config, source)
        expected_status = (
            "additive_scope_complete_current_inventory"
            if entry
            else "supported_scope_complete"
        )
        if manifest.get("status") != expected_status:
            return False
        if entry and (
            manifest["scope"].get("kind")
            != "collector_registry_additive_current_inventory"
            or manifest["scope"].get("whole_source_coverage") is not False
        ):
            return False
        start, finish = (
            timestamp(manifest["started_at"]),
            timestamp(manifest["finished_at"]),
        )
        if start > finish:
            return False
        if parent is not None:
            from cre_checkpoint_series import _child_succeeded, _expected_child_config

            member = parent["sources"][source]
            attempt = member["attempts"][-1]
            if (
                parent["collector_git_sha"] != manifest["collector_git_sha"]
                or parent["database_target"] != manifest["preflight"]["database_target"]
                or parent["config"].get("pipeline")
                or config != _expected_child_config(source, parent["config"])
                or not _child_succeeded(manifest, parent["config"])
                or member["checkpoint_run"] != f"runs/{run_dir.name}"
                or member["checkpoint_status"] != manifest["status"]
                or member["state"] != "complete"
                or type(attempt["rc"]) is not int
                or attempt["rc"] != 0
                or not timestamp(parent["started_at"])
                <= timestamp(attempt["started_at"])
                <= start
                <= finish
                <= timestamp(attempt["finished_at"])
                <= timestamp(parent["finished_at"])
            ):
                return False
        checkpoint = manifest["sources"][source]
        ingest, validation = checkpoint["ingest"], manifest["validation"]
        if (
            checkpoint["state"] != "ingested"
            or type(ingest["rc"]) is not int
            or ingest["rc"] != 0
            or ingest.get("mark_missing") is not False
            or ingest.get("status_activation") is not False
            or checkpoint.get("ingest_recovery")
            or ingest.get("recovered_from_exact_readback")
            or type(checkpoint["dry_run"]["rc"]) is not int
            or checkpoint["dry_run"]["rc"] != 0
            or not start
            <= timestamp(ingest["started_at"])
            <= timestamp(ingest["finished_at"])
            <= finish
            or type(validation["rc"]) is not int
            or validation["rc"] != 0
            or not all(
                validation.get(k) is True
                for k in ("query_execution_ok", "quality_no_regression", "readback_ok")
            )
            or validation.get("failed_readback_sources") != []
            or checkpoint["readback"].get("ok") is not True
        ):
            return False
        recorded = checkpoint["artifact"]
        expected_artifact = contained(recorded["path"])
        artifact_before = expected_artifact.stat()
        if artifact_before.st_nlink != 1:
            return False
        artifact_path, stats = expected_artifact, recorded
        if (
            not artifact_path.is_file()
            or type(stats["staged_unique"]) is not int
            or stats["staged_unique"] <= 0
            or type(stats["bytes"]) is not int
            or stats["bytes"] != artifact_before.st_size
            or sha256_file(artifact_path) != stats["sha256"]
            or stats.get("strict_freshness") is not True
            or stats.get("freshness_generation_id") != manifest["run_id"]
            or timestamp(stats["freshness_generation_started_at"]) != start
            or not start
            <= timestamp(stats["started_at"])
            <= timestamp(stats["finished_at"])
            <= finish
            or any(
                type(stats.get(key)) is not int or stats[key] != 0
                for key in ("rejected_by_ingest", "detail_errors")
            )
            or bound_staged_transaction_breakdown(
                stats, source, stats["staged_unique"], manifest["run_id"]
            )
            is None
        ):
            return False
        log = read(ingest["log"]).decode("utf-8")
        command = re.fullmatch(r"\[([^\]]+)\] command: (.+)", log.splitlines()[0])
        if command is None or not start <= timestamp(command[1]) <= finish:
            return False
        argv = shlex.split(command[2])
        for flag, expected in (
            ("--in", str(artifact_path)),
            ("--expected-db-target-sha256", target),
        ):
            if argv.count(flag) != 1 or argv[argv.index(flag) + 1] != expected:
                return False
        if (
            len(argv) < 2
            or argv[1] != "cre_ingest.py"
            or any(
                flag in argv
                for flag in ("--dry-run", "--mark-missing", "--activate-status")
            )
            or re.fullmatch(r"\[[^\]]+\] rc=0", log.splitlines()[-1]) is None
        ):
            return False
        for info in (checkpoint["gate"], manifest["aggregate_gate"]):
            if type(info["rc"]) is not int or info["rc"] != 0:
                return False
            gate = read_json(info["path"])
            row = gate["per_source"][source]
            expected_verdict = "ok_additive_current_inventory" if entry else "ok"
            if (
                row["verdict"] != expected_verdict
                or type(gate["summary"].get("torow_errors")) is not int
                or gate["summary"]["torow_errors"] != 0
                or type(row.get("current_active")) is not int
                or row["current_active"] != stats["staged_unique"]
                or gate["summary"].get("hold_sources") != []
                or gate.get("generated_for_artifact") != str(artifact_path)
            ):
                return False
            if entry:
                proof = row["current_inventory_proof"]
                if (
                    proof != checkpoint["gate"].get("current_inventory_proof")
                    or proof["artifact_sha256"] != recorded["sha256"]
                    or proof["collector_git_sha"] != manifest["collector_git_sha"]
                    or proof["database_target_sha256"] != target
                    or proof["generation_id"] != manifest["run_id"]
                    or timestamp(proof["generation_started_at"]) != start
                ):
                    return False
        report = read_json(validation["path"], validation.get("sha256"))
        if report.get("validation_profile") != "full" or report.get("ok") is not True:
            return False
        require_full_validation_report(report)
        if (
            not timestamp(ingest["finished_at"])
            <= timestamp(report["generated_at"])
            <= finish
        ):
            return False
        before = read_json(manifest["preflight"]["validation_path"])
        from cre_colliers_image_transition import verified_checkpoint

        if not compare_validation_quality(
            before, report, image_transition=verified_checkpoint(run_dir, manifest)
        )["ok"]:
            return False
        projected = copy.deepcopy(manifest)
        result = verify_validation_readback(
            run_dir, projected, report, now=finish, persist_manifest=False
        )
        if (
            not result["ok"]
            or projected["sources"][source]["readback"] != checkpoint["readback"]
        ):
            return False
        return identity(expected_artifact.stat()) == identity(artifact_before) and all(
            identity(path.stat()) == before
            and _read_regular_file(path, limit=16 * 1024 * 1024) == raw
            for path, before, raw in snapshots
        )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        IndexError,
        OverflowError,
        RecursionError,
        RefreshError,
    ):
        return False
