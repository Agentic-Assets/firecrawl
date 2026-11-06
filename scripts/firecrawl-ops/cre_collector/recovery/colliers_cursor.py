"""Qualifications for readback-only Colliers cursor recovery.

These checks do not write the cursor or query the database. A caller must hold
and continuously verify the real owner before using their result.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series
from cre_partial_cursor import read_cursor
from cre_series_control import read_receipt
from cre_stop_guarded_tier import _read_at, _safe_stat


def _digest(path: Path, limit: int) -> tuple[bytes, str]:
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _safe_stat(directory, directory=True)
        raw, _ = _read_at(directory, path.name, limit=limit)
    finally:
        os.close(directory)
    return raw, hashlib.sha256(raw).hexdigest()


def _check_owner(owner):
    owner._require_authority()
    descriptor = owner._owned_directory_fd()
    os.close(descriptor)


def qualify(series_dir: Path, owner: refresh.SharedLock) -> dict:
    """Bind a stopped series and its unverified last Colliers window.

    This is only a local evidence check: callers must still perform fresh,
    target-bound exact readback and the normal full quality comparison.
    """
    if not isinstance(owner, refresh.SharedLock):
        raise TypeError("Colliers cursor recovery requires its canonical owner")
    _check_owner(owner)
    if owner.path.resolve() != refresh.canonical_shared_lock_dir().resolve():
        raise ValueError("Colliers cursor recovery owner differs")
    series_dir = Path(series_dir)
    if (series_dir.is_symlink() or series_dir.parent.name != "checkpoint-series"
            or series_dir.parent.parent.name != "out"):
        raise ValueError("Colliers cursor recovery series path invalid")
    parent_raw, parent_hash = _digest(series_dir / "manifest.json", 4 * 1024 * 1024)
    parent = json.loads(parent_raw)
    control = parent["latest_invocation_control"]["invocation_id"]
    receipt = read_receipt(series_dir, control)
    if (receipt.get("local_settlement") != "confirmed"
            or receipt.get("status") not in {"failed", "resource_guard_interrupted", "deadline_interrupted"}):
        raise ValueError("Colliers series process settlement is unconfirmed")
    last = receipt["last_checkpoint"]
    run_id = last["run_id"]
    if not isinstance(run_id, str) or Path(run_id).name != run_id:
        raise ValueError("Colliers child identity invalid")
    run_dir = series_dir / "runs" / run_id
    if run_dir.is_symlink() or (series_dir / "runs").is_symlink():
        raise ValueError("Colliers child path is not owned")
    child_raw, child_hash = _digest(run_dir / "manifest.json", 4 * 1024 * 1024)
    child = json.loads(child_raw)
    source = parent["sources"]["colliers"]
    attempts = source["attempts"]
    if (source.get("checkpoint_run") != f"runs/{run_id}" or not attempts
            or attempts[-1].get("series_control_child_id") != last.get("child_invocation_id")
            or child.get("run_id") != run_id or child.get("collector_git_sha") != parent.get("collector_git_sha")
            or child.get("collector_git_dirty") is not False
            or not series._exact_json_equal(child.get("config"), series._expected_child_config(
                ",".join(parent["config"]["sources"]), parent["config"]))
            or child.get("preflight", {}).get("database_target") != parent.get("database_target")
            or child.get("preflight", {}).get("shared_lock") != str(owner.path)
            or child.get("pipeline", {}).get("commit_outcome_unknown") is not False):
        raise ValueError("Colliers child binding differs")
    checkpoint = child["sources"]["colliers"]
    ingest = checkpoint["ingest"]
    if (checkpoint.get("state") != "ingested"
            or checkpoint.get("partial_cursor_settlement") is not None
            or type(ingest.get("rc")) is not int or ingest["rc"] != 0
            or not ingest.get("finished_at") or ingest.get("additive") is not True
            or ingest.get("mark_missing") is not False or ingest.get("status_activation") is not False
            or checkpoint.get("subset_ingest_attempt_id") != ingest.get("attempt_id")):
        raise ValueError("Colliers ingest is not a settled unverified write")
    binding = checkpoint["gate"]["current_inventory_proof"]
    worker = checkpoint["colliers_partial_worker"]
    if (binding.get("source_key") != "colliers" or binding.get("whole_source_coverage") is not False
            or checkpoint["gate"].get("mark_missing_safe") is not False
            or binding.get("generation_id") != run_id or binding.get("producer_sha") != child["collector_git_sha"]
            or binding.get("database_target_sha256") != parent["database_target"]["value"]
            or binding.get("artifact_sha256") != checkpoint["artifact"]["sha256"]
            or worker.get("state") != "settled" or type(worker.get("rc")) is not int or worker["rc"] != 0
            or worker.get("cursor") != binding.get("cursor")
            or type(binding.get("next_cursor")) is not int or binding["next_cursor"] < 1
            or binding["next_cursor"] == binding.get("cursor")):
        raise ValueError("Colliers partial source binding differs")
    windows = checkpoint["partial_windows"]
    if (not isinstance(windows, list) or not windows
            or worker.get("window_index") != len(windows) + 1
            or windows[-1].get("state") != "verified"
            or windows[-1].get("next_cursor") != binding.get("cursor")):
        raise ValueError("Colliers prior window chain differs")
    if not series.pipeline_source_qualified(child, "colliers", run_dir=run_dir,
            parent_started_at=parent["started_at"], parent_finished_at=parent["finished_at"],
            parent_attempts=attempts):
        raise ValueError("Colliers previous windows do not requalify")
    target = parent["database_target"]["value"]
    old = windows[-1]
    expected_cursor = {"schema": "cre-partial-cursor/v1", "sourceKey": "colliers",
        "targetSha256": target, "position": old["next_cursor"], "generation": run_id,
        "artifactSha256": old["artifact_sha256"], "attemptId": old["attempt_id"],
        "outcome": old["outcome"]}
    position, cursor = read_cursor(owner.path, "colliers", target)
    if cursor != expected_cursor or position != binding["cursor"]:
        raise ValueError("Colliers cursor no longer matches prior window")
    units = [u for u in child["pipeline"]["units"] if u.get("sources") == ["colliers"]
             and u.get("state") == "failed" and u.get("before_sha256")]
    if len(units) != 1:
        raise ValueError("Colliers incomplete validation unit is ambiguous")
    unit = units[0]
    previous_result = windows[-1]["result"]
    if (unit.get("before_path") != previous_result.get("validation_path")
            or unit.get("before_sha256") != previous_result.get("validation_sha256")):
        raise ValueError("Colliers before report is not the prior verified window")
    before = Path(unit["before_path"])
    if before.is_absolute() or ".." in before.parts:
        raise ValueError("Colliers before report path invalid")
    before_raw, before_hash = _digest(run_dir / before, 32 * 1024 * 1024)
    if before_hash != unit["before_sha256"]:
        raise ValueError("Colliers before report changed")
    _check_owner(owner)
    if (_digest(series_dir / "manifest.json", 4 * 1024 * 1024)[0] != parent_raw
            or _digest(run_dir / "manifest.json", 4 * 1024 * 1024)[0] != child_raw
            or _digest(run_dir / before, 32 * 1024 * 1024)[0] != before_raw
            or read_receipt(series_dir, control) != receipt
            or read_cursor(owner.path, "colliers", target) != (position, cursor)):
        raise ValueError("Colliers recovery evidence changed")
    return {"series_dir": str(series_dir), "run_dir": str(run_dir), "parent_sha256": parent_hash,
        "child_sha256": child_hash, "before_sha256": before_hash, "unit_id": unit["unit_id"],
        "target": target, "generation": run_id, "producer_sha": child["collector_git_sha"],
        "attempt_id": ingest["attempt_id"], "artifact_sha256": binding["artifact_sha256"],
        "cursor": binding["cursor"], "next_cursor": binding["next_cursor"],
        "before_path": str(before), "prior_cursor": cursor}


def recover(series_dir: Path, owner: refresh.SharedLock, *, expected_target: str, env_file: str | None,
            before_publish, after_publish):
    """Revalidate and publish one cursor without replaying its database write.

    The caller owns the canonical foreground lock and decides source-local
    versus global failure handling. This function never clears a recovery fence.
    """
    from cre_partial_cursor import advance_cursor

    initial = qualify(series_dir, owner)
    if initial["target"] != expected_target:
        raise ValueError("Colliers recovery database target differs")
    verifier_sha, dirty = refresh.git_identity()
    if (dirty is not False or not re.fullmatch(r"[a-f0-9]{40}", verifier_sha or "")
            or series.local_git_output(["merge-base", initial["producer_sha"], verifier_sha]).strip()
               != initial["producer_sha"]):
        raise ValueError("Colliers recovery verifier is not a clean reviewed descendant")
    run_dir = Path(initial["run_dir"])
    child_raw, digest = _digest(run_dir / "manifest.json", 4 * 1024 * 1024)
    if digest != initial["child_sha256"]:
        raise ValueError("Colliers recovery child changed")
    child = json.loads(child_raw)
    pipeline_dir = run_dir / "pipeline"
    directory = os.open(pipeline_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _safe_stat(directory, directory=True)
        name = "colliers-cursor-recovery-" + secrets.token_hex(16)
        os.mkdir(name, mode=0o700, dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)
    evidence_dir = pipeline_dir / name
    _check_owner(owner)
    # The ordinary full validation implementation runs only read-only queries;
    # its result is written to new private evidence, not the old manifest.
    try:
        validation = refresh.run_final_validation(run_dir, child, env_file, sources=["colliers"],
            evidence_dir=evidence_dir, before_path=run_dir / initial["before_path"],
            historical_colliers_producer_sha=initial["producer_sha"], persist_manifest=False)
    except refresh.GlobalStageError as exc:
        completed = child.get("validation") or {}
        if (not refresh._cleanup_unknown_in_chain(exc) and type(completed.get("rc")) is int
                and completed["rc"] == 0
                and completed.get("query_execution_ok") is True
                and (completed.get("quality_no_regression") is False or completed.get("readback_ok") is False)):
            raise ValueError("Colliers recovery readback or quality refused") from exc
        raise
    proof = child["sources"]["colliers"]["readback"]["observation"]
    validation_path = evidence_dir / "validation.json"
    validation_raw, validation_sha = _digest(validation_path, 32 * 1024 * 1024)
    if (validation.get("query_execution_ok") is not True or validation.get("quality_no_regression") is not True
            or validation.get("readback_ok") is not True or proof.get("ok") is not True
            or proof.get("outcome") != "exact_commit"):
        raise ValueError("Colliers recovery validation did not qualify")
    _check_owner(owner)
    if qualify(series_dir, owner) != initial or refresh.git_identity() != (verifier_sha, False):
        raise ValueError("Colliers recovery inputs changed after validation")
    report = {"schema": "colliers-cursor-recovery/v1", **initial, "verifier_sha": verifier_sha,
        "validation_sha256": validation_sha, "validation_path": str(validation_path.relative_to(run_dir)),
        "readback": proof, "whole_source_coverage": False}
    payload = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode()
    directory = os.open(evidence_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _safe_stat(directory, directory=True, private=True)
        descriptor = os.open("receipt.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)
    _check_owner(owner)
    if (qualify(series_dir, owner) != initial or refresh.git_identity() != (verifier_sha, False)
            or _digest(validation_path, 32 * 1024 * 1024)[0] != validation_raw):
        raise ValueError("Colliers recovery evidence changed before cursor publication")
    before_publish()
    _check_owner(owner)
    desired = advance_cursor(owner.path, "colliers", expected_target, initial["cursor"], initial["next_cursor"],
        generation=initial["generation"], artifact_sha256=initial["artifact_sha256"],
        attempt_id=initial["attempt_id"], outcome="exact_commit", proof=proof,
        owner_check=lambda: _check_owner(owner))
    _check_owner(owner)
    if read_cursor(owner.path, "colliers", expected_target) != (initial["next_cursor"], desired):
        raise ValueError("Colliers recovery cursor readback differs")
    after_publish()
    _check_owner(owner)
    return report


def recover_prior_generation(owner: refresh.SharedLock, *, expected_target: str, env_file: str | None,
                             before_publish, after_publish):
    """Discover only a pending window in the cursor's exact source generation.

    Absence means no action. This deliberately does not discover an interrupted
    first window whose cursor still names a different generation.
    """
    _check_owner(owner)
    position, cursor = read_cursor(owner.path, "colliers", expected_target)
    if cursor is None:
        return None
    run_id = cursor["generation"]
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{6}Z)-[a-f0-9]{12}", run_id)
    if match is None:
        return None
    base = Path(refresh.__file__).resolve().parent / "out" / "checkpoint-series"
    series_dir = base / match.group(1)
    run_dir = series_dir / "runs" / run_id
    if not (series_dir / "manifest.json").exists() or not (run_dir / "manifest.json").exists():
        return None
    parent = json.loads(_digest(series_dir / "manifest.json", 4 * 1024 * 1024)[0])
    child = json.loads(_digest(run_dir / "manifest.json", 4 * 1024 * 1024)[0])
    source = (child.get("sources") or {}).get("colliers") or {}
    worker = source.get("colliers_partial_worker") or {}
    binding = (source.get("gate") or {}).get("current_inventory_proof") or {}
    windows = source.get("partial_windows") or []
    if (not isinstance(windows, list) or not windows
            or worker.get("window_index") != len(windows) + 1):
        return None
    if source.get("state") in {"ingesting", "ingest_recovery_required"} or (
            (child.get("pipeline") or {}).get("commit_outcome_unknown") is not False):
        raise refresh.GlobalStageError("Colliers prior write settlement remains unknown")
    if source.get("state") != "ingested":
        return None
    desired = {"schema": "cre-partial-cursor/v1", "sourceKey": "colliers", "targetSha256": expected_target,
        "position": binding.get("next_cursor"), "generation": run_id,
        "artifactSha256": binding.get("artifact_sha256"), "attemptId": source.get("subset_ingest_attempt_id"),
        "outcome": "exact_commit"}
    if cursor == desired:
        return None  # An earlier owner already published this exact result.
    if binding.get("cursor") != position:
        return None  # This generation is not the current cursor's successor.
    if parent.get("database_target") != {"algorithm": "sha256", "value": expected_target}:
        raise ValueError("Colliers prior series target differs")
    return recover(series_dir, owner, expected_target=expected_target, env_file=env_file,
                   before_publish=before_publish, after_publish=after_publish)
