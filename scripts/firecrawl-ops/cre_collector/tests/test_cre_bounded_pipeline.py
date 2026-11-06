"""Deterministic scheduling probes for actual bounded pipeline control flow."""

from datetime import UTC, datetime, timedelta
import hashlib
import json
import subprocess
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
    dispatch_sources=None,
    failure=None,
    artifact_failure=None,
    oversized_source=None,
    db_failure=False,
    retained_paths=(),
    temporary_bytes=None,
    free_bytes=None,
    attempts_this_run=1,
    terminal_code=None,
    failure_once=None,
    deterministic_clock=False,
    pyramid_partial=False,
    pyramid_admitted=False,
    pyramid_guard_refusal=False,
    pyramid_guard_valid=True,
    transwestern_guard_refusal=False,
    jll_guard_refusal=False,
    settled_ingest_failure=None,
    atomic_rollback_valid=True,
    colliers_partial=None,
    colliers_failure_nonzero=False,
    lyon_partial=None,
    clock_deferrals=None,
    owner_check=None,
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
        pipeline=pyramid_partial or pyramid_admitted,
        admit_current_inventory_sources=("pyramid-brokerage",) if pyramid_partial or pyramid_admitted else (),
    )
    for relative in retained_paths:
        path = run_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            handle.truncate(513 * 1024**2)
    clock = {"now": 0.0}
    events = []
    alive = {}
    if clock_deferrals:
        monkeypatch.setattr(pipeline.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(pipeline, "require_pipeline_owner", lambda *a: None)
    if deterministic_clock or failure_once is not None:
        epoch = datetime(2026, 10, 21, tzinfo=UTC)

        def utc_now():
            return (epoch + timedelta(seconds=clock["now"])).isoformat()

        monkeypatch.setattr(refresh, "utc_now", utc_now)
    monkeypatch.setattr(
        pipeline.time,
        "sleep",
        lambda delay: clock.__setitem__("now", clock["now"] + delay),
    )
    monkeypatch.setattr(
        pipeline.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(
            free=free_bytes(clock["now"]) if free_bytes else 10 * 1024**3
        ),
    )

    def start(root, owner, source, **kwargs):
        if clock_deferrals and clock["now"] < clock_deferrals.get(("start", source), 0):
            events.append(("clock_start_pending", source, clock["now"]))
            raise refresh.ClockOrderingPending("synthetic wall reversal")
        assert source not in alive
        lanes = refresh.source_worker_lanes(source)
        assert all(not lanes & refresh.source_worker_lanes(other) for other in alive)
        assert len(alive) < workers
        checkpoint = owner["sources"][source]
        attempt_number = len(checkpoint["attempts"]) + 1
        checkpoint["attempts"].append(
            {"number": attempt_number, "started_at": refresh.utc_now()}
        )
        checkpoint["state"] = "collecting"
        path = root / "sources" / f"{source}.tmp"
        path.parent.mkdir(exist_ok=True)
        path.write_text("candidate")
        if temporary_bytes and source in temporary_bytes:
            with path.open("r+b") as handle:
                handle.truncate(temporary_bytes[source])
        ready_at = clock["now"] + (0.2 if source == sources[0] else 0.5)
        process = SimpleNamespace(poll=lambda: 0 if clock["now"] >= ready_at else None)
        item = SimpleNamespace(
            source=source,
            process=process,
            tmp_artifact=path,
            attempt_started_at=checkpoint["attempts"][-1]["started_at"],
            attempt_number=attempt_number,
        )
        alive[source] = item
        events.append(("start", source, clock["now"], attempt_number))
        return item

    def publish(root, owner, item, **kwargs):
        if clock_deferrals and clock["now"] < clock_deferrals.get(("finish", item.source), 0):
            if not kwargs.get("clock_expired"):
                events.append(("clock_finish_pending", item.source, clock["now"]))
                raise refresh.ClockOrderingPending("synthetic wall reversal")
            del alive[item.source]
            owner["sources"][item.source]["state"] = "collect_infrastructure_failed"
            events.append(("clock_finish_refused", item.source, clock["now"]))
            return False
        del alive[item.source]
        checkpoint = owner["sources"][item.source]
        checkpoint["candidate"] = {
            "path": str(item.tmp_artifact.relative_to(root)),
            "bytes": 513 * 1024**2
            if item.source == oversized_source
            else item.tmp_artifact.stat().st_size,
        }
        checkpoint["state"] = "candidate"
        events.append(("published", item.source, clock["now"], item.attempt_number))
        if lyon_partial and item.source == "lyon-stahl":
            checkpoint["state"] = "collect_failed"
            item.attempt = checkpoint["attempts"][-1]
            item.attempt.update(rc=(refresh.COHORT_SETTLED_INTERRUPT_EXIT if lyon_partial == "interrupted"
                                   else refresh.COHORT_SETTLED_FAILURE_EXIT), finished_at=refresh.utc_now(),
                **{"lyon-stahl_partial": {"state": "sealed_local", "outcome": "source_error"}})
            if lyon_partial == "clock_refused":
                item.attempt["clock_order_refused"] = True
            return False
        if colliers_failure_nonzero and item.source == "colliers":
            checkpoint["state"] = "collect_failed"
            item.attempt = checkpoint["attempts"][-1]
            item.attempt.update(rc=(refresh.COHORT_SETTLED_INTERRUPT_EXIT if colliers_failure_nonzero == "interrupted"
                                   else refresh.COHORT_SETTLED_FAILURE_EXIT), finished_at=refresh.utc_now(),
                colliers_partial={"state": "sealed_local", "outcome": "source_error"})
            return False
        if item.source == failure or (
            item.source == failure_once and item.attempt_number == 1
        ):
            checkpoint["state"] = "collect_failed"
            if terminal_code:
                checkpoint["terminal_collection_failure"] = {"code": terminal_code}
                if item.source == "pyramid-brokerage":
                    checkpoint["terminal_collection_failure"]["http_status"] = 403
            return False
        return True

    def prepare(root, owner, source):
        # Deliberately slow compute, exercising the actual scoped polling wait.
        until = clock["now"] + 0.7
        pipeline.wait_with_poll_hook(
            SimpleNamespace(poll=lambda: 0 if clock["now"] >= until else None)
        )
        if source == artifact_failure and not (colliers_partial and owner["sources"][source].get("partial_attempted")):
            raise refresh.ArtifactValidationError("ordinary invalid artifact")
        checkpoint = owner["sources"][source]
        checkpoint["artifact"] = {"path": f"sources/{source}.tmp", "sha256": "b" * 64}
        if lyon_partial in {"query_refusal", "timeout_refusal"} and source == "lyon-stahl":
            checkpoint["artifact"]["sha256"] = refresh.sha256_file(root / checkpoint["artifact"]["path"])
        if colliers_partial and source == "colliers":
            checkpoint["artifact"]["colliers_partial"] = True
            checkpoint["gate"] = {"current_inventory_proof": {"emitted_count": 1, "held_count": 1}}
        if lyon_partial and source == "lyon-stahl":
            checkpoint["artifact"]["lyon_partial"] = True
            checkpoint["gate"] = {"current_inventory_proof": {"emitted_count": 1, "held_count": 0}}
        if pyramid_partial and source == "pyramid-brokerage":
            checkpoint["artifact"]["pyramid_subset"] = True
            checkpoint["gate"] = {"current_inventory_proof": {"inventory_count": 102, "emitted_count": 100, "held_count": 2}}
        checkpoint["state"] = "validated"
        return True

    def database(root, owner, unit, env_file, previous):
        events.append(("db_start", tuple(unit["sources"]), clock["now"]))
        if pyramid_guard_refusal and "pyramid-brokerage" in unit["sources"]:
            owner["sources"]["pyramid-brokerage"]["state"] = "pyramid_historical_guard_refused"
            raise refresh.PyramidHistoricalGuardRefusal("typed")
        if transwestern_guard_refusal and "transwestern" in unit["sources"]:
            owner["sources"]["transwestern"]["state"] = "transwestern_image_guard_refused"
            raise refresh.TranswesternImageGuardRefusal("Transwestern protected image multiplicity lost")
        if jll_guard_refusal and "jll" in unit["sources"]:
            owner["sources"]["jll"]["state"] = "jll_image_guard_refused"
            raise refresh.JllImageGuardRefusal("JLL image-loss guard refused")
        if settled_ingest_failure in unit["sources"]:
            owner["sources"][settled_ingest_failure]["state"] = "settled_ingest_failed"
            raise refresh.SettledIngestFailure("settled synthetic ingest failure")
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
    actual_preseal = pipeline.preseal_source_gate
    monkeypatch.setattr(pipeline, "preseal_source_gate", lambda _root, _owner, source, _env: not (lyon_partial == "probe_hold" and source == "lyon-stahl"))
    monkeypatch.setattr(pipeline, "run_database_unit", database)
    if colliers_partial:
        import cre_colliers_partial_checkpoint as partial

        def archive(_path, _root, _manifest, _source, attempt):
            attempt["colliers_partial"] = {"state": "sealed_local", "outcome": "source_error"}
            return "rejected/colliers.json", "d" * 64

        def worker(root, owner, _lock, _runner, _save, *, window_index=1, stop_before_position=None):
            events.append(("partial_worker", window_index, stop_before_position))
            checkpoint = owner["sources"]["colliers"]
            checkpoint["partial_attempted"] = True
            if colliers_partial == "held":
                checkpoint["state"] = "partial_held_no_write"
                return None
            return root / checkpoint["candidate"]["path"]

        monkeypatch.setattr(refresh, "archive_rejected_with_evidence", archive)
        monkeypatch.setattr(partial, "run_window_worker", worker)
        # This scheduler fixture has no real receipt or cursor; end the single
        # synthetic window here. Receipt-bound continuation is tested separately.
        monkeypatch.setattr(partial, "archive_verified_pipeline_window", lambda *_args: False)
        monkeypatch.setattr(partial, "archive_no_write_pipeline_window", lambda *_args: False)
    if lyon_partial:
        import cre_lyon_partial_checkpoint as partial

        def lyon_worker(root, owner, _lock, _save):
            checkpoint = owner["sources"]["lyon-stahl"]
            checkpoint["partial_attempted"] = True
            if lyon_partial == "held":
                checkpoint["state"] = "partial_held_no_write"
                return None
            return root / checkpoint["candidate"]["path"]

        monkeypatch.setattr(partial, "run_replay_worker", lyon_worker)

        if lyon_partial in {"query_refusal", "timeout_refusal"}:
            import cre_ingest as ingest
            import cre_validate
            from cre_lyon_partial import stage_record
            from tests.test_lyon_partial_stage import record

            manifest["preflight"]["database_target"] = {"algorithm": "sha256", "value": "b" * 64}
            monkeypatch.setattr(refresh, "git_identity", lambda: ("a" * 40, False))
            monkeypatch.setattr(refresh, "sha256_file", lambda path: hashlib.sha256(path.read_bytes()).hexdigest())
            monkeypatch.setattr(ingest, "load_db_url", lambda _: ("synthetic", None))
            monkeypatch.setattr(ingest, "database_target_fingerprint_from_url", lambda _: {"value": "b" * 64})
            monkeypatch.setattr(ingest, "find_psql", lambda: "synthetic")
            staged = [stage_record(record("https://lyonstahl.com/properties/example/", "property_path", "example"))]
            source_receipt = {"database_target_sha256": "b" * 64}
            monkeypatch.setattr(partial, "verify_candidate_artifact", lambda *args, **kwargs: {
                "source": source_receipt, "staged": staged})

            def query(_psql, _url, sql, timeout):
                if "SELECT 1 AS healthy" in sql:
                    events.append(("probe_health", "lyon-stahl", clock["now"]))
                    return SimpleNamespace(returncode=0, stdout="healthy" + chr(10) + "1" + chr(10))
                events.append(("probe_failure", "lyon-stahl", clock["now"]))
                if lyon_partial == "timeout_refusal":
                    raise subprocess.TimeoutExpired(cmd="psql", timeout=timeout)
                return SimpleNamespace(returncode=3, stderr="ERROR:  22023: invalid source input")

            def gate_command(argv, _log, env=None):
                from pathlib import Path
                Path(argv[argv.index("--out") + 1]).write_text(json.dumps({"per_source": {"lyon-stahl": {}}}))
                return 0

            monkeypatch.setattr(cre_validate, "_run_psql_script", query)
            monkeypatch.setattr(refresh, "run_command", gate_command)
            monkeypatch.setattr(pipeline, "preseal_source_gate", lambda root, owner, source, env:
                actual_preseal(root, owner, source, env) if source == "lyon-stahl" else True)
    if settled_ingest_failure:
        import cre_atomic_ingest_rollback as atomic
        monkeypatch.setattr(atomic, "verified_rollback", lambda *a: atomic_rollback_valid)
    if pyramid_guard_refusal or transwestern_guard_refusal or jll_guard_refusal:
        import cre_pyramid_rollback_result as outcome
        monkeypatch.setattr(outcome, "verified_rollback", lambda *a: pyramid_guard_valid)

    def cleanup(items):
        events.append(("cleanup", tuple(item.source for item in items), clock["now"]))
        alive.clear()

    monkeypatch.setattr(refresh, "_terminate_cohort_processes", cleanup)
    failures = pipeline.run_bounded_pipeline(
        run_dir,
        manifest,
        dispatch_sources or sources,
        page_cap=400,
        concurrency=1,
        attempts_this_run=attempts_this_run,
        env_file=None,
        source_workers=workers,
        owner_check=owner_check,
    )
    return manifest, events, failures


def test_clock_deferred_source_does_not_block_peer_or_db_polling(tmp_path, monkeypatch):
    manifest, events, failures = run_schedule(tmp_path, monkeypatch, 2,
        sources=("savills", "transwestern"),
        clock_deferrals={("start", "savills"): 0.15, ("finish", "savills"): 0.8})
    assert failures == []
    assert any(event[:2] == ("clock_start_pending", "savills") for event in events)
    assert any(event[:2] == ("clock_finish_pending", "savills") for event in events)
    assert next(event[2] for event in events if event[:2] == ("start", "transwestern")) < 0.15
    assert all(manifest["pipeline"]["source_results"][key]["state"] == "verified"
               for key in ("savills", "transwestern"))


def test_clock_deferred_single_source_makes_progress_without_active_worker(tmp_path, monkeypatch):
    manifest, _, failures = run_schedule(tmp_path, monkeypatch, 1,
        sources=("savills",), clock_deferrals={("start", "savills"): 0.15})
    assert failures == []
    assert manifest["pipeline"]["source_results"]["savills"]["state"] == "verified"


def test_clock_deferred_first_source_does_not_starve_single_worker_peer(tmp_path, monkeypatch):
    manifest, events, failures = run_schedule(tmp_path, monkeypatch, 1,
        sources=("savills", "transwestern"), clock_deferrals={("start", "savills"): 0.15})
    assert failures == []
    assert next(event[1] for event in events if event[0] == "start") == "transwestern"
    assert manifest["pipeline"]["source_results"]["savills"]["state"] == "verified"


@pytest.mark.parametrize("phase", ["start", "finish"])
def test_clock_deferral_timeout_fails_only_source(tmp_path, monkeypatch, phase):
    manifest, events, failures = run_schedule(tmp_path, monkeypatch, 2,
        sources=("savills", "transwestern"), clock_deferrals={(phase, "savills"): 10})
    assert failures == ["savills"]
    assert manifest["pipeline"]["source_results"]["transwestern"]["state"] == "verified"
    assert manifest["sources"]["savills"]["state"] == "collect_infrastructure_failed"
    assert not any(event[:2] == ("db_start", ("savills",)) for event in events)


@pytest.mark.parametrize("outcome", ["accepted", "held"])
def test_pipeline_colliers_partial_preserves_other_sources(tmp_path, monkeypatch, outcome):
    manifest, events, failures = run_schedule(tmp_path, monkeypatch, 1,
        sources=("colliers", "savills"), artifact_failure="colliers", colliers_partial=outcome)
    assert manifest["pipeline"]["status"] == "partial"
    assert manifest["pipeline"]["source_results"]["savills"]["state"] == "verified"
    assert ("partial_worker", 1, None) in events
    if outcome == "accepted":
        assert failures == []
        assert manifest["pipeline"]["source_results"]["colliers"]["partial"] is True
        assert any(event[0] == "db_start" and "colliers" in event[1] for event in events)
    else:
        assert failures == ["colliers"]
        assert not any(event[0] == "db_start" and "colliers" in event[1] for event in events)


@pytest.mark.parametrize("outcome", ["accepted", "held"])
def test_pipeline_settled_nonzero_colliers_evidence_reaches_foreground(tmp_path, monkeypatch, outcome):
    manifest, events, failures = run_schedule(tmp_path, monkeypatch, 1,
        sources=("colliers", "savills"), colliers_partial=outcome, colliers_failure_nonzero=True)
    assert manifest["pipeline"]["source_results"]["savills"]["state"] == "verified"
    assert manifest["pipeline"]["status"] == "partial"
    assert ("partial_worker", 1, None) in events
    if outcome == "accepted":
        assert failures == []
        assert manifest["pipeline"]["source_results"]["colliers"]["partial"] is True
        assert any(event[0] == "db_start" and "colliers" in event[1] for event in events)
    else:
        assert failures == ["colliers"]
        assert not any(event[0] == "db_start" and "colliers" in event[1] for event in events)


def test_pipeline_interrupted_colliers_attempt_never_dispatches_partial(tmp_path, monkeypatch):
    manifest, events, failures = run_schedule(tmp_path, monkeypatch, 1,
        sources=("colliers", "savills"), colliers_partial="accepted", colliers_failure_nonzero="interrupted")
    assert failures == ["colliers"]
    assert not manifest["sources"]["colliers"].get("partial_attempted")
    assert not any(event[0] == "db_start" and "colliers" in event[1] for event in events)


@pytest.mark.parametrize("outcome", ["accepted", "held", "interrupted", "clock_refused", "probe_hold", "query_refusal", "timeout_refusal"])
def test_pipeline_lyon_partial_keeps_peer_running(tmp_path, monkeypatch, outcome):
    manifest, events, failures = run_schedule(tmp_path, monkeypatch, 1,
        sources=("lyon-stahl", "savills"), lyon_partial=outcome)
    assert manifest["pipeline"]["source_results"]["savills"]["state"] == "verified"
    assert manifest["pipeline"]["status"] == "partial"
    if outcome in {"query_refusal", "timeout_refusal"}:
        assert any(event[0] == "probe_failure" for event in events)
        assert any(event[0] == "probe_health" for event in events)
    if outcome == "accepted":
        assert failures == []
        assert manifest["pipeline"]["source_results"]["lyon-stahl"]["partial"] is True
        assert any(event[0] == "db_start" and "lyon-stahl" in event[1] for event in events)
    else:
        assert failures == ["lyon-stahl"]
        assert not any(event[0] == "db_start" and "lyon-stahl" in event[1] for event in events)
        if outcome in {"interrupted", "clock_refused"}:
            assert not manifest["sources"]["lyon-stahl"].get("partial_attempted")


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
    assert all(len(unit["sources"]) == 1 for unit in manifest["pipeline"]["units"])


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


@pytest.mark.parametrize("retry", [False, True])
def test_pipeline_source_timing_uses_first_attempt_and_separates_wall_time(
    tmp_path, monkeypatch, retry
):
    """Use simulated control-flow time only; this is not source acceptance evidence."""
    from cre_performance_compare import source_stage_timing

    sources = ("transwestern", "savills", "nai-global")
    manifest, events, failures = run_schedule(
        tmp_path,
        monkeypatch,
        1,
        sources=sources,
        failure_once="savills" if retry else None,
        attempts_this_run=2,
        deterministic_clock=True,
    )

    assert failures == []
    assert manifest["pipeline"]["source_results"]["savills"]["state"] == "verified"
    attempts = [event for event in events if event[:2] == ("start", "savills")]
    expected_attempts = 2 if retry else 1
    assert [event[3] for event in attempts] == list(range(1, expected_attempts + 1))
    completions = [event for event in events if event[:2] == ("published", "savills")]
    assert [event[3] for event in completions] == list(
        range(1, expected_attempts + 1)
    )
    attempt_records = manifest["sources"]["savills"]["attempts"]
    assert [attempt["started_at"] for attempt in attempt_records] == [
        (
            datetime(2026, 10, 21, tzinfo=UTC)
            + timedelta(seconds=event[2])
        ).isoformat()
        for event in attempts
    ]

    timing = manifest["pipeline"]["source_timings"]["savills"]
    first_start = timing["collection_started_at"]
    queued_at = timing["queued_at"]
    first_start_from_pipeline_event = (
            datetime(2026, 10, 21, tzinfo=UTC)
        + timedelta(seconds=attempts[0][2])
    ).isoformat()
    assert first_start == first_start_from_pipeline_event

    stages = source_stage_timing(timing)
    expected_dispatch_wait = (
        datetime.fromisoformat(first_start) - datetime.fromisoformat(queued_at)
    ).total_seconds()
    assert stages["dispatch_wait_seconds"] == pytest.approx(expected_dispatch_wait)
    assert stages["dispatch_wait_seconds"] > 0
    expected_source_elapsed = (
        datetime.fromisoformat(timing["verified_at"])
        - datetime.fromisoformat(first_start)
    ).total_seconds()
    assert stages["source_elapsed_seconds"] == pytest.approx(expected_source_elapsed)
    attempt_active_seconds = sum(
        completion[2] - attempt[2]
        for attempt, completion in zip(attempts, completions, strict=True)
    )
    assert stages["collection_wall_seconds"] >= attempt_active_seconds
    if retry:
        assert attempts[1][2] > completions[0][2]
        assert stages["source_elapsed_seconds"] > attempt_active_seconds
    assert queued_at < first_start


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


def test_pipeline_drains_ready_sources_as_validated_singleton_units(
    tmp_path, monkeypatch
):
    manifest, _, failures = run_schedule(tmp_path, monkeypatch, 4)
    assert not failures
    assert [len(unit["sources"]) for unit in manifest["pipeline"]["units"]] == [1] * len(SOURCES)


@pytest.mark.parametrize("retained_cache", [False, True])
def test_pipeline_real_owned_compute_and_db_waits_refill_fetches(
    tmp_path, monkeypatch, retained_cache
):
    """Real local child groups and the real compute worker; no provider/DB IO."""
    import json
    import subprocess
    import sys
    import time
    from test_cre_checkpoint_refresh import strict_artifact
    from cre_owned_processes import OwnedProcessGroup, settle_owned_groups

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
    owned = {}
    cache_ready = root / "cache-ready"
    release_cache_worker = root / "release-cache-worker"
    first_compute_started = root / "first-compute-started"
    fourth_started = root / "fourth-started"
    first_compute_launched = False
    monkeypatch.setattr(pipeline, "require_pipeline_owner", lambda *a: None)
    original_command = refresh.run_command

    def command(argv, log, **kwargs):
        nonlocal first_compute_launched
        if "cre_pipeline_artifact_worker.py" in str(argv[1]):
            # Delay the actual compute process, not parent validation code.
            events.append(("compute", time.monotonic()))
            code = "import runpy,sys,time; time.sleep(0.3); sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"
            if not first_compute_launched:
                first_compute_launched = True
                # The real worker stays in flight while its parent's real poll
                # hook frees the held collector slot and dispatches source four.
                code = (
                    "import pathlib,runpy,sys,time; root=pathlib.Path(sys.argv[1]); "
                    "(root/'first-compute-started').touch(); deadline=time.monotonic()+5\n"
                    "while not (root/'fourth-started').exists():\n"
                    " if time.monotonic()>=deadline: raise RuntimeError('fourth fetch not dispatched during compute')\n"
                    " time.sleep(0.01)\n"
                    "sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name='__main__')"
                )
                argv = [sys.executable, "-c", code, str(root), *argv[1:]]
            else:
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
        if retained_cache and source == sources[0]:
            code = (
                "import pathlib,sys,time; root=pathlib.Path(sys.argv[2]).parent.parent; "
                "cache=root/'cache/jll/page.json'; cache.parent.mkdir(parents=True); "
                "handle=cache.open('wb'); handle.truncate(513*1024**2); handle.close(); "
                "(root/'cache-ready').touch(); deadline=time.monotonic()+5\n"
                "while not (root/'release-cache-worker').exists():\n"
                " if time.monotonic()>=deadline: raise RuntimeError('independent dispatch withheld')\n"
                " time.sleep(0.01)\n"
                "time.sleep(float(sys.argv[1])); pathlib.Path(sys.argv[2]).write_text(sys.argv[3])"
            )
        if source == sources[1]:
            # Fixed child sleeps cannot require compute to begin before both
            # collection slots refill. Hold one slot until actual worker start.
            code = (
                "import pathlib,sys,time; root=pathlib.Path(sys.argv[2]).parent.parent; "
                "deadline=time.monotonic()+5\n"
                "while not (root/'first-compute-started').exists():\n"
                " if time.monotonic()>=deadline: raise RuntimeError('first compute did not start')\n"
                " time.sleep(0.01)\n"
                "pathlib.Path(sys.argv[2]).write_text(sys.argv[3])"
            )
        if retained_cache and source == sources[1]:
            assert owned[sources[0]].process.poll() is None
            release_cache_worker.touch()
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
        item = refresh.CohortCollectionProcess(
            source,
            process,
            log,
            temporary,
            attempt,
            started,
            OwnedProcessGroup.from_spawn(process),
        )
        owned[source] = item
        if source == sources[3]:
            fourth_started.touch()
        if retained_cache and source == sources[0]:
            deadline = time.monotonic() + 3
            while not cache_ready.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    refresh._terminate_cohort_processes([item])
                    pytest.fail("owned cache writer did not publish readiness")
                time.sleep(0.01)
        return item

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
    monkeypatch.setattr(pipeline, "preseal_source_gate", lambda *args: True)
    monkeypatch.setattr(pipeline, "run_database_unit", database)
    try:
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
    finally:
        first_compute_started.touch()
        fourth_started.touch()
        release_cache_worker.touch()
        groups = [item.owned_group for item in owned.values()]
        settlement = settle_owned_groups(groups, grace_seconds=1, kill_seconds=2)
        assert settlement.settled, "private fixture children did not settle"
        for item in owned.values():
            item.log_handle.close()
    first_compute = next(event[1] for event in events if event[0] == "compute")
    fourth_start = next(
        event[2] for event in events if event[:2] == ("start", sources[3])
    )
    first_db = next(event[1] for event in events if event[0] == "db_start")
    assert first_compute < fourth_start < first_db
    assert manifest["pipeline"]["status"] == "complete"
    if retained_cache:
        assert (root / "cache/jll/page.json").stat().st_size == 513 * 1024**2
        assert all(item.process.poll() is not None for item in owned.values())


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


@pytest.mark.parametrize(
    "retained", ["cache/jll/page.json", "rejected/old-attempt.json"]
)
def test_retained_auxiliary_bytes_do_not_withhold_an_independent_collection_slot(
    tmp_path, monkeypatch, retained
):
    manifest, events, failures = run_schedule(
        tmp_path,
        monkeypatch,
        2,
        sources=("transwestern", "savills"),
        retained_paths=(retained,),
    )
    assert not failures
    first_publish = next(event for event in events if event[0] == "published")
    second_start = next(event for event in events if event[:2] == ("start", "savills"))
    assert second_start[2] < first_publish[2], (
        "free independent slot withheld by retained data"
    )
    path = tmp_path / manifest["run_id"] / retained
    assert path.stat().st_size == 513 * 1024**2
    assert manifest["pipeline"]["status"] == "complete"


def test_actual_pending_temp_output_blocks_until_oversized_unit_drains(
    tmp_path, monkeypatch
):
    manifest, events, failures = run_schedule(
        tmp_path,
        monkeypatch,
        2,
        sources=("transwestern", "savills"),
        temporary_bytes={"transwestern": 513 * 1024**2},
    )
    assert not failures
    first_end = next(event for event in events if event[0] == "db_end")
    second_start = next(event for event in events if event[:2] == ("start", "savills"))
    assert second_start[2] >= first_end[2]
    assert manifest["pipeline"]["units"][0]["sources"] == ["transwestern"]
    # Drained output is retained as evidence, rather than deleted to create capacity.
    assert (
        tmp_path / manifest["run_id"] / "sources/transwestern.tmp"
    ).stat().st_size == 513 * 1024**2


def test_zero_pending_queue_still_refuses_low_disk(tmp_path, monkeypatch):
    with pytest.raises(refresh.GlobalStageError, match="disk/output admission"):
        run_schedule(
            tmp_path,
            monkeypatch,
            2,
            sources=("transwestern", "savills"),
            free_bytes=lambda now: 1024**3,
        )
    import json

    manifest = json.loads(next(tmp_path.glob("*/manifest.json")).read_text())
    assert all(
        not checkpoint["attempts"] for checkpoint in manifest["sources"].values()
    )
    assert manifest["pipeline"]["status"] == "reconciliation_required"


@pytest.mark.parametrize("source,code,attempts", [
    ("pyramid-brokerage", refresh.PYRAMID_DENIED_FAILURE_CODE, 1),
    ("colliers-main", "COLLIERS_MAIN_BOOTSTRAP_HTTP_403", 1),
    ("avison-young", refresh.AVISON_YOUNG_TERMINAL_FAILURE_CODE, 1),
    ("colliers-main", refresh.AVISON_YOUNG_TERMINAL_FAILURE_CODE, 3),
    ("cbre", "COLLIERS_MAIN_BOOTSTRAP_HTTP_403", 3),
    ("colliers-main", None, 3),
])
def test_pipeline_terminal_source_refusal_does_not_retry_or_block_peer(
    tmp_path, monkeypatch, source, code, attempts
):
    manifest, events, failures = run_schedule(
        tmp_path, monkeypatch, 2, sources=(source, "hanley"), failure=source,
        terminal_code=code, attempts_this_run=3,
    )
    assert failures == [source]
    assert len([event for event in events if event[:2] == ("start", source)]) == attempts
    assert manifest["pipeline"]["source_results"][source]["state"] == "failed"
    assert manifest["sources"][source]["artifact"] is None
    assert manifest["pipeline"]["source_results"]["hanley"]["state"] == "verified"
    assert all(source not in unit["sources"] for unit in manifest["pipeline"]["units"])


def test_scheduled_pipeline_marks_pyramid_subset_partial_and_verifies_sibling(tmp_path, monkeypatch):
    manifest, events, failures = run_schedule(tmp_path, monkeypatch, 2,
        sources=("pyramid-brokerage", "hanley"), pyramid_partial=True)
    assert failures == []
    assert manifest["pipeline"]["status"] == "partial"
    pyramid = manifest["pipeline"]["source_results"]["pyramid-brokerage"]
    assert pyramid["state"] == "verified" and pyramid["partial"] is True
    assert (pyramid["emitted_count"], pyramid["held_count"]) == (100, 2)
    assert pyramid["whole_source_coverage"] is False
    assert manifest["pipeline"]["source_results"]["hanley"]["state"] == "verified"
    assert any(event[0] == "db_end" and "hanley" in event[1] for event in events)


def test_scheduled_pipeline_complete_pyramid_does_not_become_partial(tmp_path, monkeypatch):
    manifest, _, failures = run_schedule(tmp_path, monkeypatch, 2,
        sources=("pyramid-brokerage", "hanley"), pyramid_admitted=True)
    assert failures == [] and manifest["pipeline"]["status"] == "complete"
    assert manifest["pipeline"]["source_results"]["pyramid-brokerage"].get("partial") is not True


@pytest.mark.parametrize("refused", [False, True])
def test_pyramid_is_singleton_unit_and_healthy_peers_continue(tmp_path, monkeypatch, refused):
    sources = ("savills", "pyramid-brokerage", "hanley")
    manifest, events, failures = run_schedule(tmp_path, monkeypatch, 2, sources=sources,
                                               pyramid_guard_refusal=refused)
    pyramid_starts = [event for event in events if event[0] == "db_start" and "pyramid-brokerage" in event[1]]
    assert len(pyramid_starts) == 1 and pyramid_starts[0][1] == ("pyramid-brokerage",)
    prior_verified = next(i for i, event in enumerate(events) if event[0] == "db_end" and "savills" in event[1])
    guard_start = next(i for i, event in enumerate(events) if event == pyramid_starts[0])
    assert prior_verified < guard_start
    assert manifest["pipeline"]["source_results"]["savills"]["state"] == "verified"
    assert manifest["pipeline"]["source_results"]["hanley"]["state"] == "verified"
    if refused:
        assert failures == ["pyramid-brokerage"] and manifest["pipeline"]["status"] == "partial"
        assert manifest["pipeline"]["source_results"]["pyramid-brokerage"]["state"] == "failed"
        pyramid_units = [unit for unit in manifest["pipeline"]["units"] if "pyramid-brokerage" in unit["sources"]]
        assert len(pyramid_units) == 1 and pyramid_units[0]["state"] == "source_held"
        assert len({unit["unit_id"] for unit in manifest["pipeline"]["units"]}) == len(manifest["pipeline"]["units"])
    else:
        assert failures == [] and manifest["pipeline"]["status"] == "complete"


def test_invalid_pyramid_rollback_stops_the_pipeline_globally(tmp_path, monkeypatch):
    with pytest.raises(refresh.GlobalStageError, match="rollback evidence changed"):
        run_schedule(tmp_path, monkeypatch, 2, sources=("pyramid-brokerage", "hanley"),
                     pyramid_guard_refusal=True, pyramid_guard_valid=False)


def test_pyramid_only_typed_hold_is_partial_without_verified_members(tmp_path, monkeypatch):
    manifest, _, failures = run_schedule(tmp_path, monkeypatch, 1,
        sources=("pyramid-brokerage",), pyramid_guard_refusal=True)
    assert failures == ["pyramid-brokerage"]
    assert manifest["pipeline"]["status"] == "partial"
    assert manifest["pipeline"]["counts"]["verified"] == 0
    assert manifest["pipeline"]["source_results"]["pyramid-brokerage"]["state"] == "failed"
    assert manifest["pipeline"]["units"][0]["state"] == "source_held"


@pytest.mark.parametrize("valid", [True, False])
def test_transwestern_singleton_hold_continues_peers_only_when_bound(tmp_path, monkeypatch, valid):
    kwargs = dict(sources=("savills", "transwestern", "hanley"),
                  transwestern_guard_refusal=True, pyramid_guard_valid=valid)
    if not valid:
        with pytest.raises(refresh.GlobalStageError, match="rollback evidence changed"):
            run_schedule(tmp_path, monkeypatch, 2, **kwargs)
        return
    manifest, events, failures = run_schedule(tmp_path, monkeypatch, 2, **kwargs)
    assert failures == ["transwestern"]
    assert manifest["pipeline"]["status"] == "partial"
    assert manifest["pipeline"]["source_results"]["savills"]["state"] == "verified"
    assert manifest["pipeline"]["source_results"]["hanley"]["state"] == "verified"
    assert [event[1] for event in events if event[0] == "db_start" and "transwestern" in event[1]] == [("transwestern",)]


def test_jll_rolled_back_image_guard_keeps_other_sources_moving(tmp_path, monkeypatch):
    manifest, events, failures = run_schedule(
        tmp_path, monkeypatch, 2, sources=("jll", "svn", "savills"),
        jll_guard_refusal=True,
    )
    assert failures == ["jll"]
    assert manifest["pipeline"]["source_results"]["jll"]["state"] == "failed"
    assert manifest["pipeline"]["source_results"]["svn"]["state"] == "verified"
    assert manifest["pipeline"]["source_results"]["savills"]["state"] == "verified"
    assert next(e for e in events if e[0] == "db_start" and "jll" in e[1])[1] == ("jll",)


def test_jll_unverified_rollback_stops_pipeline(tmp_path, monkeypatch):
    with pytest.raises(refresh.GlobalStageError, match="rollback evidence"):
        run_schedule(tmp_path, monkeypatch, 2, sources=("jll", "svn"),
                     jll_guard_refusal=True, pyramid_guard_valid=False)



def test_dispatch_order_keeps_configured_scope_and_runs_every_peer(tmp_path, monkeypatch):
    sources = ("savills", "transwestern", "nai-global")
    order = ("transwestern", "nai-global", "savills")
    manifest, events, failures = run_schedule(
        tmp_path, monkeypatch, 1, sources=sources, dispatch_sources=order)
    starts = [event[1] for event in events if event[0] == "start"]
    assert starts == list(order)
    assert manifest["config"]["sources"] == list(sources)
    assert set(manifest["pipeline"]["source_results"]) == set(sources)
    assert failures == []


@pytest.mark.parametrize("refuse", [False, True])
def test_normal_pipeline_calls_cursor_recovery_and_continues_valid_peer(tmp_path, monkeypatch, refuse):
    import recovery.colliers_cursor as recovery

    class Owner(refresh.SharedLock):
        def _require_authority(self):
            pass
    owner = object.__new__(Owner)
    calls = []
    def reconcile(actual_owner, **kwargs):
        calls.append(actual_owner)
        if refuse:
            raise recovery.SourceEvidenceRefusal("synthetic settled source-local refusal")
        polls = iter([None] * 12 + [0])
        pipeline.wait_with_poll_hook(SimpleNamespace(poll=lambda: next(polls, 0)))
        return None
    monkeypatch.setattr(recovery, "recover_prior_generation", reconcile)
    monkeypatch.setattr(refresh, "manifest_database_target_sha256", lambda _: "c" * 64)
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _: None)
    manifest, events, failures = run_schedule(tmp_path, monkeypatch, 2,
        sources=("colliers", "savills"), owner_check=owner._require_authority,
        colliers_partial="accepted", colliers_failure_nonzero=True)
    assert calls == [owner]
    assert any(event[:2] == ("start", "savills") for event in events)
    assert ("colliers" in failures) is refuse
    if refuse:
        assert not any(event[0] == "partial_worker" for event in events)
        assert manifest["pipeline"]["source_results"]["colliers"]["reason"] == "prior cursor recovery refused"
    else:
        assert any(event[:2] == ("start", "colliers") for event in events)
        assert next(i for i, event in enumerate(events) if event[:2] == ("published", "savills")) < next(
            i for i, event in enumerate(events) if event[0] == "partial_worker")


def test_quality_refusal_records_safe_reason_and_keeps_peer(tmp_path, monkeypatch):
    import recovery.colliers_cursor as recovery

    class Owner(refresh.SharedLock):
        def _require_authority(self):
            pass

    owner = object.__new__(Owner)
    def refuse(*args, **kwargs):
        raise recovery.SourceEvidenceRefusal("untrusted detail not recorded", code="quality_regression")
    monkeypatch.setattr(recovery, "recover_prior_generation", refuse)
    monkeypatch.setattr(refresh, "manifest_database_target_sha256", lambda _: "c" * 64)
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _: None)
    manifest, events, failures = run_schedule(tmp_path, monkeypatch, 2,
        sources=("colliers", "savills"), owner_check=owner._require_authority,
        colliers_partial="accepted", colliers_failure_nonzero=True)
    assert "colliers" in failures
    assert any(event[:2] == ("start", "savills") for event in events)
    assert manifest["pipeline"]["source_results"]["colliers"] == {
        "state": "failed", "reason": "prior cursor recovery refused", "refusal_code": "quality_regression"}
    assert "untrusted detail" not in json.dumps(manifest)


def test_unavailable_legacy_series_is_reported_and_fresh_window_continues(tmp_path, monkeypatch):
    import recovery.colliers_cursor as recovery

    class Owner(refresh.SharedLock):
        def _require_authority(self):
            pass

    owner = object.__new__(Owner)
    monkeypatch.setattr(recovery, "recover_prior_generation", lambda actual, **kwargs: {
        "history_unavailable": True, "generation": "2026-11-06T030006Z-123456789abc", "position": 1565})
    monkeypatch.setattr(refresh, "manifest_database_target_sha256", lambda _: "c" * 64)
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _: None)
    manifest, events, failures = run_schedule(tmp_path, monkeypatch, 2,
        sources=("colliers", "savills"), owner_check=owner._require_authority,
        colliers_partial="accepted", colliers_failure_nonzero=True)
    assert "colliers" not in failures
    assert any(event[0] == "partial_worker" for event in events)
    assert any(event[:2] == ("start", "savills") for event in events)
    assert manifest["sources"]["colliers"]["cursor_recovery"] == {
        "state": "prior_cursor_history_unavailable",
        "generation": "2026-11-06T030006Z-123456789abc", "position": 1565,
        "prior_window_qualified": False, "whole_source_coverage": False,
    }


def test_unavailable_legacy_series_does_not_bypass_target_change(tmp_path, monkeypatch):
    import recovery.colliers_cursor as recovery

    class Owner(refresh.SharedLock):
        def _require_authority(self):
            pass

    owner = object.__new__(Owner)
    monkeypatch.setattr(recovery, "recover_prior_generation", lambda actual, **kwargs: {
        "history_unavailable": True, "generation": "2026-11-06T030006Z-123456789abc", "position": 1565})
    monkeypatch.setattr(refresh, "manifest_database_target_sha256", lambda _: "c" * 64)
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _: {"algorithm": "sha256", "value": "f" * 64})
    with pytest.raises(refresh.GlobalStageError, match="target changed"):
        run_schedule(tmp_path, monkeypatch, 2, sources=("colliers", "savills"),
            owner_check=owner._require_authority, colliers_partial="accepted", colliers_failure_nonzero=True)


@pytest.mark.parametrize("failure", [KeyError("missing field"), FileNotFoundError("missing file")])
def test_malformed_prior_evidence_holds_colliers_but_continues_peer(tmp_path, monkeypatch, failure):
    import recovery.colliers_cursor as recovery

    class Owner(refresh.SharedLock):
        def _require_authority(self):
            pass

    owner = object.__new__(Owner)
    def malformed(*args):
        raise failure
    monkeypatch.setattr(recovery, "_qualify", malformed)
    monkeypatch.setattr(recovery, "_check_owner", lambda actual: actual._require_authority())
    monkeypatch.setattr(recovery, "recover_prior_generation",
        lambda actual, **kwargs: recovery.qualify(tmp_path, actual))
    monkeypatch.setattr(refresh, "manifest_database_target_sha256", lambda _: "c" * 64)
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _: None)
    manifest, events, failures = run_schedule(tmp_path, monkeypatch, 2,
        sources=("colliers", "savills"), owner_check=owner._require_authority,
        colliers_partial="accepted", colliers_failure_nonzero=True)
    assert "colliers" in failures
    assert any(event[:2] == ("start", "savills") for event in events)
    assert not any(event[0] == "partial_worker" for event in events)
    assert manifest["pipeline"]["source_results"]["colliers"]["reason"] == "prior cursor recovery refused"


def test_cursor_publication_uncertainty_stays_recorded_in_pipeline(tmp_path, monkeypatch):
    import recovery.colliers_cursor as recovery

    class Owner(refresh.SharedLock):
        def _require_authority(self):
            pass
    owner = object.__new__(Owner)
    def fail_after_arm(actual_owner, **kwargs):
        kwargs["before_publish"]()
        raise OSError("synthetic uncertain cursor publication")
    monkeypatch.setattr(recovery, "recover_prior_generation", fail_after_arm)
    monkeypatch.setattr(refresh, "manifest_database_target_sha256", lambda _: "c" * 64)
    with pytest.raises(OSError, match="uncertain cursor publication"):
        run_schedule(tmp_path, monkeypatch, 2, sources=("colliers", "savills"), owner_check=owner._require_authority,
            colliers_partial="accepted", colliers_failure_nonzero=True)
    manifest = json.loads((tmp_path / "2026-10-15T120000Z" / "manifest.json").read_text())
    assert manifest["pipeline"]["cursor_recovery_outcome_unknown"] is True


@pytest.mark.parametrize("valid", [True, False])
def test_settled_daum_ingest_failure_is_singleton_and_peer_continues(tmp_path, monkeypatch, valid):
    kwargs = dict(sources=("savills", "daum-commercial", "hanley"),
                  settled_ingest_failure="daum-commercial", atomic_rollback_valid=valid)
    if not valid:
        with pytest.raises(refresh.GlobalStageError, match="rollback evidence changed"):
            run_schedule(tmp_path, monkeypatch, 2, **kwargs)
        return
    manifest, events, failures = run_schedule(tmp_path, monkeypatch, 2, **kwargs)
    assert failures == ["daum-commercial"]
    assert manifest["pipeline"]["status"] == "partial"
    assert manifest["pipeline"]["source_results"]["savills"]["state"] == "verified"
    assert manifest["pipeline"]["source_results"]["hanley"]["state"] == "verified"
    assert manifest["pipeline"]["source_results"]["daum-commercial"]["reason"] == "settled_ingest_failure"
    assert [event[1] for event in events if event[0] == "db_start" and "daum-commercial" in event[1]] == [("daum-commercial",)]
    prior = next(i for i, event in enumerate(events) if event[0] == "db_end" and "savills" in event[1])
    failed = next(i for i, event in enumerate(events) if event[0] == "db_start" and "daum-commercial" in event[1])
    later = next(i for i, event in enumerate(events) if event[0] == "db_end" and "hanley" in event[1])
    assert prior < failed < later


@pytest.mark.parametrize("valid", [True, False])
def test_non_daum_atomic_failure_keeps_prior_and_later_peers_independent(tmp_path, monkeypatch, valid):
    kwargs = dict(sources=("transwestern", "savills", "hanley"),
                  settled_ingest_failure="savills", atomic_rollback_valid=valid)
    if not valid:
        with pytest.raises(refresh.GlobalStageError, match="rollback evidence changed"):
            run_schedule(tmp_path, monkeypatch, 2, **kwargs)
        return
    manifest, events, failures = run_schedule(tmp_path, monkeypatch, 2, **kwargs)
    assert failures == ["savills"]
    assert manifest["pipeline"]["status"] == "partial"
    assert manifest["pipeline"]["source_results"]["transwestern"]["state"] == "verified"
    assert manifest["pipeline"]["source_results"]["hanley"]["state"] == "verified"
    assert manifest["pipeline"]["source_results"]["savills"]["reason"] == "settled_ingest_failure"
    writes = [event[1] for event in events if event[0] == "db_start"]
    assert all(len(sources) == 1 for sources in writes)
    assert writes.index(("transwestern",)) < writes.index(("savills",)) < writes.index(("hanley",))
