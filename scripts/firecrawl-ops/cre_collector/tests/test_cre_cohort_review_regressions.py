"""Offline adversarial regressions for cohort resume and process cleanup."""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series
import pytest


@pytest.mark.parametrize("state", ["ingesting", "ingest_recovery_required"])
@pytest.mark.parametrize("prior_attempts", [0, 3])
def test_cohort_resume_never_recollects_ambiguous_ingest(
    tmp_path, monkeypatch, state, prior_attempts
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("transwestern",),
        page_cap=400,
        concurrency=2,
        source_workers=2,
    )
    checkpoint = manifest["sources"]["transwestern"]
    checkpoint.update(
        state=state,
        artifact={"path": "sources/missing.json"},
        ingest={"rc": None},
        attempts=[{"number": number + 1} for number in range(prior_attempts)],
    )

    def forbidden(*_args, **_kwargs):
        pytest.fail("cohort tried new collection before resolving ambiguous ingest")

    monkeypatch.setattr(refresh, "_start_cohort_collection", forbidden)
    monkeypatch.setattr(refresh, "collect_source", forbidden)
    monkeypatch.setattr(refresh, "gate_source", forbidden)
    monkeypatch.setattr(refresh, "dry_run_source", forbidden)
    monkeypatch.setattr(refresh, "ingest_source", forbidden)

    with pytest.raises(refresh.GlobalStageError):
        refresh.prepare_sources_cohort(
            run_dir,
            manifest,
            ("transwestern",),
            page_cap=400,
            concurrency=2,
            attempts_this_run=1,
            env_file=None,
            source_workers=2,
        )

    assert checkpoint["state"] == "ingest_recovery_required"
    assert len(checkpoint["attempts"]) == prior_attempts
    assert checkpoint["artifact"] == {"path": "sources/missing.json"}


def test_cohort_resume_recovers_valid_ingest_without_recollection(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("transwestern",),
        page_cap=400,
        concurrency=2,
        source_workers=2,
    )
    checkpoint = manifest["sources"]["transwestern"]
    checkpoint.update(state="ingesting", gate={"verdict": "ok"}, ingest={"rc": None})
    recovered = []

    def recover(_run_dir, _manifest, source, _env_file):
        recovered.append(source)
        checkpoint["state"] = "ingested"

    def forbidden(*_args, **_kwargs):
        pytest.fail("completed interrupted ingest must not collect, gate, or replay")

    monkeypatch.setattr(
        refresh,
        "_manifest_checkpoint_artifact_valid",
        lambda *_args: (run_dir / "sources/transwestern.json", {"staged_unique": 1}),
    )
    monkeypatch.setattr(refresh, "recover_interrupted_ingest", recover)
    monkeypatch.setattr(refresh, "_start_cohort_collection", forbidden)
    monkeypatch.setattr(refresh, "collect_source", forbidden)
    monkeypatch.setattr(refresh, "gate_source", forbidden)
    monkeypatch.setattr(refresh, "dry_run_source", forbidden)
    monkeypatch.setattr(refresh, "ingest_source", forbidden)

    assert refresh.prepare_sources_cohort(
        run_dir,
        manifest,
        ("transwestern",),
        page_cap=400,
        concurrency=2,
        attempts_this_run=1,
        env_file=None,
        source_workers=2,
    ) == []
    assert recovered == ["transwestern"]
    assert checkpoint["state"] == "ingested"


class FailingLog(io.StringIO):
    """Model a failed evidence device independently of child shutdown."""

    def __init__(self, failing_operation: str):
        super().__init__()
        self.failing_operation = failing_operation

    def write(self, text: str) -> int:
        if self.failing_operation == "write":
            raise OSError("synthetic evidence write failure")
        return super().write(text)

    def flush(self) -> None:
        if self.failing_operation == "flush":
            raise OSError("synthetic evidence flush failure")
        super().flush()

    def close(self) -> None:
        super().close()
        if self.failing_operation == "close":
            raise OSError("synthetic evidence close failure")


class OwnedProcess:
    """A process double that requires wait to prove it was reaped."""

    def __init__(self, pid: int, *, stubborn: bool):
        self.pid = pid
        self.stubborn = stubborn
        self.reaped = False

    def poll(self) -> int | None:
        return -signal.SIGINT if self.reaped else None

    def wait(self, timeout: float | None = None) -> int:
        if self.stubborn and timeout is not None:
            raise subprocess.TimeoutExpired("offline worker", timeout)
        self.reaped = True
        return -signal.SIGINT


@pytest.mark.parametrize("failing_operation", ["write", "flush", "close"])
@pytest.mark.parametrize("stubborn", [False, True])
def test_cohort_cleanup_reaps_every_child_despite_log_failure(
    tmp_path, monkeypatch, failing_operation, stubborn
):
    processes = [OwnedProcess(99101 + index, stubborn=stubborn) for index in range(2)]
    logs = [FailingLog(failing_operation), io.StringIO()]
    items = [
        refresh.CohortCollectionProcess(
            source=source,
            process=process,
            log_handle=log,
            tmp_artifact=Path(tmp_path) / f"{source}.attempt-1.json.tmp",
            attempt={"number": 1},
            attempt_started_at="2026-09-13T12:00:00+00:00",
        )
        for source, process, log in zip(
            ("transwestern", "savills"), processes, logs, strict=True
        )
    ]
    signals = []
    monkeypatch.setattr(
        refresh.os, "killpg", lambda pid, sent_signal: signals.append((pid, sent_signal))
    )

    # Failing evidence may remain fatal, but only after every owned child has
    # been signaled and reaped. The canonical lock must remain held until then.
    try:
        refresh._terminate_cohort_processes(items)
    except (OSError, refresh.RefreshError):
        pass

    assert {pid for pid, sent in signals if sent == signal.SIGINT} == {99101, 99102}
    if stubborn:
        assert {pid for pid, sent in signals if sent == signal.SIGKILL} == {99101, 99102}
    assert all(process.reaped for process in processes)
    assert all(log.closed for log in logs)


@pytest.mark.parametrize("stop_type", [KeyboardInterrupt, refresh.CpuGuardTrip])
@pytest.mark.parametrize("failing_operation", [None, "write", "flush", "close"])
@pytest.mark.parametrize("stubborn", [False, True])
def test_serial_cleanup_reaps_child_before_reporting_evidence_failure(
    tmp_path, monkeypatch, stop_type, failing_operation, stubborn
):
    class ArmedLog(FailingLog):
        def __init__(self):
            super().__init__("")

    log = ArmedLog()

    class InterruptedProcess(OwnedProcess):
        def __init__(self):
            super().__init__(99201, stubborn=stubborn)
            self.wait_calls = 0

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                log.failing_operation = failing_operation or ""
                raise stop_type("offline interruption")
            return super().wait(timeout)

    process = InterruptedProcess()
    signals = []
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: log)
    monkeypatch.setattr(refresh.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        refresh.os, "killpg", lambda pid, sent_signal: signals.append((pid, sent_signal))
    )
    refresh._clear_cpu_guard_trip()
    if stop_type is refresh.CpuGuardTrip:
        refresh._set_cpu_guard_trip(
            "offline saturation",
            host_cpu_percent=80.0,
            context={"phase": "collect", "source": "transwestern"},
        )
    try:
        with pytest.raises((KeyboardInterrupt, OSError, refresh.RefreshError)) as caught:
            refresh.run_command(["offline-child"], tmp_path / "command.log", env={})

        assert (99201, signal.SIGINT) in signals
        if stubborn:
            assert (99201, signal.SIGKILL) in signals
        assert process.reaped
        assert log.closed
        if failing_operation is None:
            assert type(caught.value) is stop_type
        elif isinstance(caught.value, refresh.CpuGuardTrip):
            details = refresh._peek_cpu_guard_trip_details()
            assert details is not None
            assert details.evidence_valid is False
    finally:
        refresh._clear_cpu_guard_trip()


def test_nested_shutdown_graces_leave_time_for_inner_cleanup():
    assert refresh.COMMAND_INTERRUPT_GRACE_SECONDS < refresh.COHORT_INTERRUPT_GRACE_SECONDS
    assert refresh.COHORT_INTERRUPT_GRACE_SECONDS < series.SERIES_INTERRUPT_GRACE_SECONDS


def test_nested_dummy_process_is_reaped_before_cohort_worker_shutdown(tmp_path, monkeypatch):
    """Exercise actual nested groups with scaled shutdown grace and no I/O services."""
    monkeypatch.setattr(refresh, "COHORT_INTERRUPT_GRACE_SECONDS", 0.8)
    child_code = (
        "import os,signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); "
        'print("DUMMY_PID="+str(os.getpid()),flush=True); time.sleep(5)'
    )
    worker_code = (
        "import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
        "import cre_checkpoint_refresh as r; r.COMMAND_INTERRUPT_GRACE_SECONDS=0.2; "
        "r.run_command([sys.executable,'-u','-c',sys.argv[2]],Path(sys.argv[3]),env={})"
    )
    worker = None
    inner_pid = None
    try:
        worker = subprocess.Popen(
            [sys.executable, "-u", "-c", worker_code, str(refresh.COLLECTOR_DIR),
             child_code, str(tmp_path / "child.log")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if (tmp_path / "child.log").exists():
                for line in (tmp_path / "child.log").read_text().splitlines():
                    if line.startswith("DUMMY_PID="):
                        inner_pid = int(line.partition("=")[2])
            if inner_pid is not None:
                break
            time.sleep(0.01)
        assert inner_pid is not None, "inert nested child did not start"
        item = refresh.CohortCollectionProcess(
            "transwestern", worker, io.StringIO(), tmp_path / "unused",
            {"number": 1}, "2026-09-13T12:00:00+00:00",
        )

        refresh._terminate_cohort_processes([item])

        status = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(inner_pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        assert worker.poll() != -signal.SIGKILL, "outer grace cut off worker cleanup"
        assert not status, "nested child must be reaped, including any zombie"
    finally:
        if inner_pid is not None:
            try:
                os.killpg(inner_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if worker is not None and worker.poll() is None:
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            worker.wait()


def test_series_reaps_child_that_exits_between_poll_and_signal(monkeypatch):
    process = OwnedProcess(99301, stubborn=False)

    def vanished(*_args):
        raise ProcessLookupError

    monkeypatch.setattr(series.os, "killpg", vanished)
    series._terminate_child(process)
    assert process.reaped
