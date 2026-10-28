"""JSON-LD graph identifiers are metadata, not fetch targets."""
import json

import pytest
from source_evidence import document_candidates, events
from test_document_candidates import artifact, plan_for


def script_plan(document):
    value = artifact()
    value["listings"] = []
    page = value["sources"][0]["retainedObservations"][0]["pageContext"]
    page["links"] = []
    page["documentLinks"] = []
    page["scripts"] = [{"type": "application/ld+json", "text": json.dumps(document)}]
    return plan_for(value)


def test_graph_id_fragment_does_not_block_valid_document():
    plan = script_plan({"@id": "https://broker.example/#organization",
                        "urlDocument": "https://docs.example.test/file.pdf"})
    events.validate_content_policy(plan)
    result = document_candidates.discover(plan)
    assert [candidate["url"] for candidate in result["candidates"]] == ["https://docs.example.test/file.pdf"]
    assert result["candidates"][0]["occurrences"][0]["script_json_pointer"] == "/urlDocument"


def test_candidate_fragment_is_still_excluded():
    plan = script_plan({"@id": "https://broker.example/#organization",
                        "urlDocument": "https://docs.example.test/file.pdf#page=1"})
    result = document_candidates.discover(plan)
    assert result["candidates"] == []
    assert result["excluded"]


def test_identity_witness_fragment_does_not_block_valid_document():
    plan = script_plan({"url": "https://broker.example/#office",
                        "urlDocument": "https://docs.example.test/file.pdf"})
    result = document_candidates.discover(plan)
    assert [candidate["url"] for candidate in result["candidates"]] == ["https://docs.example.test/file.pdf"]


def test_empty_candidate_fragment_is_still_excluded():
    plan = script_plan({"urlDocument": "https://docs.example.test/file.pdf#"})
    result = document_candidates.discover(plan)
    assert result["candidates"] == []
    assert result["excluded"]


def test_escaped_protected_url_in_metadata_remains_refused():
    value = artifact()
    value["listings"] = []
    page = value["sources"][0]["retainedObservations"][0]["pageContext"]
    page["scripts"] = [{"type": "application/json", "text":
                        '{"@id":"https:\\/\\/jll.com/#organization","urlDocument":"https://docs.example.test/file.pdf"}'}]
    plan = plan_for(value)
    with pytest.raises(ValueError, match="JLL URL"):
        document_candidates.discover(plan)
