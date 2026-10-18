"""DAUM-only explicit subset configuration and no ambient scope leakage."""

from pathlib import Path
import pytest
import cre_current_inventory_admission as admission
import cre_checkpoint_refresh as refresh
from test_daum_verified_subset import complete, subset  # noqa: F401

SOURCE = "daum-commercial"
SCOPE = "daum_verified_subset_v1"


def test_explicit_subset_configuration_and_map():
    entry = admission.subset_configuration()
    assert entry == {**admission.configuration(SOURCE), "admission_scope": SCOPE}
    config = {
        "pipeline": True,
        "sources": [SOURCE, "transwestern"],
        "transactions": ["sale", "lease"],
        "current_inventory_admissions": admission.configuration_map(
            [], [SOURCE, "transwestern"], daum_verified_subset=True
        ),
    }
    assert admission.admission_configuration(config, SOURCE) == entry
    assert admission.admission_configuration(config, "transwestern") is None
    config["current_inventory_admissions"][SOURCE]["admission_scope"] = "other"
    with pytest.raises(ValueError):
        admission.admission_configuration(config, SOURCE)


@pytest.mark.parametrize(
    "selected,admitted,kwargs",
    [
        (["transwestern"], [], {}),
        ([SOURCE], [SOURCE], {}),
        ([SOURCE], [], {"transactions": ("sale",)}),
        ([SOURCE], [], {"additive_hold": True}),
        ([SOURCE], [], {"allow_dirty": True}),
    ],
)
def test_subset_wrong_source_or_complete_conflict_refuses(selected, admitted, kwargs):
    transactions = kwargs.pop("transactions", ("sale", "lease"))
    with pytest.raises(ValueError):
        admission.validate_options(
            admitted,
            selected,
            transactions,
            pipeline=True,
            daum_verified_subset=True,
            **kwargs,
        )


def test_serial_subset_entry_and_complete_isolation():
    entry = admission.subset_configuration()
    config = {
        "sources": [SOURCE],
        "transactions": ["sale", "lease"],
        "current_inventory_admission": entry,
    }
    assert admission.admission_configuration(config, SOURCE) == entry
    config["sources"].append("transwestern")
    with pytest.raises(ValueError):
        admission.admission_configuration(config, SOURCE)


def test_ambient_subset_env_is_always_cleared_without_explicit_config(tmp_path):
    for source in [SOURCE, "transwestern"]:
        env, overrides = refresh.fresh_source_env(
            source, tmp_path, base={"DAUM_VERIFIED_SUBSET": "1"}
        )
        assert "DAUM_VERIFIED_SUBSET" not in env
        assert overrides["DAUM_VERIFIED_SUBSET"] == "<unset>"
    env, _ = refresh.fresh_source_env(SOURCE, tmp_path, daum_verified_subset=True)
    assert env["DAUM_VERIFIED_SUBSET"] == "1"
    with pytest.raises(ValueError):
        refresh.fresh_source_env("transwestern", tmp_path, daum_verified_subset=True)


def test_subset_manifest_scope_and_guarded_worker(tmp_path):
    manifest = refresh.new_manifest(
        tmp_path,
        git_sha="a" * 40,
        git_dirty=False,
        sources=[SOURCE, "transwestern"],
        page_cap=400,
        concurrency=1,
        pipeline=True,
        admit_daum_verified_subset=True,
    )
    assert manifest["scope"]["kind"] == "collector_registry_verified_subset"
    assert manifest["scope"]["whole_source_coverage"] is False
    assert admission.is_verified_subset(manifest["config"], SOURCE)
    assert not admission.is_verified_subset(manifest["config"], "transwestern")
    assert refresh.gate_verdict_is_admitted(
        manifest, admission.DAUM_SUBSET_VERDICT, source=SOURCE
    )
    assert not refresh.gate_verdict_is_admitted(
        manifest, admission.DAUM_SUBSET_VERDICT, source="transwestern"
    )
    argv = refresh.build_cohort_collect_worker_argv(
        SOURCE,
        tmp_path / "artifact.json",
        run_dir=tmp_path,
        attempt_log=tmp_path / "log",
        attempt_number=1,
        generation_started_at="2026-10-16T00:00:00Z",
        page_cap=400,
        concurrency=1,
        transactions=("sale", "lease"),
        daum_verified_subset=True,
    )
    assert argv.count("--admit-daum-verified-subset") == 1


def test_subset_carrier_requires_both_gates_and_exact_attempt(tmp_path):
    manifest = refresh.new_manifest(
        tmp_path,
        git_sha="a" * 40,
        git_dirty=False,
        sources=[SOURCE],
        page_cap=400,
        concurrency=1,
        admit_daum_verified_subset=True,
        database_target={"algorithm": "sha256", "value": "b" * 64},
    )
    with pytest.raises(refresh.GlobalStageError, match="both sealed"):
        refresh.subset_ingest_arguments(tmp_path, manifest, SOURCE)
    path = tmp_path / "claims.json"
    path.write_text("{}")
    checkpoint = manifest["sources"][SOURCE]
    checkpoint["subset_ingest_attempt_id"] = "c" * 32
    checkpoint["daum_verified_subset_claims"] = {
        "path": str(path),
        "sha256": refresh.sha256_file(path),
        "attempt_id": "c" * 32,
    }
    argv = refresh.subset_ingest_arguments(tmp_path, manifest, SOURCE)
    assert argv[0:2] == ["--daum-verified-subset-claims", str(path)]
    assert argv[2:4] == ["--checkpoint-ingest-attempt", "c" * 32]
    path.write_text('{"tampered":true}')
    with pytest.raises(refresh.GlobalStageError, match="changed"):
        refresh.subset_ingest_arguments(tmp_path, manifest, SOURCE)


def test_subset_dryrun_defers_without_fabricating_gate(tmp_path, monkeypatch):
    manifest = refresh.new_manifest(
        tmp_path,
        git_sha="a" * 40,
        git_dirty=False,
        sources=[SOURCE],
        page_cap=400,
        concurrency=1,
        admit_daum_verified_subset=True,
    )
    calls = []
    monkeypatch.setattr(refresh, "run_command", lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(refresh, "save_manifest", lambda *a: None)
    assert (
        refresh.dry_run_source(tmp_path, manifest, SOURCE, tmp_path / "artifact.json")
        is True
    )
    assert calls == []
    assert manifest["sources"][SOURCE]["dry_run"] == {
        "deferred_until": "sealed_subset_aggregate"
    }
    assert manifest["aggregate_gate"] is None


def test_actual_worker_source_aggregate_claims_and_no_early_sql(subset, monkeypatch):  # noqa: F811
    import json
    import copy
    import cre_pipeline_artifact_worker as worker
    import test_essex_current_inventory as helpers
    from cre_daum_current_inventory import ingest_subset_context

    run, path, artifact, manifest, raw = subset
    # Fixture derives the emitted projection from the complete fake-transport
    # producer; refresh its emitted freshness counters as the subset producer does.
    for entry in artifact["sources"]:
        metrics = entry["freshness"]
        count = entry["listingsCollected"]
        for key in ("listings", "inventoryObserved", "detailObserved"):
            metrics[key] = count
    helpers.refresh(path, artifact, manifest)
    monkeypatch.setattr(
        refresh, "git_identity", lambda: (manifest["collector_git_sha"], False)
    )
    source_report = copy.deepcopy(raw)
    value = worker.compute(
        "admit",
        {
            "path": str(path),
            "source": SOURCE,
            "run_dir": str(run),
            "manifest": manifest,
            "report": source_report,
            "phase": "source",
        },
        run / "source-response.json",
    )
    assert "subset_claims" not in value
    manifest["sources"][SOURCE]["gate"] = {
        "path": str(Path(value["report_path"]).relative_to(run)),
        **source_report["per_source"][SOURCE],
    }
    manifest["sources"][SOURCE]["subset_ingest_attempt_id"] = "e" * 32
    calls = []
    monkeypatch.setattr(
        refresh, "run_command", lambda argv, *a, **kw: calls.append(argv) or 0
    )
    monkeypatch.setattr(refresh, "save_manifest", lambda *a: None)
    assert refresh.dry_run_source(run, manifest, SOURCE, path)
    assert calls == []
    aggregate = copy.deepcopy(raw)
    value = worker.compute(
        "admit",
        {
            "path": str(path),
            "source": SOURCE,
            "run_dir": str(run),
            "manifest": manifest,
            "report": aggregate,
            "phase": "aggregate",
        },
        run / "aggregate-response.json",
    )
    claims = value["subset_claims"]
    envelope = json.loads(Path(claims["path"]).read_text())
    assert set(envelope) == {
        "manifest",
        "source_report",
        "aggregate_report",
        "binding",
        "attempt_id",
    }
    context = ingest_subset_context(
        path,
        envelope,
        current_code_sha=manifest["collector_git_sha"],
        current_git_dirty=False,
        expected_database_target_sha256="b" * 64,
        attempt_id="e" * 32,
    )
    assert context["binding"]["emitted_count"] == 100
    manifest["sources"][SOURCE]["daum_verified_subset_claims"] = claims
    assert refresh.dry_run_source(run, manifest, SOURCE, path)
    assert len(calls) == 1 and "--daum-verified-subset-claims" in calls[0]
    assert "--checkpoint-ingest-attempt" in calls[0]
    # A refused actual aggregate never produces a claims carrier or SQL call.
    bad = copy.deepcopy(raw)
    bad["per_source"][SOURCE]["verdict"] = "hold"
    with pytest.raises(refresh.GlobalStageError):
        worker.compute(
            "admit",
            {
                "path": str(path),
                "source": SOURCE,
                "run_dir": str(run),
                "manifest": manifest,
                "report": bad,
                "phase": "aggregate",
            },
            run / "bad-response.json",
        )
    assert not (run / "bad-response.subset-claims.json").exists()
    assert len(calls) == 1


def test_pipeline_subset_claims_pass_real_dryrun_reader_under_umask_022(subset):
    """The pipeline's sealed claims carrier is readable by the strict dry-run guard."""
    import copy
    import json
    import os
    import stat
    import cre_pipeline_artifact_worker as worker
    import cre_refresh_pipeline as pipeline
    import test_essex_current_inventory as helpers

    run, path, artifact, manifest, raw = subset
    for entry in artifact["sources"]:
        metrics = entry["freshness"]
        count = entry["listingsCollected"]
        for key in ("listings", "inventoryObserved", "detailObserved"):
            metrics[key] = count
    helpers.refresh(path, artifact, manifest)

    source_report = copy.deepcopy(raw)
    source_result = worker.compute(
        "admit",
        {
            "path": str(path),
            "source": SOURCE,
            "run_dir": str(run),
            "manifest": manifest,
            "report": source_report,
            "phase": "source",
        },
        run / "source-response.json",
    )
    manifest["sources"][SOURCE]["gate"] = {
        "path": str(Path(source_result["report_path"]).relative_to(run)),
        **source_report["per_source"][SOURCE],
    }
    manifest["sources"][SOURCE]["subset_ingest_attempt_id"] = "e" * 32

    previous_umask = os.umask(0o022)
    try:
        with pipeline.artifact_compute_scope(run):
            aggregate = copy.deepcopy(raw)
            refresh._admit_current_inventory(
                run, manifest, aggregate, path, "aggregate", source=SOURCE
            )

        claims = manifest["sources"][SOURCE]["daum_verified_subset_claims"]
        compute_dir = run / "pipeline" / "compute"
        claims_path = Path(claims["path"])
        assert stat.S_IMODE(compute_dir.stat().st_mode) == 0o700
        assert claims_path.parent == compute_dir
        assert stat.S_IMODE(claims_path.stat().st_mode) == 0o600
        assert refresh.dry_run_source(run, manifest, SOURCE, path)
    finally:
        os.umask(previous_umask)

    log = (run / "logs" / f"{SOURCE}-ingest-dry-run.log").read_text()
    assert "dry run: not connecting" in log
    assert "unsafe local evidence" not in log
    assert json.loads(claims_path.read_text())["attempt_id"] == "e" * 32


def test_dirty_subset_manifest_refuses_before_child_scope(tmp_path):
    with pytest.raises(ValueError):
        refresh.new_manifest(
            tmp_path,
            git_sha="a" * 40,
            git_dirty=True,
            sources=[SOURCE],
            page_cap=400,
            concurrency=1,
            admit_daum_verified_subset=True,
        )
