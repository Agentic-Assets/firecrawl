"""No provider access: exercise saved synthetic JSON and private artifacts."""
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

import buildout_observation_artifact as builder


def raw_page(count=80):
    rows = []
    for i in range(count):
        row = {"id": i, "show_link": f"https://example.test/?propertyId={i}-sale",
               "nestedUnknown": {"values": [False, None, {"unit": "SF", "amount": 42}]},
               "index_attributes": [["Rate", "$12.50/SF/yr"], ["Custom", False], ["Custom", None],
                                    ["Area", {"value": 123, "unit": "acres"}]]}
        if i % 2:
            row.pop("id")
            row.pop("show_link")
        rows.append(row)
    return {"page": 0, "inventory": rows, "unknownPageFact": ["one", False]}


def run(page, kind="buildout-page"):
    raw = json.dumps(page).encode()
    encoded, summary = builder.build_artifact(raw, source_key="fortis-net-lease", input_kind=kind)
    return json.loads(encoded, parse_float=Decimal), summary


def test_eighty_records_keep_nested_values_duplicates_and_unresolved_identity():
    data = raw_page()
    document, summary = run(data)
    assert summary["inputRecords"] == summary["retainedRecords"] == 80
    assert summary["status"] == "observed_only"
    assert document["provenance"]["sourceCompleteness"] == "unverified"
    assert document["provenance"]["confidentiality"] == "unreviewed_private_data"
    assert document["pageFields"]["unknownPageFact"] == ["one", False]
    for i, record in enumerate(document["records"]):
        assert record["identityResolution"] == "unresolved"
        assert record["sourceFields"]["nestedUnknown"] == data["inventory"][i]["nestedUnknown"]
        assert [v["literal"] for v in record["indexAttributes"]] == data["inventory"][i]["index_attributes"]
        assert ("id" in record["sourceFields"]) is (i % 2 == 0)


def test_credentials_and_malformed_pairs_are_quarantined_with_source_locations():
    data = {"inventory": [{"safe": {"detail": ["text", None]}, "Authorization": "Bearer unsafe",
                           "index_attributes": [["Price", "$5"], ["API Key", "secret"],
                                                ["Link", "https://example.test/?%74oken=unsafe"],
                                                ["broken"], ["Rate", False]],
                           "nested": {"safe": "yes", "private key": "unsafe"}}]}
    doc, summary = run(data)
    assert summary["status"] == "partial" and summary["quarantinedItems"] == 5
    row = doc["records"][0]
    assert row["sourceFields"] == {"safe": {"detail": ["text", None]}, "nested": {"safe": "yes"}}
    assert [v["sourceOrdinal"] for v in row["indexAttributes"]] == [0, 2, 4]
    assert row["indexAttributes"][1]["literal"][1] == {"$buildoutQuarantined": True}
    assert sorted(q["fieldOrdinal"] for q in doc["quarantine"] if "fieldOrdinal" in q) == [1, 1]
    assert "unsafe" not in json.dumps(doc)
    assert "index_attributes/1" in json.dumps(doc)


def test_decimal_precision_and_raw_input_digest():
    original = b'{"inventory":[{"index_attributes":[["fraction",1234567890.12345678901234567890]]}]}'
    encoded, _ = builder.build_artifact(original, source_key="fortis-net-lease", input_kind="buildout-page")
    doc = json.loads(encoded, parse_float=Decimal)
    assert doc["records"][0]["indexAttributes"][0]["literal"][1] == Decimal("1234567890.12345678901234567890")
    assert doc["provenance"]["inputIsByteExactAuthority"] is True
    assert len(doc["provenance"]["inputSha256"]) == 64


def test_retained_observation_upstream_omissions_remain_visible():
    retained = [{"id": "1", "url": "https://example.test/a", "transactionType": "Sale",
                 "buildoutInventoryObservation": {"attributes": [["Custom", False]], "omittedCount": 2}}]
    doc, summary = run(retained, kind="retained-observations")
    assert doc["records"][0]["sourceFields"]["buildoutInventoryObservation"] == retained[0]["buildoutInventoryObservation"]
    assert summary["upstreamReportedOmissions"] == 2 and summary["status"] == "partial"


@pytest.mark.parametrize("raw", [b'{"inventory":[],"inventory":[]}', b'{"inventory":[NaN]}', b'{"inventory":[' + b'9'*1001 + b']}'])
def test_ambiguous_or_unbounded_json_rejected(raw):
    with pytest.raises(ValueError):
        builder.build_artifact(raw, source_key="fortis-net-lease", input_kind="buildout-page")


def test_bounds_and_bad_structure_fail_closed(monkeypatch):
    monkeypatch.setattr(builder, "MAX_INPUT_BYTES", 16)
    with pytest.raises(ValueError, match="input exceeds"):
        builder.build_artifact(b" " * 17, source_key="fortis-net-lease", input_kind="buildout-page")
    with pytest.raises(ValueError, match="registered"):
        builder.build_artifact(b'{}', source_key="jll", input_kind="buildout-page")


def test_private_exclusive_output_and_cli_no_data_in_stdout(tmp_path):
    source = tmp_path / "feed.json"
    source.write_text(json.dumps(raw_page(1)))
    output = tmp_path / "artifact.json"
    script = Path(builder.__file__)
    command = [sys.executable, str(script), "--input", str(source), "--input-kind", "buildout-page",
               "--source-key", "fortis-net-lease", "--out", str(output)]
    result = subprocess.run(command, text=True, capture_output=True, check=True)
    assert "example.test" not in result.stdout and '"inputRecords": 1' in result.stdout
    assert output.stat().st_mode & 0o077 == 0
    before = output.read_bytes()
    assert subprocess.run(command, text=True, capture_output=True, check=False).returncode != 0
    assert output.read_bytes() == before
    link = tmp_path / "link.json"
    link.symlink_to(output)
    with pytest.raises(FileExistsError):
        builder.write_private(link, b"replacement")
    assert output.read_bytes() == before


def test_unverifiable_urls_and_secret_keys_are_quarantined_without_losing_siblings():
    data = {"inventory": [{"nested": {"count": 7, "token": "secret-value", "valid": [False, None]},
                           "index_attributes": [["Link", "https://example.test/?bad=%FF"],
                                                ["ordinary", "Secretary rents Basic space"]]}]}
    doc, summary = run(data)
    assert summary["quarantinedItems"] == 2
    assert doc["records"][0]["sourceFields"]["nested"] == {"count": 7, "valid": [False, None]}
    assert doc["records"][0]["indexAttributes"][0]["literal"][1] == {"$buildoutQuarantined": True}
    assert doc["records"][0]["indexAttributes"][1]["literal"][1] == "Secretary rents Basic space"


def test_recognized_nested_attribute_labels_are_quarantined_recursively():
    raw_input = {"inventory": [{"index_attributes": [["Metadata", [["API Key", "dummy-credential"], ["Area", 4]]]],
                                 "other": ["API Key", "ordinary pair outside attribute context"]}]}
    doc, summary = run(raw_input)
    nested = doc["records"][0]["indexAttributes"][0]["literal"][1]
    assert nested == [{"$buildoutQuarantined": True}, ["Area", 4]]
    assert doc["records"][0]["sourceFields"]["other"] == ["API Key", "ordinary pair outside attribute context"]
    assert summary["quarantinedItems"] == 1
    assert "dummy-credential" not in json.dumps(doc)
    retained = [{"buildoutInventoryObservation": {"attributes": [["API Key", "dummy-credential"], ["Area", 4]], "omittedCount": 0}}]
    doc, summary = run(retained, kind="retained-observations")
    assert doc["records"][0]["sourceFields"]["buildoutInventoryObservation"]["attributes"] == [{"$buildoutQuarantined": True}, ["Area", 4]]
    assert summary["quarantinedItems"] == 1
    assert "dummy-credential" not in json.dumps(doc)


def test_nested_index_attributes_are_recognized_at_any_depth():
    input_page = {"inventory": [{"nested": {"safe": "retained", "index_attributes": [
        ["API Key", "dummy-secret"], ["Area", 4], ["Metadata", [["token", "nested-secret"], ["units", "SF"]]],
    ]}}]}
    doc, summary = run(input_page)
    fields = doc["records"][0]["sourceFields"]["nested"]
    assert fields["safe"] == "retained"
    assert fields["index_attributes"] == [
        {"$buildoutQuarantined": True}, ["Area", 4],
        ["Metadata", [{"$buildoutQuarantined": True}, ["units", "SF"]]],
    ]
    assert summary["quarantinedItems"] == 2 and summary["status"] == "partial"
    assert "dummy-secret" not in json.dumps(doc) and "nested-secret" not in json.dumps(doc)
    assert {q["pointer"] for q in doc["quarantine"]} == {
        "/inventory/0/nested/index_attributes/0",
        "/inventory/0/nested/index_attributes/2/1/0",
    }


def test_page_level_index_attributes_also_use_attribute_context():
    page = {"index_attributes": [["API Key", "page-secret"], ["Area", 4]], "inventory": []}
    doc, summary = run(page)
    assert doc["pageFields"]["index_attributes"] == [{"$buildoutQuarantined": True}, ["Area", 4]]
    assert summary["quarantinedItems"] == 1
    assert "page-secret" not in json.dumps(doc)
    assert doc["quarantine"][0]["pointer"] == "/index_attributes/0"


def test_plural_and_camel_case_credential_labels_do_not_expose_values():
    labels = ["credentials", "apiKeys", "authorizationHeader", "sessionToken", "tokens", "passwords", "APIKeys"]
    data = {"inventory": [{"nested": {**{label: "dummy-value" for label in labels},
                                      "Secretary": "Jane", "Tokenization": "ordinary", "Sessional": "normal",
                                      "Session Street": "Main", "Token Street": "North"},
                            "index_attributes": [[label, "dummy-value"] for label in labels] + [["Secretary", "Jane"]]}]}
    doc, summary = run(data)
    fields = doc["records"][0]["sourceFields"]["nested"]
    assert fields == {"Secretary": "Jane", "Tokenization": "ordinary", "Sessional": "normal",
                      "Session Street": "Main", "Token Street": "North"}
    assert [entry["literal"] for entry in doc["records"][0]["indexAttributes"]] == [["Secretary", "Jane"]]
    assert summary["quarantinedItems"] == 2 * len(labels)
    assert "dummy-value" not in json.dumps(doc)


def test_nested_malformed_attribute_pairs_are_quarantined_without_reclassifying_unrelated_arrays():
    data = {"inventory": [{"nested": {"index_attributes": [["API Key", "dummy-secret", "extra"],
                                                                  ["Rate", "$5", "extra"], [123, "invalid-label-secret"], ["Area", 4]]},
                            "index_attributes": [["Metadata", [["API Key", "nested-secret", "extra"],
                                                                  ["ordinary", 4, 5]]]],
                            "other": ["Rate", "$5", "extra"]}]}
    doc, summary = run(data)
    row = doc["records"][0]
    assert row["sourceFields"]["nested"]["index_attributes"] == [
        {"$buildoutQuarantined": True}, {"$buildoutQuarantined": True},
        {"$buildoutQuarantined": True}, ["Area", 4]]
    assert row["indexAttributes"][0]["literal"][1] == [{"$buildoutQuarantined": True}, ["ordinary", 4, 5]]
    assert row["sourceFields"]["other"] == ["Rate", "$5", "extra"]
    assert summary["quarantinedItems"] == 4
    assert "dummy-secret" not in json.dumps(doc) and "nested-secret" not in json.dumps(doc)
    assert "invalid-label-secret" not in json.dumps(doc)
