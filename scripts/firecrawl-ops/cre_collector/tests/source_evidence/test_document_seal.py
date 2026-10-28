import copy
import os

import pytest

from source_evidence import document_acquisition, document_seal
from test_doc_extract_process import artifact as text_artifact
from test_document_acquisition import fixture


def context_and_artifact(index=0):
    plan, candidates, policy, selection, _, receipt, body, binding = fixture()
    item = selection["documents"][index]
    url = item["candidate"]["url"]
    receipt = {**receipt, "request_url": url, "final_url": url, "hops": [{"url": url, "status": 200}]}
    context = document_acquisition.build_context(plan, candidates, policy, selection, item["candidate_index"],
                                                 receipt, body, **binding)
    artifact = text_artifact()
    artifact["source"].update(url=url, observed_at=receipt["finished_at"], sha256=receipt["body_sha256"],
                              byte_length=receipt["body_bytes"])
    return context, artifact


def test_78_distinct_contexts_prepare_with_exact_relationships():
    captures = set()
    for index in range(78):
        context, artifact = context_and_artifact(index)
        files, marker = document_seal.prepare(context, artifact)
        assert [name for name, _ in files] == ["acquisition.json", "converter-artifact.json", "artifact.json",
                                                "binding.json", "plan.json"]
        assert len(marker["files"]) == 5
        captures.add(marker["capture_id"])
    assert len(captures) == 78


def test_private_readback_rederives_marker_and_does_not_claim_original_bytes(tmp_path):
    tmp_path.chmod(0o700)
    context, artifact = context_and_artifact()
    directory = tmp_path / "seal"
    marker = document_seal.write(directory, context, artifact)
    result = document_seal.read(directory, expected_context=context, expected_marker=marker)
    assert result == {"artifact": artifact, "marker": marker, "original_bytes_reverified": False}
    wrong = copy.deepcopy(marker)
    wrong["capture_id"] = "incorrect"
    with pytest.raises(ValueError):
        document_seal.read(directory, expected_context=context, expected_marker=wrong)


def test_failed_context_has_only_acquisition_receipt(tmp_path):
    tmp_path.chmod(0o700)
    context, _ = context_and_artifact()
    context["receipt"].update(outcome="failed", completion="incomplete", failure_code="http_status",
                              body_sha256=None, body_bytes=None)
    directory = tmp_path / "failed"
    marker = document_seal.write(directory, context)
    result = document_seal.read(directory, expected_context=context, expected_marker=marker)
    assert result["artifact"] is None and set(marker["files"]) == {"acquisition.json"}


@pytest.mark.parametrize("change", ["extra", "missing", "symlink", "fifo", "modified", "permissions"])
def test_refuses_unexpected_or_changed_private_files(tmp_path, change):
    tmp_path.chmod(0o700)
    context, artifact = context_and_artifact()
    directory = tmp_path / "seal"
    marker = document_seal.write(directory, context, artifact)
    path = directory / "artifact.json"
    if change == "extra":
        (directory / "unexpected").write_text("synthetic")
    elif change == "missing":
        path.unlink()
    elif change == "symlink":
        path.unlink()
        path.symlink_to(directory / "plan.json")
    elif change == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif change == "modified":
        path.write_bytes(b"{}")
    else:
        path.chmod(0o644)
    with pytest.raises((ValueError, OSError)):
        document_seal.read(directory, expected_context=context, expected_marker=marker)


def test_context_or_converter_binding_cannot_be_swapped(tmp_path):
    tmp_path.chmod(0o700)
    context, artifact = context_and_artifact()
    directory = tmp_path / "seal"
    marker = document_seal.write(directory, context, artifact)
    other, _ = context_and_artifact(1)
    with pytest.raises(ValueError):
        document_seal.read(directory, expected_context=other, expected_marker=marker)
    artifact["source"]["byte_length"] = False
    with pytest.raises(ValueError):
        document_seal.prepare(context, artifact)
