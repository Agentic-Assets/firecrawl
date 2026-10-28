"""Draft coordinator contracts; no real workers, providers or databases."""

import copy

import cre_checkpoint_refresh as refresh
import pytest

from source_evidence import document_automatic, document_jobs
from source_evidence.document_process import DocumentProcessCleanupUnknown

CODE = "a" * 40
DOCUMENT = {"policy_path": "/private/documents.json", "policy_sha256": "b" * 64,
            "node_executable": "/private/node", "collector_git_sha": CODE,
            "target_sha256": "c" * 64, "source_keys": ["svn"]}
AUTOMATIC = {"collector_git_sha": CODE, "target_sha256": "c" * 64, "source_keys": ["svn"]}


def manifest(tmp_path, *, enabled=True):
    return refresh.new_manifest(tmp_path, git_sha=CODE, git_dirty=False, sources=("svn",),
                                page_cap=400, concurrency=1,
                                automatic_source_evidence=AUTOMATIC if enabled else None,
                                source_document_acquisition=DOCUMENT if enabled else None)


def test_document_configuration_is_bound_on_resume(tmp_path):
    value = manifest(tmp_path)
    refresh.save_manifest(tmp_path, value)
    args = {"git_sha": CODE, "sources": ("svn",), "page_cap": 400, "concurrency": 1,
            "automatic_source_evidence": AUTOMATIC, "source_document_acquisition": DOCUMENT}
    restored = refresh.load_resume_manifest(tmp_path / "manifest.json", **args)
    assert restored["config"]["source_document_acquisition"] == DOCUMENT
    changed = dict(DOCUMENT, node_executable="/private/other-node")
    with pytest.raises(refresh.RefreshError):
        refresh.load_resume_manifest(tmp_path / "manifest.json", **{**args, "source_document_acquisition": changed})
    with pytest.raises(refresh.RefreshError):
        refresh.load_resume_manifest(tmp_path / "manifest.json", **{**args, "source_document_acquisition": None})


def test_foreground_document_pump_requires_owner_before_and_after(monkeypatch, tmp_path):
    value = manifest(tmp_path)
    calls = []
    monkeypatch.setattr(refresh, "git_identity", lambda: (CODE, False))
    monkeypatch.setattr(document_automatic, "pump", lambda run, current, save: calls.append("documents"))
    refresh.pump_source_documents(tmp_path, value, owner_check=lambda: calls.append("owner"))
    assert calls == ["owner", "documents", "owner"]


def test_foreground_pump_rechecks_owner_on_failure(monkeypatch, tmp_path):
    value = manifest(tmp_path)
    calls = []
    monkeypatch.setattr(refresh, "git_identity", lambda: (CODE, False))
    def fail(*_):
        calls.append("documents")
        raise ValueError("synthetic failure")
    monkeypatch.setattr(document_automatic, "pump", fail)
    with pytest.raises(ValueError):
        refresh.pump_source_documents(tmp_path, value, owner_check=lambda: calls.append("owner"))
    assert calls == ["owner", "documents", "owner"]


def test_changed_checkout_cannot_start_document_work(monkeypatch, tmp_path):
    value = manifest(tmp_path)
    monkeypatch.setattr(refresh, "git_identity", lambda: (CODE, True))
    monkeypatch.setattr(document_automatic, "pump", lambda *_: pytest.fail("must not acquire"))
    monkeypatch.setattr(refresh, "save_manifest", lambda *_: None)
    with pytest.raises(refresh.GlobalStageError):
        refresh.pump_source_documents(tmp_path, value, owner_check=lambda: None)
    assert value["source_document_acquisition_paused"] is True


def test_legacy_run_has_no_document_runtime_effect(monkeypatch, tmp_path):
    value = manifest(tmp_path, enabled=False)
    before = copy.deepcopy(value)
    def forbidden(*_):
        pytest.fail("legacy run must not enter document work")
    monkeypatch.setattr(refresh, "git_identity", forbidden)
    monkeypatch.setattr(document_automatic, "pump", forbidden)
    refresh.pump_source_documents(tmp_path, value, owner_check=forbidden)
    refresh.mark_source_document_settlement(value, {"number": 1})
    assert value == before and "source_document_acquisition" not in value["config"]


def test_settlement_marker_requires_known_command_result(tmp_path):
    value = manifest(tmp_path)
    attempt = {"number": 1, "rc": None, "finished_at": None}
    with pytest.raises(refresh.GlobalStageError):
        refresh.mark_source_document_settlement(value, attempt)
    attempt.update(rc=1, finished_at="2026-10-28T12:00:00Z")
    refresh.mark_source_document_settlement(value, attempt)
    assert document_automatic._terminal(attempt, value)
    attempt["rc"] = True
    assert not document_automatic._terminal(attempt, value)


def test_unknown_document_cleanup_preserves_parent_recovery_barrier():
    unknown = DocumentProcessCleanupUnknown("synthetic unsettled child")
    later = OSError("synthetic checkpoint save failure")
    later.__context__ = unknown
    assert refresh._cleanup_unknown_in_chain(unknown)
    assert refresh._cleanup_unknown_in_chain(later)


def test_v3_marks_partial_seals_before_shared_foreground_append(monkeypatch, tmp_path):
    value = manifest(tmp_path)
    value["config"]["automatic_source_evidence"]["document_text_formats"] = {"svn": document_jobs.FORMAT}
    document = {"state": "extraction_sealed", "extraction_state": "success"}
    value["sources"]["svn"]["attempts"] = [{"number": 1, "document_source_evidence": {
        "state": "reconciliation_required", "documents": [document]}}]
    order = []
    monkeypatch.setattr(refresh, "git_identity", lambda: (CODE, False))
    def acquire(*_):
        order.append("acquisition")
        value["source_document_acquisition_paused"] = True
    def mark(_run, _manifest, source, attempt, selected, save):
        assert source == "svn" and selected is document
        selected["append"] = {"state": "pending", "kind": "document_text"}
        order.append("mark")
    def append(*_):
        assert document["append"]["state"] == "pending"
        order.append("append")
    monkeypatch.setattr(document_automatic, "pump", acquire)
    monkeypatch.setattr(document_jobs, "mark", mark)
    monkeypatch.setattr(refresh, "pump_source_evidence", append)
    refresh.pump_source_documents(tmp_path, value, owner_check=lambda: None)
    assert order == ["acquisition", "mark", "append"]
    assert value["source_document_acquisition_paused"] is True


def test_v1_policy_does_not_admit_document_append(monkeypatch, tmp_path):
    value = manifest(tmp_path)
    monkeypatch.setattr(refresh, "git_identity", lambda: (CODE, False))
    monkeypatch.setattr(document_automatic, "pump", lambda *_: None)
    def forbidden(*_):
        pytest.fail("legacy evidence policy cannot grant document append")
    monkeypatch.setattr(document_jobs, "mark", forbidden)
    monkeypatch.setattr(refresh, "pump_source_evidence", forbidden)
    refresh.pump_source_documents(tmp_path, value, owner_check=lambda: None)


def test_explicit_append_replay_does_not_resolve_acquisition_uncertainty(tmp_path):
    value = manifest(tmp_path)
    job = {"kind": "document_text", "state": "outcome_unknown"}
    value["sources"]["svn"]["attempts"] = [{"document_source_evidence": {
        "state": "reconciliation_required", "documents": [{"append": job}]}}]
    before = copy.deepcopy(value)
    with pytest.raises(ValueError):
        document_automatic.require_resume(value)
    document_automatic.require_resume(value, replay_unknown=True)
    assert value == before
    job["state"] = "appended"
    with pytest.raises(ValueError):
        document_automatic.require_resume(value)
