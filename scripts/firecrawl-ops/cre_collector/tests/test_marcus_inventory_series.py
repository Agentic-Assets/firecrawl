"""Source-bound Marcus inventory publication with mixed pipeline peers."""
import copy
import hashlib
import json

import cre_checkpoint_series as series
import pytest
from reporting import source_health as health
from tests.test_pipeline_configuration import PARENT_START, RUN_ID, config, qualification_context, qualified_child


def mixed_child(run):
    child = qualified_child(run)
    child.update(collector_git_sha="a" * 40, config={"pipeline": True},
                 preflight={"database_target": {"algorithm": "sha256", "value": "c" * 64}},
                 scope={"kind": "collector_registry"}, status="failed")
    binding = {"schema": "marcus-inventory-partial-v1", "scope": "marcus_inventory_partial_v1",
        "source_key": "marcus-millichap", "artifact_sha256": "b" * 64, "producer_sha": "a" * 40,
        "database_target_sha256": "c" * 64, "generation_id": RUN_ID, "generation_started_at": child["started_at"],
        "staged_sha256": "d" * 64, "attempt": 1, "source_sha256": "2" * 64, "preimage_sha256": "4" * 64,
        "held": ["held-id"], "held_count": 3, "emitted_count": 2, "detail_qualified": 0, "detail_errors": 1,
        "original_source_outcome": "detail_freshness_rejected", "whole_source_coverage": False}
    observed = {"ok": True, "outcome": "exact_commit", "artifact_sha256": "b" * 64,
        "generation_id": RUN_ID, "attempt_id": "e" * 32, "database_target_sha256": "c" * 64,
        "expected": 2, "held": binding["held"], "staged_sha256": "d" * 64, "detail_qualified": 0,
        "child_collections_verified": True, "whole_source_coverage": False, "new_missing_canonical_count": 2}
    checkpoint = {"state": "ingested", "ingest": {"rc": 0},
        "artifact": {"marcus_inventory_partial": True, "sha256": "b" * 64,
        "path": "marcus-millichap-partial/candidate-1.json", "bytes": 123, "staged_unique": 3},
        "gate": {"verdict": "ok_additive_verified_subset", "mark_missing_safe": False,
        "admission_scope": "marcus_inventory_partial_v1", "current_inventory_proof": binding},
        "subset_ingest_attempt_id": "e" * 32, "readback": {"ok": True, "kind": "marcus_inventory_partial",
        "whole_source_coverage": False, "observation": observed},
        "marcus_inventory_worker": {"state": "settled", "attempt": 1, "sha256": "b" * 64},
        "marcus_inventory_probe": {"artifact_sha256": "b" * 64, "generation_id": RUN_ID,
        "producer_sha": "a" * 40, "database_target_sha256": "c" * 64, "staged_sha256": "d" * 64,
        "preimage_sha256": "4" * 64, "held": ["held-id"]},
        "attempts": [{"number": 1, "rc": 0, "finished_at": child["finished_at"],
          "error": "marcus-millichap/sale freshness metric detailErrors must be zero",
          "rejected_artifact": "rejected/marcus-millichap-attempt-1.json", "rejected_artifact_sha256": "2" * 64}]}
    child["sources"]["marcus-millichap"] = checkpoint
    unit = child["pipeline"]["units"][0]
    unit["sources"].append("marcus-millichap")
    result = copy.deepcopy(child["pipeline"]["source_results"]["cbre"])
    result.update(artifact_sha256="b" * 64, qualification="qualified_partial_subset", partial=True,
                  whole_source_coverage=False, emitted_count=2, held_count=3)
    child["pipeline"]["source_results"]["marcus-millichap"] = result
    path = run / unit["validation_path"]
    report = json.loads(path.read_bytes())
    report["marcus_inventory_readback"] = copy.deepcopy(observed)
    raw = json.dumps(report).encode()
    path.write_bytes(raw)
    unit["validation_sha256"] = hashlib.sha256(raw).hexdigest()
    for entry in child["pipeline"]["source_results"].values():
        entry["validation_sha256"] = unit["validation_sha256"]
    return child


def test_mixed_complete_partial_and_failed_source(tmp_path):
    run = tmp_path / RUN_ID
    child = mixed_child(run)
    child["sources"]["savills"] = {"state": "collect_failed"}
    child["pipeline"]["source_results"]["savills"] = {"state": "failed"}
    assert series.pipeline_source_qualified(child, "marcus-millichap", **qualification_context(run))
    assert series.pipeline_source_qualified(child, "cbre", **qualification_context(run))
    assert not series.pipeline_source_qualified(child, "savills", **qualification_context(run))


@pytest.mark.parametrize("field", ["source", "target", "code", "outcome", "original", "attempt", "worker", "readback", "detail", "complete"])
def test_invalid_partial_fences_entire_unit(tmp_path, field):
    run = tmp_path / RUN_ID
    child = mixed_child(run)
    checkpoint = child["sources"]["marcus-millichap"]
    binding = checkpoint["gate"]["current_inventory_proof"]
    if field == "source": binding["source_key"] = "cbre"
    elif field == "target": binding["database_target_sha256"] = "0" * 64
    elif field == "code": binding["producer_sha"] = "0" * 40
    elif field == "outcome": binding["original_source_outcome"] = "success"
    elif field == "original": checkpoint["attempts"][0]["rejected_artifact_sha256"] = "0" * 64
    elif field == "attempt": checkpoint["subset_ingest_attempt_id"] = "0" * 32
    elif field == "worker": checkpoint["marcus_inventory_worker"]["state"] = "publishing"
    elif field == "readback": checkpoint["readback"]["observation"]["expected"] = 1
    elif field == "detail": binding["detail_qualified"] = 1
    else: child["pipeline"]["source_results"]["marcus-millichap"]["whole_source_coverage"] = True
    assert not series.pipeline_source_qualified(child, "marcus-millichap", **qualification_context(run))
    assert not series.pipeline_source_qualified(child, "cbre", **qualification_context(run))


def test_mixed_failure_reconciliation_never_replays_partial(tmp_path, monkeypatch):
    settings = config(pipeline=True, sources=["cbre", "marcus-millichap", "savills"])
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=settings,
        database_target={"algorithm": "sha256", "value": "c" * 64})
    manifest["started_at"] = PARENT_START
    run = tmp_path / "runs" / RUN_ID
    child = mixed_child(run)
    child.update(error="Savills source failure", config=series._expected_child_config("cbre,marcus-millichap,savills", settings))
    child["sources"]["savills"] = {"state": "collect_failed"}
    child["pipeline"]["source_results"]["savills"] = {"state": "failed", "reason": "provider failure"}
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


def test_partial_health_retains_previous_whole_source_watermark(tmp_path):
    run = tmp_path / "runs" / RUN_ID
    child = mixed_child(run)
    (run / "manifest.json").write_text(json.dumps(child))
    manifest = {"series_id": "series-1", "status": "failed", "updated_at": "2026-10-15T12:02:01Z",
        "sources": {"marcus-millichap": {"state": "qualified_partial_subset", "checkpoint_run": f"runs/{RUN_ID}",
            "attempts": [{"started_at": child["started_at"], "finished_at": child["finished_at"]}]}}}
    previous = {"sources": {"marcus-millichap": {"lastSuccessfulObservationAt": "2026-10-14T12:00:00Z"}}}
    result = health.build_receipt(tmp_path, manifest, previous=previous)["sources"]["marcus-millichap"]
    assert result["publicationStatus"] == "partial" and result["lastAttemptObservationAt"] is None
    assert result["lastSuccessfulObservationAt"] == "2026-10-14T12:00:00Z"
    child["sources"]["marcus-millichap"]["readback"]["observation"]["generation_id"] = "foreign"
    (run / "manifest.json").write_text(json.dumps(child))
    assert health.build_receipt(tmp_path, manifest)["sources"]["marcus-millichap"]["publicationStatus"] != "partial"
