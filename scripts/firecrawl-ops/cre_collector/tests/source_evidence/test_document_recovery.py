"""Offline recovery fixtures; no provider or converter is invoked by recovery."""

import copy
from pathlib import Path

import pytest

from source_evidence import document_pipeline, document_recovery
from test_document_pipeline import execute, install_workers, setup_run


def recover(tmp_path, manifest, attempt, kwargs, *, cleanup=False):
    arguments = {key: value for key, value in kwargs.items() if key != "node_executable"}
    return document_recovery.sealed(tmp_path, manifest, "svn", attempt, lambda *_: None,
                                    cleanup=cleanup, **arguments)


def forbid_workers(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("recovery must not fetch or invoke a converter")
    monkeypatch.setattr(document_pipeline.doc_fetch_process, "run", forbidden)
    monkeypatch.setattr(document_pipeline.doc_extract_process, "run", forbidden)


def test_78_seals_recovered_without_new_work_or_budget_changes(monkeypatch, tmp_path):
    manifest, attempt, kwargs = setup_run(tmp_path)
    install_workers(monkeypatch)
    execute(tmp_path, manifest, attempt, kwargs)
    state = attempt["document_source_evidence"]
    budget = copy.deepcopy(state["budget"])
    original = copy.deepcopy(manifest)
    forbid_workers(monkeypatch)
    result = recover(tmp_path, manifest, attempt, kwargs)
    assert result["state"] == "sealed_outputs_reconciled" and len(result["documents"]) == 78
    assert all(row["state"] == "seal_reverified" and row["original_bytes_reverified"] is False
               for row in result["documents"])
    assert result["network_execution"] == result["conversion_execution"] == "not_performed"
    assert result["new_acquisition_allowed"] is False and state["budget"] == budget
    assert state["database_append"] == "not_performed" and state["coverage"] == "unknown"
    assert manifest["sources"]["svn"]["attempts"][0]["source_evidence"] == (
        original["sources"]["svn"]["attempts"][0]["source_evidence"])


def interrupted_cleanup(monkeypatch, tmp_path):
    manifest, attempt, kwargs = setup_run(tmp_path)
    install_workers(monkeypatch)
    original_remove = document_pipeline._remove_owned_body
    removed = []
    def interrupt_second(path, digest, length):
        if removed:
            raise OSError("synthetic interruption")
        original_remove(path, digest, length)
        removed.append(path)
    monkeypatch.setattr(document_pipeline, "_remove_owned_body", interrupt_second)
    with pytest.raises(OSError):
        execute(tmp_path, manifest, attempt, kwargs)
    monkeypatch.setattr(document_pipeline, "_remove_owned_body", original_remove)
    return manifest, attempt, kwargs


def test_pending_absent_copy_is_reconciled_without_actor_assertion(monkeypatch, tmp_path):
    manifest, attempt, kwargs = interrupted_cleanup(monkeypatch, tmp_path)
    state = attempt["document_source_evidence"]
    document = state["documents"][0]
    # Model a durable checkpoint from before the first unlink's save.
    document["cleanup"][0]["state"] = "pending"
    budget = copy.deepcopy(state["budget"])
    forbid_workers(monkeypatch)
    result = recover(tmp_path, manifest, attempt, kwargs, cleanup=True)
    assert result["state"] == "sealed_outputs_reconciled"
    assert [item["state"] for item in document["cleanup"]] == ["reconciled_absent", "removed"]
    assert document["original_retention"] == "owned_body_copies_absent_after_reconciliation"
    assert result["original_bytes_reverified"] is False and state["budget"] == budget


def test_previously_removed_copy_reappearing_is_unresolved(monkeypatch, tmp_path):
    manifest, attempt, kwargs = interrupted_cleanup(monkeypatch, tmp_path)
    document = attempt["document_source_evidence"]["documents"][0]
    returned = Path(document["cleanup"][0]["path"])
    returned.write_bytes(b"pdf")
    returned.chmod(0o600)
    forbid_workers(monkeypatch)
    result = recover(tmp_path, manifest, attempt, kwargs, cleanup=True)
    assert result["state"] == "incomplete" and result["documents"][0]["state"] == "unresolved"
    assert returned.read_bytes() == b"pdf"


def test_checkpoint_save_failure_stops_cleanup_before_second_copy_and_later_documents(monkeypatch, tmp_path):
    manifest, attempt, kwargs = setup_run(tmp_path)
    install_workers(monkeypatch)
    execute(tmp_path, manifest, attempt, kwargs)
    state = attempt["document_source_evidence"]
    first, later = state["documents"][:2]
    # Model a saved checkpoint in which these copies are still awaiting cleanup.
    for document in (first, later):
        for item in document["cleanup"]:
            path = Path(item["path"])
            path.write_bytes(b"pdf")
            path.chmod(0o600)
            item["state"] = "pending"
    later_before = copy.deepcopy(later)
    budget_before = copy.deepcopy(state["budget"])
    forbid_workers(monkeypatch)
    def save(_, current):
        cleanup = current["sources"]["svn"]["attempts"][0]["document_source_evidence"]["documents"][0]["cleanup"]
        if cleanup[0]["state"] == "removed":
            raise OSError("synthetic durable checkpoint failure")
    arguments = {key: value for key, value in kwargs.items() if key != "node_executable"}
    with pytest.raises(document_recovery.CheckpointSaveError):
        document_recovery.sealed(tmp_path, manifest, "svn", attempt, save, cleanup=True, **arguments)
    assert not Path(first["cleanup"][0]["path"]).exists()
    assert Path(first["cleanup"][1]["path"]).read_bytes() == b"pdf"
    assert later == later_before
    assert all(Path(item["path"]).read_bytes() == b"pdf" for item in later["cleanup"])
    assert state["budget"] == budget_before


@pytest.mark.parametrize("target", ["marker", "context", "candidate", "selection"])
def test_tampered_bindings_are_refused_or_unresolved(monkeypatch, tmp_path, target):
    manifest, attempt, kwargs = setup_run(tmp_path, total=10)
    install_workers(monkeypatch)
    execute(tmp_path, manifest, attempt, kwargs)
    document = attempt["document_source_evidence"]["documents"][0]
    if target == "marker":
        document["sealed"]["capture_id"] = "forged"
    elif target == "context":
        path = tmp_path / document["directory"] / "sealed" / "acquisition.json"
        path.write_bytes(b"{}")
    elif target == "candidate":
        document["candidate_sha256"] = "0" * 64
    else:
        kwargs["selection"] = copy.deepcopy(kwargs["selection"])
        kwargs["selection"]["documents"][0]["candidate_index"] = 1000
    forbid_workers(monkeypatch)
    if target in ("candidate", "selection"):
        with pytest.raises(ValueError):
            recover(tmp_path, manifest, attempt, kwargs)
    else:
        result = recover(tmp_path, manifest, attempt, kwargs)
        assert result["state"] == "incomplete" and result["documents"][0]["state"] == "unresolved"


def test_missing_persisted_marker_stays_unresolved_despite_seal_files(monkeypatch, tmp_path):
    manifest, attempt, kwargs = setup_run(tmp_path, total=10)
    install_workers(monkeypatch)
    execute(tmp_path, manifest, attempt, kwargs)
    document = attempt["document_source_evidence"]["documents"][0]
    del document["sealed"]
    assert (tmp_path / document["directory"] / "sealed" / "plan.json").is_file()
    forbid_workers(monkeypatch)
    result = recover(tmp_path, manifest, attempt, kwargs)
    assert result["state"] == "incomplete"
    assert result["documents"][0]["reason"] == "missing_persisted_seal_marker"
