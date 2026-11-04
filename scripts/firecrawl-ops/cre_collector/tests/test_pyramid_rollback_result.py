import pytest
from cre_pyramid_rollback_result import build_guard_sql, read_result

BINDING = {
    "artifact_sha256": "a" * 64,
    "artifact_bytes": 123,
    "artifact_run_key": "ingest:v1:" + "b" * 64,
    "generation_id": "2026-11-03T000000Z",
    "generation_started_at": "2026-11-03T00:00:00Z",
    "database_target_sha256": "c" * 64,
    "attempt_id": "d" * 32,
    "collector_git_sha": "e" * 40,
}


def test_guard_result_is_emitted_after_explicit_rollback():
    sql = build_guard_sql(BINDING)
    assert (
        sql.index("ROLLBACK;")
        < sql.index("CRE_PYRAMID_GUARD_RESULT ")
        < sql.index("\\quit")
    )
    assert "t.external_id IS DISTINCT FROM s.external_id" in sql
    assert "t.source_url IS DISTINCT FROM s.source_url" in sql


def test_reader_rejects_boolean_artifact_size_alias(tmp_path):
    import json

    expected = {**BINDING, "artifact_bytes": 1}
    actual = {**expected, "artifact_bytes": True}
    path = tmp_path / "ingest.log"
    path.write_text(
        "CRE_PYRAMID_GUARD_RESULT " + json.dumps({
            "schema": "pyramid-historical-identity-result-v1",
            "source_key": "pyramid-brokerage",
            "outcome": "historical_identity_refused",
            "binding": actual,
        }) + "\n"
    )
    with pytest.raises(ValueError):
        read_result(path, expected)


def test_reader_rejects_missing_or_duplicate_result(tmp_path):
    path = tmp_path / "ingest.log"
    path.write_text("not a receipt\n")
    with pytest.raises(ValueError):
        read_result(path, BINDING)


@pytest.mark.parametrize(
    "case", ["rollback", "committed", "invalid", "unknown", "cleanup", "nonzero"]
)
def test_checkpoint_consumes_only_bound_settled_pyramid_results(
    tmp_path, monkeypatch, case
):
    import cre_checkpoint_refresh as refresh
    import cre_pyramid_rollback_result as result_module

    checkpoint = {"state": "dry_run_passed", "artifact": {}}
    manifest = {
        "sources": {"pyramid-brokerage": checkpoint},
        "collector_git_sha": "e" * 40,
    }
    monkeypatch.setattr(refresh, "save_manifest", lambda *a: None)
    monkeypatch.setattr(refresh, "manifest_database_target_sha256", lambda *a: "c" * 64)
    monkeypatch.setattr(refresh, "verified_subset_candidate", lambda *a: False)
    monkeypatch.setattr(refresh, "run_command", lambda *a, **k: 1 if case == "nonzero" else 0)
    if case == "nonzero":
        monkeypatch.setattr(refresh, "poll_hook", lambda: lambda: None)
    monkeypatch.setattr(result_module, "expected_binding", lambda *a: BINDING)
    monkeypatch.setattr(result_module, "verified_rollback", lambda *a: True)
    monkeypatch.setattr(refresh, "sha256_file", lambda *a: "f" * 64)

    def result(*args):
        if case == "nonzero":
            pytest.fail("nonzero subprocess cannot be demoted by a receipt")
        if case == "invalid":
            raise ValueError("bad")
        return {
            "outcome": {
                "outcome": "committed"
                if case == "committed"
                else "historical_identity_refused"
            }
        }

    monkeypatch.setattr(result_module, "read_result", result)

    def recovery(*args):
        if case == "cleanup":
            raise refresh.ProcessCleanupUnknown("group unsettled")
        if case == "unknown":
            raise refresh.GlobalStageError("unknown")
        checkpoint["state"] = "ingest_recovery_required"
        checkpoint["ingest_recovery"] = {
            "reason": "pyramid_historical_guard_refusal",
            "outcome": "exact_rollback",
            "replay_safe": True,
            "validation_path": "recovery/validation.json",
        }
        raise refresh.GlobalStageError("rolled back")

    monkeypatch.setattr(refresh, "recover_interrupted_ingest", recovery)
    artifact = tmp_path / "pyramid.json"
    if case == "rollback":
        with pytest.raises(refresh.PyramidHistoricalGuardRefusal):
            refresh.ingest_source(
                tmp_path, manifest, "pyramid-brokerage", artifact, None
            )
        assert checkpoint["state"] == "pyramid_historical_guard_refused"
    elif case == "committed":
        refresh.ingest_source(tmp_path, manifest, "pyramid-brokerage", artifact, None)
        assert checkpoint["state"] == "ingested"
    else:
        error = (
            refresh.ProcessCleanupUnknown
            if case == "cleanup"
            else refresh.GlobalStageError
        )
        with pytest.raises(error):
            refresh.ingest_source(
                tmp_path, manifest, "pyramid-brokerage", artifact, None
            )
        assert checkpoint["state"] == "ingest_recovery_required"


@pytest.mark.parametrize(
    "mutation",
    [
        "valid",
        "foreign_attempt",
        "tampered_report",
        "malformed_generation",
        "matching_generation",
        "matching_job",
        "duplicate_receipt",
        "wrong_target",
        "wrong_key",
        "wrong_probe_attempt",
        "stale_report",
    ],
)
def test_rollback_evidence_is_rechecked_from_stable_bound_files(
    tmp_path, monkeypatch, mutation
):
    import hashlib
    import json

    import cre_checkpoint_refresh as refresh
    import cre_pyramid_rollback_result as outcome

    logs = tmp_path / "logs"
    recovery_dir = tmp_path / "recovery"
    logs.mkdir()
    recovery_dir.mkdir()
    receipt = {
        "schema": outcome.SCHEMA,
        "source_key": "pyramid-brokerage",
        "outcome": "historical_identity_refused",
        "binding": dict(BINDING),
    }
    if mutation == "foreign_attempt":
        receipt["binding"]["attempt_id"] = "f" * 32
    log = logs / "pyramid-brokerage-ingest.log"
    line = outcome.PREFIX + json.dumps(receipt) + "\n"
    log.write_text(line + (line if mutation == "duplicate_receipt" else ""))
    from cre_validate import FULL_VALIDATION_PROFILE, VALIDATION_AUDIT_SCOPES

    report = {
        "ok": True,
        "validation_profile": FULL_VALIDATION_PROFILE,
        "audit_scope": VALIDATION_AUDIT_SCOPES[FULL_VALIDATION_PROFILE],
        "queries": {
            "freshness_generations": [],
            "artifact_run_jobs": [{"matching_jobs": 0}],
        },
    }
    report["generated_at"] = "2026-11-03T00:00:03+00:00"
    report["artifact_run_probe"] = {
        "database_target_sha256": BINDING["database_target_sha256"],
        "artifact_run_key_sha256": hashlib.sha256(
            BINDING["artifact_run_key"].encode()
        ).hexdigest(),
        "attempt_id": BINDING["attempt_id"],
    }
    if mutation == "wrong_target":
        report["artifact_run_probe"]["database_target_sha256"] = "f" * 64
    if mutation == "wrong_key":
        report["artifact_run_probe"]["artifact_run_key_sha256"] = "f" * 64
    if mutation == "wrong_probe_attempt":
        report["artifact_run_probe"]["attempt_id"] = "f" * 32
    if mutation == "stale_report":
        report["generated_at"] = "2026-11-02T23:59:59+00:00"
    if mutation == "malformed_generation":
        report["queries"]["freshness_generations"] = [{}]
    if mutation == "matching_generation":
        report["queries"]["freshness_generations"] = [
            {
                "source_key": "pyramid-brokerage",
                "generation_id": BINDING["generation_id"],
            }
        ]
    if mutation == "matching_job":
        report["queries"]["artifact_run_jobs"][0]["matching_jobs"] = 1
    raw = json.dumps(report).encode()
    (recovery_dir / "pyramid-brokerage-validation.json").write_bytes(raw)
    evidence = {
        "outcome": receipt,
        "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        "log_bytes": log.stat().st_size,
    }
    recovery = {
        "reason": "pyramid_historical_guard_refusal",
        "outcome": "exact_rollback",
        "replay_safe": True,
        "readback_ok": False,
        "artifact_run_job_match_ok": False,
        "rc": 0,
        "subprocess_rc": 0,
        "probe_started_at": "2026-11-03T00:00:02+00:00",
        "probe_finished_at": "2026-11-03T00:00:04+00:00",
        "validation_path": "recovery/pyramid-brokerage-validation.json",
        "validation_sha256": hashlib.sha256(raw).hexdigest(),
        "generation_id": BINDING["generation_id"],
        "artifact_run_key_sha256": hashlib.sha256(
            BINDING["artifact_run_key"].encode()
        ).hexdigest(),
    }
    if mutation == "tampered_report":
        recovery["validation_sha256"] = "0" * 64
    ingest = {
        "rc": 0,
        "log": "logs/pyramid-brokerage-ingest.log",
        "additive": True,
        "mark_missing": False,
        "status_activation": False,
        "attempt_id": BINDING["attempt_id"],
        "started_at": "2026-11-03T00:00:00+00:00",
        "finished_at": "2026-11-03T00:00:01+00:00",
        "pyramid_guard_result": evidence,
    }
    manifest = {
        "sources": {
            "pyramid-brokerage": {
                "state": "pyramid_historical_guard_refused",
                "artifact": {"path": "sources/pyramid-brokerage.json"},
                "ingest": ingest,
                "ingest_recovery": recovery,
            }
        }
    }
    monkeypatch.setattr(outcome, "expected_binding", lambda *a: BINDING)
    monkeypatch.setattr(refresh, "require_full_validation_report", lambda *a: None)
    assert outcome.verified_rollback(tmp_path, manifest) is (mutation == "valid")


def test_interrupted_pyramid_with_bound_receipt_cannot_replay_without_settlement(tmp_path, monkeypatch):
    import hashlib
    import json

    import cre_checkpoint_refresh as refresh
    import cre_pyramid_rollback_result as outcome

    run = tmp_path / "run"
    (run / "sources").mkdir(parents=True)
    (run / "logs").mkdir()
    artifact = run / "sources" / "pyramid-brokerage.json"
    artifact.write_bytes(b"immutable fixture")
    manifest = refresh.new_manifest(
        run, git_sha="e" * 40, git_dirty=False, sources=("pyramid-brokerage",),
        page_cap=400, concurrency=3,
        database_target={"algorithm": "sha256", "value": "c" * 64},
    )
    checkpoint = manifest["sources"]["pyramid-brokerage"]
    checkpoint.update({
        "state": "ingesting",
        "artifact": {"path": "sources/pyramid-brokerage.json",
                     "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                     "bytes": artifact.stat().st_size},
        "ingest": {"rc": None, "finished_at": None, "attempt_id": "d" * 32},
    })
    binding = outcome.expected_binding(run, manifest, "d" * 32)
    log = run / "logs" / "pyramid-brokerage-ingest.log"
    log.write_text(outcome.PREFIX + json.dumps({
        "schema": outcome.SCHEMA, "source_key": "pyramid-brokerage",
        "outcome": "historical_identity_refused", "binding": binding,
    }) + "\n")
    assert outcome.read_result(log, binding)["outcome"]["outcome"] == "historical_identity_refused"
    monkeypatch.setattr(refresh, "_manifest_checkpoint_artifact_valid", lambda *a: (artifact, {}))
    monkeypatch.setattr(refresh, "run_command", lambda *a, **k: pytest.fail("must not probe or replay"))
    with pytest.raises(refresh.GlobalStageError, match="interrupted Pyramid ingest"):
        refresh.advance_source(run, manifest, "pyramid-brokerage", page_cap=400,
                               concurrency=3, attempts_this_run=1, env_file=None)
    saved = json.loads((run / "manifest.json").read_text())
    assert saved["sources"]["pyramid-brokerage"]["state"] == "ingest_recovery_required"
    with pytest.raises(refresh.GlobalStageError, match="reviewed ingest recovery"):
        refresh.advance_source(run, saved, "pyramid-brokerage", page_cap=400,
                               concurrency=3, attempts_this_run=1, env_file=None)


def test_verified_rollback_uses_real_artifact_and_full_report_binding(tmp_path):
    import hashlib
    import json

    import cre_pyramid_rollback_result as outcome
    from cre_ingest import artifact_run_identity_from_digests
    from cre_validate import FULL_VALIDATION_PROFILE, QUERIES, VALIDATION_AUDIT_SCOPES

    (tmp_path / "sources").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "recovery").mkdir()
    artifact = tmp_path / "sources" / "pyramid-brokerage.json"
    artifact.write_bytes(b"synthetic immutable artifact")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    checkpoint = {
        "state": "pyramid_historical_guard_refused",
        "artifact": {
            "path": "sources/pyramid-brokerage.json",
            "sha256": digest,
            "bytes": artifact.stat().st_size,
        },
    }
    manifest = {
        "run_id": "2026-11-03T000000Z",
        "started_at": "2026-11-03T00:00:00Z",
        "collector_git_sha": "e" * 40,
        "preflight": {"database_target": {"value": "c" * 64}},
        "sources": {"pyramid-brokerage": checkpoint},
    }
    binding = outcome.expected_binding(tmp_path, manifest, "d" * 32)
    receipt = {
        "schema": outcome.SCHEMA,
        "source_key": "pyramid-brokerage",
        "outcome": "historical_identity_refused",
        "binding": binding,
    }
    log = tmp_path / "logs" / "pyramid-brokerage-ingest.log"
    log.write_text(outcome.PREFIX + json.dumps(receipt) + "\n")
    checkpoint["ingest"] = {
        "rc": 0,
        "log": "logs/pyramid-brokerage-ingest.log",
        "attempt_id": "d" * 32,
        "started_at": "2026-11-03T00:00:00+00:00",
        "finished_at": "2026-11-03T00:00:01+00:00",
        "additive": True,
        "mark_missing": False,
        "status_activation": False,
        "pyramid_guard_result": outcome.read_result(log, binding),
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
    }
    run_key = artifact_run_identity_from_digests([digest])[0]
    report["generated_at"] = "2026-11-03T00:00:03+00:00"
    report["artifact_run_probe"] = {
        "database_target_sha256": binding["database_target_sha256"],
        "artifact_run_key_sha256": hashlib.sha256(run_key.encode()).hexdigest(),
        "attempt_id": binding["attempt_id"],
    }
    raw = json.dumps(report).encode()
    (tmp_path / "recovery" / "pyramid-brokerage-validation.json").write_bytes(raw)
    checkpoint["ingest_recovery"] = {
        "reason": "pyramid_historical_guard_refusal",
        "outcome": "exact_rollback",
        "replay_safe": True,
        "readback_ok": False,
        "artifact_run_job_match_ok": False,
        "rc": 0,
        "subprocess_rc": 0,
        "probe_started_at": "2026-11-03T00:00:02+00:00",
        "probe_finished_at": "2026-11-03T00:00:04+00:00",
        "generation_id": manifest["run_id"],
        "artifact_run_key_sha256": hashlib.sha256(run_key.encode()).hexdigest(),
        "validation_path": "recovery/pyramid-brokerage-validation.json",
        "validation_sha256": hashlib.sha256(raw).hexdigest(),
    }
    assert outcome.verified_rollback(tmp_path, manifest)
    artifact.write_bytes(b"changed artifact")
    assert not outcome.verified_rollback(tmp_path, manifest)


@pytest.mark.parametrize(
    "fault", ["valid", "mixed", "dirty", "changed_bytes", "wrong_target", "wrong_code"]
)
def test_pyramid_guard_input_requires_single_current_bound_artifact(
    tmp_path, monkeypatch, fault
):
    import hashlib
    import json
    from types import SimpleNamespace

    import cre_checkpoint_refresh as refresh
    from cre_ingest import artifact_run_identity_from_digests
    from cre_pyramid_rollback_result import bind_input

    artifact = tmp_path / "artifact.json"
    data = {
        "sources": [{"sourceKey": "pyramid-brokerage"}],
        "listings": [{"sourceKey": "pyramid-brokerage"}],
        "runMeta": {
            "freshness": {
                "generationId": BINDING["generation_id"],
                "generationStartedAt": BINDING["generation_started_at"],
            }
        },
    }
    if fault == "mixed":
        data["listings"][0]["sourceKey"] = "foreign"
    raw = json.dumps(data).encode()
    artifact.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    key = artifact_run_identity_from_digests([digest])[0]
    args = SimpleNamespace(
        inputs=[str(artifact)],
        checkpoint_ingest_attempt="d" * 32,
        expected_db_target_sha256=("invalid" if fault == "wrong_target" else "c" * 64),
        mark_missing=False,
        daum_verified_subset_claims=None,
        matthews_targeted_claims=None,
        checkpoint_collector_git_sha=None,
        checkpoint_pyramid_code_sha="e" * 40,
    )
    monkeypatch.setattr(
        refresh,
        "git_identity",
        lambda: ("f" * 40 if fault == "wrong_code" else "e" * 40, fault == "dirty"),
    )
    if fault == "changed_bytes":
        artifact.write_bytes(b"changed")
    if fault == "valid":
        binding = bind_input(args, data, raw, key, activate_status=False)
        assert (
            binding["artifact_sha256"] == digest and binding["attempt_id"] == "d" * 32
        )
    else:
        with pytest.raises(ValueError):
            bind_input(args, data, raw, key, activate_status=False)
