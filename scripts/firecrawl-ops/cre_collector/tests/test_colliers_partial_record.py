import base64
import copy
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cre_colliers_partial import verify_detail_record


def sample():
    raw = json.dumps({"ProjectSummary": {"ProjectId": "42"}})
    source = {"generation": "gen", "attempt": 1, "start": 1, "page": 1, "pageSize": 1, "offset": 0,
              "inventoryObservedAt": "2026-11-04T15:30:00Z", "detailObservedAt": "2026-11-04T16:01:00Z",
              "mapProjectId": "42", "detailTokenSha256": hashlib.sha256(b"fixture").hexdigest(), "listSha256": "a" * 64, "mapSha256": "b" * 64,
              "decodedDetailSha256": hashlib.sha256(raw.encode()).hexdigest(), "nativeSha256": hashlib.sha256(raw.encode()).hexdigest()}
    listing = {"sourceKey": "colliers", "transactionMode": "sale", "transactionType": "Investment Sale", "id": "42", "brokerIds": [],
               "url": "https://my.rcm1.com/handler/landing.aspx?pv=fixture",
               "preserveChildCollections": True, "inventoryObservedAt": source["inventoryObservedAt"],
               "detailObservedAt": source["detailObservedAt"],
               "freshnessProvenance": {"generationId": "gen", "method": "colliers_salestracker_slp_detail"},
               "colliersPartialEvidence": {"schema": "colliers-partial-detail-v1", **source},
               "colliersSalesTrackerDetail": {"projectId": "42"}}
    return {"listing": listing, "source": source, "decodedDetail": raw,
            "nativeBase64": base64.b64encode(raw.encode()).decode()}


def verify(item):
    return verify_detail_record(item, "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z")


def test_bound_detail_identity_and_clock():
    assert verify(sample())["external_id"] == "42"


@pytest.mark.parametrize("mutate", [
    lambda item: item["source"].update(attempt=True),
    lambda item: item["source"].update(mapProjectId="wrong"),
    lambda item: item["source"].update(decodedDetailSha256="0" * 64),
    lambda item: item["listing"].update(preserveChildCollections=False),
    lambda item: item["listing"].update(brokerIds=[0]),
    lambda item: item["listing"]["freshnessProvenance"].update(generationId="other"),
    lambda item: item["source"].update(detailObservedAt="2026-11-04T15:35:00Z"),
])
def test_refuses_tampered_detail_or_binding(mutate):
    item = copy.deepcopy(sample())
    mutate(item)
    with pytest.raises(ValueError):
        verify(item)


def test_missing_native_project_id_cannot_match_literal_none():
    item = sample()
    item["decodedDetail"] = json.dumps({"ProjectSummary": {}})
    item["source"]["decodedDetailSha256"] = hashlib.sha256(item["decodedDetail"].encode()).hexdigest()
    item["source"]["mapProjectId"] = "None"
    item["listing"]["id"] = "None"
    item["listing"]["colliersSalesTrackerDetail"]["projectId"] = "None"
    item["listing"]["colliersPartialEvidence"] = {"schema": "colliers-partial-detail-v1", **item["source"]}
    with pytest.raises(ValueError):
        verify(item)


def test_canonical_mapper_uses_verified_id_and_registered_source():
    from cre_colliers_partial import stage_detail_record

    row = stage_detail_record(sample(), "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", "2026-11-04T16:02:00Z")
    assert row["slug"] == "colliers" and row["external_id"] == "42" and row["transaction_type"] == "sale"


def test_canonical_mapper_holds_an_untrusted_source_url():
    from cre_colliers_partial import stage_detail_record

    item = sample()
    item["listing"]["url"] = "https://other.example/handler/landing.aspx?pv=fixture"
    with pytest.raises(ValueError, match="source URL"):
        stage_detail_record(item, "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", "2026-11-04T16:02:00Z")


def test_canonical_mapper_rejects_duplicate_source_token():
    from cre_colliers_partial import stage_detail_record

    item = sample()
    item["listing"]["url"] += "&pv=other"
    with pytest.raises(ValueError, match="token"):
        stage_detail_record(item, "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", "2026-11-04T16:02:00Z")


def test_canonical_mapper_rejects_token_from_other_card():
    from cre_colliers_partial import stage_detail_record

    item = sample()
    item["listing"]["url"] = item["listing"]["url"].replace("fixture", "other")
    with pytest.raises(ValueError, match="token"):
        stage_detail_record(item, "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", "2026-11-04T16:02:00Z")


def test_canonical_mapper_rejects_detail_after_artifact_finish():
    from cre_colliers_partial import stage_detail_record

    with pytest.raises(ValueError, match="after artifact finished"):
        stage_detail_record(sample(), "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", "2026-11-04T16:00:30Z")


def test_canonical_mapper_rejects_colliers_main_namespace():
    from cre_colliers_partial import stage_detail_record

    item = sample()
    item["decodedDetail"] = json.dumps({"ProjectSummary": {"ProjectId": "main:42"}})
    item["source"]["decodedDetailSha256"] = hashlib.sha256(item["decodedDetail"].encode()).hexdigest()
    item["source"]["nativeSha256"] = item["source"]["decodedDetailSha256"]
    item["nativeBase64"] = base64.b64encode(item["decodedDetail"].encode()).decode()
    item["source"]["mapProjectId"] = "main:42"
    item["listing"]["id"] = "main:42"
    item["listing"]["colliersSalesTrackerDetail"]["projectId"] = "main:42"
    item["listing"]["colliersPartialEvidence"] = {"schema": "colliers-partial-detail-v1", **item["source"]}
    with pytest.raises(ValueError, match="staged identity"):
        stage_detail_record(item, "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", "2026-11-04T16:02:00Z")


def test_canonical_mapper_rejects_future_artifact_finish():
    from cre_colliers_partial import stage_detail_record

    future = (datetime.now(UTC) + timedelta(minutes=6)).isoformat()
    with pytest.raises(ValueError, match="clock-skew"):
        stage_detail_record(sample(), "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", future)



def test_source_bytes_through_node_replay_into_python_staging(tmp_path):
    from cre_colliers_partial import stage_detail_record

    collector = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        ["node", "--import", "tsx", "tests/ts/fixtures/colliers-window-staging.ts"],
        cwd=collector, env={**os.environ, "COLLIERS_FIXTURE_DIRECTORY": str(tmp_path)},
        capture_output=True, text=True, timeout=60, check=True,
    )
    fixture = json.loads(completed.stdout)
    assert len(fixture["rows"]) == 1
    staged = stage_detail_record(fixture["rows"][0], "gen", 1, "2026-11-04T15:00:00Z",
                                 "2026-11-04T16:00:00Z", fixture["artifactFinishedAt"])
    assert staged["external_id"] == "native-e2e"
    assert fixture["rows"][0]["listing"]["city"] == "Native City"
    assert fixture["rows"][0]["listing"]["units"] == 10

    from cre_colliers_partial_checkpoint import (
        cursor_after_database_holds,
        validate_window_artifact,
    )

    artifact = json.loads((tmp_path / "artifact").read_text())
    source = artifact["source"]
    receipt = {"generation": "gen", "generation_started_at": "2026-11-04T14:00:00Z", "attempt": 1, "started_at": "2026-11-04T15:00:00Z",
               "finished_at": "2026-11-04T16:00:00Z", "bytes": source["bytes"], "sha256": source["sha256"],
               "main_sha256": "c" * 64, "producer_sha": "a" * 40, "database_target_sha256": "b" * 64}
    result = validate_window_artifact(artifact, receipt, artifact["evidence"])
    assert result == [staged]
    assert cursor_after_database_holds(artifact, {"native-e2e": "reserved native history is unknown"}) == artifact["proposedCursor"]
    from cre_gate import count_artifacts

    counts, errors, observed, _, rejected = count_artifacts([str(tmp_path / "artifact")], True)
    assert counts == {"colliers": 1} and observed == {"colliers"} and rejected == 0
    assert errors["colliers"] == "original source pass failed"
    from cre_checkpoint_refresh import _validate_source_artifact_local

    partial_stats = _validate_source_artifact_local(tmp_path / "artifact", "colliers", "2026-11-04T15:00:00Z",
        expected_generation_id="gen", expected_generation_started_at="2026-11-04T14:00:00Z",
        expected_transactions=("sale", "lease"), require_strict_freshness=True)
    assert partial_stats["colliers_partial"] is True and partial_stats["staged_unique"] == 1
    unguarded = subprocess.run([sys.executable, "cre_ingest.py", "--in", str(tmp_path / "artifact"), "--dry-run"],
                               cwd=collector, capture_output=True, text=True, timeout=30, check=False)
    assert unguarded.returncode != 0
    assert "refusing Colliers partial artifact without guarded checkpoint admission" in unguarded.stderr

    # The shared source and aggregate gate adapter binds the exact source,
    # worker receipt, bytes, target and DB preimage. Files remain synthetic.
    import cre_colliers_partial_checkpoint as partial

    run = tmp_path / "gen"
    private = run / "colliers-partial"
    private.mkdir(parents=True, mode=0o700)
    artifact_path = private / "window-1-artifact.json"
    evidence_path = private / "window-1-evidence.jsonl"
    artifact_bytes = (tmp_path / "artifact").read_bytes()
    artifact_path.write_bytes(artifact_bytes)
    artifact_path.chmod(0o600)
    evidence_path.write_bytes((tmp_path / "evidence").read_bytes())
    evidence_path.chmod(0o600)
    artifact_sha = hashlib.sha256(artifact_bytes).hexdigest()
    worker_receipt = {"schema": "colliers-window-worker-receipt/v1", "generation": "gen", "attempt": 1,
                      "producerSha": "a" * 40, "targetSha256": "b" * 64,
                      "artifact": {"bytes": len(artifact_bytes), "sha256": artifact_sha},
                      "evidence": artifact["evidence"], "detailFinishedAt": artifact["detailFinishedAt"],
                      "proposedCursor": artifact["proposedCursor"], "admitted": 1, "held": artifact["held"],
                      "heldOther": artifact["heldOther"], "unattempted": artifact["unattempted"]}
    worker_path = private / "window-1-receipt.json"
    worker_path.write_text(json.dumps(worker_receipt))
    worker_path.chmod(0o600)
    receipt_sha = hashlib.sha256(worker_path.read_bytes()).hexdigest()
    staged_sha = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    preimage_path = private / ("window-1-preimage-" + "f" * 16 + ".json")
    preimage_path.write_text(json.dumps([{"external_id": "native-e2e", "listing": None, "source_index": None}]))
    preimage_path.chmod(0o600)
    preimage_sha = hashlib.sha256(preimage_path.read_bytes()).hexdigest()
    checkpoint = {"artifact": {"path": "colliers-partial/window-1-artifact.json", "bytes": len(artifact_bytes), "sha256": artifact_sha},
                  "colliers_partial_worker": {"state": "settled", "rc": 0, "attempt": 1,
                      "receipt_path": "colliers-partial/window-1-receipt.json", "receipt_sha256": receipt_sha,
                      "evidence_path": "colliers-partial/window-1-evidence.jsonl"},
                  "colliers_identity_probe": {"artifact_sha256": artifact_sha, "generation_id": "gen",
                      "producer_sha": "a" * 40, "database_target_sha256": "b" * 64, "staged_sha256": staged_sha,
                      "held": [], "preimage_path": "colliers-partial/window-1-preimage-" + "f" * 16 + ".json",
                      "preimage_sha256": preimage_sha, "history_holds": {}}}
    manifest = {"run_id": "gen", "started_at": "2026-11-04T14:00:00Z", "collector_git_sha": "a" * 40,
                "collector_git_dirty": False, "preflight": {"database_target": {"algorithm": "sha256", "value": "b" * 64}},
                "config": {"additive": True, "mark_missing": False,
                "status_activation": False, "transactions": ["sale", "lease"], "max_items": 0,
                "sources": ["colliers"], "pipeline": False}, "scope": {}, "sources": {"colliers": checkpoint}}
    report = {"generated_for_artifact": str(artifact_path), "summary": {"mode": "live", "torow_errors": 0,
              "baseline_updated": False, "baseline_update_sources": [], "hold_sources": ["colliers"],
              "mark_missing_safe_sources": [], "mark_missing_safe_brokerages": [], "verdict_counts": {}},
              "per_source": {"colliers": {"verdict": "first_seen", "reason": "no baseline row; cannot gate (first sight); a source pass also errored this run",
                  "mark_missing_safe": False, "current_active": 1}}}
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(partial, "revalidate_source_receipt", lambda *_: receipt)
        aggregate = copy.deepcopy(report)
        proof = partial.admit_window(artifact_path, manifest, report, current_code_sha="a" * 40,
                                     current_git_dirty=False, phase="source")
        assert proof["emitted_count"] == 1 and proof["original_source_outcome"] == "source_error"
        assert proof["whole_source_coverage"] is False and report["per_source"]["colliers"]["mark_missing_safe"] is False
        checkpoint["gate"] = {"current_inventory_proof": proof}
        assert partial.admit_window(artifact_path, manifest, aggregate, current_code_sha="a" * 40,
                                    current_git_dirty=False, phase="aggregate") == proof
        envelope = {"manifest": manifest, "source_report": report, "aggregate_report": aggregate,
                    "binding": proof, "attempt_id": "e" * 32}
        context = partial.ingest_subset_context(artifact_path, envelope, current_code_sha="a" * 40,
            current_git_dirty=False, expected_database_target_sha256="b" * 64, attempt_id="e" * 32)
        assert context["staged"] == result and context["history_holds"] == {}
        claims_path = private / "claims.json"
        claims_path.write_text(json.dumps(envelope))
        claims_path.chmod(0o600)
        dry_dir = tmp_path / "dry"
        shim = ("import cre_colliers_partial_checkpoint as p; import cre_checkpoint_refresh as c; "
                "c.git_identity=lambda: ('" + "a" * 40 + "',False); "
                "p.revalidate_source_receipt=lambda *_: " + repr(receipt) + "; "
                "import cre_ingest; cre_ingest.main()")
        dry = subprocess.run([sys.executable, "-c", shim, "--in", str(artifact_path), "--dry-run",
                              "--colliers-partial-claims", str(claims_path), "--require-strict-freshness",
                              "--expected-db-target-sha256", "b" * 64, "--checkpoint-ingest-attempt", "e" * 32,
                              "--keep-artifacts", str(dry_dir)],
                             cwd=collector, capture_output=True, text=True, timeout=30, check=False)
        assert dry.returncode == 0, dry.stderr
        sql_path = dry_dir / "ingest.sql"
        assert sql_path.stat().st_mode & 0o077 == 0
        assert "colliersNativeObservations" in sql_path.read_text()
        changed_claim = copy.deepcopy(envelope)
        changed_claim["aggregate_report"]["per_source"]["colliers"]["current_inventory_proof"]["artifact_sha256"] = "0" * 64
        with pytest.raises(ValueError, match="gate claim"):
            partial.ingest_subset_context(artifact_path, changed_claim, current_code_sha="a" * 40,
                current_git_dirty=False, expected_database_target_sha256="b" * 64, attempt_id="e" * 32)
        changed = copy.deepcopy(manifest)
        changed["sources"]["colliers"]["colliers_partial_worker"]["state"] = "dispatching"
        with pytest.raises(ValueError, match="settlement"):
            partial.admit_window(artifact_path, changed, copy.deepcopy(aggregate), current_code_sha="a" * 40,
                                 current_git_dirty=False, phase="source")
    finally:
        monkeypatch.undo()
    for key, value in [("targetSha256", "0" * 64), ("sourceOutcome", "success"), ("unattempted", 999)]:
        changed = copy.deepcopy(artifact)
        changed[key] = value
        with pytest.raises(ValueError):
            validate_window_artifact(changed, receipt, artifact["evidence"])

def test_native_byte_change_is_rejected_even_when_decoded_text_matches():
    item = sample()
    item["nativeBase64"] = base64.b64encode(b"\xef\xbb\xbf" + item["decodedDetail"].encode()).decode()
    with pytest.raises(ValueError, match="native binding"):
        verify_detail_record(item, "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z")


def test_native_observation_preserves_verified_bytes_and_rejects_tampering():
    from cre_colliers_partial import native_observation

    item = sample()
    result = native_observation(item, "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z")
    assert result["nativeBase64"] == item["nativeBase64"]
    item["nativeBase64"] = base64.b64encode(b"tampered").decode()
    with pytest.raises(ValueError, match="native binding"):
        native_observation(item, "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z")

def test_native_project_id_matches_javascript_safe_integer_semantics():
    from cre_colliers_partial import _native_project_id

    assert _native_project_id(1.0) == "1"
    assert _native_project_id(True) is None
    assert _native_project_id(1.5) is None
    assert _native_project_id(9007199254740992) is None
    assert _native_project_id(10 ** 1000) is None


def test_independent_source_and_detail_clocks_do_not_fabricate_order():
    item = sample()
    source = item["source"]
    source["inventoryObservedAt"] = "2026-11-04T16:00:00.806Z"
    source["detailObservedAt"] = "2026-11-04T16:00:00.200Z"
    item["listing"]["inventoryObservedAt"] = source["inventoryObservedAt"]
    item["listing"]["detailObservedAt"] = source["detailObservedAt"]
    item["listing"]["colliersPartialEvidence"] = {"schema": "colliers-partial-detail-v1", **source}
    actual = verify_detail_record(item, "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00.900Z")
    assert actual["external_id"] == source["mapProjectId"]
    assert source["detailObservedAt"] == "2026-11-04T16:00:00.200Z"
    with pytest.raises(ValueError, match="partial observation clock"):
        verify_detail_record(item, "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00.700Z")
