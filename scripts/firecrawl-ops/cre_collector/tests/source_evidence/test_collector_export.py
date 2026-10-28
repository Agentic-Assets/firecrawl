import copy
import json
import os

import pytest
from source_evidence import acquisition, collector, collector_export, events, map_projection


def artifact():
    return {"runMeta": {"finishedAt": "2026-10-28T12:00:00Z"},
            "sources": [{"sourceKey": "foundry-commercial",
                         "retainedObservations": [{"index": i, "facts": [False, None, []]} for i in range(78)]}],
            "listings": [{"sourceKey": "foundry-commercial", "id": "one"}], "errors": [None]}


def rows_for(value, **kwargs):
    wrapped = collector.convert(value, source_key="foundry-commercial", **kwargs)
    return events.plan(wrapped, source_key="foundry-commercial")["events"]


def test_v2_export_reconstructs_78_unqualified_observations(tmp_path):
    original = artifact()
    rows = rows_for(original)
    for row in rows:
        row["observed_at"] = "2026-10-28T08:00:00-04:00"
    payload, metadata = collector_export.reconstruct(rows)
    assert json.loads(payload) == original
    assert metadata["listing_count"] == 1 and metadata["observation_count"] == 78
    assert metadata["coverage"] == "unverified" and metadata["publisher_identity"] == "unverified"
    out = tmp_path / "export"
    assert collector_export.export(rows, out) == metadata
    assert (out / "collector.json").read_bytes() == payload
    assert json.loads((out / "metadata.json").read_bytes()) == metadata
    assert os.stat(out).st_mode & 0o777 == 0o700
    assert all(os.stat(out / name).st_mode & 0o777 == 0o600 for name in ("collector.json", "metadata.json"))
    with pytest.raises(FileExistsError):
        collector_export.export(rows, out)


def test_exceptional_numeric_literal_restored_at_original_pointer():
    original = artifact()
    original["sources"][0]["retainedObservations"][0]["exact"] = "REPLACE_NUMBER"
    raw = json.dumps(original).replace('"REPLACE_NUMBER"', '0.10000000000000000001').encode()
    document, literals = acquisition.decode_json(raw)
    rows = rows_for(document, numeric_literals=literals)
    payload, metadata = collector_export.reconstruct(rows)
    assert b"0.10000000000000000001" in payload
    assert acquisition.decode_json(payload) == (document, literals)
    assert metadata["export_sha256"] == events.sha256(payload)
    assert metadata["artifact_hash_kind"] == "canonical_json_exact_numeric_literals_utf8"
    assert metadata["collector_provenance"]["numericLiterals"] == literals


def test_empty_v1_capture_and_historical_v1_nonempty_context():
    original = artifact()
    original["listings"] = []
    original["sources"][0]["retainedObservations"] = []
    rows = rows_for(original)
    payload, metadata = collector_export.reconstruct(rows)
    assert len(rows) == 1 and json.loads(payload) == original
    assert metadata["observation_schema"] == collector.SCHEMA
    legacy = artifact()
    rows = rows_for(legacy, schema=collector.SCHEMA)
    payload, metadata = collector_export.reconstruct(rows)
    assert json.loads(payload) == legacy
    assert metadata["observation_count"] == 78
    assert metadata["separate_observation_count"] == 0


def test_unwitnessed_jsonb_numeric_representation_change_is_refused():
    original = artifact()
    original["listings"][0]["ordinaryFloat"] = 1e20
    rows = rows_for(original, integral_floats=None)
    # Model JSONB's decimal rendering; this does not execute a database query.
    rows[1]["facts"][0]["value"]["ordinaryFloat"] = 100000000000000000000
    with pytest.raises(ValueError):
        collector_export.reconstruct(rows)


@pytest.mark.parametrize("schema", [collector.SCHEMA, collector.SCHEMA_V2])
def test_witnessed_scientific_integral_floats_survive_modeled_jsonb_rendering(schema):
    original = artifact()
    original["runMeta"]["unknownNumber"] = -1e20
    original["listings"][0]["ordinaryFloat"] = 1e20
    for observation in original["sources"][0]["retainedObservations"]:
        observation["ordinaryFloat"] = 1e20
    rows = rows_for(original, schema=schema)
    def jsonb_shape(value):
        if isinstance(value, dict):
            return {key: jsonb_shape(child) for key, child in value.items()}
        if isinstance(value, list):
            return [jsonb_shape(child) for child in value]
        if type(value) is float and value.is_integer() and "e" in repr(value):
            # Decimal JSON rendering, not int(float), which can change the
            # decimal value for some floating-point exponent forms.
            from decimal import Decimal
            return int(Decimal(repr(value)))
        return value
    readback = jsonb_shape(rows)
    for row in readback:
        row["observed_at"] = "2026-10-28T08:00:00-04:00"
    payload, metadata = collector_export.reconstruct(readback)
    assert payload == events.encoded(original)
    assert metadata["export_sha256"] == events.sha256(payload)
    assert len(metadata["collector_provenance"]["integralFloats"]) == 80


def test_witnessed_capture_refuses_a_changed_numeric_value():
    original = artifact()
    original["listings"][0]["ordinaryFloat"] = 1e20
    rows = rows_for(original)
    rows[1]["facts"][0]["value"]["ordinaryFloat"] = 100000000000000000001
    with pytest.raises(ValueError):
        collector_export.reconstruct(rows)


def test_numeric_witness_in_unsplit_unknown_context_stays_in_header():
    original = artifact()
    original["sources"].append({"sourceKey": "foundry-commercial",
                                "retainedObservations": {"unknown": 1e20}})
    rows = rows_for(original)
    header = rows[0]["facts"][0]["value"]
    header["context"]["sources"][1]["retainedObservations"]["unknown"] = 100000000000000000000
    assert collector_export.reconstruct(rows)[0] == events.encoded(original)


@pytest.mark.parametrize("mutation", ["missing", "reorder", "duplicate", "foreign", "fact", "time", "field"])
def test_incomplete_or_mixed_capture_is_refused(mutation, tmp_path):
    rows = rows_for(artifact())
    if mutation == "missing":
        rows.pop()
    elif mutation == "reorder":
        rows[1], rows[2] = rows[2], rows[1]
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(rows[-1]))
    elif mutation == "foreign":
        rows[1]["capture_id"] = "evidence-foreign"
    elif mutation == "fact":
        rows[1]["facts"][0]["value"]["id"] = "tampered"
    elif mutation == "time":
        rows[1]["observed_at"] = "2026-10-28T12:00:01Z"
    else:
        rows[1]["unexpected"] = True
    out = tmp_path / "refused"
    with pytest.raises(ValueError):
        collector_export.export(rows, out)
    assert not out.exists()


def test_projection_is_labeled_and_source_wire_is_not_claimed():
    original = {"runMeta": {"finishedAt": "2026-10-28T12:00:00Z"},
                "sources": [{"sourceKey": "faris-lee"}],
                "listings": [{"sourceKey": "faris-lee", "photo":
                    "https://maps.googleapis.com/maps/vt?pb=x&key=sample&token=123"}]}
    projected, literals, receipt = map_projection.prepare(json.dumps(original).encode(), source_key="faris-lee")
    wrapped = collector.convert(projected, source_key="faris-lee", numeric_literals=literals or None,
                                projection=receipt)
    rows = events.plan(wrapped, source_key="faris-lee")["events"]
    payload, metadata = collector_export.reconstruct(rows)
    assert json.loads(payload) == projected
    assert b"token=123" not in payload
    assert metadata["representation"] == "source_projected_collector_artifact"
    assert metadata["artifact_hash_kind"] == "projected_canonical_json_utf8"
    assert metadata["collector_provenance"]["projection"] == receipt
