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
