import copy

import pytest

from source_evidence import (
    collector,
    document_acquisition,
    document_batch,
    document_candidates,
    document_priority,
    events,
)
from source_evidence import document_acquisition_policy as policy

CODE = "a" * 40
TARGET = "b" * 64
BODY = "c" * 64
CLOCK = "2026-10-29T00:00:00Z"
PAGE = "https://transwestern.com/property/example"
SITE = "https://docs.example.test/site.pdf"
PROP = "https://docs.example.test/property.pdf"
OTHER = "https://docs.example.test/other.pdf"


def fixture(*, binding=True, generation="generation-1", bound_generation=None, page=0, mutate=None, run_mutate=None):
    occurrences = [{"url": PROP, "channel": "document", "role": "property",
                    "basis": "property_attachment_table", "element": "a", "attribute": "href"}]
    row = {"sourceKey": "transwestern", "url": PAGE, "detailObservedAt": CLOCK, "inventoryObservedAt": CLOCK,
           "detailScrape": {"url": PAGE}, "freshnessProvenance": {"detailScope": "detail_page", "generationId": generation,
               "method": "transwestern_detail_page", "cacheDisposition": "live"},
           "transwesternBuildingFacts": {"sourceUrl": PAGE, "bodySha256": BODY, "observedAt": CLOCK},
           "transwesternDocumentMediaObservations": {"schema": "transwestern-document-media-roles/v1",
               "sourceUrl": PAGE, "bodySha256": BODY, "observedAt": CLOCK, "state": "inspected",
               "candidateCount": 1, "eligibleLinkCandidateCount": 0, "occurrenceCount": len(occurrences),
               "omittedCount": 0, "skippedCandidateCount": 0, "skippedLinkEntryCount": 0,
               "occurrences": occurrences},
           "documents": [{"url": SITE}, {"url": PROP}, {"url": OTHER}]}
    if mutate:
        mutate(row)
    artifact = {"sources": [{"sourceKey": "transwestern"}], "listings": [row],
                "runMeta": {"startedAt": CLOCK, "finishedAt": CLOCK, "page": page,
                            "freshness": {"generationId": bound_generation or generation,
                                          "generationStartedAt": CLOCK, "requireFreshDetails": True}}}
    if run_mutate:
        run_mutate(artifact["runMeta"])
    auto = {"policySha256": "d" * 64, "collectorGitSha": CODE, "targetSha256": TARGET,
            "transportSha256": "e" * 64, "artifactFileSha256": "f" * 64,
            "generation": bound_generation or generation, "attempt": 1}
    wrapped = collector.convert(artifact, source_key="transwestern", policy_binding=auto if binding else None)
    return events.plan(wrapped, source_key="transwestern")


def settings():
    return {"schema_version": policy.SCHEMA_V4, "purpose": "private_public_listing_document_text",
            "data_scope": "public_listing_source", "collector_git_sha": CODE, "target_sha256": TARGET,
            "source_keys": ["transwestern"], "allowed_hosts": {"transwestern": ["docs.example.test"]},
            "limits": {"max_documents": 1, "max_document_bytes": 1024, "max_total_bytes": 2048,
                       "document_timeout_seconds": 30, "run_timeout_seconds": 3600, "max_redirects": 2},
            "original_retention": "temporary_only", "selection_order": document_priority.MODE,
            "pdf_complement": {"mode": "poppler_pdf_text_v1", "pdftotext_path": "/usr/bin/pdftotext"},
            "sparse_ocr": {"mode": "sparse_ocr_v1", "strategy": "no_text", "max_pages": 1}}


def prepare(plan, value=None):
    candidates = document_batch.discover(plan) if isinstance(plan, list) else document_candidates.discover(plan)
    raw = events.encoded(value or settings())
    bindings = {"expected_policy_sha256": events.sha256(raw), "target_sha256": TARGET, "collector_git_sha": CODE}
    fn = document_batch.prepare if isinstance(plan, list) else policy.prepare
    return candidates, raw, bindings, fn(plan, candidates, raw, **bindings)


def test_property_first_preserves_original_indices_and_accounts_for_every_candidate():
    plan = fixture()
    before = copy.deepcopy(plan)
    candidates, raw, bindings, selected = prepare(plan)
    assert plan == before
    assert [x["url"] for x in candidates["candidates"]] == [SITE, PROP, OTHER]
    assert selected["documents"][0]["candidate_index"] == 1
    assert selected["documents"][0]["priority"]["class"] == "property"
    assert selected["documents"][0]["priority"]["witness"]["generation"] == "generation-1"
    assert {x["candidate_index"] for x in selected["exclusions"]} == {0, 2}
    assert selected["selection_order"] == document_priority.MODE
    assert document_acquisition.validate_selection(plan, candidates, raw, selected, **bindings) == selected
    changed = copy.deepcopy(selected)
    changed["documents"][0]["priority"]["witness"]["generation"] = "other"
    with pytest.raises(ValueError):
        document_acquisition.validate_selection(plan, candidates, raw, changed, **bindings)


def test_manual_plan_or_mismatched_generation_is_not_prioritized():
    for plan in (fixture(binding=False), fixture(bound_generation="other")):
        _, _, _, selected = prepare(plan)
        assert selected["documents"][0]["candidate_index"] == 0
        assert selected["documents"][0]["priority"] == {"class": "unprioritized", "witness": None}


@pytest.mark.parametrize("mutate", [
    lambda r: r["transwesternDocumentMediaObservations"].update(state="incomplete"),
    lambda r: r["transwesternDocumentMediaObservations"].update(omittedCount=1),
    lambda r: r["transwesternDocumentMediaObservations"].update(omittedCount=False),
    lambda r: r["transwesternDocumentMediaObservations"].update(skippedCandidateCount=False),
    lambda r: r["transwesternDocumentMediaObservations"].update(skippedLinkEntryCount=False),
    lambda r: r["transwesternDocumentMediaObservations"].update(candidateCount=0),
    lambda r: r["transwesternDocumentMediaObservations"].update(eligibleLinkCandidateCount=1),
    lambda r: r["transwesternDocumentMediaObservations"].update(candidateCount=True),
    lambda r: r["transwesternDocumentMediaObservations"].update(bodySha256="d" * 64),
    lambda r: r["transwesternDocumentMediaObservations"].update(sourceUrl="https://transwestern.com/property/other"),
    lambda r: r["transwesternDocumentMediaObservations"].update(observedAt="2026-10-28T00:00:00Z"),
    lambda r: r["freshnessProvenance"].update(detailScope="inventory"),
    lambda r: r["freshnessProvenance"].update(cacheDisposition="source_revision_cache"),
    lambda r: r.update(inventoryObservedAt="2026-10-28T00:00:00Z"),
    lambda r: r["transwesternDocumentMediaObservations"].update(occurrenceCount=999),
    lambda r: r["transwesternDocumentMediaObservations"]["occurrences"][0].update(role="sitewide"),
    lambda r: r["transwesternDocumentMediaObservations"]["occurrences"][0].update(basis="unknown"),
    lambda r: r["transwesternDocumentMediaObservations"]["occurrences"].append(None),
])
def test_stale_malformed_and_unrelated_role_observations_do_not_prioritize(mutate):
    _, _, _, selected = prepare(fixture(mutate=mutate))
    assert selected["documents"][0]["candidate_index"] == 0


def test_oversized_complete_role_evidence_is_not_prioritized():
    def mutate(row):
        role = row["transwesternDocumentMediaObservations"]
        role["occurrences"] *= 1001
        role["occurrenceCount"] = len(role["occurrences"])
    _, _, _, selected = prepare(fixture(mutate=mutate))
    assert selected["documents"][0]["candidate_index"] == 0


@pytest.mark.parametrize("bad", [None, [], "not-an-object", 3])
def test_validated_collector_plan_with_nonobject_freshness_is_unprioritized(bad):
    plan = fixture(run_mutate=lambda run: run.update(freshness=bad))
    events.validate_content_policy(plan)
    _, _, _, selected = prepare(plan)
    assert selected["documents"][0]["candidate_index"] == 0


@pytest.mark.parametrize("key", ["provenance", "context"])
def test_malformed_header_dict_is_ignored_defensively(key):
    plan = fixture()
    plan["events"][0]["facts"][0]["value"][key] = None
    assert document_priority._records(plan, collector_git_sha=CODE, target_sha256=TARGET) == {}


def test_independent_property_and_sitewide_locations_preserve_property_witness():
    def mutate(row):
        role = row["transwesternDocumentMediaObservations"]
        role["occurrences"].append({"url": PROP, "channel": "document", "role": "sitewide",
                                    "basis": "footer_navigation", "element": "a", "attribute": "href"})
        role["occurrenceCount"] += 1
    _, _, _, selected = prepare(fixture(mutate=mutate))
    assert selected["documents"][0]["candidate_index"] == 1
    assert selected["documents"][0]["priority"]["class"] == "property"


def test_host_refusal_before_limit_and_no_candidate_lost():
    def mutate(row):
        outside = "https://outside.example.test/property.pdf"
        row["documents"][1]["url"] = outside
        row["transwesternDocumentMediaObservations"]["occurrences"][0]["url"] = outside
    candidates, _, _, selected = prepare(fixture(mutate=mutate))
    assert selected["documents"][0]["candidate"]["url"] == SITE
    assert next(e for e in selected["exclusions"] if e["candidate_index"] == 1)["reason"] == "host_not_allowed"
    assert sorted(x["candidate_index"] for x in selected["documents"] + selected["exclusions"]) == list(range(len(candidates["candidates"])))


def test_batch_exact_url_keeps_occurrences_and_global_cap():
    plans = [fixture(page=0), fixture(page=1)]
    candidates, _, _, selected = prepare(plans)
    prop = next(x for x in candidates["candidates"] if x["url"] == PROP)
    assert len(prop["occurrences"]) == 2
    assert selected["documents"][0]["candidate"]["url"] == PROP
    assert len(selected["documents"][0]["candidate"]["occurrences"]) == 2


def test_batch_property_and_sitewide_occurrences_preserve_property_witness():
    def sitewide(row):
        row["transwesternDocumentMediaObservations"]["occurrences"][0].update(
            role="sitewide", basis="footer_navigation")
    plans = [fixture(page=0), fixture(page=1, mutate=sitewide)]
    _, _, _, selected = prepare(plans)
    assert selected["documents"][0]["candidate"]["url"] == PROP
    assert len(selected["documents"][0]["candidate"]["occurrences"]) == 2


def test_batch_unbound_occurrence_does_not_remove_independently_bound_property_witness():
    _, _, _, selected = prepare([fixture(page=0), fixture(page=1, binding=False)])
    assert selected["documents"][0]["candidate"]["url"] == PROP
    witness = selected["documents"][0]["priority"]["witness"]
    assert witness["plan_sha256"] == fixture(page=0)["plan_sha256"]


def test_nested_provider_document_does_not_borrow_listing_role():
    def mutate(row):
        row["documents"] = [row["documents"][0], row["documents"][2]]
        row["rawTranswesternFeed"] = {"documents": [{"url": PROP}]}
    _, _, _, selected = prepare(fixture(mutate=mutate))
    assert selected["documents"][0]["candidate"]["url"] == SITE
    prop = next(item for item in selected["exclusions"] if item["priority"]["class"] == "unprioritized"
                and item["candidate_index"] == 2)
    assert prop["reason"] == "document_count_limit"


def test_v4_invalid_mode_and_legacy_v3_have_distinct_contracts():
    plan = fixture()
    value = settings()
    value["selection_order"] = "other"
    with pytest.raises(ValueError):
        prepare(plan, value)
    value = settings()
    value["schema_version"] = policy.SCHEMA_V3
    del value["selection_order"]
    _, _, _, selected = prepare(plan, value)
    assert selected["documents"][0]["candidate_index"] == 0
    assert "selection_order" not in selected
    assert "priority" not in selected["documents"][0]
