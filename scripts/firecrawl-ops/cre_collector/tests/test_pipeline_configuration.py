"""Portable opt-in settings, one owner forwarding and exact member results."""

from pathlib import Path

import pytest

import cre_checkpoint_series as series


def config(**overrides):
    values = {
        "sources": ["cbre", "jll"],
        "page_cap": 400,
        "concurrency": 1,
        "attempts_per_source": 3,
        "max_resume_age_hours": 24,
        "max_host_cpu_percent": 90,
        "cpu_sustain_seconds": 30,
        "cpu_sample_seconds": 2,
        "nice": 15,
    }
    values.update(overrides)
    return series.series_config(**values)


@pytest.mark.parametrize("workers", [1, 2, 3, 4])
def test_explicit_workers_reach_single_portable_owner(workers, tmp_path):
    settings = config(pipeline=True, source_workers=workers)
    argv = series.build_checkpoint_argv(
        "cbre,jll", child_out_root=tmp_path, env_file=None, config=settings
    )
    assert argv[argv.index("--source-workers") + 1] == str(workers)
    assert argv[argv.index("--sources") + 1] == "cbre,jll"
    assert argv.count("--pipeline") == 1
    assert settings["source_workers"] == workers
    assert series._expected_child_config("cbre,jll", settings)["sources"] == [
        "cbre",
        "jll",
    ]


def test_defaults_preserve_serial_on_every_host():
    assert config()["source_workers"] == 1
    assert "pipeline" not in config()
    assert config(pipeline=True)["source_workers"] == 2


@pytest.mark.parametrize("workers", [0, 5, True, 1.5])
def test_invalid_workers_refused_before_dispatch(workers):
    with pytest.raises(ValueError):
        config(pipeline=True, source_workers=workers)


def test_distinct_admissions_forwarded_as_explicit_repeatable_keys(tmp_path):
    settings = config(
        pipeline=True,
        sources=["interra-realty", "faris-lee"],
        admit_current_inventory_source=["interra-realty", "faris-lee"],
    )
    argv = series.build_checkpoint_argv(
        "interra-realty,faris-lee",
        child_out_root=tmp_path,
        env_file=None,
        config=settings,
    )
    assert argv.count("--admit-current-inventory-source") == 2
    assert set(settings["current_inventory_admissions"]) == {
        "interra-realty",
        "faris-lee",
    }


def qualified_child():
    result = {
        "state": "verified",
        "unit_id": "unit-0001",
        "readback_ok": True,
        "generation_id": "generation",
        "artifact_sha256": "a" * 64,
        "validation_path": "pipeline/unit-0001/validation.json",
        "validation_sha256": "b" * 64,
    }
    unit = {
        "unit_id": "unit-0001",
        "state": "verified",
        "sources": ["cbre"],
        "validation_path": result["validation_path"],
        "validation_sha256": "b" * 64,
    }
    return {
        "run_id": "generation",
        "status": "supported_scope_complete",
        "sources": {
            "cbre": {"artifact": {"sha256": "a" * 64}, "readback": {"ok": True}}
        },
        "pipeline": {"source_results": {"cbre": result}, "units": [unit]},
    }


@pytest.mark.parametrize(
    "drift",
    [
        None,
        "generation_id",
        "artifact_sha256",
        "validation_path",
        "readback_ok",
        "unit",
    ],
)
def test_exact_per_source_readback_required(drift):
    child = qualified_child()
    if drift == "unit":
        child["pipeline"]["units"][0]["sources"] = ["jll"]
    elif drift:
        child["pipeline"]["source_results"]["cbre"][drift] = "foreign"
    assert series.pipeline_source_qualified(child, "cbre") is (drift is None)
    assert not series.pipeline_source_qualified(child, "jll")


def test_single_pipeline_owner_even_with_partial_success(monkeypatch, tmp_path):
    settings = config(pipeline=True)
    root = tmp_path / "2026-10-15T120000Z"
    root.mkdir()
    manifest = series.new_manifest(
        root,
        git_sha="a" * 40,
        config=settings,
        database_target={"algorithm": "sha256", "value": "d" * 64},
    )
    calls = []
    monkeypatch.setattr(series, "save_manifest", lambda *a: None)
    monkeypatch.setattr(series, "allocate_run_id", lambda: "2026-10-15T120001Z")
    monkeypatch.setattr(series, "_terminate_child", lambda p: None)

    class Proc:
        pid = 12345

        def wait(self):
            return 0

    class Control:
        invocation_id = "parent"
        child_invocation_id = "child"

        def prepare_checkpoint(self, *a):
            pass

        def spawn(self, popen, argv, **kwargs):
            calls.append(argv)
            return Proc()

        def check(self):
            pass

        def observe_exit(self, proc):
            pass

    child = qualified_child()
    child["config"] = series._expected_child_config("cbre,jll", settings)
    child["preflight"] = {"database_target": manifest["database_target"]}
    monkeypatch.setattr(
        series, "_load_child_manifest", lambda *a, **k: (Path("manifest.json"), child)
    )
    assert (
        series.run_series(
            root, manifest, env_file=None, retry_failed=False, control=Control()
        )
        != 0
    )
    assert len(calls) == 1
    assert calls[0][calls[0].index("--sources") + 1] == "cbre,jll"
    assert manifest["sources"]["cbre"]["state"] == "complete"
    assert manifest["sources"]["jll"]["state"] == "failed_global"
    assert manifest["status"] == "failed"


def test_status_reads_shared_pipeline_owner_once(tmp_path):
    import json

    import cre_series_status as status

    settings = config(pipeline=True)
    directory = tmp_path / "series"
    run = directory / "runs/2026-10-15T120001Z"
    run.mkdir(parents=True)
    manifest = series.new_manifest(directory, git_sha="a" * 40, config=settings)
    for row in manifest["sources"].values():
        row["checkpoint_run"] = "runs/" + run.name
    child = qualified_child()
    child.update(
        run_id=run.name,
        collector_git_sha="a" * 40,
        config=series._expected_child_config("cbre,jll", settings),
    )
    child["pipeline"]["source_results"]["cbre"]["generation_id"] = run.name
    child["sources"]["jll"] = {"state": "collecting"}
    (run / "manifest.json").write_text(json.dumps(child))
    counts = status.pipeline_progress(manifest, directory / "manifest.json")
    assert counts["source_workers"] == 2
    assert counts["fetching"] == 1
    assert counts["verified"] == 1
    manifest["sources"]["jll"]["checkpoint_run"] = "runs/foreign"
    assert status.pipeline_progress(manifest, directory / "manifest.json") is None


def test_pipeline_resume_rejects_worker_or_source_selection_drift(tmp_path):
    import json

    settings = config(pipeline=True)
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=settings)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    for changed in (
        config(pipeline=True, source_workers=3),
        config(pipeline=True, sources=["jll", "cbre"]),
    ):
        with pytest.raises(series.SeriesError, match="configuration differs"):
            series.load_resume_manifest(path, git_sha="a" * 40, config=changed)


def test_pipeline_stale_reconciliation_uses_same_terminal_owner(monkeypatch, tmp_path):
    settings = config(pipeline=True)
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=settings)
    child = qualified_child()
    child.update(
        started_at="2026-10-15T12:00:00Z",
        finished_at="2026-10-15T12:02:00Z",
        preflight={"database_target": None},
    )
    for row in manifest["sources"].values():
        row.update(
            state="running",
            checkpoint_run="runs/2026-10-15T120001Z",
            attempts=[{"started_at": "2026-10-15T12:00:01Z"}],
        )
    calls = []

    def load(*args, **kwargs):
        calls.append(kwargs)
        return tmp_path / "manifest.json", child

    monkeypatch.setattr(series, "_load_child_manifest", load)
    monkeypatch.setattr(series, "save_manifest", lambda *a: None)
    series.reconcile_stale_running_sources(tmp_path, manifest)
    assert len(calls) == 1
    assert calls[0]["expected_config"]["sources"] == ["cbre", "jll"]
    assert manifest["sources"]["cbre"]["state"] == "complete"
    assert manifest["sources"]["jll"]["state"] == "interrupted"
