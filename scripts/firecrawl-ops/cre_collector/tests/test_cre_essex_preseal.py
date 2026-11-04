"""Essex identity precheck uses exact staging and fails closed."""

import copy
import hashlib
import json
import subprocess

import pytest


def artifact():
    url = "https://essexrealtygroup.com/properties/example/"
    return {"runMeta": {"finishedAt": "2026-11-03T08:15:20Z", "freshness": {"generationId": "generation"}}, "brokers": [],
            "listings": [{"sourceKey": "essex-realty", "id": "properties/example", "url": url,
                          "essexWordpressPage": {"type": "wordpress_properties_post", "id": "123",
                                                 "canonicalUrl": url}}]}


def test_essex_projection_uses_maintained_staging_and_preserves_claim():
    from cre_essex_identity import stage_essex_identity

    rows = stage_essex_identity(artifact())
    assert len(rows) == 1
    assert rows[0]["external_id"] == "properties/example"
    assert rows[0]["source_url"] == artifact()["listings"][0]["url"]
    assert rows[0]["raw_data"]["essexWordpressPage"]["id"] == "123"


@pytest.mark.parametrize("change", ["duplicate", "foreign", "missing"])
def test_essex_projection_refuses_incomplete_or_mixed_artifacts(change):
    from cre_essex_identity import stage_essex_identity

    value = artifact()
    if change == "duplicate":
        value["listings"].append(copy.deepcopy(value["listings"][0]))
    elif change == "foreign":
        value["listings"][0]["sourceKey"] = "other"
    else:
        value["listings"][0].pop("url")
    with pytest.raises(ValueError):
        stage_essex_identity(value)


def test_probe_parser_requires_one_complete_typed_result():
    from cre_essex_identity import classify_probe_result

    good = {"brokerages": "1", "staged": "1", "joined": "1", "claims": "1", "invalid": "0", "conflicts": "2"}
    assert classify_probe_result([good], 1) == 2
    for value in ([], [dict(good, brokerages="0")], [dict(good, staged="0")],
                  [dict(good, invalid="1")], [dict(good, conflicts="unknown")]):
        with pytest.raises(ValueError):
            classify_probe_result(value, 1)


def test_readonly_psql_runner_kills_stalled_process_at_wall_timeout(tmp_path, monkeypatch):
    import cre_validate

    executable = tmp_path / "stalled-psql"
    executable.write_text("#!/bin/sh\nexec sleep 10\n")
    executable.chmod(0o700)
    monkeypatch.setattr(cre_validate, "psql_connection_args", lambda _url: [])
    monkeypatch.setattr(cre_validate, "psql_connection_env", lambda _url: {})
    with pytest.raises(subprocess.TimeoutExpired):
        cre_validate._run_psql_script(str(executable), "unused", "SELECT 1;", timeout=0.1)


@pytest.mark.parametrize("conflicts,expected", [(0, True), (2, False), (3, None)])
def test_preseal_accepts_only_bound_clear_or_holds_proven_conflict(tmp_path, monkeypatch, conflicts, expected):
    import cre_checkpoint_refresh as refresh
    import cre_refresh_pipeline as pipeline
    from cre_essex_identity import projection_digest, stage_essex_identity

    raw = json.dumps(artifact()).encode()
    source = tmp_path / "essex.json"
    source.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    coverage = tmp_path / "coverage.json"
    coverage.write_text("reviewed coverage")
    coverage_digest = hashlib.sha256(coverage.read_bytes()).hexdigest()
    manifest = {"run_id": "generation", "collector_git_sha": "a" * 40, "collector_git_dirty": False,
                "preflight": {"database_target": {"algorithm": "sha256", "value": "b" * 64}},
                "sources": {"essex-realty": {"artifact": {"path": "essex.json", "sha256": digest, "bytes": len(raw)}, "gate": {"verdict": "ok"}}}}
    monkeypatch.setattr(refresh, "git_identity", lambda: ("a" * 40, False))
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _env: {"value": "b" * 64})
    monkeypatch.setattr(refresh, "save_manifest", lambda *_: None)
    monkeypatch.setattr(pipeline, "require_pipeline_owner", lambda *_: None)
    def command(argv, log, **_kwargs):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("closed synthetic probe\n")
        report = {"schema": "cre-essex-preseal-identity-v1", "source": "essex-realty",
                  "artifact_sha256": digest, "generation_id": "generation", "producer_sha": "a" * 40,
                  "database_target_sha256": "b" * 64, "staged_count": 1,
                  "staged_sha256": projection_digest(stage_essex_identity(artifact())),
                  "conflicts": conflicts, "verdict": "hold" if conflicts else "clear"}
        (tmp_path / "gates" / "essex-realty-identity-preseal.json").write_text(json.dumps(report))
        if expected is None:
            coverage.write_text("changed coverage")
        return 0
    monkeypatch.setattr(refresh, "run_command", command)
    if expected is None:
        with pytest.raises(refresh.GlobalStageError):
            pipeline.preseal_essex_identity(tmp_path, manifest, source, None, coverage, coverage_digest, {"verdict": "ok"})
        assert manifest["sources"]["essex-realty"].get("state") != "gate_blocked"
    else:
        assert pipeline.preseal_essex_identity(tmp_path, manifest, source, None, coverage, coverage_digest, {"verdict": "ok"}) is expected
    if expected is False:
        assert manifest["sources"]["essex-realty"]["state"] == "gate_blocked"
        assert manifest["sources"]["essex-realty"]["identity_preseal"]["verdict"] == "hold"
        assert manifest["sources"]["essex-realty"]["identity_preseal"]["conflicts"] == conflicts


def test_preseal_query_failure_is_global_not_a_hold(tmp_path, monkeypatch):
    import cre_checkpoint_refresh as refresh
    import cre_refresh_pipeline as pipeline

    source = tmp_path / "essex.json"
    source.write_text(json.dumps(artifact()))
    manifest = {"run_id": "generation", "collector_git_sha": "a" * 40, "collector_git_dirty": False,
                "preflight": {"database_target": {"algorithm": "sha256", "value": "b" * 64}},
                "sources": {"essex-realty": {"artifact": {"path": "essex.json", "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                                                             "bytes": len(source.read_bytes())}}}}
    monkeypatch.setattr(refresh, "git_identity", lambda: ("a" * 40, False))
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _env: {"value": "b" * 64})
    monkeypatch.setattr(pipeline, "require_pipeline_owner", lambda *_: None)
    monkeypatch.setattr(refresh, "run_command", lambda *_args, **_kwargs: 1)
    with pytest.raises(refresh.GlobalStageError):
        pipeline.preseal_essex_identity(tmp_path, manifest, source, None, tmp_path / "coverage", "unused", {})
    assert manifest["sources"]["essex-realty"].get("state") != "gate_blocked"


def test_admitted_essex_gate_routes_to_identity_before_sealing(tmp_path, monkeypatch):
    import cre_checkpoint_refresh as refresh
    import cre_refresh_pipeline as pipeline

    source = tmp_path / "essex.json"
    source.write_text("artifact")
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({
        "per_source": {"essex-realty": {"verdict": "ok"}},
        "summary": {"mode": "live", "torow_errors": 0, "baseline_updated": False,
                    "baseline_update_sources": []},
    }))
    gate = {"path": "coverage.json", "verdict": "ok", "rc": 0}
    manifest = {"run_id": "generation", "collector_git_sha": "a" * 40,
                "collector_git_dirty": False,
                "preflight": {"database_target": {"algorithm": "sha256", "value": "b" * 64}},
                "sources": {"essex-realty": {"artifact": {"path": "essex.json",
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}, "gate": gate}}}
    monkeypatch.setattr(refresh, "git_identity", lambda: ("a" * 40, False))
    monkeypatch.setattr(pipeline, "require_pipeline_owner", lambda *_: None)
    monkeypatch.setattr(refresh, "gate_source", lambda *_: None)
    monkeypatch.setattr(refresh.current_inventory, "validate_report_scope", lambda *_: None)
    monkeypatch.setattr(refresh, "gate_verdict_is_admitted", lambda *_args, **_kwargs: True)
    seen = []

    def identity(*args):
        seen.append(args)
        return False

    monkeypatch.setattr(pipeline, "preseal_essex_identity", identity)
    assert pipeline.preseal_source_gate(tmp_path, manifest, "essex-realty", None) is False
    assert seen == [(tmp_path, manifest, source, None, report,
                     hashlib.sha256(report.read_bytes()).hexdigest(), gate)]


def test_essex_guard_is_recomputed_after_lifecycle_locks():
    import cre_ingest

    sql = cre_ingest.build_sql([], [], "2026-01-01T00:00:00Z", [])
    source_lock = sql.index("FOR UPDATE OF si;")
    listing_lock = sql.index("FOR UPDATE OF t;", source_lock)
    essex = sql.index("CREATE TEMP TABLE _essex_identity_claims", listing_lock)
    historical = sql.index("CREATE TEMP TABLE _essex_historical_identity_claims", essex)
    assert source_lock < listing_lock < essex < historical < sql.index("ON CONFLICT (brokerage_id, external_id) WHERE external_id IS NOT NULL", historical)


def test_essex_probe_binds_identity_evidence_to_staging():
    from cre_essex_identity import projection_digest, stage_essex_identity

    value = artifact()
    row = value["listings"][0]
    row.update(name="Example", street="1 Main St", city="Chicago", state="IL",
               transactionType="sale", description="A building")
    staged = stage_essex_identity(value)
    assert staged[0]["title"] == "Example"
    assert staged[0]["address"] == "1 Main St"
    before = projection_digest(staged)
    row["description"] = "Different building"
    assert projection_digest(stage_essex_identity(value)) != before
