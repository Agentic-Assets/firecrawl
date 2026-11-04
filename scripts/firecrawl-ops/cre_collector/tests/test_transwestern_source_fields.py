"""Synthetic source-field boundary tests; no provider or database access."""

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta

import cre_ingest as ingest
from source_fields.transwestern import qualified_building_fields


def artifact():
    start = datetime.now(UTC) - timedelta(minutes=10)
    stamp = lambda delta: (
        (start + timedelta(minutes=delta)).isoformat().replace("+00:00", "Z")
    )
    listing = {
        "sourceKey": "transwestern",
        "id": "sample-property",
        "url": "https://transwestern.com/property/sample-property",
        "transactionMode": "sale",
        "transactionType": "Sale",
        "rawTranswesternFeed": {"PageUrl": "sample-property"},
        "inventoryObservedAt": stamp(1),
        "detailObservedAt": stamp(2),
        "freshnessProvenance": {
            "generationId": "test-fields",
            "detailScope": "detail_page",
            "method": "transwestern_detail_page",
            "cacheDisposition": "live",
        },
        "transwesternBuildingFacts": {
            "schema": "transwestern-building-facts-v1",
            "scope": "property_details_tab",
            "visibility": "not_evaluated",
            "state": "inspected",
            "sourceUrl": "https://transwestern.com/property/sample-property",
            "bodySha256": "b" * 64,
            "observedAt": stamp(2),
            "observations": [
                {
                    "field": "elevators",
                    "label": "Elevators",
                    "literal": "0",
                    "eligibility": "eligible",
                },
                {
                    "field": "typical_floor_size",
                    "label": "Typical Floor Size",
                    "literal": "34,567",
                    "eligibility": "eligible",
                },
            ],
            "elevatorCount": 0,
            "typicalFloorSizeText": "34,567",
        },
    }
    return {
        "runMeta": {
            "mode": "full",
            "startedAt": stamp(0),
            "finishedAt": stamp(5),
            "freshness": {
                "requireFreshDetails": True,
                "generationId": "test-fields",
                "generationStartedAt": stamp(0),
            },
        },
        "listings": [listing],
    }


def qualify(value):
    ingest.validate_strict_artifact_freshness(value)
    return qualified_building_fields(
        value, hashlib.sha256(json.dumps(value).encode()).hexdigest()
    )


def test_zero_unitless_floor_and_missing_address_keep_source_context():
    data = artifact()
    listing = data["listings"][0]
    candidates = qualify(data)
    row = ingest.to_row(
        listing, {}, data["runMeta"]["finishedAt"], building_candidates=candidates
    )
    assert row["address"] is None
    fields = row["source_field_building"]
    assert fields["elevator_count"]["value"] == 0
    assert fields["typical_floor_size_text"]["value"] == "34,567"
    assert fields["typical_floor_size_text"]["literal"] == "34,567"
    assert (
        fields["elevator_count"]["original_observed_at"] == listing["detailObservedAt"]
    )
    assert fields["elevator_count"]["tenure"] == "property"
    assert fields["elevator_count"]["body_sha256"] == "b" * 64


def test_mutated_source_identity_clock_and_scope_are_not_candidates():
    for key, value in (
        ("sourceUrl", "https://transwestern.com/property/other"),
        ("observedAt", "2020-01-01T00:00:00Z"),
        ("scope", "entire_document"),
        ("state", "ambiguous_property_section"),
        ("bodySha256", "invalid"),
    ):
        data = artifact()
        data["listings"][0]["transwesternBuildingFacts"][key] = value
        assert not qualify(data), key
    data = artifact()
    data["listings"][0]["rawTranswesternFeed"]["PageUrl"] = "other"
    assert not qualify(data)
    assert not qualified_building_fields(artifact(), "invalid")
    data = artifact()
    data["runMeta"]["mode"] = "synthetic_offline_replay"
    assert not qualified_building_fields(data, "a" * 64)


def test_duplicates_ineligible_and_unparseable_preserve_other_field_only():
    for mutation in ("duplicate", "ineligible", "bool", "overflow", "wrong_label"):
        data = artifact()
        listing = data["listings"][0]
        facts = listing["transwesternBuildingFacts"]
        if mutation == "duplicate":
            facts["observations"].append(copy.deepcopy(facts["observations"][0]))
            facts["state"] = "ambiguous_fields"
        elif mutation == "ineligible":
            facts["observations"][0]["eligibility"] = "ineligible"
        elif mutation == "bool":
            facts["elevatorCount"] = False
        elif mutation == "overflow":
            facts["elevatorCount"] = 2147483648
            facts["observations"][0]["literal"] = "2147483648"
        else:
            facts["observations"][0]["label"] = "Floors"
        fields = qualify(data)[id(listing)]["values"]
        assert "elevator_count" not in fields, mutation
        assert fields["typical_floor_size_text"]["value"] == "34,567"


def test_property_field_conflicts_are_independent_ordered_and_sticky():
    data = artifact()
    first = data["listings"][0]
    second = copy.deepcopy(first)
    second["transactionMode"] = "lease"
    second["transactionType"] = "Lease"
    second["detailObservedAt"] = (
        (
            datetime.fromisoformat(first["detailObservedAt"].replace("Z", "+00:00"))
            + timedelta(minutes=1)
        )
        .isoformat()
        .replace("+00:00", "Z")
    )
    second["transwesternBuildingFacts"].update(
        observedAt=second["detailObservedAt"], elevatorCount=1
    )
    second["transwesternBuildingFacts"]["observations"][0]["literal"] = "1"
    data["listings"].append(second)
    candidates = qualify(data)
    a, b = [
        ingest.to_row(
            row, {}, data["runMeta"]["finishedAt"], building_candidates=candidates
        )
        for row in (first, second)
    ]
    for left, right in ((a, b), (b, a)):
        merged = ingest.merge_rows(copy.deepcopy(left), copy.deepcopy(right))
        assert "elevator_count" not in merged["source_field_building"]
        assert (
            merged["source_field_building"]["typical_floor_size_text"]
            == candidates[id(second)]["values"]["typical_floor_size_text"]
        )
        assert merged["_source_building_conflicts"] == ["elevator_count"]
        again = ingest.merge_rows(merged, copy.deepcopy(a))
        assert "elevator_count" not in again["source_field_building"]


def test_placeholder_needs_the_existing_published_identity_proof():
    data = artifact()
    listing = data["listings"][0]
    listing["id"] = listing["rawTranswesternFeed"]["PageUrl"] = "-"
    listing["url"] = listing["transwesternBuildingFacts"]["sourceUrl"] = (
        "https://transwestern.com/property/-"
    )
    assert not qualify(data)
    listing["transwesternPublishedIdentity"] = {
        "url": listing["url"],
        "sitemapUrl": "https://transwestern.com/sitemap.xml",
        "identitySha256": "c" * 64,
        "generationId": "test-fields",
        "verifiedAt": listing["inventoryObservedAt"],
    }
    assert qualify(data)


def test_unresolved_source_observation_survives_qualification_and_both_merge_orders():
    for kind in ("ambiguous", "concealed", "unparseable"):
        data = artifact()
        good = data["listings"][0]
        uncertain = copy.deepcopy(good)
        uncertain["transactionMode"] = "lease"
        uncertain["transactionType"] = "Lease"
        facts = uncertain["transwesternBuildingFacts"]
        facts.pop("elevatorCount")
        if kind == "ambiguous":
            facts["state"] = "ambiguous_fields"
            other = copy.deepcopy(facts["observations"][0])
            other["literal"] = "1"
            facts["observations"].append(other)
        elif kind == "concealed":
            facts["state"] = "malformed_or_concealed_row"
            facts["observations"][0]["eligibility"] = "ineligible"
        else:
            facts["observations"][0]["literal"] = "two passenger"
        data["listings"].append(uncertain)
        candidates = qualify(data)
        assert "elevator_count" in candidates[id(uncertain)]["blocked"]
        a, b = [
            ingest.to_row(
                row, {}, data["runMeta"]["finishedAt"], building_candidates=candidates
            )
            for row in (good, uncertain)
        ]
        for left, right in ((a, b), (b, a)):
            merged = ingest.merge_rows(copy.deepcopy(left), copy.deepcopy(right))
            assert not (merged.get("source_field_building") or {}).get(
                "elevator_count"
            ), kind
            assert "elevator_count" in merged["_source_building_conflicts"]
            assert (
                merged["source_field_building"]["typical_floor_size_text"]["value"]
                == "34,567"
            )
            again = ingest.merge_rows(merged, copy.deepcopy(a))
            assert not (again.get("source_field_building") or {}).get("elevator_count")


def test_unknown_structure_does_not_masquerade_as_missing_fields():
    data = artifact()
    good = data["listings"][0]
    unknown = copy.deepcopy(good)
    unknown["transactionMode"] = "lease"
    unknown["transactionType"] = "Lease"
    unknown["transwesternBuildingFacts"].update(
        state="ambiguous_details_structure", observations=[]
    )
    data["listings"].append(unknown)
    candidates = qualify(data)
    assert candidates[id(unknown)]["blocked"] == [
        "elevator_count",
        "typical_floor_size_text",
    ]
    a, b = [
        ingest.to_row(
            row, {}, data["runMeta"]["finishedAt"], building_candidates=candidates
        )
        for row in (good, unknown)
    ]
    for left, right in ((a, b), (b, a)):
        merged = ingest.merge_rows(copy.deepcopy(left), copy.deepcopy(right))
        assert merged["source_field_building"] is None
