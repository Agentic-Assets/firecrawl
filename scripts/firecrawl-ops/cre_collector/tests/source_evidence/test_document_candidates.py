import copy

import pytest
from source_evidence import collector, document_candidates, events


def artifact():
    listings = [{"sourceKey": "svn", "id": f"property-{i}", "url": f"https://example.test/property/{i}",
                 "brochures": [{"url": f"https://docs.example.test/file-{i}", "name": f"Brochure {i}"}]}
                for i in range(78)]
    listings[1]["documents"] = [{"url": "https://docs.example.test/file-0", "title": "Conflicting label", "docType": "om"}]
    held = {"event": "foundry_held_observation", "observation": {"id": "held-no-address", "status": None},
            "pageContext": {"schema": "cre-source-page-context/v1", "sourceUrl": "https://example.test/property/held/", "omissions": {"characters": 0},
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


def test_generic_page_and_scraped_fields_retain_72_mixed_document_witnesses():
    value = artifact()
    value["listings"] = []
    page = {"schema": "cre-source-page-context/v1", "sourceUrl": "https://example.test/page/", "omissions": {"characters": 0},
            "links": [], "documentLinks": []}
    scraped = {"links": [], "attributes": [{"selector": "a.download", "attribute": "href", "values": []}]}
    for index in range(18):
        page["links"].append({"href": f"https://docs.example.test/anchor-{index}",
                              "text": "", "aria-label": "Offering memorandum", "download": None, "type": None})
        page["documentLinks"].append({"href": f"https://docs.example.test/meta-{index}",
                                      "rel": "alternate", "type": "application/pdf", "title": None})
        scraped["links"].append(f"https://docs.example.test/link-{index}.PDF?view=1")
        scraped["attributes"][0]["values"].append(f"https://docs.example.test/attribute-{index}.xlsx")
    page["links"].append({"href": "https://docs.example.test/anchor-0", "text": "Brochure", "download": ""})
    page["links"].append({"href": "relative.pdf", "text": "", "download": "", "type": None})
    page["links"].append({"href": "https://docs.example.test/contact", "text": "Contact", "download": None})
    value["sources"][0]["retainedObservations"] = [{
        "event": "generic_enrichment_observation", "schema": "cre-generic-enrichment-observation/v1",
        "claim": {"sourceKey": "svn", "externalId": "synthetic", "url": "https://example.test/claim",
                  "transaction": "sale", "identity": "unverified"},
        "pageContext": page, "scrapedDoc": scraped}]
    result = document_candidates.discover(plan_for(value))
    assert len(result["candidates"]) == 73
    duplicate = next(candidate for candidate in result["candidates"] if candidate["url"].endswith("/anchor-0"))
    assert len(duplicate["occurrences"]) == 2
    assert duplicate["occurrences"][0]["labels"]["aria-label"] == "Offering memorandum"
    assert any(witness["fields"].get("externalId") == "synthetic" and witness["fields"].get("transaction") == "sale"
               and witness["json_pointer"].endswith("/claim")
               for witness in duplicate["occurrences"][0]["identity_witnesses"])
    attribute = next(candidate for candidate in result["candidates"] if candidate["url"].endswith("attribute-0.xlsx"))
    assert attribute["occurrences"][0]["labels"] == {"selector": "a.download", "attribute": "href"}
    relative = next(candidate for candidate in result["candidates"] if candidate["url"].endswith("/page/relative.pdf"))
    assert relative["occurrences"][0]["resolution"]["base"] == "https://example.test/page/"
    assert relative["occurrences"][0]["resolution"]["verification"] == "unverified"
    assert not any(candidate["url"].endswith("/contact") for candidate in result["candidates"])
    assert all(candidate["fetch_authority"] is False for candidate in result["candidates"])
    from test_document_acquisition_policy import preflight
    selection = preflight(plan_for(value), result)
    relative_index = result["candidates"].index(relative)
    assert {"candidate_index": relative_index, "reason": "relative_only"} in selection["exclusions"]


def test_document_signals_preserve_labels_and_do_not_promote_preview():
    value = artifact()
    value["listings"] = [{"sourceKey": "svn", "documentPreview": "https://docs.example.test/preview.jpg",
                           "links": [{"href": "https://docs.example.test/download", "download": ""},
                                     {"url": "https://docs.example.test/mime", "type": "application/pdf; charset=binary"},
                                     {"url": "https://docs.example.test/photo.png", "type": "image/png"}],
                           "attributes": [{"selector": "a.download", "attribute": "href",
                                           "values": ["https://docs.example.test/not-evidenced"]}]}]
    value["sources"][0]["retainedObservations"] = []
    result = document_candidates.discover(plan_for(value))
    assert {candidate["url"] for candidate in result["candidates"]} == {
        "https://docs.example.test/download", "https://docs.example.test/mime"}
    assert result["candidates"][0]["occurrences"][0]["labels"]["download"] == ""


def test_buildout_requested_page_and_claim_remain_distinct_witnesses():
    value = artifact()
    value["listings"] = []
    value["sources"][0]["retainedObservations"] = [{
        "schema": "cre-buildout-enrichment-observation/v1",
        "capturedAt": "2026-10-28T11:00:00Z", "captureClock": "collector_after_scrape_return",
        "qualification": "rejected_missing_detail_shell",
        "claim": {"sourceKey": "svn", "externalId": "synthetic", "url": "https://example.test/claim",
                  "transaction": "sale"},
        "requestedPageUrl": "https://example.test/iframe/page/",
        "pageContext": {"schema": "cre-source-page-context/v1", "sourceUrl": "https://example.test/iframe/page/", "omissions": {"characters": 0},
                        "detailObservedAt": "2026-10-28T11:00:00Z",
                        "links": [{"href": "relative.pdf", "text": ""}]},
        "scrapedDoc": {"links": ["../scraped.pdf"], "attributes": [
            {"selector": "a", "attribute": "href", "values": ["file.xlsx"]},
            {"selector": "a", "attribute": "price", "values": ["wrong.pdf"]}]},
        "unknown": {"attributes": [{"selector": "a", "attribute": "href", "values": ["other.pdf"]}]}}]
    result = document_candidates.discover(plan_for(value))
    candidate = next(item for item in result["candidates"] if item["url"].endswith("/page/relative.pdf"))
    assert candidate["url"] == "https://example.test/iframe/page/relative.pdf"
    occurrence = candidate["occurrences"][0]
    assert occurrence["resolution"]["base"] == "https://example.test/iframe/page/"
    assert any(item["fields"].get("externalId") == "synthetic" for item in occurrence["identity_witnesses"])
    assert any(item["fields"].get("requestedPageUrl") == "https://example.test/iframe/page/"
               for item in occurrence["identity_witnesses"])
    assert {item["url"] for item in result["candidates"]} == {
        "https://example.test/iframe/page/relative.pdf", "https://example.test/iframe/scraped.pdf",
        "https://example.test/iframe/page/file.xlsx"}
    scraped = next(item for item in result["candidates"] if item["url"].endswith("/scraped.pdf"))
    witnesses = scraped["occurrences"][0]["identity_witnesses"]
    assert any(item["fields"].get("detailObservedAt") == "2026-10-28T11:00:00Z"
               and item["json_pointer"].endswith("/pageContext") for item in witnesses)
    assert any(item["fields"].get("qualification") == "rejected_missing_detail_shell"
               and item["fields"].get("captureClock") == "collector_after_scrape_return" for item in witnesses)


def test_conflicting_sibling_page_base_does_not_fall_back_to_claim_url():
    value = artifact()
    value["listings"] = []
    value["sources"][0]["retainedObservations"] = [{
        "schema": "cre-buildout-enrichment-observation/v1",
        "claim": {"sourceKey": "svn", "url": "https://example.test/claim/"},
        "requestedPageUrl": "https://example.test/iframe/",
        "pageContext": {"schema": "cre-source-page-context/v1", "sourceUrl": "https://example.test/iframe/",
                        "requestedUrl": "https://example.test/different/"},
        "scrapedDoc": {"links": ["relative.pdf"], "attributes": [
            {"selector": "a", "attribute": "href", "values": ["relative.xlsx"]}]}}]
    result = document_candidates.discover(plan_for(value))
    assert result["candidates"] == [] and len(result["excluded"]) == 2


def test_unrelated_native_attributes_never_become_scraped_attribute_candidates():
    value = artifact()
    value["sources"][0]["retainedObservations"] = []
    value["listings"] = [{"sourceKey": "svn", "attributes": [
        {"selector": "a", "attribute": "href", "values": ["https://docs.example.test/unrelated.pdf"]}]}]
    assert document_candidates.discover(plan_for(value))["candidates"] == []


def test_complete_json_script_preserves_real_source_pointer_and_inner_pointer():
    value = artifact()
    value["listings"] = []
    page = value["sources"][0]["retainedObservations"][0]["pageContext"]
    page["links"] = []
    page["scripts"] = [{"type": "application/ld+json", "text": __import__("json").dumps({"id": "script-witness",
        "documents": [{"url": f"https://docs.example.test/{i}.pdf", "title": f"Source {i}"} for i in range(60)]})}]
    result = document_candidates.discover(plan_for(value))
    assert len(result["candidates"]) == 60
    first = result["candidates"][0]["occurrences"][0]
    assert first["json_pointer"].endswith("/pageContext/scripts/0/text")
    assert first["script_json_pointer"] == "/documents/0/url"
    assert first["labels"]["title"] == "Source 0"
    assert first["identity"] == "unverified"
    assert any(witness.get("script_json_pointer") == "" and witness["json_pointer"] == first["json_pointer"]
               and witness["fields"].get("id") == "script-witness" for witness in first["identity_witnesses"])


def test_truncated_and_unverified_links_and_scripts_are_diagnostic_not_targets():
    value = artifact()
    value["listings"] = []
    page = value["sources"][0]["retainedObservations"][0]["pageContext"]
    page["omissions"] = {"characters": 12}
    page["documentLinks"] = []
    page["links"] = [
        {"href": None, "text": "Brochure", "referenceIntegrity": {"href": "truncated"},
         "referencePrefix": "https://docs.example.test/wrong.pdf"},
        {"href": "https://docs.example.test/legacy.pdf", "text": "Brochure"},
        {"href": "https://docs.example.test/complete.pdf", "text": "Brochure",
         "referenceIntegrity": {"href": "complete"}},
    ]
    page["scripts"] = [
        {"type": "application/json", "text": '{"urlDocument":"https://docs.example.test/truncated.pdf"}', "textComplete": False},
        {"type": "application/json", "text": '{"urlDocument":"https://docs.example.test/legacy.pdf"}'},
        {"type": "application/json", "text": '{"urlDocument":"https://docs.example.test/script.pdf"}', "textComplete": True},
        {"type": "application/json", "text": '{"duplicate":1,"duplicate":2}', "textComplete": True},
    ]
    result = document_candidates.discover(plan_for(value))
    assert {item["url"] for item in result["candidates"]} == {
        "https://docs.example.test/complete.pdf", "https://docs.example.test/script.pdf"}
    assert [entry["reason"] for entry in result["excluded"]].count("reference_incomplete_or_unverified") == 2
    assert [entry["reason"] for entry in result["excluded"]].count("script_text_incomplete_or_unverified") == 2
    assert [entry["reason"] for entry in result["excluded"]].count("script_json_invalid_or_unsupported") == 1


def test_decoded_json_script_cannot_bypass_protected_source_policy():
    value = artifact()
    value["listings"] = []
    page = value["sources"][0]["retainedObservations"][0]["pageContext"]
    page["scripts"] = [{"type": "application/json",
                        "text": '{"urlDocument":"https:\\/\\/jll.com\\/document.pdf"}'}]
    with pytest.raises(ValueError, match="JLL URL"):
        document_candidates.discover(plan_for(value))


def test_script_exceptional_numeric_literals_do_not_hide_document_url():
    value = artifact()
    value["listings"] = []
    page = value["sources"][0]["retainedObservations"][0]["pageContext"]
    page["links"] = []
    page["documentLinks"] = []
    page["scripts"] = [{"type": "application/json",
                        "text": '{"id":1.234567890123456789,"price":1e400,"urlDocument":"https://docs.example.test/a.pdf"}'}]
    result = document_candidates.discover(plan_for(value))
    occurrence = result["candidates"][0]["occurrences"][0]
    assert occurrence["script_json_pointer"] == "/urlDocument"
    assert occurrence["script_numeric_literals"] == {"/id": "1.234567890123456789", "/price": "1e400"}
    assert occurrence["script_numeric_encoding"] == "json_pointer_to_literal_null_placeholder_v1"
    witness = next(item for item in occurrence["identity_witnesses"] if item.get("script_json_pointer") == "")
    assert witness["fields"]["id"] is None and witness["script_numeric_literals"] == occurrence["script_numeric_literals"]


@pytest.mark.parametrize("url", ["https://broker.example/?access_token=secret#organization",
                                  "https://broker.example/#access_token=secret"])
def test_decoded_metadata_fragments_do_not_bypass_credential_checks(url):
    with pytest.raises(ValueError):
        document_candidates._safe_metadata({"@id": url}, decoded_source_key="svn")


def test_relative_empty_fragment_is_excluded_without_urljoin_rewrite():
    value = artifact()
    value["listings"] = []
    page = value["sources"][0]["retainedObservations"][0]["pageContext"]
    page["links"] = [{"href": "file.pdf#", "text": "Brochure"}]
    page["documentLinks"] = []
    result = document_candidates.discover(plan_for(value))
    assert result["candidates"] == []
    assert result["excluded"][0]["reason"] == "unsafe_or_unresolved_reference"
