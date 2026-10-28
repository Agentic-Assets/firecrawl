import copy

import pytest
from source_evidence import document_batch, events
from test_document_acquisition_policy import CODE, TARGET, policy_value
from test_document_candidates import artifact, plan_for


def plans_fixture():
    plans = []
    for page in range(3):
        value = artifact()
        value["runMeta"]["page"] = page
        value["sources"][0]["retainedObservations"] = []
        value["listings"] = [{"sourceKey": "svn", "id": f"listing-{page}-{i}",
                              "brochures": [{"url": f"https://docs.example.test/{page}-{i}",
                                             "name": f"Page {page} item {i}"}]}
                             for i in range(30)]
        # Later-page witnesses occur past the local two-document cap used below.
        value["listings"][0 if page == 0 else -1]["documents"] = [
            {"url": "https://docs.example.test/shared", "name": f"label-{page}"}]
        plans.append(plan_for(value))
    return plans


def select(plans, candidates, settings=None):
    raw = events.encoded(settings or policy_value())
    return document_batch.prepare(plans, candidates, raw, expected_policy_sha256=events.sha256(raw),
                                  target_sha256=TARGET, collector_git_sha=CODE)


def test_many_captures_group_exact_url_without_losing_occurrences():
    plans = plans_fixture()
    original = copy.deepcopy(plans)
    candidates = document_batch.discover(plans)
    assert plans == original
    assert len(candidates["candidates"]) == 91
    shared = next(item for item in candidates["candidates"] if item["url"] == "https://docs.example.test/shared")
    assert [item["labels"]["name"] for item in shared["occurrences"]] == ["label-0", "label-1", "label-2"]
    assert [item["plan_sha256"] for item in shared["occurrences"]] == [plan["plan_sha256"] for plan in plans]
    selection = select(plans, candidates)
    assert len(selection["documents"]) == 91
    assert "capture_id" not in selection and "source_evidence_plan_sha256" not in selection
    assert selection["source_evidence_plans"] == candidates["source_evidence_plans"]
    assert selection["candidate_manifest_sha256"] == events.sha256(events.encoded(candidates))


def test_global_cap_keeps_later_page_occurrences_on_selected_url():
    plans = plans_fixture()
    candidates = document_batch.discover(plans)
    settings = policy_value()
    settings["limits"]["max_documents"] = 2
    selection = select(plans, candidates, settings)
    selected_shared = next(item for item in selection["documents"] if item["candidate"]["url"].endswith("/shared"))
    assert len(selected_shared["candidate"]["occurrences"]) == 3
    assert any(item["reason"] == "document_count_limit" for item in selection["exclusions"])


def test_relative_occurrence_requires_absolute_witness_anywhere_in_batch():
    values = []
    for page in range(2):
        value = artifact()
        value["runMeta"]["page"] = page
        value["listings"] = [{"sourceKey": "svn", "id": str(page)}]
        page_context = {"schema": "cre-source-page-context/v1", "sourceUrl": "https://docs.example.test/property/",
                        "omissions": {"characters": 0},
                        "links": [{"href": "../shared", "text": "Brochure"}]}
        value["sources"][0]["retainedObservations"] = [{"pageContext": page_context}]
        if page == 1:
            value["listings"][0]["documents"] = ["https://docs.example.test/shared"]
        values.append(plan_for(value))
    relative_only = document_batch.discover(values[:1])
    assert select(values[:1], relative_only)["exclusions"][0]["reason"] == "relative_only"
    combined = document_batch.discover(values)
    selection = select(values, combined)
    assert len(selection["documents"]) == 1
    assert len(selection["documents"][0]["candidate"]["occurrences"]) == 3


def test_duplicate_capture_manifest_tampering_and_bounds_refused(monkeypatch):
    plans = plans_fixture()
    with pytest.raises(ValueError):
        document_batch.discover([plans[0], plans[0]])
    candidates = document_batch.discover(plans)
    tampered = copy.deepcopy(candidates)
    tampered["candidates"][0]["occurrences"][0]["labels"]["name"] = "changed"
    with pytest.raises(ValueError):
        select(plans, tampered)
    monkeypatch.setattr(document_batch, "MAX_OCCURRENCES", 1)
    with pytest.raises(ValueError, match="bound"):
        document_batch.discover(plans)
