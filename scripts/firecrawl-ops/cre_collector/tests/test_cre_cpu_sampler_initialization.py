"""Exercise first and recurring Mach samples with a delayed synthetic clock."""

import json

import cre_checkpoint_refresh as refresh
import pytest
from cre_memory_guard import HostMemoryGuard


@pytest.fixture(autouse=True)
def mach_platform(monkeypatch):
    """This file exercises Mach's four-counter contract on every test host."""
    monkeypatch.setattr(refresh.sys, "platform", "darwin")


class DelayedTickSource:
    def __init__(self):
        self.elapsed = 0.0
        self.sleeps = []
        self.read_times = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds

    def read_ticks(self):
        self.read_times.append(self.elapsed)
        if self.elapsed < 2:
            return (100, 200, 300, 400)
        if self.elapsed < 4:
            return (120, 210, 370, 400)
        return (180, 220, 400, 400)


def test_default_guard_initial_sample_waits_for_counter_advance(tmp_path, monkeypatch):
    clock = DelayedTickSource()
    monkeypatch.setattr(refresh.time, "sleep", clock.sleep)
    guard = refresh.HostCpuGuard(log_path=tmp_path / "cpu.jsonl")
    guard.sampler.read_ticks = clock.read_ticks

    assert guard.sample_once() == pytest.approx(30.0)
    assert clock.sleeps == [2.0]
    assert clock.read_times == [0.0, 2.0]


def test_recurring_sample_uses_intervening_ticks_without_extra_sleep(
    tmp_path, monkeypatch
):
    clock = DelayedTickSource()
    monkeypatch.setattr(refresh.time, "sleep", clock.sleep)
    guard = refresh.HostCpuGuard(log_path=tmp_path / "cpu.jsonl")
    guard.sampler.read_ticks = clock.read_ticks
    assert guard.sample_once() == pytest.approx(30.0)

    # The caller owns the recurring cadence, rather than the sampler.
    clock.elapsed += 2.0

    assert guard.sample_once() == pytest.approx(70.0)
    assert clock.sleeps == [2.0]
    assert clock.read_times == [0.0, 2.0, 4.0]


def test_diagnostic_sampler_retains_short_initial_interval(monkeypatch):
    clock = DelayedTickSource()
    monkeypatch.setattr(refresh.time, "sleep", clock.sleep)
    sampler = refresh.DarwinCpuSampler(read_ticks=clock.read_ticks)

    with pytest.raises(refresh.CpuTelemetryError, match="did not advance"):
        sampler()
    assert clock.sleeps == [0.1]


def test_permanently_frozen_guard_counters_remain_fail_closed(tmp_path, monkeypatch):
    clock = DelayedTickSource()
    monkeypatch.setattr(refresh.time, "sleep", clock.sleep)
    guard = refresh.HostCpuGuard(log_path=tmp_path / "cpu.jsonl")
    guard.sampler.read_ticks = lambda: (100, 200, 300, 400)

    with pytest.raises(refresh.CpuTelemetryError, match="did not advance"):
        guard.sample_once()
    assert clock.sleeps == [2.0]


def test_missing_second_guard_tick_sample_remains_fail_closed(tmp_path, monkeypatch):
    clock = DelayedTickSource()
    monkeypatch.setattr(refresh.time, "sleep", clock.sleep)

    def read_ticks():
        if clock.elapsed:
            raise refresh.CpuTelemetryError("synthetic Mach telemetry unavailable")
        return (100, 200, 300, 400)

    guard = refresh.HostCpuGuard(log_path=tmp_path / "cpu.jsonl")
    guard.sampler.read_ticks = read_ticks

    with pytest.raises(refresh.CpuTelemetryError, match="telemetry unavailable"):
        guard.sample_once()
    assert clock.sleeps == [2.0]


@pytest.mark.parametrize("cpu_outcome", ["advances", "frozen", "missing"])
def test_checkpoint_monitors_memory_during_cpu_initialization(
    tmp_path, monkeypatch, cpu_outcome
):
    clock = DelayedTickSource()
    memory_guards = []
    next_memory_tick = None
    original_cpu_guard = refresh.HostCpuGuard
    external_commands = []

    def start_memory(guard):
        nonlocal next_memory_tick
        memory_guards.append(guard)
        next_memory_tick = clock.elapsed + guard.sample_seconds

    def sleep_with_memory_ticks(seconds):
        nonlocal next_memory_tick
        target = clock.elapsed + seconds
        while next_memory_tick is not None and next_memory_tick <= target:
            clock.elapsed = next_memory_tick
            # Exercise the real required guard's freshness check and journal.
            memory_guards[0].sample_once()
            next_memory_tick += memory_guards[0].sample_seconds
        clock.elapsed = target

    def cpu_guard(**kwargs):
        guard = original_cpu_guard(**kwargs)

        def read_ticks():
            ticks = clock.read_ticks()
            if cpu_outcome == "missing" and clock.elapsed:
                raise refresh.CpuTelemetryError("synthetic Mach telemetry unavailable")
            return (100, 200, 300, 400) if cpu_outcome == "frozen" else ticks

        guard.sampler.read_ticks = read_ticks
        return guard

    def healthcheck(_argv, _log, **_kwargs):
        external_commands.append(_argv)
        assert cpu_outcome == "advances"
        assert len(memory_guards) == 1
        sleep_with_memory_ticks(2.0)
        return 7  # Stop before database validation or source work.

    monkeypatch.setattr(refresh.time, "sleep", sleep_with_memory_ticks)
    monkeypatch.setattr(refresh.time, "monotonic", lambda: clock.elapsed)
    monkeypatch.setattr(refresh, "git_identity", lambda: ("b" * 40, False))
    monkeypatch.setattr(refresh, "checkpoint_lock_dir", lambda _: tmp_path / "lock")
    monkeypatch.setattr(
        refresh,
        "database_target_fingerprint",
        lambda _: {"algorithm": "sha256", "value": "a" * 64},
    )
    monkeypatch.setattr(refresh, "HostCpuGuard", cpu_guard)
    monkeypatch.setattr(original_cpu_guard, "start", lambda _: None)
    monkeypatch.setattr(HostMemoryGuard, "start", start_memory)
    monkeypatch.setattr(refresh, "run_command", healthcheck)

    argv = ["--out-root", str(tmp_path / "runs"), "--sources", "cbre"]
    if cpu_outcome == "advances":
        with pytest.raises(refresh.GlobalStageError, match="healthcheck failed"):
            refresh.main(argv)
        assert len(external_commands) == 1
    else:
        assert refresh.main(argv) == 75
        assert external_commands == []
        manifests = list((tmp_path / "runs").glob("*/manifest.json"))
        assert len(manifests) == 1
        manifest = json.loads(manifests[0].read_text())
        assert manifest["status"] == "resource_guard_interrupted"
        assert manifest["resource_stop"]["reason_code"] == "host_cpu_telemetry_failed"
        assert manifest["resource_stop"]["telemetry_valid"] is False

    assert clock.read_times == [0.0, 2.0]
    assert len(memory_guards) == 1
    assert memory_guards[0].previous_observed == (
        4.0 if cpu_outcome == "advances" else 2.0
    )
    assert memory_guards[0].stop_event.is_set()
    assert not memory_guards[0].tripped
