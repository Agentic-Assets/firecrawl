"""Deterministic scheduling probes for actual bounded pipeline control flow."""

from types import SimpleNamespace

import pytest

import cre_checkpoint_refresh as refresh
import cre_refresh_pipeline as pipeline

SOURCES = (
    "transwestern",
    "savills",
    "nai-global",
    "marcus-millichap",
    "matthews",
    "hanley",
)


@pytest.fixture(autouse=True)
def isolated_resource_trip_state():
    """Existing fault suites may leave a synthetic sticky watchdog trip."""
    refresh._clear_cpu_guard_trip()
    try:
        yield
    finally:
        refresh._clear_cpu_guard_trip()


def run_schedule(
    tmp_path,
    monkeypatch,
    workers,
    *,
    sources=SOURCES,
    failure=None,
    artifact_failure=None,
    oversized_source=None,
    db_failure=False,
):
    run_dir = tmp_path / "2026-10-15T120000Z"
    run_dir.mkdir()
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="a" * 40,
        git_dirty=False,
        sources=sources,
        page_cap=400,
        concurrency=1,
        source_workers=workers,
    )
    clock = {"now": 0.0}
    events = []
    alive = {}
    monkeypatch.setattr(pipeline, "require_pipeline_owner", lambda *a: None)
    monkeypatch.setattr(
        pipeline.time,
        "sleep",
        lambda delay: clock.__setitem__("now", clock["now"] + delay),
    )
    monkeypatch.setattr(pipeline, "pending_disk_bytes", lambda *a, **k: 0)
    monkeypatch.setattr(
        pipeline.shutil, "disk_usage", lambda path: SimpleNamespace(free=10 * 1024**3)
    )

    def start(root, owner, source, **kwargs):
        assert source not in alive
        lanes = refresh.source_worker_lanes(source)
        assert all(not lanes & refresh.source_worker_lanes(other) for other in alive)
        assert len(alive) < workers
        checkpoint = owner["sources"][source]
        checkpoint["attempts"].append({"number": 1, "started_at": refresh.utc_now()})
        checkpoint["state"] = "collecting"
        path = root / f"{source}.tmp"
        path.write_text("candidate")
        ready_at = clock["now"] + (0.2 if source == sources[0] else 0.5)
        process = SimpleNamespace(poll=lambda: 0 if clock["now"] >= ready_at else None)
        item = SimpleNamespace(source=source, process=process, tmp_artifact=path)
        alive[source] = item
        events.append(("start", source, clock["now"]))
        return item

    def publish(root, owner, item, **kwargs):
        del alive[item.source]
        checkpoint = owner["sources"][item.source]
        checkpoint["candidate"] = {
            "path": item.tmp_artifact.name,
            "bytes": 513 * 1024**2 if item.source == oversized_source else 9,
        }
        checkpoint["state"] = "candidate"
        events.append(("published", item.source, clock["now"]))
        return item.source != failure

    def prepare(root, owner, source):
        # Deliberately slow compute, exercising the actual scoped polling wait.
        until = clock["now"] + 0.7
        pipeline.wait_with_poll_hook(
            SimpleNamespace(poll=lambda: 0 if clock["now"] >= until else None)
        )
        if source == artifact_failure:
            raise refresh.ArtifactValidationError("ordinary invalid artifact")
        checkpoint = owner["sources"][source]
        checkpoint["artifact"] = {"path": f"{source}.tmp", "sha256": "b" * 64}
        checkpoint["state"] = "validated"
        return True

    def database(root, owner, unit, env_file, previous):
        events.append(("db_start", tuple(unit["sources"]), clock["now"]))
        if db_failure:
            raise refresh.GlobalStageError("synthetic DB fault")
        until = clock["now"] + 0.6
        pipeline.wait_with_poll_hook(
            SimpleNamespace(poll=lambda: 0 if clock["now"] >= until else None)
        )
        for source in unit["sources"]:
            owner["sources"][source]["readback"] = {"ok": True}
        unit.update(validation_path="proof", validation_sha256="c" * 64)
        events.append(("db_end", tuple(unit["sources"]), clock["now"]))
        return {
            "path": "proof",
            "sha256": "c" * 64,
            "sequence": len(owner["pipeline"]["units"]),
        }

    monkeypatch.setattr(refresh, "_start_cohort_collection", start)
    monkeypatch.setattr(refresh, "_finalize_cohort_collection", publish)
    monkeypatch.setattr(pipeline, "prepare_candidate", prepare)
    monkeypatch.setattr(pipeline, "run_database_unit", database)

    def cleanup(items):
        events.append(("cleanup", tuple(item.source for item in items), clock["now"]))
        alive.clear()

    monkeypatch.setattr(refresh, "_terminate_cohort_processes", cleanup)
    failures = pipeline.run_bounded_pipeline(
        run_dir,
        manifest,
        sources,
        page_cap=400,
        concurrency=1,
        attempts_this_run=1,
        env_file=None,
        source_workers=workers,
    )
    return manifest, events, failures


@pytest.mark.parametrize("workers", [1, 2, 3, 4])
def test_pipeline_refills_during_slow_compute_and_db(tmp_path, monkeypatch, workers):
    manifest, events, failures = run_schedule(tmp_path, monkeypatch, workers)
    assert not failures
    assert len([e for e in events if e[0] == "start"]) == len(SOURCES)
    assert manifest["pipeline"]["source_workers"] == workers
    assert set(manifest["pipeline"]["source_results"]) == set(SOURCES)
    assert all(
        r["state"] == "verified"
        for r in manifest["pipeline"]["source_results"].values()
    )
    first_db = next(e for e in events if e[0] == "db_start")
    first_post = next(e for e in events if e[0] == "db_end")
    # Work started after the first publication while its source awaited compute/DB.
    first_publish = next(e for e in events if e[0] == "published")
    assert any(
        e[0] == "start" and first_publish[2] <= e[2] < first_post[2] for e in events
    )
    assert any(e[0] == "start" and first_db[2] <= e[2] < first_post[2] for e in events)
    assert len(first_db[1]) == 1  # did not wait to fill a ready cohort
    assert all(len(unit["sources"]) <= 5 for unit in manifest["pipeline"]["units"])


def test_pipeline_same_provider_releases_lane_before_prior_db_finishes(
    tmp_path, monkeypatch
):
    manifest, events, _ = run_schedule(
        tmp_path, monkeypatch, 4, sources=("svn", "lee-associates", "franklin-street")
    )
    published = next(e for e in events if e[:2] == ("published", "svn"))
    next_start = next(e for e in events if e[:2] == ("start", "lee-associates"))
    first_end = next(e for e in events if e[0] == "db_end")
    assert published[2] <= next_start[2] < first_end[2]
    assert manifest["pipeline"]["status"] == "complete"


def test_pipeline_colliers_runs_alone_through_full_db_boundary(tmp_path, monkeypatch):
    _, events, _ = run_schedule(
        tmp_path, monkeypatch, 4, sources=("colliers-main", "transwestern", "savills")
    )
    first_end = next(e for e in events if e[0] == "db_end")
    starts = [e for e in events if e[0] == "start" and e[1] != "colliers-main"]
    assert starts and all(e[2] >= first_end[2] for e in starts)


def test_pipeline_local_fetch_failure_preserves_other_verified_sources(
    tmp_path, monkeypatch
):
    manifest, _, failures = run_schedule(tmp_path, monkeypatch, 2, failure="savills")
    assert failures == ["savills"]
    assert manifest["pipeline"]["status"] == "partial"
    assert manifest["pipeline"]["source_results"]["savills"]["state"] == "failed"
    assert manifest["pipeline"]["source_results"]["transwestern"]["state"] == "verified"


def test_pipeline_local_invalid_artifact_is_excluded_before_unit_seals(
    tmp_path, monkeypatch
):
    manifest, _, failures = run_schedule(
        tmp_path, monkeypatch, 2, artifact_failure="savills"
    )
    assert failures == ["savills"]
    assert manifest["pipeline"]["status"] == "partial"
    assert all(
        "savills" not in unit["sources"] for unit in manifest["pipeline"]["units"]
    )
    assert manifest["pipeline"]["source_results"]["transwestern"]["state"] == "verified"


def test_pipeline_drains_ready_cohort_of_five_without_waiting_to_fill(
    tmp_path, monkeypatch
):
    manifest, _, failures = run_schedule(tmp_path, monkeypatch, 4)
    assert not failures
    assert [len(unit["sources"]) for unit in manifest["pipeline"]["units"]] == [1, 5]


def test_pipeline_real_owned_compute_and_db_waits_refill_fetches(tmp_path, monkeypatch):
    """Real local child groups and the real compute worker; no provider/DB IO."""
    import json
    import subprocess
    import sys
    import time
    from test_cre_checkpoint_refresh import strict_artifact
    from cre_owned_processes import OwnedProcessGroup

    root = tmp_path / "2026-10-15T120000Z"
    root.mkdir()
    sources = ("transwestern", "savills", "nai-global", "marcus-millichap")
    manifest = refresh.new_manifest(
        root,
        git_sha="a" * 40,
        git_dirty=False,
        sources=sources,
        page_cap=400,
        concurrency=1,
        source_workers=2,
    )
    events = []
    monkeypatch.setattr(pipeline, "require_pipeline_owner", lambda *a: None)
    original_command = refresh.run_command

    def command(argv, log, **kwargs):
        if "cre_pipeline_artifact_worker.py" in str(argv[1]):
            # Delay the actual compute process, not parent validation code.
            events.append(("compute", time.monotonic()))
            code = "import runpy,sys,time; time.sleep(0.3); sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"
            argv = [sys.executable, "-c", code, *argv[1:]]
        return original_command(argv, log, **kwargs)

    def start(run_dir, owner, source, **kwargs):
        created = refresh._cohort_attempt(
            run_dir, owner, source, collector_concurrency=1
        )
        attempt, temporary, log_path, started = created
        stamp = refresh.utc_now()
        payload = strict_artifact(
            source,
            detail_scope="detail_page",
            generation=owner["run_id"],
            generation_started_at=owner["started_at"],
        )
        payload["runMeta"].update(startedAt=stamp, finishedAt=stamp)
        for row in payload["listings"]:
            row.update(inventoryObservedAt=stamp, detailObservedAt=stamp)
        code = "import pathlib,sys,time; time.sleep(float(sys.argv[1])); pathlib.Path(sys.argv[2]).write_text(sys.argv[3])"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("a")
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                code,
                "0.10" if source == sources[0] else "0.20",
                str(temporary),
                json.dumps(payload),
            ],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        events.append(("start", source, time.monotonic()))
        return refresh.CohortCollectionProcess(
            source,
            process,
            log,
            temporary,
            attempt,
            started,
            OwnedProcessGroup.from_spawn(process),
        )

    def database(run_dir, owner, unit, env_file, previous):
        events.append(("db_start", time.monotonic()))
        assert (
            refresh.run_command(
                [sys.executable, "-c", "import time; time.sleep(0.3)"],
                root / "logs" / "offline-db.log",
            )
            == 0
        )
        events.append(("db_end", time.monotonic()))
        for source in unit["sources"]:
            owner["sources"][source]["readback"] = {"ok": True}
        unit.update(validation_path="offline-proof", validation_sha256="c" * 64)
        return None

    monkeypatch.setattr(refresh, "run_command", command)
    monkeypatch.setattr(refresh, "_start_cohort_collection", start)
    monkeypatch.setattr(pipeline, "run_database_unit", database)
    assert (
        pipeline.run_bounded_pipeline(
            root,
            manifest,
            sources,
            page_cap=400,
            concurrency=1,
            attempts_this_run=1,
            env_file=None,
            source_workers=2,
        )
        == []
    )
    first_compute = next(event[1] for event in events if event[0] == "compute")
    fourth_start = next(
        event[2] for event in events if event[:2] == ("start", sources[3])
    )
    first_db = next(event[1] for event in events if event[0] == "db_start")
    assert first_compute < fourth_start < first_db
    assert manifest["pipeline"]["status"] == "complete"


def test_pipeline_oversized_candidate_waits_for_owned_fetches_then_drains_alone(
    tmp_path, monkeypatch
):
    manifest, events, failures = run_schedule(
        tmp_path, monkeypatch, 4, oversized_source=SOURCES[0]
    )
    assert not failures
    unit = manifest["pipeline"]["units"][0]
    assert unit["sources"] == [SOURCES[0]]
    end = next(e[2] for e in events if e[0] == "db_end")
    later_starts = [e[2] for e in events if e[0] == "start" and e[1] in SOURCES[4:]]
    assert later_starts and min(later_starts) >= end
    other_publications = [
        e[2] for e in events if e[0] == "published" and e[1] in SOURCES[1:4]
    ]
    begin = next(e[2] for e in events if e[0] == "db_start")
    assert max(other_publications) < begin


def test_pipeline_global_db_fault_records_stop_and_no_false_success(
    tmp_path, monkeypatch
):
    import json

    with pytest.raises(refresh.GlobalStageError, match="synthetic DB fault"):
        run_schedule(tmp_path, monkeypatch, 2, db_failure=True)
    manifest = json.loads(
        (tmp_path / "2026-10-15T120000Z" / "manifest.json").read_text()
    )
    assert manifest["pipeline"]["status"] == "reconciliation_required"
    assert all(
        result["state"] == "failed"
        for result in manifest["pipeline"]["source_results"].values()
    )
    assert manifest["pipeline"]["units"][0]["state"] == "failed"
    assert manifest["pipeline"]["commit_outcome_unknown"] is False
