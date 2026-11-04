"""Offline projection contracts. All fixtures are synthetic."""

import hashlib
import json
import subprocess
import sys

import buildout_observation_artifact as builder
import buildout_observation_manifest as manifests
import buildout_observation_projection as projection
import pytest

_INPUTS = {}
_SCOPE = {"sourceKey": "fortis-net-lease", "generationId": "synthetic-generation",
          "captureId": "synthetic-capture", "page": 0, "transaction": "lease"}


def artifact(rows, *, kind="buildout-page"):
    source = {"inventory": rows} if kind == "buildout-page" else rows
    original = json.dumps(source).encode()
    encoded, _ = builder.build_artifact(
        original, source_key="fortis-net-lease", input_kind=kind
    )
    seal = manifests.seal(original, encoded, scope=_SCOPE, input_kind=kind)
    _INPUTS[encoded] = (original, manifests.canonical(seal))
    return encoded


def decode(raw, *, sealed_artifact=None):
    original, manifest = _INPUTS[raw if sealed_artifact is None else sealed_artifact]
    output, counts = projection.project(raw, original=original, manifest=manifest, expected_scope=_SCOPE)
    return json.loads(output), counts


def test_eighty_synthetic_rows_preserve_literal_without_promoting_identity():
    rows = [
        {"id": str(i), "url": f"https://example.test/{i}",
         "index_attributes": [["# of Spaces", f"{i} Spaces"], ["Cap Rate", "6.5%"]]}
        for i in range(80)
    ]
    raw = artifact(rows)
    out, counts = decode(raw)
    assert counts == {"records": 80, "upstreamIncomplete": False}
    assert out["artifactSha256"] == hashlib.sha256(raw).hexdigest()
    assert out["promotion"] == "not_authorized"
    assert out["sourceCompleteness"] == out["freshness"] == out["visibility"] == "unverified"
    for i, record in enumerate(out["records"]):
        assert record == {
            "sourcePointer": f"/inventory/{i}", "identityResolution": "unresolved", "rowTransaction": "unverified",
            "availableSpaceCount": {"state": "candidate_unverified", "literal": f"{i} Spaces", "value": i},
            "capRate": {"state": "literal_only", "literal": "6.5%"},
        }
    assert "example.test" not in json.dumps(out)


@pytest.mark.parametrize("literal", ["1", "2 parking spaces", "1–3 Spaces", "-1 Spaces", "2147483648 Spaces", "١ Space"])
def test_unsupported_space_units_are_never_interpreted(literal):
    out, _ = decode(artifact([{"index_attributes": [["# of Spaces", literal]]}]))
    assert out["records"][0]["availableSpaceCount"] == {
        "state": "invalid", "reason": "unsupported_spaces_literal", "literal": literal,
    }


def test_duplicate_equal_or_conflicting_labels_never_select_a_value():
    rows = [{"index_attributes": [["# of Spaces", "1 Space"], ["# of Spaces", value],
                                    ["Cap Rate", "6%"], ["Cap Rate", "7%"]]}
            for value in ["1 Space", "2 Spaces"]]
    out, _ = decode(artifact(rows))
    for row in out["records"]:
        assert row["availableSpaceCount"]["state"] == "ambiguous"
        assert row["capRate"]["state"] == "ambiguous"
        assert "value" not in row["availableSpaceCount"]


def test_missing_null_and_upstream_quarantine_are_explicit():
    out, _ = decode(artifact([{}, {"index_attributes": None}]))
    assert out["records"][0]["availableSpaceCount"]["reason"] == "attributes_missing"
    assert out["records"][1]["capRate"]["reason"] == "attributes_null"
    out, summary = decode(artifact([{"index_attributes": [["# of Spaces", "3 Spaces"], ["API Key", "dummy"]]}]))
    assert summary["upstreamIncomplete"]
    assert out["records"][0]["availableSpaceCount"] == {
        "state": "omitted", "reason": "upstream_quarantine_or_omissions",
    }
    assert "dummy" not in json.dumps(out)


def test_retained_observation_requires_known_zero_omissions():
    base = {"schema": "buildout-inventory-attributes-v1", "attributes": [["# of Spaces", "1 Space"], ["Cap Rate", "7%"]], "omittedCount": 0}
    raw = artifact([{"buildoutInventoryObservation": base}], kind="retained-observations")
    out, _ = decode(raw)
    assert out["records"][0]["availableSpaceCount"]["value"] == 1
    assert out["records"][0]["capRate"] == {"state": "literal_only", "literal": "7%"}
    for omissions in [1, None]:
        data = dict(base, omittedCount=omissions)
        out, _ = decode(artifact([{"buildoutInventoryObservation": data}], kind="retained-observations"))
        assert out["records"][0]["availableSpaceCount"]["state"] == "omitted"


def test_untrusted_shapes_and_sensitive_or_oversized_literals_fail_closed():
    raw = artifact([{"index_attributes": [["# of Spaces", "1 Space"], ["Cap Rate", "Bearer dummy"]]}])
    out, _ = decode(raw)
    assert out["records"][0]["capRate"]["state"] == "omitted"
    assert "dummy" not in json.dumps(out)
    raw = artifact([{"index_attributes": [["Cap Rate", "X" * 129]]}])
    out, _ = decode(raw)
    assert out["records"][0]["capRate"]["state"] == "omitted"
    sealed = artifact([{"index_attributes": []}])
    value = json.loads(sealed)
    value["records"][0]["sourcePointer"] = "/inventory/../secret"
    with pytest.raises(ValueError, match="rebuilt input"):
        decode(json.dumps(value).encode(), sealed_artifact=sealed)
    with pytest.raises(ValueError):
        decode(b'{"schema":"a","schema":"b"}', sealed_artifact=sealed)


def test_inconsistent_summaries_and_reordered_evidence_never_emit_candidates():
    sealed = artifact([{"index_attributes": [["# of Spaces", "1 Space"], ["Cap Rate", "6%"]]}])
    data = json.loads(sealed)
    data["summary"]["inputRecords"] = 2
    with pytest.raises(ValueError, match="rebuilt input"):
        decode(json.dumps(data).encode(), sealed_artifact=sealed)
    data["summary"]["inputRecords"] = 1
    data["records"][0]["indexAttributes"].reverse()
    with pytest.raises(ValueError, match="rebuilt input"):
        decode(json.dumps(data).encode(), sealed_artifact=sealed)


def test_missing_conflicting_attribute_or_record_cannot_be_claimed_complete():
    sealed = artifact([{"index_attributes": [["# of Spaces", "1 Space"], ["# of Spaces", "2 Spaces"]]}])
    data = json.loads(sealed)
    data["records"][0]["indexAttributes"].pop(0)
    with pytest.raises(ValueError, match="rebuilt input"):
        decode(json.dumps(data).encode(), sealed_artifact=sealed)
    sealed = artifact([{"index_attributes": []}, {"index_attributes": []}])
    rows = json.loads(sealed)
    rows["records"].pop(0)
    rows["summary"]["inputRecords"] = rows["summary"]["retainedRecords"] = 1
    with pytest.raises(ValueError, match="rebuilt input"):
        decode(json.dumps(rows).encode(), sealed_artifact=sealed)


def test_retained_source_pointer_must_match_its_input_shape():
    sealed = artifact([{"buildoutInventoryObservation": {
        "schema": "buildout-inventory-attributes-v1", "attributes": [], "omittedCount": 0,
    }}], kind="retained-observations")
    data = json.loads(sealed)
    data["records"][0]["sourcePointer"] = "/inventory/0"
    with pytest.raises(ValueError, match="rebuilt input"):
        decode(json.dumps(data).encode(), sealed_artifact=sealed)


def test_seal_refuses_trailing_and_all_attribute_removal_and_wrong_scope():
    sealed = artifact([{"index_attributes": [["# of Spaces", "1 Space"], ["# of Spaces", "2 Spaces"]]}])
    for count in [1, 0]:
        data = json.loads(sealed)
        data["records"][0]["indexAttributes"] = data["records"][0]["indexAttributes"][:count]
        with pytest.raises(ValueError, match="rebuilt input"):
            decode(json.dumps(data).encode(), sealed_artifact=sealed)
    original, manifest = _INPUTS[sealed]
    with pytest.raises(ValueError, match="scope"):
        projection.project(sealed, original=original, manifest=manifest,
                           expected_scope={**_SCOPE, "page": 1})


def test_resealed_substituted_input_is_consistent_but_not_source_proof():
    raw = artifact([{"index_attributes": [["# of Spaces", "1 Space"]]}])
    out, _ = decode(raw)
    assert out["integrity"] == "verified_byte_consistency_only"
    assert out["sourceKeyOrigin"] == "operator_supplied"
    assert out["sourceCompleteness"] == "unverified"
    assert out["promotion"] == "not_authorized"


def test_asserted_scope_does_not_classify_or_filter_mixed_tenure_rows():
    raw = artifact([
        {"transactionType": "Sale", "index_attributes": [["# of Spaces", "1 Space"]]},
        {"transactionType": "Lease", "index_attributes": [["# of Spaces", "2 Spaces"]]},
        {"index_attributes": [["# of Spaces", "0 Spaces"]]},
    ])
    out, _ = decode(raw)
    assert out["scope"]["transaction"] == "lease"  # Operator assertion only.
    assert [row["rowTransaction"] for row in out["records"]] == ["unverified"] * 3
    assert [row["availableSpaceCount"]["value"] for row in out["records"]] == [1, 2, 0]


def test_cli_exclusive_private_output_and_no_literals_in_stdout(tmp_path):
    source, target = tmp_path / "in.json", tmp_path / "out.json"
    original_path, manifest_path = tmp_path / "original.json", tmp_path / "manifest.json"
    raw = artifact([{"index_attributes": [["# of Spaces", "12 Spaces"]]}])
    source.write_bytes(raw)
    original, manifest = _INPUTS[raw]
    original_path.write_bytes(original)
    manifest_path.write_bytes(manifest)
    incomplete = [sys.executable, projection.__file__, "--input", str(source), "--out", str(target)]
    assert subprocess.run(incomplete, capture_output=True, check=False).returncode != 0
    assert not target.exists()
    command = [sys.executable, projection.__file__, "--input", str(source), "--out", str(target),
               "--original", str(original_path), "--manifest", str(manifest_path),
               "--source-key", _SCOPE["sourceKey"], "--generation-id", _SCOPE["generationId"],
               "--capture-id", _SCOPE["captureId"], "--page", "0", "--transaction", "lease"]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert "12 Spaces" not in result.stdout
    assert target.stat().st_mode & 0o077 == 0
    before = target.read_bytes()
    assert subprocess.run(command, capture_output=True, text=True, check=False).returncode != 0
    assert target.read_bytes() == before


def test_cli_refuses_edited_artifact_before_output(tmp_path):
    raw = artifact([{"index_attributes": [["# of Spaces", "1 Space"], ["# of Spaces", "2 Spaces"]]}])
    original, manifest = _INPUTS[raw]
    changed = json.loads(raw)
    changed["records"][0]["indexAttributes"].pop()
    source, original_path, manifest_path, target = (tmp_path / name for name in
        ("in.json", "original.json", "manifest.json", "out.json"))
    source.write_text(json.dumps(changed))
    original_path.write_bytes(original)
    manifest_path.write_bytes(manifest)
    command = [sys.executable, projection.__file__, "--input", str(source), "--out", str(target),
               "--original", str(original_path), "--manifest", str(manifest_path),
               "--source-key", _SCOPE["sourceKey"], "--generation-id", _SCOPE["generationId"],
               "--capture-id", _SCOPE["captureId"], "--page", "0", "--transaction", "lease"]
    assert subprocess.run(command, capture_output=True, check=False).returncode != 0
    assert not target.exists()
