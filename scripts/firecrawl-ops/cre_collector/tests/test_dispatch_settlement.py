"""Fail-closed dispatch classification using synthetic receipts and child state."""
import json
from unittest.mock import MagicMock

import cre_checkpoint_refresh as refresh
import pytest
from scheduling import dispatch_settlement as settlement


@pytest.fixture
def case(tmp_path, monkeypatch):
    owner = MagicMock(spec=refresh.SharedLock)
    owner.path = tmp_path / ".cre.lock"
    owner.recovery_required = False
    owner.authority_recovery_required = False
    monkeypatch.setattr(refresh, "canonical_shared_lock_dir", lambda: owner.path)
    config = {"pipeline": True, "sources": ["colliers", "savills"]}
    series_id = "2026-11-06T010203Z-012345abcdef"
    child_id = "2026-11-06T010203Z-fedcba987654"
    invocation = "a" * 32
    child_invocation = "b" * 32
    path = tmp_path / series_id
    path.mkdir()
    parent = {"preallocated_expected_target_sha256": "d" * 64,
              "sources": {key: {"checkpoint_run": f"runs/{child_id}", "attempts": [
        {"series_control_child_id": child_invocation}]} for key in config["sources"]}}
    (path / "manifest.json").write_text(json.dumps(parent))
    control = {"work_deadline_seconds": 30, "last_checkpoint": {
        "run_id": child_id, "child_invocation_id": child_invocation}}
    child = {"pipeline": {"commit_outcome_unknown": False}, "sources": {
        "colliers": {"state": "collecting"}, "savills": {"state": "ingested"}}}
    monkeypatch.setattr(settlement, "verify_preallocated_receipt", lambda *a, **kw: {
        "status": "deadline_interrupted", "local_settlement": "confirmed"})
    monkeypatch.setattr(settlement, "read_receipt", lambda *a: control)
    monkeypatch.setattr(series := settlement.series, "_expected_child_config", lambda *a: {})
    monkeypatch.setattr(series, "_load_child_manifest", lambda *a, **kw: (None, child))
    monkeypatch.setattr(series, "_pipeline_has_unresolved", lambda *a: False)
    def reconcile(_path, manifest, *, publish):
        assert publish is False
        manifest["sources"]["colliers"]["state"] = "interrupted"
        manifest["sources"]["savills"]["state"] = "complete"
    monkeypatch.setattr(series, "reconcile_pipeline_series", reconcile)
    options = {"owner": owner, "expected_sha": "c" * 40, "expected_target": {"value": "d" * 64},
               "expected_config": config, "expected_deadline_seconds": 30}
    return tmp_path, series_id, invocation, options, child, control


def test_verified_peer_and_unwritten_interruption_can_qualify(case):
    root, series_id, invocation, options, _, _ = case
    assert settlement.qualify(root, series_id, invocation, **options)["outcome"] == "qualified"


@pytest.mark.parametrize("damage", ["unknown_write", "cursor_unknown", "ingested", "deadline", "child"])
def test_uncertain_or_foreign_child_never_qualifies(case, damage):
    root, series_id, invocation, options, child, control = case
    if damage == "unknown_write":
        child["pipeline"]["commit_outcome_unknown"] = True
    elif damage == "cursor_unknown":
        child["pipeline"]["cursor_recovery_outcome_unknown"] = True
    elif damage == "ingested":
        child["sources"]["colliers"]["state"] = "ingested"
    elif damage == "deadline":
        options["expected_deadline_seconds"] = 31
    else:
        control["last_checkpoint"]["child_invocation_id"] = "e" * 32
    with pytest.raises(settlement.DispatchSettlementRefused):
        settlement.qualify(root, series_id, invocation, **options)


def test_prior_verified_partial_does_not_qualify_new_unreadback_window(case, monkeypatch):
    root, series_id, invocation, options, child, _ = case
    child["sources"]["colliers"] = {"state": "ingested", "artifact": {"colliers_partial": True, "sha256": "f" * 64},
        "partial_windows": [{"state": "verified", "index": 1}],
        "subset_ingest_attempt_id": "e" * 32, "ingest": {"rc": 0},
        "gate": {"current_inventory_proof": {"cursor": 1565, "next_cursor": 1670, "artifact_sha256": "f" * 64}}}
    def prior_result(_path, parent, *, publish):
        parent["sources"]["colliers"]["state"] = "qualified_partial_subset"
        parent["sources"]["savills"]["state"] = "complete"
    monkeypatch.setattr(settlement.series, "reconcile_pipeline_series", prior_result)
    with pytest.raises(settlement.DispatchSettlementRefused, match="current Colliers window"):
        settlement.qualify(root, series_id, invocation, **options)


def test_current_partial_requires_actual_bound_cursor(case, monkeypatch):
    import cre_partial_cursor
    root, series_id, invocation, options, child, _ = case
    child["run_id"] = "2026-11-06T010203Z-fedcba987654"
    attempt, artifact = "e" * 32, "f" * 64
    binding = {"cursor": 1565, "next_cursor": 1670, "artifact_sha256": artifact}
    child["sources"]["colliers"] = {"state": "ingested", "artifact": {"colliers_partial": True, "sha256": artifact},
        "partial_windows": [{"state": "verified", "index": 1}], "subset_ingest_attempt_id": attempt,
        "gate": {"current_inventory_proof": binding},
        "readback": {"ok": True, "observation": {"outcome": "exact_commit", "attempt_id": attempt,
            "artifact_sha256": artifact, "generation_id": child["run_id"], "database_target_sha256": "d" * 64}},
        "partial_cursor_settlement": {"state": "settled", "attempt_id": attempt,
            "artifact_sha256": artifact, "cursor": 1565, "next_cursor": 1670}}
    def partial(_path, parent, *, publish):
        parent["sources"]["colliers"]["state"] = "qualified_partial_subset"
        parent["sources"]["savills"]["state"] = "complete"
    monkeypatch.setattr(settlement.series, "reconcile_pipeline_series", partial)
    cursor = {"schema": "cre-partial-cursor/v1", "sourceKey": "colliers", "targetSha256": "d" * 64,
        "position": 1670, "generation": child["run_id"], "artifactSha256": artifact,
        "attemptId": attempt, "outcome": "exact_commit"}
    monkeypatch.setattr(cre_partial_cursor, "read_cursor", lambda *args: (1670, cursor))
    assert settlement.qualify(root, series_id, invocation, **options)["outcome"] == "qualified"
    cursor["attemptId"] = "0" * 32
    with pytest.raises(settlement.DispatchSettlementRefused, match="cursor no longer matches"):
        settlement.qualify(root, series_id, invocation, **options)
