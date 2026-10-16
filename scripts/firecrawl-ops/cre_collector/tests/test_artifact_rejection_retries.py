"""Typed artifact refusals end automatic collection; process retries stay bounded."""

import json
import subprocess
import sys
from collections import Counter

import pytest

import cre_checkpoint_refresh as refresh
import cre_refresh_pipeline as pipeline
from cre_owned_processes import OwnedProcessGroup
from test_cre_checkpoint_refresh import strict_artifact


@pytest.fixture(autouse=True)
def clear_guard_state():
    refresh._clear_cpu_guard_trip()
    yield
    refresh._clear_cpu_guard_trip()


def setup_collection(tmp_path, monkeypatch, *, invalid=None, process_failure=False):
    """Real local owned collectors and artifact compute; only provider/DB IO replaced."""
    sources = ("transwestern", "savills")
    root = tmp_path / "2026-10-16T040000Z"
    root.mkdir()
    manifest = refresh.new_manifest(
        root,
        git_sha="a" * 40,
        git_dirty=False,
        sources=sources,
        page_cap=400,
        concurrency=1,
        source_workers=2,
    )
    starts, alive, database = Counter(), {}, []
    original_command = refresh.run_command
    original_finalize = refresh._finalize_cohort_collection

    def payload(source):
        stamp = refresh.utc_now()
        data = strict_artifact(
            source,
            detail_scope="detail_page",
            generation=manifest["run_id"],
            generation_started_at=manifest["started_at"],
        )
        data["runMeta"].update(startedAt=stamp, finishedAt=stamp)
        for row in data["listings"]:
            row.update(inventoryObservedAt=stamp, detailObservedAt=stamp)
        if source == sources[0]:
            if invalid == "source_error":
                data["sources"][0]["error"] = (
                    "provider request failed after inner retries"
                )
            elif invalid == "wrong_mode":
                data["runMeta"]["mode"] = "monitor"
        return data

    def start(run_dir, owner, source, **kwargs):
        assert source not in alive and len(alive) < 2
        assert all(
            not refresh.source_worker_lanes(source) & refresh.source_worker_lanes(other)
            for other in alive
        )
        created = refresh._cohort_attempt(
            run_dir, owner, source, collector_concurrency=1
        )
        attempt, temporary, log_path, started = created
        starts[source] += 1
        rc = (
            refresh.COHORT_SETTLED_FAILURE_EXIT
            if process_failure and source == sources[0] and starts[source] == 1
            else 0
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("a")
        code = "import pathlib,sys,time; time.sleep(0.02); pathlib.Path(sys.argv[1]).write_text(sys.argv[2]); sys.exit(int(sys.argv[3]))"
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                code,
                str(temporary),
                json.dumps(payload(source)),
                str(rc),
            ],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        item = refresh.CohortCollectionProcess(
            source,
            process,
            log,
            temporary,
            attempt,
            started,
            OwnedProcessGroup.from_spawn(process),
        )
        alive[source] = item
        return item

    def finalize(run_dir, owner, item, **kwargs):
        result = original_finalize(run_dir, owner, item, **kwargs)
        del alive[item.source]
        return result

    def command(argv, log, **kwargs):
        if "collect.ts" not in argv:
            return original_command(argv, log, **kwargs)
        source = next(
            value.split("=", 1)[1] for value in argv if value.startswith("--source=")
        )
        output = next(
            value.split("=", 1)[1] for value in argv if value.startswith("--out=")
        )
        starts[source] += 1
        if process_failure and starts[source] == 1:
            return 1
        from pathlib import Path

        Path(output).write_text(json.dumps(payload(source)))
        return 0

    def db(run_dir, owner, unit, env_file, previous):
        database.extend(unit["sources"])
        for source in unit["sources"]:
            owner["sources"][source]["readback"] = {"ok": True}
        unit.update(validation_path="synthetic-readback", validation_sha256="c" * 64)

    monkeypatch.setattr(refresh, "_start_cohort_collection", start)
    monkeypatch.setattr(refresh, "_finalize_cohort_collection", finalize)
    monkeypatch.setattr(refresh, "run_command", command)
    monkeypatch.setattr(pipeline, "require_pipeline_owner", lambda *args: None)
    monkeypatch.setattr(pipeline, "run_database_unit", db)
    monkeypatch.setattr(refresh, "advance_source", lambda *args, **kwargs: True)
    return root, manifest, sources, starts, database


@pytest.mark.parametrize("invalid", ["source_error", "wrong_mode"])
@pytest.mark.parametrize("route", ["pipeline", "serial", "cohort"])
def test_typed_rejection_does_not_spend_remaining_collection_budget(
    tmp_path,
    monkeypatch,
    invalid,
    route,
):
    root, manifest, sources, starts, database = setup_collection(
        tmp_path,
        monkeypatch,
        invalid=invalid,
    )
    kwargs = dict(page_cap=400, concurrency=1, attempts_this_run=3)
    if route == "pipeline":
        failures = pipeline.run_bounded_pipeline(
            root,
            manifest,
            sources,
            **kwargs,
            env_file=None,
            source_workers=2,
        )
        assert failures == [sources[0]]
        assert database == [sources[1]]
        assert manifest["pipeline"]["source_results"][sources[1]]["state"] == "verified"
        assert manifest["sources"][sources[0]]["pipeline_stage"] == "failed"
    elif route == "serial":
        assert (
            refresh.collect_source(
                root,
                manifest,
                sources[0],
                transactions=("sale", "lease"),
                **kwargs,
            )
            is None
        )
    else:
        assert refresh.prepare_sources_cohort(
            root,
            manifest,
            sources,
            **kwargs,
            env_file=None,
            source_workers=2,
        ) == [sources[0]]
    assert starts[sources[0]] == 1
    rejected = manifest["sources"][sources[0]]
    assert rejected["state"] == "artifact_rejected"
    assert len(rejected["attempts"]) == 1
    assert rejected["attempts"][0]["error"]
    assert (root / rejected["attempts"][0]["rejected_artifact"]).is_file()
    assert sources[0] not in database


@pytest.mark.parametrize("route", ["pipeline", "serial", "cohort"])
def test_settled_nonzero_collection_can_retry_then_succeed(
    tmp_path, monkeypatch, route
):
    root, manifest, sources, starts, database = setup_collection(
        tmp_path,
        monkeypatch,
        process_failure=True,
    )
    kwargs = dict(page_cap=400, concurrency=1, attempts_this_run=3)
    if route == "pipeline":
        assert (
            pipeline.run_bounded_pipeline(
                root,
                manifest,
                sources,
                **kwargs,
                env_file=None,
                source_workers=2,
            )
            == []
        )
        assert sorted(database) == sorted(sources)
    elif route == "serial":
        assert (
            refresh.collect_source(
                root,
                manifest,
                sources[0],
                transactions=("sale", "lease"),
                **kwargs,
            )
            is not None
        )
    else:
        assert (
            refresh.prepare_sources_cohort(
                root,
                manifest,
                sources,
                **kwargs,
                env_file=None,
                source_workers=2,
            )
            == []
        )
    assert starts[sources[0]] == 2


def test_new_collection_attempt_clears_stale_validation_stage(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    manifest = refresh.new_manifest(
        root,
        git_sha="a" * 40,
        git_dirty=False,
        sources=("savills",),
        page_cap=400,
        concurrency=1,
    )
    source = manifest["sources"]["savills"]
    source.update(state="collect_failed", pipeline_stage="validating")
    refresh._cohort_attempt(root, manifest, "savills", collector_concurrency=1)
    saved = json.loads((root / "manifest.json").read_text())["sources"]["savills"]
    assert saved["state"] == "collecting" and "pipeline_stage" not in saved


@pytest.mark.parametrize(
    "fault", [refresh.GlobalStageError, refresh.ProcessCleanupUnknown]
)
def test_global_compute_fault_does_not_retry_collection(tmp_path, monkeypatch, fault):
    root, manifest, sources, starts, database = setup_collection(tmp_path, monkeypatch)

    def fail(*args):
        raise fault("synthetic global compute failure")

    monkeypatch.setattr(pipeline, "prepare_candidate", fail)
    with pytest.raises(fault, match="synthetic global"):
        pipeline.run_bounded_pipeline(
            root,
            manifest,
            sources,
            page_cap=400,
            concurrency=1,
            attempts_this_run=3,
            env_file=None,
            source_workers=2,
        )
    assert starts[sources[0]] == 1 and not database
