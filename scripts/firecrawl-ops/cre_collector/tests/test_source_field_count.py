import copy
import hashlib
import json

import cre_ingest as ingest
from source_fields.buildout import qualified_counts
from source_fields.sql import RESERVED_KEY
from test_buildout_current_inventory import setup


def fixture(tmp_path, literal="0 Spaces"):
    _, _, artifact, _, _ = setup(tmp_path)
    listing = next(
        row for row in artifact["listings"] if row["transactionMode"] == "lease"
    )
    listing["buildoutFieldObservations"] = {
        "schema": "buildout-field-observations-v1",
        "malformedAttributeCount": 0,
        "availableSpaceCount": {
            "state": "observed",
            "omittedCount": 0,
            "literals": [{"sourceOrdinal": 0, "literal": literal}],
            "value": int(literal.split()[0]),
        },
    }
    return artifact, listing


def candidates(artifact):
    ingest.validate_strict_artifact_freshness(artifact)
    raw = json.dumps(artifact).encode()
    return qualified_counts(artifact, hashlib.sha256(raw).hexdigest())


def test_complete_bound_lease_and_zero(tmp_path):
    artifact, listing = fixture(tmp_path)
    row = ingest.to_row(
        listing, {}, artifact["runMeta"]["finishedAt"], candidates(artifact)
    )
    assert row["source_field_count"]["value"] == 0
    assert row["source_field_count"]["native_id"] == listing["id"]
    assert row["source_field_count"]["url"] == listing["url"]
    assert row["source_field_count"]["tenure"] == "lease"


def test_optional_qualification_refuses_malformed_provenance(tmp_path):
    artifact, listing = fixture(tmp_path)
    for malformed in ("invalid", ["invalid"], True):
        listing["freshnessProvenance"] = malformed
        raw = json.dumps(artifact).encode()
        assert qualified_counts(artifact, hashlib.sha256(raw).hexdigest()) == {}


def test_source_proof_and_literals_must_agree(tmp_path):
    artifact, listing = fixture(tmp_path, "2 Spaces")
    listing["buildoutFieldObservations"]["availableSpaceCount"]["value"] = 3
    assert not candidates(artifact)
    listing["buildoutFieldObservations"]["availableSpaceCount"]["value"] = 2
    artifact["sources"][0]["currentInventoryProof"]["pages"][0]["rows"][0][
        "showLink"
    ] = "https://example.org/other"
    assert not candidates(artifact)


def test_reserved_key_and_conflicting_merges(tmp_path):
    artifact, listing = fixture(tmp_path)
    listing["extraFacts"] = {RESERVED_KEY: {"forged": True}, "other": "kept"}
    row = ingest.to_row(
        listing, {}, artifact["runMeta"]["finishedAt"], candidates(artifact)
    )
    assert row["extra_facts"] == {"other": "kept"}
    other = copy.deepcopy(row)
    other["source_field_count"]["value"] = 1
    for left, right in ((row, other), (other, row)):
        merged = ingest.merge_rows(copy.deepcopy(left), copy.deepcopy(right))
        assert merged["source_field_count"] is None


def test_sale_observation_does_not_replace_lease_in_either_order(tmp_path):
    artifact, lease = fixture(tmp_path, "2 Spaces")
    sale = next(row for row in artifact["listings"] if row["transactionMode"] == "sale")
    sale["buildoutFieldObservations"] = copy.deepcopy(
        lease["buildoutFieldObservations"]
    )
    sale["buildoutFieldObservations"]["availableSpaceCount"].update(
        literals=[{"sourceOrdinal": 0, "literal": "9 Spaces"}], value=9
    )
    qualified = candidates(artifact)
    stamp = artifact["runMeta"]["finishedAt"]
    a = ingest.to_row(sale, {}, stamp, qualified)
    b = ingest.to_row(lease, {}, stamp, qualified)
    assert a["external_id"] == b["external_id"]
    for left, right in ((a, b), (b, a)):
        merged = ingest.merge_rows(copy.deepcopy(left), copy.deepcopy(right))
        assert merged["transaction_type"] == "sale_or_lease"
        assert merged["source_field_count"]["value"] == 2


def test_invalid_count_types_ordinals_and_artifact_hash_are_rejected(tmp_path):
    artifact, listing = fixture(tmp_path, "2 Spaces")
    field = listing["buildoutFieldObservations"]["availableSpaceCount"]
    for bad in (False, True, None, "0"):
        field["omittedCount"] = bad
        assert not candidates(artifact)
    field["omittedCount"] = 0
    for bad in (False, True, None, "0"):
        listing["buildoutFieldObservations"]["malformedAttributeCount"] = bad
        assert not candidates(artifact)
    listing["buildoutFieldObservations"]["malformedAttributeCount"] = 0
    for bad in (False, -1, 1000, "0"):
        field["literals"][0]["sourceOrdinal"] = bad
        assert not candidates(artifact)
    field["literals"][0]["sourceOrdinal"] = 999
    assert candidates(artifact)
    for bad in (None, "f" * 63, "F" * 64, "g" * 64):
        assert not qualified_counts(artifact, bad)


def test_provenance_retains_literal_label_ordinal_and_original_clock(tmp_path):
    artifact, listing = fixture(tmp_path, "2 Spaces")
    listing["buildoutFieldObservations"]["availableSpaceCount"]["literals"][0][
        "sourceOrdinal"
    ] = 17
    provenance = candidates(artifact)[id(listing)]
    assert provenance["schema_version"] == "cre-source-field-observation/v1"
    assert provenance["field"] == "available_space_count"
    assert provenance["source_label"] == "# of Spaces"
    assert provenance["source_ordinal"] == 17
    assert provenance["original_observed_at"] == listing["inventoryObservedAt"]


def test_malformed_optional_proof_and_field_are_not_promoted(tmp_path):
    artifact, listing = fixture(tmp_path, "2 Spaces")
    proof = artifact["sources"][0]["currentInventoryProof"]
    original_pages = proof["pages"]
    for malformed in (None, [], "page"):
        proof["pages"] = [malformed]
        assert qualified_counts(artifact, "a" * 64) == {}
    proof["pages"] = original_pages
    for malformed in (None, [], "observation"):
        listing["buildoutFieldObservations"] = malformed
        assert qualified_counts(artifact, "a" * 64) == {}
    artifact["sources"][0]["currentInventoryProof"] = []
    assert qualified_counts(artifact, "a" * 64) == {}
