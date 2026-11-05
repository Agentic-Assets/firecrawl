"""Source-bound Lyon partial publication in mixed pipeline units."""
import copy
import hashlib
import json

import cre_checkpoint_series as series
import cre_source_health as health
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
                 scope={"kind": "collector_registry"}, status="failed")
    binding = {"schema": "lyon-search-partial-v1", "scope": "lyon_partial_search_v1",
        "source_key": "lyon-stahl", "artifact_sha256": "b" * 64, "producer_sha": "a" * 40,
        "database_target_sha256": "c" * 64, "generation_id": RUN_ID, "generation_started_at": child["started_at"],
        "staged_sha256": "d" * 64, "attempt": 1, "source_sha256": "2" * 64,
        "source_main_sha256": "3" * 64, "preimage_sha256": "4" * 64,
        "held": ["held-id"], "held_count": 3, "emitted_count": 2,
        "original_source_outcome": "source_error", "whole_source_coverage": False}
    observed = {"ok": True, "outcome": "exact_commit", "artifact_sha256": "b" * 64,
        "generation_id": RUN_ID, "attempt_id": "e" * 32, "database_target_sha256": "c" * 64,
        "expected": 2, "held": binding["held"], "staged_sha256": "d" * 64,
        "child_collections_verified": True}
    checkpoint = {"state": "ingested", "ingest": {"rc": 0},
        "artifact": {"lyon_partial": True, "sha256": "b" * 64,
        "path": "lyon-stahl-partial/candidate-1.json", "bytes": 123, "staged_unique": 3},
        "gate": {"verdict": "ok_additive_verified_subset", "mark_missing_safe": False,
        "admission_scope": "lyon_partial_search_v1", "current_inventory_proof": binding},
        "subset_ingest_attempt_id": "e" * 32, "readback": {"ok": True, "kind": "lyon_partial_search",
        "whole_source_coverage": False, "observation": observed},
        "lyon_partial_worker": {"state": "settled", "attempt": 1, "sha256": "b" * 64},
        "lyon_identity_probe": {"artifact_sha256": "b" * 64, "generation_id": RUN_ID,
        "producer_sha": "a" * 40, "database_target_sha256": "c" * 64, "staged_sha256": "d" * 64,
        "preimage_sha256": "4" * 64, "held": ["held-id"]}}
    child["sources"]["lyon-stahl"] = checkpoint
    unit = child["pipeline"]["units"][0]
    unit["sources"].append("lyon-stahl")
    result = copy.deepcopy(child["pipeline"]["source_results"]["cbre"])
    result.update(artifact_sha256="b" * 64, qualification="qualified_partial_subset", partial=True,
                  whole_source_coverage=False, emitted_count=2, held_count=3)
    child["pipeline"]["source_results"]["lyon-stahl"] = result
    path = run / unit["validation_path"]
    report = json.loads(path.read_bytes())
    report["lyon_partial_readback"] = copy.deepcopy(observed)
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
    assert series.pipeline_source_qualified(child, "lyon-stahl", **qualification_context(run))
    assert series.pipeline_source_qualified(child, "cbre", **qualification_context(run))
    assert not series.pipeline_source_qualified(child, "savills", **qualification_context(run))


def test_actual_admission_producer_binding_qualifies(monkeypatch):
    import tests.test_lyon_partial_checkpoint as producer_test

    original = producer_test.admit_search
    captured = []

    def capture(path, manifest, report, **kwargs):
        binding = original(path, manifest, report, **kwargs)
        if kwargs.get("phase") == "source":
            child = copy.deepcopy(manifest)
            child["config"]["pipeline"] = True
            checkpoint = child["sources"]["lyon-stahl"]
            checkpoint["gate"] = copy.deepcopy(report["per_source"]["lyon-stahl"])
            checkpoint["subset_ingest_attempt_id"] = "e" * 32
            captured.append(child)
        return binding

    monkeypatch.setattr(producer_test, "admit_search", capture)
    producer_test.LyonCheckpointTests("test_owned_replay_worker_produces_bound_private_candidate").test_owned_replay_worker_produces_bound_private_candidate()
    assert captured and series.lyon_partial_child(captured[0])


@pytest.mark.parametrize("field", ["source", "target", "code", "outcome", "count", "worker", "probe", "attempt", "readback", "children", "complete"])
def test_invalid_partial_fences_entire_unit(tmp_path, field):
    run = tmp_path / RUN_ID
    child = mixed_child(run)
    checkpoint = child["sources"]["lyon-stahl"]
    binding = checkpoint["gate"]["current_inventory_proof"]
    if field == "source": binding["source_key"] = "cbre"
    elif field == "target": binding["database_target_sha256"] = "0" * 64
    elif field == "code": binding["producer_sha"] = "0" * 40
    elif field == "outcome": binding["original_source_outcome"] = "success"
    elif field == "count": binding["held_count"] = 0
    elif field == "worker": checkpoint["lyon_partial_worker"]["state"] = "publishing"
    elif field == "probe": checkpoint["lyon_identity_probe"]["held"] = []
    elif field == "attempt": checkpoint["subset_ingest_attempt_id"] = "0" * 32
    elif field == "readback": checkpoint["readback"]["observation"]["expected"] = 1
    elif field == "children": checkpoint["readback"]["observation"]["child_collections_verified"] = False
    else: child["pipeline"]["source_results"]["lyon-stahl"]["whole_source_coverage"] = True
    assert not series.pipeline_source_qualified(child, "lyon-stahl", **qualification_context(run))
    assert not series.pipeline_source_qualified(child, "cbre", **qualification_context(run))


def test_mixed_failure_reconciliation_and_rerun_never_replay(tmp_path, monkeypatch):
    settings = config(pipeline=True, sources=["cbre", "lyon-stahl", "savills"])
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=settings,
        database_target={"algorithm": "sha256", "value": "c" * 64})
    manifest["started_at"] = PARENT_START
    run = tmp_path / "runs" / RUN_ID
    child = mixed_child(run)
    child.update(error="Savills source failure", config=series._expected_child_config("cbre,lyon-stahl,savills", settings))
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


def test_health_retains_prior_watermark_without_partial_clock(tmp_path):
    run = tmp_path / "runs" / RUN_ID
    child = mixed_child(run)
    (run / "manifest.json").write_text(json.dumps(child))
    manifest = {"series_id": "series-1", "status": "failed", "updated_at": "2026-10-15T12:02:01Z",
        "sources": {"lyon-stahl": {"state": "qualified_partial_subset", "checkpoint_run": f"runs/{RUN_ID}",
            "attempts": [{"started_at": child["started_at"], "finished_at": child["finished_at"]}]}}}
    previous = {"sources": {"lyon-stahl": {"lastSuccessfulObservationAt": "2026-10-14T12:00:00Z"}}}
    result = health.build_receipt(tmp_path, manifest, previous=previous)["sources"]["lyon-stahl"]
    assert result["publicationStatus"] == "partial" and result["lastAttemptObservationAt"] is None
    assert result["lastSuccessfulObservationAt"] == "2026-10-14T12:00:00Z"
    child["sources"]["lyon-stahl"]["readback"]["observation"]["generation_id"] = "foreign"
    (run / "manifest.json").write_text(json.dumps(child))
    assert health.build_receipt(tmp_path, manifest)["sources"]["lyon-stahl"]["publicationStatus"] != "partial"


def add_franklin(child, run):
    binding = {"schema": "franklin-site-partial-v1", "scope": "franklin_site_partial_v1",
        "source_key": "franklin-street", "artifact_sha256": "f" * 64,
        "source_artifact_sha256": "1" * 64, "generation_id": RUN_ID, "collector_git_sha": "a" * 40,
        "database_target_sha256": "c" * 64, "staged_sha256": "2" * 64,
        "held": ["held-franklin"], "held_count": 1, "emitted_count": 2,
        "buildout_pending": 3, "held_observations": 1,
        "identity_uncertainty": {"siteToBuildout": "unresolved"}, "whole_source_coverage": False}
    observed = {"ok": True, "invalid": [], "expected": 2, "held": binding["held"],
        "artifact_sha256": "f" * 64, "generation_id": RUN_ID, "staged_sha256": "2" * 64,
        "database_target_sha256": "c" * 64, "attempt_id": "9" * 32, "matching_jobs": 1}
    checkpoint = {"state": "ingested", "ingest": {"rc": 0},
        "artifact": {"franklin_partial": True, "sha256": "f" * 64, "path": "franklin-site.json",
                     "bytes": 123, "staged_unique": 3},
        "gate": {"verdict": "ok_additive_verified_subset", "mark_missing_safe": False,
                 "admission_scope": "franklin_site_partial_v1", "current_inventory_proof": binding},
        "franklin_original": {"sha256": "1" * 64}, "subset_ingest_attempt_id": "9" * 32,
        "readback": {"ok": True, "kind": "franklin_site_partial", "whole_source_coverage": False,
                     "observation": observed}}
    child["sources"]["franklin-street"] = checkpoint
    unit = child["pipeline"]["units"][0]
    unit["sources"].append("franklin-street")
    result = copy.deepcopy(child["pipeline"]["source_results"]["cbre"])
    result.update(artifact_sha256="f" * 64, qualification="qualified_partial_subset", partial=True,
                  whole_source_coverage=False, emitted_count=2, held_count=1,
                  buildout_pending=3, held_observations=1,
                  identity_uncertainty={"siteToBuildout": "unresolved"})
    child["pipeline"]["source_results"]["franklin-street"] = result
    path = run / unit["validation_path"]
    report = json.loads(path.read_bytes())
    report["franklin_partial_readback"] = copy.deepcopy(observed)
    raw = json.dumps(report).encode()
    path.write_bytes(raw)
    unit["validation_sha256"] = hashlib.sha256(raw).hexdigest()
    for entry in child["pipeline"]["source_results"].values():
        entry["validation_sha256"] = unit["validation_sha256"]
    return child


@pytest.mark.parametrize("ordering", [False, True])
def test_franklin_lyon_mixed_failure_no_replay_and_health(tmp_path, monkeypatch, ordering):
    sources = ["cbre", "lyon-stahl", "franklin-street", "savills"]
    if ordering:
        sources[1:3] = reversed(sources[1:3])
    settings = config(pipeline=True, sources=sources)
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=settings,
        database_target={"algorithm": "sha256", "value": "c" * 64})
    manifest["started_at"] = PARENT_START
    run = tmp_path / "runs" / RUN_ID
    child = add_franklin(mixed_child(run), run)
    child.update(error="Savills source failure", config=series._expected_child_config(",".join(sources), settings))
    child["sources"]["savills"] = {"state": "collect_failed"}
    child["pipeline"]["source_results"]["savills"] = {"state": "failed", "reason": "provider failure"}
    for source in sources:
        manifest["sources"][source].update(state="running", checkpoint_run=f"runs/{RUN_ID}",
            attempts=[{"started_at": child["started_at"]}])
    monkeypatch.setattr(series, "_load_child_manifest", lambda *args, **kwargs: (run / "manifest.json", child))
    monkeypatch.setattr(series.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not replay"))
    series.reconcile_pipeline_series(tmp_path, manifest)
    assert manifest["sources"]["cbre"]["state"] == "complete"
    for source in ("lyon-stahl", "franklin-street"):
        assert manifest["sources"][source]["state"] == "qualified_partial_subset"
    assert manifest["sources"]["savills"]["state"] == "failed_source"
    assert series.run_pipeline_series(tmp_path, manifest, env_file=None) == 1
    (run / "manifest.json").write_text(json.dumps(child))
    previous = {"sources": {source: {"lastSuccessfulObservationAt": "2026-10-14T12:00:00Z"}
                            for source in ("lyon-stahl", "franklin-street")}}
    receipt = health.build_receipt(tmp_path, manifest, previous=previous)
    for source in previous["sources"]:
        assert receipt["sources"][source]["publicationStatus"] == "partial"
        assert receipt["sources"][source]["lastAttemptObservationAt"] is None
        assert receipt["sources"][source]["lastSuccessfulObservationAt"] == "2026-10-14T12:00:00Z"


@pytest.mark.parametrize("field", ["attempt", "target", "invalid"])
def test_franklin_readback_tampering_fences_unit(tmp_path, field):
    run = tmp_path / RUN_ID
    child = add_franklin(mixed_child(run), run)
    observed = child["sources"]["franklin-street"]["readback"]["observation"]
    if field == "attempt": observed["attempt_id"] = "0" * 32
    elif field == "target": observed["database_target_sha256"] = "0" * 64
    else: observed["invalid"] = ["bad"]
    assert not series.pipeline_source_qualified(child, "franklin-street", **qualification_context(run))
    assert not series.pipeline_source_qualified(child, "lyon-stahl", **qualification_context(run))


@pytest.mark.parametrize("attempt", [None, "not-an-attempt"])
def test_franklin_missing_or_malformed_attempt_never_qualifies(tmp_path, attempt):
    import cre_checkpoint_refresh as refresh

    run = tmp_path / RUN_ID
    child = add_franklin(mixed_child(run), run)
    checkpoint = child["sources"]["franklin-street"]
    observed = checkpoint["readback"]["observation"]
    if attempt is None:
        checkpoint.pop("subset_ingest_attempt_id")
        observed.pop("attempt_id")
    else:
        checkpoint["subset_ingest_attempt_id"] = observed["attempt_id"] = attempt
    report = {"franklin_partial_readback": copy.deepcopy(observed),
              "queries": {"freshness_generations": [], "inventory_only_index": []}}
    assert not series._pipeline_member_report_matches(report, checkpoint, "franklin-street", RUN_ID)
    child["config"]["sources"] = ["franklin-street"]
    result = refresh.verify_validation_readback(
        run, child, report, persist_manifest=False, sources=["franklin-street"])
    assert result["ok"] is False
    assert child["sources"]["franklin-street"]["readback"]["ok"] is False
