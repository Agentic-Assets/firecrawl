import base64
import copy
import json

import pytest

from cre_marcus_inventory import partition_inventory, stage_inventory

START = "2026-11-05T18:49:40Z"
FINISH = "2026-11-05T19:08:55Z"
GENERATION = "generation"


def listing(native="187396", activity="activity-1"):
    path = f"/properties/{native}/source-name"
    url = f"https://www.marcusmillichap.com{path}"
    tile = (f'<div class="mm-tile" data-dealid="{native}" data-activityid="{activity}">'
            f'<a href="{path}"><h2>Source title</h2><h2>Second title</h2></a></div>')
    return {"id": native, "activityId": activity, "sourceKey": "marcus-millichap", "url": url,
            "inventoryObservedAt": "2026-11-05T18:50:00Z",
            "freshnessProvenance": {"generationId": GENERATION},
            "rawMarcusSearchRow": {"ActivityId": activity, "PropertyUrl": path,
                "Latitude": "34.1", "Longitude": "-118.2", "rawMarcusMapDetail": {
                    "ActivityId": activity, "PropertyUrl": path, "PropertyDetail": tile}},
            "detailError": "detail HTML did not contain a verified property page"}


def stage(row):
    return stage_inventory(row, GENERATION, START, FINISH)


def test_inventory_preserves_unverified_observation_and_first_source_title():
    source = listing()
    staged = stage(source)
    assert staged["title"] == "Source title"
    assert staged["detail_qualified"] is False
    assert staged["detail_error"] is True
    assert (staged["lat"], staged["lng"]) == (34.1, -118.2)
    assert json.loads(base64.b64decode(staged["observation_base64"])) == {"listing": source, "brokers": []}


@pytest.mark.parametrize("mutation", [
    lambda row: row.update(id="999"),
    lambda row: row.update(url="https://example.com/properties/187396/source-name"),
    lambda row: row["rawMarcusSearchRow"].update(ActivityId="another"),
    lambda row: row["rawMarcusSearchRow"]["rawMarcusMapDetail"].update(PropertyUrl="/properties/999/other"),
    lambda row: row["freshnessProvenance"].update(generationId="different"),
    lambda row: row.update(inventoryObservedAt="2026-11-05T19:09:00Z"),
])
def test_mismatched_identity_or_custody_is_held(mutation):
    row = listing()
    mutation(row)
    with pytest.raises(ValueError):
        stage(row)


def test_invalid_witness_cannot_hide_a_conflict_with_valid_peer():
    valid = listing()
    invalid = copy.deepcopy(valid)
    invalid["activityId"] = "different"
    result = partition_inventory([valid, invalid, listing("123", "activity-2")], GENERATION, START, FINISH)
    assert [item["external_id"] for item in result["staged"]] == ["123"]
    assert {item["index"] for item in result["held"]} == {0, 1}


def test_missing_title_and_invalid_coordinates_do_not_discard_identity():
    row = listing()
    raw = row["rawMarcusSearchRow"]
    raw["Latitude"] = "NaN"
    raw["rawMarcusMapDetail"]["PropertyDetail"] = raw["rawMarcusMapDetail"]["PropertyDetail"].replace(
        "<h2>Source title</h2><h2>Second title</h2>", "")
    staged = stage(row)
    assert staged["title"] is None
    assert staged["lat"] is None and staged["lng"] is None


def test_settled_artifact_binds_attempt_bytes_and_preserves_coordinator_clock(tmp_path):
    from datetime import UTC, datetime, timedelta
    import hashlib
    from cre_marcus_inventory import rederive_settled_artifact

    run = tmp_path / GENERATION
    rejected = run / "rejected"
    rejected.mkdir(parents=True, mode=0o700)
    now = datetime.now(UTC)
    generation_start = (now - timedelta(minutes=3)).isoformat()
    attempt_start = (now - timedelta(minutes=2)).isoformat()
    parent_finish = (now - timedelta(seconds=2)).isoformat()
    child_finish = (now - timedelta(seconds=1)).isoformat()
    row = listing()
    row["inventoryObservedAt"] = (now - timedelta(minutes=1)).isoformat()
    data = {"runMeta": {"startedAt": attempt_start, "finishedAt": child_finish,
                       "freshness": {"generationId": GENERATION, "generationStartedAt": generation_start}},
            "brokers": [], "sources": [{"sourceKey": "marcus-millichap", "transaction": "sale", "listingsCollected": 1,
                         "freshness": {"detailErrors": 1}}], "listings": [row], "totalListings": 1}
    raw = json.dumps(data).encode()
    artifact = rejected / "marcus-millichap-attempt-1.json"
    artifact.write_bytes(raw)
    artifact.chmod(0o600)
    digest = hashlib.sha256(raw).hexdigest()
    attempt = {"number": 1, "rc": 0, "started_at": attempt_start, "finished_at": parent_finish,
               "error": "marcus-millichap/sale freshness metric detailErrors must be zero",
               "rejected_artifact": "rejected/marcus-millichap-attempt-1.json", "rejected_artifact_sha256": digest}
    manifest = {"run_id": GENERATION, "started_at": generation_start, "collector_git_sha": "a" * 40,
                "collector_git_dirty": False, "preflight": {"database_target": {"algorithm": "sha256", "value": "b" * 64}},
                "sources": {"marcus-millichap": {"attempts": [attempt]}}}
    result = rederive_settled_artifact(run, manifest, 1, "a" * 40, False)
    assert result["finished_at"] == child_finish
    assert result["coordinator_finished_at"] == parent_finish
    assert result["detail_qualified"] == 0 and result["detail_errors"] == 1
    assert len(result["partition"]["staged"]) == 1
    attempt["rejected_artifact_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="artifact changed"):
        rederive_settled_artifact(run, manifest, 1, "a" * 40, False)
    attempt["rejected_artifact_sha256"] = digest
    attempt["rc"] = 1
    with pytest.raises(ValueError, match="unsettled"):
        rederive_settled_artifact(run, manifest, 1, "a" * 40, False)


def test_trailing_slash_alias_is_a_conflicting_identity_witness():
    first = listing()
    conflicting = listing("123", "activity-2")
    conflicting["url"] = first["url"] + "/"
    result = partition_inventory([first, conflicting], GENERATION, START, FINISH)
    assert not result["staged"]
    assert {item["index"] for item in result["held"]} == {0, 1}


def test_probe_refuses_derived_field_tampering_and_duplicate_alias():
    from cre_marcus_inventory_db import identity_probe_sql

    row = stage(listing())
    tampered = dict(row, title="invented")
    with pytest.raises(ValueError, match="inconsistent"):
        identity_probe_sql([tampered], GENERATION, START, FINISH)
    with pytest.raises(ValueError, match="duplicate"):
        identity_probe_sql([row, row], GENERATION, START, FINISH)
    sql = identity_probe_sql([row], GENERATION, START, FINISH)
    assert "cre_source_index" in sql and "cre_listings" in sql
    assert "INSERT " not in sql and "UPDATE " not in sql and "DELETE " not in sql


def test_additive_sql_rechecks_partition_and_keeps_unverified_detail_separate():
    from cre_marcus_inventory_db import build_partial_sql

    row = stage(listing())
    receipt = {"schema": "marcus-inventory-custody/v1", "source": "marcus-millichap", "coverage": "partial",
               "generation": GENERATION, "attempt": 1, "source_sha256": "a" * 64,
               "detail_qualified": 0, "detail_errors": 1, "started_at": START, "finished_at": FINISH,
               "partition": {"staged": [row], "held": []}}
    preimage = [{"external_id": row["external_id"], "listing": None, "source_index": None, "children": {"contacts": [], "documents": [], "images": [], "media": [], "links": []}}]
    sql = build_partial_sql([row], [], preimage, receipt, "e1d58a66-76e3-4b13-bdc1-0d67c852df09")
    assert "Marcus identity partition changed" in sql
    assert "'detailQualified',false" in sql
    assert "'partial'" in sql
    assert "country,scraped_at,raw_data" in sql
    assert sql.count(row["observation_base64"]) == 1
    assert "UPDATE credeals.cre_source_index" not in sql
    assert "DELETE FROM" not in sql
    receipt["detail_errors"] = "0); SELECT 1; --"
    with pytest.raises(ValueError):
        build_partial_sql([row], [], preimage, receipt, "e1d58a66-76e3-4b13-bdc1-0d67c852df09")


def test_readback_covers_parent_index_and_all_five_children():
    from cre_marcus_inventory_db import partial_readback_sql

    row = stage(listing())
    receipt = {"schema": "marcus-inventory-custody/v1", "source": "marcus-millichap", "coverage": "partial",
               "generation": GENERATION, "attempt": 1, "source_sha256": "a" * 64,
               "detail_qualified": 0, "detail_errors": 1, "started_at": START, "finished_at": FINISH,
               "partition": {"staged": [row], "held": []}}
    children = {name: [] for name in ("contacts", "documents", "images", "media", "links")}
    preimage = [{"external_id": row["external_id"], "listing": None, "source_index": None, "children": children}]
    sql = partial_readback_sql([row], [], preimage, receipt, "e1d58a66-76e3-4b13-bdc1-0d67c852df09")
    assert "source_index IS DISTINCT FROM" in sql and "children IS DISTINCT FROM" in sql
    assert "listing->>'scraped_at' IS NOT NULL" in sql
    assert all(f"cre_listing_{name}" in sql for name in children)
    assert "'detailQualified'" not in sql  # expected JSON is bound as data, not SQL code


def test_candidate_exposes_inventory_without_claiming_detail_or_coverage():
    from cre_marcus_inventory import candidate_data

    row = stage(listing())
    receipt = {"schema": "marcus-inventory-custody/v1", "generation": GENERATION,
               "generation_started_at": START, "attempt": 1, "started_at": START, "finished_at": FINISH,
               "coordinator_finished_at": FINISH, "source_sha256": "a" * 64, "source_bytes": 100,
               "producer_sha": "b" * 40, "target_sha256": "c" * 64, "detail_errors": 1,
               "partition": {"staged": [row], "held": []}}
    candidate = candidate_data(receipt)
    assert candidate["coverage"] == "partial_unknown"
    assert candidate["detailQualified"] == 0 and candidate["detailErrors"] == 1
    assert candidate["sources"][0]["truncated"] is True
    assert candidate["listings"][0]["marcusInventoryRecord"] == {"listing": listing(), "brokers": []}
    assert "detailObservedAt" not in candidate["listings"][0]


def test_candidate_structure_rederives_identity_and_rejects_forged_wrapper():
    from cre_marcus_inventory import candidate_data
    from cre_marcus_inventory_checkpoint import candidate_structure

    row = stage(listing())
    receipt = {"schema": "marcus-inventory-custody/v1", "generation": GENERATION,
               "generation_started_at": START, "attempt": 1, "started_at": START, "finished_at": FINISH,
               "coordinator_finished_at": FINISH, "source_sha256": "a" * 64, "source_bytes": 100,
               "producer_sha": "b" * 40, "target_sha256": "c" * 64, "detail_errors": 1,
               "partition": {"staged": [row], "held": []}}
    candidate = candidate_data(receipt)
    assert candidate_structure(candidate)["partition"]["staged"] == [row]
    candidate["listings"][0]["id"] = "999"
    with pytest.raises(ValueError):
        candidate_structure(candidate)


def test_referenced_brokers_are_retained_as_unverified_source_observations():
    from cre_marcus_inventory import stage_envelope

    source = listing()
    source["brokerIds"] = [1, 1]
    brokers = [{"name": "Other"}, {"name": "Source contact", "email": "test@example.invalid"}]
    staged = stage_inventory(source, GENERATION, START, FINISH, brokers)
    envelope = json.loads(base64.b64decode(staged["observation_base64"]))
    assert envelope == {"listing": source, "brokers": [{"index": 1, "broker": brokers[1]}]}
    assert stage_envelope(envelope, GENERATION, START, FINISH) == staged
    envelope["brokers"] = []
    with pytest.raises(ValueError):
        stage_envelope(envelope, GENERATION, START, FINISH)


def test_artifact_validator_checks_inventory_freshness_without_detail_claim(tmp_path):
    from datetime import datetime
    from cre_checkpoint_refresh import ArtifactValidationError, _validate_source_artifact_local
    from cre_marcus_inventory import candidate_data

    row = stage(listing())
    source = {"schema": "marcus-inventory-custody/v1", "generation": GENERATION,
              "generation_started_at": START, "attempt": 1, "started_at": START, "finished_at": FINISH,
              "coordinator_finished_at": FINISH, "source_sha256": "a" * 64, "source_bytes": 100,
              "producer_sha": "b" * 40, "target_sha256": "c" * 64, "detail_errors": 1,
              "partition": {"staged": [row], "held": []}}
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(candidate_data(source)), encoding="utf8")
    kwargs = {"require_strict_freshness": True, "expected_generation_id": GENERATION,
              "expected_generation_started_at": START, "expected_transactions": ("sale", "lease"),
              "now": datetime.fromisoformat(FINISH.replace("Z", "+00:00"))}
    stats = _validate_source_artifact_local(path, "marcus-millichap", START, **kwargs)
    assert stats["marcus_inventory_partial"] is True
    assert stats["strict_freshness"] is False
    kwargs["max_observation_age_hours"] = 0
    with pytest.raises(ArtifactValidationError):
        _validate_source_artifact_local(path, "marcus-millichap", START, **kwargs)


def test_partial_candidate_cannot_use_ordinary_ingest_cli(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from cre_marcus_inventory import candidate_data

    row = stage(listing())
    source = {"schema": "marcus-inventory-custody/v1", "generation": GENERATION,
              "generation_started_at": START, "attempt": 1, "started_at": START, "finished_at": FINISH,
              "coordinator_finished_at": FINISH, "source_sha256": "a" * 64, "source_bytes": 100,
              "producer_sha": "b" * 40, "target_sha256": "c" * 64, "detail_errors": 1,
              "partition": {"staged": [row], "held": []}}
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(candidate_data(source)), encoding="utf8")
    script = Path(__file__).resolve().parents[1] / "cre_ingest.py"
    result = subprocess.run([sys.executable, str(script), "--in", str(path), "--dry-run"],
                            text=True, capture_output=True, check=False)
    assert result.returncode != 0
    assert "without guarded checkpoint admission" in result.stderr


def test_series_qualifies_only_exact_marcus_partial_owner_and_attempt():
    from reporting.partial_qualification import marcus_inventory_partial_child

    sha, target, producer = "a" * 64, "b" * 64, "c" * 40
    binding = {"schema": "marcus-inventory-partial-v1", "scope": "marcus_inventory_partial_v1",
               "source_key": "marcus-millichap", "original_source_outcome": "detail_freshness_rejected",
               "whole_source_coverage": False, "detail_qualified": 0, "detail_errors": 1,
               "artifact_sha256": sha, "source_sha256": sha, "database_target_sha256": target,
               "staged_sha256": sha, "preimage_sha256": sha, "generation_id": GENERATION,
               "generation_started_at": START, "producer_sha": producer, "attempt": 1,
               "held": [], "held_count": 0, "emitted_count": 1}
    checkpoint = {"artifact": {"marcus_inventory_partial": True, "sha256": sha, "staged_unique": 1,
                               "path": "rejected/marcus-millichap-inventory-attempt-1.json"},
                  "gate": {"verdict": "ok_additive_verified_subset", "mark_missing_safe": False,
                           "admission_scope": "marcus_inventory_partial_v1", "current_inventory_proof": binding},
                  "marcus_inventory_worker": {"sha256": sha, "state": "settled", "attempt": 1},
                  "marcus_inventory_probe": {"artifact_sha256": sha, "generation_id": GENERATION,
                      "producer_sha": producer, "database_target_sha256": target, "staged_sha256": sha,
                      "preimage_sha256": sha, "held": []},
                  "attempts": [{"number": 1, "rc": 0, "finished_at": FINISH,
                      "error": "marcus-millichap/sale freshness metric detailErrors must be zero",
                      "rejected_artifact": "rejected/marcus-millichap-attempt-1.json", "rejected_artifact_sha256": sha}],
                  "subset_ingest_attempt_id": "d" * 32}
    child = {"config": {"pipeline": True}, "run_id": GENERATION, "started_at": START,
             "collector_git_sha": producer, "preflight": {"database_target": {"value": target}},
             "sources": {"marcus-millichap": checkpoint}}
    assert marcus_inventory_partial_child(child)
    checkpoint["attempts"][0]["rejected_artifact_sha256"] = "e" * 64
    assert not marcus_inventory_partial_child(child)
    checkpoint["attempts"][0]["rejected_artifact_sha256"] = sha
    binding["detail_qualified"] = True
    assert not marcus_inventory_partial_child(child)
