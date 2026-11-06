"""Bound non-commit evidence must survive independent revalidation."""
import hashlib
import json

import pytest

import recovery.atomic_ingest_rollback as outcome
from cre_ingest import artifact_run_identity_from_digests
from cre_validate import FULL_VALIDATION_PROFILE, QUERIES, VALIDATION_AUDIT_SCOPES


@pytest.mark.parametrize("source", ["daum-commercial", "cbre"])
@pytest.mark.parametrize("fault", ["valid", "artifact", "target", "attempt", "job", "generation", "report_hash", "rollback_unknown", "time"])
def test_atomic_rollback_binds_real_evidence(tmp_path, source, fault):
    for name in ("sources", "logs", "recovery"):
        (tmp_path / name).mkdir()
    artifact = tmp_path / "sources" / f"{source}.json"
    artifact.write_bytes(b"synthetic immutable artifact")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    run_key = artifact_run_identity_from_digests([digest])[0]
    run_key_hash = hashlib.sha256(run_key.encode()).hexdigest()
    checkpoint = {
        "state": "settled_ingest_failed",
        "artifact": {"path": f"sources/{source}.json", "sha256": digest, "bytes": artifact.stat().st_size},
        "ingest": {"rc": 1, "log": f"logs/{source}-ingest.log", "attempt_id": "d" * 32,
                   "started_at": "2026-11-03T00:00:00Z", "finished_at": "2026-11-03T00:00:01Z",
                   "additive": True, "mark_missing": False, "status_activation": False},
    }
    manifest = {"run_id": "2026-11-03T000000Z", "started_at": "2026-11-03T00:00:00Z",
                "collector_git_sha": "e" * 40, "config": {"sources": [source]},
                "preflight": {"database_target": {"value": "c" * 64}}, "sources": {source: checkpoint}}
    report = {"ok": True, "validation_profile": FULL_VALIDATION_PROFILE,
              "audit_scope": VALIDATION_AUDIT_SCOPES[FULL_VALIDATION_PROFILE],
              "queries": {**{key: [] for key in QUERIES}, "artifact_run_jobs": [{"matching_jobs": "0"}]},
              "historical_primary_child_conflicts": [], "generated_at": "2026-11-03T00:00:03Z",
              "artifact_run_probe": {"database_target_sha256": "c" * 64,
                                     "artifact_run_key_sha256": run_key_hash, "attempt_id": "d" * 32}}
    recovery = {"reason": "settled_ingest_failure", "outcome": "exact_rollback", "replay_safe": True,
                "readback_ok": False, "artifact_run_job_match_ok": False, "rc": 0, "subprocess_rc": 1,
                "probe_started_at": "2026-11-03T00:00:02Z", "probe_finished_at": "2026-11-03T00:00:04Z",
                "generation_id": manifest["run_id"], "artifact_run_key_sha256": run_key_hash,
                "validation_path": f"recovery/{source}-validation.json"}
    checkpoint["ingest_recovery"] = recovery
    if fault == "target": report["artifact_run_probe"]["database_target_sha256"] = "f" * 64
    if fault == "attempt": report["artifact_run_probe"]["attempt_id"] = "a" * 32
    if fault == "job": report["queries"]["artifact_run_jobs"] = [{"matching_jobs": "1"}]
    if fault == "generation": report["queries"]["freshness_generations"] = [{"source_key": source, "generation_id": manifest["run_id"]}]
    if fault == "rollback_unknown": recovery["outcome"] = "unknown"
    if fault == "time": report["generated_at"] = "2026-11-02T00:00:00Z"
    raw = json.dumps(report).encode()
    (tmp_path / recovery["validation_path"]).write_bytes(raw)
    recovery["validation_sha256"] = "f" * 64 if fault == "report_hash" else hashlib.sha256(raw).hexdigest()
    if fault == "artifact": artifact.write_bytes(b"changed artifact")
    assert outcome.verified_rollback(tmp_path, manifest, source) is (fault == "valid")


@pytest.mark.parametrize("source,flag", [
    ("colliers", "colliers_partial"), ("jll-investor", "jll_partial"),
    ("franklin-street", "franklin_partial"), ("lyon-stahl", "lyon_partial"),
    ("nai-global", "nai_post_partial"), ("jll", None), ("colliers-main", None),
    ("transwestern", None), ("pyramid-brokerage", None),
])
def test_special_partial_and_guarded_sources_are_not_demoted(source, flag):
    artifact = {flag: True} if flag else {}
    manifest = {"config": {"sources": [source]}, "sources": {source: {"artifact": artifact}}}
    assert not outcome.eligible(manifest, source)


@pytest.mark.parametrize("outcome_kind", ["settled", "bad_attempt", "unknown_cleanup", "committed"])
def test_ingest_failure_requires_actual_bound_recovery_before_local_exit(tmp_path, monkeypatch, outcome_kind):
    import cre_checkpoint_refresh as refresh

    root = tmp_path / "2026-11-03T000000Z"
    root.mkdir()
    (root / "sources").mkdir()
    source = "daum-commercial"
    artifact = root / "sources" / f"{source}.json"
    artifact.write_bytes(b"synthetic immutable artifact")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    target = {"algorithm": "sha256", "value": "c" * 64}
    manifest = refresh.new_manifest(root, git_sha="e" * 40, git_dirty=False, sources=(source,),
                                    page_cap=400, concurrency=1, database_target=target)
    checkpoint = manifest["sources"][source]
    checkpoint["state"] = "dry_run_passed"
    checkpoint["artifact"] = {"path": f"sources/{source}.json", "sha256": digest,
                              "bytes": artifact.stat().st_size,
                              "freshness_generation_id": manifest["run_id"],
                              "freshness_generation_started_at": manifest["started_at"]}
    monkeypatch.setattr(refresh, "save_manifest", lambda *_: None)
    monkeypatch.setattr(refresh, "git_identity", lambda: ("e" * 40, False))
    monkeypatch.setattr(refresh, "verify_validation_readback", lambda *a, **kw: {"ok": outcome_kind == "committed"})
    run_key = artifact_run_identity_from_digests([digest])[0]

    def command(argv, log, **_kwargs):
        if "cre_validate.py" not in argv:
            assert "--checkpoint-ingest-attempt" in argv
            if outcome_kind == "unknown_cleanup":
                raise refresh.ProcessCleanupUnknown("owned child outcome unknown")
            return 1
        attempt = argv[argv.index("--expected-ingest-attempt") + 1]
        report = {"ok": True, "validation_profile": FULL_VALIDATION_PROFILE,
                  "audit_scope": VALIDATION_AUDIT_SCOPES[FULL_VALIDATION_PROFILE],
                  "queries": {**{key: [] for key in QUERIES}, "artifact_run_jobs": [{"matching_jobs": "1" if outcome_kind == "committed" else "0"}]},
                  "historical_primary_child_conflicts": [], "generated_at": refresh.utc_now(),
                  "artifact_run_probe": {"database_target_sha256": target["value"],
                    "artifact_run_key_sha256": hashlib.sha256(run_key.encode()).hexdigest(),
                    "attempt_id": "a" * 32 if outcome_kind == "bad_attempt" else attempt}}
        path = Path(argv[argv.index("--out") + 1])
        path.write_text(json.dumps(report))
        return 0
    from pathlib import Path
    monkeypatch.setattr(refresh, "run_command", command)
    if outcome_kind == "settled":
        with pytest.raises(refresh.SettledIngestFailure):
            refresh.ingest_source(root, manifest, source, artifact, None)
        assert checkpoint["state"] == "settled_ingest_failed"
        assert outcome.verified_rollback(root, manifest, source)
    elif outcome_kind == "committed":
        refresh.ingest_source(root, manifest, source, artifact, None)
        assert checkpoint["state"] == "ingested"
    elif outcome_kind == "bad_attempt":
        with pytest.raises(refresh.GlobalStageError, match="not bound"):
            refresh.ingest_source(root, manifest, source, artifact, None)
        assert checkpoint["state"] == "ingest_recovery_required"
    else:
        with pytest.raises(refresh.ProcessCleanupUnknown):
            refresh.ingest_source(root, manifest, source, artifact, None)
        assert checkpoint["state"] == "ingesting"


@pytest.mark.parametrize("fault", ["valid", "bad_unit", "wrong_result", "unknown_commit", "bad_receipt"])
def test_pipeline_reconciliation_revalidates_singleton_hold(tmp_path, monkeypatch, fault):
    import cre_checkpoint_series as series

    source = "daum-commercial"
    result = {"state": "failed", "reason": "settled_ingest_failure", "unit_id": "unit-1"}
    unit = {"unit_id": "unit-1", "state": "source_held", "sources": [source]}
    pipeline = {"status": "partial", "source_results": {source: result}, "units": [unit]}
    child = {"pipeline": pipeline}
    if fault == "bad_unit": unit["sources"].append("cbre")
    if fault == "wrong_result": result["reason"] = "generic source failure"
    if fault == "unknown_commit": pipeline["commit_outcome_unknown"] = True
    monkeypatch.setattr(outcome, "verified_rollback", lambda *_: fault != "bad_receipt")
    assert series.settled_atomic_hold(child, source, tmp_path) is (fault == "valid")
