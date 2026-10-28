import pytest
from source_evidence import document_automatic, events
from test_document_acquisition_policy import CODE, TARGET, policy_value
from test_document_candidates import artifact, plan_for


def configuration(tmp_path, *, value=None, evidence=None, sources=None, node="/usr/bin/node"):
    path = tmp_path / "policy.json"
    raw = events.encoded(value or policy_value())
    path.write_bytes(raw)
    path.chmod(0o600)
    automatic = evidence if evidence is not None else {"target_sha256": TARGET, "collector_git_sha": CODE,
                                                      "source_keys": ["svn", "nai-global"]}
    return document_automatic.configuration(path, events.sha256(raw), node, automatic, CODE, sources or ["svn"])


def test_configuration_binds_existing_scope_and_private_policy(tmp_path):
    result = configuration(tmp_path, sources=["nai-global"])
    assert result["source_keys"] == ["svn"]
    assert result["target_sha256"] == TARGET and result["collector_git_sha"] == CODE
    assert result["node_executable"] == "/usr/bin/node"


@pytest.mark.parametrize("evidence,node", [
    (None, "node"),
    ({"target_sha256": TARGET, "collector_git_sha": "0" * 40, "source_keys": ["svn"]}, "/usr/bin/node"),
    ({"target_sha256": TARGET, "collector_git_sha": CODE, "source_keys": ["nai-global"]}, "/usr/bin/node"),
])
def test_configuration_rejects_mismatch(tmp_path, evidence, node):
    with pytest.raises(ValueError):
        configuration(tmp_path, evidence=evidence, node=node)


def test_policy_revalidation_refuses_changed_bytes(tmp_path):
    config = configuration(tmp_path)
    path = tmp_path / "policy.json"
    path.write_bytes(b"{}")
    with pytest.raises(ValueError):
        document_automatic._verify_config(config, {"target_sha256": TARGET, "collector_git_sha": CODE,
                                                   "source_keys": ["svn"]}, ["svn"])


def manifest_for(tmp_path):
    config = configuration(tmp_path)
    evidence = {"target_sha256": TARGET, "collector_git_sha": CODE, "source_keys": ["svn"]}
    attempt = {"number": 1, "rc": 1, "finished_at": "2026-10-28T12:00:00Z",
               "document_collection_settlement": {"schema_version": "cre-document-collection-settlement/v1",
                   "attempt_number": 1, "collector_git_sha": CODE, "rc": 1,
                   "finished_at": "2026-10-28T12:00:00Z"}}
    manifest = {"config": {"sources": ["svn"], "automatic_source_evidence": evidence,
                           "source_document_acquisition": config}, "collector_git_sha": CODE,
                "collector_git_dirty": False, "sources": {"svn": {"attempts": [attempt]}}}
    return manifest, attempt


def test_default_off_is_noop(tmp_path):
    manifest = {"config": {}}
    document_automatic.pump(tmp_path, manifest, lambda *args: pytest.fail("must not save"))
    assert manifest == {"config": {}}


def test_terminal_failed_attempt_can_process_sealed_evidence(monkeypatch, tmp_path):
    manifest, attempt = manifest_for(tmp_path)
    plan = plan_for(artifact())
    monkeypatch.setattr(document_automatic, "_sealed_plans", lambda *args: [plan])
    calls = []
    def run(*args, **kwargs):
        calls.append(kwargs["plan"])
        state = {"state": "selection_settled", "documents": [{"state": "extraction_sealed",
                                                                 "extraction_state": "success"}]}
        attempt["document_source_evidence"] = state
        return state
    monkeypatch.setattr(document_automatic.document_pipeline, "run", run)
    document_automatic.pump(tmp_path, manifest, lambda *args: None)
    assert calls == [[plan]] and manifest["source_document_acquisition_status"] == "settled"
    assert manifest["source_document_acquisition_paused"] is False


def test_cleanup_unknown_pauses_and_propagates(monkeypatch, tmp_path):
    manifest, _ = manifest_for(tmp_path)
    plan = plan_for(artifact())
    monkeypatch.setattr(document_automatic, "_sealed_plans", lambda *args: [plan])
    def fail(*args, **kwargs):
        raise document_automatic.DocumentProcessCleanupUnknown("unknown group")
    monkeypatch.setattr(document_automatic.document_pipeline, "run", fail)
    with pytest.raises(document_automatic.DocumentProcessCleanupUnknown):
        document_automatic.pump(tmp_path, manifest, lambda *args: None)
    assert manifest["source_document_acquisition_paused"] is True
    assert manifest["source_document_acquisition_status"] == "incomplete"


def test_save_failure_context_cannot_hide_cleanup_unknown(monkeypatch, tmp_path):
    from cre_checkpoint_refresh import _cleanup_unknown_in_chain

    manifest, _ = manifest_for(tmp_path)
    monkeypatch.setattr(document_automatic, "_sealed_plans", lambda *args: [plan_for(artifact())])
    def fail(*args, **kwargs):
        try:
            raise document_automatic.DocumentProcessCleanupUnknown("unknown group")
        except document_automatic.DocumentProcessCleanupUnknown:
            raise OSError("checkpoint save failed")
    monkeypatch.setattr(document_automatic.document_pipeline, "run", fail)
    with pytest.raises(OSError) as result:
        document_automatic.pump(tmp_path, manifest, lambda *args: None)
    assert _cleanup_unknown_in_chain(result.value)
    assert manifest["source_document_acquisition_paused"] is True


def test_cleanup_unknown_survives_second_save_failure(monkeypatch, tmp_path):
    from cre_checkpoint_refresh import _cleanup_unknown_in_chain

    manifest, _ = manifest_for(tmp_path)
    monkeypatch.setattr(document_automatic, "_sealed_plans", lambda *args: [plan_for(artifact())])
    def pipeline_failure(*args, **kwargs):
        try:
            raise document_automatic.DocumentProcessCleanupUnknown("unknown group")
        except document_automatic.DocumentProcessCleanupUnknown:
            raise OSError("pipeline checkpoint save failed")
    def save_failure(*args):
        raise OSError("document lane checkpoint save failed")
    monkeypatch.setattr(document_automatic.document_pipeline, "run", pipeline_failure)
    with pytest.raises(document_automatic.DocumentProcessCleanupUnknown) as result:
        document_automatic.pump(tmp_path, manifest, save_failure)
    assert _cleanup_unknown_in_chain(result.value)
    assert manifest["source_document_acquisition_paused"] is True


def test_missing_settlement_and_unknown_pause_without_actions(monkeypatch, tmp_path):
    manifest, attempt = manifest_for(tmp_path)
    monkeypatch.setattr(document_automatic, "_sealed_plans", lambda *args: pytest.fail("must not read plans"))
    attempt.pop("document_collection_settlement")
    document_automatic.pump(tmp_path, manifest, lambda *args: None)
    assert manifest["source_document_acquisition_paused"] is True
    assert manifest["source_document_acquisition_status"] == "incomplete"
    manifest, attempt = manifest_for(tmp_path)
    attempt["source_evidence"] = {"state": "outcome_unknown"}
    document_automatic.pump(tmp_path, manifest, lambda *args: None)
    assert manifest["source_document_acquisition_paused"] is True


def test_no_data_is_incomplete_and_existing_attempt_never_restarts(monkeypatch, tmp_path):
    manifest, attempt = manifest_for(tmp_path)
    monkeypatch.setattr(document_automatic, "_sealed_plans", lambda *args: [])
    document_automatic.pump(tmp_path, manifest, lambda *args: None)
    assert attempt["document_source_evidence"]["state"] == "no_data"
    assert manifest["source_document_acquisition_status"] == "incomplete"
    monkeypatch.setattr(document_automatic.document_pipeline, "run", lambda *args, **kwargs: pytest.fail("must not retry"))
    document_automatic.pump(tmp_path, manifest, lambda *args: None)
    assert manifest["source_document_acquisition_paused"] is False
    assert manifest["source_document_acquisition_status"] == "incomplete"


def test_resume_gate_rejects_paused_and_unsettled_without_restarting(tmp_path):
    manifest, attempt = manifest_for(tmp_path)
    document_automatic.require_resume(manifest)
    attempt["document_source_evidence"] = {"state": "reconciliation_required"}
    with pytest.raises(ValueError):
        document_automatic.require_resume(manifest)
    attempt.pop("document_source_evidence")
    manifest["source_document_acquisition_paused"] = True
    with pytest.raises(ValueError):
        document_automatic.require_resume(manifest)


def test_known_no_data_remains_incomplete_but_allows_later_attempt(monkeypatch, tmp_path):
    manifest, first = manifest_for(tmp_path)
    first["document_source_evidence"] = {"schema_version": "cre-foreground-documents/v1", "state": "no_data",
                                         "reason": "no_sealed_source_plan"}
    prior = dict(first["document_source_evidence"])
    second = {"number": 2, "rc": 0, "finished_at": "2026-10-28T12:01:00Z",
              "document_collection_settlement": {"schema_version": "cre-document-collection-settlement/v1",
                  "attempt_number": 2, "collector_git_sha": CODE, "rc": 0,
                  "finished_at": "2026-10-28T12:01:00Z"}}
    manifest["sources"]["svn"]["attempts"].append(second)
    value = artifact()
    value["listings"] = []
    value["sources"][0]["retainedObservations"] = []
    plan = plan_for(value)
    monkeypatch.setattr(document_automatic, "_sealed_plans", lambda _, __, attempt: [plan] if attempt is second else pytest.fail("old attempt must not restart"))
    monkeypatch.setattr(document_automatic.document_pipeline, "run", lambda *a, **k: pytest.fail("no candidate fetch"))
    assert not document_automatic.reconciliation_required(manifest)
    document_automatic.require_resume(manifest)
    document_automatic.pump(tmp_path, manifest, lambda *args: None)
    assert first["document_source_evidence"] == prior
    assert second["document_source_evidence"]["state"] == "no_candidates"
    assert manifest["source_document_acquisition_status"] == "incomplete"
    assert manifest["source_document_acquisition_paused"] is False


def test_invalid_timestamp_and_paused_source_lane_refuse(monkeypatch, tmp_path):
    manifest, attempt = manifest_for(tmp_path)
    attempt["finished_at"] = "not-a-time"
    attempt["document_collection_settlement"]["finished_at"] = "not-a-time"
    monkeypatch.setattr(document_automatic, "_sealed_plans", lambda *args: pytest.fail("must not access plans"))
    document_automatic.pump(tmp_path, manifest, lambda *args: None)
    assert manifest["source_document_acquisition_paused"] is True
    manifest, _ = manifest_for(tmp_path)
    manifest["automatic_source_evidence_paused"] = True
    document_automatic.pump(tmp_path, manifest, lambda *args: None)
    assert manifest["source_document_acquisition_status"] == "incomplete"


def test_zero_attempts_are_incomplete(tmp_path):
    manifest, _ = manifest_for(tmp_path)
    manifest["sources"]["svn"]["attempts"] = []
    document_automatic.pump(tmp_path, manifest, lambda *args: None)
    assert manifest["source_document_acquisition_status"] == "incomplete"


def test_previously_settled_missing_private_custody_pauses(monkeypatch, tmp_path):
    manifest, attempt = manifest_for(tmp_path)
    plan = plan_for(artifact())
    monkeypatch.setattr(document_automatic, "_sealed_plans", lambda *args: [plan])
    attempt["document_source_evidence"] = {"state": "selection_settled", "documents": [
        {"state": "extraction_sealed", "extraction_state": "success"}]}
    monkeypatch.setattr(document_automatic.document_pipeline, "run", lambda *a, **k: pytest.fail("must not rerun"))
    document_automatic.pump(tmp_path, manifest, lambda *args: None)
    assert manifest["source_document_acquisition_status"] == "incomplete"
    assert manifest["source_document_acquisition_paused"] is True


def test_no_candidates_marker_must_match_rederived_selection(monkeypatch, tmp_path):
    manifest, attempt = manifest_for(tmp_path)
    value = artifact()
    value["listings"] = []
    value["sources"][0]["retainedObservations"] = []
    plan = plan_for(value)
    monkeypatch.setattr(document_automatic, "_sealed_plans", lambda *args: [plan])
    document_automatic.pump(tmp_path, manifest, lambda *args: None)
    assert attempt["document_source_evidence"]["state"] == "no_candidates"
    attempt["document_source_evidence"]["selection_sha256"] = "0" * 64
    document_automatic.pump(tmp_path, manifest, lambda *args: None)
    assert manifest["source_document_acquisition_paused"] is True
