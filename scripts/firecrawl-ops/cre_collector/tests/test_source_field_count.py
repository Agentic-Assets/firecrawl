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
    assert candidates(artifact)[id(listing)]["blocked"] == ["available_space_count"]
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
        assert candidates(artifact)[id(listing)]["blocked"] == ["available_space_count"]
    field["omittedCount"] = 0
    for bad in (False, True, None, "0"):
        listing["buildoutFieldObservations"]["malformedAttributeCount"] = bad
        assert candidates(artifact)[id(listing)]["blocked"] == ["available_space_count"]
    listing["buildoutFieldObservations"]["malformedAttributeCount"] = 0
    for bad in (False, -1, 1000, "0"):
        field["literals"][0]["sourceOrdinal"] = bad
        assert candidates(artifact)[id(listing)]["blocked"] == ["available_space_count"]
    field["literals"][0]["sourceOrdinal"] = 999
    assert candidates(artifact)
    for bad in (None, "f" * 63, "F" * 64, "g" * 64):
        assert not qualified_counts(artifact, bad)


def test_provenance_retains_literal_label_ordinal_and_original_clock(tmp_path):
    artifact, listing = fixture(tmp_path, "2 Spaces")
    listing["buildoutFieldObservations"]["availableSpaceCount"]["literals"][0][
        "sourceOrdinal"
    ] = 17
    provenance = candidates(artifact)[id(listing)]["values"]["available_space_count"]
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
    listing["buildoutFieldObservations"] = None
    assert qualified_counts(artifact, "a" * 64) == {}
    for malformed in ([], "observation"):
        listing["buildoutFieldObservations"] = malformed
        assert qualified_counts(artifact, "a" * 64)[id(listing)]["blocked"] == [
            "available_space_count"
        ]
    artifact["sources"][0]["currentInventoryProof"] = []
    assert qualified_counts(artifact, "a" * 64) == {}


def core_fixture(tmp_path, source):
    from source_fields.buildout import _core_definitions

    artifact, lease = fixture(tmp_path, "2 Spaces")
    definitions = _core_definitions()[source]
    for listing in artifact["listings"]:
        listing["sourceKey"] = source
        listing["url"] = listing["url"].replace(
            "www.farislee.com",
            definitions["lease"]["host"]
            if source == "franklin-street"
            else definitions["host"],
        )
        listing["canonicalUrl"] = listing["url"]
    for entry in artifact["sources"]:
        tx = entry["transaction"]
        definition = definitions[tx] if source == "franklin-street" else definitions
        entry["sourceKey"] = source
        proof = entry["currentInventoryProof"]
        proof.update(
            sourceKey=f"{source}-{tx}" if source == "franklin-street" else source,
            pluginKey=definition["plugin"],
            listingsPage=definition["page"],
        )
        for page in proof["pages"]:
            page["endpoint"] = page["endpoint"].replace(
                buildout_plugin(), definition["plugin"]
            )
            for item in page["rows"]:
                item["showLink"] = item["showLink"].replace(
                    "www.farislee.com", definition["host"]
                )
    return artifact, lease


def buildout_plugin():
    from cre_buildout_current_inventory import definitions

    return definitions()["faris-lee"]["plugin"]


def test_core_shared_and_separate_feeds_promote_lease_observation(tmp_path):
    for source in ("svn", "lee-associates", "franklin-street"):
        path = tmp_path / source
        path.mkdir()
        artifact, lease = core_fixture(path, source)
        candidate = candidates(artifact)[id(lease)]["values"]["available_space_count"]
        assert candidate["value"] == 2
        assert candidate["source_key"] == source
        assert candidate["native_id"] == lease["id"]
        assert candidate["url"] == lease["url"]
        assert candidate["native_id"] not in candidate["url"]


def test_franklin_requires_both_complete_distinct_feed_proofs(tmp_path):
    artifact, lease = core_fixture(tmp_path, "franklin-street")
    assert (
        candidates(artifact)[id(lease)]["values"]["available_space_count"]["value"] == 2
    )
    broken = copy.deepcopy(artifact)
    broken["sources"][0]["currentInventoryProof"]["pages"].pop()
    assert not candidates(broken)
    broken = copy.deepcopy(artifact)
    broken["sources"][1]["currentInventoryProof"]["pluginKey"] = broken["sources"][0][
        "currentInventoryProof"
    ]["pluginKey"]
    assert not candidates(broken)
    broken = copy.deepcopy(artifact)
    broken["listings"][1]["inventoryObservedAt"] = broken["runMeta"]["startedAt"]
    assert not candidates(broken)


def franklin_collector_artifact(tmp_path, *, empty_sale=False):
    import os
    import subprocess
    from datetime import UTC, datetime, timedelta
    from pathlib import Path

    from source_fields.buildout import _core_definitions

    definitions = _core_definitions()["franklin-street"]
    bodies = {}
    for tx, native in (("sale", 10), ("lease", 20)):
        item = {
            "id": native,
            "sale": tx == "sale",
            "closed": False,
            "under_contract": False,
            "also_for_sale_or_lease": False,
            "show_link": f"https://www.franklinst.com/listings/?propertyId=common-{tx}",
            "index_attributes": [
                ["# of Spaces", "9 Spaces" if tx == "sale" else "2 Spaces"]
            ],
        }
        items = [] if empty_sale and tx == "sale" else [item]
        bodies[definitions[tx]["plugin"]] = {
            "meta": {"total": len(items), "limit": 30},
            "inventory": items,
        }
    preload = tmp_path / "transport.mts"
    preload.write_text(
        f"const bodies={json.dumps(bodies)};globalThis.fetch=async(url)=>{{const u=new URL(String(url));"
        "const token=u.pathname.split('/')[2];if(u.hostname!=='buildout.com'||!bodies[token])throw new Error('unexpected endpoint');"
        "return new Response(JSON.stringify(bodies[token]));};"
    )
    stamp = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    env = {
        k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG") if k in os.environ
    }
    env.update(
        CRE_REQUIRE_FRESH_DETAILS="1",
        CRE_REFRESH_GENERATION="2026-10-15T120000Z",
        CRE_REFRESH_STARTED_AT=stamp,
        BUILDOUT_CACHE_DIR=str(tmp_path / "cache"),
    )
    artifact_path = tmp_path / "artifact.json"
    result = subprocess.run(
        [
            "node",
            "--import",
            "tsx",
            "--import",
            str(preload),
            "collect.ts",
            "--source",
            "franklin-street",
            "--transaction",
            "both",
            "--max-items",
            "0",
            "--concurrency",
            "1",
            "--out",
            str(artifact_path),
        ],
        cwd=Path(ingest.__file__).parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(artifact_path.read_text())


def test_franklin_actual_offline_collector_to_ingest(tmp_path):
    artifact = franklin_collector_artifact(tmp_path)
    original = copy.deepcopy(artifact)
    qualified = candidates(artifact)
    rows = [
        ingest.to_row(row, {}, artifact["runMeta"]["finishedAt"], qualified)
        for row in artifact["listings"]
    ]
    assert len(rows) == 2
    assert artifact == original
    merged = ingest.merge_rows(*rows)
    assert merged["source_field_count"]["value"] == 2
    assert merged["source_field_count"]["native_id"] == "20"
    # A separately copied artifact object did not receive the qualification.
    detached = copy.deepcopy(artifact["listings"][1])
    assert ingest.to_row(detached, {}, artifact["runMeta"]["finishedAt"], qualified)["source_field_count"] is None
    unsafe = copy.deepcopy(artifact["listings"][1])
    unsafe["url"] = "https://example.test/property?api_key=PRIVATE_EXAMPLE"
    qualified[id(unsafe)] = qualified[id(artifact["listings"][1])]
    assert ingest.to_row(unsafe, {}, artifact["runMeta"]["finishedAt"], qualified) is None
    assert (
        merged["raw_data"]["primary"]["buildoutFieldObservations"][
            "availableSpaceCount"
        ]["value"]
        == 9
    )
    assert (
        merged["raw_data"]["secondary_pass"]["buildoutFieldObservations"][
            "availableSpaceCount"
        ]["value"]
        == 2
    )


def test_franklin_actual_empty_sale_feed_requires_complete_zero_page(tmp_path):
    artifact = franklin_collector_artifact(tmp_path, empty_sale=True)
    sale = next(row for row in artifact["sources"] if row["transaction"] == "sale")
    proof = sale["currentInventoryProof"]
    assert sale["listingsCollected"] == 0
    assert proof["total"] == 0
    assert len(proof["pages"]) == 1
    assert proof["pages"][0]["index"] == 0
    assert proof["pages"][0]["total"] == 0
    assert proof["pages"][0]["rows"] == []
    lease = artifact["listings"][0]
    assert (
        candidates(artifact)[id(lease)]["values"]["available_space_count"]["value"] == 2
    )
    for mutate in (
        "missing_page",
        "bad_index",
        "bad_total",
        "extra_row",
        "missing_proof",
        "wrong_count",
    ):
        broken = copy.deepcopy(artifact)
        entry = next(row for row in broken["sources"] if row["transaction"] == "sale")
        p = entry["currentInventoryProof"]
        if mutate == "missing_page":
            p["pages"] = []
        elif mutate == "bad_index":
            p["pages"][0]["index"] = 1
        elif mutate == "bad_total":
            p["pages"][0]["total"] = 1
        elif mutate == "extra_row":
            p["pages"][0]["rows"] = [{}]
        elif mutate == "missing_proof":
            entry.pop("currentInventoryProof")
        else:
            entry["listingsCollected"] = 1
        assert not candidates(broken), mutate


def test_original_shared_feed_still_rejects_empty_inventory(tmp_path):
    import pytest
    from cre_buildout_current_inventory import definitions
    from source_fields.buildout import validate_inventory

    artifact, _ = fixture(tmp_path)
    artifact["listings"] = []
    artifact["totalListings"] = 0
    for entry in artifact["sources"]:
        entry["listingsCollected"] = 0
        entry["totalAvailableOnSource"] = 0
        proof = entry["currentInventoryProof"]
        proof["total"] = 0
        proof["pages"] = proof["pages"][:1]
        proof["pages"][0]["total"] = 0
        proof["pages"][0]["rows"] = []
    fresh = artifact["runMeta"]["freshness"]
    with pytest.raises(ValueError):
        validate_inventory(
            artifact,
            "faris-lee",
            definitions()["faris-lee"],
            fresh["generationId"],
            fresh["generationStartedAt"],
        )


def test_known_count_conflict_survives_repeat_artifact_merge(tmp_path):
    good, good_listing = fixture(tmp_path, "2 Spaces")
    ambiguous = copy.deepcopy(good)
    bad_listing = next(
        row for row in ambiguous["listings"] if row["transactionMode"] == "lease"
    )
    field = bad_listing["buildoutFieldObservations"]["availableSpaceCount"]
    field.update(
        state="ambiguous",
        literals=[
            {"sourceOrdinal": 0, "literal": "2 Spaces"},
            {"sourceOrdinal": 1, "literal": "3 Spaces"},
        ],
    )
    qualified = candidates(ambiguous)
    assert qualified[id(bad_listing)] == {
        "values": {},
        "blocked": ["available_space_count"],
    }
    stamp = good["runMeta"]["finishedAt"]
    a = ingest.to_row(good_listing, {}, stamp, candidates(good))
    b = ingest.to_row(bad_listing, {}, stamp, qualified)
    for left, right in ((a, b), (b, a)):
        merged = ingest.merge_rows(copy.deepcopy(left), copy.deepcopy(right))
        assert merged["source_field_count"] is None
        assert merged["_source_field_conflict"] is True
        assert ingest.merge_rows(merged, copy.deepcopy(a))["source_field_count"] is None
    del bad_listing["buildoutFieldObservations"]["availableSpaceCount"]
    assert id(bad_listing) not in candidates(ambiguous)


def test_mapper_cap_rate_only_missing_count_is_no_op_across_merges(tmp_path):
    import subprocess
    from pathlib import Path

    mapper = subprocess.run(
        [
            "node",
            "--import",
            "tsx",
            "--input-type=module",
            "-e",
            (
                "import {buildoutFieldObservations} from './sources/buildout-field-observations.ts';"
                'console.log(JSON.stringify(buildoutFieldObservations([["Cap Rate","6%"]])));'
            ),
        ],
        cwd=Path(ingest.__file__).parent,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert mapper.returncode == 0, mapper.stderr
    observation = json.loads(mapper.stdout)
    assert observation["availableSpaceCount"] == {"state": "missing", "omittedCount": 0}
    assert observation["malformedAttributeCount"] == 0
    good, good_listing = fixture(tmp_path, "2 Spaces")
    missing = copy.deepcopy(good)
    missing_listing = next(
        row for row in missing["listings"] if row["transactionMode"] == "lease"
    )
    missing_listing["buildoutFieldObservations"] = observation
    assert id(missing_listing) not in candidates(missing)
    stamp = good["runMeta"]["finishedAt"]
    a = ingest.to_row(good_listing, {}, stamp, candidates(good))
    b = ingest.to_row(missing_listing, {}, stamp, candidates(missing))
    for left, right in ((a, b), (b, a)):
        merged = ingest.merge_rows(copy.deepcopy(left), copy.deepcopy(right))
        assert merged["source_field_count"]["value"] == 2
        assert not merged["_source_field_conflict"]
    for change in ("omitted", "malformed", "literals", "value", "missing_count"):
        inconsistent = copy.deepcopy(observation)
        field = inconsistent["availableSpaceCount"]
        if change == "omitted":
            field["omittedCount"] = 1
        elif change == "malformed":
            inconsistent["malformedAttributeCount"] = 1
        elif change == "literals":
            field["literals"] = [{"sourceOrdinal": 0, "literal": "2 Spaces"}]
        elif change == "value":
            field["value"] = 2
        else:
            field.pop("omittedCount")
        missing_listing["buildoutFieldObservations"] = inconsistent
        assert candidates(missing)[id(missing_listing)]["blocked"] == [
            "available_space_count"
        ], change


def test_staging_redaction_of_qualified_literal_becomes_sticky_hold(tmp_path):
    import os
    import re
    import subprocess
    import sys

    artifact = franklin_collector_artifact(tmp_path)
    target = artifact["listings"][1]
    document = "https://buildout.com/docs/abc/123?token=Spaces"
    target["rawBuildout"]["pdf_url"] = document
    target["brochures"] = [{"url": document, "name": "Brochure"}]
    original = copy.deepcopy(artifact)
    qualified = candidates(artifact)
    assert qualified[id(target)]["values"]["available_space_count"]["value"] == 2
    rows = [ingest.to_row(row, {}, artifact["runMeta"]["finishedAt"], qualified)
            for row in artifact["listings"]]
    assert rows[1]["source_field_count"] is None
    assert rows[1]["_source_field_conflict"] is True
    merged = ingest.merge_rows(*rows)
    assert merged["source_field_count"] is None
    assert merged["_source_field_conflict"] is True
    assert artifact == original
    path = tmp_path / "modified-artifact.json"
    path.write_text(json.dumps(artifact))
    result = subprocess.run([sys.executable, "cre_ingest.py", "--in", str(path), "--dry-run"],
                            capture_output=True, text=True, env={**os.environ, "TMPDIR": str(tmp_path)}, check=True)
    match = re.search(r"^sql: (.+?) \(", result.stderr, re.MULTILINE)
    assert match is not None
    with open(match.group(1)) as staged:
        sql = staged.read()
    assert "token=Spaces" not in sql
    assert "2 Spaces" not in sql
