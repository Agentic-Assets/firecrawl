"""Offline automatic retention policy and transaction-state contracts."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series
import pytest
from cre_ingest import database_target_fingerprint_from_url
from source_evidence import acquisition, automatic, collector, events

pytest_plugins = ("test_cre_pipeline_preseal_gate",)

SHA = "a" * 40


def setup_policy(tmp_path, sources=("faris-lee",), count=78):
    url = "postgresql://evidence:fake@%2Ftmp%2Fevidence-fixture:5432/fixture"
    env = tmp_path / "writer.env"
    env.write_text("POSTGRES_URL=" + url + "\n")
    env.chmod(0o600)
    target = database_target_fingerprint_from_url(url)["value"]
    policy = {"schema_version": automatic.SCHEMA, "purpose": "private_unverified_evidence",
              "data_scope": "public_listing_source", "format": "collector_artifact_v1",
              "collector_git_sha": SHA, "target_sha256": target, "source_keys": list(sources),
              "max_artifact_bytes": 200000, "max_records": count}
    path = tmp_path / "policy.json"
    raw = json.dumps(policy).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    digest = hashlib.sha256(raw).hexdigest()
    config = automatic.configuration(str(path), digest, str(env), target, SHA, sources)
    return config, policy, path


def artifact(source="faris-lee", count=78):
    return {"runMeta": {"finishedAt": "2026-10-28T10:00:00Z", "mode": "full"},
            "sources": [{"sourceKey": source, "error": "partial", "truncated": True}],
            "listings": [{"sourceKey": source, "unknown": {"array": [None, i, i]}}
                         for i in range(count)]}


def test_retained_observations_count_toward_policy_and_replay_unchanged(tmp_path, monkeypatch):
    config, _, _ = setup_policy(tmp_path, count=78)
    obj = artifact(count=1)
    obj["sources"][0]["retainedObservations"] = [{"ordinal": i, "unknown": [None, False, ""]}
                                                 for i in range(77)]
    run, path, manifest, attempt = state(tmp_path, config, obj)
    automatic.seal(run, manifest, "faris-lee", attempt, path)
    saved = attempt["source_evidence"]
    assert saved["state"] == "pending" and saved["events"] == 79
    plan = json.loads((run / saved["path"]).read_bytes())
    header = plan["events"][0]["facts"][0]
    assert header["kind"] == "retained_capture_context_v2"
    calls = []
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *args, **kwargs: calls.append(args))
    automatic.pump(run, manifest, lambda *args: None)
    assert saved["state"] == "appended" and len(calls) == 1


def test_retained_observations_cannot_bypass_policy_record_limit(tmp_path):
    config, _, _ = setup_policy(tmp_path, count=78)
    obj = artifact(count=1)
    obj["sources"][0]["retainedObservations"] = [None] * 78
    run, path, manifest, attempt = state(tmp_path, config, obj)
    automatic.seal(run, manifest, "faris-lee", attempt, path)
    assert attempt["source_evidence"]["state"] == "refused"
    assert manifest["automatic_source_evidence_paused"] is True


def test_preexisting_v1_sealed_partial_carriers_replay_as_v1(tmp_path, monkeypatch):
    config, _, _ = setup_policy(tmp_path, count=78)
    obj = artifact(count=1)
    obj["sources"][0]["retainedObservations"] = [{"status": None}] * 60
    run, path, manifest, attempt = state(tmp_path, config, obj)
    # Model an old executable's seal without changing the current default.
    convert = collector.convert
    with monkeypatch.context() as historical:
        historical.setattr(collector, "convert", lambda *args, **kwargs: convert(
            *args, **{**kwargs, "schema": collector.SCHEMA, "integral_floats": None}))
        automatic.seal(run, manifest, "faris-lee", attempt, path)
    saved = attempt["source_evidence"]
    assert saved["state"] == "pending" and saved["events"] == 2
    calls = []
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *args, **kwargs: calls.append(args))
    automatic.pump(run, manifest, lambda *args: None)
    assert saved["state"] == "appended" and len(calls) == 1


@pytest.mark.parametrize("legacy", [False, True])
def test_scientific_float_sealed_replay_preserves_witness_presence(tmp_path, monkeypatch, legacy):
    config, _, _ = setup_policy(tmp_path, count=78)
    obj = artifact(count=78)
    for row in obj["listings"]:
        row["unknown"]["scientific"] = 1e20
    run, path, manifest, attempt = state(tmp_path, config, obj)
    if legacy:
        convert = collector.convert
        with monkeypatch.context() as historical:
            historical.setattr(collector, "convert", lambda *args, **kwargs: convert(
                *args, **{**kwargs, "integral_floats": None}))
            automatic.seal(run, manifest, "faris-lee", attempt, path)
    else:
        automatic.seal(run, manifest, "faris-lee", attempt, path)
    saved = attempt["source_evidence"]
    assert saved["state"] == "pending"
    plan = json.loads((run / saved["path"]).read_bytes())
    provenance = plan["events"][0]["facts"][0]["value"]["provenance"]
    assert ("integralFloats" not in provenance) is legacy
    if not legacy:
        assert len(provenance["integralFloats"]) == 78
    calls = []
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *args, **kwargs: calls.append(args))
    automatic.pump(run, manifest, lambda *args: None)
    assert saved["state"] == "appended" and len(calls) == 1


def state(tmp_path, config, obj):
    run = tmp_path / "generation-1"
    run.mkdir(mode=0o700)
    path = run / "candidate.json"
    path.write_text(json.dumps(obj))
    path.chmod(0o600)
    attempt = {"number": 1}
    manifest = {"run_id": run.name, "collector_git_sha": SHA, "collector_git_dirty": False,
                "config": {"sources": ["faris-lee"], "automatic_source_evidence": config},
                "sources": {"faris-lee": {"attempts": [attempt]}}}
    return run, path, manifest, attempt


def test_settle_78_partial_records_and_append_is_independent_of_source_gate(tmp_path, monkeypatch):
    config, _, _ = setup_policy(tmp_path)
    run, path, manifest, attempt = state(tmp_path, config, artifact())
    automatic.seal(run, manifest, "faris-lee", attempt, path)
    saved = attempt["source_evidence"]
    assert saved["state"] == "pending" and saved["events"] == 79
    plan = json.loads((run / saved["path"]).read_bytes())
    assert [e["facts"][0]["value"] for e in plan["events"][1:]] == artifact()["listings"]
    binding = plan["events"][0]["facts"][0]["value"]["provenance"]["automaticPolicy"]
    assert binding["policySha256"] == config["policy_sha256"]
    assert binding["artifactFileSha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    calls = []
    monkeypatch.setattr(automatic.cli, "run_psql", lambda sql, env, target, **kwargs: calls.append((sql, env, target)))
    automatic.pump(run, manifest, lambda *_, **__: None)
    assert saved["state"] == "appended" and len(calls) == 1
    assert "SET LOCAL ROLE cre_source_evidence_writer" in calls[0][0]
    assert "dedicated restricted writer login" in calls[0][0]
    automatic.pump(run, manifest, lambda *_, **__: None)
    assert len(calls) == 1


def test_unknown_commit_pauses_then_explicit_exact_replay(tmp_path, monkeypatch):
    config, _, _ = setup_policy(tmp_path)
    run, path, manifest, attempt = state(tmp_path, config, artifact())
    automatic.seal(run, manifest, "faris-lee", attempt, path)
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *_, **__: (_ for _ in ()).throw(RuntimeError("unknown")))
    automatic.pump(run, manifest, lambda *_, **__: None)
    saved = attempt["source_evidence"]
    assert saved["state"] == "outcome_unknown" and manifest["automatic_source_evidence_paused"]
    automatic.prepare_resume(manifest, replay_unknown=False)
    assert saved["state"] == "outcome_unknown"
    automatic.prepare_resume(manifest, replay_unknown=True)
    assert saved["state"] == "pending" and not manifest["automatic_source_evidence_paused"]
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *_, **__: None)
    automatic.pump(run, manifest, lambda *_, **__: None)
    assert saved["state"] == "appended"


def test_changed_seal_or_policy_stops_before_database(tmp_path, monkeypatch):
    config, _, _ = setup_policy(tmp_path)
    run, path, manifest, attempt = state(tmp_path, config, artifact())
    automatic.seal(run, manifest, "faris-lee", attempt, path)
    (run / attempt["source_evidence"]["path"]).write_text("{}")
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *_, **__: pytest.fail("database contacted"))
    automatic.pump(run, manifest, lambda *_, **__: None)
    assert attempt["source_evidence"]["state"] == "refused"
    assert manifest["automatic_source_evidence_paused"]


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public_mode"])
def test_sealed_input_alias_or_permissions_refused(tmp_path, monkeypatch, kind):
    config, _, _ = setup_policy(tmp_path)
    run, path, manifest, attempt = state(tmp_path, config, artifact())
    automatic.seal(run, manifest, "faris-lee", attempt, path)
    saved = run / attempt["source_evidence"]["input_path"]
    if kind == "public_mode":
        saved.chmod(0o644)
    else:
        other = run / "other.json"
        if kind == "hardlink":
            os.link(saved, other)
        else:
            saved.rename(other)
            saved.symlink_to(other)
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *_, **__: pytest.fail("database contacted"))
    automatic.pump(run, manifest, lambda *_, **__: None)
    assert attempt["source_evidence"]["state"] == "refused"
    assert manifest["automatic_source_evidence_paused"]


@pytest.mark.parametrize("mutation", ["credential", "secret_key", "authorization_header", "x-amz-security-token", "header_prose", "jll", "too_many", "source_mismatch"])
def test_unsafe_artifacts_refused_without_dropping_fields(tmp_path, mutation):
    config, _, _ = setup_policy(tmp_path)
    value = artifact()
    if mutation == "credential": value["listings"][0]["api_key"] = "private"
    if mutation in ("secret_key", "authorization_header", "x-amz-security-token"):
        value["listings"][0][mutation] = "private"
    if mutation == "header_prose": value["listings"][0]["notes"] = "Authorization: Bearer placeholder"
    if mutation == "jll": value["listings"][0]["description"] = "See https://JLL.com/example"
    if mutation == "too_many": value["listings"].append(copy.deepcopy(value["listings"][0]))
    if mutation == "source_mismatch": value["listings"][0]["sourceKey"] = "jll"
    run, path, manifest, attempt = state(tmp_path, config, value)
    automatic.seal(run, manifest, "faris-lee", attempt, path)
    assert attempt["source_evidence"]["state"] == "refused"
    assert manifest["automatic_source_evidence_paused"]
    assert path.exists()


def test_unknown_policy_and_target_are_rejected(tmp_path):
    config, policy, path = setup_policy(tmp_path)
    policy["source_keys"] = ["jll"]
    raw = json.dumps(policy).encode()
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        automatic.configuration(str(path), hashlib.sha256(raw).hexdigest(), config["env_file"],
                                config["target_sha256"], SHA, ["jll"])
    with pytest.raises(ValueError):
        automatic.configuration(str(path), "0" * 64, config["env_file"], config["target_sha256"], SHA, ["jll"])


def test_default_off_and_no_artifact_are_explicit(tmp_path):
    config, _, _ = setup_policy(tmp_path)
    run, path, manifest, attempt = state(tmp_path, config, artifact())
    manifest["config"].pop("automatic_source_evidence")
    automatic.seal(run, manifest, "faris-lee", attempt, path)
    assert "source_evidence" not in attempt
    manifest["config"]["automatic_source_evidence"] = config
    path.unlink()
    automatic.seal(run, manifest, "faris-lee", attempt, path)
    assert attempt["source_evidence"]["state"] == "no_artifact"
    assert manifest["automatic_source_evidence_status"] == "incomplete"


def test_transport_requires_verified_tls_and_binds_ca(tmp_path):
    ca = tmp_path / "root.crt"
    ca.write_bytes(b"fixture-ca-one")
    base = "postgresql://writer@example.test/evidence"
    encoded_ca = quote(str(ca), safe="")
    valid = f"{base}?sslmode=verify-full&sslrootcert={encoded_ca}&gssencmode=disable"
    first = automatic._transport_binding(valid)
    assert first["kind"] == "tls_verify_full" and first["ca_sha256"] == hashlib.sha256(ca.read_bytes()).hexdigest()
    ca.write_bytes(b"fixture-ca-two")
    assert automatic._transport_binding(valid) != first
    for invalid in (base, f"{base}?sslmode=require&sslrootcert={encoded_ca}&gssencmode=disable",
                    f"{base}?sslmode=verify-full&sslrootcert={encoded_ca}",
                    f"{valid}&sslmode=disable", f"{base}?sslmode=verify-full&sslrootcert=relative&gssencmode=disable"):
        with pytest.raises(ValueError):
            automatic._transport_binding(invalid)
    assert automatic._transport_binding("postgresql://writer@%2Ftmp%2Ffixture/db")["kind"] == "unix_socket"


@pytest.mark.parametrize("mutation", ["ca", "tls_mode"])
def test_pump_refuses_transport_drift_before_database(tmp_path, monkeypatch, mutation):
    config, policy, path = setup_policy(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_bytes(b"fixture-ca")
    url = f"postgresql://writer@example.test/evidence?sslmode=verify-full&sslrootcert={quote(str(ca), safe='')}&gssencmode=disable"
    target = database_target_fingerprint_from_url(url)["value"]
    env = Path(config["env_file"])
    env.write_text("POSTGRES_URL=" + url + "\n")
    policy["target_sha256"] = target
    policy_bytes = json.dumps(policy).encode()
    path.write_bytes(policy_bytes)
    config = automatic.configuration(str(path), hashlib.sha256(policy_bytes).hexdigest(), str(env), target, SHA, ["faris-lee"])
    run, artifact_path, manifest, attempt = state(tmp_path, config, artifact())
    automatic.seal(run, manifest, "faris-lee", attempt, artifact_path)
    if mutation == "ca":
        ca.write_bytes(b"different-ca")
    else:
        env.write_text("POSTGRES_URL=" + url.replace("verify-full", "disable") + "\n")
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *_, **__: pytest.fail("database contacted"))
    automatic.pump(run, manifest, lambda *_: None)
    assert attempt["source_evidence"]["state"] == "refused"
    assert manifest["automatic_source_evidence_paused"]


def test_manual_admission_still_requires_per_plan_receipt(tmp_path):
    config, _, _ = setup_policy(tmp_path)
    run, path, manifest, attempt = state(tmp_path, config, artifact())
    automatic.seal(run, manifest, "faris-lee", attempt, path)
    plan = json.loads((run / attempt["source_evidence"]["path"]).read_bytes())
    events.validate_content_policy(plan)
    with pytest.raises(ValueError, match="admission"):
        events.validate_admission(plan, {"policy_sha256": config["policy_sha256"]})


def test_default_off_hook_has_no_git_or_db_effect(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh, "git_identity", lambda: pytest.fail("unexpected git read"))
    refresh.seal_source_evidence(tmp_path, {"config": {}}, "faris-lee", {}, tmp_path / "absent")
    refresh.pump_source_evidence(tmp_path, {"config": {}})


@pytest.mark.parametrize("exit_code,candidate_only", [(0, True), (0, False), (refresh.COHORT_SETTLED_FAILURE_EXIT, True)])
def test_cohort_settlement_seals_inside_callback_but_never_writes(tmp_path, monkeypatch, exit_code, candidate_only):
    config, _, _ = setup_policy(tmp_path)
    run = tmp_path / "generation-2"
    run.mkdir()
    manifest = refresh.new_manifest(run, git_sha=SHA, git_dirty=False, sources=("faris-lee",), page_cap=400,
                                    concurrency=1, automatic_source_evidence=config)
    attempt, path, _, started = refresh._cohort_attempt(run, manifest, "faris-lee", collector_concurrency=1)
    path.write_text(json.dumps(artifact()))
    item = refresh.CohortCollectionProcess("faris-lee", SimpleNamespace(poll=lambda: exit_code, returncode=exit_code),
                                           io.StringIO(), path, attempt, started, object())
    monkeypatch.setattr(refresh, "_require_completed_group_settlement", lambda *_, **__: None)
    monkeypatch.setattr(refresh, "git_identity", lambda: (SHA, False))
    monkeypatch.setattr(refresh, "validate_source_artifact", lambda *_args, **_kwargs: {"staged_unique": 78})
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *_, **__: pytest.fail("write inside settlement callback"))
    result = refresh._finalize_cohort_collection(run, manifest, item, candidate_only=candidate_only)
    assert result is (exit_code == 0)
    assert attempt["source_evidence"]["state"] == "pending"
    assert attempt["source_evidence"]["events"] == 79
    assert (run / attempt["source_evidence"]["input_path"]).stat().st_mode & 0o777 == 0o600


def test_serial_failed_artifact_is_retained_before_retry_or_gate(tmp_path, monkeypatch):
    config, _, _ = setup_policy(tmp_path)
    run = tmp_path / "generation-3"
    run.mkdir()
    manifest = refresh.new_manifest(run, git_sha=SHA, git_dirty=False, sources=("faris-lee",), page_cap=400,
                                    concurrency=1, automatic_source_evidence=config)
    monkeypatch.setattr(refresh, "git_identity", lambda: (SHA, False))
    def command(argv, *_args, **_kwargs):
        output = next(x.removeprefix("--out=") for x in argv if x.startswith("--out="))
        from pathlib import Path
        Path(output).write_text(json.dumps(artifact()))
        return 1
    monkeypatch.setattr(refresh, "run_command", command)
    calls = []
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *_, **__: calls.append(1))
    assert refresh.collect_source(run, manifest, "faris-lee", transactions=("sale", "lease"), page_cap=400,
                                  concurrency=1, attempts_this_run=1) is None
    attempt = manifest["sources"]["faris-lee"]["attempts"][0]
    assert attempt["source_evidence"]["state"] == "appended"
    assert len(calls) == 1 and attempt["rejected_artifact"]
    assert manifest["sources"]["faris-lee"]["state"] == "collect_failed"


@pytest.mark.parametrize("cohort", [True, False])
def test_success_exit_without_artifact_records_incomplete(tmp_path, monkeypatch, cohort):
    config, _, _ = setup_policy(tmp_path)
    run = tmp_path / "generation-missing"
    run.mkdir()
    manifest = refresh.new_manifest(run, git_sha=SHA, git_dirty=False, sources=("faris-lee",), page_cap=400,
                                    concurrency=1, automatic_source_evidence=config)
    monkeypatch.setattr(refresh, "git_identity", lambda: (SHA, False))
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *_, **__: pytest.fail("database contacted"))
    if cohort:
        attempt, path, _, started = refresh._cohort_attempt(run, manifest, "faris-lee", collector_concurrency=1)
        item = refresh.CohortCollectionProcess("faris-lee", SimpleNamespace(poll=lambda: 0, returncode=0),
                                               io.StringIO(), path, attempt, started, object())
        monkeypatch.setattr(refresh, "_require_completed_group_settlement", lambda *_: None)
        assert not refresh._finalize_cohort_collection(run, manifest, item, candidate_only=True)
    else:
        monkeypatch.setattr(refresh, "run_command", lambda *_, **__: 0)
        assert refresh.collect_source(run, manifest, "faris-lee", transactions=("sale", "lease"), page_cap=400,
                                      concurrency=1, attempts_this_run=1) is None
        attempt = manifest["sources"]["faris-lee"]["attempts"][0]
    assert attempt["source_evidence"]["state"] == "no_artifact"
    assert manifest["automatic_source_evidence_status"] == "incomplete"
    assert manifest["sources"]["faris-lee"]["state"] == "artifact_rejected"


@pytest.mark.parametrize("cohort,rc,alias", [(c, r, a) for c in (True, False) for r in (0, 1) for a in ("symlink", "hardlink")])
def test_unsafe_artifacts_record_refusal_without_reading_target(tmp_path, monkeypatch, cohort, rc, alias):
    config, _, _ = setup_policy(tmp_path)
    run = tmp_path / "generation-unsafe"
    run.mkdir()
    foreign = tmp_path / "foreign.json"
    foreign.write_text("unrelated private content")
    foreign.chmod(0o644)
    manifest = refresh.new_manifest(run, git_sha=SHA, git_dirty=False, sources=("faris-lee",), page_cap=400,
                                    concurrency=1, automatic_source_evidence=config)
    monkeypatch.setattr(refresh, "git_identity", lambda: (SHA, False))
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *_, **__: pytest.fail("database contacted"))
    def link(path):
        if alias == "symlink":
            path.symlink_to(foreign)
        else:
            os.link(foreign, path)
    if cohort:
        attempt, path, _, started = refresh._cohort_attempt(run, manifest, "faris-lee", collector_concurrency=1)
        link(path)
        exit_code = refresh.COHORT_SETTLED_FAILURE_EXIT if rc else 0
        item = refresh.CohortCollectionProcess("faris-lee", SimpleNamespace(poll=lambda: exit_code, returncode=exit_code),
                                               io.StringIO(), path, attempt, started, object())
        monkeypatch.setattr(refresh, "_require_completed_group_settlement", lambda *_: None)
        if rc == 0:
            assert not refresh._finalize_cohort_collection(run, manifest, item, candidate_only=True)
        else:
            with pytest.raises(refresh.ArtifactValidationError):
                refresh._finalize_cohort_collection(run, manifest, item, candidate_only=True)
    else:
        def command(argv, *_args, **_kwargs):
            link(Path(next(x.removeprefix("--out=") for x in argv if x.startswith("--out="))))
            return rc
        monkeypatch.setattr(refresh, "run_command", command)
        with pytest.raises(refresh.ArtifactValidationError):
            refresh.collect_source(run, manifest, "faris-lee", transactions=("sale", "lease"), page_cap=400,
                                   concurrency=1, attempts_this_run=1)
        attempt = manifest["sources"]["faris-lee"]["attempts"][0]
    assert attempt["source_evidence"] == {"state": "refused", "reason": "unsafe_artifact"}
    assert manifest["automatic_source_evidence_paused"]
    assert manifest["automatic_source_evidence_status"] == "incomplete"
    assert foreign.read_text() == "unrelated private content" and foreign.stat().st_mode & 0o777 == 0o644


def test_series_forwards_bound_policy_and_child_config(tmp_path):
    config, _, _ = setup_policy(tmp_path)
    policy = series.series_config(sources=("faris-lee",), page_cap=400, concurrency=1, attempts_per_source=1,
                                  max_resume_age_hours=24, max_host_cpu_percent=90, cpu_sustain_seconds=30,
                                  cpu_sample_seconds=2, nice=10, automatic_source_evidence=config)
    argv = series.build_checkpoint_argv("faris-lee", child_out_root=tmp_path, env_file=None, config=policy)
    assert argv[argv.index("--source-evidence-policy-sha256") + 1] == config["policy_sha256"]
    assert argv[argv.index("--source-evidence-target-sha256") + 1] == config["target_sha256"]
    assert series._expected_child_config("faris-lee", policy)["automatic_source_evidence"] == config


@pytest.mark.parametrize("evidence_state", ["appending", "outcome_unknown", "refused"])
def test_series_never_treats_uncertain_evidence_as_source_local(evidence_state):
    child = {"status": "failed", "error": "RefreshError: source checkpoints remain incomplete: faris-lee",
             "sources": {"faris-lee": {"attempts": [{"source_evidence": {"state": evidence_state}}]}}}
    assert series.evidence_reconciliation_required(child)
    assert not series.source_local_failure(child)
    assert series._reconciled_state(child) == "failed_global"


def test_series_does_not_accept_canonical_success_as_retention_success():
    child = {"status": series.SUCCESS_STATUS, "automatic_source_evidence_status": "incomplete", "sources": {}}
    assert not series._child_succeeded(child, {"automatic_source_evidence": {"enabled": True}})


def test_checkpoint_resume_binds_full_policy_config(tmp_path):
    config, _, _ = setup_policy(tmp_path)
    run = tmp_path / "generation-4"
    run.mkdir()
    manifest = refresh.new_manifest(run, git_sha=SHA, git_dirty=False, sources=("faris-lee",),
                                    page_cap=400, concurrency=1, automatic_source_evidence=config)
    refresh.save_manifest(run, manifest)
    loaded = refresh.load_resume_manifest(run / "manifest.json", git_sha=SHA, sources=("faris-lee",),
                                          page_cap=400, concurrency=1, automatic_source_evidence=config)
    assert loaded["config"]["automatic_source_evidence"] == config
    changed = {**config, "target_sha256": "0" * 64}
    with pytest.raises(refresh.RefreshError, match="configuration differs"):
        refresh.load_resume_manifest(run / "manifest.json", git_sha=SHA, sources=("faris-lee",),
                                     page_cap=400, concurrency=1, automatic_source_evidence=changed)
    with pytest.raises(refresh.RefreshError, match="configuration differs"):
        refresh.load_resume_manifest(run / "manifest.json", git_sha=SHA, sources=("faris-lee",),
                                     page_cap=400, concurrency=1)


def test_collector_exceptional_numbers_are_retained_and_hash_distinct(tmp_path):
    config, _, _ = setup_policy(tmp_path, count=1)
    original = artifact(count=1)
    original["listings"][0]["numeric"] = "REPLACE_NUMERIC"
    template = json.dumps(original).replace('"REPLACE_NUMERIC"', 'NUMERIC_LITERAL')
    raw_a = template.replace('NUMERIC_LITERAL', '0.10000000000000000001').encode()
    raw_b = template.replace('NUMERIC_LITERAL', '0.10000000000000000002').encode()
    obj_a, literals_a = acquisition.decode_json(raw_a)
    obj_b, literals_b = acquisition.decode_json(raw_b)
    assert obj_a == obj_b and literals_a != literals_b
    wrapped_a = collector.convert(obj_a, source_key="faris-lee", numeric_literals=literals_a)
    wrapped_b = collector.convert(obj_b, source_key="faris-lee", numeric_literals=literals_b)
    assert wrapped_a["provenance"]["originalArtifactSha256"] != wrapped_b["provenance"]["originalArtifactSha256"]
    assert wrapped_a["provenance"]["originalArtifactHashKind"] == "canonical_json_exact_numeric_literals_utf8"
    run, path, manifest, attempt = state(tmp_path, config, original)
    path.write_bytes(raw_a)
    automatic.seal(run, manifest, "faris-lee", attempt, path)
    plan = json.loads((run / attempt["source_evidence"]["path"]).read_bytes())
    assert plan["events"][0]["facts"][0]["value"]["provenance"]["numericLiterals"] == literals_a
    events.validate_plan(plan)


def test_pipeline_hold_retains_evidence_outside_poll_callback(closed_pipeline, tmp_path, monkeypatch):
    import cre_refresh_pipeline as pipeline
    case = closed_pipeline
    config, _, _ = setup_policy(tmp_path, sources=("cbre", "svn"))
    case.manifest["config"]["automatic_source_evidence"] = config
    publish = refresh._finalize_cohort_collection
    def sealed_publish(run, manifest, item, **kwargs):
        value = json.loads(item.tmp_artifact.read_bytes())
        value["runMeta"]["finishedAt"] = value["runMeta"]["finishedAt"].replace("+00:00", "Z")
        item.tmp_artifact.write_text(json.dumps(value))
        refresh.seal_source_evidence(run, manifest, item.source, item.attempt, item.tmp_artifact)
        return publish(run, manifest, item, **kwargs)
    monkeypatch.setattr(refresh, "_finalize_cohort_collection", sealed_publish)
    calls = []
    def append(*_args, **_kwargs):
        assert not pipeline._IN_CALLBACK.get()
        calls.append(1)
    monkeypatch.setattr(automatic.cli, "run_psql", append)
    assert case.run() == ["cbre"]
    assert case.manifest["sources"]["cbre"]["state"] == "gate_blocked"
    assert case.manifest["sources"]["cbre"]["attempts"][0]["source_evidence"]["state"] == "appended"
    assert ("write", "cbre") not in case.events
    assert len(calls) == 2
