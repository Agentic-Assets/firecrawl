"""Mixed-unit qualification of Colliers' independently bound partial readback."""
import copy
import hashlib
import json

import cre_checkpoint_series as series
import pytest
from tests.test_pipeline_configuration import (
    PARENT_START,
    RUN_ID,
    config,
    qualification_context,
    qualified_child,
)


def mixed_child(run):
    child = qualified_child(run)
    child.update(collector_git_sha="a" * 40, config={"pipeline": True},
                 preflight={"database_target": {"algorithm": "sha256", "value": "c" * 64}},
                 scope={"kind": "collector_registry_verified_subset", "whole_source_coverage": False},
                 status="qualified_partial_subset")
    binding = {"schema": "colliers-partial-window-v1", "scope": "colliers_partial_window_v1",
        "source_key": "colliers", "artifact_sha256": "b" * 64, "producer_sha": "a" * 40,
        "database_target_sha256": "c" * 64, "generation_id": RUN_ID, "generation_started_at": child["started_at"],
        "staged_sha256": "d" * 64, "attempt": 1, "evidence_sha256": "1" * 64,
        "source_sha256": "2" * 64, "source_main_sha256": "3" * 64, "preimage_sha256": "4" * 64,
        "held": ["held-id"], "history_holds": {"unknown-id": "reserved native history is unknown"},
        "held_count": 2, "emitted_count": 1, "cursor": 1, "next_cursor": 4,
        "original_source_outcome": "source_error", "whole_source_coverage": False}
    observed = {"ok": True, "outcome": "exact_commit", "artifact_sha256": "b" * 64,
        "generation_id": RUN_ID, "attempt_id": "e" * 32, "database_target_sha256": "c" * 64,
        "expected": 1, "held": binding["held"], "history_holds": binding["history_holds"],
        "staged_sha256": "d" * 64, "child_collections_verified": False}
    checkpoint = {"state": "ingested", "artifact": {"colliers_partial": True, "sha256": "b" * 64,
        "path": "colliers-partial/window-1-artifact.json",
        "bytes": 123, "staged_unique": 3}, "gate": {"verdict": "ok_additive_verified_subset",
        "mark_missing_safe": False, "admission_scope": "colliers_partial_window_v1", "current_inventory_proof": binding},
        "subset_ingest_attempt_id": "e" * 32, "readback": {"ok": True, "kind": "colliers_partial_window",
        "whole_source_coverage": False, "observation": observed},
        "partial_cursor_settlement": {"state": "settled", "attempt_id": "e" * 32,
         "cursor": 1, "next_cursor": 4, "artifact_sha256": "b" * 64}}
    child["sources"]["colliers"] = checkpoint
    unit = child["pipeline"]["units"][0]
    unit["sources"].append("colliers")
    result = copy.deepcopy(child["pipeline"]["source_results"]["cbre"])
    result.update(artifact_sha256="b" * 64, qualification="qualified_partial_subset", partial=True,
                  whole_source_coverage=False, emitted_count=1, held_count=2)
    child["pipeline"]["source_results"]["colliers"] = result
    path = run / unit["validation_path"]
    report = json.loads(path.read_bytes())
    report["colliers_partial_readback"] = copy.deepcopy(observed)
    raw = json.dumps(report).encode()
    path.write_bytes(raw)
    unit["validation_sha256"] = hashlib.sha256(raw).hexdigest()
    for entry in child["pipeline"]["source_results"].values():
        entry["validation_sha256"] = unit["validation_sha256"]
    return child


def test_mixed_partial_and_complete_sources_qualify_as_distinct_scopes(tmp_path):
    run = tmp_path / RUN_ID
    child = mixed_child(run)
    assert series.pipeline_source_qualified(child, "colliers", **qualification_context(run))
    assert series.pipeline_source_qualified(child, "cbre", **qualification_context(run))


def test_partial_source_survives_unrelated_failure_without_global_scope_claim(tmp_path):
    run = tmp_path / RUN_ID
    child = mixed_child(run)
    child["status"] = "failed"
    child["scope"] = {"kind": "collector_registry", "source_keys": ["cbre", "colliers", "savills"]}
    child["sources"]["savills"] = {"state": "collect_failed"}
    child["pipeline"]["source_results"]["savills"] = {"state": "failed", "reason": "source failure"}
    assert series.pipeline_source_qualified(child, "colliers", **qualification_context(run))
    assert series.pipeline_source_qualified(child, "cbre", **qualification_context(run))
    assert not series.pipeline_source_qualified(child, "savills", **qualification_context(run))


def test_real_admission_producer_binding_does_not_depend_on_later_global_scope(tmp_path):
    from tests.test_colliers_partial_record import (
        test_source_bytes_through_node_replay_into_python_staging,
    )

    test_source_bytes_through_node_replay_into_python_staging(tmp_path)
    claims = json.loads((tmp_path / "gen" / "colliers-partial" / "claims.json").read_text())
    child, binding = claims["manifest"], claims["binding"]
    checkpoint = child["sources"]["colliers"]
    checkpoint["artifact"].update(colliers_partial=True, staged_unique=1)
    checkpoint["gate"] = claims["source_report"]["per_source"]["colliers"]
    checkpoint["subset_ingest_attempt_id"] = claims["attempt_id"]
    checkpoint["partial_cursor_settlement"] = {"state": "settled", "attempt_id": claims["attempt_id"],
        "cursor": binding["cursor"], "next_cursor": binding["next_cursor"], "artifact_sha256": binding["artifact_sha256"]}
    child["config"]["pipeline"] = True
    assert child["scope"]["kind"] == "collector_registry_verified_subset"
    assert series.colliers_partial_child(child)
    child["scope"] = {"kind": "collector_registry", "source_keys": ["colliers", "savills"]}
    child["status"] = "failed"
    assert series.colliers_partial_child(child)


@pytest.mark.parametrize("field", ["wrong_source", "wrong_target", "wrong_code", "wrong_outcome",
    "count_mismatch", "overlap", "missing_settlement", "wrong_attempt", "wrong_readback", "false_complete"])
def test_partial_claim_drift_refuses_entire_mixed_unit(tmp_path, field):
    run = tmp_path / RUN_ID
    child = mixed_child(run)
    checkpoint = child["sources"]["colliers"]
    binding = checkpoint["gate"]["current_inventory_proof"]
    if field == "wrong_source": binding["source_key"] = "cbre"
    elif field == "wrong_target": binding["database_target_sha256"] = "0" * 64
    elif field == "wrong_code": binding["producer_sha"] = "0" * 40
    elif field == "wrong_outcome": binding["original_source_outcome"] = "success"
    elif field == "count_mismatch": binding["held_count"] = 1
    elif field == "overlap": binding["history_holds"]["held-id"] = "unknown"
    elif field == "missing_settlement": checkpoint["partial_cursor_settlement"]["state"] = "publishing"
    elif field == "wrong_attempt": checkpoint["subset_ingest_attempt_id"] = "0" * 32
    elif field == "wrong_readback": checkpoint["readback"]["observation"]["expected"] = 2
    else: child["pipeline"]["source_results"]["colliers"]["whole_source_coverage"] = True
    assert not series.pipeline_source_qualified(child, "colliers", **qualification_context(run))
    assert not series.pipeline_source_qualified(child, "cbre", **qualification_context(run))


def test_mixed_reconciliation_and_rerun_preserve_partial_without_replay(tmp_path, monkeypatch):
    settings = config(pipeline=True, sources=["cbre", "colliers"])
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=settings,
        database_target={"algorithm": "sha256", "value": "c" * 64})
    manifest["started_at"] = PARENT_START
    run = tmp_path / "runs" / RUN_ID
    child = mixed_child(run)
    child["config"] = series._expected_child_config("cbre,colliers", settings)
    for source in settings["sources"]:
        manifest["sources"][source].update(state="running", checkpoint_run=f"runs/{RUN_ID}",
            attempts=[{"started_at": child["started_at"]}])
    monkeypatch.setattr(series, "_load_child_manifest", lambda *args, **kwargs: (run / "manifest.json", child))
    monkeypatch.setattr(series.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not replay"))
    series.reconcile_pipeline_series(tmp_path, manifest)
    assert manifest["sources"]["colliers"]["state"] == "qualified_partial_subset"
    assert manifest["sources"]["cbre"]["state"] == "complete"
    assert series.run_pipeline_series(tmp_path, manifest, env_file=None) == 0
    assert manifest["status"] == "qualified_partial_subset"
    assert all(len(manifest["sources"][key]["attempts"]) == 1 for key in settings["sources"])
    report = run / child["pipeline"]["units"][0]["validation_path"]
    report.write_bytes(report.read_bytes() + b" ")
    with pytest.raises(series.SeriesError, match="no longer qualifies"):
        series.run_pipeline_series(tmp_path, manifest, env_file=None)


def test_mixed_failure_reconciliation_preserves_verified_peers_without_replay(tmp_path, monkeypatch):
    settings = config(pipeline=True, sources=["cbre", "colliers", "savills"])
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=settings,
        database_target={"algorithm": "sha256", "value": "c" * 64})
    manifest["started_at"] = PARENT_START
    run = tmp_path / "runs" / RUN_ID
    child = mixed_child(run)
    child.update(status="failed", error="Savills source failure", scope={"kind": "collector_registry"},
                 config=series._expected_child_config("cbre,colliers,savills", settings))
    child["sources"]["savills"] = {"state": "collect_failed"}
    child["pipeline"]["source_results"]["savills"] = {"state": "failed", "reason": "provider failure"}
    for source in settings["sources"]:
        manifest["sources"][source].update(state="running", checkpoint_run=f"runs/{RUN_ID}",
            attempts=[{"started_at": child["started_at"]}])
    monkeypatch.setattr(series, "_load_child_manifest", lambda *args, **kwargs: (run / "manifest.json", child))
    monkeypatch.setattr(series.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not replay"))
    series.reconcile_pipeline_series(tmp_path, manifest)
    assert [manifest["sources"][key]["state"] for key in settings["sources"]] == ["complete", "qualified_partial_subset", "failed_source"]
    assert manifest["sources"]["cbre"]["error"] is None and manifest["sources"]["colliers"]["error"] is None
    assert manifest["sources"]["savills"]["error"] == "Savills source failure"
    assert series.run_pipeline_series(tmp_path, manifest, env_file=None) == 1
    assert manifest["status"] == "failed"
    assert all(len(manifest["sources"][key]["attempts"]) == 1 for key in settings["sources"])
    child["pipeline"]["commit_outcome_unknown"] = True
    with pytest.raises(series.SeriesError, match="unresolved source failure"):
        series.run_pipeline_series(tmp_path, manifest, env_file=None)
    child["pipeline"]["commit_outcome_unknown"] = False
    child["sources"]["savills"]["state"] = "ingest_recovery_required"
    with pytest.raises(series.SeriesError, match="unresolved source failure"):
        series.run_pipeline_series(tmp_path, manifest, env_file=None)
    # A stale parent label must not turn an uncertain child write into a local failure.
    manifest["sources"]["savills"]["state"] = "running"
    series.reconcile_pipeline_series(tmp_path, manifest)
    assert manifest["sources"]["savills"]["state"] == "interrupted"


@pytest.mark.parametrize("phase", ["start", "finish"])
def test_clock_hold_and_partial_resume_without_replay(tmp_path, monkeypatch, phase):
    settings = config(pipeline=True, sources=["cbre", "colliers", "savills"])
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=settings,
        database_target={"algorithm": "sha256", "value": "c" * 64})
    manifest["started_at"] = PARENT_START
    run = tmp_path / "runs" / RUN_ID
    child = mixed_child(run)
    child.update(status="failed", error="source clock hold",
        config=series._expected_child_config("cbre,colliers,savills", settings))
    attempts = [] if phase == "start" else [{"number": 1, "started_at": "2026-10-15T12:00:01Z",
        "finished_at": "2026-10-15T12:00:00Z", "rc": 80, "clock_order_refused": True}]
    child["sources"]["savills"] = {"state": "collect_infrastructure_failed", "attempts": attempts,
        "clock_hold": {"phase": phase, "generation_id": RUN_ID, "attempt_count": len(attempts)}}
    child["pipeline"]["source_results"]["savills"] = {"state": "failed", "reason": "wall clock"}
    for source in settings["sources"]:
        manifest["sources"][source].update(state="running", checkpoint_run=f"runs/{RUN_ID}",
            attempts=[{"started_at": child["started_at"]}])
    monkeypatch.setattr(series, "_load_child_manifest", lambda *args, **kwargs: (run / "manifest.json", child))
    monkeypatch.setattr(series.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not replay"))
    series.reconcile_pipeline_series(tmp_path, manifest)
    assert [manifest["sources"][key]["state"] for key in settings["sources"]] == ["complete", "qualified_partial_subset", "failed_source"]
    assert series.run_pipeline_series(tmp_path, manifest, env_file=None) == 1
    child["pipeline"]["commit_outcome_unknown"] = True
    with pytest.raises(series.SeriesError, match="unresolved source failure"):
        series.run_pipeline_series(tmp_path, manifest, env_file=None)
    child["pipeline"]["commit_outcome_unknown"] = False
    child["sources"]["savills"]["ingest"] = {"state": "unknown"}
    with pytest.raises(series.SeriesError, match="unresolved source failure"):
        series.run_pipeline_series(tmp_path, manifest, env_file=None)
    child["sources"]["savills"]["ingest"] = None
    child["sources"]["savills"]["clock_hold"]["generation_id"] = "foreign"
    with pytest.raises(series.SeriesError, match="unresolved source failure"):
        series.run_pipeline_series(tmp_path, manifest, env_file=None)


def test_source_health_keeps_partial_distinct_from_complete(tmp_path):
    from reporting import source_health as health

    child = mixed_child(tmp_path / RUN_ID)
    checkpoint = {"state": "qualified_partial_subset"}
    observed = "2026-10-15T12:00:30Z"
    assert health._qualified_subset_attempt(checkpoint, child, observed,
        computed_at="2026-10-15T12:02:01Z", source_key="colliers")
    assert health._publication_status(checkpoint, child, observed,
        computed_at="2026-10-15T12:02:01Z", source_key="colliers") == "partial"
    child["status"] = "failed"
    child["scope"] = {"kind": "collector_registry"}
    assert health._publication_status(checkpoint, child, observed,
        computed_at="2026-10-15T12:02:01Z", source_key="colliers") == "partial"
    child["sources"]["colliers"]["readback"]["observation"]["generation_id"] = "other"
    assert not health._qualified_subset_attempt(checkpoint, child, observed,
        computed_at="2026-10-15T12:02:01Z", source_key="colliers")


def test_source_health_receipt_uses_real_partial_shape_without_whole_source_clock(tmp_path):
    from reporting import source_health as health

    run = tmp_path / "runs" / RUN_ID
    child = mixed_child(run)
    # Real Colliers readback has no whole-inventory observation clocks.
    assert "earliest_inventory_observed_at" not in child["sources"]["colliers"]["readback"]
    (run / "manifest.json").write_text(json.dumps(child))
    manifest = {"series_id": "series-1", "status": "qualified_partial_subset",
        "updated_at": "2026-10-15T12:02:01Z", "sources": {"colliers": {
            "state": "qualified_partial_subset", "checkpoint_run": f"runs/{RUN_ID}",
            "attempts": [{"started_at": child["started_at"], "finished_at": child["finished_at"]}]}}}
    result = health.build_receipt(tmp_path, manifest)["sources"]["colliers"]
    assert result["attemptStatus"] == "succeeded" and result["publicationStatus"] == "partial"
    assert result["lastAttemptObservationAt"] is None
    assert result["lastSuccessfulObservationAt"] is None and result["state"] == "unknown"
    previous = {"sources": {"colliers": {"lastSuccessfulObservationAt": "2026-10-14T12:00:00Z"}}}
    retained = health.build_receipt(tmp_path, manifest, previous=previous)["sources"]["colliers"]
    assert retained["lastSuccessfulObservationAt"] == "2026-10-14T12:00:00Z"
    assert retained["lastAttemptObservationAt"] is None and retained["publicationStatus"] == "partial"
    child["sources"]["colliers"]["readback"]["observation"]["generation_id"] = "foreign"
    (run / "manifest.json").write_text(json.dumps(child))
    invalid = health.build_receipt(tmp_path, manifest)["sources"]["colliers"]
    assert invalid["publicationStatus"] != "partial" and invalid["lastSuccessfulObservationAt"] is None
    child["sources"]["colliers"]["readback"]["observation"]["generation_id"] = RUN_ID
    child["finished_at"] = "2026-10-15T12:30:00Z"
    (run / "manifest.json").write_text(json.dumps(child))
    assert health.build_receipt(tmp_path, manifest)["sources"]["colliers"]["publicationStatus"] != "partial"


@pytest.mark.parametrize("unknown_write", [False, True])
def test_deadline_reconciliation_preserves_verified_units_without_replay(
    tmp_path, monkeypatch, unknown_write
):
    from types import SimpleNamespace

    settings = config(pipeline=True, sources=["cbre", "colliers", "savills"])
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=settings,
        database_target={"algorithm": "sha256", "value": "c" * 64})
    manifest.update(started_at=PARENT_START, status="interrupted",
                    error="series work deadline expired")
    run = tmp_path / "runs" / RUN_ID
    child = mixed_child(run)
    child.update(status="interrupted", error="deadline",
                 config=series._expected_child_config("cbre,colliers,savills", settings))
    child["pipeline"].update(status="reconciliation_required", commit_outcome_unknown=unknown_write)
    child["sources"]["savills"] = {"state": "ingest_recovery_required" if unknown_write else "collecting"}
    child["pipeline"]["source_results"]["savills"] = {"state": "failed", "reason": "global pipeline stop"}
    for source in settings["sources"]:
        manifest["sources"][source].update(state="interrupted", checkpoint_run=f"runs/{RUN_ID}",
            attempts=[{"started_at": child["started_at"]}])
    monkeypatch.setattr(series, "_load_child_manifest", lambda *args, **kwargs: (run / "manifest.json", child))
    monkeypatch.setattr(series.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not replay"))
    control = SimpleNamespace(work_started=True, cleanup_unknown=False, spawn_uncertain=False,
                              group=None, child_unverified=False, checkpoint_run=run,
                              child_invocation_id="bound", checkpoint_pid=123)
    series.reconcile_settled_interruption(tmp_path, manifest, control)
    assert [manifest["sources"][key]["state"] for key in settings["sources"]] == [
        "complete", "qualified_partial_subset", "interrupted"]
    assert manifest["status"] == "interrupted"
    assert manifest["error"] == "series work deadline expired"
    assert child["pipeline"]["commit_outcome_unknown"] is unknown_write


def test_deadline_reconciliation_requires_local_settlement_and_intact_unit(tmp_path, monkeypatch):
    from types import SimpleNamespace

    settings = config(pipeline=True, sources=["cbre", "colliers"])
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=settings,
        database_target={"algorithm": "sha256", "value": "c" * 64})
    manifest["started_at"] = PARENT_START
    run = tmp_path / "runs" / RUN_ID
    child = mixed_child(run)
    child.update(status="interrupted", config=series._expected_child_config("cbre,colliers", settings))
    for source in settings["sources"]:
        manifest["sources"][source].update(state="interrupted", checkpoint_run=f"runs/{RUN_ID}",
            attempts=[{"started_at": child["started_at"]}])
    monkeypatch.setattr(series, "_load_child_manifest", lambda *args, **kwargs: (run / "manifest.json", child))
    control = SimpleNamespace(work_started=True, cleanup_unknown=True, spawn_uncertain=False,
                              group=None, child_unverified=False, checkpoint_run=run,
                              child_invocation_id="bound", checkpoint_pid=123)
    series.reconcile_settled_interruption(tmp_path, manifest, control)
    assert all(row["state"] == "interrupted" for row in manifest["sources"].values())
    control.cleanup_unknown = False
    control.checkpoint_run = tmp_path / "runs" / "foreign"
    series.reconcile_settled_interruption(tmp_path, manifest, control)
    assert all(row["state"] == "interrupted" for row in manifest["sources"].values())
    control.checkpoint_run = run
    report = run / child["pipeline"]["units"][0]["validation_path"]
    report.write_bytes(report.read_bytes() + b" ")
    series.reconcile_settled_interruption(tmp_path, manifest, control)
    assert all(row["state"] == "interrupted" for row in manifest["sources"].values())


def test_controlled_deadline_publishes_qualified_units_and_interrupted_peer(tmp_path, monkeypatch):
    import os
    import signal
    from types import SimpleNamespace

    from cre_resource_recovery import SeriesOwnershipLock
    from cre_series_control import read_receipt

    settings = config(pipeline=True, sources=["cbre", "colliers", "savills"])
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=settings,
        database_target={"algorithm": "sha256", "value": "c" * 64})
    manifest["started_at"] = PARENT_START
    run = tmp_path / "runs" / RUN_ID
    child = mixed_child(run)
    child.update(status="interrupted", config=series._expected_child_config("cbre,colliers,savills", settings))
    child["pipeline"].update(status="reconciliation_required", commit_outcome_unknown=False)
    child["sources"]["savills"] = {"state": "collecting"}
    child["pipeline"]["source_results"]["savills"] = {"state": "failed"}
    monkeypatch.setattr(series, "_load_child_manifest", lambda *args, **kwargs: (run / "manifest.json", child))
    capability = object()
    monkeypatch.setattr(series, "__name__", "__main__")
    monkeypatch.setattr(series, "_CLI_CONTROL_CAPABILITY", capability)

    def work(_path, current, **kwargs):
        for source in settings["sources"]:
            current["sources"][source].update(state="running", checkpoint_run=f"runs/{RUN_ID}",
                attempts=[{"started_at": child["started_at"]}])
        owner = kwargs["control"]
        owner.work_started = True
        owner.checkpoint_run = run
        owner.child_invocation_id = "bound"
        owner.checkpoint_pid = 123
        os.kill(os.getpid(), signal.SIGALRM)

    monkeypatch.setattr(series, "run_series", work)
    args = SimpleNamespace(work_deadline_seconds=30, env_file=None, retry_failed=False, expected_sha=None)
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        rc = series.run_controlled_series(tmp_path, manifest, lock, args, _cli_control_capability=capability)
    assert rc == 124
    assert manifest["status"] == "interrupted"
    assert [manifest["sources"][key]["state"] for key in settings["sources"]] == [
        "complete", "qualified_partial_subset", "interrupted"]
    receipt = read_receipt(tmp_path, manifest["latest_invocation_control"]["invocation_id"])
    assert receipt["status"] == "deadline_interrupted"
    assert receipt["local_settlement"] == "confirmed"


def test_multiple_window_units_requalify_each_receipt_and_fence_unknown_write(tmp_path, monkeypatch):
    import cre_colliers_partial_checkpoint as partial

    run = tmp_path / RUN_ID
    child = mixed_child(run)
    child["collector_git_dirty"] = False
    child["pipeline"]["units"][0]["sources"] = ["colliers"]
    first_checkpoint = copy.deepcopy(child["sources"]["colliers"])
    first_result = copy.deepcopy(child["pipeline"]["source_results"]["colliers"])
    first_unit = child["pipeline"]["units"][0]
    second_unit = copy.deepcopy(first_unit)
    second_unit["unit_id"] = "unit-0002"
    second_unit["validation_path"] = "pipeline/unit-0002/validation.json"
    second_unit["validation"]["path"] = second_unit["validation_path"]
    second_report = run / second_unit["validation_path"]
    second_report.parent.mkdir(parents=True)
    second_report.write_bytes((run / first_unit["validation_path"]).read_bytes())
    child["pipeline"]["units"].append(second_unit)
    private = run / "colliers-partial"
    private.mkdir(mode=0o700)
    windows = []
    for index, cursor, next_cursor in ((1, 1, 4), (2, 4, 7)):
        checkpoint = copy.deepcopy(first_checkpoint)
        checkpoint["artifact"]["path"] = f"colliers-partial/window-1{'-' + str(index) if index > 1 else ''}-artifact.json"
        checkpoint["gate"]["current_inventory_proof"].update(cursor=cursor, next_cursor=next_cursor)
        checkpoint["partial_cursor_settlement"].update(cursor=cursor, next_cursor=next_cursor)
        path = f"colliers-partial/window-1{'-' + str(index) if index > 1 else ''}-receipt.json"
        receipt = {"artifact": {"sha256": "b" * 64}, "proposedCursor": next_cursor,
                   "firstPosition": cursor, "sourceStopped": False, "rotationComplete": False,
                   "admitted": 1, "held": 2, "unattempted": 0}
        if index > 1:
            receipt["stopBeforePosition"] = 1
        raw = json.dumps(receipt).encode()
        (run / path).write_bytes(raw)
        (run / path).chmod(0o600)
        digest = hashlib.sha256(raw).hexdigest()
        checkpoint["colliers_partial_worker"] = {"attempt": 1, "window_index": index,
            "cursor": cursor, "receipt_path": path, "receipt_sha256": digest,
            "artifact_path": checkpoint["artifact"]["path"],
            "completion": {key: receipt[key] for key in ("firstPosition", "rotationComplete",
                "sourceStopped", "proposedCursor", "admitted", "held", "unattempted")}}
        result = copy.deepcopy(first_result)
        result.update(unit_id=f"unit-{index:04d}",
                      validation_path=f"pipeline/unit-{index:04d}/validation.json")
        windows.append({"index": index, "state": "verified", "unit_id": result["unit_id"],
            "first_position": 1, "cursor": cursor, "next_cursor": next_cursor,
            "artifact_sha256": "b" * 64, "attempt_id": "e" * 32, "outcome": "exact_commit",
            "receipt_path": path, "receipt_sha256": digest, "checkpoint": checkpoint, "result": result})
    child["sources"]["colliers"]["partial_windows"] = windows

    def verified(_path, projected, **_kwargs):
        worker = projected["sources"]["colliers"]["colliers_partial_worker"]
        return {"receipt": json.loads((run / worker["receipt_path"]).read_bytes())}

    monkeypatch.setattr(partial, "verify_worker_artifact", verified)
    assert series.pipeline_source_qualified(child, "colliers", **qualification_context(run))
    child["pipeline"]["commit_outcome_unknown"] = True
    assert not series.pipeline_source_qualified(child, "colliers", **qualification_context(run))
    child["pipeline"]["commit_outcome_unknown"] = False
    windows[1]["cursor"] = 5
    assert not series.pipeline_source_qualified(child, "colliers", **qualification_context(run))
    windows[1]["cursor"] = 4
    windows[0]["receipt_sha256"] = "0" * 64
    assert not series.pipeline_source_qualified(child, "colliers", **qualification_context(run))
