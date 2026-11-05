import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_franklin_site_projection import artifact

import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series
import cre_franklin_site_admission as admission
import cre_ingest as ingest
import cre_refresh_pipeline as pipeline
import cre_validate as validation
from cre_franklin_site_partial import derive, prepare_candidate, validate_derived


def mixed():
    data = artifact()
    data["runMeta"].update(transactions=["sale", "lease"], maxItemsPerSource=None,
                           freshness={"generationId": "run-1", "generationStartedAt": "2026-11-04T11:59:00Z",
                                      "requireFreshDetails": True})
    peer = {"sourceKey": "franklin-street", "transactionMode": "lease", "id": "buildout-1",
            "url": "https://buildout.example/1", "name": "private Buildout peer"}
    data["sources"].append({"sourceKey": "franklin-street", "transaction": "lease", "supported": True,
                            "truncated": False, "listingsCollected": 1})
    data["listings"] = [peer]
    data["brokers"] = [{"name": "private broker"}]
    data["totalListings"] = 1
    raw = json.dumps(data).encode()
    binding = {"artifact_sha256": hashlib.sha256(raw).hexdigest(), "generation_id": "run-1",
               "generation_started_at": "2026-11-04T11:59:00Z",
               "started_at": "2026-11-04T12:00:00Z", "finished_at": "2026-11-04T12:00:02Z"}
    return raw, binding


def test_mixed_partial_accounts_pending_peers_without_claiming_they_were_saved(tmp_path):
    raw, binding = mixed()
    result = derive(raw, binding, {"PATH": os.environ["PATH"]})
    meta = result["runMeta"]["franklinSitePartial"]
    assert meta["buildoutPending"] == 1
    assert meta["coverage"] == "partial_unknown"
    assert meta["identityUncertainty"]["siteToBuildout"] == "unresolved"
    assert len(result["listings"]) == 1
    assert result["listings"][0]["id"].startswith("franklin-site:sale:")
    assert result["sources"][1]["error"] == "buildout_peers_pending"
    assert result["brokers"] == []
    assert json.loads(raw)["listings"][0]["name"] == "private Buildout peer"
    path = tmp_path / "partial.json"
    path.write_text(json.dumps(result))
    cli = Path(__file__).resolve().parent.parent / "cre_ingest.py"
    response = subprocess.run([sys.executable, str(cli), "--in", str(path), "--dry-run"],
                              check=False, capture_output=True, text=True)
    assert response.returncode != 0
    assert "refusing Franklin site partial without guarded checkpoint admission" in response.stderr


def test_invalid_sale_page_keeps_valid_lease_in_derived_candidate():
    raw, binding = mixed()
    original = json.loads(raw)
    bad = original["sources"][0]["retainedObservations"][0]
    good = json.loads(json.dumps(bad))
    good["transaction"] = "lease"
    good["sourceUrl"] = "https://franklinst.com/properties/for-lease/"
    good["decodedHtml"] = good["decodedHtml"].replace("FOR_SALE", "FOR_LEASE").replace("/for-sale/", "/for-lease/")
    good["decodedHtmlSha256"] = hashlib.sha256(good["decodedHtml"].encode()).hexdigest()
    good["cards"][0]["html"] = good["decodedHtml"]
    good["cards"][0]["attributes"]["data-offer-type"] = "FOR_LEASE"
    bad["decodedHtmlSha256"] = "0" * 64
    original["sources"][1] = {"sourceKey": "franklin-street", "transaction": "lease",
                              "error": "failed", "retainedObservations": [good]}
    original["listings"] = []
    original["totalListings"] = 0
    raw = json.dumps(original).encode()
    binding["artifact_sha256"] = hashlib.sha256(raw).hexdigest()
    result = derive(raw, binding, {"PATH": os.environ["PATH"]})
    assert [row["transactionMode"] for row in result["listings"]] == ["lease"]
    assert result["runMeta"]["franklinSitePartial"]["heldCardObservations"] == 1
    assert result["runMeta"]["franklinSitePartial"]["coverage"] == "partial_unknown"
    assert json.loads(raw)["sources"][0]["retainedObservations"][0] == bad
    assert len(validate_derived(result, "run-1", binding["generation_started_at"], "2026-11-04T11:59:30Z",
                                now=datetime(2026, 11, 4, 12, 0, 3, tzinfo=UTC))) == 1


def test_wrong_artifact_binding_cannot_be_projected():
    raw, binding = mixed()
    binding["artifact_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        derive(raw, binding, {"PATH": os.environ["PATH"]})

def test_derived_structure_is_source_local_fresh_and_explicitly_partial():
    raw, binding = mixed()
    result = derive(raw, binding, {"PATH": os.environ["PATH"]})
    def validate(value):
        return validate_derived(value, "run-1", "2026-11-04T11:59:00Z",
                                "2026-11-04T11:59:30Z", now=datetime(2026, 11, 4, 12, 0, 3, tzinfo=UTC))
    assert len(validate(result)) == 1
    result["runMeta"]["franklinSitePartial"]["identityUncertainty"]["siteToBuildout"] = "guessed"
    with pytest.raises(ValueError):
        validate(result)


def test_checkpoint_preliminary_validation_keeps_partial_marker(tmp_path):
    raw, binding = mixed()
    derived = derive(raw, binding, {"PATH": os.environ["PATH"]})
    path = tmp_path / "site.json"
    path.write_text(json.dumps(derived))
    kwargs = {"require_strict_freshness": True, "expected_generation_id": "run-1",
              "expected_generation_started_at": "2026-11-04T11:59:00Z",
              "now": datetime(2026, 11, 4, 12, 0, 3, tzinfo=UTC)}
    result = refresh._validate_source_artifact_local(path, "franklin-street", "2026-11-04T11:59:30Z", **kwargs)
    assert result["franklin_partial"] is True
    assert result["strict_freshness"] is False
    with pytest.raises(refresh.ArtifactValidationError):
        refresh._validate_source_artifact_local(path, "svn", "2026-11-04T11:59:30Z", **kwargs)


def test_settled_candidate_keeps_private_original_and_never_overwrites(tmp_path):
    raw, binding = mixed()
    run = tmp_path / "run-1"
    original = run / "rejected" / "franklin-street-attempt-1.json"
    original.parent.mkdir(parents=True)
    original.write_bytes(raw)
    original.chmod(0o600)
    manifest = {"run_id": "run-1", "started_at": binding["generation_started_at"],
                "collector_git_sha": "a" * 40, "collector_git_dirty": False,
                "config": {"transactions": ["sale", "lease"], "max_items": 0, "additive": True,
                           "mark_missing": False, "status_activation": False}}
    attempt = {"rc": 1, "finished_at": "2026-11-04T12:00:03Z", "started_at": "2026-11-04T11:59:30Z"}
    manifest["config"]["sources"] = ["franklin-street"]
    manifest["sources"] = {"franklin-street": {"attempts": [attempt]}}
    result = prepare_candidate(run, manifest, attempt, "rejected/franklin-street-attempt-1.json",
                               binding["artifact_sha256"], {"PATH": os.environ["PATH"]},
                               git_identity=lambda: ("a" * 40, False),
                               now=datetime(2026, 11, 4, 12, 0, 3, tzinfo=UTC))
    assert original.read_bytes() == raw
    assert result["buildout_pending"] == 1
    assert (run / result["path"]).stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        prepare_candidate(run, manifest, attempt, "rejected/franklin-street-attempt-1.json",
                          binding["artifact_sha256"], {"PATH": os.environ["PATH"]},
                          git_identity=lambda: ("a" * 40, False),
                               now=datetime(2026, 11, 4, 12, 0, 3, tzinfo=UTC))


def test_failed_attempt_becomes_preliminary_candidate_without_source_acceptance(tmp_path, monkeypatch):
    raw, _ = mixed()
    original_data = json.loads(raw)
    now = datetime.now(UTC)
    generation_start = (now - timedelta(minutes=3)).isoformat()
    attempt_start = (now - timedelta(minutes=2)).isoformat()
    started = (now - timedelta(minutes=1)).isoformat()
    observed = (now - timedelta(seconds=30)).isoformat()
    finished = now.isoformat()
    original_data["runMeta"].update(startedAt=started, finishedAt=finished,
                                   freshness={"generationId": "run-1", "generationStartedAt": generation_start,
                                              "requireFreshDetails": True})
    original_data["sources"][0]["retainedObservations"][0]["observedAt"] = observed
    raw = json.dumps(original_data).encode()
    run = tmp_path / "run-1"
    original = run / "rejected" / "franklin-street-attempt-1.json"
    original.parent.mkdir(parents=True)
    original.write_bytes(raw)
    original.chmod(0o600)
    attempt = {"number": 1, "rc": 80, "started_at": attempt_start, "finished_at": finished,
               "rejected_artifact": "rejected/franklin-street-attempt-1.json",
               "rejected_artifact_sha256": hashlib.sha256(raw).hexdigest()}
    manifest = {"run_id": "run-1", "started_at": generation_start,
                "collector_git_sha": "a" * 40, "collector_git_dirty": False,
                "config": {"sources": ["franklin-street"], "transactions": ["sale", "lease"],
                           "max_items": 0, "additive": True, "mark_missing": False, "status_activation": False},
                "sources": {"franklin-street": {"attempts": [attempt], "state": "collecting"}}}
    monkeypatch.setattr(refresh, "git_identity", lambda: ("a" * 40, False))
    monkeypatch.setattr(refresh, "save_manifest", lambda *_: None)
    result = refresh.prepare_franklin_partial_candidate(run, manifest, attempt, candidate_only=True)
    assert result is not None and original.read_bytes() == raw
    checkpoint = manifest["sources"]["franklin-street"]
    assert checkpoint["state"] == "candidate"
    assert checkpoint["franklin_original"]["buildout_pending"] == 1
    assert not refresh.gate_verdict_is_admitted(manifest, "hold", source="franklin-street")


def test_pipeline_mixed_rejection_retains_original_before_partial_candidate(tmp_path, monkeypatch):
    raw, _ = mixed()
    data = json.loads(raw)
    now = datetime.now(UTC)
    generation = (now - timedelta(minutes=3)).isoformat()
    attempt_start = (now - timedelta(minutes=2)).isoformat()
    started = (now - timedelta(minutes=1)).isoformat()
    observed = (now - timedelta(seconds=30)).isoformat()
    finished = now.isoformat()
    data["runMeta"].update(startedAt=started, finishedAt=finished,
                           freshness={"generationId": "run-1", "generationStartedAt": generation,
                                      "requireFreshDetails": True})
    data["sources"][0]["retainedObservations"][0]["observedAt"] = observed
    raw = json.dumps(data).encode()
    run = tmp_path / "run-1"
    path = run / "sources" / "franklin-street.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    path.chmod(0o600)
    st = path.stat()
    attempt = {"number": 1, "rc": 0, "started_at": attempt_start, "finished_at": finished}
    checkpoint = {"attempts": [attempt], "state": "candidate",
                  "candidate": {"path": "sources/franklin-street.json", "attempt_number": 1,
                                "attempt_started_at": attempt_start,
                                "file_identity": [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns],
                                "bytes": st.st_size}}
    manifest = {"run_id": "run-1", "started_at": generation,
                "collector_git_sha": "a" * 40, "collector_git_dirty": False,
                "config": {"sources": ["franklin-street"], "transactions": ["sale", "lease"],
                           "max_items": 0, "additive": True, "mark_missing": False, "status_activation": False},
                "sources": {"franklin-street": checkpoint}}
    monkeypatch.setattr(refresh, "git_identity", lambda: ("a" * 40, False))
    monkeypatch.setattr(refresh, "save_manifest", lambda *_: None)
    calls = []
    def compute(_root, _action, arguments):
        calls.append(1)
        if len(calls) == 1:
            raise refresh.ArtifactValidationError("mixed source incomplete")
        kwargs = arguments["kwargs"]
        return refresh._validate_source_artifact_local(Path(arguments["path"]), arguments["source"],
                                                       arguments["attempt_started_at"], **kwargs)
    monkeypatch.setattr(pipeline, "run_artifact_compute", compute)
    assert pipeline.prepare_candidate(run, manifest, "franklin-street")
    assert checkpoint["state"] == "validated"
    assert checkpoint["artifact"]["franklin_partial"] is True
    assert checkpoint["franklin_original"]["buildout_pending"] == 1
    assert (run / checkpoint["franklin_original"]["path"]).read_bytes() == raw
    assert len(calls) == 2


def test_source_and_aggregate_gate_rederive_original_and_bind_pending_peers(tmp_path, monkeypatch):
    raw, _ = mixed()
    data = json.loads(raw)
    now = datetime.now(UTC)
    generation = (now - timedelta(minutes=3)).isoformat()
    attempt_start = (now - timedelta(minutes=2)).isoformat()
    started = (now - timedelta(minutes=1)).isoformat()
    observed = (now - timedelta(seconds=30)).isoformat()
    finished = now.isoformat()
    data["runMeta"].update(startedAt=started, finishedAt=finished,
                           freshness={"generationId": "run-1", "generationStartedAt": generation,
                                      "requireFreshDetails": True})
    data["sources"][0]["retainedObservations"][0]["observedAt"] = observed
    raw = json.dumps(data).encode()
    run = tmp_path / "run-1"
    original = run / "rejected" / "franklin-street-attempt-1.json"
    original.parent.mkdir(parents=True)
    original.write_bytes(raw)
    original.chmod(0o600)
    attempt = {"number": 1, "rc": 80, "started_at": attempt_start, "finished_at": finished,
               "rejected_artifact": "rejected/franklin-street-attempt-1.json",
               "rejected_artifact_sha256": hashlib.sha256(raw).hexdigest()}
    checkpoint = {"attempts": [attempt], "state": "collecting"}
    manifest = {"run_id": "run-1", "started_at": generation,
                "collector_git_sha": "a" * 40, "collector_git_dirty": False,
                "config": {"sources": ["franklin-street"], "transactions": ["sale", "lease"],
                           "max_items": 0, "additive": True, "mark_missing": False,
                           "status_activation": False, "pipeline": False},
                "scope": {"source_keys": ["franklin-street"]},
                "preflight": {"database_target": {"algorithm": "sha256", "value": "b" * 64}},
                "sources": {"franklin-street": checkpoint}}
    monkeypatch.setattr(refresh, "git_identity", lambda: ("a" * 40, False))
    monkeypatch.setattr(refresh, "save_manifest", lambda *_: None)
    assert refresh.prepare_franklin_partial_candidate(run, manifest, attempt, candidate_only=False)
    path = run / checkpoint["artifact"]["path"]
    env = {"PATH": os.environ["PATH"]}
    monkeypatch.setattr(ingest, "load_db_url", lambda _: ("postgres://fixture", "fixture"))
    monkeypatch.setattr(ingest, "database_target_fingerprint_from_url", lambda _: {"algorithm": "sha256", "value": "b" * 64})
    monkeypatch.setattr(ingest, "find_psql", lambda: "psql")
    monkeypatch.setattr(validation, "_run_psql_script",
                        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout='result\n{"staged":1,"joined":1,"held":[]}\n'))
    checkpoint["franklin_identity_probe"] = admission.run_identity_probe(path, manifest, env_file=None, process_env=env)
    def report():
        return {"generated_for_artifact": str(path),
                "per_source": {"franklin-street": {"current_active": 1, "verdict": "hold",
                               "reason": "source pass error: source_incomplete", "mark_missing_safe": False}},
                "summary": {"mode": "live", "torow_errors": 0, "baseline_updated": False,
                            "baseline_update_sources": [], "hold_sources": ["franklin-street"],
                            "mark_missing_safe_sources": [], "mark_missing_safe_brokerages": []}}
    first = report()
    binding = admission.admit(path, manifest, first, code_sha="a" * 40, dirty=False, env=env, phase="source")
    assert binding["buildout_pending"] == 1 and binding["whole_source_coverage"] is False
    checkpoint["gate"] = {"current_inventory_proof": binding}
    second = report()
    assert admission.admit(path, manifest, second, code_sha="a" * 40, dirty=False, env=env, phase="aggregate") == binding
    envelope = {"manifest": manifest, "source_report": first, "aggregate_report": second,
                "binding": binding, "attempt_id": "c" * 32}
    context = admission.ingest_subset_context(path, envelope, code_sha="a" * 40, dirty=False,
                                               target_sha256="b" * 64, attempt_id="c" * 32, env=env)
    assert context["sql_binding"]["artifact_sha256"] == checkpoint["artifact"]["sha256"]
    assert context["sql_binding"]["generation_id"] == "run-1"
    assert len(context["listings"]) == 1 and context["held"] == []
    monkeypatch.setattr(validation, "_run_psql_script",
                        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout='result\n{"expected":1,"invalid":[],"matching_jobs":1}\n'))
    result = admission.run_readback(path, manifest, context, target_sha256="b" * 64, env_file=None, process_env=env)
    assert result["ok"] is True and result["expected"] == 1 and result["held"] == []
    claims_dir = run / "claims"
    claims_dir.mkdir(mode=0o700)
    claims = claims_dir / "claims.json"
    claims.write_text(json.dumps(envelope))
    claims.chmod(0o600)
    args = SimpleNamespace(inputs=[str(path)], mark_missing=False, activate_status=False,
        require_strict_freshness=True, expected_db_target_sha256="b" * 64,
        checkpoint_ingest_attempt="c" * 32, jll_partial_claims=None, colliers_partial_claims=None,
        daum_verified_subset_claims=None, pyramid_verified_subset_claims=None,
        matthews_targeted_claims=None, newmark_page_attempt=None, newmark_page_claims=None,
        checkpoint_collector_git_sha=None, checkpoint_pyramid_code_sha=None, checkpoint_jll_code_sha=None,
        franklin_site_claims=str(claims), keep_artifacts=str(run / "dry"), dry_run=True, env_file=None)
    admission.run_ingest(args)
    assert (run / "dry" / "ingest.sql").is_file()
    args.colliers_partial_claims = "other-claim"
    with pytest.raises(ValueError, match="unbound Franklin"):
        admission.run_ingest(args)
    args.colliers_partial_claims = None
    args.mark_missing = True
    with pytest.raises(ValueError):
        admission.run_ingest(args)
    with pytest.raises(ValueError):
        admission.ingest_subset_context(path, envelope, code_sha="a" * 40, dirty=False,
                                        target_sha256="0" * 64, attempt_id="c" * 32, env=env)
    original.write_bytes(raw + b" ")
    with pytest.raises(ValueError):
        admission.admit(path, manifest, report(), code_sha="a" * 40, dirty=False, env=env, phase="source")


def test_series_recognizes_only_exact_partial_binding():
    binding = {"schema": "franklin-site-partial-v1", "scope": "franklin_site_partial_v1",
               "whole_source_coverage": False, "artifact_sha256": "1" * 64,
               "source_artifact_sha256": "2" * 64, "generation_id": "run-1",
               "collector_git_sha": "a" * 40, "database_target_sha256": "b" * 64,
               "held_count": 0, "held": [], "emitted_count": 1, "buildout_pending": 1,
               "held_observations": 0, "identity_uncertainty": {"siteToBuildout": "unresolved"}}
    child = {"run_id": "run-1", "collector_git_sha": "a" * 40,
             "preflight": {"database_target": {"value": "b" * 64}},
             "sources": {"franklin-street": {"artifact": {"franklin_partial": True, "sha256": "1" * 64,
                                                            "staged_unique": 1},
                         "franklin_original": {"sha256": "2" * 64},
                         "gate": {"verdict": "ok_additive_verified_subset", "mark_missing_safe": False,
                                  "current_inventory_proof": binding}}}}
    assert series.franklin_partial_child(child)
    binding["whole_source_coverage"] = True
    assert not series.franklin_partial_child(child)


def recovery_manifest(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    manifest = refresh.new_manifest(run, git_sha="a" * 40, git_dirty=False,
                                    sources=("franklin-street",), page_cap=400, concurrency=1,
                                    database_target={"algorithm": "sha256", "value": "b" * 64})
    manifest["sources"]["franklin-street"].update(
        artifact={"franklin_partial": True, "sha256": "1" * 64, "path": "sources/site.json"},
        state="ingesting", ingest={"started_at": "2026-11-04T12:00:00Z", "finished_at": None, "rc": None})
    return run, manifest


def test_interrupted_owner_never_probes_or_replays_without_settlement(tmp_path, monkeypatch):
    run, manifest = recovery_manifest(tmp_path)
    monkeypatch.setattr(refresh, "run_command", lambda *_args, **_kwargs: pytest.fail("unsettled owner probed DB"))
    with pytest.raises(refresh.GlobalStageError, match="settled-owner"):
        refresh.recover_interrupted_ingest(run, manifest, "franklin-street", None)
    checkpoint = manifest["sources"]["franklin-street"]
    assert checkpoint["state"] == "ingest_recovery_required"
    assert checkpoint["ingest_recovery"]["reason"] == "franklin_unsettled_ingest"


def test_nonzero_ingest_requires_reconciliation_before_peer_dispatch(tmp_path, monkeypatch):
    run, manifest = recovery_manifest(tmp_path)
    checkpoint = manifest["sources"]["franklin-street"]
    checkpoint["state"] = "dry_run_passed"
    checkpoint["subset_ingest_attempt_id"] = "c" * 32
    monkeypatch.setattr(refresh, "subset_ingest_arguments", lambda *_args: ["--franklin-site-claims", "fixture"])
    monkeypatch.setattr(refresh, "run_command", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(refresh, "recover_interrupted_ingest",
                        lambda *_args: pytest.fail("nonzero outcome was automatically replayed"))
    with pytest.raises(refresh.GlobalStageError, match="manual exact-generation reconciliation"):
        refresh.ingest_source(run, manifest, "franklin-street", run / "sources/site.json", None)
    assert checkpoint["state"] == "ingest_recovery_required"
    assert checkpoint["ingest_recovery"]["readback_ok"] is False


@pytest.mark.parametrize("exact", [False, True])
def test_settled_recovery_requires_exact_job_and_card_readback(tmp_path, monkeypatch, exact):
    run, manifest = recovery_manifest(tmp_path)
    checkpoint = manifest["sources"]["franklin-street"]
    checkpoint["ingest"]["finished_at"] = "2026-11-04T12:05:00Z"
    def fake_run(argv, _log, **_kwargs):
        Path(argv[argv.index("--out") + 1]).write_text("{}")
        return 0
    monkeypatch.setattr(refresh, "run_command", fake_run)
    monkeypatch.setattr(refresh, "require_full_validation_report", lambda *_args: None)
    monkeypatch.setattr(refresh, "franklin_partial_readback", lambda *_args: {"ok": exact})
    monkeypatch.setattr(refresh, "verify_validation_readback", lambda *_args, **_kwargs: {"ok": exact})
    monkeypatch.setattr(refresh, "_artifact_run_job_probe_is_exact", lambda *_args, **_kwargs: exact)
    if exact:
        refresh.recover_interrupted_ingest(run, manifest, "franklin-street", None)
        assert checkpoint["state"] == "ingested"
        assert checkpoint["ingest_recovery"]["readback_ok"] is True
    else:
        with pytest.raises(refresh.GlobalStageError, match="partial ingest outcome is not exact"):
            refresh.recover_interrupted_ingest(run, manifest, "franklin-street", None)
        assert checkpoint["state"] == "ingest_recovery_required"
        assert checkpoint["ingest_recovery"]["readback_ok"] is False
