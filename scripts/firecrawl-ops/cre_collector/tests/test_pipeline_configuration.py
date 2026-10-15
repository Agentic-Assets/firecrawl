"""Portable opt-in settings, one owner forwarding and exact member results."""

import hashlib
import json
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


RUN_ID = "2026-10-15T120001Z"
PARENT_START = "2026-10-15T12:00:00Z"
PARENT_END = "2026-10-15T12:02:01Z"
PARENT_ATTEMPTS = [{"started_at": "2026-10-15T12:00:01Z", "finished_at": PARENT_END}]


def qualified_child(run_dir=None):
    from cre_validate import QUERIES, VALIDATION_AUDIT_SCOPES

    observed = "2026-10-15T12:00:15Z"
    report = {
        "ok": True,
        "validation_profile": "full",
        "audit_scope": VALIDATION_AUDIT_SCOPES["full"],
        "generated_at": "2026-10-15T12:00:30Z",
        "queries": {key: [] for key in QUERIES},
        "historical_primary_child_conflicts": [],
    }
    report["queries"]["freshness_generations"] = [
        {
            "source_key": "cbre",
            "generation_id": RUN_ID,
            "active": "1",
            "persisted_inventory_observed": "1",
            "evidence_class": "authoritative_inventory_feed",
            "earliest_inventory_observed_at": observed,
            "latest_inventory_observed_at": observed,
            "earliest_detail_observed_at": "",
            "latest_detail_observed_at": "",
        }
    ]
    report["queries"]["inventory_generation_fingerprints"] = [
        {
            "source_key": "cbre",
            "row_count": "1",
            "max_row_updated_at": observed,
            "max_observation_at": observed,
        }
    ]
    raw = json.dumps(report).encode()
    digest = hashlib.sha256(raw).hexdigest()
    result = {
        "state": "verified",
        "unit_id": "unit-0001",
        "readback_ok": True,
        "generation_id": RUN_ID,
        "artifact_sha256": "a" * 64,
        "validation_path": "pipeline/unit-0001/validation.json",
        "validation_sha256": digest,
    }
    validation = {
        "path": result["validation_path"],
        "rc": 0,
        "query_execution_ok": True,
        "quality_no_regression": True,
        "quality_failures": [],
        "readback_ok": True,
        "failed_readback_sources": [],
        "absolute_quality_ok": False,
        "absolute_quality_failures": ["retained historical diagnostic"],
    }
    unit = {
        "unit_id": "unit-0001",
        "state": "verified",
        "sources": ["cbre"],
        "started_at": "2026-10-15T12:00:10Z",
        "finished_at": "2026-10-15T12:01:00Z",
        "validation_path": result["validation_path"],
        "validation_sha256": digest,
        "validation": validation,
    }
    child = {
        "run_id": RUN_ID,
        "started_at": "2026-10-15T12:00:01Z",
        "finished_at": "2026-10-15T12:02:00Z",
        "status": "supported_scope_complete",
        "sources": {
            "cbre": {
                "artifact": {
                    "sha256": "a" * 64,
                    "bytes": 123,
                    "staged_unique": 1,
                    "inventory_only": 0,
                },
                "readback": {
                    "ok": True,
                    "generation_id": RUN_ID,
                    "expected_staged_unique": 1,
                    "latest_inventory_batch_active": 1,
                    "evidence_class": "authoritative_inventory_feed",
                    "earliest_inventory_observed_at": observed,
                    "latest_inventory_observed_at": observed,
                    "earliest_detail_observed_at": "",
                    "latest_detail_observed_at": "",
                    "inventory_fingerprint": {
                        "sourceId": "cbre",
                        "rowCount": 1,
                        "publicationStatus": "complete",
                        "maxRowUpdatedAt": observed,
                        "maxObservationAt": observed,
                    },
                },
            }
        },
        "pipeline": {"source_results": {"cbre": result}, "units": [unit]},
    }
    if run_dir is not None:
        output = run_dir / result["validation_path"]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(raw)
    return child


def qualification_context(run):
    return {
        "run_dir": run,
        "parent_started_at": PARENT_START,
        "parent_finished_at": PARENT_END,
        "parent_attempts": PARENT_ATTEMPTS,
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
def test_exact_per_source_readback_required(drift, tmp_path):
    run = tmp_path / RUN_ID
    child = qualified_child(run)
    if drift == "unit":
        child["pipeline"]["units"][0]["sources"] = ["jll"]
    elif drift:
        child["pipeline"]["source_results"]["cbre"][drift] = "foreign"
    assert series.pipeline_source_qualified(
        child, "cbre", **qualification_context(run)
    ) is (drift is None)
    assert not series.pipeline_source_qualified(
        child, "jll", **qualification_context(run)
    )


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
            for row in manifest["sources"].values():
                row["attempts"][-1]["started_at"] = PARENT_ATTEMPTS[0]["started_at"]
            calls.append(argv)
            return Proc()

        def check(self):
            pass

        def observe_exit(self, proc):
            pass

    manifest["started_at"] = PARENT_START
    child = qualified_child(root / "runs" / RUN_ID)
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
    # The strict notification reader consumes the actual producer attempt locator.
    for row in manifest["sources"].values():
        assert row["attempts"][-1]["checkpoint_run"] == row["checkpoint_run"]
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
    manifest["started_at"] = PARENT_START
    for row in manifest["sources"].values():
        row["checkpoint_run"] = "runs/" + run.name
        row["attempts"] = PARENT_ATTEMPTS
    child = qualified_child(run)
    child.update(
        run_id=run.name,
        collector_git_sha="a" * 40,
        config=series._expected_child_config("cbre,jll", settings),
        preflight={"database_target": manifest.get("database_target")},
    )
    child["pipeline"]["source_results"]["cbre"]["generation_id"] = run.name
    child["sources"]["jll"] = {"state": "collecting"}
    (run / "manifest.json").write_text(json.dumps(child))
    counts = status.pipeline_progress(manifest, directory / "manifest.json")
    assert counts["source_workers"] == 2
    assert counts["fetching"] == 1
    assert counts["verified"] == 1
    child.update(status="running", finished_at=None)
    (run / "manifest.json").write_text(json.dumps(child))
    assert status.pipeline_progress(manifest, directory / "manifest.json")["verified"] == 1
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
    manifest["started_at"] = PARENT_START
    child = qualified_child(tmp_path / "runs" / RUN_ID)
    child.update(
        started_at="2026-10-15T12:00:01Z",
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


@pytest.mark.parametrize(
    "fault",
    [
        "missing_report",
        "null_artifact_sha",
        "traversal",
        "null_generation",
        "arbitrary_unit",
        "missing_time",
        "reversed_time",
        "report_tamper",
        "missing_profile",
        "missing_family",
        "missing_historical",
        "zero_bytes",
        "outside_parent_attempt",
        "mismatched_report_readback",
    ],
)
def test_review_qualification_failures_are_refused(tmp_path, fault):
    run = tmp_path / RUN_ID
    child = qualified_child(run)
    unit = child["pipeline"]["units"][0]
    proof = child["pipeline"]["source_results"]["cbre"]
    report_path = run / proof["validation_path"]
    context = qualification_context(run)
    if fault == "missing_report":
        report_path.unlink()
    elif fault == "null_artifact_sha":
        proof["artifact_sha256"] = child["sources"]["cbre"]["artifact"]["sha256"] = None
    elif fault == "traversal":
        proof["validation_path"] = unit["validation_path"] = (
            "../../foreign-validation.json"
        )
    elif fault == "null_generation":
        proof["generation_id"] = child["run_id"] = None
    elif fault == "arbitrary_unit":
        proof["unit_id"] = unit["unit_id"] = "../../foreign"
    elif fault == "missing_time":
        unit.pop("started_at")
    elif fault == "reversed_time":
        unit["finished_at"] = "2026-10-15T11:00:00Z"
    elif fault == "zero_bytes":
        child["sources"]["cbre"]["artifact"]["bytes"] = 0
    elif fault == "outside_parent_attempt":
        context["parent_attempts"] = [
            {"started_at": "2026-10-15T12:01:01Z", "finished_at": PARENT_END}
        ]
    else:
        report = json.loads(report_path.read_bytes())
        if fault == "report_tamper":
            report["ok"] = False
        elif fault == "missing_profile":
            report.pop("validation_profile")
        elif fault == "missing_family":
            report["queries"].pop("duplicates")
        elif fault == "missing_historical":
            report.pop("historical_primary_child_conflicts")
        elif fault == "mismatched_report_readback":
            report["queries"]["freshness_generations"][0]["active"] = "99"
        raw = json.dumps(report).encode()
        report_path.write_bytes(raw)
        if fault != "report_tamper":
            unit["validation_sha256"] = proof["validation_sha256"] = hashlib.sha256(
                raw
            ).hexdigest()
    assert not series.pipeline_source_qualified(child, "cbre", **context)


def test_qualification_without_original_run_context_fails_closed(tmp_path):
    child = qualified_child(tmp_path / RUN_ID)
    assert not series.pipeline_source_qualified(child, "cbre")


def test_full_unit_report_is_read_once_for_all_members(tmp_path, monkeypatch):
    import copy

    import cre_series_status as status

    run = tmp_path / RUN_ID
    child = qualified_child(run)
    unit = child["pipeline"]["units"][0]
    unit["sources"].append("jll")
    child["sources"]["jll"] = copy.deepcopy(child["sources"]["cbre"])
    child["sources"]["jll"]["readback"]["inventory_fingerprint"]["sourceId"] = "jll"
    child["pipeline"]["source_results"]["jll"] = copy.deepcopy(
        child["pipeline"]["source_results"]["cbre"]
    )
    report_path = run / unit["validation_path"]
    report = json.loads(report_path.read_bytes())
    for family in ("freshness_generations", "inventory_generation_fingerprints"):
        row = copy.deepcopy(report["queries"][family][0])
        row["source_key"] = "jll"
        report["queries"][family].append(row)
    raw = json.dumps(report).encode()
    report_path.write_bytes(raw)
    unit["validation_sha256"] = hashlib.sha256(raw).hexdigest()
    for proof in child["pipeline"]["source_results"].values():
        proof["validation_sha256"] = unit["validation_sha256"]
    reads = []
    original = status._read_regular_file

    def read(path, **kwargs):
        reads.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(status, "_read_regular_file", read)
    cache = {}
    for source in ("cbre", "jll"):
        assert series.pipeline_source_qualified(
            child, source, unit_cache=cache, **qualification_context(run)
        )
    assert reads == [report_path]
    # A new qualification pass must detect changed evidence; caches never persist.
    report_path.write_bytes(b"{}")
    assert not series.pipeline_source_qualified(
        child, "cbre", unit_cache={}, **qualification_context(run)
    )


def test_existing_inventory_only_readback_contract_remains_supported(tmp_path):
    run = tmp_path / RUN_ID
    child = json.loads(
        json.dumps(qualified_child(run)).replace("cbre", "cbre-dealflow")
    )
    checkpoint = child["sources"]["cbre-dealflow"]
    checkpoint["artifact"].update(staged_unique=0, inventory_only=1)
    checkpoint["readback"].update(
        generation_id=None,
        expected_staged_unique=0,
        latest_inventory_batch_active=0,
        inventory_only={"expected_active": 1},
    )
    unit = child["pipeline"]["units"][0]
    report_path = run / unit["validation_path"]
    report = json.loads(report_path.read_text().replace("cbre", "cbre-dealflow"))
    report["queries"]["freshness_generations"] = []
    report["queries"]["inventory_only_index"] = [
        {"source_key": "cbre-dealflow", "active": "1", "latest_batch_active": "1"}
    ]
    raw = json.dumps(report).encode()
    report_path.write_bytes(raw)
    unit["validation_sha256"] = hashlib.sha256(raw).hexdigest()
    child["pipeline"]["source_results"]["cbre-dealflow"]["validation_sha256"] = unit[
        "validation_sha256"
    ]
    assert series.pipeline_source_qualified(
        child, "cbre-dealflow", **qualification_context(run)
    )
    # Result/owner generation identity itself can never be absent.
    child["pipeline"]["source_results"]["cbre-dealflow"]["generation_id"] = None
    assert not series.pipeline_source_qualified(
        child, "cbre-dealflow", **qualification_context(run)
    )


@pytest.mark.parametrize("saved,queried", [("", ""), (None, None), ("", None), (None, "")])
def test_authoritative_feed_sql_null_detail_clocks_qualify(tmp_path, saved, queried):
    """Real feed reports use empty psql timestamp cells, not absent keys."""
    run = tmp_path / RUN_ID
    child = qualified_child(run)
    unit = child["pipeline"]["units"][0]
    report_path = run / unit["validation_path"]
    report = json.loads(report_path.read_bytes())
    generation = report["queries"]["freshness_generations"][0]
    readback = child["sources"]["cbre"]["readback"]
    for field in ("earliest_detail_observed_at", "latest_detail_observed_at"):
        readback[field] = saved
        generation[field] = queried
    raw = json.dumps(report).encode()
    report_path.write_bytes(raw)
    unit["validation_sha256"] = child["pipeline"]["source_results"]["cbre"]["validation_sha256"] = hashlib.sha256(raw).hexdigest()
    assert series.pipeline_source_qualified(child, "cbre", **qualification_context(run))


@pytest.mark.parametrize(
    "field,saved,queried,evidence_class",
    [
        ("earliest_inventory_observed_at", "", "", "authoritative_inventory_feed"),
        ("latest_inventory_observed_at", None, None, "authoritative_inventory_feed"),
        ("earliest_inventory_observed_at", "not-a-date", "not-a-date", "authoritative_inventory_feed"),
        ("earliest_detail_observed_at", "not-a-date", "not-a-date", "authoritative_inventory_feed"),
        ("latest_detail_observed_at", "", "2026-10-15T12:00:15Z", "authoritative_inventory_feed"),
        ("latest_detail_observed_at", "2026-10-15T12:00:15Z", "", "authoritative_inventory_feed"),
        ("earliest_detail_observed_at", "", "", "strict_detail"),
        ("latest_detail_observed_at", None, None, "property_detail"),
    ],
)
def test_qualification_refuses_missing_required_or_malformed_clock(
    tmp_path, field, saved, queried, evidence_class
):
    run = tmp_path / RUN_ID
    child = qualified_child(run)
    unit = child["pipeline"]["units"][0]
    report_path = run / unit["validation_path"]
    report = json.loads(report_path.read_bytes())
    generation = report["queries"]["freshness_generations"][0]
    readback = child["sources"]["cbre"]["readback"]
    generation["evidence_class"] = readback["evidence_class"] = evidence_class
    generation[field] = queried
    readback[field] = saved
    raw = json.dumps(report).encode()
    report_path.write_bytes(raw)
    unit["validation_sha256"] = child["pipeline"]["source_results"]["cbre"]["validation_sha256"] = hashlib.sha256(raw).hexdigest()
    assert not series.pipeline_source_qualified(child, "cbre", **qualification_context(run))

@pytest.mark.parametrize("evidence_class", ["strict_detail", "property_detail"])
def test_required_populated_detail_clocks_still_qualify(tmp_path, evidence_class):
    run = tmp_path / RUN_ID
    child = qualified_child(run)
    unit = child["pipeline"]["units"][0]
    report_path = run / unit["validation_path"]
    report = json.loads(report_path.read_bytes())
    generation = report["queries"]["freshness_generations"][0]
    readback = child["sources"]["cbre"]["readback"]
    generation["evidence_class"] = readback["evidence_class"] = evidence_class
    for field in ("earliest_detail_observed_at", "latest_detail_observed_at"):
        generation[field] = readback[field] = "2026-10-15T12:00:15Z"
    raw = json.dumps(report).encode()
    report_path.write_bytes(raw)
    unit["validation_sha256"] = child["pipeline"]["source_results"]["cbre"]["validation_sha256"] = hashlib.sha256(raw).hexdigest()
    assert series.pipeline_source_qualified(child, "cbre", **qualification_context(run))


@pytest.mark.parametrize("missing_from", ["report", "readback"])
def test_absent_required_inventory_clock_key_refused(tmp_path, missing_from):
    run = tmp_path / RUN_ID
    child = qualified_child(run)
    unit = child["pipeline"]["units"][0]
    report_path = run / unit["validation_path"]
    report = json.loads(report_path.read_bytes())
    row = (report["queries"]["freshness_generations"][0] if missing_from == "report"
           else child["sources"]["cbre"]["readback"])
    row.pop("earliest_inventory_observed_at")
    raw = json.dumps(report).encode()
    report_path.write_bytes(raw)
    unit["validation_sha256"] = child["pipeline"]["source_results"]["cbre"]["validation_sha256"] = hashlib.sha256(raw).hexdigest()
    assert not series.pipeline_source_qualified(child, "cbre", **qualification_context(run))
