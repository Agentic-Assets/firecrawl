"""Bound non-commit evidence must survive independent revalidation."""

import hashlib
import json

import pytest

import recovery.atomic_ingest_rollback as outcome
from cre_ingest import artifact_run_identity_from_digests
from cre_validate import FULL_VALIDATION_PROFILE, QUERIES, VALIDATION_AUDIT_SCOPES


@pytest.mark.parametrize("source", ["daum-commercial", "cbre"])
@pytest.mark.parametrize(
    "fault",
    [
        "valid",
        "artifact",
        "target",
        "attempt",
        "job",
        "generation",
        "report_hash",
        "rollback_unknown",
        "time",
        "fence_missing",
        "fence_jobs",
        "fence_generation",
        "fence_binding",
    ],
)
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
        "artifact": {
            "path": f"sources/{source}.json",
            "sha256": digest,
            "bytes": artifact.stat().st_size,
        },
        "ingest": {
            "rc": 1,
            "log": f"logs/{source}-ingest.log",
            "attempt_id": "d" * 32,
            "started_at": "2026-11-03T00:00:00Z",
            "finished_at": "2026-11-03T00:00:01Z",
            "additive": True,
            "mark_missing": False,
            "status_activation": False,
        },
    }
    manifest = {
        "run_id": "2026-11-03T000000Z",
        "started_at": "2026-11-03T00:00:00Z",
        "collector_git_sha": "e" * 40,
        "config": {"sources": [source]},
        "preflight": {"database_target": {"value": "c" * 64}},
        "sources": {source: checkpoint},
    }
    report = {
        "ok": True,
        "validation_profile": FULL_VALIDATION_PROFILE,
        "audit_scope": VALIDATION_AUDIT_SCOPES[FULL_VALIDATION_PROFILE],
        "queries": {
            **{key: [] for key in QUERIES},
            "artifact_run_jobs": [{"matching_jobs": "0"}],
        },
        "historical_primary_child_conflicts": [],
        "generated_at": "2026-11-03T00:00:03Z",
        "artifact_run_probe": {
            "database_target_sha256": "c" * 64,
            "artifact_run_key_sha256": run_key_hash,
            "attempt_id": "d" * 32,
        },
    }
    report["queries"]["atomic_settlement"] = [
        {"matching_jobs": "0", "matching_generations": "0"}
    ]
    report["atomic_settlement_probe"] = {
        **report["artifact_run_probe"],
        "source": source,
        "generation_id": manifest["run_id"],
    }
    if fault == "fence_missing":
        report["queries"].pop("atomic_settlement")
    if fault == "fence_jobs":
        report["queries"]["atomic_settlement"][0]["matching_jobs"] = "1"
    if fault == "fence_generation":
        report["queries"]["atomic_settlement"][0]["matching_generations"] = "1"
    if fault == "fence_binding":
        report["atomic_settlement_probe"]["attempt_id"] = "f" * 32
    recovery = {
        "reason": "settled_ingest_failure",
        "outcome": "exact_rollback",
        "replay_safe": True,
        "readback_ok": False,
        "artifact_run_job_match_ok": False,
        "rc": 0,
        "subprocess_rc": 1,
        "probe_started_at": "2026-11-03T00:00:02Z",
        "probe_finished_at": "2026-11-03T00:00:04Z",
        "generation_id": manifest["run_id"],
        "artifact_run_key_sha256": run_key_hash,
        "validation_path": f"recovery/{source}-validation.json",
    }
    checkpoint["ingest_recovery"] = recovery
    if fault == "target":
        report["artifact_run_probe"]["database_target_sha256"] = "f" * 64
    if fault == "attempt":
        report["artifact_run_probe"]["attempt_id"] = "a" * 32
    if fault == "job":
        report["queries"]["artifact_run_jobs"] = [{"matching_jobs": "1"}]
    if fault == "generation":
        report["queries"]["freshness_generations"] = [
            {"source_key": source, "generation_id": manifest["run_id"]}
        ]
    if fault == "rollback_unknown":
        recovery["outcome"] = "unknown"
    if fault == "time":
        report["generated_at"] = "2026-11-02T00:00:00Z"
    raw = json.dumps(report).encode()
    (tmp_path / recovery["validation_path"]).write_bytes(raw)
    recovery["validation_sha256"] = (
        "f" * 64 if fault == "report_hash" else hashlib.sha256(raw).hexdigest()
    )
    if fault == "artifact":
        artifact.write_bytes(b"changed artifact")
    assert outcome.verified_rollback(tmp_path, manifest, source) is (fault == "valid")


@pytest.mark.parametrize(
    "source,flag",
    [
        ("colliers", "colliers_partial"),
        ("jll-investor", "jll_partial"),
        ("franklin-street", "franklin_partial"),
        ("lyon-stahl", "lyon_partial"),
        ("nai-global", "nai_post_partial"),
        ("jll", None),
        ("colliers-main", None),
        ("transwestern", None),
        ("pyramid-brokerage", None),
    ],
)
def test_special_partial_and_guarded_sources_are_not_demoted(source, flag):
    artifact = {flag: True} if flag else {}
    manifest = {
        "config": {"sources": [source]},
        "sources": {source: {"artifact": artifact}},
    }
    assert not outcome.eligible(manifest, source)


@pytest.mark.parametrize(
    "outcome_kind",
    [
        "settled",
        "bad_attempt",
        "unknown_cleanup",
        "committed",
        "fence_jobs",
        "fence_generation",
        "fence_missing",
        "committed_fence_zero",
    ],
)
def test_ingest_failure_requires_actual_bound_recovery_before_local_exit(
    tmp_path, monkeypatch, outcome_kind
):
    import cre_checkpoint_refresh as refresh

    root = tmp_path / "2026-11-03T000000Z"
    root.mkdir()
    (root / "sources").mkdir()
    source = "daum-commercial"
    artifact = root / "sources" / f"{source}.json"
    artifact.write_bytes(b"synthetic immutable artifact")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    target = {"algorithm": "sha256", "value": "c" * 64}
    manifest = refresh.new_manifest(
        root,
        git_sha="e" * 40,
        git_dirty=False,
        sources=(source,),
        page_cap=400,
        concurrency=1,
        database_target=target,
    )
    checkpoint = manifest["sources"][source]
    checkpoint["state"] = "dry_run_passed"
    checkpoint["artifact"] = {
        "path": f"sources/{source}.json",
        "sha256": digest,
        "bytes": artifact.stat().st_size,
        "freshness_generation_id": manifest["run_id"],
        "freshness_generation_started_at": manifest["started_at"],
    }
    monkeypatch.setattr(refresh, "save_manifest", lambda *_: None)
    monkeypatch.setattr(refresh, "git_identity", lambda: ("e" * 40, False))
    monkeypatch.setattr(
        refresh,
        "verify_validation_readback",
        lambda *a, **kw: {"ok": outcome_kind in {"committed", "committed_fence_zero"}},
    )
    run_key = artifact_run_identity_from_digests([digest])[0]

    def command(argv, log, **_kwargs):
        if "cre_validate.py" not in argv:
            assert "--checkpoint-ingest-attempt" in argv
            if outcome_kind == "unknown_cleanup":
                raise refresh.ProcessCleanupUnknown("owned child outcome unknown")
            return 1
        attempt = argv[argv.index("--expected-ingest-attempt") + 1]
        report = {
            "ok": True,
            "validation_profile": FULL_VALIDATION_PROFILE,
            "audit_scope": VALIDATION_AUDIT_SCOPES[FULL_VALIDATION_PROFILE],
            "queries": {
                **{key: [] for key in QUERIES},
                "artifact_run_jobs": [
                    {
                        "matching_jobs": "1"
                        if outcome_kind in {"committed", "committed_fence_zero"}
                        else "0"
                    }
                ],
            },
            "historical_primary_child_conflicts": [],
            "generated_at": refresh.utc_now(),
            "artifact_run_probe": {
                "database_target_sha256": target["value"],
                "artifact_run_key_sha256": hashlib.sha256(run_key.encode()).hexdigest(),
                "attempt_id": "a" * 32 if outcome_kind == "bad_attempt" else attempt,
            },
        }
        assert argv[argv.index("--expected-settlement-source") + 1] == source
        assert (
            argv[argv.index("--expected-settlement-generation") + 1]
            == manifest["run_id"]
        )
        report["queries"]["atomic_settlement"] = [
            {
                "matching_jobs": "1" if outcome_kind == "committed" else "0",
                "matching_generations": "1" if outcome_kind == "committed" else "0",
            }
        ]
        report["atomic_settlement_probe"] = {
            **report["artifact_run_probe"],
            "source": source,
            "generation_id": manifest["run_id"],
        }
        if outcome_kind == "fence_jobs":
            report["queries"]["atomic_settlement"][0]["matching_jobs"] = "1"
        if outcome_kind == "fence_generation":
            report["queries"]["atomic_settlement"][0]["matching_generations"] = "1"
        if outcome_kind == "fence_missing":
            report["queries"].pop("atomic_settlement")
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
        with pytest.raises(refresh.GlobalStageError, match="readback is invalid"):
            refresh.ingest_source(root, manifest, source, artifact, None)
        assert checkpoint["state"] == "ingest_recovery_required"
    elif outcome_kind == "committed_fence_zero":
        with pytest.raises(refresh.GlobalStageError, match="settlement fence disagree"):
            refresh.ingest_source(root, manifest, source, artifact, None)
        assert checkpoint["state"] == "ingest_recovery_required"
        assert checkpoint["ingest_recovery"]["readback_ok"] is False
    elif outcome_kind in {"fence_jobs", "fence_generation", "fence_missing"}:
        with pytest.raises(refresh.GlobalStageError):
            refresh.ingest_source(root, manifest, source, artifact, None)
        assert checkpoint["state"] == "ingest_recovery_required"
        assert checkpoint["ingest_recovery"].get("outcome") != "exact_rollback"
    else:
        with pytest.raises(refresh.ProcessCleanupUnknown):
            refresh.ingest_source(root, manifest, source, artifact, None)
        assert checkpoint["state"] == "ingesting"


@pytest.mark.parametrize(
    "fault", ["valid", "bad_unit", "wrong_result", "unknown_commit", "bad_receipt"]
)
def test_pipeline_reconciliation_revalidates_singleton_hold(
    tmp_path, monkeypatch, fault
):
    import cre_checkpoint_series as series

    source = "daum-commercial"
    result = {
        "state": "failed",
        "reason": "settled_ingest_failure",
        "unit_id": "unit-1",
    }
    unit = {"unit_id": "unit-1", "state": "source_held", "sources": [source]}
    pipeline = {
        "status": "partial",
        "source_results": {source: result},
        "units": [unit],
    }
    child = {"pipeline": pipeline}
    if fault == "bad_unit":
        unit["sources"].append("cbre")
    if fault == "wrong_result":
        result["reason"] = "generic source failure"
    if fault == "unknown_commit":
        pipeline["commit_outcome_unknown"] = True
    monkeypatch.setattr(outcome, "verified_rollback", lambda *_: fault != "bad_receipt")
    assert series.settled_atomic_hold(child, source, tmp_path) is (fault == "valid")


@pytest.mark.parametrize("job_count", [0, 1])
def test_changed_code_during_recovery_cannot_accept_either_outcome(
    tmp_path, monkeypatch, job_count
):
    import cre_checkpoint_refresh as refresh

    root = tmp_path / "2026-11-03T000000Z"
    root.mkdir()
    source = "daum-commercial"
    target = {"algorithm": "sha256", "value": "c" * 64}
    manifest = refresh.new_manifest(
        root,
        git_sha="e" * 40,
        git_dirty=False,
        sources=(source,),
        page_cap=400,
        concurrency=1,
        database_target=target,
    )
    checkpoint = manifest["sources"][source]
    checkpoint.update(
        state="ingesting",
        artifact={"sha256": "b" * 64},
        ingest={"attempt_id": "d" * 32},
        ingest_recovery={"reason": "settled_ingest_failure"},
    )
    identity = {"value": ("e" * 40, False)}
    monkeypatch.setattr(refresh, "git_identity", lambda: identity["value"])
    monkeypatch.setattr(refresh, "save_manifest", lambda *_: None)
    monkeypatch.setattr(
        refresh,
        "verify_validation_readback",
        lambda *_args, **_kwargs: pytest.fail("readback must not be accepted"),
    )

    def command(argv, _log, **_kwargs):
        from pathlib import Path

        Path(argv[argv.index("--out") + 1]).write_text(
            json.dumps(
                {"queries": {"artifact_run_jobs": [{"matching_jobs": str(job_count)}]}}
            )
        )
        identity["value"] = ("f" * 40, False)
        return 0

    monkeypatch.setattr(refresh, "run_command", command)
    with pytest.raises(refresh.GlobalStageError, match="code changed during"):
        refresh.recover_interrupted_ingest(root, manifest, source, None)
    assert checkpoint["state"] == "ingest_recovery_required"
    assert checkpoint["ingest_recovery"]["readback_ok"] is False
    assert "outcome" not in checkpoint["ingest_recovery"]


def test_atomic_fence_uses_separate_lock_first_read_committed_transaction():
    import cre_validate
    import cre_ingest

    query = cre_validate.atomic_settlement_query(
        "ingest:v1:" + "a" * 64, "daum-commercial", "2026-11-03T000000Z"
    )
    script = cre_validate._build_query_batch_script(
        {"ordinary": "SELECT 1;", "atomic_settlement": query}
    )
    read_committed = script.index(
        "BEGIN TRANSACTION ISOLATION LEVEL READ COMMITTED READ ONLY;"
    )
    lock = script.index(
        cre_ingest.lifecycle_transaction_lock_sql().removesuffix(";"), read_committed
    )
    count = script.index("AS matching_jobs", lock)
    assert script.index("ROLLBACK;") < read_committed < lock < count
    assert "AS matching_generations" in script[count:]
    assert "EXISTS (" in query and "b.slug IN (" in query
    assert "raw AS MATERIALIZED" not in query
    assert "SET LOCAL lock_timeout = '30s';" in script[read_committed:lock]
    assert script.count(r"\o :CRE_VALIDATION_RESULTS") == 1
    assert r"\g /dev/null" in script[lock:count] or r"\g nul" in script[lock:count]
    lock_segment = script[lock:count]
    assert lock_segment.count("pg_advisory_xact_lock") == 1
    assert ";" not in lock_segment.split(chr(92) + "g", 1)[0]
    with pytest.raises(ValueError):
        cre_validate.atomic_settlement_query("bad-key", "daum-commercial", "gen")
    with pytest.raises(ValueError):
        cre_validate.atomic_settlement_query(
            "ingest:v1:" + "a" * 64, "foreign-source", "gen"
        )


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "code",
        "manifest_code",
        "artifact",
        "target",
        "attempt",
        "second_fence",
        "second_rc",
        "cleanup_unknown",
    ],
)
def test_late_commit_gets_one_bound_readback_without_write_retry(
    tmp_path, monkeypatch, fault
):
    from pathlib import Path
    import cre_checkpoint_refresh as refresh

    root = tmp_path / "2026-11-03T000000Z"
    (root / "sources").mkdir(parents=True)
    source = "daum-commercial"
    artifact = root / "sources" / f"{source}.json"
    artifact.write_bytes(b"bound synthetic artifact")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    target = {"algorithm": "sha256", "value": "c" * 64}
    manifest = refresh.new_manifest(
        root,
        git_sha="e" * 40,
        git_dirty=False,
        sources=(source,),
        page_cap=400,
        concurrency=1,
        database_target=target,
    )
    checkpoint = manifest["sources"][source]
    checkpoint["state"] = "dry_run_passed"
    checkpoint["artifact"] = {
        "path": f"sources/{source}.json",
        "sha256": digest,
        "bytes": artifact.stat().st_size,
        "freshness_generation_id": manifest["run_id"],
        "freshness_generation_started_at": manifest["started_at"],
    }
    identity = {"sha": "e" * 40}
    monkeypatch.setattr(refresh, "git_identity", lambda: (identity["sha"], False))
    monkeypatch.setattr(refresh, "save_manifest", lambda *_: None)
    monkeypatch.setattr(
        refresh, "verify_validation_readback", lambda *a, **kw: {"ok": True}
    )
    run_key = artifact_run_identity_from_digests([digest])[0]
    calls = {"write": 0, "read": 0}

    def command(argv, _log, **_kwargs):
        if "cre_validate.py" not in argv:
            calls["write"] += 1
            return 1
        calls["read"] += 1
        second = calls["read"] == 2
        if second and fault == "cleanup_unknown":
            raise refresh.ProcessCleanupUnknown("synthetic unknown")
        if second and fault == "second_rc":
            return 1
        attempt = argv[argv.index("--expected-ingest-attempt") + 1]
        probe = {
            "database_target_sha256": "c" * 64,
            "artifact_run_key_sha256": hashlib.sha256(run_key.encode()).hexdigest(),
            "attempt_id": attempt,
        }
        queries = {key: [] for key in QUERIES}
        queries["artifact_run_jobs"] = [{"matching_jobs": "1" if second else "0"}]
        if second:
            queries["freshness_generations"] = [
                {"source_key": source, "generation_id": manifest["run_id"]}
            ]
        queries["atomic_settlement"] = [
            {
                "matching_jobs": "0" if second and fault == "second_fence" else "1",
                "matching_generations": "1",
            }
        ]
        report = {
            "ok": True,
            "validation_profile": FULL_VALIDATION_PROFILE,
            "audit_scope": VALIDATION_AUDIT_SCOPES[FULL_VALIDATION_PROFILE],
            "queries": queries,
            "historical_primary_child_conflicts": [],
            "generated_at": refresh.utc_now(),
            "artifact_run_probe": probe,
            "atomic_settlement_probe": {
                **probe,
                "source": source,
                "generation_id": manifest["run_id"],
            },
        }
        Path(argv[argv.index("--out") + 1]).write_text(json.dumps(report))
        if second and fault == "code":
            identity["sha"] = "f" * 40
        if second and fault == "manifest_code":
            manifest["collector_git_sha"] = "f" * 40
        if second and fault == "artifact":
            artifact.write_bytes(b"changed")
        if second and fault == "target":
            manifest["preflight"]["database_target"]["value"] = "f" * 64
        if second and fault == "attempt":
            checkpoint["ingest"]["attempt_id"] = "f" * 32
        return 0

    monkeypatch.setattr(refresh, "run_command", command)
    if fault == "none":
        refresh.ingest_source(root, manifest, source, artifact, None)
        assert checkpoint["state"] == "ingested"
        assert checkpoint["ingest"]["recovered_from_exact_readback"] is True
        assert checkpoint["ingest_recovery"]["first_validation_sha256"]
    else:
        with pytest.raises(refresh.GlobalStageError):
            refresh.ingest_source(root, manifest, source, artifact, None)
        assert checkpoint["state"] == "ingest_recovery_required"
        assert checkpoint["ingest_recovery"]["readback_ok"] is False
        assert checkpoint["ingest_recovery"].get("outcome") != "exact_rollback"
    assert calls == {"write": 1, "read": 2}
