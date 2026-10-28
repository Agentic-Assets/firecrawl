import copy

import pytest
from source_evidence import collector, collector_export, events, map_projection, number_witness


def fixture():
    return {"runMeta": {"finishedAt": "2026-10-28T12:00:00Z", "measure": 1e20},
            "sources": [{"sourceKey": "foundry-commercial", "retainedObservations": [
                {"area": 1e18, "small": 1e-7, "ordinary": 1.0, "flag": False}]}],
            "listings": [{"sourceKey": "foundry-commercial", "value": -1e21}]}


def test_witnesses_only_scientific_integral_floats_and_pins_legacy():
    original = fixture()
    wrapped = collector.convert(original, source_key="foundry-commercial")
    assert wrapped["provenance"]["integralFloatEncoding"] == number_witness.ENCODING
    assert wrapped["provenance"]["integralFloats"] == {
        "/runMeta/measure": "1e+20", "/sources/0/retainedObservations/0/area": "1e+18",
        "/listings/0/value": "-1e+21"}
    assert collector.reconstruct(wrapped) == original
    legacy = collector.convert(original, source_key="foundry-commercial", integral_floats=None)
    assert "integralFloats" not in legacy["provenance"]
    assert collector.convert(collector.reconstruct(legacy), source_key="foundry-commercial",
                             schema=legacy["schema"], integral_floats=None) == legacy


def test_jsonb_integer_readback_restores_only_witnessed_v2_events():
    original = fixture()
    wrapped = collector.convert(original, source_key="foundry-commercial")
    rows = events.plan(wrapped, source_key="foundry-commercial")["events"]
    stored = copy.deepcopy(rows)
    stored[0]["facts"][0]["value"]["context"]["runMeta"]["measure"] = 100000000000000000000
    stored[1]["facts"][0]["value"]["value"] = -1000000000000000000000
    stored[2]["facts"][0]["value"]["value"]["area"] = 1000000000000000000
    corrected, normalized = collector.restore_event_numbers(stored)
    assert events.encoded(corrected) == events.encoded(wrapped)
    assert events.encoded(normalized) == events.encoded(rows)
    payload, _ = collector_export.reconstruct(stored)
    assert payload == events.encoded(original)


def test_v1_context_witness_uses_original_source_pointer():
    original = fixture()
    wrapped = collector.convert(original, source_key="foundry-commercial", schema=collector.SCHEMA)
    rows = events.plan(wrapped, source_key="foundry-commercial")["events"]
    rows[0]["facts"][0]["value"]["context"]["sources"][0]["retainedObservations"][0]["area"] = 10**18
    restored, _ = collector.restore_event_numbers(rows)
    assert events.encoded(restored) == events.encoded(wrapped)


@pytest.mark.parametrize("value,literal", [(True, "1e+20"), (1, "1e+20"), (10**20, "1E+20"),
                                              (10**20, "1e20"), (10**20, "inf"),
                                              (10**20, "1.0000000000000001e+20")])
def test_invalid_witness_or_value_refuses(value, literal):
    with pytest.raises(ValueError):
        number_witness.restore_value(value, literal)


def test_tampered_pointers_overlap_and_incomplete_map_refuse():
    original = fixture()
    witnesses = number_witness.collect(original)
    with pytest.raises(ValueError):
        number_witness.restore(original, {**witnesses, "/missing": "1e+20"})
    with pytest.raises(ValueError):
        number_witness.restore(original, witnesses, {"/runMeta/measure": "1e20"})
    incomplete = dict(witnesses)
    incomplete.pop("/listings/0/value")
    with pytest.raises(ValueError):
        number_witness.restore(original, incomplete)
    with pytest.raises(ValueError):
        number_witness.restore(original, {})


def test_boolean_tampering_is_not_accepted_as_numeric_equivalence():
    wrapped = collector.convert(fixture(), source_key="foundry-commercial")
    rows = events.plan(wrapped, source_key="foundry-commercial")["events"]
    rows[2]["facts"][0]["value"]["value"]["flag"] = 0
    with pytest.raises(ValueError):
        collector_export.reconstruct(rows)


def test_projected_faris_digest_survives_witnessed_jsonb_readback():
    import json

    original = {"runMeta": {"finishedAt": "2026-10-28T12:00:00Z"},
                "sources": [{"sourceKey": "faris-lee"}],
                "listings": [{"sourceKey": "faris-lee", "amount": 1e20,
                              "photo": "https://maps.googleapis.com/maps/vt?pb=x&key=abc&token=123"}]}
    projected, literals, receipt = map_projection.prepare(json.dumps(original).encode(), source_key="faris-lee")
    wrapped = collector.convert(projected, source_key="faris-lee", numeric_literals=literals or None,
                                projection=receipt)
    rows = events.plan(wrapped, source_key="faris-lee")["events"]
    rows[1]["facts"][0]["value"]["amount"] = 10**20
    payload, metadata = collector_export.reconstruct(rows)
    assert metadata["export_sha256"] == receipt["projectedCanonicalSha256"]
    assert b"1e+20" in payload and b"token=123" not in payload


def test_escaped_pointer_and_witness_count_bounds(monkeypatch):
    document = {"a/b~c": [1e20, 1e21]}
    witnesses = number_witness.collect(document)
    assert set(witnesses) == {"/a~1b~0c/0", "/a~1b~0c/1"}
    assert number_witness.restore({"a/b~c": [10**20, 10**21]}, witnesses) == document
    monkeypatch.setattr(number_witness, "MAX_WITNESSES", 1)
    with pytest.raises(ValueError):
        number_witness.collect(document)
