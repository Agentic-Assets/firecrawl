"""Draft synthetic serial-series document configuration and stop contracts."""

import copy

import pytest

import cre_checkpoint_series as series
from test_cre_checkpoint_series import config as base_config


def configured():
    value = base_config(("svn", "cbre"))
    value["automatic_source_evidence"] = {"policy_path": "/private/evidence.json", "policy_sha256": "a" * 64,
                                          "env_file": "/private/db.env", "target_sha256": "b" * 64}
    value["source_document_acquisition"] = {"policy_path": "/private/documents.json", "policy_sha256": "c" * 64,
                                             "node_executable": "/usr/bin/node", "source_keys": ["svn"],
                                             "target_sha256": "b" * 64, "collector_git_sha": "d" * 40}
    return value


def test_child_arguments_and_exact_child_configuration(tmp_path):
    config = configured()
    args = series.build_checkpoint_argv("svn", child_out_root=tmp_path, env_file=None, config=config)
    assert args[args.index("--source-document-policy") + 1] == "/private/documents.json"
    assert args[args.index("--source-document-policy-sha256") + 1] == "c" * 64
    assert args[args.index("--source-document-node") + 1] == "/usr/bin/node"
    assert series._expected_child_config("svn", config)["source_document_acquisition"] == config["source_document_acquisition"]
    legacy = base_config(("svn",))
    assert "source_document_acquisition" not in series._expected_child_config("svn", legacy)
    assert "--source-document-policy" not in series.build_checkpoint_argv(
        "svn", child_out_root=tmp_path, env_file=None, config=legacy)


@pytest.mark.parametrize("status,paused", [(None, None), ("running", False), ("reconciliation_required", True),
                                            ("settled", True)])
def test_unsettled_prior_child_cannot_be_retried(status, paused):
    config = configured()
    child = {"config": series._expected_child_config("svn", config),
             "source_document_acquisition_status": status, "source_document_acquisition_paused": paused}
    assert series.document_reconciliation_required(child, config)
    assert not series._child_succeeded(child, config)
    child.update(status="failed", error="RefreshError: source checkpoints remain incomplete: svn")
    assert not series.source_local_failure(child)


def test_settled_scoped_child_and_unscoped_child_legacy_behavior():
    config = configured()
    child = {"config": series._expected_child_config("svn", config),
             "source_document_acquisition_status": "settled", "source_document_acquisition_paused": False}
    assert not series.document_reconciliation_required(child, config)
    assert not series.document_reconciliation_required({"config": {"sources": ["cbre"]}}, config)
    assert not series.document_reconciliation_required({}, base_config(("svn",)))


def test_pipeline_accepts_document_config_and_blocks_incomplete_document_outcome():
    configured_value = configured()
    config = series.series_config(
        sources=("svn", "cbre"), page_cap=20, concurrency=1, attempts_per_source=1,
        max_resume_age_hours=24, max_host_cpu_percent=90, cpu_sustain_seconds=30,
        cpu_sample_seconds=2, nice=10, pipeline=True,
        automatic_source_evidence=configured_value["automatic_source_evidence"],
        source_document_acquisition=configured_value["source_document_acquisition"])
    child = {"config": series._expected_child_config("svn,cbre", config),
             "automatic_source_evidence_status": "settled", "sources": {},
             "source_document_acquisition_status": "incomplete", "source_document_acquisition_paused": False}
    assert series._pipeline_has_unresolved(child, config)
    child["source_document_acquisition_status"] = "settled"
    assert not series._pipeline_has_unresolved(child, config)
    child["source_document_acquisition_paused"] = True
    assert series._pipeline_has_unresolved(child, config)


def test_known_no_data_is_retryable_without_claiming_document_completion():
    config = configured()
    child = {"config": series._expected_child_config("svn", config),
             "status": "failed", "error": "RefreshError: source checkpoints remain incomplete: svn",
             "source_document_acquisition_status": "incomplete", "source_document_acquisition_paused": False,
             "sources": {"svn": {"attempts": [{"document_source_evidence": {
                 "schema_version": "cre-foreground-documents/v1", "state": "no_data",
                 "reason": "no_sealed_source_plan"}}]}}}
    assert not series.document_reconciliation_required(child, config)
    assert series.document_incomplete(child, config)
    assert not series._child_succeeded(child, config)
    assert series.source_local_failure(child)
    forged = copy.deepcopy(child)
    forged["sources"]["svn"]["attempts"][0]["document_source_evidence"]["reason"] = "unknown"
    assert series.document_reconciliation_required(forged, config)
    missing = copy.deepcopy(child)
    del missing["sources"]["svn"]["attempts"][0]["document_source_evidence"]
    assert series.document_reconciliation_required(missing, config)


def test_resume_rejects_document_policy_or_node_drift(tmp_path, monkeypatch):
    original = configured()
    manifest = series.new_manifest(tmp_path, git_sha="d" * 40, config=original)
    # Isolate persistence from health publication and the external environment.
    monkeypatch.setattr(series, "publish_series_health", lambda *args: None)
    series.save_manifest(tmp_path, manifest)
    for key in ("policy_sha256", "node_executable"):
        changed = copy.deepcopy(original)
        changed["source_document_acquisition"][key] = "changed"
        with pytest.raises(series.SeriesError, match="configuration differs"):
            series.load_resume_manifest(tmp_path / "manifest.json", git_sha="d" * 40, config=changed)


def test_retry_failed_stops_before_child_launch_when_prior_document_outcome_unknown(tmp_path, monkeypatch):
    config = configured()
    manifest = series.new_manifest(tmp_path, git_sha="d" * 40, config=config)
    checkpoint = manifest["sources"]["svn"]
    checkpoint.update(state="failed_source", checkpoint_run="runs/prior")
    prior = {"config": series._expected_child_config("svn", config),
             "source_document_acquisition_status": "reconciliation_required",
             "source_document_acquisition_paused": True}
    monkeypatch.setattr(series, "_load_child_manifest", lambda *a, **k: (tmp_path / "prior.json", prior))
    monkeypatch.setattr(series, "save_manifest", lambda *a: None)
    def forbidden(*args, **kwargs):
        pytest.fail("unresolved prior document work must not launch a child")
    monkeypatch.setattr(series.subprocess, "Popen", forbidden)
    assert series.run_series(tmp_path, manifest, env_file=None, retry_failed=True) == 1
    assert manifest["status"] == "failed"
