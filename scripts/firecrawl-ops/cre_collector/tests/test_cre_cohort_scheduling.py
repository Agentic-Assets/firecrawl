"""Offline ownership and throughput contracts for checkpoint source cohorts."""

from __future__ import annotations

import io
import signal
import subprocess
from pathlib import Path
from typing import Any

import cre_checkpoint_refresh as refresh
import pytest

SOURCES = (
    "transwestern",
    "savills",
    "nai-global",
    "marcus-millichap",
    "matthews",
    "hanley",
)


class VirtualProcess:
    """A deterministic process double driven by a shared virtual clock."""

    _next_pid = 9100

    def __init__(
        self,
        clock: dict[str, float],
        *,
        duration: float,
        rc: int,
    ) -> None:
        self.clock = clock
        self.ready_at = clock["now"] + duration
        self.rc = rc
        self.terminated = False
        self.pid = self._next_pid
        type(self)._next_pid += 1

    def poll(self) -> int | None:
        if self.terminated:
            return -signal.SIGINT
        return self.rc if self.clock["now"] >= self.ready_at else None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.terminated = True
        return -signal.SIGINT


class CleanupProcess:
    """A stubborn child that records only a final unbounded reap."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.reaped = False

    def poll(self) -> int | None:
        return -signal.SIGINT if self.reaped else None

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None:
            raise subprocess.TimeoutExpired("offline cohort worker", timeout)
        self.reaped = True
        return -signal.SIGKILL


class WriteFailingLog(io.StringIO):
    def write(self, _text: str) -> int:
        raise OSError("synthetic evidence write failure")


def _new_manifest(run_dir: Path, *, sources: tuple[str, ...]) -> dict[str, Any]:
    run_dir.mkdir(parents=True)
    return refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=sources,
        page_cap=400,
        concurrency=2,
        source_workers=2,
    )


def _valid_artifact(
    run_dir: Path,
    manifest: dict[str, Any],
    source: str,
) -> tuple[Path, dict[str, int]] | None:
    info = manifest["sources"][source].get("artifact")
    if not isinstance(info, dict) or not isinstance(info.get("path"), str):
        return None
    path = run_dir / info["path"]
    if not path.exists():
        return None
    return path, {"staged_unique": 1}


def _run_virtual_schedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    source_workers: int,
    schedules: dict[str, list[tuple[float, int]]],
    prior_attempts: dict[str, int] | None = None,
    prior_interrupted: set[str] | None = None,
) -> dict[str, Any]:
    """Run the production coordinator against deterministic worker doubles."""
    run_dir = tmp_path / name
    sources = tuple(schedules)
    manifest = _new_manifest(run_dir, sources=sources)
    for source, count in (prior_attempts or {}).items():
        manifest["sources"][source]["attempts"] = [
            {
                "number": number,
                "started_at": f"2026-09-12T12:00:{number:02d}+00:00",
                "finished_at": f"2026-09-12T12:01:{number:02d}+00:00",
                "rc": 1,
            }
            for number in range(1, count + 1)
        ]
        if source in (prior_interrupted or set()):
            manifest["sources"][source]["attempts"][-1].update(
                {"finished_at": None, "rc": None}
            )
            manifest["sources"][source]["state"] = "collecting"
        else:
            manifest["sources"][source]["state"] = "collect_failed"

    clock = {"now": 0.0}
    processes: list[VirtualProcess] = []
    running: set[str] = set()
    cursors = {source: 0 for source in sources}
    launches: list[dict[str, Any]] = []
    finalized: list[str] = []
    admitted: list[str] = []
    outputs: dict[str, str] = {}
    max_running = 0
    original_finalize = refresh._finalize_cohort_collection

    def start(
        current_run_dir: Path,
        current_manifest: dict[str, Any],
        source: str,
        *,
        page_cap: int,
        concurrency: int,
    ) -> refresh.CohortCollectionProcess:
        del page_cap
        nonlocal max_running
        assert source not in running, f"duplicate active source: {source}"
        created = refresh._cohort_attempt(
            current_run_dir,
            current_manifest,
            source,
            collector_concurrency=concurrency,
        )
        assert created is not None
        attempt, tmp_artifact, _attempt_log, started_at = created
        index = cursors[source]
        cursors[source] += 1
        duration, rc = schedules[source][index]
        tmp_artifact.write_text(f"canonical:{source}\n", encoding="utf-8")
        process = VirtualProcess(clock, duration=duration, rc=rc)
        processes.append(process)
        running.add(source)
        max_running = max(max_running, len(running))
        launches.append(
            {
                "source": source,
                "attempt": attempt["number"],
                "started_at": clock["now"],
                "ready_at": process.ready_at,
                "temporary_artifact": tmp_artifact.name,
            }
        )
        return refresh.CohortCollectionProcess(
            source=source,
            process=process,
            log_handle=io.StringIO(),
            tmp_artifact=tmp_artifact,
            attempt=attempt,
            attempt_started_at=started_at,
        )

    def finalize(
        current_run_dir: Path,
        current_manifest: dict[str, Any],
        item: refresh.CohortCollectionProcess,
    ) -> bool:
        try:
            return original_finalize(current_run_dir, current_manifest, item)
        finally:
            finalized.append(item.source)
            running.remove(item.source)

    def advance(
        current_run_dir: Path,
        current_manifest: dict[str, Any],
        source: str,
        **_kwargs: Any,
    ) -> bool:
        existing = _valid_artifact(current_run_dir, current_manifest, source)
        if existing is None:
            # source_workers=1 deliberately routes through the established
            # serial preparation function. Model the same controlled workload
            # without invoking a collector or duplicating scheduler logic.
            duration, rc = schedules[source][0]
            clock["now"] += duration
            launches.append(
                {
                    "source": source,
                    "attempt": 1,
                    "started_at": clock["now"] - duration,
                    "ready_at": clock["now"],
                    "temporary_artifact": None,
                }
            )
            if rc != 0:
                return False
            canonical = current_run_dir / "sources" / f"{source}.json"
            canonical.parent.mkdir(parents=True, exist_ok=True)
            canonical.write_text(f"canonical:{source}\n", encoding="utf-8")
            current_manifest["sources"][source]["artifact"] = {
                "path": f"sources/{source}.json"
            }
            current_manifest["sources"][source]["state"] = "validated"
            existing = (canonical, {"staged_unique": 1})
        artifact_path, _stats = existing
        admitted.append(source)
        outputs[source] = artifact_path.read_text(encoding="utf-8")
        return True

    def sleep(_seconds: float) -> None:
        future_completions = [
            process.ready_at for process in processes if process.poll() is None
        ]
        assert future_completions, "scheduler slept without a live worker"
        clock["now"] = min(future_completions)

    with monkeypatch.context() as patcher:
        patcher.setattr(refresh, "_start_cohort_collection", start)
        patcher.setattr(refresh, "_finalize_cohort_collection", finalize)
        patcher.setattr(
            refresh,
            "_manifest_checkpoint_artifact_valid",
            _valid_artifact,
        )
        patcher.setattr(
            refresh,
            "validate_source_artifact",
            lambda *_args, **_kwargs: {"staged_unique": 1},
        )
        patcher.setattr(refresh, "advance_source", advance)
        patcher.setattr(refresh.time, "sleep", sleep)
        failures = refresh.prepare_sources_cohort(
            run_dir,
            manifest,
            sources,
            page_cap=400,
            concurrency=2,
            attempts_this_run=max(len(items) for items in schedules.values()),
            env_file=None,
            source_workers=source_workers,
        )

    return {
        "makespan": clock["now"],
        "failures": failures,
        "launches": launches,
        "finalized": finalized,
        "admitted": admitted,
        "outputs": outputs,
        "manifest": manifest,
        "max_running": max_running,
    }


def test_selector_excludes_active_identity_and_duplicate_pending_entries():
    assert refresh.select_cohort_sources(
        ("transwestern", "transwestern", "savills"),
        ("transwestern",),
        source_workers=2,
    ) == ["savills"]
    assert refresh.select_cohort_sources(
        ("colliers-main", "savills"), (), source_workers=2
    ) == ["colliers-main"]
    assert refresh.select_cohort_sources(
        ("jll-investor", "savills"), ("jll",), source_workers=2
    ) == ["savills"]
    assert refresh.select_cohort_sources(
        ("savills",), ("transwestern", "nai-global"), source_workers=2
    ) == []
    with pytest.raises(ValueError, match="source_workers must be positive"):
        refresh.select_cohort_sources(("savills",), (), source_workers=0)
    with pytest.raises(ValueError, match="active cohort sources must be unique"):
        refresh.select_cohort_sources(
            ("savills",),
            ("transwestern", "transwestern"),
            source_workers=2,
        )


def test_collection_eligibility_uses_only_this_invocation_budget(
    tmp_path,
    monkeypatch,
):
    run_dir = tmp_path / "eligibility"
    manifest = _new_manifest(
        run_dir,
        sources=("transwestern", "savills", "nai-global"),
    )
    manifest["sources"]["transwestern"]["state"] = "collect_infrastructure_failed"
    manifest["sources"]["nai-global"]["attempts"] = [
        {"number": 1},
        {"number": 2},
    ]
    monkeypatch.setattr(
        refresh,
        "_manifest_checkpoint_artifact_valid",
        lambda _run_dir, _manifest, source: (
            (run_dir / "sources" / "savills.json", {"staged_unique": 1})
            if source == "savills"
            else None
        ),
    )

    assert not refresh._cohort_needs_collection(
        run_dir,
        manifest,
        "transwestern",
        attempts_before=0,
        attempts_this_run=2,
    )
    assert not refresh._cohort_needs_collection(
        run_dir,
        manifest,
        "savills",
        attempts_before=0,
        attempts_this_run=2,
    )
    assert not refresh._cohort_needs_collection(
        run_dir,
        manifest,
        "nai-global",
        attempts_before=1,
        attempts_this_run=1,
    )
    assert refresh._cohort_needs_collection(
        run_dir,
        manifest,
        "nai-global",
        attempts_before=2,
        attempts_this_run=1,
    )


def test_cohort_rejects_duplicate_source_sequence(tmp_path, monkeypatch):
    manifest = _new_manifest(tmp_path / "run", sources=("transwestern",))
    with pytest.raises(ValueError, match="cohort sources must be unique"):
        refresh.prepare_sources_cohort(
            tmp_path / "run",
            manifest,
            ("transwestern", "transwestern"),
            page_cap=400,
            concurrency=2,
            attempts_this_run=1,
            env_file=None,
            source_workers=2,
        )


def test_ambiguous_ingest_is_rejected_before_any_cohort_launch(
    tmp_path,
    monkeypatch,
):
    run_dir = tmp_path / "ambiguous-ingest"
    manifest = _new_manifest(run_dir, sources=("transwestern", "savills"))
    checkpoint = manifest["sources"]["savills"]
    checkpoint.update(
        state="ingesting",
        artifact={"path": "sources/missing.json"},
        ingest={"rc": None},
        attempts=[{"number": 1}],
    )
    launches: list[str] = []
    monkeypatch.setattr(
        refresh,
        "_start_cohort_collection",
        lambda _run, _manifest, source, **_kwargs: launches.append(source),
    )

    with pytest.raises(refresh.GlobalStageError, match="ambiguous interrupted ingest"):
        refresh.prepare_sources_cohort(
            run_dir,
            manifest,
            ("transwestern", "savills"),
            page_cap=400,
            concurrency=2,
            attempts_this_run=1,
            env_file=None,
            source_workers=2,
        )

    assert launches == []
    assert checkpoint["state"] == "ingest_recovery_required"
    assert checkpoint["artifact"] == {"path": "sources/missing.json"}
    assert checkpoint["attempts"] == [{"number": 1}]


def test_valid_interrupted_ingest_uses_exact_recovery_before_collection(
    tmp_path,
    monkeypatch,
):
    run_dir = tmp_path / "valid-ingest"
    manifest = _new_manifest(run_dir, sources=("transwestern",))
    checkpoint = manifest["sources"]["transwestern"]
    checkpoint.update(
        state="ingesting",
        artifact={"path": "sources/transwestern.json"},
        gate={"verdict": "ok"},
        ingest={"rc": None},
    )
    recovered: list[str] = []

    def recover(_run, _manifest, source, _env_file):
        recovered.append(source)
        checkpoint["state"] = "ingested"

    monkeypatch.setattr(
        refresh,
        "_manifest_checkpoint_artifact_valid",
        lambda *_args: (run_dir / "sources/transwestern.json", {"staged_unique": 1}),
    )
    monkeypatch.setattr(refresh, "recover_interrupted_ingest", recover)
    monkeypatch.setattr(
        refresh,
        "_start_cohort_collection",
        lambda *_args, **_kwargs: pytest.fail("valid recovery recollected source"),
    )

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


def test_cleanup_reports_log_failure_after_every_child_is_reaped(
    tmp_path,
    monkeypatch,
):
    processes = [CleanupProcess(9201), CleanupProcess(9202)]
    logs = [WriteFailingLog(), io.StringIO()]
    items = [
        refresh.CohortCollectionProcess(
            source=source,
            process=process,
            log_handle=log,
            tmp_artifact=tmp_path / f"{source}.attempt-1.json.tmp",
            attempt={"number": 1},
            attempt_started_at="2026-09-13T12:00:00+00:00",
        )
        for source, process, log in zip(
            ("transwestern", "savills"), processes, logs, strict=True
        )
    ]
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        refresh.os,
        "killpg",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )

    with pytest.raises(refresh.RefreshError, match="failed process or log evidence"):
        refresh._terminate_cohort_processes(items)

    assert {pid for pid, sent in signals if sent == signal.SIGINT} == {9201, 9202}
    assert {pid for pid, sent in signals if sent == signal.SIGKILL} == {9201, 9202}
    assert all(process.reaped for process in processes)
    assert all(log.closed for log in logs)


def test_attempt_paths_are_isolated_and_stale_completion_is_rejected(tmp_path):
    run_dir = tmp_path / "run"
    manifest = _new_manifest(run_dir, sources=("transwestern",))
    first = refresh._cohort_attempt(
        run_dir,
        manifest,
        "transwestern",
        collector_concurrency=2,
    )
    assert first is not None
    first_attempt, first_path, _first_log, first_started = first
    first_path.write_text("live first attempt", encoding="utf-8")

    second = refresh._cohort_attempt(
        run_dir,
        manifest,
        "transwestern",
        collector_concurrency=2,
    )
    assert second is not None
    _second_attempt, second_path, _second_log, _second_started = second
    second_path.write_text("live second attempt", encoding="utf-8")

    assert first_path != second_path
    assert first_path.read_text(encoding="utf-8") == "live first attempt"
    stale_log = io.StringIO()
    stale = refresh.CohortCollectionProcess(
        source="transwestern",
        process=VirtualProcess({"now": 1.0}, duration=0, rc=0),
        log_handle=stale_log,
        tmp_artifact=first_path,
        attempt=first_attempt,
        attempt_started_at=first_started,
    )
    with pytest.raises(refresh.RefreshError, match="does not own"):
        refresh._finalize_cohort_collection(run_dir, manifest, stale)

    assert stale_log.closed
    assert first_path.exists()
    assert second_path.exists()
    assert not (run_dir / "sources" / "transwestern.json").exists()


def test_uneven_workers_retry_without_duplicate_active_source(tmp_path, monkeypatch):
    result = _run_virtual_schedule(
        tmp_path,
        monkeypatch,
        name="uneven-retry",
        source_workers=2,
        schedules={
            "transwestern": [(3.0, 1), (2.0, 0)],
            "savills": [(1.0, 0)],
            "nai-global": [(4.0, 0)],
        },
    )

    assert result["failures"] == []
    assert result["max_running"] == 2
    assert result["admitted"] == ["transwestern", "savills", "nai-global"]
    transwestern = [
        item for item in result["launches"] if item["source"] == "transwestern"
    ]
    assert [item["attempt"] for item in transwestern] == [1, 2]
    assert transwestern[1]["started_at"] >= transwestern[0]["ready_at"]
    assert len({item["temporary_artifact"] for item in transwestern}) == 2
    assert result["outputs"] == {
        source: f"canonical:{source}\n"
        for source in ("transwestern", "savills", "nai-global")
    }


def test_resume_receives_a_fresh_per_invocation_retry_budget(tmp_path, monkeypatch):
    result = _run_virtual_schedule(
        tmp_path,
        monkeypatch,
        name="resume-retries",
        source_workers=2,
        schedules={
            "transwestern": [(1.0, 1), (1.0, 0)],
            "savills": [(1.0, 0)],
        },
        prior_attempts={"transwestern": 3},
        prior_interrupted={"transwestern"},
    )

    attempts = result["manifest"]["sources"]["transwestern"]["attempts"]
    assert result["failures"] == []
    assert [item["attempt"] for item in result["launches"] if item["source"] == "transwestern"] == [4, 5]
    assert [attempt["number"] for attempt in attempts] == [1, 2, 3, 4, 5]
    assert attempts[2]["finished_at"] is None
    assert result["manifest"]["sources"]["transwestern"]["state"] == "validated"


def test_interrupt_reaps_distinct_active_attempts(tmp_path, monkeypatch):
    run_dir = tmp_path / "interrupt"
    manifest = _new_manifest(run_dir, sources=("transwestern", "savills"))
    clock = {"now": 0.0}
    signals: list[tuple[int, signal.Signals]] = []
    paths: list[Path] = []

    def start(
        current_run_dir: Path,
        current_manifest: dict[str, Any],
        source: str,
        *,
        page_cap: int,
        concurrency: int,
    ) -> refresh.CohortCollectionProcess:
        del page_cap
        created = refresh._cohort_attempt(
            current_run_dir,
            current_manifest,
            source,
            collector_concurrency=concurrency,
        )
        assert created is not None
        attempt, path, _log, started_at = created
        path.write_text(f"partial:{source}", encoding="utf-8")
        paths.append(path)
        return refresh.CohortCollectionProcess(
            source=source,
            process=VirtualProcess(clock, duration=100.0, rc=0),
            log_handle=io.StringIO(),
            tmp_artifact=path,
            attempt=attempt,
            attempt_started_at=started_at,
        )

    monkeypatch.setattr(refresh, "_start_cohort_collection", start)
    monkeypatch.setattr(
        refresh.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        refresh.os,
        "killpg",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )

    with pytest.raises(KeyboardInterrupt):
        refresh.prepare_sources_cohort(
            run_dir,
            manifest,
            ("transwestern", "savills"),
            page_cap=400,
            concurrency=2,
            attempts_this_run=2,
            env_file=None,
            source_workers=2,
        )

    assert len(paths) == 2
    assert len(set(paths)) == 2
    assert all(path.exists() for path in paths)
    assert len(signals) == 2
    assert {sent_signal for _pid, sent_signal in signals} == {signal.SIGINT}


@pytest.mark.parametrize(
    "durations",
    [
        (5.0, 1.0, 4.0, 2.0, 3.0, 1.0),
        (1.0, 5.0, 2.0, 4.0, 1.0, 3.0),
        (3.0, 3.0, 3.0, 3.0, 3.0, 3.0),
        (8.0, 1.0, 1.0, 1.0, 1.0, 1.0),
        (2.0, 7.0, 1.0, 5.0, 2.0, 4.0),
    ],
)
def test_controlled_latency_two_workers_preserve_output_and_reduce_makespan(
    tmp_path,
    monkeypatch,
    durations,
):
    schedules = {
        source: [(duration, 0)]
        for source, duration in zip(SOURCES, durations, strict=True)
    }
    serial = _run_virtual_schedule(
        tmp_path,
        monkeypatch,
        name="serial",
        source_workers=1,
        schedules=schedules,
    )
    parallel = _run_virtual_schedule(
        tmp_path,
        monkeypatch,
        name="parallel",
        source_workers=2,
        schedules=schedules,
    )

    assert serial["failures"] == parallel["failures"] == []
    assert serial["outputs"] == parallel["outputs"]
    assert parallel["max_running"] == 2
    assert parallel["makespan"] < serial["makespan"]
