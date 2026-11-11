"""Qualifications for readback-only Colliers cursor recovery.

These checks do not write the cursor or query the database. A caller must hold
and continuously verify the real owner before using their result.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series
from cre_partial_cursor import read_cursor
from cre_series_control import SeriesControlError, read_receipt
from cre_stop_guarded_tier import StopRefused, _read_at, _safe_stat

MAX_PARENT_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_CHILD_MANIFEST_BYTES = 32 * 1024 * 1024
LEGACY_SETTLED_PRODUCER = "8471ede8087035f56d3ae9201f29330550d9a8d6"


class SourceEvidenceRefusal(ValueError):
    """Settled Colliers-specific evidence cannot qualify; hold only this source."""

    def __init__(self, message, *, code="source_evidence_refused"):
        super().__init__(message)
        self.code = code


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


def _legacy_journal_candidate(child):
    """Recognize only the historical normal-return shape; not a settlement proof."""
    try:
        pipeline = child["pipeline"]
        source = child["sources"]["colliers"]
        windows = source["partial_windows"]
        units = pipeline["units"]
        worker_index = (source.get("colliers_partial_worker") or {}).get("window_index")
        return (child.get("collector_git_sha") == LEGACY_SETTLED_PRODUCER
                and child.get("collector_git_dirty") is False and child.get("status") == "failed"
                and isinstance(pipeline, dict) and "commit_outcome_unknown" not in pipeline
                and pipeline.get("status") == "partial" and "active_unit" in pipeline
                and pipeline["active_unit"] is None and isinstance(units, list) and bool(units)
                and all(isinstance(unit, dict) and unit.get("state") == "verified" for unit in units)
                and isinstance(child.get("sources"), dict) and bool(child["sources"])
                and all(isinstance(item, dict) and item.get("state") in {
                    "ingested", "collect_failed", "artifact_rejected"}
                    and not item.get("ingest_recovery") for item in child["sources"].values())
                and source.get("state") == "ingested" and isinstance(windows, list) and bool(windows)
                and all(isinstance(window, dict) and window.get("state") == "verified" for window in windows)
                and type(worker_index) is int and worker_index == len(windows))
    except (KeyError, TypeError, AttributeError):
        return False


def _legacy_journal_is_settled(series_dir, parent, run_dir, child, owner, target, position, cursor):
    """Requalify an already published historical cursor without replay or writes."""
    from cre_colliers_partial_checkpoint import verify_worker_artifact
    from cre_series_control import _open_directories, _read_private_receipt

    try:
        if not _legacy_journal_candidate(child):
            return False
        parent_raw = _digest(series_dir / "manifest.json", MAX_PARENT_MANIFEST_BYTES)[0]
        child_raw = _digest(run_dir / "manifest.json", MAX_CHILD_MANIFEST_BYTES)[0]
        if (not series._exact_json_equal(json.loads(parent_raw), parent)
                or not series._exact_json_equal(json.loads(child_raw), child)):
            return False
        source = child["sources"]["colliers"]
        windows, worker = source["partial_windows"], source["colliers_partial_worker"]
        last, settlement, ingest = windows[-1], source["partial_cursor_settlement"], source["ingest"]
        parent_source = parent["sources"]["colliers"]
        attempts = parent_source["attempts"]
        invocation = parent["latest_invocation_control"]["invocation_id"]
        parent_receipt = read_receipt(series_dir, invocation)
        last_child = parent_receipt["last_checkpoint"]
        child_id, pid = last_child["child_invocation_id"], last_child["pid"]
        if (parent.get("status") != "failed" or parent.get("collector_git_sha") != LEGACY_SETTLED_PRODUCER
                or parent.get("database_target") != {"algorithm": "sha256", "value": target}
                or child.get("preflight", {}).get("database_target") != parent["database_target"]
                or child.get("preflight", {}).get("shared_lock") != str(owner.path)
                or not series._exact_json_equal(child.get("config"), series._expected_child_config(
                    ",".join(parent["config"]["sources"]), parent["config"]))
                or not attempts or parent_source.get("checkpoint_run") != f"runs/{child['run_id']}"
                or attempts[-1].get("series_control_child_id") != child_id
                or parent_receipt.get("local_settlement") != "confirmed" or parent_receipt.get("status") != "failed"
                or last_child.get("run_id") != child["run_id"]
                or not isinstance(child_id, str) or re.fullmatch(r"[a-f0-9]{32}", child_id) is None
                or type(pid) is not int or pid <= 1
                or pid == os.getpgrp() or refresh._group_alive(pid)):
            return False
        times = [series._parse_utc_timestamp(value, field="legacy journal time") for value in (
            parent["started_at"], child["started_at"], child["finished_at"], parent["finished_at"])]
        if times != sorted(times):
            return False
        root, private, root_stat, private_stat = _open_directories(run_dir)
        try:
            control_raw, _ = _read_private_receipt(private, child_id + ".json")
            control = json.loads(control_raw)
            if (type(control.get("schema_version")) is not int or control["schema_version"] != 1
                    or control.get("invocation_id") != invocation or control.get("child_invocation_id") != child_id
                    or control.get("run_id") != child["run_id"] or control.get("collector_git_sha") != LEGACY_SETTLED_PRODUCER
                    or control.get("pid") != pid or control.get("guards_stopped") is not True
                    or control.get("nested_cleanup_confirmed") is not True or control.get("terminal_evidence_saved") is not True
                    or control.get("pre_acquisition_refusal") is not False):
                return False
            root_now, private_now = run_dir.stat(follow_symlinks=False), os.stat(".series-control", dir_fd=root, follow_symlinks=False)
            identity = lambda stat: (stat.st_dev, stat.st_ino, stat.st_uid, stat.st_mode)
            if (identity(root_stat) != identity(root_now) or identity(private_stat) != identity(private_now)
                    or _read_private_receipt(private, child_id + ".json")[0] != control_raw):
                return False
        finally:
            os.close(private)
            os.close(root)
        binding = source["gate"]["current_inventory_proof"]
        expected_cursor = {"schema": "cre-partial-cursor/v1", "sourceKey": "colliers", "targetSha256": target,
                           "position": last["next_cursor"], "generation": child["run_id"],
                           "artifactSha256": last["artifact_sha256"], "attemptId": last["attempt_id"],
                           "outcome": "exact_commit"}
        units = [unit for unit in child["pipeline"]["units"] if unit.get("unit_id") == last.get("unit_id")]
        if (type(last.get("index")) is not int or last["index"] != len(windows)
                or type(position) is not int or type(last.get("cursor")) is not int
                or type(last.get("next_cursor")) is not int
                or any(type(value) is not int for value in (settlement.get("cursor"), settlement.get("next_cursor"),
                    worker.get("cursor"), binding.get("next_cursor")))
                or last.get("outcome") != "exact_commit"
                or position != last["next_cursor"] or not series._exact_json_equal(cursor, expected_cursor)
                or settlement.get("state") != "settled" or settlement.get("attempt_id") != last["attempt_id"]
                or settlement.get("artifact_sha256") != last["artifact_sha256"]
                or settlement.get("cursor") != last["cursor"] or settlement.get("next_cursor") != position
                or worker.get("state") != "settled" or type(worker.get("rc")) is not int or worker["rc"] != 0
                or worker.get("cursor") != last["cursor"] or worker.get("receipt_path") != last["receipt_path"]
                or worker.get("receipt_sha256") != last["receipt_sha256"]
                or ingest.get("attempt_id") != last["attempt_id"] or type(ingest.get("rc")) is not int or ingest["rc"] != 0
                or ingest.get("additive") is not True or ingest.get("mark_missing") is not False
                or ingest.get("status_activation") is not False or not ingest.get("finished_at")
                or source.get("subset_ingest_attempt_id") != last["attempt_id"]
                or binding.get("artifact_sha256") != last["artifact_sha256"] or binding.get("next_cursor") != position
                or not series._exact_json_equal(parent_source.get("pipeline_result"), last["result"])
                or len(units) != 1 or units[0].get("sources") != ["colliers"]):
            return False
        previous = copy.deepcopy(last["checkpoint"])
        if "partial_windows" in previous:
            return False
        projected = dict(child)
        projected["sources"] = {**child["sources"], "colliers": previous}
        projected["pipeline"] = {**child["pipeline"], "units": units,
                                 "source_results": {**child["pipeline"]["source_results"], "colliers": last["result"]}}
        verify_worker_artifact(run_dir / worker["artifact_path"], projected,
                               current_code_sha=LEGACY_SETTLED_PRODUCER, current_git_dirty=False)
        if not series.pipeline_source_qualified(projected, "colliers", run_dir=run_dir,
                parent_started_at=parent["started_at"], parent_finished_at=parent["finished_at"],
                parent_attempts=attempts):
            return False
        _check_owner(owner)
        after_position, after_cursor = read_cursor(owner.path, "colliers", target)
        if (_digest(series_dir / "manifest.json", MAX_PARENT_MANIFEST_BYTES)[0] != parent_raw
                or _digest(run_dir / "manifest.json", MAX_CHILD_MANIFEST_BYTES)[0] != child_raw
                or not series._exact_json_equal(read_receipt(series_dir, invocation), parent_receipt)
                or after_position != position or not series._exact_json_equal(after_cursor, cursor)):
            return False
        return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError, StopRefused, SeriesControlError):
        return False


def _qualify(series_dir: Path, owner: refresh.SharedLock) -> dict:
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
    parent_raw, parent_hash = _digest(series_dir / "manifest.json", MAX_PARENT_MANIFEST_BYTES)
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
    child_raw, child_hash = _digest(run_dir / "manifest.json", MAX_CHILD_MANIFEST_BYTES)
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
    # The already-published cursor identifies the immediately prior window.
    # Requalify that one original receipt and unit instead of re-reading the
    # same source spool once for every earlier window in the journal.
    from cre_colliers_partial_checkpoint import verify_worker_artifact

    last_window = windows[-1]
    previous = copy.deepcopy(last_window["checkpoint"])
    if "partial_windows" in previous:
        raise ValueError("Colliers prior checkpoint is nested")
    prior_worker = previous["colliers_partial_worker"]
    prior_binding = previous["gate"]["current_inventory_proof"]
    prior_settlement = previous["partial_cursor_settlement"]
    if (last_window.get("index") != len(windows) or prior_worker.get("window_index", 1) != len(windows)
            or last_window.get("receipt_path") != prior_worker.get("receipt_path")
            or last_window.get("receipt_sha256") != prior_worker.get("receipt_sha256")
            or last_window.get("artifact_sha256") != prior_binding.get("artifact_sha256")
            or last_window.get("attempt_id") != prior_settlement.get("attempt_id")
            or last_window.get("cursor") != prior_binding.get("cursor")
            or prior_worker.get("cursor") != last_window.get("cursor")
            or last_window.get("next_cursor") != prior_binding.get("next_cursor")
            or last_window.get("outcome") != "exact_commit"):
        raise ValueError("Colliers prior window binding differs")
    projected = dict(child)
    projected["sources"] = {**child["sources"], "colliers": previous}
    checked = verify_worker_artifact(run_dir / prior_worker["artifact_path"], projected,
        current_code_sha=child["collector_git_sha"], current_git_dirty=child["collector_git_dirty"])
    worker_receipt = checked["receipt"]
    # The cursor is a position to resume scanning, not necessarily an
    # observed card. Bind the actual first evidence position and require it
    # to lie in the forward rotated interval ending at the committed cursor.
    from cre_colliers_partial_checkpoint import _read_private_file

    data = checked["data"]
    total, start, end = data.get("total"), last_window.get("cursor"), last_window.get("next_cursor")
    first = worker_receipt.get("firstPosition")
    if (any(type(value) is not int for value in (total, start, end, first))
            or total < 1 or start < 1 or not all(1 <= value <= total for value in (end, first))
            or data.get("cursor") != start or data.get("proposedCursor") != end):
        raise ValueError("Colliers prior rotation interval differs")
    # The selector orders every position before an out-of-range cursor,
    # starting at position 1 after a source inventory shrinks.
    rotation_start = start if start <= total else 1
    distance = (end - rotation_start) % total
    # A cursor beyond a shrunken inventory starts scanning at 1. Reaching
    # cursor 1 after processing the tail is a full rotation, not zero work.
    if start > total and end == 1:
        distance = total
    if (first - rotation_start) % total >= distance:
        raise ValueError("Colliers prior rotation interval differs")
    evidence_raw = _read_private_file(run_dir / prior_worker["evidence_path"], 128 * 1024 * 1024)
    evidence = worker_receipt.get("evidence") or {}
    if (len(evidence_raw) != evidence.get("bytes")
            or hashlib.sha256(evidence_raw).hexdigest() != evidence.get("sha256")):
        raise ValueError("Colliers prior rotation evidence changed")
    lines = evidence_raw.splitlines()
    if len(lines) < 2:
        raise ValueError("Colliers prior rotation evidence is empty")
    header, first_record, last_record = (json.loads(lines[0]), json.loads(lines[1]), json.loads(lines[-1]))
    if header != {"schema": "colliers-window-evidence/v1", "sourceKey": "colliers",
                  "generation": run_id, "attempt": prior_worker["attempt"]}:
        raise ValueError("Colliers prior rotation evidence differs")

    def evidence_position(record):
        if not isinstance(record, dict):
            raise ValueError("Colliers prior rotation record invalid")
        source = record.get("source") if record.get("kind") == "detail" else (
            record.get("candidate") if record.get("kind") == "held" else None)
        if (not isinstance(source, dict) or type(source.get("start")) is not int
                or type(source.get("offset")) is not int):
            raise ValueError("Colliers prior rotation position invalid")
        return source["start"] + source["offset"]

    if evidence_position(first_record) != first:
        raise ValueError("Colliers prior first evidence position differs")
    if evidence_position(last_record) != (end - 2) % total + 1:
        raise ValueError("Colliers prior last evidence position differs")
    completion = prior_worker.get("completion")
    if (not isinstance(completion, dict) or set(completion) != {"firstPosition", "rotationComplete",
            "sourceStopped", "proposedCursor", "admitted", "held", "unattempted"}
            or completion != {key: worker_receipt[key] for key in completion}
            or worker_receipt.get("sourceStopped") is not False or worker_receipt.get("rotationComplete") is not False
            or worker_receipt.get("proposedCursor") != last_window["next_cursor"]
            or (len(windows) == 1 and last_window.get("first_position") != first)
            or (len(windows) > 1 and worker_receipt.get("stopBeforePosition") != last_window.get("first_position"))
            or worker.get("stop_before_position") != last_window.get("first_position")):
        raise ValueError("Colliers prior worker completion differs")
    prior_unit = [unit for unit in child["pipeline"]["units"] if unit.get("unit_id") == last_window.get("unit_id")]
    if len(prior_unit) != 1 or prior_unit[0].get("sources") != ["colliers"]:
        raise ValueError("Colliers prior validation unit differs")
    projected["pipeline"] = {**child["pipeline"], "units": prior_unit,
        "source_results": {**child["pipeline"]["source_results"], "colliers": last_window["result"]}}
    if not series.pipeline_source_qualified(projected, "colliers", run_dir=run_dir,
            parent_started_at=parent["started_at"], parent_finished_at=parent["finished_at"],
            parent_attempts=attempts):
        raise ValueError("Colliers previous window does not requalify")
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
    if (_digest(series_dir / "manifest.json", MAX_PARENT_MANIFEST_BYTES)[0] != parent_raw
            or _digest(run_dir / "manifest.json", MAX_CHILD_MANIFEST_BYTES)[0] != child_raw
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


def qualify(series_dir: Path, owner: refresh.SharedLock) -> dict:
    """Classify only settled source-local evidence refusal; owner loss stays global."""
    _check_owner(owner)
    try:
        return _qualify(series_dir, owner)
    except (FileNotFoundError, NotADirectoryError, PermissionError, KeyError, TypeError,
            ValueError, StopRefused, SeriesControlError, series.SeriesError) as exc:
        _check_owner(owner)
        raise SourceEvidenceRefusal("Colliers saved source evidence did not qualify") from exc


def recover(series_dir: Path, owner: refresh.SharedLock, *, expected_target: str, env_file: str | None,
            before_publish, after_publish):
    """Revalidate and publish one cursor without replaying its database write.

    The caller owns the canonical foreground lock and decides source-local
    versus global failure handling. This function never clears a recovery fence.
    """
    from cre_partial_cursor import advance_cursor

    initial = qualify(series_dir, owner)
    if initial["target"] != expected_target:
        raise refresh.GlobalStageError("Colliers recovery database target differs")
    verifier_sha, dirty = refresh.git_identity()
    if (dirty is not False or not re.fullmatch(r"[a-f0-9]{40}", verifier_sha or "")
            or series.local_git_output(["merge-base", initial["producer_sha"], verifier_sha]).strip()
               != initial["producer_sha"]):
        raise refresh.GlobalStageError("Colliers recovery verifier is not a clean reviewed descendant")
    run_dir = Path(initial["run_dir"])
    child_raw, digest = _digest(run_dir / "manifest.json", MAX_CHILD_MANIFEST_BYTES)
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
    # its result is written to new private evidence, not the old manifest. The
    # source-specific psql readback has a 60-second timeout and does not poll
    # peer collection while it waits; it does not issue source or database writes.
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
            code = ("quality_regression" if completed.get("quality_no_regression") is False
                    else "readback_mismatch")
            raise SourceEvidenceRefusal("Colliers recovery readback or quality refused", code=code) from exc
        raise
    except refresh.LockHeldError:
        raise
    except Exception as exc:
        from cre_colliers_partial_checkpoint import ColliersHistoricalEvidenceRefusal

        if not isinstance(exc, ColliersHistoricalEvidenceRefusal):
            raise
        _check_owner(owner)
        if (refresh.database_target_fingerprint(env_file) != {"algorithm": "sha256", "value": expected_target}
                or refresh.git_identity() != (verifier_sha, False)):
            raise refresh.GlobalStageError("Colliers recovery owner, verifier or target changed") from exc
        raise SourceEvidenceRefusal("Colliers historical source evidence refused", code="historical_readback_refused") from exc
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

    An absent legacy series is reported for fresh collection; it does not
    qualify any old window. Re-observation can append a distinct history entry.
    This does not discover a first window whose cursor names another generation.
    """
    _check_owner(owner)
    position, cursor = read_cursor(owner.path, "colliers", expected_target)
    if cursor is None:
        return None
    run_id = cursor["generation"]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{6}Z-[a-f0-9]{12}", run_id) is None:
        raise SourceEvidenceRefusal("Colliers cursor generation has no supported history")
    base = Path(refresh.__file__).resolve().parent / "out" / "checkpoint-series"
    # Series and child IDs are generated independently and can differ by a
    # second. Bind the exact child path in the parent, not its timestamp.
    try:
        directory = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _safe_stat(directory, directory=True)
            names = os.listdir(directory)
        finally:
            os.close(directory)
        if len(names) > 4096:
            raise SourceEvidenceRefusal("Colliers cursor history search exceeds limit")
        matches = []
        unsafe_child = False
        for name in names:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{6}Z", name) is None:
                continue
            candidate = base / name
            if candidate.is_symlink():
                # Unowned links are not history and are never followed.
                continue
            if not candidate.is_dir():
                continue
            child_path = candidate / "runs" / run_id
            child_exists = child_path.exists() or child_path.is_symlink()
            try:
                parent = json.loads(_digest(candidate / "manifest.json", MAX_PARENT_MANIFEST_BYTES)[0])
                bound = parent.get("sources", {}).get("colliers", {}).get("checkpoint_run") == f"runs/{run_id}"
            except (FileNotFoundError, NotADirectoryError, PermissionError, ValueError, TypeError,
                    AttributeError, StopRefused):
                if child_exists:
                    matches.append((candidate, None, False))
                continue
            if bound or child_exists:
                matches.append((candidate, parent, bound))
        if not matches:
            _check_owner(owner)
            return {"history_unavailable": True, "generation": run_id, "position": position}
        # An ambiguous location must not hide an independently readable
        # unsettled write in any candidate history.
        for candidate, _, _ in matches:
            candidate_run = candidate / "runs" / run_id
            if candidate_run.is_symlink() or (candidate / "runs").is_symlink():
                unsafe_child = True
                continue
            try:
                candidate_child = json.loads(_digest(candidate_run / "manifest.json", MAX_CHILD_MANIFEST_BYTES)[0])
                candidate_source = candidate_child["sources"]["colliers"]
                candidate_pipeline = candidate_child["pipeline"]
                if (candidate_source.get("state") in {"ingesting", "ingest_recovery_required"}
                        or (candidate_pipeline.get("commit_outcome_unknown") is not False
                            and not (len(matches) == 1 and matches[0][2]
                                     and _legacy_journal_candidate(candidate_child)))):
                    raise refresh.GlobalStageError("Colliers prior write settlement remains unknown")
            except (FileNotFoundError, NotADirectoryError, PermissionError, ValueError, TypeError,
                    KeyError, AttributeError, StopRefused):
                pass  # The selected history is classified below; duplicates are held.
        if unsafe_child or len(matches) != 1 or not matches[0][2]:
            raise SourceEvidenceRefusal("Colliers cursor history is ambiguous or unbound")
        series_dir, parent, _ = matches[0]
        run_dir = series_dir / "runs" / run_id
        if run_dir.is_symlink() or (series_dir / "runs").is_symlink():
            raise SourceEvidenceRefusal("Colliers cursor child path is not owned")
        child = json.loads(_digest(run_dir / "manifest.json", MAX_CHILD_MANIFEST_BYTES)[0])
        if not isinstance(child, dict) or child.get("run_id") != run_id:
            raise SourceEvidenceRefusal("Colliers cursor child identity differs")
    except (FileNotFoundError, NotADirectoryError, PermissionError, ValueError, TypeError, StopRefused) as exc:
        _check_owner(owner)
        if isinstance(exc, SourceEvidenceRefusal):
            raise
        raise SourceEvidenceRefusal("Colliers cursor history is missing or malformed") from exc
    try:
        source = (child.get("sources") or {}).get("colliers") or {}
        worker = source.get("colliers_partial_worker") or {}
        binding = (source.get("gate") or {}).get("current_inventory_proof") or {}
        windows = source.get("partial_windows") or []
        if not all(isinstance(value, dict) for value in (parent, child, source, worker, binding)):
            raise TypeError("invalid Colliers history shape")
    except (AttributeError, TypeError) as exc:
        _check_owner(owner)
        raise SourceEvidenceRefusal("Colliers cursor history is malformed") from exc
    if not isinstance(windows, list) or not isinstance(child.get("pipeline"), dict):
        raise SourceEvidenceRefusal("Colliers cursor history is malformed")
    if _legacy_journal_candidate(child):
        if not _legacy_journal_is_settled(series_dir, parent, run_dir, child, owner,
                                           expected_target, position, cursor):
            raise refresh.GlobalStageError("Colliers historical journal settlement remains unknown")
        return None  # Exact already-published legacy cursor; do not replay or publish.
    if source.get("state") in {"ingesting", "ingest_recovery_required"} or (
            child["pipeline"].get("commit_outcome_unknown") is not False):
        raise refresh.GlobalStageError("Colliers prior write settlement remains unknown")
    if not windows:
        return None  # First-window recovery is not supported by this discovery.
    index = worker.get("window_index")
    if index == len(windows):
        return None  # The last window is already journaled.
    if type(index) is not int or index != len(windows) + 1:
        raise SourceEvidenceRefusal("Colliers window history is incomplete")
    if source.get("state") != "ingested":
        return None
    desired = {"schema": "cre-partial-cursor/v1", "sourceKey": "colliers", "targetSha256": expected_target,
        "position": binding.get("next_cursor"), "generation": run_id,
        "artifactSha256": binding.get("artifact_sha256"), "attemptId": source.get("subset_ingest_attempt_id"),
        "outcome": "exact_commit"}
    if cursor == desired:
        return None  # An earlier owner already published this exact result.
    if binding.get("cursor") != position:
        raise SourceEvidenceRefusal("Colliers pending window does not match the current cursor")
    if parent.get("database_target") != {"algorithm": "sha256", "value": expected_target}:
        raise refresh.GlobalStageError("Colliers prior series target differs")
    return recover(series_dir, owner, expected_target=expected_target, env_file=env_file,
                   before_publish=before_publish, after_publish=after_publish)
