import pytest
from test_pyramid_rollback_result import BINDING


def test_guard_rejects_boolean_size_and_duplicate_receipts(tmp_path):
    import json
    import cre_transwestern_rollback_result as outcome

    expected = {**BINDING, "artifact_bytes": 1}
    receipt = {"schema": outcome.SCHEMA, "source_key": "transwestern",
               "outcome": outcome.OUTCOME, "binding": {**expected, "artifact_bytes": True}}
    path = tmp_path / "ingest.log"
    line = outcome.PREFIX + json.dumps(receipt) + "\n"
    path.write_text(line)
    with pytest.raises(ValueError):
        outcome.read_result(path, expected)
    receipt["binding"] = expected
    line = outcome.PREFIX + json.dumps(receipt) + "\n"
    path.write_text(line + line)
    with pytest.raises(ValueError):
        outcome.read_result(path, expected)

@pytest.mark.parametrize(
    "case", ["rollback", "committed", "invalid", "unknown", "cleanup", "nonzero"]
)
def test_checkpoint_consumes_only_bound_settled_transwestern_results(
    tmp_path, monkeypatch, case
):
    import cre_checkpoint_refresh as refresh
    import cre_transwestern_rollback_result as result_module

    checkpoint = {"state": "dry_run_passed", "artifact": {}}
    manifest = {
        "sources": {"transwestern": checkpoint},
        "collector_git_sha": "e" * 40,
    }
    monkeypatch.setattr(refresh, "save_manifest", lambda *a: None)
    monkeypatch.setattr(refresh, "manifest_database_target_sha256", lambda *a: "c" * 64)
    monkeypatch.setattr(refresh, "verified_subset_candidate", lambda *a: False)
    monkeypatch.setattr(refresh, "run_command", lambda *a, **k: 1 if case == "nonzero" else 0)
    if case == "nonzero":
        monkeypatch.setattr(refresh, "poll_hook", lambda: lambda: None)
    monkeypatch.setattr(result_module, "expected_binding", lambda *a: BINDING)
    import cre_transwestern_image_outcome as image_outcome
    monkeypatch.setattr(image_outcome, "read_outcome", lambda *a: {})
    monkeypatch.setattr(image_outcome, "verified_checkpoint", lambda *a: {"ok": True} if case == "committed" else None)
    import cre_pyramid_rollback_result as shared
    monkeypatch.setattr(shared, "verified_rollback", lambda *a: True)
    monkeypatch.setattr(refresh, "sha256_file", lambda *a: "f" * 64)

    def result(*args):
        if case == "nonzero":
            pytest.fail("nonzero subprocess cannot be demoted by a receipt")
        if case == "committed":
            pytest.fail("successful image receipt must not require a guard receipt")
        if case == "invalid":
            raise ValueError("bad")
        return {
            "outcome": {
                "outcome": "committed"
                if case == "committed"
                else "protected_image_multiplicity_refused"
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
            "reason": "transwestern_image_guard_refusal",
            "outcome": "exact_rollback",
            "replay_safe": True,
            "validation_path": "recovery/validation.json",
        }
        raise refresh.GlobalStageError("rolled back")

    monkeypatch.setattr(refresh, "recover_interrupted_ingest", recovery)
    artifact = tmp_path / "transwestern.json"
    if case == "rollback":
        with pytest.raises(refresh.TranswesternImageGuardRefusal):
            refresh.ingest_source(
                tmp_path, manifest, "transwestern", artifact, None
            )
        assert checkpoint["state"] == "transwestern_image_guard_refused"
    elif case == "committed":
        refresh.ingest_source(tmp_path, manifest, "transwestern", artifact, None)
        assert checkpoint["state"] == "ingested"
    else:
        error = (
            refresh.ProcessCleanupUnknown
            if case == "cleanup"
            else refresh.GlobalStageError
        )
        with pytest.raises(error):
            refresh.ingest_source(
                tmp_path, manifest, "transwestern", artifact, None
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
    import cre_transwestern_rollback_result as tw

    logs = tmp_path / "logs"
    recovery_dir = tmp_path / "recovery"
    logs.mkdir()
    recovery_dir.mkdir()
    receipt = {
        "schema": tw.SCHEMA,
        "source_key": "transwestern",
        "outcome": "protected_image_multiplicity_refused",
        "binding": dict(BINDING),
    }
    if mutation == "foreign_attempt":
        receipt["binding"]["attempt_id"] = "f" * 32
    log = logs / "transwestern-ingest.log"
    line = tw.PREFIX + json.dumps(receipt) + "\n"
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
                "source_key": "transwestern",
                "generation_id": BINDING["generation_id"],
            }
        ]
    if mutation == "matching_job":
        report["queries"]["artifact_run_jobs"][0]["matching_jobs"] = 1
    raw = json.dumps(report).encode()
    (recovery_dir / "transwestern-validation.json").write_bytes(raw)
    evidence = {
        "outcome": receipt,
        "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        "log_bytes": log.stat().st_size,
    }
    recovery = {
        "reason": "transwestern_image_guard_refusal",
        "outcome": "exact_rollback",
        "replay_safe": True,
        "readback_ok": False,
        "artifact_run_job_match_ok": False,
        "rc": 0,
        "subprocess_rc": 0,
        "probe_started_at": "2026-11-03T00:00:02+00:00",
        "probe_finished_at": "2026-11-03T00:00:04+00:00",
        "validation_path": "recovery/transwestern-validation.json",
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
        "log": "logs/transwestern-ingest.log",
        "additive": True,
        "mark_missing": False,
        "status_activation": False,
        "attempt_id": BINDING["attempt_id"],
        "started_at": "2026-11-03T00:00:00+00:00",
        "finished_at": "2026-11-03T00:00:01+00:00",
        "transwestern_guard_result": evidence,
    }
    manifest = {
        "sources": {
            "transwestern": {
                "state": "transwestern_image_guard_refused",
                "artifact": {"path": "sources/transwestern.json"},
                "ingest": ingest,
                "ingest_recovery": recovery,
            }
        }
    }
    monkeypatch.setattr(tw, "expected_binding", lambda *a: BINDING)
    monkeypatch.setattr(refresh, "require_full_validation_report", lambda *a: None)
    assert outcome.verified_rollback(tmp_path, manifest, "transwestern") is (mutation == "valid")
