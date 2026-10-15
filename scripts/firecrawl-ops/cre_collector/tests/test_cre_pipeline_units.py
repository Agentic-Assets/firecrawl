"""Sealed-unit scope and exact-outcome recovery without write replay."""

import json

import pytest

import cre_checkpoint_refresh as refresh
import cre_refresh_pipeline as pipeline
from cre_validate import FULL_VALIDATION_PROFILE, QUERIES, VALIDATION_AUDIT_SCOPES


@pytest.fixture(autouse=True)
def isolated_resource_trip_state():
    """Existing fault suites may leave a synthetic sticky watchdog trip."""
    refresh._clear_cpu_guard_trip()
    try:
        yield
    finally:
        refresh._clear_cpu_guard_trip()


def owner(tmp_path):
    root = tmp_path / "2026-10-15T120000Z"
    root.mkdir()
    manifest = refresh.new_manifest(
        root,
        git_sha="a" * 40,
        git_dirty=False,
        sources=("transwestern", "savills"),
        page_cap=400,
        concurrency=1,
        database_target={"algorithm": "sha256", "value": "d" * 64},
    )
    manifest["pipeline"] = {"units": [], "commit_sequence": 1}
    return root, manifest


def full_report():
    return {
        "ok": True,
        "validation_profile": FULL_VALIDATION_PROFILE,
        "audit_scope": VALIDATION_AUDIT_SCOPES[FULL_VALIDATION_PROFILE],
        "queries": {name: [] for name in QUERIES},
        "historical_primary_child_conflicts": [],
    }


def test_pipeline_chains_only_hash_target_generation_sequence_bound_full_post(tmp_path):
    root, manifest = owner(tmp_path)
    before = root / "prior.json"
    before.write_text(json.dumps(full_report()))
    previous = {
        "path": "prior.json",
        "sha256": refresh.sha256_file(before),
        "target": "d" * 64,
        "sequence": 1,
        "run_id": manifest["run_id"],
    }
    unit = {"unit_id": "unit-0002"}
    assert pipeline.unit_before_report(root, manifest, unit, None, previous) == before
    assert unit["before_reused"] is True
    before.write_text("changed")
    with pytest.raises(refresh.GlobalStageError, match="changed"):
        pipeline.unit_before_report(root, manifest, unit, None, previous)


@pytest.mark.parametrize(
    "field,value", [("sequence", 0), ("target", "e" * 64), ("run_id", "foreign")]
)
def test_pipeline_prior_audit_foreign_binding_refused(tmp_path, field, value):
    root, manifest = owner(tmp_path)
    report = root / "prior.json"
    report.write_text(json.dumps(full_report()))
    previous = {
        "path": "prior.json",
        "sha256": refresh.sha256_file(report),
        "target": "d" * 64,
        "sequence": 1,
        "run_id": manifest["run_id"],
    }
    previous[field] = value
    with pytest.raises(refresh.GlobalStageError):
        pipeline.unit_before_report(
            root, manifest, {"unit_id": "unit-0002"}, None, previous
        )


@pytest.mark.parametrize("outcome", ["committed", "rollback", "unknown"])
def test_pipeline_reconciles_existing_probe_under_owner_without_replaying(
    tmp_path, monkeypatch, outcome
):
    root, manifest = owner(tmp_path)
    checkpoint = manifest["sources"]["transwestern"]
    checkpoint["state"] = "ingest_recovery_required"
    calls = []

    def probe(*args):
        calls.append("read_only_probe")
        if outcome == "committed":
            checkpoint["state"] = "ingested"
        elif outcome == "rollback":
            checkpoint["state"] = "dry_run_passed"
            checkpoint["ingest_recovery"] = {
                "outcome": "exact_rollback",
                "replay_safe": True,
            }
            raise refresh.GlobalStageError("exact rollback stops run")
        else:
            raise refresh.GlobalStageError("not exact")

    monkeypatch.setattr(refresh, "recover_interrupted_ingest", probe)
    monkeypatch.setattr(
        refresh, "ingest_source", lambda *a: pytest.fail("replayed write")
    )
    known = pipeline.reconcile_pipeline_ingests(
        root, manifest, None, probe_allowed=True
    )
    assert calls == ["read_only_probe"]
    assert known is (outcome != "unknown")
    assert manifest["pipeline"]["commit_outcome_unknown"] is (outcome == "unknown")


def test_pipeline_interrupted_or_unsettled_owner_never_probes_or_replays(
    tmp_path, monkeypatch
):
    root, manifest = owner(tmp_path)
    manifest["sources"]["transwestern"]["state"] = "ingesting"
    monkeypatch.setattr(
        refresh,
        "recover_interrupted_ingest",
        lambda *a: pytest.fail("probe after uncertain settlement"),
    )
    assert (
        pipeline.reconcile_pipeline_ingests(root, manifest, None, probe_allowed=False)
        is False
    )
    assert manifest["pipeline"]["commit_outcome_unknown"] is True


def test_pipeline_cleanup_unknown_during_probe_is_not_swallowed(tmp_path, monkeypatch):
    root, manifest = owner(tmp_path)
    manifest["sources"]["transwestern"]["state"] = "ingesting"

    def unknown(*args):
        raise refresh.ProcessCleanupUnknown("probe group uncertain")

    monkeypatch.setattr(refresh, "recover_interrupted_ingest", unknown)
    with pytest.raises(refresh.ProcessCleanupUnknown):
        pipeline.reconcile_pipeline_ingests(root, manifest, None, probe_allowed=True)
    assert manifest["pipeline"]["commit_outcome_unknown"] is True


def test_pipeline_scoped_gate_uses_only_sealed_members_and_unique_evidence(
    tmp_path, monkeypatch
):
    root, manifest = owner(tmp_path)
    for source, checkpoint in manifest["sources"].items():
        checkpoint["artifact"] = {"path": f"sources/{source}.json"}
    commands = []

    def command(argv, log, **kwargs):
        commands.append((argv, log))
        output = __import__("pathlib").Path(argv[argv.index("--out") + 1])
        output.write_text(
            json.dumps({"per_source": {"savills": {"verdict": "ok"}}, "summary": {}})
        )
        return 0

    monkeypatch.setattr(refresh, "run_command", command)
    info = refresh.run_aggregate_gate(
        root,
        manifest,
        None,
        sources=("savills",),
        evidence_dir=root / "pipeline" / "unit-0001",
    )
    assert info["path"] == "pipeline/unit-0001/aggregate-gate.json"
    assert commands[0][1] == root / "logs" / "unit-0001-aggregate-gate.log"
    assert str(root / "sources" / "savills.json") in commands[0][0]
    assert str(root / "sources" / "transwestern.json") not in commands[0][0]
    with pytest.raises(refresh.GlobalStageError):
        refresh.run_aggregate_gate(root, manifest, None, sources=("foreign",))


@pytest.mark.parametrize(
    "bad",
    [
        {"ok": True},
        {"ok": True, "queries": {}},
        {
            "ok": True,
            "validation_profile": "full",
            "audit_scope": "full_standalone_post_ingest_or_recovery",
            "queries": {},
        },
    ],
)
def test_pipeline_rejects_legacy_or_incomplete_full_evidence(bad):
    with pytest.raises(refresh.GlobalStageError):
        pipeline.require_pipeline_full_report(bad)


def test_pipeline_manifest_api_defaults_match_portable_cli_defaults(tmp_path):
    root = tmp_path / "2026-10-15T120000Z"
    root.mkdir()
    config = refresh.new_manifest(
        root,
        git_sha="a" * 40,
        git_dirty=False,
        sources=("transwestern",),
        page_cap=400,
        concurrency=1,
        pipeline=True,
    )["config"]
    assert config["pipeline"] is True
    assert config["source_workers"] == 2
    assert config["current_inventory_admissions"] == {}


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_pipeline_configuration_and_default_workers_share_one_platform_path(
    tmp_path, monkeypatch, platform
):
    monkeypatch.setattr(refresh.sys, "platform", platform)
    manifest = refresh.new_manifest(
        tmp_path / "generation",
        git_sha="a" * 40,
        git_dirty=False,
        sources=("transwestern",),
        page_cap=400,
        concurrency=1,
        pipeline=True,
    )
    assert manifest["config"]["pipeline"] is True
    assert manifest["config"]["source_workers"] == 2
    assert pipeline.resolve_source_workers(True, 4) == 4
    assert pipeline.resolve_source_workers(False, None) == 1


@pytest.mark.parametrize(
    "options",
    [
        ("--pipeline", "--source-workers", "0"),
        ("--pipeline", "--source-workers", "5"),
        (
            "--admit-current-inventory-source",
            "svn",
            "--admit-current-inventory-source",
            "svn",
        ),
    ],
)
def test_pipeline_invalid_options_refused_before_target_or_provider_dispatch(
    monkeypatch, options
):
    monkeypatch.setattr(
        refresh,
        "database_target_fingerprint",
        lambda *a: pytest.fail("dispatched invalid options"),
    )
    with pytest.raises(SystemExit) as caught:
        refresh.main(list(options))
    assert caught.value.code == 2


def test_pipeline_gate_consumes_replaced_worker_report_without_stale_verdict(
    tmp_path, monkeypatch
):
    root, manifest = owner(tmp_path)
    source = "transwestern"
    artifact = root / "sources" / f"{source}.json"
    monkeypatch.setattr(
        refresh.current_inventory,
        "admission_configuration",
        lambda *a: {"source_key": source},
    )

    def command(argv, log, **kwargs):
        output = __import__("pathlib").Path(argv[argv.index("--out") + 1])
        output.write_text(
            json.dumps(
                {"per_source": {source: {"verdict": "first_seen"}}, "summary": {}}
            )
        )
        return 2

    def admitted(run_dir, parent, report, path, phase, **kwargs):
        report.clear()
        report.update(
            {
                "per_source": {
                    source: {
                        "verdict": refresh.current_inventory.VERDICT,
                        "raw_verdict": "first_seen",
                        "mark_missing_safe": False,
                        "current_inventory_proof": {"source": source},
                    }
                },
                "summary": {},
            }
        )

    monkeypatch.setattr(refresh, "run_command", command)
    monkeypatch.setattr(refresh, "_admit_current_inventory", admitted)
    refresh.gate_source(root, manifest, source, artifact, None)
    gate = manifest["sources"][source]["gate"]
    assert gate["verdict"] == refresh.current_inventory.VERDICT
    assert gate["raw_verdict"] == "first_seen"
    assert gate["current_inventory_proof"] == {"source": source}


def test_pure_admission_compute_returns_scope_changes_for_parent_publication(
    tmp_path, monkeypatch
):
    from cre_pipeline_artifact_worker import compute

    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}")
    manifest = {
        "scope": {"source_keys": ["transwestern"], "kind": "collector_registry"}
    }

    def admit(run_dir, owner, report, path, phase, **kwargs):
        owner["scope"].update(
            kind="collector_registry_additive_current_inventory",
            whole_source_coverage=False,
        )
        report["admitted"] = True

    monkeypatch.setattr(refresh, "_admit_current_inventory", admit)
    result = compute(
        "admit",
        {
            "path": str(artifact),
            "run_dir": str(tmp_path),
            "manifest": manifest,
            "report": {},
            "phase": "source",
            "source": "transwestern",
        },
        tmp_path / "response.json",
    )
    assert result["scope"]["whole_source_coverage"] is False
    assert result["scope"]["source_keys"] == ["transwestern"]
    assert (
        json.loads((tmp_path / "response.admission.json").read_text())["admitted"]
        is True
    )
