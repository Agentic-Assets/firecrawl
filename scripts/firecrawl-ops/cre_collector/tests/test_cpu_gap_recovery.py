"""Linux CPU-gap recovery uses fresh synthetic counters only."""
import json

import cre_checkpoint_refresh as refresh
import pytest


@pytest.fixture
def setup_guard(tmp_path, monkeypatch):
    now = [0.0]
    values = iter([20.0, 20.0, 20.0, 20.0, 20.0])
    monkeypatch.setattr(refresh.sys, "platform", "linux")
    monkeypatch.setattr(refresh.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(refresh.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    sampler = refresh.LinuxCpuSampler(read_ticks=lambda: (1, 2, 3, 4))
    sampler.previous = (1, 2, 3, 4)
    monkeypatch.setattr(refresh, "linux_cpu_percent", lambda *_: next(values))
    guard = refresh.HostCpuGuard(log_path=tmp_path / "cpu.jsonl", sampler=sampler)
    return guard, now, monkeypatch


def test_bounded_gap_requires_fresh_sample_and_records_gap(setup_guard):
    guard, now, _ = setup_guard
    guard.sample_once()
    now[0] = 7.9
    assert guard.sample_once() == 20.0
    states = [json.loads(line)["state"] for line in guard.log_path.read_text().splitlines()]
    assert states == ["ok", "gap_resampling", "gap_recovered", "ok"]
    assert guard.previous_observed == pytest.approx(8.0)
    assert guard.high_since is None
    now[0] += 3.1
    with pytest.raises(refresh.CpuTelemetryError, match="stale"):
        guard.sample_once()


@pytest.mark.parametrize("gap,prior_high,stale,fresh", [
    (8.1, False, 20.0, 20.0),
    (7.9, True, 20.0, 20.0),
    (7.9, False, 99.0, 20.0),
    (7.9, False, 20.0, 99.0),
    (7.9, False, 20.0, float("nan")),
])
def test_unsafe_gap_or_fresh_sample_fails_closed(setup_guard, gap, prior_high, stale, fresh):
    guard, now, monkeypatch = setup_guard
    guard.sample_once()
    guard.high_since = 0.0 if prior_high else None
    values = iter([stale, fresh])
    monkeypatch.setattr(refresh, "linux_cpu_percent", lambda *_: next(values))
    now[0] = gap
    with pytest.raises(refresh.CpuTelemetryError):
        guard.sample_once()


def test_darwin_cannot_use_linux_gap_recovery(setup_guard):
    guard, now, monkeypatch = setup_guard
    guard.sample_once()
    monkeypatch.setattr(refresh.sys, "platform", "darwin")
    now[0] = 7.9
    with pytest.raises(refresh.CpuTelemetryError, match="stale"):
        guard.sample_once()


def test_recovery_evidence_failure_stops(setup_guard, monkeypatch):
    guard, now, _ = setup_guard
    guard.sample_once()
    now[0] = 7.9
    monkeypatch.setattr(guard, "_write_record", lambda **_: (_ for _ in ()).throw(refresh.CpuEvidenceError("unavailable")))
    with pytest.raises(refresh.CpuEvidenceError):
        guard.sample_once()


def test_second_gap_allowed_only_after_sixty_seconds_fresh_low(setup_guard):
    guard, now, monkeypatch = setup_guard
    monkeypatch.setattr(refresh, "linux_cpu_percent", lambda *_: 20.0)
    guard.sample_once()
    now[0] = 7.9
    guard.sample_once()
    start = guard.previous_observed
    for second in range(1, 60):
        now[0] = start + second
        guard.sample_once()
    assert guard.gap_resampled is True
    now[0] = start + 60
    guard.sample_once()
    assert guard.gap_resampled is False
    now[0] += 7.9
    guard.sample_once()
    records = [json.loads(line)["state"] for line in guard.log_path.read_text().splitlines()]
    assert records.count("gap_recovered") == 2
    assert records.count("gap_cooldown_complete") == 1


def test_high_sample_restarts_cooldown(setup_guard):
    guard, now, monkeypatch = setup_guard
    value = [20.0]
    monkeypatch.setattr(refresh, "linux_cpu_percent", lambda *_: value[0])
    guard.sample_once()
    now[0] = 7.9
    guard.sample_once()
    start = guard.previous_observed
    for second in range(1, 60):
        now[0] = start + second
        guard.sample_once()
    value[0] = 99.0
    now[0] = start + 60
    guard.sample_once()
    assert guard.gap_resampled is True
    assert guard.gap_low_since is None
    value[0] = 20.0
    now[0] += 1
    guard.sample_once()
    now[0] += 3.1
    with pytest.raises(refresh.CpuTelemetryError, match="stale"):
        guard.sample_once()


def test_failed_cooldown_record_never_releases_budget(setup_guard, monkeypatch):
    guard, now, _ = setup_guard
    monkeypatch.setattr(refresh, "linux_cpu_percent", lambda *_: 20.0)
    guard.sample_once()
    now[0] = 7.9
    guard.sample_once()
    start = guard.previous_observed
    for second in range(1, 60):
        now[0] = start + second
        guard.sample_once()
    write = guard._write_record
    def fail_cooldown(**record):
        if record["state"] == "gap_cooldown_complete":
            raise refresh.CpuEvidenceError("unavailable")
        write(**record)
    monkeypatch.setattr(guard, "_write_record", fail_cooldown)
    now[0] = start + 60
    with pytest.raises(refresh.CpuEvidenceError):
        guard.sample_once()
    assert guard.gap_resampled is True


def test_invalid_sample_during_cooldown_fails_closed(setup_guard, monkeypatch):
    guard, now, _ = setup_guard
    monkeypatch.setattr(refresh, "linux_cpu_percent", lambda *_: 20.0)
    guard.sample_once()
    now[0] = 7.9
    guard.sample_once()
    monkeypatch.setattr(refresh, "linux_cpu_percent", lambda *_: float("nan"))
    now[0] += 1
    with pytest.raises(refresh.CpuTelemetryError, match="invalid"):
        guard.sample_once()
    assert guard.gap_resampled is True
