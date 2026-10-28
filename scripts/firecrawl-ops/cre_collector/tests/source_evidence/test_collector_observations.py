import copy
import json

import pytest
from source_evidence import acquisition, collector, events


def artifact():
    held = [None, False, [], {"unknown": [None, False, []]}, {"duplicate": 1}, {"duplicate": 1}]
    held += [{"index": i, "facts": [False, None, []]} for i in range(72)]
    return {"runMeta": {"finishedAt": "2026-10-28T12:00:00Z"},
            "sources": [{"sourceKey": "foundry-commercial", "retainedObservations": held[:40]},
                        {"sourceKey": "foundry-commercial", "retainedObservations": []},
                        {"sourceKey": "foundry-commercial", "retainedObservations": held[40:]}],
            "listings": [{"sourceKey": "foundry-commercial", "id": "accepted"}],
            "errors": [None, False, []]}


def test_split_roundtrip_retains_all_values_and_source_positions():
    original = artifact()
    before = copy.deepcopy(original)
    wrapped = collector.convert(original, source_key="foundry-commercial")
    assert original == before
    assert wrapped["schema"] == collector.SCHEMA_V2
    assert wrapped["observationGroups"] == [{"sourceIndex": 0, "count": 40}, {"sourceIndex": 2, "count": 38}]
    assert collector.record_count(wrapped) == 79
    assert len(wrapped["observations"]) == 78
    assert wrapped["context"]["sources"][0]["retainedObservations"] == []
    assert wrapped["context"]["sources"][1]["retainedObservations"] == []
    assert collector.reconstruct(wrapped) == before
    wrapped["observations"][3]["value"]["unknown"].append("changed")
    assert original == before


def test_v1_historical_envelope_replays_even_when_carrier_is_nonempty():
    original = artifact()
    legacy = collector.convert(original, source_key="foundry-commercial", schema=collector.SCHEMA)
    assert legacy["schema"] == collector.SCHEMA
    assert "observations" not in legacy
    assert collector.reconstruct(legacy) == original
    assert collector.convert(collector.reconstruct(legacy), source_key="foundry-commercial",
                             schema=legacy["schema"]) == legacy
    empty = {**original, "sources": [{"sourceKey": "foundry-commercial", "retainedObservations": []}]}
    assert collector.convert(empty, source_key="foundry-commercial")["schema"] == collector.SCHEMA
    with pytest.raises(ValueError):
        collector.convert(empty, source_key="foundry-commercial", schema=collector.SCHEMA_V2)


def test_large_aggregate_is_split_without_dropping_or_truncation():
    original = artifact()
    for source in original["sources"]:
        if source["retainedObservations"]:
            source["retainedObservations"] = [{"blob": "z" * 50000, "ordinal": i}
                                               for i, _ in enumerate(source["retainedObservations"])]
    assert len(json.dumps(original).encode()) > 3_500_000
    wrapped = collector.convert(original, source_key="foundry-commercial")
    assert len(json.dumps(wrapped["context"]).encode()) < 10000
    assert len(wrapped["observations"]) == 78
    assert collector.reconstruct(wrapped) == original
    plan = events.plan(wrapped, source_key="foundry-commercial")
    events.validate_content_policy(plan)
    assert len(plan["events"]) == 80
    assert plan["events"][1]["facts"][0]["kind"] == "source_record"
    assert all(row["facts"][0]["kind"] == "source_observation" for row in plan["events"][2:])
    with pytest.raises(ValueError, match="metadata exceeds storage contract"):
        events.plan(collector.convert(original, source_key="foundry-commercial", schema=collector.SCHEMA),
                    source_key="foundry-commercial")


def test_exceptional_numeric_literals_keep_original_pointers():
    original = artifact()
    original["sources"][0]["retainedObservations"][0] = "REPLACE_NUMBER"
    raw = json.dumps(original).replace('"REPLACE_NUMBER"', '0.10000000000000000001').encode()
    document, literals = acquisition.decode_json(raw)
    wrapped = collector.convert(document, source_key="foundry-commercial", numeric_literals=literals)
    assert wrapped["provenance"]["numericLiterals"] == literals
    restored = collector.reconstruct(wrapped)
    assert acquisition.decode_json(acquisition.encode_json(restored, literals).encode()) == (document, literals)
    assert collector.convert(restored, source_key="foundry-commercial", numeric_literals=literals,
                             schema=collector.SCHEMA_V2) == wrapped
    events.validate_plan(events.plan(wrapped, source_key="foundry-commercial"))


def test_historical_v1_event_plan_reconstructs_nonempty_carriers():
    wrapped = collector.convert(artifact(), source_key="foundry-commercial", schema=collector.SCHEMA)
    plan = events.plan(wrapped, source_key="foundry-commercial")
    assert plan["events"][0]["facts"][0]["kind"] == "retained_capture_context"
    assert len(plan["events"]) == 2
    events.validate_content_policy(plan)


@pytest.mark.parametrize("value", ["https://jll.com/listing", "https://example.org/?token=secret",
                                    {"sourceKey": "jll"}])
def test_split_observations_preserve_source_and_credential_refusals(value):
    original = artifact()
    original["sources"][0]["retainedObservations"][0] = value
    plan = events.plan(collector.convert(original, source_key="foundry-commercial"),
                       source_key="foundry-commercial")
    with pytest.raises(ValueError):
        events.validate_content_policy(plan)


@pytest.mark.parametrize("mutation", ["interleave", "drop", "duplicate", "reorder"])
def test_rehashed_observation_event_tampering_is_refused(mutation):
    plan = events.plan(collector.convert(artifact(), source_key="foundry-commercial"),
                       source_key="foundry-commercial")
    rows = plan["events"]
    if mutation == "interleave":
        rows[1], rows[2] = rows[2], rows[1]
    elif mutation == "drop":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(rows[-1]))
    else:
        rows[2], rows[3] = rows[3], rows[2]
    plan["plan_sha256"] = events.sha256(events.encoded({key: value for key, value in plan.items()
                                                      if key != "plan_sha256"}))
    with pytest.raises(ValueError):
        events.validate_plan(plan)


@pytest.mark.parametrize("mutation", ["group_order", "source", "index", "missing", "extra", "skeleton", "duplicate"])
def test_tampered_v2_pointers_and_skeleton_refuse(mutation):
    wrapped = collector.convert(artifact(), source_key="foundry-commercial")
    if mutation == "group_order":
        wrapped["observationGroups"].reverse()
    elif mutation == "source":
        wrapped["observations"][0]["sourceIndex"] = 2
    elif mutation == "index":
        wrapped["observations"][0]["observationIndex"] = 1
    elif mutation == "missing":
        wrapped["observations"].pop()
    elif mutation == "extra":
        wrapped["observations"].append(copy.deepcopy(wrapped["observations"][-1]))
    elif mutation == "skeleton":
        wrapped["context"]["sources"][0]["retainedObservations"] = ["injected"]
    else:
        wrapped["observationGroups"].append(copy.deepcopy(wrapped["observationGroups"][-1]))
    with pytest.raises(ValueError):
        collector.reconstruct(wrapped)


def test_combined_record_limit_and_schema_validation():
    original = artifact()
    original["sources"][0]["retainedObservations"] = [None] * 10000
    original["sources"][2]["retainedObservations"] = []
    with pytest.raises(ValueError):
        collector.convert(original, source_key="foundry-commercial")
    original["listings"] = []
    wrapped = collector.convert(original, source_key="foundry-commercial")
    assert collector.record_count(wrapped) == 10000
    with pytest.raises(ValueError):
        collector.convert(original, source_key="foundry-commercial", schema="future")
