"""Conservative child qualification for a locally dispatched series.

The caller must own the canonical CRE lock. A terminal process receipt alone
does not establish that an interrupted database write or cursor was settled.
"""
from __future__ import annotations

import copy
import fcntl
import json
import os
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series
from cre_series_control import read_receipt, validate_fresh_series_id, verify_preallocated_receipt
from cre_stop_guarded_tier import _identity, _safe_stat


class DispatchSettlementRefused(ValueError):
    """The exact child cannot safely support a subsequent dispatch."""


def qualify(series_root: Path, series_id: str, invocation_id: str, *, owner,
            expected_sha: str, expected_target: dict, expected_config: dict,
            expected_deadline_seconds: int) -> dict:
    """Hold the original parent lock while checking its exact terminal child."""
    validate_fresh_series_id(series_id)
    root_fd = os.open(series_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _safe_stat(root_fd, directory=True)
        directory = os.open(series_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            _safe_stat(directory, directory=True)
            descriptor = os.open(".series.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                observed = _safe_stat(descriptor, private=True)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise DispatchSettlementRefused("series parent has not settled") from exc
                result = _qualify(series_root, series_id, invocation_id, owner=owner,
                    expected_sha=expected_sha, expected_target=expected_target,
                    expected_config=expected_config, expected_deadline_seconds=expected_deadline_seconds)
                receipt = read_receipt(Path(series_root) / series_id, invocation_id)
                named = os.stat(".series.lock", dir_fd=directory, follow_symlinks=False)
                if (_identity(observed) != _identity(named)
                        or (receipt.get("stop_owner") or {}).get("series_lock_identity") != _identity(observed)):
                    raise DispatchSettlementRefused("series parent lock identity differs")
                return result
            finally:
                os.close(descriptor)
        finally:
            os.close(directory)
    finally:
        os.close(root_fd)


def _qualify(series_root: Path, series_id: str, invocation_id: str, *, owner,
             expected_sha: str, expected_target: dict, expected_config: dict,
             expected_deadline_seconds: int) -> dict:
    """Verify a settled exact child under the current canonical owner.

    This does not clear a fence, replay a write, or alter either manifest.
    """
    if not isinstance(owner, refresh.SharedLock):
        raise DispatchSettlementRefused("canonical owner is required")

    def check_owner():
        owner._require_authority()
        if (owner.recovery_required or owner.authority_recovery_required
                or owner.path.resolve() != refresh.canonical_shared_lock_dir().resolve()):
            raise DispatchSettlementRefused("canonical owner differs")

    check_owner()
    root = Path(series_root)
    terminal = verify_preallocated_receipt(root, series_id, invocation_id,
        expected_sha=expected_sha, expected_target=expected_target,
        expected_config=expected_config)
    path = root / series_id
    control = read_receipt(path, invocation_id)
    if (type(expected_deadline_seconds) is not int
            or control.get("work_deadline_seconds") != expected_deadline_seconds):
        raise DispatchSettlementRefused("series deadline differs")
    if type(control.get("finished_unix_ms")) is not int or control["finished_unix_ms"] < 0:
        raise DispatchSettlementRefused("series finish time differs")
    if terminal["local_settlement"] == "not_started":
        if control.get("last_checkpoint") is not None:
            raise DispatchSettlementRefused("no-work receipt has a child")
        check_owner()
        return {"status": terminal["status"], "outcome": "settled_no_work",
                "unfinished_sources": list(expected_config.get("sources") or []),
                "finished_unix_ms": control["finished_unix_ms"]}
    manifest_path = path / "manifest.json"
    parent_bytes = manifest_path.read_bytes()
    parent = json.loads(parent_bytes)
    if parent.get("preallocated_expected_target_sha256") != expected_target.get("value"):
        raise DispatchSettlementRefused("preallocated target evidence differs")
    if not expected_config.get("pipeline"):
        sources = expected_config.get("sources") or []
        if len(sources) != 1:
            raise DispatchSettlementRefused("serial source scope differs")
        source = sources[0]
        checkpoint = parent["sources"][source]
        ref = checkpoint.get("checkpoint_run")
        last = control.get("last_checkpoint") or {}
        attempts = checkpoint.get("attempts") or []
        if (not isinstance(ref, str) or len(Path(ref).parts) != 2
                or Path(ref).parts[0] != "runs" or Path(ref).name != last.get("run_id")
                or not attempts or attempts[-1].get("series_control_child_id") != last.get("child_invocation_id")):
            raise DispatchSettlementRefused("terminal serial child receipt differs")
        _, child = series._load_child_manifest(path / "runs", before=set(), source=source,
            git_sha=expected_sha, expected_path=path / ref / "manifest.json",
            expected_config=series._expected_child_config(source, expected_config))
        if ((child.get("preflight") or {}).get("database_target") != expected_target
                or series.evidence_reconciliation_required(child)
                or series.document_reconciliation_required(child, expected_config)):
            raise DispatchSettlementRefused("serial target or evidence differs")
        current = (child.get("sources") or {}).get(source) or {}
        if current.get("state") in {"ingesting", "ingest_recovery_required"}:
            raise DispatchSettlementRefused("serial write is unresolved")
        state = series._reconciled_state(child, expected_config)
        if state in {"complete", "qualified_partial_subset"}:
            validation = child.get("validation") or {}
            if (attempts[-1].get("rc") != 0 or checkpoint.get("state") != state
                    or any(validation.get(key) is not True for key in
                           ("query_execution_ok", "quality_no_regression", "readback_ok"))):
                raise DispatchSettlementRefused("serial success is not bound to the terminal attempt")
            unfinished = []
        elif state in {"failed_source", "interrupted"}:
            if current.get("ingest") is not None or current.get("state") == "ingested":
                raise DispatchSettlementRefused("serial incomplete work may include a write")
            unfinished = [source]
        else:
            raise DispatchSettlementRefused("serial outcome is not settled")
        check_owner()
        if manifest_path.read_bytes() != parent_bytes or read_receipt(path, invocation_id) != control:
            raise DispatchSettlementRefused("series evidence changed")
        verify_preallocated_receipt(root, series_id, invocation_id, expected_sha=expected_sha,
            expected_target=expected_target, expected_config=expected_config)
        return {"status": terminal["status"], "outcome": "qualified", "child": ref,
                "unfinished_sources": unfinished, "finished_unix_ms": control["finished_unix_ms"]}
    refs = {parent["sources"][key].get("checkpoint_run") for key in expected_config["sources"]}
    if len(refs) != 1:
        raise DispatchSettlementRefused("pipeline child owner differs")
    ref = next(iter(refs))
    if not isinstance(ref, str) or len(Path(ref).parts) != 2 or Path(ref).parts[0] != "runs":
        raise DispatchSettlementRefused("pipeline child path differs")
    last = control.get("last_checkpoint") or {}
    if (Path(ref).name != last.get("run_id")
            or any(not parent["sources"][key].get("attempts")
                   or parent["sources"][key]["attempts"][-1].get("series_control_child_id")
                      != last.get("child_invocation_id") for key in expected_config["sources"])):
        raise DispatchSettlementRefused("terminal child receipt differs")
    _, child = series._load_child_manifest(path / "runs", before=set(),
        source=",".join(expected_config["sources"]), git_sha=expected_sha,
        expected_path=path / ref / "manifest.json",
        expected_config=series._expected_child_config(",".join(expected_config["sources"]), expected_config))
    pipeline = child.get("pipeline") or {}
    if (pipeline.get("commit_outcome_unknown") is not False
            or pipeline.get("cursor_recovery_outcome_unknown") is True
            or series._pipeline_has_unresolved(child, expected_config)):
        raise DispatchSettlementRefused("pipeline has unresolved write or evidence")
    # Reuse the same generation/target/readback qualifications as the series.
    candidate = copy.deepcopy(parent)
    series.reconcile_pipeline_series(path, candidate, publish=False)
    unfinished = []
    for key in expected_config["sources"]:
        state = candidate["sources"][key]["state"]
        checkpoint = (child.get("sources") or {}).get(key) or {}
        if (checkpoint.get("state") in {"ingesting", "ingest_recovery_required"}
                or (key == "colliers" and (
                    checkpoint.get("state") == "partial_window_pending"
                    or
                    (checkpoint.get("colliers_partial_worker") or {}).get("state") in {
                        "cursor_publishing", "cursor_recovery_required", "settlement_unknown"}
                    or (checkpoint.get("partial_cursor_settlement") or {}).get("state") in {
                        "publishing", "recovery_required"}))):
            raise DispatchSettlementRefused("current source has unresolved write or cursor")
        if (key == "colliers" and checkpoint.get("state") == "ingested"
                and ((checkpoint.get("artifact") or {}).get("colliers_partial") is True
                     or checkpoint.get("partial_windows"))):
            # A previously verified partial result cannot qualify a newer
            # ingested window whose readback or cursor publication was cut off.
            binding = (checkpoint.get("gate") or {}).get("current_inventory_proof") or {}
            observed = (checkpoint.get("readback") or {}).get("observation") or {}
            cursor = checkpoint.get("partial_cursor_settlement") or {}
            if ((checkpoint.get("readback") or {}).get("ok") is not True
                    or observed.get("outcome") != "exact_commit"
                    or cursor.get("state") != "settled"
                    or cursor.get("attempt_id") != checkpoint.get("subset_ingest_attempt_id")
                    or observed.get("attempt_id") != cursor.get("attempt_id")
                    or not cursor.get("attempt_id")
                    or observed.get("generation_id") != child.get("run_id")
                    or observed.get("database_target_sha256") != expected_target.get("value")
                    or any(observed.get("artifact_sha256") != item for item in (
                        cursor.get("artifact_sha256"), binding.get("artifact_sha256"),
                        (checkpoint.get("artifact") or {}).get("sha256")))
                    or not observed.get("artifact_sha256")
                    or cursor.get("cursor") != binding.get("cursor")
                    or cursor.get("next_cursor") != binding.get("next_cursor")):
                raise DispatchSettlementRefused("current Colliers window is not exactly settled")
            from cre_partial_cursor import read_cursor
            position, cursor_record = read_cursor(owner.path, "colliers", expected_target["value"])
            expected_cursor = {"schema": "cre-partial-cursor/v1", "sourceKey": "colliers",
                "targetSha256": expected_target["value"], "position": binding["next_cursor"],
                "generation": child["run_id"], "artifactSha256": binding["artifact_sha256"],
                "attemptId": cursor["attempt_id"], "outcome": "exact_commit"}
            if position != binding["next_cursor"] or cursor_record != expected_cursor:
                raise DispatchSettlementRefused("Colliers cursor no longer matches settled window")
        if state == "qualified_partial_subset" and key == "colliers" and terminal["status"] == "deadline_interrupted":
            from cre_colliers_partial_checkpoint import partial_window_terminal_stop
            if partial_window_terminal_stop(checkpoint) is None:
                unfinished.append(key)
            continue
        if state in {"complete", "qualified_partial_subset", "failed_source"}:
            continue
        if (state != "interrupted" or checkpoint.get("ingest") is not None
                or checkpoint.get("state") in {"ingested", "ingesting", "ingest_recovery_required"}
                or checkpoint.get("partial_cursor_settlement") is not None
                or (checkpoint.get("colliers_partial_worker") or {}).get("state") in {
                    "cursor_publishing", "cursor_recovery_required", "settlement_unknown"}):
            raise DispatchSettlementRefused("interrupted source may have unsettled work")
        unfinished.append(key)
    check_owner()
    if (manifest_path.read_bytes() != parent_bytes
            or read_receipt(path, invocation_id) != control):
        raise DispatchSettlementRefused("series evidence changed")
    verify_preallocated_receipt(root, series_id, invocation_id,
        expected_sha=expected_sha, expected_target=expected_target,
        expected_config=expected_config)
    return {"status": terminal["status"], "outcome": "qualified", "child": ref,
            "unfinished_sources": unfinished, "finished_unix_ms": control["finished_unix_ms"]}
