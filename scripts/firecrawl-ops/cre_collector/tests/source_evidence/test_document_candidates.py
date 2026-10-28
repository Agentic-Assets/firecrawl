import copy

import pytest
from source_evidence import collector, document_candidates, events


def artifact():
    listings = [{"sourceKey": "svn", "id": f"property-{i}", "url": f"https://example.test/property/{i}",
                 "brochures": [{"url": f"https://docs.example.test/file-{i}", "name": f"Brochure {i}"}]}
                for i in range(78)]
    listings[1]["documents"] = [{"url": "https://docs.example.test/file-0", "title": "Conflicting label", "docType": "om"}]
    held = {"event": "foundry_held_observation", "observation": {"id": "held-no-address", "status": None},
            "pageContext": {"schema": "cre-source-page-context/v1", "sourceUrl": "https://example.test/property/held/",
                            "links": [{"href": "../download", "text": "Offering memorandum"},
                                      {"href": "/contact", "text": "Contact"}],
                            "documentLinks": [{"rel": "canonical", "href": "https://example.test/canonical"}]}}
    return {"runMeta": {"finishedAt": "2026-10-28T12:00:00Z"},
            "sources": [{"sourceKey": "svn", "retainedObservations": [held]}], "listings": listings}


def plan_for(value):
    return events.plan(collector.convert(value, source_key="svn"), source_key="svn")


def test_discovers_distinct_urls_and_preserves_all_duplicate_relationships():
    value = artifact()
    plan = plan_for(value)
    before = copy.deepcopy(plan)
    result = document_candidates.discover(plan)
    assert plan == before
    assert len(result["candidates"]) == 79
    duplicate = next(candidate for candidate in result["candidates"] if candidate["url"] == "https://docs.example.test/file-0")
    assert len(duplicate["occurrences"]) == 2
    assert duplicate["occurrences"][0]["labels"]["name"] == "Brochure 0"
    assert duplicate["occurrences"][1]["labels"]["title"] == "Conflicting label"
    assert duplicate["occurrences"][1]["labels"]["docType"] == "om"
    assert duplicate["fetch_authority"] is False and duplicate["possible_duplicate_bytes"] == "unverified"
    relative = next(candidate for candidate in result["candidates"] if candidate["url"] == "https://example.test/property/download")
    occurrence = relative["occurrences"][0]
    assert occurrence["raw_reference"] == "../download"
    assert occurrence["resolution"]["verification"] == "unverified"
    assert any(witness["fields"].get("id") == "held-no-address" for witness in occurrence["identity_witnesses"])
    assert not any(candidate["url"].endswith("/contact") or candidate["url"].endswith("/canonical")
                   for candidate in result["candidates"])


def test_native_document_fields_and_typed_links_do_not_require_address():
    value = artifact()
    value["listings"] = [{"sourceKey": "svn", "id": None,
                           "pdf_url": ["https://docs.example.test/a", "https://docs.example.test/b"],
                           "urlDocument": "https://docs.example.test/c",
                           "links": [{"url": "https://docs.example.test/d", "linkType": "document"},
                                     {"url": "https://docs.example.test/not-document", "linkType": "other"}]}]
    value["sources"][0]["retainedObservations"] = []
    result = document_candidates.discover(plan_for(value))
    assert {candidate["url"] for candidate in result["candidates"]} == {
        "https://docs.example.test/a", "https://docs.example.test/b",
        "https://docs.example.test/c", "https://docs.example.test/d"}
    assert all(candidate["publicness"] == "unverified" for candidate in result["candidates"])


def test_relative_without_base_and_credential_key_are_redacted_exclusions():
    value = artifact()
    value["sources"][0]["retainedObservations"] = []
    value["listings"] = [{"sourceKey": "svn", "brochures": [
        {"url": "relative.pdf"}, {"url": "https://docs.example.test/file?key=PRIVATE_VALUE"}]}]
    result = document_candidates.discover(plan_for(value))
    assert result["candidates"] == [] and len(result["excluded"]) == 2
    assert "PRIVATE_VALUE" not in str(result["excluded"])
    assert all(set(entry) == {"item_no", "json_pointer", "reason"} for entry in result["excluded"])


def test_protected_source_and_credential_plan_refuse_whole_capture():
    value = artifact()
    value["listings"][0]["brochures"][0]["url"] = "https://jll.com/private"
    with pytest.raises(ValueError):
        document_candidates.discover(plan_for(value))
    value = artifact()
    value["listings"][0]["authorization_header"] = "placeholder"
    with pytest.raises(ValueError):
        document_candidates.discover(plan_for(value))


def test_output_bounds_fail_without_truncation(monkeypatch):
    plan = plan_for(artifact())
    monkeypatch.setattr(document_candidates, "MAX_OCCURRENCES", 2)
    with pytest.raises(ValueError, match="bound"):
        document_candidates.discover(plan)


def test_requested_url_base_and_ambiguous_base():
    value = artifact()
    page = value["sources"][0]["retainedObservations"][0]["pageContext"]
    page["requestedURL"] = page.pop("sourceUrl")
    assert any(c["url"] == "https://example.test/property/download"
               for c in document_candidates.discover(plan_for(value))["candidates"])
    page["sourceUrl"] = "https://another.example.test/"
    result = document_candidates.discover(plan_for(value))
    assert not any(c["url"].endswith("/download") for c in result["candidates"])
    assert result["excluded"]


def test_credential_url_in_copied_label_or_identity_refuses():
    value = artifact()
    value["listings"][0]["brochures"][0]["name"] = "Download https://example.test/file?key=PRIVATE_VALUE"
    with pytest.raises(ValueError):
        document_candidates.discover(plan_for(value))
    value = artifact()
    value["listings"][0]["url"] = "https://example.test/property?key=PRIVATE_VALUE"
    with pytest.raises(ValueError):
        document_candidates.discover(plan_for(value))


def test_unknown_structured_schema_fields_do_not_block_document_discovery():
    value = artifact()
    value['listings'][0]['schema'] = {'unknownProviderVersion': [False, None, []]}
    result = document_candidates.discover(plan_for(value))
    assert len(result['candidates']) == 79
    assert result['coverage'] == 'unverified' and result['acquisition_enabled'] is False


@pytest.mark.parametrize('label', ['Risk Questionnaires', 'View Documents', 'Offering Memoranda', 'Floor Plans'])
def test_source_labelled_plural_document_roles_are_candidates(label):
    value = artifact()
    value['listings'] = []
    page = value['sources'][0]['retainedObservations'][0]['pageContext']
    page['links'] = [{'href': '../download', 'text': label}]
    result = document_candidates.discover(plan_for(value))
    assert len(result['candidates']) == 1
    assert result['candidates'][0]['occurrences'][0]['labels']['text'] == label
    assert result['candidates'][0]['fetch_authority'] is False
