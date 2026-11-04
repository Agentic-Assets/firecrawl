"""Synthetic fixture-only evidence fidelity and isolation checks."""

import json
from decimal import Decimal

import buildout_observation_fixture_planner as writer
import pytest

import buildout_observation_artifact as builder
import buildout_observation_manifest as manifest


def bundle():
    rows = [
        {
            "index_attributes": [
                ["Cap Rate", "6.25%"],
                ["Cap Rate", "6.75%"],
                ["Zoning", "CMU"],
            ],
            "unknownFacts": {
                "floorplate": {
                    "arbitraryLabel": "fixture data",
                    "highPrecision": "precision-number",
                },
                "extra": [1, False],
            },
            "password": "fixture secret",
        },
        *[{"unknownFacts": {"floorplate": i}} for i in range(84)],
    ]
    raw = (
        json.dumps({"syntheticFixture": True, "inventory": rows})
        .replace('"precision-number"', "12345678901234567890.1234567890123456789")
        .encode()
    )
    scope = {
        "sourceKey": "fortis-net-lease",
        "generationId": None,
        "captureId": "fixture-full",
        "page": None,
        "transaction": "unknown",
    }
    artifact, _ = builder.build_artifact(
        raw, source_key=scope["sourceKey"], input_kind="buildout-page"
    )
    seal = manifest.canonical(
        manifest.seal(raw, artifact, scope=scope, input_kind="buildout-page")
    )
    return raw, artifact, seal, scope


def test_full_safe_record_fidelity_and_no_raw_original(tmp_path):
    raw, artifact, seal, scope = bundle()
    original = tmp_path / "original"
    original.write_bytes(raw)
    original.chmod(0o600)
    plan = writer.plan(
        original=original.read_bytes(), artifact=artifact, seal=seal, scope=scope
    )
    assert original.read_bytes() == raw
    assert plan["p_artifact_bytes"] == artifact and "p_original_bytes" not in plan
    records = plan["p_observations"]
    assert len(records) == 85
    assert (
        records == manifest.parse_object(artifact, builder.MAX_OUTPUT_BYTES)["records"]
    )
    assert (
        records[0]["sourceFields"]["unknownFacts"]["floorplate"]["arbitraryLabel"]
        == "fixture data"
    )
    assert records[0]["sourceFields"]["unknownFacts"]["floorplate"][
        "highPrecision"
    ] == Decimal("12345678901234567890.1234567890123456789")
    assert [x["literal"][1] for x in records[0]["indexAttributes"]] == [
        "6.25%",
        "6.75%",
        "CMU",
    ]
    assert [x["sourceOrdinal"] for x in records[0]["indexAttributes"]] == [0, 1, 2]
    assert (
        b"fixture secret" not in artifact
        and plan["p_provenance"]["quarantinedItems"] == 1
    )
    assert plan["p_provenance"]["sourceCompleteness"] == "unverified"


def test_reject_tampering_non_fixture_and_jll():
    raw, artifact, seal, scope = bundle()
    with pytest.raises(ValueError):
        writer.plan(original=raw + b" ", artifact=artifact, seal=seal, scope=scope)
    with pytest.raises(ValueError):
        writer.plan(original=raw, artifact=artifact + b" ", seal=seal, scope=scope)
    with pytest.raises(ValueError):
        writer.plan(
            original=raw,
            artifact=artifact,
            seal=seal,
            scope={**scope, "sourceKey": "jll"},
        )
    with pytest.raises(ValueError):
        writer.plan(
            original=b'{"inventory":[]}', artifact=artifact, seal=seal, scope=scope
        )


def test_local_retry_model():
    raw, artifact, seal, scope = bundle()
    plan = writer.plan(original=raw, artifact=artifact, seal=seal, scope=scope)
    store = {}
    canonical = {"listing": "active", "sourceIndex": "present"}

    def apply(item):
        key = (item["p_capture_id"], item["p_capture_page"])
        if key in store and store[key] != item:
            raise ValueError("divergent")
        if key in store:
            return "unchanged"
        store[key] = item
        return "inserted"

    assert apply(plan) == "inserted" and apply(plan) == "unchanged"
    with pytest.raises(ValueError):
        apply(dict(plan, p_original_sha256="0" * 64))
    assert store[(scope["captureId"], 0)] == plan and canonical == {
        "listing": "active",
        "sourceIndex": "present",
    }
