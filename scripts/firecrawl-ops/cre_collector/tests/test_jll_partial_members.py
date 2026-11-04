import copy
from datetime import UTC

import pytest
from cre_jll_partial import verified_members


def artifact():
    identity = "006" + "a" * 15
    url = "https://invest.jll.com/us/en/listings/land/example"
    row = {
        "sourceKey": "jll-investor", "transactionMode": "sale", "transactionType": "Sale (investment)", "id": identity,
        "url": url, "canonicalUrl": url, "jllInvestorUsSearchMembership": True,
        "jllInvestorSearchRow": {"id": identity, "alias": "land/example"},
        "jllInvestorDetail": {"id": identity, "alias": "land/example",
                              "scrape": {"rawHtmlLength": 0, "markdownLength": 0, "linkCount": 0}},
        "freshnessProvenance": {"generationId": "run", "detailScope": "detail_page",
                                "method": "jll_investor_next_data_detail", "cacheDisposition": "live",
                                "identityMethod": "provider_listing_id"},
        "preserveChildCollections": True, "detailObservedWithChildPreservation": True,
    }
    return {"listings": [row], "totalListings": 1, "sources": [
        {"sourceKey": "jll-investor", "transaction": "sale", "supported": True,
         "truncated": True, "listingsCollected": 1},
        {"sourceKey": "jll-investor", "transaction": "lease", "supported": True,
         "truncated": False, "listingsCollected": 0},
    ]}


def test_member_proof_does_not_infer_absent_identities():
    members = verified_members(artifact(), "run")
    assert len(members) == 1
    assert members[0]["external_id"] == "investor:006" + "a" * 15


@pytest.mark.parametrize("mutation", [
    lambda d: d["listings"][0]["jllInvestorDetail"].update(id="006" + "b" * 15),
    lambda d: d["listings"][0]["jllInvestorDetail"].update(alias="land/other"),
    lambda d: d["listings"][0].update(canonicalUrl="https://example.com/listing"),
    lambda d: d["listings"][0]["freshnessProvenance"].update(generationId="other"),
    lambda d: d["listings"][0]["freshnessProvenance"].update(identityMethod="url_only"),
    lambda d: d["listings"][0].update(transactionType="For Lease"),
    lambda d: d["listings"][0].update(jllInvestorUsSearchMembership=False),
    lambda d: d["listings"][0].update(detailObservedWithChildPreservation=False),
    lambda d: d["sources"][0].update(truncated=False),
])
def test_member_proof_rejects_changed_identity_and_provenance(mutation):
    data = artifact()
    mutation(data)
    with pytest.raises(ValueError):
        verified_members(data, "run")


def test_member_proof_rejects_duplicate_identity():
    data = artifact()
    data["listings"].append(copy.deepcopy(data["listings"][0]))
    data["totalListings"] = data["sources"][0]["listingsCollected"] = 2
    with pytest.raises(ValueError, match="unique identity"):
        verified_members(data, "run")


def test_actual_ingest_mapper_matches_verified_identity_and_sale():
    from cre_jll_partial import stage_members
    staged = stage_members(artifact(), "run")
    assert staged == [{"slug": "jll", "external_id": "investor:006" + "a" * 15,
                       "source_url": "https://invest.jll.com/us/en/listings/land/example",
                       "canonical_url": "https://invest.jll.com/us/en/listings/land/example",
                       "transaction_type": "sale"}]


def test_source_and_aggregate_gates_bind_exact_artifact_and_target(tmp_path):
    import hashlib
    import json
    from datetime import datetime, timedelta

    from cre_jll_partial import admit, ingest_subset_context, stage_members

    data = artifact()
    at = datetime.now(UTC) - timedelta(seconds=5)
    start = at.isoformat()
    observed = (at + timedelta(seconds=1)).isoformat()
    finish = (at + timedelta(seconds=2)).isoformat()
    data["runMeta"] = {"mode": "full", "transactions": ["sale", "lease"], "maxItemsPerSource": None,
                       "startedAt": start, "finishedAt": finish,
                       "freshness": {"generationId": "run", "generationStartedAt": start,
                                     "requireFreshDetails": True}}
    data["listings"][0].update(inventoryObservedAt=observed, detailObservedAt=observed)
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(data))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    staged = stage_members(data, "run")
    staged_hash = hashlib.sha256(json.dumps(staged, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=False).encode()).hexdigest()
    source = "jll-investor"
    manifest = {"config": {"additive": True, "mark_missing": False, "status_activation": False,
                           "transactions": ["sale", "lease"], "max_items": 0, "sources": [source]},
                "collector_git_dirty": False, "collector_git_sha": "a"*40,
                "run_id": "run", "started_at": start, "scope": {},
                "preflight": {"database_target": {"algorithm": "sha256", "value": "b"*64}},
                "sources": {source: {"artifact": {"sha256": digest, "bytes": path.stat().st_size},
                           "jll_identity_probe": {"artifact_sha256": digest, "generation_id": "run",
                            "producer_sha": "a"*40, "database_target_sha256": "b"*64,
                            "staged_sha256": staged_hash, "held": []}}}}
    raw_gate = {"current_active": 1, "verdict": "hold",
                "reason": "source pass error: source pass truncated", "mark_missing_safe": False}
    report = {"generated_for_artifact": str(path), "per_source": {source: raw_gate},
              "summary": {"mode": "live", "torow_errors": 0, "baseline_updated": False,
                          "baseline_update_sources": []}}
    first_seen_report = copy.deepcopy(report)
    first_seen_report["per_source"][source].update(
        verdict="first_seen",
        reason="no baseline row; cannot gate (first sight); a source pass also errored this run")
    first_seen_binding = admit(path, copy.deepcopy(manifest), first_seen_report, current_code_sha="a"*40,
                               current_git_dirty=False, phase="source")
    binding = admit(path, manifest, report, current_code_sha="a"*40, current_git_dirty=False, phase="source")
    assert first_seen_binding == binding
    assert binding["emitted_count"] == 1 and binding["excluded_source_identities"] == "unknown"
    source_report = copy.deepcopy(report)
    manifest["sources"][source]["gate"] = {"current_inventory_proof": binding}
    aggregate = copy.deepcopy(report)
    assert admit(path, manifest, aggregate, current_code_sha="a"*40,
                 current_git_dirty=False, phase="aggregate") == binding
    envelope = {"manifest": manifest, "source_report": source_report, "aggregate_report": aggregate,
                "binding": binding, "attempt_id": "c"*32}
    claims = ingest_subset_context(path, envelope, current_code_sha="a"*40, current_git_dirty=False,
                                   expected_database_target_sha256="b"*64, attempt_id="c"*32)
    assert claims["staged"] == staged and claims["held"] == []
    # The partial source cannot erase a healthy sibling's gate result.
    pipeline = copy.deepcopy(manifest)
    pipeline["config"].update(pipeline=True, sources=[source, "newmark"])
    pipeline["sources"][source]["artifact"]["path"] = "artifact.json"
    pipeline["sources"]["newmark"] = {"artifact":{"path":"other.json"}}
    source_gate = {"generated_for_artifact":str(path), "per_source":{source:copy.deepcopy(raw_gate)},
                   "summary":{"mode":"live", "torow_errors":0, "baseline_updated":False,
                              "baseline_update_sources":[], "hold_sources":[source],
                              "mark_missing_safe_sources":["newmark"], "mark_missing_safe_brokerages":["newmark"]}}
    # Start from the original hold; earlier admit mutates its report in place.
    source_gate["per_source"][source] = {"current_active":1, "verdict":"hold",
        "reason":"source pass error: source pass truncated", "mark_missing_safe":False}
    first = admit(path, pipeline, source_gate, current_code_sha="a"*40, current_git_dirty=False, phase="source")
    pipeline["sources"][source]["gate"] = {"current_inventory_proof":first}
    aggregated = copy.deepcopy(source_gate)
    aggregated["generated_for_artifact"] = [str(path), str(tmp_path / "other.json")]
    aggregated["per_source"]["newmark"] = {"verdict":"ok", "mark_missing_safe":True}
    assert admit(path, pipeline, aggregated, current_code_sha="a"*40, current_git_dirty=False, phase="aggregate") == first
    assert aggregated["per_source"]["newmark"]["verdict"] == "ok"
    assert aggregated["summary"]["mark_missing_safe_sources"] == ["newmark"]
    with pytest.raises(ValueError):
        ingest_subset_context(path, envelope, current_code_sha="a"*40, current_git_dirty=False,
                              expected_database_target_sha256="d"*64, attempt_id="c"*32)
    path.write_text(path.read_text().replace("land/example", "land/mutated"))
    with pytest.raises(ValueError):
        ingest_subset_context(path, envelope, current_code_sha="a"*40, current_git_dirty=False,
                              expected_database_target_sha256="b"*64, attempt_id="c"*32)


def test_checkpoint_exact_readback_accepts_only_bound_partial_generation(tmp_path):
    import cre_checkpoint_refresh as checkpoint

    run = tmp_path / "run"
    manifest = checkpoint.new_manifest(run, git_sha="a" * 40, git_dirty=False,
                                       sources=("jll-investor",), page_cap=400, concurrency=1)
    binding = {"artifact_sha256": "b"*64, "generation_id": "run",
               "staged_sha256": "c"*64, "held": ["investor:held"], "emitted_count": 1}
    entry = manifest["sources"]["jll-investor"]
    entry["artifact"] = {"jll_partial": True, "sha256": "b"*64}
    entry["gate"] = {"current_inventory_proof": binding}
    readback = {"artifact_sha256": "b"*64, "generation_id": "run", "staged_sha256": "c"*64,
                "held": ["investor:held"], "expected": 1, "invalid": [], "ok": True}
    validation = {"queries": {"freshness_generations": [], "source_counts": [], "inventory_only_index": []},
                  "jll_partial_readback": readback}
    result = checkpoint.verify_validation_readback(run, manifest, validation, persist_manifest=False)
    assert result["ok"] is True
    assert entry["readback"]["whole_source_coverage"] is False
    validation["jll_partial_readback"] = {**readback, "generation_id": "other"}
    result = checkpoint.verify_validation_readback(run, manifest, validation, persist_manifest=False)
    assert result["ok"] is False


def test_partial_summary_reports_unknown_source_holds_without_inventory_claim():
    from cre_checkpoint_series import _pipeline_member_report_matches, jll_partial_child
    from cre_notification_summary import _jll_emitted_breakdown, _subset_summary_fields

    binding = {"schema": "jll-investor-partial-v1", "scope": "jll_investor_partial_v1",
               "artifact_sha256": "b"*64, "generation_id": "run", "collector_git_sha": "a"*40,
               "database_target_sha256": "c"*64, "staged_sha256": "d"*64,
               "held": ["investor:held"], "held_count": 1, "emitted_count": 1,
               "excluded_source_identities": "unknown", "whole_source_coverage": False}
    observed = {"artifact_sha256": "b"*64, "generation_id": "run", "staged_sha256": "d"*64,
                "expected": 1, "held": ["investor:held"], "invalid": [], "ok": True}
    checkpoint = {"artifact": {"jll_partial": True, "sha256": "b"*64, "staged_unique": 2},
                  "gate": {"verdict": "ok_additive_verified_subset", "mark_missing_safe": False,
                           "current_inventory_proof": binding},
                  "readback": {"ok": True, "observation": observed, "generation_id":"run",
                               "expected_staged_unique":1, "whole_source_coverage":False}}
    child = {"run_id": "run", "collector_git_sha": "a"*40, "config": {},
             "preflight": {"database_target": {"value": "c"*64}},
             "sources": {"jll-investor": checkpoint}}
    assert jll_partial_child(child)
    summary = _subset_summary_fields(child, "jll-investor", 1)
    assert summary["whole_source_coverage"] is False
    assert summary["excluded_source_identities"] == "unknown"
    assert "inventory_count" not in summary
    assert _jll_emitted_breakdown(child, "jll-investor", 1)["sale"] == 1
    assert _pipeline_member_report_matches({"jll_partial_readback": observed}, checkpoint, "jll-investor", "run")
    mutated = dict(observed, generation_id="other")
    assert not _pipeline_member_report_matches({"jll_partial_readback": mutated}, checkpoint, "jll-investor", "run")

    from cre_source_health import _qualified_subset_attempt
    checkpoint.update(state="ingested")
    checkpoint["readback"].update(kind="jll_investor_partial", generation_id="run")
    child.update(status="qualified_partial_subset", started_at="2026-11-04T15:00:00Z",
                 finished_at="2026-11-04T15:03:00Z", scope={"whole_source_coverage":False})
    observed_at = "2026-11-04T15:02:00Z"
    assert _qualified_subset_attempt({"state":"qualified_partial_subset"}, child, observed_at,
                                     computed_at="2026-11-04T15:04:00Z", source_key="jll-investor")
    checkpoint["readback"]["generation_id"] = "other"
    assert not _qualified_subset_attempt({"state":"qualified_partial_subset"}, child, observed_at,
                                         computed_at="2026-11-04T15:04:00Z", source_key="jll-investor")


def test_real_collector_to_artifact_worker_keeps_valid_peer_and_held_evidence(tmp_path, monkeypatch):
    import json
    import os
    import subprocess
    from datetime import datetime, timedelta
    from pathlib import Path

    from cre_pipeline_artifact_worker import compute

    root = Path(__file__).resolve().parents[1]
    start = (datetime.now(UTC) - timedelta(seconds=3)).isoformat()
    preload = tmp_path / "preload.mjs"
    output = tmp_path / "sources" / "jll-investor.json"
    output.parent.mkdir()
    source = """
export async function srcJllInvestor(tx) {
 if (tx === "lease") return {company:"JLL",sourceUrl:"https://invest.jll.com",method:"fixture",totalAvailable:0,listings:[],truncated:false};
 const id="006aaaaaaaaaaaaaaa", url="https://invest.jll.com/us/en/listings/office/good";
 const observed=new Date().toISOString();
 return {company:"JLL",sourceUrl:"https://invest.jll.com",method:"fixture",totalAvailable:2,truncated:true,
 jllInvestorHeldObservations:[{id:"held",detailError:"detail unavailable"}],
 listings:[{id,name:"Synthetic",url,canonicalUrl:url,transactionType:"Sale (investment)",jllInvestorUsSearchMembership:true,
 jllInvestorSearchRow:{id,alias:"office/good"},jllInvestorDetail:{id,alias:"office/good",scrape:{rawHtmlLength:0,markdownLength:0,linkCount:0}},
 inventoryObservedAt:observed,detailObservedAt:observed,preserveChildCollections:true,detailObservedWithChildPreservation:true,
 freshnessProvenance:{generationId:"jll-test",detailScope:"detail_page",method:"jll_investor_next_data_detail",cacheDisposition:"live",identityMethod:"provider_listing_id"}}]};
}
"""
    preload.write_text(
        'import {registerHooks} from "node:module";'
        + "const fixture=" + json.dumps("data:text/javascript," + __import__("urllib.parse", fromlist=["quote"]).quote(source)) + ";"
        + 'registerHooks({resolve(s,c,n){if(c.parentURL?.endsWith("/collect.ts")&&s==="./sources/jll-investor.js")'
          'return {url:fixture,shortCircuit:true};return n(s,c);}});'
    )
    env = dict(os.environ, CRE_REQUIRE_FRESH_DETAILS="1", CRE_REFRESH_GENERATION="jll-test",
               CRE_REFRESH_STARTED_AT=start)
    result = subprocess.run(["node", "--import", "tsx", "--import", str(preload), "collect.ts",
                             "--source=jll-investor", "--transaction=both", "--max-items=0",
                             "--out=" + str(output)], cwd=root, env=env, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    generated = json.loads(output.read_text())
    sale = next(item for item in generated["sources"] if item["transaction"] == "sale")
    assert sale["jllInvestorHeldObservations"][0]["detailError"] == "detail unavailable"
    assert len(generated["listings"]) == 1
    checked = compute("validate", {"path": str(output), "source": "jll-investor",
                      "attempt_started_at": start, "kwargs": {"require_strict_freshness": True,
                      "expected_generation_id": "jll-test", "expected_generation_started_at": start,
                      "expected_transactions": ["sale", "lease"]}}, tmp_path / "worker.json")
    assert checked["jll_partial"] is True and checked["staged_unique"] == 1

    import hashlib

    import cre_checkpoint_refresh as checkpoint
    from cre_ingest import build_sql, to_row
    from cre_jll_partial import ingest_subset_context, stage_members

    raw = output.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    staged = stage_members(generated, "jll-test")
    stage_digest = hashlib.sha256(json.dumps(staged, sort_keys=True, separators=(",", ":"),
                                            ensure_ascii=False).encode()).hexdigest()
    source_key = "jll-investor"
    stats = dict(checked, path="sources/jll-investor.json", attempt_started_at=start)
    manifest = {"config": {"additive": True, "mark_missing": False, "status_activation": False,
                           "transactions": ["sale", "lease"], "max_items": 0, "sources": [source_key]},
                "collector_git_dirty": False, "collector_git_sha": "a"*40,
                "run_id": "jll-test", "started_at": start, "scope": {},
                "preflight": {"database_target": {"algorithm": "sha256", "value": "b"*64}},
                "sources": {source_key: {"artifact": stats, "subset_ingest_attempt_id": "c"*32,
                            "jll_identity_probe": {"artifact_sha256": digest, "generation_id":"jll-test",
                            "producer_sha":"a"*40, "database_target_sha256":"b"*64,
                            "staged_sha256":stage_digest, "held":[]}}}}
    gate = {"generated_for_artifact":str(output), "per_source":{source_key:
            {"current_active":1, "verdict":"hold", "reason":"source pass error: source pass truncated",
             "mark_missing_safe":False}},
            "summary":{"mode":"live", "torow_errors":0, "baseline_updated":False, "baseline_update_sources":[]}}
    monkeypatch.setattr(checkpoint, "git_identity", lambda: ("a"*40, False))
    source_args = {"path":str(output), "run_dir":str(tmp_path), "source":source_key,
                   "manifest":manifest, "report":copy.deepcopy(gate), "phase":"source"}
    source_result = compute("admit", source_args, tmp_path / "source-response.json")
    source_report = json.loads(Path(source_result["report_path"]).read_text())
    binding = source_report["per_source"][source_key]["current_inventory_proof"]
    gate_path = tmp_path / "source-gate.json"
    gate_path.write_text(json.dumps(source_report))
    manifest["sources"][source_key]["gate"] = {"path":"source-gate.json", "current_inventory_proof":binding}
    aggregate_args = dict(source_args, report=copy.deepcopy(gate), phase="aggregate")
    aggregate_result = compute("admit", aggregate_args, tmp_path / "aggregate-response.json")
    envelope = json.loads(Path(aggregate_result["subset_claims"]["path"]).read_text())
    claims = ingest_subset_context(output, envelope, current_code_sha="a"*40, current_git_dirty=False,
                                   expected_database_target_sha256="b"*64, attempt_id="c"*32)
    row = to_row(generated["listings"][0], {}, generated["runMeta"]["finishedAt"])
    sql = build_sql([row], [{"slug":"jll","discovered":1,"saved":1,"errors":0,"notes":"fixture"}],
                    start, set(), jll_partial_ids=tuple(item["external_id"] for item in claims["staged"]),
                    jll_partial_claims=claims)
    assert "JLL partial identity partition changed" in sql
    assert binding["whole_source_coverage"] is False
    assert binding["excluded_source_identities"] == "unknown"


@pytest.mark.parametrize("exact,jobs,committed", [(True, "1", True), (False, "0", False), (True, "0", False)])
def test_jll_recovery_requires_exact_id_readback_and_one_job(tmp_path, monkeypatch, exact, jobs, committed):
    import cre_checkpoint_refresh as checkpoint

    run = tmp_path / "run"
    manifest = checkpoint.new_manifest(run, git_sha="a"*40, git_dirty=False,
                                       sources=("jll-investor",), page_cap=400, concurrency=1)
    source = manifest["sources"]["jll-investor"]
    binding = {"artifact_sha256": "b"*64, "generation_id": "run",
               "staged_sha256": "c"*64, "held": [], "emitted_count": 1}
    source.update(artifact={"jll_partial":True, "sha256":"b"*64},
                  gate={"current_inventory_proof":binding}, ingest={"rc":None}, state="ingesting")
    observed = {"artifact_sha256":"b"*64, "generation_id":"run", "staged_sha256":"c"*64,
                "held":[], "expected":1, "invalid":[] if exact else ["investor:missing"], "ok":exact}
    validation = {"queries":{"source_counts":[], "freshness_generations":[], "inventory_only_index":[],
                             "artifact_run_jobs":[{"matching_jobs":jobs}]}}
    def fake_run(argv, _log, **kwargs):
        checkpoint.atomic_write_json(Path(argv[argv.index("--out")+1]), validation)
        return 0
    from pathlib import Path
    monkeypatch.setattr(checkpoint, "run_command", fake_run)
    monkeypatch.setattr(checkpoint, "jll_partial_readback", lambda *_: observed)
    if committed:
        checkpoint.recover_interrupted_ingest(run, manifest, "jll-investor", None)
        assert source["state"] == "ingested"
        assert source["ingest_recovery"]["readback_ok"] is True
    else:
        with pytest.raises(checkpoint.GlobalStageError, match="manual reconciliation"):
            checkpoint.recover_interrupted_ingest(run, manifest, "jll-investor", None)
        assert source["state"] == "ingest_recovery_required"
        assert source["ingest_recovery"]["readback_ok"] is False
        assert source["ingest_recovery"].get("replay_safe") is not True
