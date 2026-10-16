"""Offline pure compute-worker and dispatch-budget contracts."""

import json
from datetime import datetime, timezone

import pytest

import cre_checkpoint_refresh as refresh
from test_cre_checkpoint_refresh import ATTEMPT, artifact


@pytest.fixture(autouse=True)
def isolated_resource_trip_state():
    """Existing fault suites may leave a synthetic sticky watchdog trip."""
    refresh._clear_cpu_guard_trip()
    try:
        yield
    finally:
        refresh._clear_cpu_guard_trip()


def test_pipeline_compute_validates_and_hashes_without_parent_decode(
    tmp_path, monkeypatch
):
    from cre_refresh_pipeline import command_poll_hook

    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(artifact("transwestern")))
    expected = refresh.sha256_file(path)
    ticks = []
    real_load = refresh._load_json

    def no_payload_decode(target):
        assert target != path, "parent decoded source payload"
        return real_load(target)

    monkeypatch.setattr(refresh, "_load_json", no_payload_decode)
    with command_poll_hook(lambda: ticks.append(1)):
        stats = refresh.validate_source_artifact(
            path,
            "transwestern",
            ATTEMPT,
            now=datetime(2026, 7, 29, 12, 2, tzinfo=timezone.utc),
        )
        assert refresh.sha256_file(path) == expected
    assert ticks and stats["sha256"] == expected
    assert stats["staged_unique"] == 2


def test_pipeline_compute_rejects_wrong_source(tmp_path):
    from cre_refresh_pipeline import command_poll_hook

    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(artifact("transwestern")))
    with (
        command_poll_hook(lambda: None),
        pytest.raises(refresh.ArtifactValidationError),
    ):
        refresh.validate_source_artifact(path, "savills", ATTEMPT)


def test_pipeline_compute_response_is_bounded_and_request_bound(tmp_path, monkeypatch):
    from cre_refresh_pipeline import command_poll_hook, run_artifact_compute

    def forge(argv, log, **kwargs):
        response = __import__("pathlib").Path(argv[argv.index("--response") + 1])
        response.write_text(json.dumps({"request_id": "foreign", "ok": True}))
        return 0

    monkeypatch.setattr(refresh, "run_command", forge)
    with command_poll_hook(lambda: None), pytest.raises(refresh.GlobalStageError):
        run_artifact_compute(tmp_path, "hash", {"path": str(tmp_path / "unused")})


@pytest.mark.parametrize("workers", [1, 2, 3, 4])
def test_pipeline_resolves_one_portable_worker_setting(workers):
    from cre_refresh_pipeline import resolve_source_workers

    assert resolve_source_workers(True, workers) == workers
    assert resolve_source_workers(False, workers) == workers
    assert resolve_source_workers(True, None) == 2
    assert resolve_source_workers(False, None) == 1
    with pytest.raises(ValueError):
        resolve_source_workers(True, 5)


def test_pipeline_budget_counts_inflight_and_reserves_disk():
    from cre_refresh_pipeline import PipelineBudget

    budget = PipelineBudget()
    gib = 1024**3
    assert budget.can_dispatch(
        pending_count=4, observed_bytes=0, active_sizes=[0], free_bytes=2 * gib
    )
    assert not budget.can_dispatch(
        pending_count=5, observed_bytes=0, active_sizes=[], free_bytes=2 * gib
    )
    assert not budget.can_dispatch(
        pending_count=1,
        observed_bytes=513 * 1024**2,
        active_sizes=[],
        free_bytes=2 * gib,
    )
    assert not budget.can_dispatch(
        pending_count=0, observed_bytes=0, active_sizes=[], free_bytes=gib
    )
    # Already emitted bytes consume real disk; only unused active reservations
    # and the new output reservation are added to required free space.
    assert budget.can_dispatch(
        pending_count=1,
        observed_bytes=128 * 1024**2,
        active_sizes=[128 * 1024**2],
        free_bytes=gib + 65 * 1024**2,
    )


def test_pipeline_disk_accounting_charges_only_unconsumed_source_outputs(tmp_path):
    from cre_refresh_pipeline import pending_disk_bytes

    paths = {
        "sources/ready.json": 9,
        "sources/source.tmp": 11,
        "sources/retry-leftover.tmp": 19,
        "rejected/retry.json": 13,
        "cache/source/page.json": 17,
    }
    for rel, size in paths.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    excluded = {tmp_path / "sources/ready.json"}
    assert pending_disk_bytes(tmp_path, excluded) == 30
    assert pending_disk_bytes(tmp_path, set()) == 39
    assert all(
        (tmp_path / relative).stat().st_size == size for relative, size in paths.items()
    )


def test_staging_transaction_statistics_use_merged_canonical_types_and_bind_digest(
    tmp_path,
):
    from test_cre_checkpoint_refresh import strict_artifact

    payload = strict_artifact("svn")
    # Same canonical ID offered for sale and lease merges once as both.
    payload["listings"][1]["id"] = payload["listings"][0]["id"]
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(payload))
    stats = refresh.validate_source_artifact(path, "svn", ATTEMPT)
    assert stats["staged_unique"] == 1
    assert stats["transaction_breakdown"] == {
        "sale": 1,
        "lease": 1,
        "both": 1,
        "measurement": "current_staged_inventory_transaction_types",
        "source_key": "svn",
        "artifact_sha256": refresh.sha256_file(path),
        "artifact_bytes": path.stat().st_size,
        "generation_id": "refresh-generation-1",
    }


@pytest.mark.parametrize("candidate_only", [False, True])
def test_settled_publication_makes_raw_0644_artifact_private_before_proof(
    tmp_path, monkeypatch, candidate_only
):
    import io
    from types import SimpleNamespace

    root = tmp_path / "2026-10-15T120000Z"
    root.mkdir()
    manifest = refresh.new_manifest(
        root,
        git_sha="a" * 40,
        git_dirty=False,
        sources=("transwestern",),
        page_cap=400,
        concurrency=1,
    )
    attempt, path, _, started = refresh._cohort_attempt(
        root, manifest, "transwestern", collector_concurrency=1
    )
    path.write_text("{}")
    path.chmod(0o644)
    assert path.stat().st_mode & 0o777 == 0o644
    item = refresh.CohortCollectionProcess(
        "transwestern",
        SimpleNamespace(poll=lambda: 0, returncode=0),
        io.StringIO(),
        path,
        attempt,
        started,
        object(),
    )
    monkeypatch.setattr(
        refresh, "_require_completed_group_settlement", lambda group: None
    )

    def inspect(artifact_path, *args, **kwargs):
        assert artifact_path.stat().st_mode & 0o777 == 0o600
        return {"staged_unique": 1}

    monkeypatch.setattr(refresh, "validate_source_artifact", inspect)
    assert refresh._finalize_cohort_collection(
        root, manifest, item, candidate_only=candidate_only
    )
    assert (root / "sources" / "transwestern.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("alias", ["symlink", "hardlink", "fifo"])
def test_private_publication_refuses_aliases_without_chmod_foreign_file(
    tmp_path, alias
):
    import os

    foreign = tmp_path / "foreign.json"
    foreign.write_text("unrelated")
    foreign.chmod(0o644)
    candidate = tmp_path / "candidate.json"
    if alias == "symlink":
        candidate.symlink_to(foreign)
    elif alias == "hardlink":
        os.link(foreign, candidate)
    else:
        os.mkfifo(candidate)
    with pytest.raises(refresh.ArtifactValidationError):
        refresh.make_private_artifact(candidate)
    assert foreign.stat().st_mode & 0o777 == 0o644
    assert foreign.read_text() == "unrelated"


def test_pending_output_scan_never_walks_auxiliary_or_symlink_directories(
    tmp_path, monkeypatch
):
    import cre_refresh_pipeline as pipeline

    for relative in (
        "sources/nested/ready.json",
        "cache/page.json",
        "rejected/old.json",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"retained")
    (tmp_path / "sources/cache-alias").symlink_to(
        tmp_path / "cache", target_is_directory=True
    )
    walk = pipeline.os.walk
    visited = []

    def observed_walk(root, **kwargs):
        assert root == tmp_path / "sources"
        assert kwargs == {"followlinks": False}
        for result in walk(root, **kwargs):
            visited.append(result[0])
            yield result

    monkeypatch.setattr(pipeline.os, "walk", observed_walk)
    ready = tmp_path / "sources/nested/ready.json"
    assert pipeline.pending_disk_bytes(tmp_path, set()) == len(b"retained")
    assert pipeline.pending_disk_bytes(tmp_path, {ready}) == 0
    assert all("cache-alias" not in str(path) for path in visited)
    assert (tmp_path / "cache/page.json").read_bytes() == b"retained"
    assert (tmp_path / "rejected/old.json").read_bytes() == b"retained"
