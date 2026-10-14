"""Versioned PSI warnings and strict resume binding, using synthetic evidence."""

import copy
import json
from datetime import datetime, timedelta, timezone

import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series
import cre_linux_telemetry as linux
import cre_memory_guard as memory
import pytest
from test_cre_checkpoint_series import config as series_config
from test_cre_linux_telemetry import EVENTS, PSI, write
from test_cre_linux_telemetry import kernel as kernel
from test_cre_memory_diagnostics import actual_linux_guard

V1 = "linux_psi_memavailable_swap_cgroup_v2_policy_v1"
V2 = "linux_psi_memavailable_swap_cgroup_v2_policy_v2"
NOW = datetime(2026, 10, 14, 8, tzinfo=timezone.utc)


def full_psi(kernel, location, full=2):
    sampler, group = kernel
    path = (
        sampler.proc / "pressure/memory"
        if location == "host"
        else group / "memory.pressure"
    )
    write(path, PSI.replace("avg10=0.00", f"avg10={full}"))


def legacy_policy(policy):
    result = copy.deepcopy(policy)
    result["basis"] = V1
    result["psi_full_avg10_critical_percent"] = result.pop(
        "psi_full_avg10_warning_percent", 2.0
    )
    return result


def test_linux_v2_records_full_as_warning_without_changing_other_thresholds():
    policy = linux.memory_policy()
    assert policy["basis"] == V2
    assert policy["psi_full_avg10_warning_percent"] == 2
    assert "psi_full_avg10_critical_percent" not in policy
    assert policy["psi_some_avg10_warning_percent"] == 10
    assert policy["available_critical_percent"] == 5
    assert policy["cgroup_usage_critical_percent"] == 95
    assert (
        policy["cgroup_event_policy"] == "new_high_max_warn_new_oom_oom_kill_critical"
    )


@pytest.mark.parametrize("location", ["host", "cgroup"])
def test_psi_only_spike_warns_and_normal_resets_without_interruption(
    tmp_path, kernel, monkeypatch, location
):
    guard, trips = actual_linux_guard(tmp_path, kernel, monkeypatch)
    clock = {"now": 0}
    monkeypatch.setattr(memory.time, "monotonic", lambda: clock["now"])
    assert guard.preflight() == 1
    full_psi(kernel, location)
    clock["now"] = 2
    assert guard.sample_once() == 2
    record = json.loads(guard.log_path.read_text().splitlines()[-1])
    assert record["state"] == "warning"
    assert f"{location}_psi_full" in record["linux_memory_sample"]["causes"]
    full_psi(kernel, location, 0)
    for clock["now"] in range(4, 28, 2):
        assert guard.sample_once() == 1
    assert guard.high_since is None
    assert not trips


@pytest.mark.parametrize("location", ["host", "cgroup"])
def test_psi_only_sustained_warning_stops_at_existing_twenty_seconds(
    tmp_path, kernel, monkeypatch, location
):
    guard, trips = actual_linux_guard(tmp_path, kernel, monkeypatch)
    clock = {"now": 0}
    monkeypatch.setattr(memory.time, "monotonic", lambda: clock["now"])
    assert guard.preflight() == 1
    full_psi(kernel, location)
    for clock["now"] in range(2, 22, 2):
        assert guard.sample_once() == 2
        assert not trips
    clock["now"] = 22
    assert guard.sample_once() == 2
    assert trips[0]["reason_code"] == "host_memory_warning_sustained"
    assert trips[0]["pressure"] == 2


@pytest.mark.parametrize("location", ["host", "cgroup"])
def test_normal_tick_restarts_the_full_warning_window(
    tmp_path, kernel, monkeypatch, location
):
    guard, trips = actual_linux_guard(tmp_path, kernel, monkeypatch)
    clock = {"now": 0}
    monkeypatch.setattr(memory.time, "monotonic", lambda: clock["now"])
    assert guard.preflight() == 1
    full_psi(kernel, location)
    for clock["now"] in range(2, 20, 2):
        guard.sample_once()
    full_psi(kernel, location, 0)
    clock["now"] = 20
    assert guard.sample_once() == 1
    full_psi(kernel, location)
    for clock["now"] in range(22, 42, 2):
        assert guard.sample_once() == 2
        assert not trips
    clock["now"] = 42
    assert guard.sample_once() == 2
    assert trips[0]["reason_code"] == "host_memory_warning_sustained"


@pytest.mark.parametrize("location", ["host", "cgroup"])
def test_psi_warning_still_refuses_startup(tmp_path, kernel, monkeypatch, location):
    guard, trips = actual_linux_guard(tmp_path, kernel, monkeypatch)
    full_psi(kernel, location)
    assert guard.preflight() == 2
    assert trips[0]["reason_code"] == "host_memory_start_blocked"


@pytest.mark.parametrize("critical", ["available", "usage", "oom", "oom_kill"])
def test_mixed_actual_exhaustion_still_stops_immediately(
    tmp_path, kernel, monkeypatch, critical
):
    guard, trips = actual_linux_guard(tmp_path, kernel, monkeypatch)
    assert guard.preflight() == 1
    sampler, group = kernel
    full_psi(kernel, "host")
    if critical == "available":
        write(
            sampler.proc / "meminfo",
            "MemTotal: 1000000 kB\nMemAvailable: 50000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n",
        )
    elif critical == "usage":
        write(group / "memory.current", "950000000")
    else:
        write(group / "memory.events", EVENTS.replace(f"{critical} 0", f"{critical} 1"))
    assert guard.sample_once() == 4
    assert trips[0]["reason_code"] == "host_memory_critical"
    assert "host_psi_full" in guard.linux_sampler.last_sample["causes"]


def checkpoint(tmp_path, monkeypatch, platform="linux"):
    monkeypatch.setattr(memory.sys, "platform", platform)
    run = tmp_path / "checkpoint"
    manifest = refresh.new_manifest(
        run,
        git_sha="a" * 40,
        git_dirty=False,
        sources=("jll",),
        page_cap=400,
        concurrency=3,
    )
    manifest["started_at"] = NOW.isoformat()
    path = run / "manifest.json"
    refresh.atomic_write_json(path, manifest)
    return path, manifest


def load_checkpoint(path, **overrides):
    return refresh.load_resume_manifest(
        path,
        git_sha=overrides.get("git_sha", "a" * 40),
        sources=("jll",),
        page_cap=overrides.get("page_cap", 400),
        concurrency=3,
        now=NOW + timedelta(minutes=1),
    )


def test_missing_linux_policy_refuses_without_rewriting_receipt(tmp_path, monkeypatch):
    path, manifest = checkpoint(tmp_path, monkeypatch)
    del manifest["config"]["host_memory_guard"]
    refresh.atomic_write_json(path, manifest)
    original = path.read_bytes()
    with pytest.raises(refresh.RefreshError, match="memory policy"):
        load_checkpoint(path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("fault", ["v1", "unknown", "mutated", "null"])
def test_explicit_policy_drift_refuses_without_rewriting(tmp_path, monkeypatch, fault):
    path, manifest = checkpoint(tmp_path, monkeypatch)
    policy = manifest["config"]["host_memory_guard"]
    if fault == "v1":
        policy = legacy_policy(policy)
    elif fault == "unknown":
        policy["basis"] = "unknown"
    elif fault == "mutated":
        policy["available_critical_percent"] = 6
    else:
        policy = None
    manifest["config"]["host_memory_guard"] = policy
    refresh.atomic_write_json(path, manifest)
    original = path.read_bytes()
    with pytest.raises(refresh.RefreshError, match="configuration differs"):
        load_checkpoint(path)
    assert path.read_bytes() == original


def test_current_linux_policy_resumes_but_sha_and_config_stay_bound(
    tmp_path, monkeypatch
):
    path, _ = checkpoint(tmp_path, monkeypatch)
    assert load_checkpoint(path)["config"]["host_memory_guard"]["basis"] == V2
    for overrides, message in [
        ({"git_sha": "b" * 40}, "different collector Git SHA"),
        ({"page_cap": 401}, "configuration differs"),
    ]:
        with pytest.raises(refresh.RefreshError, match=message):
            load_checkpoint(path, **overrides)


def test_missing_darwin_legacy_policy_still_resumes(tmp_path, monkeypatch):
    path, manifest = checkpoint(tmp_path, monkeypatch, "darwin")
    del manifest["config"]["host_memory_guard"]
    refresh.atomic_write_json(path, manifest)
    original = path.read_bytes()
    assert (
        load_checkpoint(path)["config"]["host_memory_guard"]
        == memory.memory_guard_config()
    )
    assert path.read_bytes() == original


def test_series_and_parent_child_refuse_old_or_missing_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(memory.sys, "platform", "linux")
    config = series_config(("jll",))
    assert config["host_memory_guard"]["basis"] == V2
    for fault in ["v1", "missing"]:
        manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=config)
        if fault == "v1":
            manifest["config"]["host_memory_guard"] = legacy_policy(
                config["host_memory_guard"]
            )
            with pytest.raises(TypeError, match="not canonical"):
                series._expected_child_config("jll", manifest["config"])
        else:
            del manifest["config"]["host_memory_guard"]
        series.save_manifest(tmp_path, manifest)
        with pytest.raises(series.SeriesError, match="configuration differs"):
            series.load_resume_manifest(
                tmp_path / "manifest.json", git_sha="a" * 40, config=config
            )
