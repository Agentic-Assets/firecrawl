import copy
import json

import pytest
from source_evidence import document_acquisition_policy as policy
from source_evidence import document_candidates, events
from test_document_candidates import artifact, plan_for


CODE = "a" * 40
TARGET = "b" * 64


def policy_value():
    return {"schema_version": policy.SCHEMA, "purpose": "private_public_listing_document_text",
            "data_scope": "public_listing_source", "collector_git_sha": CODE, "target_sha256": TARGET,
            "source_keys": ["svn"], "allowed_hosts": {"svn": ["docs.example.test", "example.test"]},
            "limits": {"max_documents": 100, "max_document_bytes": 1024, "max_total_bytes": 2048,
                       "document_timeout_seconds": 30, "run_timeout_seconds": 120, "max_redirects": 2},
            "original_retention": "temporary_only"}


def preflight(plan, candidates, value=None, raw=None, **bindings):
    raw = raw if raw is not None else events.encoded(value or policy_value())
    return policy.prepare(plan, candidates, raw, expected_policy_sha256=bindings.get("sha", events.sha256(raw)),
                          target_sha256=bindings.get("target", TARGET), collector_git_sha=bindings.get("code", CODE))


def test_v2_complement_requires_bounded_full_phase_and_absolute_parser():
    plan = plan_for(artifact())
    candidates = document_candidates.discover(plan)
    value = policy_value()
    value.update(schema_version=policy.SCHEMA_V2,
                 pdf_complement={'mode': 'poppler_pdf_text_v1', 'pdftotext_path': '/usr/bin/pdftotext'})
    with pytest.raises(ValueError):
        preflight(plan, candidates, value=value)
    value['limits']['run_timeout_seconds'] = 3600
    selected = preflight(plan, candidates, value=value)
    assert selected['pdf_complement'] == value['pdf_complement']
    from source_evidence import document_budget
    with pytest.raises(ValueError):
        document_budget.reserve(document_budget.initial(selected), selected,
                                selected['documents'][0]['candidate_index'], elapsed_seconds=0, phase_seconds=1)
    value['pdf_complement']['pdftotext_path'] = 'pdftotext'
    with pytest.raises(ValueError):
        preflight(plan, candidates, value=value)


def test_many_documents_preserve_candidates_and_explain_relative_only():
    plan = plan_for(artifact())
    candidates = document_candidates.discover(plan)
    result = preflight(plan, candidates)
    assert len(result["documents"]) == 78
    assert result["network_execution"] == "not_performed"
    assert result["destination_verification"] == "pending_each_request"
    assert result["cumulative_byte_budget"] == "not_verified_before_acquisition"
    assert result["exclusions"] == [{"candidate_index": 78, "reason": "relative_only"}]
    for document in result["documents"]:
        original = candidates["candidates"][document["candidate_index"]]
        assert document["candidate"] == original
        assert document["candidate_sha256"] == events.sha256(events.encoded(original))
    before = copy.deepcopy(candidates)
    result["documents"][0]["candidate"]["occurrences"][0]["labels"]["name"] = "later worker state"
    assert candidates == before


def test_manifest_tampering_and_bindings_refused():
    plan = plan_for(artifact())
    candidates = document_candidates.discover(plan)
    modified = copy.deepcopy(candidates)
    modified["candidates"][0]["occurrences"][0]["labels"]["name"] = "changed"
    with pytest.raises(ValueError):
        preflight(plan, modified)
    for bindings in ({"sha": "0" * 64}, {"target": "0" * 64}, {"code": "0" * 40}):
        with pytest.raises(ValueError):
            preflight(plan, candidates, **bindings)


@pytest.mark.parametrize("change", [
    {"source_keys": ["unknown"]}, {"source_keys": ["jll"]},
    {"source_keys": ["svn", "svn"]}, {"original_retention": "forever"},
    {"allowed_hosts": {"svn": ["*.example.test"]}},
    {"allowed_hosts": {"svn": ["EXAMPLE.test"]}},
    {"allowed_hosts": {"svn": ["example.test."]}},
    {"allowed_hosts": {"svn": ["127.0.0.1"]}},
    {"allowed_hosts": {"svn": ["localhost"]}},
    {"limits": {"max_documents": True}},
])
def test_policy_refuses_unknown_scope_or_bad_fields(change):
    plan = plan_for(artifact())
    candidates = document_candidates.discover(plan)
    value = policy_value()
    value.update(change)
    with pytest.raises(ValueError):
        preflight(plan, candidates, value)


def test_duplicate_policy_json_and_exceptional_numbers_refused():
    plan = plan_for(artifact())
    candidates = document_candidates.discover(plan)
    raw = b'{"schema_version":"x","schema_version":"y"}'
    with pytest.raises(ValueError):
        preflight(plan, candidates, raw=raw)
    raw = json.dumps(policy_value()).replace('"max_documents": 100', '"max_documents": 1e999').encode()
    with pytest.raises(ValueError):
        preflight(plan, candidates, raw=raw)


def test_explicit_exclusions_and_original_retention():
    value = artifact()
    value["listings"][0]["brochures"][0]["url"] = "http://docs.example.test/a"
    value["listings"][1]["brochures"][0]["url"] = "https://docs.example.test:444/b"
    value["listings"][2]["brochures"][0]["url"] = "https://other.example.test/c"
    plan = plan_for(value)
    candidates = document_candidates.discover(plan)
    settings = policy_value()
    settings["limits"]["max_documents"] = 1
    settings["original_retention"] = "keep_unresolved_until_review"
    result = preflight(plan, candidates, settings)
    assert len(result["documents"]) == 1
    assert {entry["reason"] for entry in result["exclusions"]} >= {
        "nondefault_port", "host_not_allowed", "relative_only", "document_count_limit"}
    assert result["documents"][0]["candidate"]["url"] == "http://docs.example.test/a"
    assert result["original_retention"] == "keep_unresolved_until_review"


@pytest.mark.parametrize('strategy,max_pages,valid', [
    ('no_text', 1, True), ('all_pages', 1000, True), ('unknown', 1, False),
    ('no_text', 0, False), ('no_text', 1001, False), ('no_text', True, False),
])
def test_v3_sparse_ocr_has_explicit_strategy_page_bound_and_phase(strategy, max_pages, valid):
    plan = plan_for(artifact())
    candidates = document_candidates.discover(plan)
    value = policy_value()
    value.update(schema_version=policy.SCHEMA_V3,
                 pdf_complement={'mode': 'poppler_pdf_text_v1', 'pdftotext_path': '/usr/bin/pdftotext'},
                 sparse_ocr={'mode': 'sparse_ocr_v1', 'strategy': strategy, 'max_pages': max_pages})
    value['limits']['run_timeout_seconds'] = 3600
    if valid:
        assert preflight(plan, candidates, value=value)['sparse_ocr'] == value['sparse_ocr']
        value['limits']['run_timeout_seconds'] = 300
    with pytest.raises(ValueError):
        preflight(plan, candidates, value=value)
