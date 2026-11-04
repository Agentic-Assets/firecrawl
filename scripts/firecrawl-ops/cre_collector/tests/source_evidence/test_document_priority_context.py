"""Priority selection remains authenticated through context, seals and recovery."""

import copy
import json

import pytest
from test_doc_extract_process import artifact as text_artifact
from test_document_acquisition import fixture

from source_evidence import (
    document_acquisition,
    document_acquisition_policy,
    document_seal,
    events,
)


def selected_context(*, batch=False):
    plan, candidates, _, _, _, receipt, body, binding = fixture()
    from test_document_acquisition_policy import policy_value

    policy = policy_value()
    policy.update(schema_version=document_acquisition_policy.SCHEMA_V4,
                  selection_order="property_occurrences_first_v1",
                  pdf_complement={"mode": "poppler_pdf_text_v1", "pdftotext_path": "/usr/bin/pdftotext"},
                  sparse_ocr={"mode": "sparse_ocr_v1", "strategy": "no_text", "max_pages": 100})
    policy["limits"]["run_timeout_seconds"] = 3600
    raw = events.encoded(policy)
    binding["expected_policy_sha256"] = events.sha256(raw)
    prepare = document_acquisition_policy.prepare
    if batch:
        from source_evidence import document_batch

        plan = [plan]
        candidates = document_batch.discover(plan)
        prepare = document_batch.prepare
    selection = prepare(plan, candidates, raw, **binding)
    chosen = selection["documents"][3]
    url = chosen["candidate"]["url"]
    receipt.update(request_url=url, final_url=url, hops=[{"url": url, "status": 200}])
    context = document_acquisition.build_context(plan, candidates, raw, selection, chosen["candidate_index"],
                                                 receipt, body, **binding)
    return plan, candidates, raw, selection, context, binding


@pytest.mark.parametrize("batch", [False, True])
def test_selection_digest_rank_and_witness_are_sealed_and_rederived(batch, tmp_path):
    plan, candidates, raw, selection, context, binding = selected_context(batch=batch)
    witness = context["selection_binding"]
    assert witness == {
        "schema_version": "cre-document-selection-binding/v1",
        "selection_sha256": events.sha256(events.encoded(selection)),
        "selection_order": "property_occurrences_first_v1",
        "selected_position": 3,
        "priority": selection["documents"][3]["priority"],
    }
    expected_context_sha256 = events.sha256(events.encoded(context))
    assert document_acquisition.validate_archived_context(
        plan, candidates, raw, selection, context, expected_context_sha256=expected_context_sha256,
        **binding) == context

    artifact = text_artifact()
    receipt = context["receipt"]
    artifact["source"].update(url=receipt["final_url"], observed_at=receipt["finished_at"],
                              sha256=receipt["body_sha256"], byte_length=receipt["body_bytes"])
    tmp_path.chmod(0o700)
    directory = tmp_path / "seal"
    marker = document_seal.write(directory, context, artifact)
    result = document_seal.read(directory, expected_context=context, expected_marker=marker)
    assert result["marker"] == marker
    for field, value in (("selection_sha256", "0" * 64), ("selected_position", 2),
                         ("selection_order", "invented_order"),
                         ("priority", {"class": "property", "witness": {"invented": True}})):
        changed = copy.deepcopy(context)
        changed["selection_binding"][field] = value
        # Even a supplied digest of the altered context cannot substitute for
        # exact policy/selection rederivation against the source plans.
        with pytest.raises(ValueError, match="disagrees with source selection"):
            document_acquisition.validate_archived_context(
                plan, candidates, raw, selection, changed,
                expected_context_sha256=events.sha256(events.encoded(changed)), **binding)
        with pytest.raises(ValueError):
            document_seal.read(directory, expected_context=changed, expected_marker=marker)


def test_legacy_context_does_not_gain_priority_metadata():
    plan, candidates, raw, selection, index, receipt, body, binding = fixture()
    context = document_acquisition.build_context(plan, candidates, raw, selection, index, receipt, body, **binding)
    assert "selection_binding" not in context
    assert "selection_order" not in selection


@pytest.mark.parametrize("failure", ["denied", "unknown"])
def test_ranked_foreground_attempt_is_sealed_or_fenced_and_never_restarted(monkeypatch, tmp_path, failure):
    from test_document_pipeline import install_workers
    from test_document_priority import CODE, PROP, prepare
    from test_document_priority import fixture as priority_plan

    from source_evidence import document_pipeline, document_recovery

    plan = priority_plan()
    candidates, raw, binding, selection = prepare(plan)
    tmp_path.chmod(0o700)
    parent = tmp_path / "source-evidence"
    parent.mkdir(mode=0o700)
    plan_bytes = events.encoded(plan)
    plan_path = parent / "transwestern-attempt-1.json"
    plan_path.write_bytes(plan_bytes)
    plan_path.chmod(0o600)
    attempt = {"number": 1, "source_evidence": {
        "path": "source-evidence/transwestern-attempt-1.json", "plan_sha256": plan["plan_sha256"],
        "plan_file_sha256": events.sha256(plan_bytes), "capture_id": plan["events"][0]["capture_id"]}}
    manifest = {"collector_git_sha": CODE, "collector_git_dirty": False,
                "sources": {"transwestern": {"attempts": [attempt]}}}
    kwargs = {"plan": plan, "candidates": candidates, "policy_raw": raw, "selection": selection, **binding}
    calls = install_workers(monkeypatch, denial=403 if failure == "denied" else None,
                            fetch_state="completed" if failure == "denied" else "fetch_outcome_unknown")
    state = document_pipeline.run(tmp_path, manifest, "transwestern", attempt, lambda *_: None,
                                  node_executable="/usr/bin/node", **kwargs)
    assert calls["fetch"] == [PROP]
    assert not calls["extract"]
    assert state["budget"]["indices"] == [1]
    assert state["budget"]["attempts"][0]["index"] == 1
    budget = copy.deepcopy(state["budget"])
    assert state["state"] == ("access_denied" if failure == "denied" else "reconciliation_required")
    with pytest.raises(ValueError, match="never automatic restart"):
        document_pipeline.run(tmp_path, manifest, "transwestern", attempt, lambda *_: None,
                              node_executable="/usr/bin/node", **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("priority recovery must not fetch or convert")

    monkeypatch.setattr(document_pipeline.doc_fetch_process, "run", forbidden)
    monkeypatch.setattr(document_pipeline.doc_extract_process, "run", forbidden)
    result = document_recovery.sealed(tmp_path, manifest, "transwestern", attempt, lambda *_: None, **kwargs)
    assert state["budget"] == budget
    assert result["new_acquisition_allowed"] is False
    if failure == "denied":
        assert result["state"] == "sealed_outputs_reconciled"
        path = tmp_path / state["documents"][0]["directory"] / "sealed" / "acquisition.json"
        context = json.loads(path.read_bytes())
        assert context["candidate_index"] == 1
        assert context["selection_binding"]["selected_position"] == 0
        assert context["selection_binding"]["priority"]["class"] == "property"
        assert context["selection_binding"]["selection_sha256"] == state["selection_sha256"]
    else:
        assert result["state"] == "incomplete"
        assert result["documents"][0]["reason"] == "missing_persisted_seal_marker"
