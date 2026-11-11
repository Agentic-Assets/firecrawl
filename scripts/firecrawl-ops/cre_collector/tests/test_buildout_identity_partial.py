"""Offline input and SQL-shape checks for the Buildout partial probe."""

import copy
import json

import pytest
from test_buildout_current_inventory import CODE, SOURCE, TARGET, setup

from source_fields.buildout_partial import (
    identity_probe_sql,
    prepare_partition,
    validate_probe,
    verify_probe,
)


def partition(tmp_path):
    _run, path, _artifact, manifest, _ = setup(tmp_path)
    return prepare_partition(path.read_bytes(), manifest, SOURCE, code_sha=CODE,
                             dirty=False, target_sha=TARGET, attempt_id="a" * 32)


def test_partial_partition_retains_exact_merged_identity_and_input_binding(tmp_path):
    result = partition(tmp_path)
    assert [row["external_id"] for row in result["staged"]] == result["scope"]["staged_external_ids"]
    dual = next(row for row in result["staged"] if row["external_id"] == "dual")
    assert dual["native_ids"] == ["10", "20"]
    assert result["attempt_id"] == "a" * 32
    assert len(result["staged_sha256"]) == 64


def test_partial_probe_reuses_real_guard_without_mutating_source_rows(tmp_path):
    result = partition(tmp_path)
    sql = identity_probe_sql(result)
    assert "_buildout_identity_held AS MATERIALIZED" in sql
    assert "credeals.cre_listing_contacts" in sql
    assert "credeals.cre_listing_documents" in sql
    assert "credeals.cre_document_" not in sql
    assert "DELETE FROM _src" not in sql
    assert "FOR UPDATE" not in sql
    assert "CREATE " not in sql


def test_partial_probe_refuses_changed_staged_inputs(tmp_path):
    result = partition(tmp_path)
    changed = copy.deepcopy(result)
    changed["staged"][0]["native_ids"] = ["999"]
    with pytest.raises(ValueError, match="changed"):
        identity_probe_sql(changed)


def test_partial_partition_rejects_unbound_attempt(tmp_path):
    _run, path, _artifact, manifest, _ = setup(tmp_path)
    with pytest.raises(ValueError, match="attempt"):
        prepare_partition(path.read_bytes(), manifest, SOURCE, code_sha=CODE,
                          dirty=False, target_sha=TARGET, attempt_id="invalid")


def test_partial_probe_checks_exact_identity_partition_and_preimages(tmp_path):
    result = partition(tmp_path)
    ids = result["scope"]["staged_external_ids"]
    broker = "11111111-1111-4111-8111-111111111111"
    listing = "22222222-2222-4222-8222-222222222222"
    children = {key: [] for key in ("contacts", "documents", "images", "media", "links")}
    preimage = [{"brokerage_id": broker, "external_id": key,
                 "listing": {"id": listing, "brokerage_id": broker, "external_id": key} if key == "prior-key" else None,
                 "source_index": None, "children": children} for key in ids + ["prior-key"]]
    row = {"brokerage_id": broker, "staged": str(len(ids)), "held": json.dumps([ids[0]]),
           "conflicts": json.dumps([{"held_external_id": ids[0], "related_external_id": "prior-key"}]),
           "preimage": json.dumps(preimage)}
    accepted = validate_probe(result, row)
    assert accepted["held"] == [ids[0]]
    assert accepted["preimage"] == row["preimage"]
    assert verify_probe(result, accepted) == accepted
    for key, value in (("native_ids", ["999"]), ("raw_data", {"changed": True}),
                       ("source_url", "https://example.com/changed"),
                       ("transaction_type", "changed"), ("scraped_at", "2000-01-01T00:00:00Z")):
        changed_partition = copy.deepcopy(result)
        changed_partition["staged"][0][key] = value
        with pytest.raises(ValueError):
            verify_probe(changed_partition, accepted)
    changed_proof = {**accepted, "held": []}
    with pytest.raises(ValueError):
        verify_probe(result, changed_proof)
    for key, value in (("conflicts", []), ("preimage", row["preimage"] + " "),
                       ("scope", {}), ("attempt_id", "b" * 32)):
        with pytest.raises(ValueError):
            verify_probe(result, {**accepted, key: value})
    for mutation in ("missing", "duplicate", "foreign", "wrong_broker", "bad_child", "missing_related"):
        changed = copy.deepcopy(preimage)
        if mutation == "missing":
            changed.pop()
        elif mutation == "duplicate":
            changed.append(copy.deepcopy(changed[0]))
        elif mutation == "foreign":
            changed[-1]["external_id"] = "foreign"
        elif mutation == "wrong_broker":
            changed[-1]["brokerage_id"] = "33333333-3333-4333-8333-333333333333"
        elif mutation == "bad_child":
            changed[0]["children"]["images"] = [{"listing_id": None}]
        else:
            changed[-1]["listing"] = None
        with pytest.raises(ValueError):
            validate_probe(result, {**row, "preimage": json.dumps(changed)})


def test_partial_probe_rejects_held_and_conflict_tampering(tmp_path):
    result = partition(tmp_path)
    ids = result["scope"]["staged_external_ids"]
    broker = "11111111-1111-4111-8111-111111111111"
    children = {key: [] for key in ("contacts", "documents", "images", "media", "links")}
    preimage = [{"brokerage_id": broker, "external_id": key, "listing": None,
                 "source_index": None, "children": children} for key in ids]
    row = {"brokerage_id": broker, "staged": str(len(ids)), "held": "[]", "conflicts": "[]", "preimage": json.dumps(preimage)}
    for held, conflicts in ((["foreign"], []), ([ids[0], ids[0]], []),
                            ([ids[0]], [{"held_external_id": ids[1], "related_external_id": "prior"}])):
        with pytest.raises(ValueError):
            validate_probe(result, {**row, "held": json.dumps(held), "conflicts": json.dumps(conflicts)})
