"""Typed, attempt-bound outcomes for the Pyramid historical identity guard."""

from __future__ import annotations

import json

from cre_image_transition_log import (
    _unique_object,
    artifact_binding,
    read_outcome,
    valid_binding,
)
from cre_pyramid_subset_sql import historical_conflict_rows_sql

PREFIX = "CRE_PYRAMID_GUARD_RESULT "
SCHEMA = "pyramid-historical-identity-result-v1"


def _result_sql(binding, outcome):
    if not valid_binding(binding, require_collector_git_sha=True):
        raise ValueError("invalid Pyramid guard binding")
    payload = json.dumps(
        {
            "schema": SCHEMA,
            "source_key": "pyramid-brokerage",
            "outcome": outcome,
            "binding": binding,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    literal = (PREFIX + payload).replace("'", "''")
    return "\\pset format unaligned\n\\pset tuples_only on\nSELECT '" + literal + "';\n"


def build_guard_sql(binding):
    """Emit a bound refusal only after the exact guard's explicit rollback."""
    receipt = _result_sql(binding, "historical_identity_refused")
    return (
        "SELECT EXISTS ("
        + historical_conflict_rows_sql()
        + r") AS pyramid_historical_refused \gset"
        + "\n"
        + r"\if :pyramid_historical_refused"
        + "\nROLLBACK;\n"
        + receipt
        + "\\quit\n\\endif\n"
    )


def build_commit_sql(binding):
    return _result_sql(binding, "committed")


def read_result(path, binding):
    if not valid_binding(binding, require_collector_git_sha=True):
        raise ValueError("invalid Pyramid guard binding")
    evidence = read_outcome(path, PREFIX)
    result = evidence["outcome"]
    if (
        not isinstance(result, dict)
        or set(result) != {"schema", "source_key", "outcome", "binding"}
        or result["schema"] != SCHEMA
        or result["source_key"] != "pyramid-brokerage"
        or result["outcome"] not in {"committed", "historical_identity_refused"}
        or not valid_binding(result["binding"], require_collector_git_sha=True)
        or result["binding"] != binding
    ):
        raise ValueError("invalid Pyramid guard result")
    return evidence


def expected_binding(run_dir, manifest, attempt_id):
    binding = artifact_binding(
        run_dir,
        manifest,
        "pyramid-brokerage",
        attempt_id,
        include_collector_git_sha=True,
    )
    if not valid_binding(binding, require_collector_git_sha=True):
        raise ValueError("invalid Pyramid artifact binding")
    return binding


def verified_rollback(run_dir, manifest, source="pyramid-brokerage"):
    """Revalidate retained source, attempt, receipt, and zero-row evidence."""
    import hashlib
    import json
    from pathlib import Path

    import cre_checkpoint_refresh as refresh
    from cre_series_status import _read_regular_file
    from cre_validate import FULL_VALIDATION_PROFILE, VALIDATION_AUDIT_SCOPES

    try:
        if source == "pyramid-brokerage":
            state, reason, result_key, outcome = ("pyramid_historical_guard_refused", "pyramid_historical_guard_refusal", "pyramid_guard_result", "historical_identity_refused")
            result_reader, binding_reader = read_result, expected_binding
        elif source == "transwestern":
            from cre_transwestern_rollback_result import read_result as result_reader, expected_binding as binding_reader
            state, reason, result_key, outcome = ("transwestern_image_guard_refused", "transwestern_image_guard_refusal", "transwestern_guard_result", "protected_image_multiplicity_refused")
        elif source == "jll":
            from cre_jll_guard_result import read_result as result_reader, expected_binding as binding_reader
            state, reason, result_key, outcome = ("jll_image_guard_refused", "jll_image_guard_refusal", "jll_guard_result", "image_loss_refused")
        else:
            return False
        checkpoint = manifest["sources"][source]
        root = Path(run_dir)
        if checkpoint["artifact"].get(
            "path"
        ) != f"sources/{source}.json" or any(
            path.is_symlink()
            for path in (root, root / "sources", root / "logs", root / "recovery")
        ):
            return False
        ingest = checkpoint["ingest"]
        recovery = checkpoint["ingest_recovery"]
        if (
            checkpoint.get("state") != state
            or type(ingest.get("rc")) is not int
            or ingest["rc"] != 0
            or ingest.get("log") != f"logs/{source}-ingest.log"
            or ingest.get("additive") is not True
            or ingest.get("mark_missing") is not False
            or ingest.get("status_activation") is not False
            or recovery.get("reason") != reason
            or recovery.get("outcome") != "exact_rollback"
            or recovery.get("replay_safe") is not True
            or recovery.get("readback_ok") is not False
            or recovery.get("artifact_run_job_match_ok") is not False
            or type(recovery.get("rc")) is not int
            or recovery.get("rc") != 0
            or type(recovery.get("subprocess_rc")) is not int
            or recovery.get("subprocess_rc") != 0
            or recovery.get("validation_path")
            != f"recovery/{source}-validation.json"
        ):
            return False
        binding = binding_reader(run_dir, manifest, ingest["attempt_id"])
        evidence = result_reader(Path(run_dir) / ingest["log"], binding)
        if (
            evidence != ingest.get(result_key)
            or evidence["outcome"]["outcome"] != outcome
            or recovery.get("generation_id") != binding["generation_id"]
            or recovery.get("artifact_run_key_sha256")
            != hashlib.sha256(binding["artifact_run_key"].encode()).hexdigest()
        ):
            return False
        report_path = Path(run_dir) / recovery["validation_path"]
        if any(
            part.is_symlink()
            for part in (Path(run_dir), report_path.parent, report_path)
        ):
            return False
        raw = _read_regular_file(report_path, limit=32 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != recovery.get("validation_sha256"):
            return False
        report = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(report, dict):
            return False
        if (
            report.get("validation_profile") != FULL_VALIDATION_PROFILE
            or report.get("audit_scope")
            != VALIDATION_AUDIT_SCOPES[FULL_VALIDATION_PROFILE]
        ):
            return False
        expected_probe = {
            "database_target_sha256": binding["database_target_sha256"],
            "artifact_run_key_sha256": recovery["artifact_run_key_sha256"],
            "attempt_id": binding["attempt_id"],
        }
        if report.get("artifact_run_probe") != expected_probe:
            return False
        attempt_started = refresh.parse_iso8601(
            ingest["started_at"], field="ingest started"
        )
        attempt_finished = refresh.parse_iso8601(
            ingest["finished_at"], field="ingest finished"
        )
        probe_started = refresh.parse_iso8601(
            recovery["probe_started_at"], field="probe started"
        )
        probe_finished = refresh.parse_iso8601(
            recovery["probe_finished_at"], field="probe finished"
        )
        generated = refresh.parse_iso8601(
            report["generated_at"], field="report generated"
        )
        if (
            not attempt_started
            <= attempt_finished
            <= probe_started
            <= generated
            <= probe_finished
        ):
            return False
        refresh.require_full_validation_report(report)
        if report.get("ok") is not True or not refresh._artifact_run_job_probe_is_exact(
            report, expected_matching_jobs=0
        ):
            return False
        rows = report["queries"]["freshness_generations"]
        return isinstance(rows, list) and all(
            isinstance(row, dict)
            and isinstance(row.get("source_key"), str)
            and row["source_key"]
            and isinstance(row.get("generation_id"), str)
            and row["generation_id"]
            and (row["source_key"], row["generation_id"])
            != (source, binding["generation_id"])
            for row in rows
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        refresh.RefreshError,
    ):
        return False


def bind_input(args, data, original_bytes, artifact_run_key, *, activate_status):
    """Bind an isolated Pyramid attempt to its current artifact and clean code."""
    import hashlib
    from pathlib import Path

    from cre_checkpoint_refresh import git_identity
    from cre_ingest import artifact_run_identity_from_digests

    if (
        len(args.inputs) != 1
        or not args.checkpoint_ingest_attempt
        or not args.expected_db_target_sha256
        or args.mark_missing
        or activate_status
        or args.daum_verified_subset_claims
        or args.matthews_targeted_claims
        or args.checkpoint_collector_git_sha
    ):
        raise ValueError("unbound Pyramid guard outcome")
    if (
        not isinstance(data.get("sources"), list)
        or not data["sources"]
        or any(
            not isinstance(entry, dict) or entry.get("sourceKey") != "pyramid-brokerage"
            for entry in data["sources"]
        )
        or not isinstance(data.get("listings"), list)
        or any(
            not isinstance(row, dict) or row.get("sourceKey") != "pyramid-brokerage"
            for row in data["listings"]
        )
    ):
        raise ValueError("mixed Pyramid guard input")
    code_sha, dirty = git_identity()
    if dirty or code_sha != args.checkpoint_pyramid_code_sha:
        raise ValueError("Pyramid guard collector code changed")
    input_bytes = Path(args.inputs[0]).read_bytes()
    digest = hashlib.sha256(input_bytes).hexdigest()
    if (
        input_bytes != original_bytes
        or artifact_run_key != artifact_run_identity_from_digests([digest])[0]
    ):
        raise ValueError("Pyramid guard input changed")
    generation = data["runMeta"]["freshness"]
    binding = {
        "artifact_sha256": digest,
        "artifact_bytes": len(input_bytes),
        "artifact_run_key": artifact_run_key,
        "generation_id": generation["generationId"],
        "generation_started_at": generation["generationStartedAt"],
        "database_target_sha256": args.expected_db_target_sha256,
        "attempt_id": args.checkpoint_ingest_attempt,
        "collector_git_sha": code_sha,
    }
    if not valid_binding(binding, require_collector_git_sha=True):
        raise ValueError("invalid Pyramid guard binding")
    return binding
