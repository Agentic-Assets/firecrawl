import copy

import pytest
from source_evidence import document_automatic, document_batch, document_jobs, document_pipeline, document_recovery, events
from test_document_acquisition_policy import CODE, TARGET
from test_document_pipeline import install_workers, setup_run


def prepared_run(monkeypatch, tmp_path, *, extraction_state="success"):
    manifest, attempt, kwargs = setup_run(tmp_path, retention="keep_unresolved_until_review")
    plans = [kwargs["plan"]]
    candidates = document_batch.discover(plans)
    selection = document_batch.prepare(plans, candidates, kwargs["policy_raw"],
        expected_policy_sha256=kwargs["expected_policy_sha256"], target_sha256=TARGET, collector_git_sha=CODE)
    kwargs.update(plan=plans, candidates=candidates, selection=selection)
    manifest.update(run_id="test-generation", config={"sources": ["svn"]})
    attempt.update(rc=1, finished_at="2026-10-28T12:00:00Z", document_collection_settlement={
        "schema_version": "cre-document-collection-settlement/v1", "attempt_number": 1,
        "collector_git_sha": CODE, "rc": 1, "finished_at": "2026-10-28T12:00:00Z"})
    policy_path = tmp_path / "document-policy.json"
    policy_path.write_bytes(kwargs["policy_raw"])
    policy_path.chmod(0o600)
    manifest["config"]["automatic_source_evidence"] = {"source_keys": ["svn"], "collector_git_sha": CODE,
        "target_sha256": TARGET, "policy_sha256": "b" * 64, "transport": {"kind": "test"},
        "document_text_formats": {"svn": document_jobs.FORMAT}}
    manifest["config"]["source_document_acquisition"] = {"source_keys": ["svn"], "collector_git_sha": CODE,
        "target_sha256": TARGET, "policy_sha256": kwargs["expected_policy_sha256"], "policy_path": str(policy_path)}
    monkeypatch.setattr(document_jobs.automatic, "_verify_config", lambda *args: None)
    monkeypatch.setattr(document_jobs.document_automatic, "_verify_config", lambda *args: None)
    install_workers(monkeypatch, extraction_state=extraction_state)
    state = document_pipeline.run(tmp_path, manifest, "svn", attempt, lambda *args: None, **kwargs)
    return manifest, attempt, state


def test_78_sealed_jobs_rederive_exact_plans_without_original_bytes(monkeypatch, tmp_path):
    manifest, attempt, state = prepared_run(monkeypatch, tmp_path)
    assert len(state["documents"]) == 78
    calls = []
    for document in state["documents"]:
        job = document_jobs.mark(tmp_path, manifest, "svn", attempt, document, lambda *args: calls.append(1))
        assert state["database_append"] == "managed_by_append_jobs"
        plan = document_jobs.prepare_job(tmp_path, manifest, "svn", attempt, document, job)
        assert job["state"] == "pending" and job["kind"] == "document_text"
        assert plan["plan_sha256"] == job["plan_sha256"] and plan["events"][0]["capture_id"] == job["capture_id"]
        assert plan["events"][0]["facts"][0]["value"]["acquisition_context"]["candidate"]["occurrences"]
    assert len(calls) == 78


def test_sealed_recovery_accepts_managed_append_marker(monkeypatch, tmp_path):
    manifest, attempt, state = prepared_run(monkeypatch, tmp_path)
    document_jobs.mark(tmp_path, manifest, "svn", attempt, state["documents"][0], lambda *args: None)
    plans = document_automatic._sealed_plans(tmp_path, "svn", attempt)
    candidates = document_batch.discover(plans)
    config = manifest["config"]["source_document_acquisition"]
    raw = (tmp_path / "document-policy.json").read_bytes()
    selected = document_batch.prepare(plans, candidates, raw, expected_policy_sha256=config["policy_sha256"],
                                      target_sha256=TARGET, collector_git_sha=CODE)
    result = document_recovery.sealed(tmp_path, manifest, "svn", attempt, lambda *args: None,
        plan=plans, candidates=candidates, policy_raw=raw, selection=selected,
        expected_policy_sha256=config["policy_sha256"], target_sha256=TARGET, collector_git_sha=CODE, cleanup=False)
    assert result["state"] == "sealed_outputs_reconciled"
    assert state["database_append"] == "managed_by_append_jobs"


@pytest.mark.parametrize("extraction_state", ["success", "ocr_needed", "failed"])
def test_individual_sealed_documents_can_append_despite_partial_selection(monkeypatch, tmp_path, extraction_state):
    manifest, attempt, state = prepared_run(monkeypatch, tmp_path, extraction_state=extraction_state)
    state["state"] = "reconciliation_required"
    document = state["documents"][0]
    job = document_jobs.mark(tmp_path, manifest, "svn", attempt, document, lambda *args: None)
    plan = document_jobs.prepare_job(tmp_path, manifest, "svn", attempt, document, job)
    expected = "extracted" if extraction_state == "success" else extraction_state
    assert plan["events"][0]["facts"][0]["value"]["extraction"]["status"] == expected


def test_existing_unknown_job_is_not_rearmed(monkeypatch, tmp_path):
    manifest, attempt, state = prepared_run(monkeypatch, tmp_path)
    document = state["documents"][0]
    job = document_jobs.mark(tmp_path, manifest, "svn", attempt, document, lambda *args: None)
    job["state"] = "outcome_unknown"
    before = copy.deepcopy(job)
    assert document_jobs.mark(tmp_path, manifest, "svn", attempt, document, lambda *args: pytest.fail("must not save")) == before
    assert job["state"] == "outcome_unknown"


@pytest.mark.parametrize("mutation", ["policy", "missing", "seal", "marker", "settlement", "format"])
def test_refuses_changed_custody_or_policy(monkeypatch, tmp_path, mutation):
    manifest, attempt, state = prepared_run(monkeypatch, tmp_path)
    document = state["documents"][0]
    job = document_jobs.mark(tmp_path, manifest, "svn", attempt, document, lambda *args: None)
    seal = tmp_path / document["directory"] / "sealed"
    if mutation == "policy":
        path = tmp_path / "document-policy.json"
        path.write_bytes(b"{}")
    elif mutation == "missing":
        (seal / "artifact.json").unlink()
    elif mutation == "seal":
        (seal / "plan.json").write_bytes(b"{}")
    elif mutation == "marker":
        document["sealed"]["capture_id"] = "changed"
    elif mutation == "settlement":
        attempt["document_collection_settlement"]["rc"] = False
    else:
        manifest["config"]["automatic_source_evidence"]["document_text_formats"] = {}
    with pytest.raises((ValueError, OSError, KeyError)):
        document_jobs.prepare_job(tmp_path, manifest, "svn", attempt, document, job)
