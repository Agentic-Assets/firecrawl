"""Verify a settled ordinary ingest rollback before peers may continue."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cre_image_transition_log import _unique_object, artifact_binding, valid_binding
from cre_series_status import _read_regular_file
from cre_validate import FULL_VALIDATION_PROFILE, VALIDATION_AUDIT_SCOPES


def eligible(manifest, source):
    """Only regular atomic writers; special partial writers keep their fences."""
    import cre_checkpoint_refresh as refresh

    if source not in refresh.SOURCE_KEYS or source in {
        "pyramid-brokerage", "transwestern", "jll", "colliers-main"
    }:
        return False
    if any(check(manifest, source) for check in (
        refresh.jll_partial_candidate, refresh.franklin_partial_candidate,
        refresh.colliers_partial_candidate, refresh.lyon_partial_candidate,
        refresh.nai_post_partial_candidate,
    )):
        return False
    return source == "daum-commercial" or not refresh.verified_subset_candidate(manifest, source)


def verified_rollback(run_dir, manifest, source):
    """Recheck the actual artifact, attempt, validation report and zero-write proof."""
    import cre_checkpoint_refresh as refresh

    try:
        if not eligible(manifest, source):
            return False
        root = Path(run_dir)
        checkpoint = manifest["sources"][source]
        ingest, recovery = checkpoint["ingest"], checkpoint["ingest_recovery"]
        if (checkpoint.get("state") != "settled_ingest_failed"
                or checkpoint["artifact"].get("path") != f"sources/{source}.json"
                or any(path.is_symlink() for path in (root, root / "sources", root / "logs", root / "recovery"))
                or type(ingest.get("rc")) is not int or ingest["rc"] == 0
                or ingest.get("log") != f"logs/{source}-ingest.log"
                or ingest.get("additive") is not True or ingest.get("mark_missing") is not False
                or ingest.get("status_activation") is not False
                or recovery.get("reason") != "settled_ingest_failure"
                or recovery.get("outcome") != "exact_rollback" or recovery.get("replay_safe") is not True
                or recovery.get("readback_ok") is not False or recovery.get("artifact_run_job_match_ok") is not False
                or type(recovery.get("rc")) is not int or recovery["rc"] != 0
                or recovery.get("subprocess_rc") != ingest["rc"]
                or recovery.get("validation_path") != f"recovery/{source}-validation.json"):
            return False
        binding = artifact_binding(root, manifest, source, ingest["attempt_id"], include_collector_git_sha=True)
        if not valid_binding(binding, require_collector_git_sha=True):
            return False
        if (recovery.get("generation_id") != binding["generation_id"]
                or recovery.get("artifact_run_key_sha256") != hashlib.sha256(binding["artifact_run_key"].encode()).hexdigest()):
            return False
        path = root / recovery["validation_path"]
        if path.is_symlink():
            return False
        raw = _read_regular_file(path, limit=32 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != recovery.get("validation_sha256"):
            return False
        report = json.loads(raw, object_pairs_hook=_unique_object)
        if (not isinstance(report, dict) or report.get("validation_profile") != FULL_VALIDATION_PROFILE
                or report.get("audit_scope") != VALIDATION_AUDIT_SCOPES[FULL_VALIDATION_PROFILE]
                or report.get("artifact_run_probe") != {
                    "database_target_sha256": binding["database_target_sha256"],
                    "artifact_run_key_sha256": recovery["artifact_run_key_sha256"],
                    "attempt_id": binding["attempt_id"],
                }):
            return False
        times = [refresh.parse_iso8601(value, field="rollback evidence time") for value in (
            ingest["started_at"], ingest["finished_at"], recovery["probe_started_at"],
            report["generated_at"], recovery["probe_finished_at"])]
        if times != sorted(times):
            return False
        refresh.require_full_validation_report(report)
        if report.get("ok") is not True or not refresh._artifact_run_job_probe_is_exact(report, expected_matching_jobs=0):
            return False
        rows = report["queries"]["freshness_generations"]
        return isinstance(rows, list) and all(
            isinstance(row, dict) and isinstance(row.get("source_key"), str) and row["source_key"]
            and isinstance(row.get("generation_id"), str) and row["generation_id"]
            and (row["source_key"], row["generation_id"]) != (source, binding["generation_id"])
            for row in rows
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, refresh.RefreshError):
        return False
