import copy

import pytest

from source_evidence import document_acquisition as adapter
from source_evidence import document_candidates, events
from test_document_acquisition_policy import CODE, TARGET, policy_value, preflight
from test_document_candidates import artifact, plan_for


def fixture():
    plan = plan_for(artifact())
    candidates = document_candidates.discover(plan)
    policy_raw = events.encoded(policy_value())
    selection = preflight(plan, candidates)
    binding = {"expected_policy_sha256": events.sha256(policy_raw), "target_sha256": TARGET,
               "collector_git_sha": CODE}
    chosen = selection["documents"][0]
    body = b"synthetic pdf bytes"
    url = chosen["candidate"]["url"]
    receipt = {"started_at": "2026-10-28T15:00:00.123456Z", "finished_at": "2026-10-28T15:00:01.123456Z",
               "request_url": url, "final_url": url, "http_status": 200, "hops": [{"url": url, "status": 200}],
               "outcome": "acquired", "failure_code": None, "body_sha256": events.sha256(body),
               "body_bytes": len(body), "body_representation": adapter.REPRESENTATION,
               "completion": "complete", "deadline_enforcement": "external_worker_required"}
    return plan, candidates, policy_raw, selection, chosen["candidate_index"], receipt, body, binding


def context_for(values):
    plan, candidates, policy_raw, selection, index, receipt, body, binding = values
    return adapter.build_context(plan, candidates, policy_raw, selection, index, receipt, body, **binding)


def test_context_rederives_selection_and_preserves_all_candidate_occurrences():
    values = fixture()
    context = context_for(values)
    assert context["receipt"]["finished_at"] == "2026-10-28T15:00:01.123456Z"
    assert context["candidate"] == values[3]["documents"][0]["candidate"]
    assert len(context["candidate"]["occurrences"]) == 2
    assert context["original_retention"] == "not_asserted"
    assert "synthetic pdf bytes" not in str(context)
    changed = list(values)
    changed[3] = copy.deepcopy(values[3])
    changed[3]["documents"][0]["candidate"]["url"] = "https://docs.example.test/other"
    with pytest.raises(ValueError):
        context_for(changed)


@pytest.mark.parametrize("field,value", [("body_bytes", 999), ("body_sha256", "0" * 64),
                                         ("http_status", 404), ("completion", "incomplete"),
                                         ("request_url", "https://other.example.test/a"),
                                         ("finished_at", "2026-10-28T14:59:59Z")])
def test_invalid_acquired_receipts_fail_closed(field, value):
    values = list(fixture())
    values[5] = {**values[5], field: value}
    with pytest.raises(ValueError):
        context_for(values)


def test_failure_is_retained_without_inventing_body_or_access_conclusion():
    values = list(fixture())
    values[5] = {**values[5], "final_url": None, "http_status": None, "hops": [],
                 "outcome": "failed", "failure_code": "dns_failed", "completion": "incomplete",
                 "body_sha256": None, "body_bytes": None}
    values[6] = None
    context = context_for(values)
    assert context["receipt"]["failure_code"] == "dns_failed"
    assert context["visibility"] == "unverified"
    with pytest.raises(ValueError):
        adapter.bind_extraction(context, {})


def test_extraction_requires_exact_acquired_url_hash_size_and_precise_clock():
    values = fixture()
    context = context_for(values)
    receipt = context["receipt"]
    text = "synthetic document"
    artifact = {"schema_version": "cre-document-text/v1",
                "source": {"url": receipt["final_url"], "observed_at": receipt["finished_at"],
                           "sha256": receipt["body_sha256"], "byte_length": receipt["body_bytes"]},
                "parser": {"name": "@firecrawl/anydoc", "version": "0.2.4"},
                "extraction": {"status": "extracted", "coverage": "unknown", "text": text,
                               "text_sha256": events.sha256(text.encode())}}
    binding, plan = adapter.bind_extraction(context, artifact)
    assert binding["document_plan_sha256"] == plan["plan_sha256"]
    assert binding["source_identity"] == "unverified"
    assert binding["extraction_execution"] == "caller_asserted_metadata_not_reexecuted"
    for key, value in (("url", "https://docs.example.test/wrong"), ("sha256", "0" * 64),
                       ("byte_length", 0), ("observed_at", "2026-10-28T15:00:01Z")):
        forged = copy.deepcopy(artifact)
        forged["source"][key] = value
        with pytest.raises(ValueError):
            adapter.bind_extraction(context, forged)


def test_boolean_byte_length_does_not_equal_zero_length_body():
    values = list(fixture())
    values[6] = b""
    values[5] = {**values[5], "body_bytes": 0, "body_sha256": events.sha256(b"")}
    context = context_for(values)
    receipt = context["receipt"]
    artifact = {"schema_version": "cre-document-text/v1",
                "source": {"url": receipt["final_url"], "sha256": receipt["body_sha256"],
                           "byte_length": False, "observed_at": receipt["finished_at"]}}
    with pytest.raises(ValueError):
        adapter.bind_extraction(context, artifact)


def test_connection_close_failure_is_preserved():
    values = list(fixture())
    values[5] = {**values[5], "outcome": "failed", "failure_code": "connection_close_failed",
                 "completion": "incomplete", "body_sha256": None, "body_bytes": None}
    values[6] = None
    assert context_for(values)["receipt"]["failure_code"] == "connection_close_failed"
