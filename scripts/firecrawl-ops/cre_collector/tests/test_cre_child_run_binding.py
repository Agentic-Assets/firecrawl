"""No-network contracts for exact first-attempt child-run binding."""

from __future__ import annotations

import json

import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series
import pytest

GIT_SHA = "a" * 40
FRESH_RUN_ID = "2026-09-13T120000Z-0123456789ab"


def _config() -> dict[str, object]:
    return series.series_config(
        sources=("cbre",),
        page_cap=400,
        concurrency=3,
        attempts_per_source=3,
        max_resume_age_hours=24.0,
        max_host_cpu_percent=80.0,
        cpu_sustain_seconds=30.0,
        cpu_sample_seconds=5.0,
        nice=10,
    )


def _child_manifest(run_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "collector_git_sha": GIT_SHA,
        "config": series._expected_child_config("cbre", _config()),
        "status": series.SUCCESS_STATUS,
        "error": None,
    }


@pytest.mark.parametrize(
    "run_id",
    [
        "2026-09-13T120000Z",
        FRESH_RUN_ID,
    ],
)
def test_validate_run_id_accepts_canonical_generations(run_id):
    assert refresh.validate_run_id(run_id) == run_id


def test_parent_run_id_allocation_adds_a_valid_random_suffix(monkeypatch):
    monkeypatch.setattr(refresh, "_run_id", lambda: "2026-09-13T120000Z")

    allocated = {refresh.allocate_run_id() for _ in range(8)}

    assert len(allocated) == 8
    assert all(refresh.validate_run_id(run_id) == run_id for run_id in allocated)
    assert all(run_id.startswith("2026-09-13T120000Z-") for run_id in allocated)


@pytest.mark.parametrize(
    "run_id",
    [
        "../../escape",
        "/absolute",
        "2026-09-13T120000Z-ABCDEF012345",
        "2026-09-13T120000Z-short",
        "2026-02-30T120000Z",
        "2026-09-13T120000Z\x00-hidden",
        " 2026-09-13T120000Z",
    ],
)
def test_validate_run_id_rejects_noncanonical_or_unsafe_values(run_id):
    with pytest.raises(ValueError, match="run-id"):
        refresh.validate_run_id(run_id)


def test_fresh_run_directory_creation_is_atomic_and_collision_safe(tmp_path):
    created = refresh.create_fresh_run_dir(tmp_path, run_id=FRESH_RUN_ID)

    assert created == tmp_path / FRESH_RUN_ID
    assert created.is_dir()
    with pytest.raises(refresh.RefreshError, match="already exists"):
        refresh.create_fresh_run_dir(tmp_path, run_id=FRESH_RUN_ID)


def test_omitted_run_id_preserves_standalone_timestamp_shape(tmp_path, monkeypatch):
    standalone_run_id = "2026-09-13T120000Z"
    monkeypatch.setattr(refresh, "_run_id", lambda: standalone_run_id)

    created = refresh.create_fresh_run_dir(tmp_path)

    assert created == tmp_path / standalone_run_id


@pytest.mark.parametrize("collision_kind", ["file", "symlink"])
def test_fresh_run_directory_refuses_non_directory_collisions(tmp_path, collision_kind):
    collision = tmp_path / FRESH_RUN_ID
    if collision_kind == "file":
        collision.write_text("occupied", encoding="utf-8")
    else:
        target = tmp_path / "target"
        target.mkdir()
        collision.symlink_to(target, target_is_directory=True)

    with pytest.raises(refresh.RefreshError, match="already exists"):
        refresh.create_fresh_run_dir(tmp_path, run_id=FRESH_RUN_ID)


@pytest.mark.parametrize(
    "argv",
    [
        ["--run-id", FRESH_RUN_ID, "--resume", "/tmp/existing"],
        ["--run-id", FRESH_RUN_ID, "--_cohort-collect-source", "cbre"],
    ],
)
def test_fresh_cli_run_id_rejects_resume_and_internal_worker(argv, capsys):
    with pytest.raises(SystemExit, match="2"):
        refresh.main(argv)

    assert "--run-id cannot be combined" in capsys.readouterr().err


def test_build_checkpoint_argv_binds_fresh_run_id(tmp_path):
    argv = series.build_checkpoint_argv(
        "cbre",
        child_out_root=tmp_path / "runs",
        env_file=None,
        config=_config(),
        fresh_run_id=FRESH_RUN_ID,
    )

    assert argv[argv.index("--run-id") + 1] == FRESH_RUN_ID
    assert "--resume" not in argv
    with pytest.raises(series.SeriesError, match="cannot be combined"):
        series.build_checkpoint_argv(
            "cbre",
            child_out_root=tmp_path / "runs",
            env_file=None,
            config=_config(),
            resume_run=tmp_path / "existing",
            fresh_run_id=FRESH_RUN_ID,
        )


def test_first_attempt_is_bound_before_popen_and_loaded_by_exact_path(
    tmp_path, monkeypatch
):
    manifest = series.new_manifest(tmp_path, git_sha=GIT_SHA, config=_config())
    monkeypatch.setattr(series, "allocate_run_id", lambda: FRESH_RUN_ID)

    class FakeProcess:
        pid = 4321

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def fake_popen(argv, **_kwargs):
        persisted = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
        checkpoint = persisted["sources"]["cbre"]
        assert checkpoint["state"] == "running"
        assert checkpoint["checkpoint_run"] == f"runs/{FRESH_RUN_ID}"
        assert argv[argv.index("--run-id") + 1] == FRESH_RUN_ID

        bound = tmp_path / checkpoint["checkpoint_run"]
        bound.mkdir(parents=True)
        (bound / "manifest.json").write_text(
            json.dumps(_child_manifest(FRESH_RUN_ID)), encoding="utf-8"
        )
        decoy = tmp_path / "runs" / "2026-09-13T120001Z-deadbeefcafe"
        decoy.mkdir()
        (decoy / "manifest.json").write_text(
            json.dumps(_child_manifest(decoy.name)), encoding="utf-8"
        )
        return FakeProcess()

    monkeypatch.setattr(series.subprocess, "Popen", fake_popen)

    assert series.run_series(tmp_path, manifest, env_file=None, retry_failed=False) == 0
    checkpoint = manifest["sources"]["cbre"]
    assert checkpoint["state"] == "complete"
    assert checkpoint["checkpoint_run"] == f"runs/{FRESH_RUN_ID}"


def test_bound_child_identity_mismatch_fails_closed(tmp_path, monkeypatch):
    manifest = series.new_manifest(tmp_path, git_sha=GIT_SHA, config=_config())
    monkeypatch.setattr(series, "allocate_run_id", lambda: FRESH_RUN_ID)

    class FakeProcess:
        pid = 4321

        def __init__(self, *_args, **_kwargs):
            bound = tmp_path / "runs" / FRESH_RUN_ID
            bound.mkdir(parents=True)
            (bound / "manifest.json").write_text(
                json.dumps(_child_manifest("2026-09-13T120002Z-badbadbadbad")),
                encoding="utf-8",
            )

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(series.subprocess, "Popen", FakeProcess)

    assert series.run_series(tmp_path, manifest, env_file=None, retry_failed=False) == 1
    assert manifest["sources"]["cbre"]["state"] == "failed_global"
    assert "identity mismatch" in manifest["sources"]["cbre"]["error"]


def test_spawn_failure_preserves_explicit_reserved_child(tmp_path, monkeypatch):
    manifest = series.new_manifest(tmp_path, git_sha=GIT_SHA, config=_config())
    monkeypatch.setattr(series, "allocate_run_id", lambda: FRESH_RUN_ID)

    def fail_spawn(*_args, **_kwargs):
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(series.subprocess, "Popen", fail_spawn)

    assert series.run_series(tmp_path, manifest, env_file=None, retry_failed=False) == 1
    checkpoint = manifest["sources"]["cbre"]
    assert checkpoint["checkpoint_run"] == f"runs/{FRESH_RUN_ID}"
    assert checkpoint["state"] == "failed_global"
    assert checkpoint["attempts"][0]["rc"] is None
    assert manifest["status"] == "failed"


def test_cancellation_preserves_reserved_child_and_interrupts(tmp_path, monkeypatch):
    manifest = series.new_manifest(tmp_path, git_sha=GIT_SHA, config=_config())
    monkeypatch.setattr(series, "allocate_run_id", lambda: FRESH_RUN_ID)
    terminated = []

    class FakeProcess:
        pid = 4321

        def wait(self, timeout=None):
            raise KeyboardInterrupt

        def poll(self):
            return None

    monkeypatch.setattr(
        series.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess()
    )
    monkeypatch.setattr(
        series, "_terminate_child", lambda proc: terminated.append(proc.pid)
    )

    assert (
        series.run_series(tmp_path, manifest, env_file=None, retry_failed=False) == 130
    )
    checkpoint = manifest["sources"]["cbre"]
    assert checkpoint["checkpoint_run"] == f"runs/{FRESH_RUN_ID}"
    assert checkpoint["state"] == "interrupted"
    assert checkpoint["attempts"][0]["rc"] == 130
    assert manifest["status"] == "interrupted"
    assert terminated == [4321]


def test_interrupted_startup_without_child_manifest_never_spawns(tmp_path, monkeypatch):
    manifest = series.new_manifest(tmp_path, git_sha=GIT_SHA, config=_config())
    checkpoint = manifest["sources"]["cbre"]
    checkpoint["state"] = "interrupted"
    checkpoint["checkpoint_run"] = f"runs/{FRESH_RUN_ID}"
    checkpoint["attempts"] = [
        {
            "number": 1,
            "started_at": "2026-09-13T12:00:00+00:00",
            "finished_at": "2026-09-13T12:00:01+00:00",
            "rc": 130,
            "log": "logs/01-cbre-attempt-1.log",
        }
    ]
    launches = []
    monkeypatch.setattr(
        series.subprocess,
        "Popen",
        lambda *_args, **_kwargs: launches.append(True),
    )

    for _ in range(2):
        assert (
            series.run_series(tmp_path, manifest, env_file=None, retry_failed=False)
            == 130
        )
    assert launches == []
    assert checkpoint["checkpoint_run"] == f"runs/{FRESH_RUN_ID}"
    assert len(checkpoint["attempts"]) == 1
    assert checkpoint["attempts"][0]["rc"] == 130
    assert checkpoint["state"] == "interrupted"
    assert "lacks a readable bound manifest" in checkpoint["error"]
    assert manifest["status"] == "interrupted"
