"""Conservative child qualification for a locally dispatched series.

The caller must own the canonical CRE lock. A terminal process receipt alone
does not establish that an interrupted database write or cursor was settled.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series
from cre_series_control import read_receipt, verify_preallocated_receipt


class DispatchSettlementRefused(ValueError):
    """The exact child cannot safely support a subsequent dispatch."""


def qualify(series_root: Path, series_id: str, invocation_id: str, *, owner,
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
    if terminal["local_settlement"] == "not_started":
        if control.get("last_checkpoint") is not None:
            raise DispatchSettlementRefused("no-work receipt has a child")
        check_owner()
        return {"status": terminal["status"], "outcome": "settled_no_work",
                "unfinished_sources": list(expected_config.get("sources") or [])}
    manifest_path = path / "manifest.json"
    parent_bytes = manifest_path.read_bytes()
    parent = json.loads(parent_bytes)
    if parent.get("preallocated_expected_target_sha256") != expected_target.get("value"):
        raise DispatchSettlementRefused("preallocated target evidence differs")
    if not expected_config.get("pipeline"):
        raise DispatchSettlementRefused("only the pipeline series is supported")
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
        if state in {"complete", "qualified_partial_subset", "failed_source"}:
            continue
        checkpoint = (child.get("sources") or {}).get(key) or {}
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
            "unfinished_sources": unfinished}
