"""Independent offline review of real series recovery transitions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series
import pytest
from cre_resource_recovery import (
    RecoveryConfig,
    RecoveryTelemetryError,
    wait_for_cpu_recovery,
)


class Clock:
    def __init__(self):
        self.seconds = 0.0

    def now(self):
        return datetime(2026, 9, 13, 12, tzinfo=timezone.utc) + timedelta(
            seconds=self.seconds
        )

    def iso(self):
        return self.now().isoformat()

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds


def pair(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(series, "utc_now", clock.iso)
    monkeypatch.setattr(refresh, "utc_now", clock.iso)
    monkeypatch.setattr(series, "_now_utc", clock.now)
    monkeypatch.setattr(series, "publish_series_health", lambda *_args: None)
    config = series.series_config(
        sources=("cbre",),
        page_cap=400,
        concurrency=2,
        attempts_per_source=1,
        max_resume_age_hours=24.0,
        max_host_cpu_percent=75.0,
        cpu_sustain_seconds=10.0,
        cpu_sample_seconds=2.0,
        nice=10,
        recovery=RecoveryConfig(
            max_recoveries_per_source=1, low_cpu_seconds=2.0, sample_seconds=1.0
        ),
    )
    target = {"sha256": "d" * 64}
    root = tmp_path / "series"
    child_dir = root / "runs" / "2026-09-13T120000Z"
    parent = series.new_manifest(root, git_sha="a" * 40, config=config, database_target=target)
    child = refresh.new_manifest(
        child_dir,
        git_sha="a" * 40,
        git_dirty=False,
        sources=("cbre",),
        page_cap=400,
        concurrency=2,
        source_workers=1,
        max_host_cpu_percent=75.0,
        cpu_sustain_seconds=10.0,
        cpu_sample_seconds=2.0,
        database_target=target,
    )
    return clock, root, parent, child_dir / "manifest.json", child


def stop_child(clock, child):
    child["status"] = series.RESOURCE_GUARD_STATUS
    child["sources"]["cbre"]["state"] = "collecting"
    child["resource_stop"] = refresh.resource_stop_record(
        child,
        refresh.CpuGuardTripDetails(
            reason_code="host_cpu_sustained",
            reason="offline CPU saturation",
            telemetry_valid=True,
            evidence_valid=True,
            host_cpu_percent=80.0,
            occurred_at=clock.iso(),
            context={"phase": "collect", "source": "cbre"},
        ),
    )
    child["finished_at"] = clock.iso()


def test_recovery_rejects_internally_ordered_future_cpu_stop(tmp_path, monkeypatch):
    clock, root, parent, path, child = pair(tmp_path, monkeypatch)
    current = clock.now()
    clock.seconds = 7200.0
    stop_child(clock, child)

    with pytest.raises(series.SeriesError):
        series.validate_resource_recovery_admission(
            root, parent, path, child, source="cbre", now=current
        )


def test_stale_parent_reconciles_terminal_resumed_generation(tmp_path, monkeypatch):
    clock, root, parent, path, child = pair(tmp_path, monkeypatch)
    clock.seconds = 10.0
    resumed_at = clock.iso()
    clock.seconds = 20.0
    stop_child(clock, child)
    refresh.atomic_write_json(path, child)
    checkpoint = parent["sources"]["cbre"]
    checkpoint.update(
        state="running",
        checkpoint_run=str(path.parent.relative_to(root)),
        attempts=[
            {"number": 1, "started_at": child["started_at"], "finished_at": resumed_at},
            {"number": 2, "started_at": resumed_at, "finished_at": None},
        ],
    )

    series.reconcile_stale_running_sources(root, parent)

    assert checkpoint["state"] == "resource_guard_interrupted"
    assert checkpoint["checkpoint_run"] == str(path.parent.relative_to(root))
    assert checkpoint["attempts"][-1]["finished_at"] == child["finished_at"]
    assert child["started_at"] < resumed_at


def test_stale_parent_never_attributes_old_stop_to_new_resume_attempt(tmp_path, monkeypatch):
    clock, root, parent, path, child = pair(tmp_path, monkeypatch)
    clock.seconds = 10.0
    stop_child(clock, child)
    refresh.atomic_write_json(path, child)
    clock.seconds = 20.0
    checkpoint = parent["sources"]["cbre"]
    checkpoint.update(
        state="running",
        checkpoint_run=str(path.parent.relative_to(root)),
        attempts=[
            {"number": 1, "started_at": child["started_at"]},
            {"number": 2, "started_at": clock.iso(), "finished_at": None},
        ],
    )

    with pytest.raises(series.SeriesError):
        series.reconcile_stale_running_sources(root, parent)

    assert checkpoint["state"] == "running"
    assert checkpoint["attempts"][-1]["finished_at"] is None


@pytest.mark.parametrize("second_result", ["success", "cpu_stop"])
def test_real_series_cooldown_resumes_same_generation_once(
    tmp_path, monkeypatch, second_result
):
    clock, root, parent, path, child = pair(tmp_path, monkeypatch)
    launches = []
    initial_generation_start = child["started_at"]
    monkeypatch.setattr(series, "allocate_run_id", lambda: path.parent.name)
    monkeypatch.setattr(series.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(series.time, "sleep", clock.sleep)
    monkeypatch.setattr(series, "read_host_cpu_percent", lambda: 55.0)
    monkeypatch.setattr(series, "git_identity", lambda: ("a" * 40, False))
    monkeypatch.setattr(
        series, "database_target_fingerprint", lambda _env: parent["database_target"]
    )

    class ChildProcess:
        pid = 99801

        def __init__(self, argv, **_kwargs):
            launches.append(list(argv))
            assert len(launches) <= 2, "bounded recovery launched an extra child"

        def wait(self, timeout=None):
            del timeout
            clock.sleep(1.0)
            if len(launches) == 1 or second_result == "cpu_stop":
                stop_child(clock, child)
                rc = 75
            else:
                child["status"] = series.SUCCESS_STATUS
                child["finished_at"] = clock.iso()
                child["resource_stop"] = None
                rc = 0
            refresh.atomic_write_json(path, child)
            return rc

    monkeypatch.setattr(series.subprocess, "Popen", ChildProcess)

    result = series.run_series(root, parent, env_file=None, retry_failed=False)

    assert len(launches) == 2
    assert "--resume" not in launches[0]
    resume_index = launches[1].index("--resume")
    assert launches[1][resume_index + 1] == str(path.parent)
    assert parent["resource_recovery"]["total_recoveries"] == 1
    assert parent["resource_recovery"]["cumulative_wait_seconds"] == 2.0
    assert json.loads(path.read_text())["started_at"] == initial_generation_start
    if second_result == "success":
        assert result == 0
        assert parent["status"] == "complete"
    else:
        assert result == 75
        assert parent["resource_recovery"]["last_event"]["reason_code"] == (
            "recovery_budget_exhausted"
        )


@pytest.mark.parametrize("gap_origin", ["sleep", "progress"])
def test_unobserved_gap_cannot_satisfy_continuous_low_cpu_window(gap_origin):
    clock = Clock()
    sample_times = []
    inserted_gap = False

    def sample():
        sample_times.append(clock.seconds)
        return 55.0

    def sleep(seconds):
        nonlocal inserted_gap
        if gap_origin == "sleep" and not inserted_gap:
            inserted_gap = True
            clock.sleep(40.0)
        else:
            clock.sleep(seconds)

    def progress(_progress):
        nonlocal inserted_gap
        if gap_origin == "progress" and not inserted_gap:
            inserted_gap = True
            clock.sleep(40.0)

    try:
        result = wait_for_cpu_recovery(
            RecoveryConfig(max_recoveries_per_source=1),
            already_waited_seconds=0.0,
            series_waited_seconds=0.0,
            sampler=sample,
            monotonic=clock.monotonic,
            sleep=sleep,
            utc_now=clock.iso,
            on_progress=progress,
        )
    except RecoveryTelemetryError:
        # A missing observation interval can fail closed immediately.
        return

    # Alternatively the coordinator may restart the low window at the first
    # observation after the gap, but two samples cannot certify the gap itself.
    assert len(sample_times) > 2
    assert result.waited_seconds >= sample_times[1] + 30.0
