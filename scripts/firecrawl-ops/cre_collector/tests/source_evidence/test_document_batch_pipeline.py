"""Synthetic multi-page foreground cases; workers are mocks, not providers."""

import copy
import json

import pytest

from source_evidence import document_batch, document_pipeline, document_recovery, events
from test_document_acquisition_policy import CODE, TARGET, policy_value
from test_document_candidates import artifact, plan_for
from test_document_pipeline import execute, install_workers


def batch_fixture(tmp_path, *, total=78000):
    tmp_path.chmod(0o700)
    base = artifact()
    plans = []
    for page in range(3):
        value = copy.deepcopy(base)
        value["sources"][0]["retainedObservations"] = []
        value["listings"] = value["listings"][page * 26:(page + 1) * 26]
        value["runMeta"]["finishedAt"] = f"2026-10-28T12:0{page}:00Z"
        if page:
            value["listings"][0]["documents"] = [{"url": "https://docs.example.test/file-0",
                                                    "title": f"Page {page} witness"}]
        plans.append(plan_for(value))
    candidates = document_batch.discover(plans)
    policy = policy_value()
    policy["limits"].update(max_document_bytes=9, max_total_bytes=total, run_timeout_seconds=3600)
    raw = events.encoded(policy)
    selection = document_batch.prepare(plans, candidates, raw, expected_policy_sha256=events.sha256(raw),
                                       target_sha256=TARGET, collector_git_sha=CODE)
    parent = tmp_path / "source-evidence"
    parent.mkdir(mode=0o700)
    directory = parent / "native-svn-attempt-1"
    directory.mkdir(mode=0o700)
    pages = []
    for number, plan in enumerate(plans):
        name = f"page-{number}.json"
        payload = events.encoded(plan)
        path = directory / (name + ".plan")
        path.write_bytes(payload)
        path.chmod(0o600)
        pages.append({"filename": name, "plan_name": name + ".plan", "plan_sha256": plan["plan_sha256"],
                      "plan_file_sha256": events.sha256(payload), "capture_id": plan["events"][0]["capture_id"]})
    attempt = {"number": 1, "native_source_evidence": {"directory": "source-evidence/native-svn-attempt-1",
                                                        "pages": pages}}
    manifest = {"collector_git_sha": CODE, "collector_git_dirty": False,
                "sources": {"svn": {"attempts": [attempt]}}}
    kwargs = {"plan": plans, "candidates": candidates, "policy_raw": raw, "selection": selection,
              "expected_policy_sha256": events.sha256(raw), "target_sha256": TARGET,
              "collector_git_sha": CODE, "node_executable": "/usr/bin/node"}
    return manifest, attempt, kwargs


def test_three_pages_share_one_fetch_and_preserve_all_occurrence_origins(monkeypatch, tmp_path):
    manifest, attempt, kwargs = batch_fixture(tmp_path)
    calls = install_workers(monkeypatch)
    result = execute(tmp_path, manifest, attempt, kwargs)
    assert result["state"] == "selection_settled" and len(result["documents"]) == 78
    assert len(calls["fetch"]) == len(set(calls["fetch"])) == 78
    shared = next(item for item in kwargs["selection"]["documents"]
                  if item["candidate"]["url"] == "https://docs.example.test/file-0")
    occurrences = shared["candidate"]["occurrences"]
    assert len({item["capture_id"] for item in occurrences}) == 3
    assert len({item["plan_sha256"] for item in occurrences}) == 3
    assert len({item["observed_at"] for item in occurrences}) == 3
    document = next(item for item in result["documents"] if item["candidate_index"] == shared["candidate_index"])
    sealed = tmp_path / document["directory"] / "sealed" / "plan.json"
    value = json.loads(sealed.read_bytes())["events"][0]["facts"][0]["value"]
    assert value["acquisition_context"]["candidate"]["occurrences"] == occurrences
    assert value["acquisition_context"]["schema_version"] == "cre-document-acquisition-context/v2"
    assert all(item["identity"] == "unverified" for item in occurrences)
    before = copy.deepcopy(result["budget"])
    def forbidden(*args, **kwargs):
        pytest.fail("batch recovery must not fetch or convert")
    monkeypatch.setattr(document_pipeline.doc_fetch_process, "run", forbidden)
    monkeypatch.setattr(document_pipeline.doc_extract_process, "run", forbidden)
    args = {key: value for key, value in kwargs.items() if key != "node_executable"}
    recovered = document_recovery.sealed(tmp_path, manifest, "svn", attempt, lambda *_: None, **args)
    assert recovered["state"] == "sealed_outputs_reconciled"
    assert result["budget"] == before and recovered["new_acquisition_allowed"] is False


@pytest.mark.parametrize("kind", ["denial", "unknown", "budget"])
def test_global_stop_applies_to_whole_batch(monkeypatch, tmp_path, kind):
    manifest, attempt, kwargs = batch_fixture(tmp_path, total=10 if kind == "budget" else 78000)
    calls = install_workers(monkeypatch, denial=403 if kind == "denial" else None,
                            fetch_state="fetch_outcome_unknown" if kind == "unknown" else "completed")
    result = execute(tmp_path, manifest, attempt, kwargs)
    expected = {"denial": "access_denied", "unknown": "reconciliation_required", "budget": "budget_exhausted"}
    assert result["state"] == expected[kind] and len(calls["fetch"]) == 1
    assert len(result["budget"]["attempts"]) == 1
