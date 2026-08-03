"""Pure, no-network contracts for cre_checkpoint_series.py."""

from __future__ import annotations

from pathlib import Path

import pytest

import cre_checkpoint_series as series


def config(sources=("cbre", "jll")):
    return series.series_config(
        sources=sources,
        page_cap=400,
        concurrency=3,
        attempts_per_source=3,
        max_resume_age_hours=24.0,
        max_host_cpu_percent=80.0,
        cpu_sustain_seconds=30.0,
        cpu_sample_seconds=5.0,
        nice=10,
    )


def test_parse_all_uses_current_complete_registry():
    parsed = series.parse_sources("all")

    assert parsed == series.SOURCE_KEYS
    assert len(parsed) == 51
    assert parsed[0] == "cbre"
    assert parsed[-1] == "nai-dominion"


def test_build_checkpoint_argv_is_serial_nice_and_cpu_guarded(tmp_path):
    argv = series.build_checkpoint_argv(
        "cbre",
        child_out_root=tmp_path / "runs",
        env_file="/private/equire.env",
        config=config(("cbre",)),
    )

    assert argv[:4] == ["/usr/bin/nice", "-n", "10", series.sys.executable]
    assert argv[argv.index("--sources") + 1] == "cbre"
    assert argv[argv.index("--source-workers") + 1] == "1"
    assert argv[argv.index("--max-host-cpu-percent") + 1] == "80.0"
    assert argv[argv.index("--cpu-sustain-seconds") + 1] == "30.0"
    assert argv[argv.index("--cpu-sample-seconds") + 1] == "5.0"
    assert argv[-2:] == ["--env-file", "/private/equire.env"]


def test_build_checkpoint_argv_resumes_exact_resource_guard_run(tmp_path):
    resume_run = tmp_path / "runs" / "2026-08-02T010000Z"
    argv = series.build_checkpoint_argv(
        "cbre",
        child_out_root=tmp_path / "runs",
        env_file=None,
        config=config(("cbre",)),
        resume_run=resume_run,
    )

    assert argv[argv.index("--resume") + 1] == str(resume_run)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("RefreshError: source checkpoints remain incomplete: cbre", True),
        (
            "RefreshError: aggregate coverage gate is not established for source(s): cbre",
            True,
        ),
        ("GlobalStageError: final validation failed", False),
        (None, False),
    ],
)
def test_source_local_failure_classification(error, expected):
    assert (
        series.source_local_failure({"status": "failed", "error": error})
        is expected
    )


def test_resume_rejects_config_or_sha_drift(tmp_path):
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=config())
    series.save_manifest(tmp_path, manifest)

    with pytest.raises(series.SeriesError, match="different collector Git SHA"):
        series.load_resume_manifest(
            tmp_path / "manifest.json",
            git_sha="b" * 40,
            config=config(),
        )
    with pytest.raises(series.SeriesError, match="configuration differs"):
        series.load_resume_manifest(
            tmp_path / "manifest.json",
            git_sha="a" * 40,
            config=config(("cbre",)),
        )


def test_series_continues_source_local_failure_then_completes(tmp_path, monkeypatch):
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=config())
    processes = []
    returns = iter([1, 0])
    child_manifests = iter(
        [
            {
                "status": "failed",
                "error": "RefreshError: source checkpoints remain incomplete: cbre",
            },
            {"status": series.SUCCESS_STATUS, "error": None},
        ]
    )

    class FakeProcess:
        def __init__(self, *_args, **_kwargs):
            self.pid = 4321
            self.rc = next(returns)
            processes.append(self)

        def wait(self, timeout=None):
            return self.rc

        def poll(self):
            return self.rc

    monkeypatch.setattr(series.subprocess, "Popen", FakeProcess)

    def fake_child(_root, *, source, **_kwargs):
        return (
            tmp_path / "runs" / source / "manifest.json",
            next(child_manifests),
        )

    monkeypatch.setattr(series, "_load_child_manifest", fake_child)

    rc = series.run_series(tmp_path, manifest, env_file=None, retry_failed=False)

    assert rc == 2
    assert len(processes) == 2
    assert manifest["status"] == "complete_with_source_failures"
    assert manifest["sources"]["cbre"]["state"] == "failed_source"
    assert manifest["sources"]["jll"]["state"] == "complete"


def test_series_stops_immediately_on_resource_guard(tmp_path, monkeypatch):
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=config())
    starts = []

    class FakeProcess:
        pid = 4321

        def __init__(self, *_args, **_kwargs):
            starts.append(True)

        def wait(self, timeout=None):
            return 75

        def poll(self):
            return 75

    monkeypatch.setattr(series.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(
        series,
        "_load_child_manifest",
        lambda *_args, **_kwargs: (
            tmp_path / "runs" / "cbre" / "manifest.json",
            {
                "status": series.RESOURCE_GUARD_STATUS,
                "error": "host CPU guard tripped",
            },
        ),
    )

    rc = series.run_series(tmp_path, manifest, env_file=None, retry_failed=False)

    assert rc == 75
    assert len(starts) == 1
    assert manifest["status"] == "resource_guard_interrupted"
    assert manifest["sources"]["jll"]["state"] == "pending"


def test_series_resumes_exact_interrupted_child_checkpoint(tmp_path, monkeypatch):
    manifest = series.new_manifest(
        tmp_path,
        git_sha="a" * 40,
        config=config(("cbre",)),
    )
    checkpoint = manifest["sources"]["cbre"]
    checkpoint["state"] = "resource_guard_interrupted"
    checkpoint["checkpoint_run"] = "runs/2026-08-02T010000Z"
    resume_run = tmp_path / checkpoint["checkpoint_run"]
    resume_run.mkdir(parents=True)
    (resume_run / "manifest.json").write_text(
        '{"status":"supported_scope_complete","error":null}',
        encoding="utf-8",
    )
    started_argv = []

    class FakeProcess:
        pid = 4321

        def __init__(self, argv, **_kwargs):
            started_argv.append(argv)

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(series.subprocess, "Popen", FakeProcess)

    rc = series.run_series(tmp_path, manifest, env_file=None, retry_failed=False)

    assert rc == 0
    assert started_argv[0][started_argv[0].index("--resume") + 1] == str(
        resume_run
    )
    assert manifest["sources"]["cbre"]["state"] == "complete"
