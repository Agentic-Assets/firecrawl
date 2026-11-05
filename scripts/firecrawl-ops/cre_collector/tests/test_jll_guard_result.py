import pytest
from test_pyramid_rollback_result import BINDING

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
    import cre_jll_guard_result as tw

    logs = tmp_path / "logs"
    recovery_dir = tmp_path / "recovery"
    logs.mkdir()
    recovery_dir.mkdir()
    receipt = {
        "schema": tw.SCHEMA,
        "source_key": "jll",
        "outcome": "image_loss_refused",
        "binding": dict(BINDING),
    }
    if mutation == "foreign_attempt":
        receipt["binding"]["attempt_id"] = "f" * 32
    log = logs / "jll-ingest.log"
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
                "source_key": "jll",
                "generation_id": BINDING["generation_id"],
            }
        ]
    if mutation == "matching_job":
        report["queries"]["artifact_run_jobs"][0]["matching_jobs"] = 1
    raw = json.dumps(report).encode()
    (recovery_dir / "jll-validation.json").write_bytes(raw)
    evidence = {
        "outcome": receipt,
        "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        "log_bytes": log.stat().st_size,
    }
    recovery = {
        "reason": "jll_image_guard_refusal",
        "outcome": "exact_rollback",
        "replay_safe": True,
        "readback_ok": False,
        "artifact_run_job_match_ok": False,
        "rc": 0,
        "subprocess_rc": 0,
        "probe_started_at": "2026-11-03T00:00:02+00:00",
        "probe_finished_at": "2026-11-03T00:00:04+00:00",
        "validation_path": "recovery/jll-validation.json",
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
        "log": "logs/jll-ingest.log",
        "additive": True,
        "mark_missing": False,
        "status_activation": False,
        "attempt_id": BINDING["attempt_id"],
        "started_at": "2026-11-03T00:00:00+00:00",
        "finished_at": "2026-11-03T00:00:01+00:00",
        "jll_guard_result": evidence,
    }
    manifest = {
        "sources": {
            "jll": {
                "state": "jll_image_guard_refused",
                "artifact": {"path": "sources/jll.json"},
                "ingest": ingest,
                "ingest_recovery": recovery,
            }
        }
    }
    monkeypatch.setattr(tw, "expected_binding", lambda *a: BINDING)
    monkeypatch.setattr(refresh, "require_full_validation_report", lambda *a: None)
    assert outcome.verified_rollback(tmp_path, manifest, "jll") is (mutation == "valid")


@pytest.mark.parametrize(
    "case", ["rollback", "committed", "invalid", "unknown", "cleanup", "nonzero"]
)
def test_checkpoint_consumes_only_bound_settled_jll_results(
    tmp_path, monkeypatch, case
):
    import cre_checkpoint_refresh as refresh
    import cre_jll_guard_result as result_module

    checkpoint = {"state": "dry_run_passed", "artifact": {}}
    manifest = {
        "sources": {"jll": checkpoint},
        "collector_git_sha": "e" * 40,
        "config": {"transactions": ["sale", "lease"]},
    }
    monkeypatch.setattr(refresh, "save_manifest", lambda *a: None)
    monkeypatch.setattr(refresh, "manifest_database_target_sha256", lambda *a: "c" * 64)
    monkeypatch.setattr(refresh, "verified_subset_candidate", lambda *a: False)
    monkeypatch.setattr(refresh, "run_command", lambda *a, **k: 1 if case == "nonzero" else 0)
    if case == "nonzero":
        monkeypatch.setattr(refresh, "poll_hook", lambda: lambda: None)
    monkeypatch.setattr(result_module, "expected_binding", lambda *a: BINDING)
    import cre_pyramid_rollback_result as shared
    monkeypatch.setattr(shared, "verified_rollback", lambda *a: True)
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
                else "image_loss_refused"
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
            "reason": "jll_image_guard_refusal",
            "outcome": "exact_rollback",
            "replay_safe": True,
            "validation_path": "recovery/validation.json",
        }
        raise refresh.GlobalStageError("rolled back")

    monkeypatch.setattr(refresh, "recover_interrupted_ingest", recovery)
    artifact = tmp_path / "jll.json"
    if case == "rollback":
        with pytest.raises(refresh.JllImageGuardRefusal):
            refresh.ingest_source(
                tmp_path, manifest, "jll", artifact, None
            )
        assert checkpoint["state"] == "jll_image_guard_refused"
    elif case == "committed":
        refresh.ingest_source(tmp_path, manifest, "jll", artifact, None)
        assert checkpoint["state"] == "ingested"
    else:
        error = (
            refresh.ProcessCleanupUnknown
            if case == "cleanup"
            else refresh.GlobalStageError
        )
        with pytest.raises(error):
            refresh.ingest_source(
                tmp_path, manifest, "jll", artifact, None
            )
        assert checkpoint["state"] == "ingest_recovery_required"




def test_jll_image_guard_receipt_precedes_commit_and_retains_general_guard():
    import cre_ingest
    sql = cre_ingest.build_sql([], [], "2026-11-04T00:00:00Z", set(),
                               artifact_run_key=BINDING["artifact_run_key"],
                               jll_guard_binding=BINDING)
    rollback = sql.index("\\if :jll_image_refused\nROLLBACK;")
    general = sql.index("checkpoint child quality regression before commit")
    commit = sql.index("COMMIT;", rollback)
    assert rollback < general < commit
    assert "b.source_key = 'jll' AND b.child_type = 'images'" in sql
    assert "CRE_JLL_IMAGE_GUARD_RESULT" in sql[rollback:general]
    assert '"outcome":"committed"' in sql[commit:]


@pytest.mark.parametrize("transactions", [("sale",), ("lease",), ("sale", "lease")])
def test_jll_binding_accepts_exact_configured_transaction_scope(tmp_path, monkeypatch, transactions):
    import hashlib
    import json
    from types import SimpleNamespace
    import cre_checkpoint_refresh as refresh
    import cre_ingest as ingest
    from test_cre_checkpoint_refresh import strict_artifact

    data = strict_artifact(source="jll", detail_scope="detail_page", preserve_children=False)
    data["runMeta"]["transactions"] = list(transactions)
    data["sources"] = [entry for entry in data["sources"] if entry["transaction"] in transactions]
    data["listings"] = [row for row in data["listings"] if row["transactionMode"] in transactions]
    data["totalListings"] = len(data["listings"])
    raw = json.dumps(data).encode()
    path = tmp_path / "artifact.json"
    path.write_bytes(raw)
    key, _ = ingest.artifact_run_identity_from_digests([hashlib.sha256(raw).hexdigest()])
    args = SimpleNamespace(inputs=[str(path)], checkpoint_ingest_attempt="c" * 32,
        checkpoint_jll_code_sha="d" * 40, checkpoint_jll_transactions=",".join(transactions),
        checkpoint_pyramid_code_sha=None, checkpoint_collector_git_sha=None,
        expected_db_target_sha256="b" * 64, require_strict_freshness=True,
        mark_missing=False, activate_status=False, daum_verified_subset_claims=None,
        pyramid_verified_subset_claims=None, matthews_targeted_claims=None)
    monkeypatch.setattr(refresh, "git_identity", lambda: ("d" * 40, False))
    binding = ingest.jll_checkpoint_binding(args, data, raw, key)
    assert binding["artifact_sha256"] == hashlib.sha256(raw).hexdigest()
    args.checkpoint_jll_transactions = "lease" if transactions != ("lease",) else "sale"
    with pytest.raises(ValueError):
        ingest.jll_checkpoint_binding(args, data, raw, key)
