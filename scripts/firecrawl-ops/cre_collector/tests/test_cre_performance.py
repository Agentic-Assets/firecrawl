"""Pure filesystem and runner contracts for CRE command telemetry."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_performance as performance
import pytest

RUN_ID = "2026-09-13T123456Z-012345abcdef"


def _log_path(tmp_path: Path, name: str = "cbre-collect-attempt-1.log") -> Path:
    logs = tmp_path / RUN_ID / "logs"
    logs.mkdir(parents=True)
    return logs / name


def _records(log_path: Path) -> list[dict[str, object]]:
    journal = log_path.with_suffix(".performance.jsonl")
    return [
        json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()
    ]


def _synthetic_clock(monkeypatch: pytest.MonkeyPatch, *values: float) -> None:
    monotonic = iter(values)
    observed = iter(
        f"2026-09-13T12:34:{index:02d}.000Z" for index in range(len(values))
    )
    monkeypatch.setattr(performance.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(performance, "utc_now", lambda: next(observed))


def test_collect_success_emits_bounded_start_and_finish_and_injects_metrics_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _synthetic_clock(monkeypatch, 10.0, 12.75)
    monkeypatch.setattr(
        performance.uuid,
        "uuid4",
        lambda: type("FixedUuid", (), {"hex": "a" * 32})(),
    )
    log_path = _log_path(tmp_path)
    argv = [
        "/usr/local/bin/npx",
        "/repo/node_modules/.bin/tsx",
        "/repo/collect.ts",
        "--source=cbre",
        "--concurrency=4",
        "--page-cap",
        "400",
        "--max-items=0",
        "--out=/private/listings.json",
    ]
    supplied_env = {
        "CRE_REFRESH_GENERATION": RUN_ID,
        "SECRET_SENTINEL": "must-not-be-journaled",
    }
    received: dict[str, object] = {}

    def runner(command, command_log, *, env=None):
        received.update(argv=command, log_path=command_log, env=env)
        return 0

    result = performance.run_observed_command(
        argv,
        log_path,
        env=supplied_env,
        runner=runner,
    )

    assert result == 0
    assert received["argv"] is argv
    assert received["log_path"] == log_path
    runner_env = received["env"]
    assert isinstance(runner_env, dict)
    assert runner_env is not supplied_env
    assert runner_env["CRE_REFRESH_GENERATION"] == RUN_ID
    assert runner_env["SECRET_SENTINEL"] == "must-not-be-journaled"
    assert runner_env["CRE_PERFORMANCE_COMMAND_ID"] == "a" * 32
    assert runner_env["CRE_PERFORMANCE_PATH"] == str(
        log_path.with_name(
            f"{log_path.stem}.{'a' * 32}.scrape-performance.json"
        ).absolute()
    )
    assert "CRE_PERFORMANCE_PATH" not in supplied_env

    records = _records(log_path)
    assert len(records) == 2
    expected_common = {
        "schema_version": 1,
        "kind": "cre_command_performance",
        "run_id": RUN_ID,
        "command_id": "a" * 32,
        "command_log": log_path.name,
        "phase": "collection",
        "source": "cbre",
        "metrics_file": f"{log_path.stem}.{'a' * 32}.scrape-performance.json",
        "configured_concurrency": 4,
        "configured_page_cap": 400,
        "configured_max_items": 0,
    }
    assert records[0] == {
        **expected_common,
        "event": "started",
        "observed_at": "2026-09-13T12:34:00.000Z",
        "elapsed_seconds": 0.0,
        "outcome": "running",
        "returncode": None,
    }
    assert records[1] == {
        **expected_common,
        "event": "finished",
        "observed_at": "2026-09-13T12:34:01.000Z",
        "elapsed_seconds": 2.75,
        "outcome": "success",
        "returncode": 0,
    }
    journal = log_path.with_suffix(".performance.jsonl")
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert journal.stat().st_size < performance.MAX_RECORD_BYTES * 2
    journal_text = journal.read_text(encoding="utf-8")
    assert "SECRET_SENTINEL" not in journal_text
    assert "must-not-be-journaled" not in journal_text
    assert "/private/listings.json" not in journal_text


def test_non_collect_preserves_env_object_and_nonzero_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _synthetic_clock(monkeypatch, 3.0, 4.0)
    log_path = _log_path(tmp_path, "cbre-gate.log")
    env = {"EXISTING": "value"}
    received: dict[str, object] = {}

    def runner(command, command_log, *, env=None):
        received.update(argv=command, log_path=command_log, env=env)
        return 17

    assert (
        performance.run_observed_command(
            ["python3", "cre_gate.py"],
            log_path,
            env=env,
            runner=runner,
        )
        == 17
    )
    assert received["env"] is env
    assert _records(log_path)[1] == {
        "schema_version": 1,
        "kind": "cre_command_performance",
        "run_id": RUN_ID,
        "command_id": _records(log_path)[1]["command_id"],
        "command_log": "cbre-gate.log",
        "phase": "source_gate",
        "source": "cbre",
        "event": "finished",
        "observed_at": "2026-09-13T12:34:01.000Z",
        "elapsed_seconds": 1.0,
        "outcome": "failed",
        "returncode": 17,
        "metrics_file": None,
    }


def test_collect_with_none_env_copies_current_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _synthetic_clock(monkeypatch, 1.0, 1.5)
    monkeypatch.setenv("CRE_REFRESH_GENERATION", RUN_ID)
    monkeypatch.setenv("INHERITED_SENTINEL", "present")
    log_path = _log_path(tmp_path)
    received_env = None

    def runner(_argv, _log_path, *, env=None):
        nonlocal received_env
        received_env = env
        return 0

    performance.run_observed_command(
        ["npx", "tsx", "collect.ts", "--source", "cbre"],
        log_path,
        env=None,
        runner=runner,
    )

    assert isinstance(received_env, dict)
    assert received_env is not os.environ
    assert received_env["CRE_REFRESH_GENERATION"] == RUN_ID
    assert received_env["INHERITED_SENTINEL"] == "present"
    assert re.fullmatch(r"[0-9a-f]{32}", received_env["CRE_PERFORMANCE_COMMAND_ID"])


@pytest.mark.parametrize(
    ("exception_factory", "expected_outcome"),
    [
        (lambda: KeyboardInterrupt("operator stopped"), "interrupted"),
        (lambda: refresh.CpuGuardTrip("cpu guard stopped"), "interrupted"),
        (lambda: RuntimeError("provider secret detail"), "exception"),
    ],
)
def test_runner_exception_identity_and_cleanup_are_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception_factory,
    expected_outcome: str,
) -> None:
    _synthetic_clock(monkeypatch, 50.0, 51.25)
    log_path = _log_path(tmp_path, "healthcheck.log")
    expected = exception_factory()
    state = {"cleaned": False}

    def runner(_argv, _log_path, *, env=None):
        del env
        try:
            raise expected
        finally:
            state["cleaned"] = True

    with pytest.raises(BaseException) as caught:
        performance.run_observed_command(
            ["bash", "firecrawl_healthcheck.sh"],
            log_path,
            env={},
            runner=runner,
        )

    assert caught.value is expected
    assert state["cleaned"] is True
    records = _records(log_path)
    assert [record["event"] for record in records] == ["started", "interrupted"]
    assert records[1]["outcome"] == expected_outcome
    assert records[1]["returncode"] is None
    assert records[1]["elapsed_seconds"] == 1.25
    journal_text = log_path.with_suffix(".performance.jsonl").read_text()
    assert str(expected) not in journal_text


def test_repeated_same_log_path_uses_unique_stable_ids_per_invocation(
    tmp_path: Path,
) -> None:
    log_path = _log_path(tmp_path)

    def runner(_argv, _log_path, *, env=None):
        del env
        return 0

    for _ in range(2):
        performance.run_observed_command(
            ["npx", "tsx", "collect.ts", "--source=cbre"],
            log_path,
            env={"CRE_REFRESH_GENERATION": RUN_ID},
            runner=runner,
        )

    records = _records(log_path)
    assert len(records) == 4
    first_ids = {record["command_id"] for record in records[:2]}
    second_ids = {record["command_id"] for record in records[2:]}
    assert len(first_ids) == len(second_ids) == 1
    assert first_ids.isdisjoint(second_ids)
    assert all(
        re.fullmatch(r"[0-9a-f]{32}", str(record["command_id"])) for record in records
    )


@pytest.mark.parametrize("target_kind", ["symlink", "directory"])
def test_unsafe_journal_target_is_rejected_without_changing_command_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    target_kind: str,
) -> None:
    log_path = _log_path(tmp_path, "healthcheck.log")
    journal = log_path.with_suffix(".performance.jsonl")
    protected = tmp_path / "protected.txt"
    if target_kind == "symlink":
        protected.write_text("unchanged", encoding="utf-8")
        journal.symlink_to(protected)
    else:
        journal.mkdir()

    calls = 0

    def runner(_argv, _log_path, *, env=None):
        nonlocal calls
        del env
        calls += 1
        return 23

    assert (
        performance.run_observed_command(
            ["bash", "firecrawl_healthcheck.sh"],
            log_path,
            env=None,
            runner=runner,
        )
        == 23
    )
    assert calls == 1
    assert capsys.readouterr().err == f"{performance.PERFORMANCE_WARNING}\n"
    if target_kind == "symlink":
        assert protected.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_fifo_journal_is_rejected_without_blocking_runner(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = _log_path(tmp_path, "healthcheck.log")
    journal = log_path.with_suffix(".performance.jsonl")
    os.mkfifo(journal)
    calls = 0

    def runner(_argv, _log_path, *, env=None):
        nonlocal calls
        del env
        calls += 1
        return 0

    assert (
        performance.run_observed_command(
            ["bash", "firecrawl_healthcheck.sh"],
            log_path,
            env=None,
            runner=runner,
        )
        == 0
    )
    assert calls == 1
    assert capsys.readouterr().err == f"{performance.PERFORMANCE_WARNING}\n"
    assert stat.S_ISFIFO(journal.stat().st_mode)


def test_full_journal_stays_capped_and_warns_only_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = _log_path(tmp_path, "healthcheck.log")
    journal = log_path.with_suffix(".performance.jsonl")
    journal.write_bytes(b"x" * performance.MAX_JOURNAL_BYTES)

    assert (
        performance.run_observed_command(
            ["bash", "firecrawl_healthcheck.sh"],
            log_path,
            env=None,
            runner=lambda *_args, **_kwargs: 0,
        )
        == 0
    )
    assert journal.stat().st_size == performance.MAX_JOURNAL_BYTES
    assert capsys.readouterr().err == f"{performance.PERFORMANCE_WARNING}\n"
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600


def test_record_writer_refuses_huge_json_before_creating_file(tmp_path: Path) -> None:
    journal = tmp_path / "huge.performance.jsonl"
    with pytest.raises(ValueError, match="record exceeds cap"):
        performance._append_jsonl_record(
            journal,
            {"payload": "private" * performance.MAX_RECORD_BYTES},
        )
    assert not journal.exists()


def test_huge_or_sensitive_argv_is_not_serialized(
    tmp_path: Path,
) -> None:
    log_path = _log_path(tmp_path, "unknown.log")
    secret = "https://user:token@example.test/" + "x" * 5000
    argv = ["npx", "tsx", "collect.ts", f"--source={secret}", f"--max-items={secret}"]
    assert (
        performance.run_observed_command(
            argv,
            log_path,
            env={"CRE_REFRESH_GENERATION": RUN_ID},
            runner=lambda *_args, **_kwargs: 0,
        )
        == 0
    )
    journal = log_path.with_suffix(".performance.jsonl")
    assert journal.stat().st_size <= performance.MAX_RECORD_BYTES * 2
    assert "token" not in journal.read_text(encoding="utf-8")
    records = _records(log_path)
    assert all(record["source"] is None for record in records)
    assert all("configured_max_items" not in record for record in records)


def test_unavailable_start_clock_records_unknown_elapsed_without_skipping_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = _log_path(tmp_path, "healthcheck.log")
    calls = 0
    clock_reads = 0

    def clock():
        nonlocal clock_reads
        clock_reads += 1
        if clock_reads == 1:
            raise OSError("private clock detail")
        return 12.0

    def runner(_argv, _log_path, *, env=None):
        nonlocal calls
        del env
        calls += 1
        return 0

    monkeypatch.setattr(performance.time, "monotonic", clock)
    performance.run_observed_command(
        ["bash", "firecrawl_healthcheck.sh"],
        log_path,
        env=None,
        runner=runner,
    )

    assert calls == 1
    records = _records(log_path)
    assert records[0]["elapsed_seconds"] is None
    assert records[1]["elapsed_seconds"] is None
    assert capsys.readouterr().err == f"{performance.PERFORMANCE_WARNING}\n"


@pytest.mark.parametrize("finished", [9.0, float("nan"), float("inf")])
def test_invalid_or_reversed_finish_clock_records_unknown_elapsed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    finished: float,
) -> None:
    log_path = _log_path(tmp_path, "healthcheck.log")
    readings = iter([10.0, finished])
    monkeypatch.setattr(performance.time, "monotonic", lambda: next(readings))

    result = performance.run_observed_command(
        ["bash", "firecrawl_healthcheck.sh"],
        log_path,
        env=None,
        runner=lambda *_args, **_kwargs: 7,
    )

    assert result == 7
    records = _records(log_path)
    assert records[0]["elapsed_seconds"] == 0.0
    assert records[1]["elapsed_seconds"] is None
    assert records[1]["outcome"] == "failed"
    assert records[1]["returncode"] == 7
    assert capsys.readouterr().err == f"{performance.PERFORMANCE_WARNING}\n"


@pytest.mark.parametrize(
    ("log_name", "argv", "expected_phase"),
    [
        ("healthcheck.log", ["bash", "tool"], "healthcheck"),
        ("pre-validation.log", ["python3", "cre_validate.py"], "pre_validation"),
        ("cbre-gate.log", ["python3", "cre_gate.py"], "source_gate"),
        ("cbre-ingest-dry-run.log", ["python3", "cre_ingest.py"], "dry_run"),
        ("aggregate-gate.log", ["python3", "cre_gate.py"], "aggregate_gate"),
        ("cbre-ingest.log", ["python3", "cre_ingest.py"], "ingest"),
        ("cbre-ingest-recovery.log", ["python3", "cre_validate.py"], "ingest_recovery"),
        ("validation.log", ["python3", "cre_validate.py"], "readback"),
        ("other.log", ["offline-child"], "unknown"),
    ],
)
def test_phase_whitelist_is_inferred_from_safe_command_log(
    tmp_path: Path,
    log_name: str,
    argv: list[str],
    expected_phase: str,
) -> None:
    log_path = _log_path(tmp_path, log_name)
    performance.run_observed_command(
        argv,
        log_path,
        env={},
        runner=lambda *_args, **_kwargs: 0,
    )
    assert {record["phase"] for record in _records(log_path)} == {expected_phase}


@pytest.mark.parametrize(
    "argv",
    [
        ["python3", "wrapper.py", "collect.ts"],
        ["npx", "tsx", "different.ts", "collect.ts"],
        ["npx", "different", "collect.ts"],
    ],
)
def test_only_actual_collect_layout_receives_typescript_metrics_environment(
    tmp_path: Path,
    argv: list[str],
) -> None:
    log_path = _log_path(tmp_path, "other.log")
    env = {"CRE_REFRESH_GENERATION": RUN_ID}
    received_env = None

    def runner(_argv, _log_path, *, env=None):
        nonlocal received_env
        received_env = env
        return 0

    performance.run_observed_command(
        argv,
        log_path,
        env=env,
        runner=runner,
    )
    assert received_env is env
    assert "CRE_PERFORMANCE_PATH" not in env
    assert all(record["metrics_file"] is None for record in _records(log_path))


def test_invalid_run_id_degrades_without_altering_runner(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    logs = tmp_path / "not-a-run-id" / "logs"
    logs.mkdir(parents=True)
    log_path = logs / "healthcheck.log"
    env = {"UNCHANGED": "yes"}
    received_env = None

    def runner(_argv, _log_path, *, env=None):
        nonlocal received_env
        received_env = env
        return 5

    assert (
        performance.run_observed_command(
            ["bash", "firecrawl_healthcheck.sh"],
            log_path,
            env=env,
            runner=runner,
        )
        == 5
    )
    assert received_env is env
    assert not log_path.with_suffix(".performance.jsonl").exists()
    assert capsys.readouterr().err == f"{performance.PERFORMANCE_WARNING}\n"


def test_command_id_failure_degrades_without_skipping_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = _log_path(tmp_path)
    env = {"CRE_REFRESH_GENERATION": RUN_ID}
    received_env = None

    def fail_uuid():
        raise OSError("random source unavailable with private detail")

    def runner(_argv, _log_path, *, env=None):
        nonlocal received_env
        received_env = env
        return 0

    monkeypatch.setattr(performance.uuid, "uuid4", fail_uuid)
    assert (
        performance.run_observed_command(
            ["npx", "tsx", "collect.ts", "--source=cbre"],
            log_path,
            env=env,
            runner=runner,
        )
        == 0
    )
    assert received_env is env
    assert not log_path.with_suffix(".performance.jsonl").exists()
    assert capsys.readouterr().err == f"{performance.PERFORMANCE_WARNING}\n"


def _runtime_inspect_output(*, extra: list[dict[str, object]] | None = None) -> str:
    rows = [
        {
            "id": "a" * 64,
            "name": "/firecrawl-api-1",
            "image": f"sha256:{'b' * 64}",
            "started_at": "2026-09-13T12:00:00.123456789Z",
            "nano_cpus": 2_000_000_000,
            "memory": 8 * 1024**3,
            "memory_swap": 8 * 1024**3,
            "pids_limit": None,
            "shm_size": 64 * 1024**2,
            "ports": {
                "3002/tcp": [
                    {
                        "HostIp": "private-host-value-must-not-survive",
                        "HostPort": "3002",
                    }
                ]
            },
        },
        {
            "id": "c" * 64,
            "name": "/firecrawl-playwright-service-1",
            "image": f"sha256:{'d' * 64}",
            "started_at": "not-a-timestamp",
            "nano_cpus": 1_500_000_000,
            "memory": 4 * 1024**3,
            "memory_swap": -1,
            "pids_limit": 384,
            "shm_size": 1024**3,
            "ports": {"3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3103"}]},
        },
    ]
    rows.extend(extra or [])
    return "\n".join(json.dumps(row) for row in rows) + "\n"


def test_runtime_configuration_uses_narrow_inspect_and_writes_safe_atomic_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / RUN_ID
    run_dir.mkdir()
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=_runtime_inspect_output(),
            stderr="stderr-secret-must-not-survive",
        )

    monkeypatch.setattr(performance.subprocess, "run", fake_run)
    monkeypatch.setattr(performance, "utc_now", lambda: "2026-09-13T12:34:56.000Z")
    monkeypatch.setattr(performance.os, "cpu_count", lambda: 12)
    monkeypatch.setattr(
        performance.os,
        "sysconf",
        lambda name: 1_000_000 if name == "SC_PHYS_PAGES" else 4096,
    )

    result = performance.capture_runtime_configuration(run_dir)

    assert result is not None
    assert result["availability"] == "available"
    assert result["unavailable_code"] is None
    assert result["hardware"] == {
        "logical_cpu_count": 12,
        "memory_bytes": 4_096_000_000,
    }
    command = captured["argv"]
    assert command[:5] == ["docker", "inspect", "--type", "container", "--format"]
    assert command[-2:] == [
        "firecrawl-api-1",
        "firecrawl-playwright-service-1",
    ]
    serialized_command = " ".join(command)
    assert "Config.Env" not in serialized_command
    assert "Config.Image" not in serialized_command
    assert "docker stats" not in serialized_command
    assert captured["kwargs"] == {
        "capture_output": True,
        "check": False,
        "text": True,
        "timeout": 2.0,
    }

    path = run_dir / "runtime-performance.json"
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == result
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_size < performance.MAX_RUNTIME_BYTES
    assert written["containers"] == [
        {
            "id": "a" * 64,
            "name": "firecrawl-api-1",
            "image": f"sha256:{'b' * 64}",
            "started_at": "2026-09-13T12:00:00.123456Z",
            "cpu_limit": 2.0,
            "memory_limit_bytes": 8 * 1024**3,
            "memory_swap_limit_bytes": 8 * 1024**3,
            "pids_limit": None,
            "shm_size_bytes": 64 * 1024**2,
            "ports": ["3002/tcp->3002"],
        },
        {
            "id": "c" * 64,
            "name": "firecrawl-playwright-service-1",
            "image": f"sha256:{'d' * 64}",
            "started_at": None,
            "cpu_limit": 1.5,
            "memory_limit_bytes": 4 * 1024**3,
            "memory_swap_limit_bytes": -1,
            "pids_limit": 384,
            "shm_size_bytes": 1024**3,
            "ports": ["3000/tcp->3103"],
        },
    ]
    serialized = json.dumps(written)
    assert "private-host-value" not in serialized
    assert "stderr-secret" not in serialized
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("side_effect", "completed", "expected_code"),
    [
        (
            subprocess.TimeoutExpired(["docker", "inspect"], 2.0),
            None,
            "inspect_timeout",
        ),
        (OSError("private docker socket path"), None, "docker_unavailable"),
        (
            None,
            subprocess.CompletedProcess(
                ["docker", "inspect"],
                1,
                stdout="",
                stderr="private daemon detail",
            ),
            "inspect_failed",
        ),
        (
            None,
            subprocess.CompletedProcess(
                ["docker", "inspect"],
                0,
                stdout="not-json private value",
                stderr="",
            ),
            "invalid_output",
        ),
        (
            None,
            subprocess.CompletedProcess(
                ["docker", "inspect"],
                0,
                stdout="[" * 1000 + "0" + "]" * 1000,
                stderr="",
            ),
            "invalid_output",
        ),
    ],
)
def test_runtime_configuration_records_only_bounded_unavailable_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    side_effect: BaseException | None,
    completed: subprocess.CompletedProcess[str] | None,
    expected_code: str,
) -> None:
    run_dir = tmp_path / RUN_ID
    run_dir.mkdir()

    def fake_run(*_args, **_kwargs):
        if side_effect is not None:
            raise side_effect
        return completed

    monkeypatch.setattr(performance.subprocess, "run", fake_run)
    result = performance.capture_runtime_configuration(run_dir)

    assert result is not None
    assert result["availability"] == "unavailable"
    assert result["unavailable_code"] == expected_code
    assert result["containers"] == []
    serialized = (run_dir / "runtime-performance.json").read_text(encoding="utf-8")
    assert "private" not in serialized
    assert capsys.readouterr().err == f"{performance.PERFORMANCE_WARNING}\n"


def test_runtime_configuration_rejects_unapproved_container_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / RUN_ID
    run_dir.mkdir()
    extra = {
        "id": "e" * 64,
        "name": "/unapproved-database-1",
        "image": f"sha256:{'f' * 64}",
        "started_at": "2026-09-13T12:00:00Z",
        "nano_cpus": 1_000_000_000,
        "memory": 1024,
        "memory_swap": 1024,
        "pids_limit": 1,
        "shm_size": 1024,
        "ports": {},
    }
    monkeypatch.setattr(
        performance.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv,
            0,
            stdout=_runtime_inspect_output(extra=[extra]),
            stderr="",
        ),
    )
    result = performance.capture_runtime_configuration(run_dir)
    assert result is not None
    assert result["availability"] == "unavailable"
    assert result["unavailable_code"] == "invalid_output"
    assert result["containers"] == []


def test_runtime_configuration_refuses_symlink_target_and_preserves_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / RUN_ID
    run_dir.mkdir()
    protected = tmp_path / "protected-runtime.txt"
    protected.write_text("unchanged", encoding="utf-8")
    (run_dir / "runtime-performance.json").symlink_to(protected)
    monkeypatch.setattr(
        performance.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv,
            0,
            stdout=_runtime_inspect_output(),
            stderr="",
        ),
    )

    result = performance.capture_runtime_configuration(run_dir)

    assert result is not None
    assert result["availability"] == "unavailable"
    assert result["unavailable_code"] == "write_failed"
    assert protected.read_text(encoding="utf-8") == "unchanged"
    assert (run_dir / "runtime-performance.json").is_symlink()
    assert capsys.readouterr().err == f"{performance.PERFORMANCE_WARNING}\n"


def test_runtime_configuration_invalid_run_id_never_invokes_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "invalid-run"
    run_dir.mkdir()
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not run")

    monkeypatch.setattr(performance.subprocess, "run", fake_run)
    assert performance.capture_runtime_configuration(run_dir) is None
    assert called is False
    assert not (run_dir / "runtime-performance.json").exists()
    assert capsys.readouterr().err == f"{performance.PERFORMANCE_WARNING}\n"
