"""Typed artifact refusals end automatic collection; process retries stay bounded."""

import hashlib
import json
import os
import stat
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


def setup_collection(
    tmp_path,
    monkeypatch,
    *,
    invalid=None,
    process_failure=False,
    write_failed_artifact=False,
    rejected_bytes=None,
):
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
    original_archive_rejected = refresh._archive_rejected

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
        code = (
            "import pathlib,sys,time; time.sleep(0.02); "
            "path=pathlib.Path(sys.argv[1]); path.write_text(sys.argv[2]); "
            "path.chmod(0o644); sys.exit(int(sys.argv[3]))"
        )
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
        if (
            process_failure
            and starts[source] == 1
            and not write_failed_artifact
        ):
            return 1
        from pathlib import Path

        Path(output).write_text(json.dumps(payload(source)))
        Path(output).chmod(0o644)
        if process_failure and starts[source] == 1:
            return 1
        return 0

    def archive_rejected(tmp_artifact, run_dir, source, attempt_number):
        if rejected_bytes is not None and tmp_artifact.exists():
            rejected_bytes[(source, attempt_number)] = tmp_artifact.read_bytes()
        return original_archive_rejected(
            tmp_artifact, run_dir, source, attempt_number
        )

    def db(run_dir, owner, unit, env_file, previous):
        database.extend(unit["sources"])
        for source in unit["sources"]:
            owner["sources"][source]["readback"] = {"ok": True}
        unit.update(validation_path="synthetic-readback", validation_sha256="c" * 64)

    monkeypatch.setattr(refresh, "_start_cohort_collection", start)
    monkeypatch.setattr(refresh, "_finalize_cohort_collection", finalize)
    monkeypatch.setattr(refresh, "run_command", command)
    if rejected_bytes is not None:
        monkeypatch.setattr(refresh, "_archive_rejected", archive_rejected)
    monkeypatch.setattr(pipeline, "require_pipeline_owner", lambda *args: None)
    monkeypatch.setattr(pipeline, "preseal_source_gate", lambda *args: True)
    monkeypatch.setattr(pipeline, "run_database_unit", db)
    monkeypatch.setattr(refresh, "advance_source", lambda *args, **kwargs: True)
    return root, manifest, sources, starts, database


@pytest.mark.parametrize(
    ("route", "failure", "expected_state", "expected_rc"),
    [
        ("serial", "nonzero", "collect_failed", 1),
        ("cohort", "nonzero", "collect_failed", 1),
        ("serial", "validation", "artifact_rejected", 0),
        ("cohort", "validation", "artifact_rejected", 0),
    ],
)
def test_rejected_artifact_is_private_and_hash_bound_on_public_failure_paths(
    tmp_path,
    monkeypatch,
    route,
    failure,
    expected_state,
    expected_rc,
):
    source_bytes = {}
    root, manifest, sources, starts, database = setup_collection(
        tmp_path,
        monkeypatch,
        invalid="wrong_mode" if failure == "validation" else None,
        process_failure=failure == "nonzero",
        write_failed_artifact=True,
        rejected_bytes=source_bytes,
    )
    kwargs = dict(page_cap=400, concurrency=1, attempts_this_run=1)
    if route == "serial":
        result = refresh.collect_source(
            root,
            manifest,
            sources[0],
            transactions=("sale", "lease"),
            **kwargs,
        )
        assert result is None
    else:
        failures = refresh.prepare_sources_cohort(
            root,
            manifest,
            sources,
            **kwargs,
            env_file=None,
            source_workers=2,
        )
        assert failures == [sources[0]]

    checkpoint = manifest["sources"][sources[0]]
    assert checkpoint["state"] == expected_state
    assert sources[0] not in database
    assert starts[sources[0]] == 1
    attempt = checkpoint["attempts"][0]
    assert attempt["rc"] == expected_rc
    assert attempt["error"]
    relative_path = attempt["rejected_artifact"]
    assert relative_path == f"rejected/{sources[0]}-attempt-1.json"
    rejected_path = root / relative_path
    original_bytes = source_bytes[(sources[0], 1)]
    retained_bytes = rejected_path.read_bytes()
    assert retained_bytes == original_bytes
    assert stat.S_IMODE(rejected_path.stat().st_mode) == 0o600
    assert attempt["rejected_artifact_sha256"] == refresh.sha256_file(rejected_path)
    assert attempt["rejected_artifact_sha256"] == hashlib.sha256(
        original_bytes
    ).hexdigest()


def test_nonzero_exit_without_temp_artifact_records_no_hash(
    tmp_path, monkeypatch
):
    root, manifest, sources, starts, database = setup_collection(
        tmp_path,
        monkeypatch,
        process_failure=True,
    )
    result = refresh.collect_source(
        root,
        manifest,
        sources[0],
        transactions=("sale", "lease"),
        page_cap=400,
        concurrency=1,
        attempts_this_run=1,
    )
    assert result is None
    checkpoint = manifest["sources"][sources[0]]
    assert checkpoint["state"] == "collect_failed"
    assert sources[0] not in database
    attempt = checkpoint["attempts"][0]
    assert attempt["rc"] == 1
    assert attempt["error"] == "collector exited 1"
    assert attempt["rejected_artifact"] is None
    assert attempt.get("rejected_artifact_sha256") is None


@pytest.mark.skipif(
    os.name != "posix", reason="artifact guard uses POSIX no-follow semantics"
)
@pytest.mark.parametrize(
    "unsafe_kind", ["symlink", "hardlink", "nonregular", "unowned"]
)
def test_private_artifact_guard_refuses_unsafe_inputs_without_changing_them(
    tmp_path, monkeypatch, unsafe_kind
):
    artifact = tmp_path / "candidate.json"
    run_dir = tmp_path / "run"
    rejected = run_dir / "rejected" / "test-source-attempt-1.json"
    target = tmp_path / "target.json"
    target.write_bytes(b"immutable fixture")
    target.chmod(0o644)
    artifact.write_bytes(b"immutable fixture")
    artifact.chmod(0o644)
    before_mode = stat.S_IMODE(artifact.stat().st_mode)

    if unsafe_kind == "symlink":
        artifact.unlink()
        artifact.symlink_to(target)
        expected_error = "artifact private publication refused"
    elif unsafe_kind == "hardlink":
        alias = tmp_path / "alias.json"
        os.link(artifact, alias)
        expected_error = "owned, singly linked regular file"
    elif unsafe_kind == "nonregular":
        artifact.unlink()
        artifact.mkdir(mode=0o755)
        before_mode = stat.S_IMODE(artifact.stat().st_mode)
        expected_error = "owned, singly linked regular file"
    else:
        actual_uid = os.getuid()
        monkeypatch.setattr(refresh.os, "getuid", lambda: actual_uid + 1)
        expected_error = "owned, singly linked regular file"

    with pytest.raises(refresh.ArtifactValidationError, match=expected_error):
        refresh._archive_rejected(artifact, run_dir, "test-source", 1)

    assert artifact.exists() or artifact.is_symlink()
    assert not rejected.exists()

    if unsafe_kind == "symlink":
        assert artifact.is_symlink()
        assert target.read_bytes() == b"immutable fixture"
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
    elif unsafe_kind == "hardlink":
        assert artifact.read_bytes() == b"immutable fixture"
        assert alias.read_bytes() == b"immutable fixture"
        assert stat.S_IMODE(artifact.stat().st_mode) == before_mode == 0o644
    elif unsafe_kind == "nonregular":
        assert artifact.is_dir()
        assert stat.S_IMODE(artifact.stat().st_mode) == before_mode
    else:
        assert artifact.read_bytes() == b"immutable fixture"
        assert stat.S_IMODE(artifact.stat().st_mode) == before_mode == 0o644


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
