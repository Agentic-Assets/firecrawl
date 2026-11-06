"""Failure diagnostics preserve strict decisions without disclosing raw errors."""

import json
from unittest.mock import patch

import pytest
import cre_memory_guard as memory


@pytest.mark.parametrize(
    "kind,clocks,previous,category",
    [
        ("sample_gap", [21.28, 21.29], 0.0, "sample_gap"),
        ("read_stale", [0.0, 1.01], None, "read_stale"),
        ("read_error", [0.0, 0.01], None, "read_unavailable"),
    ],
)
def test_failure_journal_distinguishes_sampling_failures_without_relaxing(
    tmp_path, kind, clocks, previous, category
):
    def sampler():
        if kind == "read_error":
            raise OSError("secret/path credential body")
        return 1

    trips = []
    guard = memory.HostMemoryGuard(
        tmp_path / "memory.jsonl", lambda **kw: trips.append(kw), sampler=sampler,
        advisory_failures=False
    )
    guard.previous_observed = previous
    with patch.object(memory.time, "monotonic", side_effect=clocks):
        with pytest.raises((memory.MemoryTelemetryError, OSError)):
            guard.preflight()
    row = json.loads(guard.log_path.read_text())
    assert row["reason_code"] == "host_memory_telemetry_failed"
    assert row["telemetry_failure"]["category"] == category
    assert row["telemetry_failure"]["read_seconds"] >= 0
    assert trips[0]["telemetry_valid"] is False
    assert "secret" not in json.dumps(row)
    assert "linux_memory_sample" not in row


@pytest.mark.parametrize("error", [ValueError("secret/path"), KeyError("secret-body")])
def test_unexpected_failure_is_bounded_and_run_still_stops(tmp_path, error):
    class OneTick:
        def __init__(self):
            self.calls = 0

        def wait(self, seconds):
            self.calls += 1
            return self.calls > 1

    def sampler():
        raise error

    trips = []
    guard = memory.HostMemoryGuard(
        tmp_path / "memory.jsonl", lambda **kw: trips.append(kw), sampler=sampler,
        advisory_failures=False
    )
    guard.stop_event = OneTick()
    guard._run()
    assert guard.stop_event.calls <= 2
    row = json.loads(guard.log_path.read_text())
    assert row["telemetry_failure"]["category"] == "unexpected"
    assert row["reason_code"] == "host_memory_telemetry_failed"
    assert trips[0]["telemetry_valid"] is False
    assert "secret" not in json.dumps(row)


def test_log_evidence_failure_keeps_original_flags(tmp_path, monkeypatch):
    guard = memory.HostMemoryGuard(
        tmp_path / "memory.jsonl", lambda **kw: None, sampler=lambda: 1,
        advisory_failures=False
    )
    monkeypatch.setattr(
        guard, "_write", lambda **kw: (_ for _ in ()).throw(OSError("secret"))
    )
    with pytest.raises(memory.MemoryEvidenceError):
        guard.preflight()
    assert guard.tripped
    assert guard.telemetry_failure["category"] == "evidence_write"
    assert "secret" not in json.dumps(guard.telemetry_failure)


@pytest.mark.parametrize("missing", [False, True])
def test_linux_error_chain_classification_is_bounded_and_original_preserved(
    tmp_path, missing
):
    from cre_linux_telemetry import LinuxTelemetryError

    def sampler():
        if missing:
            try:
                raise FileNotFoundError("secret kernelpath")
            except OSError as cause:
                try:
                    raise LinuxTelemetryError(
                        "Linux resource evidence unavailable"
                    ) from cause
                except LinuxTelemetryError as wrapped:
                    raise memory.MemoryTelemetryError(
                        "Linux resource evidence unavailable"
                    ) from wrapped
        raise LinuxTelemetryError("secret malformed PSI text")

    trips = []
    guard = memory.HostMemoryGuard(
        tmp_path / "memory.jsonl", lambda **kw: trips.append(kw), sampler=sampler,
        advisory_failures=False
    )
    with pytest.raises(memory.MemoryTelemetryError if missing else LinuxTelemetryError):
        guard.preflight()
    row = json.loads(guard.log_path.read_text())
    info = row["telemetry_failure"]
    assert info["category"] == ("read_unavailable" if missing else "linux_telemetry")
    if missing:
        assert info["exception_classes"] == [
            "MemoryTelemetryError",
            "LinuxTelemetryError",
            "FileNotFoundError",
        ]
    assert len(info["exception_classes"]) <= 3
    assert "secret" not in json.dumps(row)
    assert trips[0]["telemetry_valid"] is False
