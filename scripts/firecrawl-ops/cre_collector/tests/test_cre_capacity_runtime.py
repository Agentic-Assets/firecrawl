"""Pure and mocked contracts for the CRE capacity runtime controller."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import cre_capacity_experiment as experiment
import cre_capacity_runtime as runtime
import pytest


def profile() -> tuple[dict[str, object], str]:
    return experiment.load_profile(experiment.DEFAULT_CONFIG, "bold-jll-128")


def public_state(
    *, state: str = "baseline", docker_memory: int | None = None
) -> dict[str, object]:
    selected, _ = profile()
    baseline = selected["runtime_baseline"]
    requested = selected["requested"]
    browser_pages = (
        baseline["global_pages"] if state == "baseline" else requested["global_pages"]
    )
    browser_cpu = (
        baseline["browser_cpus"] if state == "baseline" else requested["browser_cpus"]
    )
    browser_pids = (
        baseline["browser_pids"] if state == "baseline" else requested["browser_pids"]
    )
    api_cpu = baseline["api_cpus"] if state == "baseline" else requested["api_cpus"]
    browser_env = {
        "count": 2,
        "keys_sha256": "k",
        "values_sha256": "v",
        "excluding_pages_sha256": "same",
    }
    api_env = {
        "count": 1,
        "keys_sha256": "a",
        "values_sha256": "b",
        "excluding_pages_sha256": "b",
    }
    value: dict[str, object] = {
        "repo": {
            "git_sha": "a" * 40,
            "dirty": False,
            "compose_sha256": "c" * 64,
            "override_sha256": "d" * 64,
            "execution_inputs_sha256": {
                key: str(index) * 64
                for index, key in enumerate(runtime.EXECUTION_INPUTS, 1)
            },
        },
        "host": {
            "orb_status": "Running",
            "orbstack_memory_mib": 32768,
            "docker_context": "orbstack",
            "docker_memtotal_bytes": docker_memory
            if docker_memory is not None
            else 32768 * 1024 * 1024 - 32768,
        },
        "browser": {
            "id": "browser-before" if state == "baseline" else "browser-after",
            "image": "sha256:" + "b" * 64,
            "env": browser_env,
            "page_slots": str(browser_pages),
            "nano_cpus": browser_cpu * 1_000_000_000,
            "memory_bytes": baseline["browser_memory_bytes"],
            "swap_bytes": 0,
            "pids_limit": browser_pids,
            "shm_bytes": baseline["browser_shm_bytes"],
            "port_bindings": {
                "3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3103"}]
            },
            "network_mode": "firecrawl_backend",
            "mounts_sha256": "empty",
            "mount_count": 0,
            "security_opt": ["no-new-privileges:true"],
            "cap_drop": ["ALL"],
            "cgroup_memory_max": baseline["browser_memory_bytes"],
            "cgroup_swap_max": 0,
            "cgroup_memory_current": 512 * 1024 * 1024,
        },
        "api": {
            "id": "api-same",
            "image": "sha256:" + "a" * 64,
            "env": api_env,
            "page_slots": None,
            "nano_cpus": api_cpu * 1_000_000_000,
            "memory_bytes": baseline["api_memory_bytes"],
            "swap_bytes": 0,
            "pids_limit": None,
            "shm_bytes": 64 * 1024 * 1024,
            "port_bindings": {"3002/tcp": [{"HostIp": "0.0.0.0", "HostPort": "3102"}]},
            "network_mode": "firecrawl_backend",
            "mounts_sha256": "api-mount",
            "mount_count": 1,
            "security_opt": ["no-new-privileges:true"],
            "cap_drop": ["ALL"],
            "cgroup_memory_max": baseline["api_memory_bytes"],
            "cgroup_swap_max": 0,
            "cgroup_memory_current": 3 * 1024 * 1024 * 1024,
        },
        "settlement": {
            "api_root_status": 200,
            "browser_root_status": 404,
            "api": {"active": 0, "waiting": 0, "total": 0},
            "active_crawls": 0,
            "rabbitmq_queue_count": 2,
            "rabbitmq_ready": 0,
            "rabbitmq_unacknowledged": 0,
            "nuq": {
                "queue_scrape_total": 0,
                "queue_scrape_backlog_total": 0,
                "queue_crawl_finished_total": 0,
            },
            "cre_process_active": False,
        },
    }
    value["transition_sha256"] = runtime.transition_fingerprint(value)
    value["snapshot_sha256"] = runtime.snapshot_fingerprint(value)
    return value


def capture(state: str = "baseline") -> runtime.RuntimeCapture:
    return runtime.RuntimeCapture(
        public=public_state(state=state),
        browser_env={
            "MAX_CONCURRENT_PAGES": "4" if state == "baseline" else "10",
            "PROXY_SERVER": "private",
        },
        api_env={"MODEL_NAME": "private"},
    )


def write_approval(path: Path, receipt: dict[str, object], digest: str) -> Path:
    approval = {
        "schema_version": runtime.SCHEMA_VERSION,
        "kind": runtime.APPROVAL_KIND,
        "profile": "bold-jll-128",
        "config_sha256": digest,
        "transition_receipt_sha256": receipt["receipt_sha256"],
        "source_git_sha": receipt["baseline"]["repo"]["git_sha"],  # type: ignore[index]
        "approved_by": "coordinating-review",
        "approved": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_after_seconds": runtime.RECEIPT_MAX_AGE_SECONDS,
        "nonce": "e" * 64,
    }
    runtime.write_private(path, approval)
    return path


def mock_transition_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, events: list[str] | None = None
) -> None:
    observed = events if events is not None else []

    class FakeLock:
        def __init__(self, path: Path) -> None:
            assert path == tmp_path / "out" / "daily" / ".cre.lock"

        def acquire(self) -> None:
            observed.append("lock-acquire")

        def release(self) -> None:
            observed.append("lock-release")

    monkeypatch.setattr(runtime, "SharedLock", FakeLock)
    monkeypatch.setattr(
        runtime,
        "_canonical_transition_lock",
        lambda: tmp_path / "out" / "daily" / ".cre.lock",
    )

    def consume_approval(path: Path, recovery_path: Path) -> bytes:
        try:
            raw = path.read_bytes()
            approval = json.loads(raw)
            grant_path = runtime._benchmark_grant_path(path.parent, approval)
            runtime.write_private(
                grant_path,
                runtime._benchmark_grant_payload(approval),
                refuse_existing=True,
            )
            path.unlink()
        except OSError as exc:
            raise runtime.RuntimeAdmissionError(
                "review approval could not be atomically consumed"
            ) from exc
        return raw

    monkeypatch.setattr(runtime, "_consume_review_approval_bytes", consume_approval)
    monkeypatch.setattr(runtime, "_recover_review_consumption", lambda *a, **kw: None)

    def destroy_grant(path: Path) -> None:
        observed.append("grant-destroy")
        path.unlink()

    monkeypatch.setattr(runtime, "_destroy_review_benchmark_grant", destroy_grant)


def mixed_capture(browser_state: str, api_state: str) -> runtime.RuntimeCapture:
    value = capture(browser_state)
    selected, _ = profile()
    value.public["api"]["nano_cpus"] = (  # type: ignore[index]
        selected["runtime_baseline"]["api_cpus"]  # type: ignore[index]
        if api_state == "baseline"
        else selected["requested"]["api_cpus"]  # type: ignore[index]
    ) * 1_000_000_000
    value.public["transition_sha256"] = runtime.transition_fingerprint(value.public)
    value.public["snapshot_sha256"] = runtime.snapshot_fingerprint(value.public)
    return value


def test_benign_32k_docker_memory_variation_passes() -> None:
    selected, _ = profile()
    checks = runtime.evaluate_state(public_state(), selected, "baseline")
    assert checks["docker_usable_memory"] is True
    assert all(checks.values())


def test_material_docker_memory_shortfall_fails() -> None:
    selected, _ = profile()
    configured = 32768 * 1024 * 1024
    checks = runtime.evaluate_state(
        public_state(docker_memory=configured * 94 // 100), selected, "baseline"
    )
    assert checks["docker_usable_memory"] is False


def test_runtime_endpoint_retries_transient_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[int] = []
    clock = 0.0

    class Response:
        status = 404

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _size: int) -> bytes:
            return b""

    def urlopen(_url: str, *, timeout: float) -> Response:
        nonlocal attempts
        assert 0 < timeout <= 10
        attempts += 1
        if attempts < 3:
            raise urllib.error.URLError("container listener is starting")
        return Response()

    def sleep(seconds: int) -> None:
        nonlocal clock
        sleeps.append(seconds)
        clock += seconds

    monkeypatch.setattr(runtime.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock)
    monkeypatch.setattr(runtime.time, "sleep", sleep)

    assert runtime._http_status("http://127.0.0.1:3103/") == 404
    assert attempts == 3
    assert sleeps == [runtime.RUNTIME_ENDPOINT_RETRY_SECONDS] * 2


def test_runtime_endpoint_fails_after_bounded_readiness_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[int] = []
    clock = 0.0

    def urlopen(_url: str, *, timeout: float) -> object:
        nonlocal attempts
        assert 0 < timeout <= 10
        attempts += 1
        raise urllib.error.URLError("listener never became ready")

    def sleep(seconds: int) -> None:
        nonlocal clock
        sleeps.append(seconds)
        clock += seconds

    monkeypatch.setattr(runtime.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock)
    monkeypatch.setattr(runtime.time, "sleep", sleep)

    with pytest.raises(runtime.RuntimeAdmissionError, match="endpoint unavailable"):
        runtime._http_status("http://127.0.0.1:3103/")

    assert clock == runtime.RUNTIME_ENDPOINT_READY_SECONDS
    assert attempts == len(sleeps) + 1


def test_runtime_endpoint_readiness_wait_propagates_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(
        runtime.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.URLError("listener is starting")
        ),
    )
    monkeypatch.setattr(
        runtime.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        runtime._http_status("http://127.0.0.1:3103/")


def test_runtime_settlement_accepts_shared_rabbitmq_3137_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rabbitmq = (
        Path(__file__).with_name("fixtures") / "rabbitmq-3.13.7-idle.txt"
    ).read_text(encoding="utf-8")
    nuq = (
        "queue_crawl_finished_total|0\n"
        "queue_scrape_backlog_total|0\n"
        "queue_scrape_total|0\n"
    )

    def queue_json(url: str) -> dict[str, object]:
        if url.endswith("queue-status"):
            return {
                "activeJobsInQueue": 0,
                "waitingJobsInQueue": 0,
                "jobsInQueue": 0,
            }
        return {"data": {"crawls": []}}

    def runner(argv, _cwd, _env) -> runtime.CommandResult:
        if "rabbitmqctl" in argv:
            return runtime.CommandResult(0, rabbitmq)
        if "psql" in argv:
            return runtime.CommandResult(0, nuq)
        if argv[:2] == ["/bin/ps", "-Ao"]:
            return runtime.CommandResult(0, "")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(runtime, "_queue_json", queue_json)
    monkeypatch.setattr(
        runtime,
        "_http_status",
        lambda url: 200 if ":3102" in url else 404,
    )

    result = runtime._settlement(runner)

    assert result["rabbitmq_queue_count"] == 4
    assert result["rabbitmq_ready"] == 0
    assert result["rabbitmq_unacknowledged"] == 0
    assert result["nuq"] == {
        "queue_crawl_finished_total": 0,
        "queue_scrape_backlog_total": 0,
        "queue_scrape_total": 0,
    }


def test_container_memory_headroom_is_required() -> None:
    selected, _ = profile()
    public = public_state()
    public["browser"]["cgroup_memory_current"] = 15 * 1024 * 1024 * 1024  # type: ignore[index]
    checks = runtime.evaluate_state(public, selected, "baseline")
    assert checks["browser_memory_headroom"] is False


def test_candidate_requires_requested_cpu_pages_and_pid() -> None:
    selected, _ = profile()
    checks = runtime.evaluate_state(
        public_state(state="candidate"), selected, "candidate"
    )
    assert checks["browser_cpu"] is True
    assert checks["browser_pages"] is True
    assert checks["browser_pids"] is True
    assert checks["api_cpu"] is True


def test_private_overlay_changes_only_page_environment_and_resources() -> None:
    selected, _ = profile()
    current = capture()
    service = runtime._private_overlay(current, selected, "candidate")["services"][
        "playwright-service"
    ]
    assert service["environment"] == {
        "MAX_CONCURRENT_PAGES": "10",
        "PROXY_SERVER": "private",
    }
    assert current.browser_env["MAX_CONCURRENT_PAGES"] == "4"
    assert service["cpus"] == 6
    assert service["pids_limit"] == 768
    assert service["ports"] == ["127.0.0.1:3103:3000"]


def test_receipt_is_private_and_stale_apply_is_rejected(tmp_path: Path) -> None:
    selected, digest = profile()
    baseline = capture()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, baseline)
    receipt["created_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=601)
    ).isoformat()
    receipt["receipt_sha256"] = runtime._hash(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    path = tmp_path / "private" / "receipt.json"
    runtime.write_private(path, receipt)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(runtime.RuntimeAdmissionError, match="stale"):
        runtime.load_fresh_receipt(path, "bold-jll-128")
    runtime.load_fresh_receipt(path, "bold-jll-128", require_fresh=False)


def test_receipt_tampering_is_rejected(tmp_path: Path) -> None:
    selected, digest = profile()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, capture())
    receipt["baseline"]["browser"]["nano_cpus"] = 6_000_000_000
    path = tmp_path / "receipt.json"
    runtime.write_private(path, receipt)
    with pytest.raises(runtime.RuntimeAdmissionError, match="bind"):
        runtime.load_fresh_receipt(path, "bold-jll-128")


def test_unrelated_dirty_tree_is_diagnostic_not_transition_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = capture()
    failed.public["repo"]["dirty"] = True
    failed.public["transition_sha256"] = runtime.transition_fingerprint(failed.public)
    failed.public["snapshot_sha256"] = runtime.snapshot_fingerprint(failed.public)
    monkeypatch.setattr(
        runtime, "capture_runtime", lambda runner=runtime._default_runner: failed
    )
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    path = tmp_path / "tasks" / "tmp" / "cre-capacity-transition-test" / "failed.json"
    runtime.preflight("bold-jll-128", path)
    saved = json.loads(path.read_text())
    assert saved["admitted"] is True
    assert saved["baseline"]["repo"]["dirty"] is True
    assert saved["checks"]["source_inputs_identified"] is True


def test_preflight_rejects_output_outside_controlled_root_without_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    (repo / "tasks" / "tmp").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    target = outside / "receipt.json"
    monkeypatch.setattr(runtime, "REPO_ROOT", repo)
    with pytest.raises(runtime.RuntimeAdmissionError, match="controller output"):
        runtime.preflight("bold-jll-128", target)
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert not target.exists()


def test_dry_run_transition_requires_unchanged_machine_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, digest = profile()
    baseline = capture()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, baseline)
    path = tmp_path / "receipt.json"
    runtime.write_private(path, receipt)
    monkeypatch.setattr(
        runtime, "capture_runtime", lambda runner=runtime._default_runner: baseline
    )
    monkeypatch.setattr(
        runtime, "_canonical_transition_lock", lambda: tmp_path / ".cre.lock"
    )
    monkeypatch.setattr(
        runtime,
        "SharedLock",
        lambda path: pytest.fail("dry-run must not acquire the CRE lock"),
    )
    validations: list[bool] = []
    monkeypatch.setattr(
        runtime,
        "_compose_recreate",
        lambda *args, **kwargs: validations.append(kwargs["execute"]),
    )
    plan = runtime.transition(path, "bold-jll-128", "candidate", execute=False)
    assert plan["execute"] is False
    assert plan["commands"]["api"][0:2] == ["docker", "update"]
    assert plan["canonical_lock"] == str(tmp_path / ".cre.lock")
    assert validations == [False]


def test_dry_run_allows_safe_volatile_usage_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, digest = profile()
    baseline, current = capture(), capture()
    current.public["api"]["cgroup_memory_current"] += 4096
    current.public["snapshot_sha256"] = runtime.snapshot_fingerprint(current.public)
    assert current.public["snapshot_sha256"] != baseline.public["snapshot_sha256"]
    assert current.public["transition_sha256"] == baseline.public["transition_sha256"]
    path = tmp_path / "receipt.json"
    runtime.write_private(
        path, runtime._receipt_payload("bold-jll-128", selected, digest, baseline)
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda runner: current)
    monkeypatch.setattr(runtime, "_compose_recreate", lambda *args, **kwargs: None)
    plan = runtime.transition(path, "bold-jll-128", "candidate", execute=False)
    assert plan["execute"] is False


def test_transition_rejects_execution_input_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, digest = profile()
    baseline, current = capture(), capture()
    current.public["repo"]["execution_inputs_sha256"]["runtime_controller"] = "f" * 64
    current.public["transition_sha256"] = runtime.transition_fingerprint(current.public)
    current.public["snapshot_sha256"] = runtime.snapshot_fingerprint(current.public)
    path = tmp_path / "receipt.json"
    runtime.write_private(
        path, runtime._receipt_payload("bold-jll-128", selected, digest, baseline)
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda runner: current)
    monkeypatch.setattr(
        runtime,
        "_compose_recreate",
        lambda *args, **kwargs: pytest.fail("compose must not be evaluated"),
    )
    with pytest.raises(runtime.RuntimeAdmissionError, match="drifted"):
        runtime.transition(path, "bold-jll-128", "candidate", execute=False)


def test_compose_dry_run_uses_private_files_without_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, _ = profile()
    current = capture()
    seen_overlay: list[Path] = []
    seen_env_files: list[Path] = []
    seen_commands: list[list[str]] = []
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runtime, "COMPOSE_PATH", tmp_path / "docker-compose.yaml")
    monkeypatch.setattr(runtime, "OVERRIDE_PATH", tmp_path / "candidate.yaml")
    (tmp_path / "tasks" / "tmp").mkdir(parents=True)

    def runner(argv: object, cwd: object, env: object) -> runtime.CommandResult:
        values = list(argv)  # type: ignore[arg-type]
        seen_commands.append(values)
        if values[:3] == ["docker", "image", "inspect"]:
            return runtime.CommandResult(
                0, str(current.public["browser"]["image"]) + "\n"
            )
        env_file = Path(values[values.index("--env-file") + 1])
        assert env_file.read_text() == "PLAYWRIGHT_HOST_PORT=3103\n"
        assert env == {
            key: runtime.os.environ[key]
            for key in (
                "PATH",
                "HOME",
                "DOCKER_CONFIG",
                "DOCKER_HOST",
                "DOCKER_CONTEXT",
                "DOCKER_TLS_VERIFY",
                "DOCKER_CERT_PATH",
            )
            if key in runtime.os.environ
        } | {"PLAYWRIGHT_HOST_PORT": "3103"}
        seen_env_files.append(env_file)
        overlay = Path(values[values.index("-f", values.index("-f") + 1) + 1])
        overlay = Path(
            values[
                values.index("-f", values.index("-f", values.index("-f") + 1) + 1) + 1
            ]
        )
        seen_overlay.append(overlay)
        if "config" in values:
            service = json.loads(overlay.read_text())["services"]["playwright-service"]
            service["ports"] = [
                {
                    "host_ip": "127.0.0.1",
                    "mode": "ingress",
                    "protocol": "tcp",
                    "published": "3103",
                    "target": 3000,
                }
            ]
            service["networks"] = {"backend": None}
            service["volumes"] = None
            service["tmpfs"] = ["/tmp/.cache:noexec,nosuid,size=1g"]
            service["security_opt"] = ["no-new-privileges:true"]
            service["cap_drop"] = ["ALL"]
            return runtime.CommandResult(
                0, json.dumps({"services": {"playwright-service": service}})
            )
        return runtime.CommandResult(0, "ok")

    runtime._compose_recreate(current, selected, "candidate", runner, execute=False)
    assert seen_overlay
    assert all(not path.exists() for path in seen_overlay)
    assert seen_env_files
    assert all(not path.exists() for path in seen_env_files)
    assert all("up" not in command for command in seen_commands)
    assert not list((tmp_path / "tasks" / "tmp").glob("cre-capacity-private-*"))


def test_compose_recreate_rejects_topology_drift_before_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, _ = profile()
    current = capture()
    called_up = False
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runtime, "COMPOSE_PATH", tmp_path / "docker-compose.yaml")
    monkeypatch.setattr(runtime, "OVERRIDE_PATH", tmp_path / "candidate.yaml")
    (tmp_path / "tasks" / "tmp").mkdir(parents=True)

    def runner(argv: object, cwd: object, env: object) -> runtime.CommandResult:
        nonlocal called_up
        values = list(argv)  # type: ignore[arg-type]
        if values[:3] == ["docker", "image", "inspect"]:
            return runtime.CommandResult(
                0, str(current.public["browser"]["image"]) + "\n"
            )
        overlay = Path(values[values.index("-f", values.index("-f") + 1) + 1])
        overlay = Path(
            values[
                values.index("-f", values.index("-f", values.index("-f") + 1) + 1) + 1
            ]
        )
        if "config" in values:
            service = json.loads(overlay.read_text())["services"]["playwright-service"]
            service.update(
                {
                    "ports": [
                        {
                            "host_ip": "127.0.0.1",
                            "mode": "ingress",
                            "protocol": "tcp",
                            "published": "3103",
                            "target": 3000,
                        }
                    ],
                    "networks": {"backend": None},
                    "volumes": None,
                    "tmpfs": ["/tmp/.cache:noexec,nosuid,size=1g"],
                    "security_opt": [],
                    "cap_drop": ["ALL"],
                }
            )
            return runtime.CommandResult(
                0, json.dumps({"services": {"playwright-service": service}})
            )
        called_up = True
        return runtime.CommandResult(0, "ok")

    with pytest.raises(runtime.RuntimeAdmissionError, match="unapproved drift"):
        runtime._compose_recreate(current, selected, "candidate", runner)
    assert called_up is False
    assert not list((tmp_path / "tasks" / "tmp").glob("cre-capacity-private-*"))


def test_preservation_rejects_api_identity_or_browser_env_drift() -> None:
    baseline = capture()
    candidate = capture("candidate")
    candidate.public["api"]["id"] = "different"  # type: ignore[index]
    candidate.public["browser"]["env"]["excluding_pages_sha256"] = "different"  # type: ignore[index]
    checks = runtime.preservation_checks(candidate, baseline.public, "candidate")
    assert checks["api_identity"] is False
    assert checks["browser_environment_except_pages"] is False


def test_apply_executes_and_writes_bound_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, digest = profile()
    baseline, candidate = capture(), capture("candidate")
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, baseline)
    receipt_path = tmp_path / "receipt.json"
    admission_path = (
        tmp_path / "tasks" / "tmp" / "cre-capacity-transition-test" / "candidate.json"
    )
    runtime.write_private(receipt_path, receipt)
    captures = iter((baseline, candidate))
    calls: list[tuple[str, str]] = []
    events: list[str] = []
    mock_transition_authority(monkeypatch, tmp_path, events)
    original_fsync_directory = runtime._fsync_directory

    def fsync_directory_with_event(path: Path) -> None:
        events.append(f"fsync-{path.name}")
        original_fsync_directory(path)

    monkeypatch.setattr(runtime, "_fsync_directory", fsync_directory_with_event)

    def capture_with_event(runner: object) -> runtime.RuntimeCapture:
        events.append("capture")
        return next(captures)

    monkeypatch.setattr(runtime, "capture_runtime", capture_with_event)
    monkeypatch.setattr(
        runtime,
        "_compose_recreate",
        lambda current, selected, state, runner, **kwargs: (
            events.append("browser"),
            calls.append(("browser", state)),
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_api_update",
        lambda selected, state, runner, **kwargs: (
            events.append("api"),
            calls.append(("api", state)),
        ),
    )
    original_write = runtime.write_private

    def write_with_event(path: Path, value: object, **kwargs: object) -> None:
        if path == admission_path:
            events.append("admission")
        original_write(path, value, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime, "write_private", write_with_event)
    approval_path = write_approval(
        tmp_path / "approval" / "review.json", receipt, digest
    )
    original_approval = json.loads(approval_path.read_text())
    result = runtime.transition(
        receipt_path,
        "bold-jll-128",
        "candidate",
        execute=True,
        runner=lambda argv, cwd, env: runtime.CommandResult(0, ""),
        admission_out=admission_path,
        approval_path=approval_path,
    )
    admission = json.loads(admission_path.read_text())
    assert result["verified"] is True
    assert calls == [("browser", "candidate"), ("api", "candidate")]
    assert admission["kind"] == runtime.ADMISSION_KIND
    assert admission["transition_receipt_sha256"] == receipt["receipt_sha256"]
    assert admission["review_approval_nonce_sha256"] == runtime._hash("e" * 64)
    assert admission["review_approval_created_at"] == original_approval["created_at"]
    grant_path = Path(admission["review_benchmark_grant_path"])
    assert grant_path.is_absolute()
    assert json.loads(grant_path.read_text()) == {
        "schema_version": 1,
        "kind": runtime.BENCHMARK_GRANT_KIND,
        "profile": "bold-jll-128",
        "config_sha256": digest,
        "transition_receipt_sha256": receipt["receipt_sha256"],
        "source_git_sha": receipt["baseline"]["repo"]["git_sha"],  # type: ignore[index]
        "review_approval_nonce_sha256": runtime._hash("e" * 64),
        "review_approval_created_at": original_approval["created_at"],
        "expires_after_seconds": 600,
        "approved": True,
    }
    assert admission["writes"] == "forbidden"
    assert all(admission["checks"].values())
    assert not approval_path.exists()
    assert events == [
        "lock-acquire",
        "capture",
        "fsync-out",
        "fsync-.capacity-review-consumption",
        "browser",
        "api",
        "capture",
        "admission",
        "lock-release",
    ]


def test_candidate_execute_requires_admission_path_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, digest = profile()
    baseline = capture()
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    receipt_path = tmp_path / "receipt.json"
    runtime.write_private(
        receipt_path,
        runtime._receipt_payload("bold-jll-128", selected, digest, baseline),
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda runner: baseline)
    monkeypatch.setattr(
        runtime,
        "_compose_recreate",
        lambda *args: pytest.fail("mutation must not start"),
    )
    with pytest.raises(runtime.RuntimeAdmissionError, match="admission output"):
        runtime.transition(
            receipt_path,
            "bold-jll-128",
            "candidate",
            execute=True,
            runner=lambda argv, cwd, env: runtime.CommandResult(0, ""),
        )


def test_candidate_execute_requires_bound_review_approval_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, digest = profile()
    baseline = capture()
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    receipt_path = tmp_path / "receipt.json"
    runtime.write_private(
        receipt_path,
        runtime._receipt_payload("bold-jll-128", selected, digest, baseline),
    )
    monkeypatch.setattr(
        runtime,
        "_compose_recreate",
        lambda *args: pytest.fail("mutation must not start"),
    )
    with pytest.raises(
        runtime.RuntimeAdmissionError, match="coordinating-review approval"
    ):
        runtime.transition(
            receipt_path,
            "bold-jll-128",
            "candidate",
            execute=True,
            runner=lambda argv, cwd, env: runtime.CommandResult(0, ""),
            admission_out=(
                tmp_path
                / "tasks"
                / "tmp"
                / "cre-capacity-transition-test"
                / "admission.json"
            ),
        )


def test_failed_candidate_verification_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, digest = profile()
    baseline, invalid_candidate = capture(), capture("candidate")
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    invalid_candidate.public["browser"]["nano_cpus"] = 5_000_000_000
    invalid_candidate.public["transition_sha256"] = runtime.transition_fingerprint(
        invalid_candidate.public
    )
    invalid_candidate.public["snapshot_sha256"] = runtime.snapshot_fingerprint(
        invalid_candidate.public
    )
    receipt_path = tmp_path / "receipt.json"
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, baseline)
    runtime.write_private(
        receipt_path,
        receipt,
    )
    captures = iter(
        (baseline, invalid_candidate, invalid_candidate, baseline, baseline)
    )
    calls: list[tuple[str, str]] = []
    mock_transition_authority(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, "capture_runtime", lambda runner: next(captures))
    monkeypatch.setattr(
        runtime,
        "_compose_recreate",
        lambda current, selected, state, runner, **kwargs: calls.append(
            ("browser", state)
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_api_update",
        lambda selected, state, runner, **kwargs: calls.append(("api", state)),
    )
    approval_path = write_approval(
        tmp_path / "approval" / "review.json", receipt, digest
    )
    copied_approval_path = tmp_path / "approval-copy" / "review.json"
    runtime.write_private(copied_approval_path, json.loads(approval_path.read_text()))
    admission_path = (
        tmp_path / "tasks" / "tmp" / "cre-capacity-transition-test" / "admission.json"
    )
    with pytest.raises(runtime.RuntimeAdmissionError, match="verification failed"):
        runtime.transition(
            receipt_path,
            "bold-jll-128",
            "candidate",
            execute=True,
            runner=lambda argv, cwd, env: runtime.CommandResult(0, ""),
            admission_out=admission_path,
            approval_path=approval_path,
        )
    assert calls == [
        ("browser", "candidate"),
        ("api", "candidate"),
        ("browser", "baseline"),
        ("api", "baseline"),
    ]
    assert not list(
        (tmp_path / "approval").glob(".cre-capacity-benchmark-grant-*.json")
    )
    mutation_calls = list(calls)
    with pytest.raises(runtime.RuntimeAdmissionError, match="already consumed"):
        runtime.transition(
            receipt_path,
            "bold-jll-128",
            "candidate",
            execute=True,
            runner=lambda argv, cwd, env: runtime.CommandResult(0, ""),
            admission_out=admission_path,
            approval_path=copied_approval_path,
        )
    assert calls == mutation_calls
    assert not list(
        (tmp_path / "approval-copy").glob(".cre-capacity-benchmark-grant-*.json")
    )


def test_rollback_accepts_stale_receipt_and_restores_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, digest = profile()
    baseline, candidate, restored = capture(), capture("candidate"), capture()
    mixed = mixed_capture("baseline", "candidate")
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, baseline)
    receipt["created_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    receipt["receipt_sha256"] = runtime._hash(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    receipt_path = tmp_path / "receipt.json"
    runtime.write_private(receipt_path, receipt)
    captures = iter((candidate, mixed, restored))
    calls: list[tuple[str, str]] = []
    mock_transition_authority(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, "capture_runtime", lambda runner: next(captures))
    monkeypatch.setattr(
        runtime,
        "_compose_recreate",
        lambda current, selected, state, runner, **kwargs: calls.append(
            ("browser", state)
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_api_update",
        lambda selected, state, runner, **kwargs: calls.append(("api", state)),
    )
    result = runtime.transition(
        receipt_path,
        "bold-jll-128",
        "baseline",
        execute=True,
        runner=lambda argv, cwd, env: runtime.CommandResult(0, ""),
    )
    assert result["verified"] is True
    assert calls == [("browser", "baseline"), ("api", "baseline")]


def test_review_approval_requires_operator_ownership_and_is_one_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, digest = profile()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, capture())
    approval_path = write_approval(
        tmp_path / "approval" / "review.json", receipt, digest
    )
    runtime._validate_review_authority(approval_path)
    operator_uid = os.geteuid()
    monkeypatch.setattr(runtime.os, "geteuid", lambda: operator_uid + 1)
    with pytest.raises(runtime.RuntimeAdmissionError, match="non-root unswitched"):
        runtime._validate_review_authority(approval_path)
    monkeypatch.setattr(runtime.os, "geteuid", lambda: operator_uid)
    assert approval_path.exists()

    def consume_approval(path: Path, recovery_path: Path) -> bytes:
        try:
            raw = path.read_bytes()
            path.unlink()
        except OSError as exc:
            raise runtime.RuntimeAdmissionError(
                "review approval could not be atomically consumed"
            ) from exc
        return raw

    monkeypatch.setattr(runtime, "_consume_review_approval_bytes", consume_approval)
    monkeypatch.setattr(runtime, "_recover_review_consumption", lambda *a, **kw: None)
    approval, grant_path = runtime.consume_review_approval(
        approval_path, receipt, "bold-jll-128", digest
    )
    assert approval["nonce"] == "e" * 64
    assert grant_path == runtime._benchmark_grant_path(approval_path.parent, approval)
    assert not approval_path.exists()
    with pytest.raises(runtime.RuntimeAdmissionError, match="atomically consumed"):
        runtime.consume_review_approval(approval_path, receipt, "bold-jll-128", digest)


@pytest.mark.parametrize(
    ("unsafe_kind", "message"),
    [
        ("public", "operator-owned mode 0600"),
        ("hardlink", "regular private file"),
        ("symlink", "regular private file"),
        ("public-parent", "operator-owned mode 0700"),
    ],
)
def test_review_approval_rejects_unsafe_file_or_parent(
    unsafe_kind: str,
    message: str,
    tmp_path: Path,
) -> None:
    selected, digest = profile()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, capture())
    approval_path = write_approval(
        tmp_path / "approval" / "review.json", receipt, digest
    )
    if unsafe_kind == "public":
        approval_path.chmod(0o644)
    elif unsafe_kind == "hardlink":
        os.link(approval_path, approval_path.parent / "review-copy.json")
    elif unsafe_kind == "symlink":
        target = approval_path.parent / "review-target.json"
        approval_path.rename(target)
        approval_path.symlink_to(target)
    else:
        approval_path.parent.chmod(0o755)

    with pytest.raises(runtime.RuntimeAdmissionError, match=message):
        runtime._validate_review_authority(approval_path)


@pytest.mark.parametrize(
    ("uid", "euid"),
    [
        (0, 0),
        (501, 0),
        (501, 502),
    ],
)
def test_review_approval_rejects_root_or_switched_account(
    uid: int, euid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime.os, "getuid", lambda: uid)
    monkeypatch.setattr(runtime.os, "geteuid", lambda: euid)

    with pytest.raises(runtime.RuntimeAdmissionError, match="non-root unswitched"):
        runtime._operator_uid()


def test_review_approval_consumer_never_invokes_sudo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, digest = profile()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, capture())
    approval_path = write_approval(
        tmp_path / "approval" / "review.json", receipt, digest
    )
    recovery_path = approval_path.parent / (
        ".cre-capacity-consumption-" + "d" * 64 + ".json"
    )
    raw = approval_path.read_bytes()
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> object:
        calls.append(argv)
        return runtime.subprocess.CompletedProcess(argv, 0, raw, b"")

    monkeypatch.setattr(runtime.subprocess, "run", run)

    assert runtime._consume_review_approval_bytes(approval_path, recovery_path) == raw
    assert calls[0][:2] == ["/usr/bin/python3", "-c"]
    assert "/usr/bin/sudo" not in calls[0]


def test_review_approval_nonce_has_canonical_durable_one_use_marker(
    tmp_path: Path,
) -> None:
    selected, digest = profile()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, capture())
    approval_path = write_approval(
        tmp_path / "approval" / "review.json", receipt, digest
    )
    approval = json.loads(approval_path.read_text())
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"

    marker = runtime._record_review_approval_consumption(
        lock_path, approval, receipt, digest
    )

    value = json.loads(marker.read_text())
    assert marker.parent == tmp_path / "out" / ".capacity-review-consumption"
    assert stat.S_IMODE(marker.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert value["kind"] == "cre_capacity_review_approval_consumption"
    assert value["approval_sha256"] == runtime._hash(approval)
    assert value["config_sha256"] == digest
    assert value["transition_receipt_sha256"] == receipt["receipt_sha256"]
    assert value["source_git_sha"] == approval["source_git_sha"]
    assert value["review_approval_nonce_sha256"] == runtime._hash(approval["nonce"])
    with pytest.raises(runtime.RuntimeAdmissionError, match="already consumed"):
        runtime._record_review_approval_consumption(
            lock_path, approval, receipt, digest
        )


def test_review_benchmark_grant_is_exact_private_and_exclusive(tmp_path: Path) -> None:
    selected, digest = profile()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, capture())
    approval_path = write_approval(
        tmp_path / "approval" / "review.json", receipt, digest
    )
    approval = json.loads(approval_path.read_text())
    grant_path = runtime._write_review_benchmark_grant(approval_path.parent, approval)
    grant = json.loads(grant_path.read_text())
    assert set(grant) == {
        "schema_version",
        "kind",
        "profile",
        "config_sha256",
        "transition_receipt_sha256",
        "source_git_sha",
        "review_approval_nonce_sha256",
        "review_approval_created_at",
        "expires_after_seconds",
        "approved",
    }
    assert grant["review_approval_nonce_sha256"] == runtime._hash("e" * 64)
    assert grant["review_approval_created_at"] == approval["created_at"]
    assert grant["expires_after_seconds"] == 600
    assert "nonce" not in grant
    assert stat.S_IMODE(grant_path.stat().st_mode) == 0o600
    with pytest.raises(runtime.RuntimeAdmissionError, match="exclusively"):
        runtime._write_review_benchmark_grant(approval_path.parent, approval)
    assert json.loads(grant_path.read_text()) == grant


def test_invalid_consumed_approval_destroys_its_review_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, digest = profile()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, capture())
    approval_path = write_approval(
        tmp_path / "approval" / "review.json", receipt, digest
    )
    approval = json.loads(approval_path.read_text())
    approval["profile"] = "wrong-profile"
    raw = json.dumps(approval).encode()
    destroyed: list[Path] = []
    monkeypatch.setattr(
        runtime, "_consume_review_approval_bytes", lambda path, recovery: raw
    )
    monkeypatch.setattr(runtime, "_recover_review_consumption", lambda *a, **kw: None)
    monkeypatch.setattr(
        runtime,
        "_destroy_review_benchmark_grant",
        lambda path: destroyed.append(path),
    )
    with pytest.raises(runtime.RuntimeAdmissionError, match="does not bind"):
        runtime.consume_review_approval(approval_path, receipt, "bold-jll-128", digest)
    assert destroyed == [runtime._benchmark_grant_path(approval_path.parent, approval)]


@pytest.mark.parametrize(
    "browser_state,api_state,subsequent_states,expected_calls",
    [
        ("baseline", "baseline", [], []),
        ("candidate", "baseline", [("baseline", "baseline")], ["browser"]),
        ("baseline", "candidate", [("baseline", "baseline")], ["api"]),
        (
            "candidate",
            "candidate",
            [("baseline", "candidate"), ("baseline", "baseline")],
            ["browser", "api"],
        ),
    ],
)
def test_rollback_is_idempotent_and_resumes_mixed_components(
    browser_state: str,
    api_state: str,
    subsequent_states: list[tuple[str, str]],
    expected_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected, digest = profile()
    baseline = capture()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, baseline)
    current = mixed_capture(browser_state, api_state)
    subsequent = iter(mixed_capture(browser, api) for browser, api in subsequent_states)
    calls: list[str] = []
    monkeypatch.setattr(runtime, "capture_runtime", lambda runner: next(subsequent))
    monkeypatch.setattr(
        runtime,
        "_compose_recreate",
        lambda *args, **kwargs: calls.append("browser"),
    )
    monkeypatch.setattr(
        runtime,
        "_api_update",
        lambda *args, **kwargs: calls.append("api"),
    )
    restored = runtime._restore_baseline(
        current,
        receipt,
        selected,
        lambda argv, cwd, env: runtime.CommandResult(0, ""),
    )
    assert runtime._component_states(restored, receipt, selected) == (
        "baseline",
        "baseline",
    )
    assert calls == expected_calls


def test_rollback_refuses_unrelated_runtime_drift() -> None:
    selected, digest = profile()
    baseline = capture()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, baseline)
    current = mixed_capture("candidate", "baseline")
    current.public["browser"]["network_mode"] = "other"  # type: ignore[index]
    with pytest.raises(runtime.RuntimeAdmissionError, match="unrelated drift"):
        runtime._restore_baseline(
            current,
            receipt,
            selected,
            lambda argv, cwd, env: runtime.CommandResult(0, ""),
        )


def test_rollback_ignores_diagnostic_dirty_change_after_preflight() -> None:
    selected, digest = profile()
    baseline = capture()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, baseline)
    current = capture()
    current.public["repo"]["dirty"] = True  # type: ignore[index]
    restored = runtime._restore_baseline(
        current,
        receipt,
        selected,
        lambda argv, cwd, env: runtime.CommandResult(0, ""),
    )
    assert restored is current


def test_automatic_compensation_ignores_diagnostic_dirty_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected, digest = profile()
    baseline = capture()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, baseline)
    current = capture("candidate")
    current.public["repo"]["dirty"] = True  # type: ignore[index]
    calls: list[str] = []
    monkeypatch.setattr(
        runtime,
        "_compose_recreate",
        lambda *args, **kwargs: calls.append("browser"),
    )
    monkeypatch.setattr(
        runtime,
        "_api_update",
        lambda *args, **kwargs: calls.append("api"),
    )
    restored = capture()
    restored.public["repo"]["dirty"] = True  # type: ignore[index]
    monkeypatch.setattr(runtime, "capture_runtime", lambda runner: restored)
    result = runtime._compensate_candidate_attempt(
        current,
        receipt,
        selected,
        lambda argv, cwd, env: runtime.CommandResult(0, ""),
    )
    assert result is restored
    assert calls == ["browser", "api"]


def test_automatic_compensation_attempts_api_after_browser_rollback_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected, digest = profile()
    baseline = capture()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, baseline)
    current = mixed_capture("candidate", "candidate")
    calls: list[str] = []

    def failed_browser(*args: object, **kwargs: object) -> None:
        calls.append("browser")
        raise runtime.RuntimeMutationError("browser rollback uncertain")

    monkeypatch.setattr(runtime, "_compose_recreate", failed_browser)
    monkeypatch.setattr(
        runtime,
        "_api_update",
        lambda *args, **kwargs: calls.append("api"),
    )
    monkeypatch.setattr(runtime, "capture_runtime", lambda runner: capture())
    with pytest.raises(
        runtime.RuntimeCompensationError, match="baseline verified"
    ) as caught:
        runtime._compensate_candidate_attempt(
            current,
            receipt,
            selected,
            lambda argv, cwd, env: runtime.CommandResult(0, ""),
        )
    assert caught.value.baseline_verified is True
    assert isinstance(caught.value.__cause__, runtime.RuntimeMutationError)
    assert calls == ["browser", "api"]


@pytest.mark.parametrize(
    "browser_state,api_state,expected_commands",
    [
        ("baseline", "baseline", set()),
        ("candidate", "baseline", {"browser"}),
        ("baseline", "candidate", {"api"}),
        ("candidate", "candidate", {"browser", "api"}),
    ],
)
def test_rollback_dry_run_reports_only_required_component_commands(
    browser_state: str,
    api_state: str,
    expected_commands: set[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected, digest = profile()
    baseline = capture()
    receipt_path = tmp_path / "receipt.json"
    runtime.write_private(
        receipt_path,
        runtime._receipt_payload("bold-jll-128", selected, digest, baseline),
    )
    current = mixed_capture(browser_state, api_state)
    monkeypatch.setattr(runtime, "capture_runtime", lambda runner: current)
    monkeypatch.setattr(
        runtime, "_canonical_transition_lock", lambda: tmp_path / ".cre.lock"
    )
    monkeypatch.setattr(runtime, "_compose_recreate", lambda *args, **kwargs: None)
    plan = runtime.transition(receipt_path, "bold-jll-128", "baseline", execute=False)
    assert set(plan["commands"]) == expected_commands
    assert plan["observed_components"] == {
        "browser": browser_state,
        "api": api_state,
    }


def test_compose_cleanup_failure_after_up_is_a_mutation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, _ = profile()
    current = capture()
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runtime, "COMPOSE_PATH", tmp_path / "docker-compose.yaml")
    monkeypatch.setattr(runtime, "OVERRIDE_PATH", tmp_path / "candidate.yaml")
    (tmp_path / "tasks" / "tmp").mkdir(parents=True)

    def runner(argv: object, cwd: object, env: object) -> runtime.CommandResult:
        values = list(argv)  # type: ignore[arg-type]
        if values[:3] == ["docker", "image", "inspect"]:
            return runtime.CommandResult(
                0,
                str(current.public["browser"]["image"]) + "\n",  # type: ignore[index]
            )
        overlay = Path(values[-1])
        if "config" in values:
            overlay = Path(values[values.index("-f", values.index("-f") + 1) + 1])
            overlay = Path(
                values[
                    values.index("-f", values.index("-f", values.index("-f") + 1) + 1)
                    + 1
                ]
            )
            service = json.loads(overlay.read_text())["services"]["playwright-service"]
            service.update(
                {
                    "ports": [
                        {
                            "host_ip": "127.0.0.1",
                            "mode": "ingress",
                            "protocol": "tcp",
                            "published": "3103",
                            "target": 3000,
                        }
                    ],
                    "networks": {"backend": None},
                    "volumes": None,
                    "tmpfs": ["/tmp/.cache:noexec,nosuid,size=1g"],
                    "security_opt": ["no-new-privileges:true"],
                    "cap_drop": ["ALL"],
                }
            )
            return runtime.CommandResult(
                0, json.dumps({"services": {"playwright-service": service}})
            )
        private_parent = Path(values[values.index("--env-file") + 1]).parent
        (private_parent / "cleanup-blocker").write_text("held")
        return runtime.CommandResult(0, "ok")

    with pytest.raises(runtime.RuntimeMutationError, match="cleanup failed"):
        runtime._compose_recreate(current, selected, "candidate", runner)


def test_keyboard_interrupt_after_mutation_request_compensates_before_reraise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, digest = profile()
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    baseline = capture()
    partial = mixed_capture("candidate", "baseline")
    restored = capture()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, baseline)
    receipt_path = tmp_path / "receipt.json"
    approval_path = write_approval(
        tmp_path / "approval" / "review.json", receipt, digest
    )
    admission_path = (
        tmp_path / "tasks" / "tmp" / "cre-capacity-transition-test" / "out.json"
    )
    runtime.write_private(receipt_path, receipt)
    events: list[str] = []
    mock_transition_authority(monkeypatch, tmp_path, events)
    captures = iter((baseline, partial, restored))
    monkeypatch.setattr(runtime, "capture_runtime", lambda runner: next(captures))

    def compose(
        current: object, selected: object, state: str, runner: object, **kwargs: object
    ) -> None:
        events.append(f"browser-{state}")
        if state == "candidate":
            kwargs["mutation_observer"]()  # type: ignore[operator]
            raise KeyboardInterrupt("simulated SIGTERM")

    monkeypatch.setattr(runtime, "_compose_recreate", compose)
    monkeypatch.setattr(
        runtime,
        "_api_update",
        lambda selected, state, runner, **kwargs: events.append(f"api-{state}"),
    )
    with pytest.raises(KeyboardInterrupt, match="simulated SIGTERM"):
        runtime.transition(
            receipt_path,
            "bold-jll-128",
            "candidate",
            execute=True,
            runner=lambda argv, cwd, env: runtime.CommandResult(0, ""),
            admission_out=admission_path,
            approval_path=approval_path,
        )
    assert events == [
        "lock-acquire",
        "browser-candidate",
        "grant-destroy",
        "browser-baseline",
        "api-baseline",
        "lock-release",
    ]
    assert not admission_path.exists()
    assert not approval_path.exists()


def test_candidate_overlay_cleanup_error_still_compensates_both_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, digest = profile()
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    baseline = capture()
    partial = mixed_capture("candidate", "baseline")
    restored = capture()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, baseline)
    receipt_path = tmp_path / "receipt.json"
    approval_path = write_approval(
        tmp_path / "approval" / "review.json", receipt, digest
    )
    runtime.write_private(receipt_path, receipt)
    events: list[str] = []
    mock_transition_authority(monkeypatch, tmp_path, events)
    captures = iter((baseline, partial, restored))
    monkeypatch.setattr(runtime, "capture_runtime", lambda runner: next(captures))

    def compose(
        current: object, selected: object, state: str, runner: object, **kwargs: object
    ) -> None:
        events.append(f"browser-{state}")
        if state == "candidate":
            kwargs["mutation_observer"]()  # type: ignore[operator]
            raise runtime.RuntimeOverlayCleanupError("cleanup failed")

    monkeypatch.setattr(runtime, "_compose_recreate", compose)
    monkeypatch.setattr(
        runtime,
        "_api_update",
        lambda selected, state, runner, **kwargs: events.append(f"api-{state}"),
    )
    with pytest.raises(runtime.RuntimeOverlayCleanupError, match="cleanup failed"):
        runtime.transition(
            receipt_path,
            "bold-jll-128",
            "candidate",
            execute=True,
            runner=lambda argv, cwd, env: runtime.CommandResult(0, ""),
            admission_out=(
                tmp_path / "tasks" / "tmp" / "cre-capacity-transition-test" / "out.json"
            ),
            approval_path=approval_path,
        )
    assert events == [
        "lock-acquire",
        "browser-candidate",
        "grant-destroy",
        "browser-baseline",
        "api-baseline",
        "lock-release",
    ]


def test_sigterm_handler_uses_keyboard_interrupt_cleanup_path() -> None:
    with pytest.raises(KeyboardInterrupt, match="signal"):
        runtime._signal_as_interrupt(runtime.signal.SIGTERM, None)


def test_shared_lock_source_is_bound_and_its_drift_is_rejected() -> None:
    assert (
        runtime.EXECUTION_INPUTS["shared_lock"]
        == Path(runtime.checkpoint_refresh.__file__).resolve()
    )
    selected, digest = profile()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, capture())
    changed = capture()
    changed.public["repo"]["dirty"] = True
    changed.public["repo"]["execution_inputs_sha256"]["shared_lock"] = "f" * 64
    with pytest.raises(runtime.RuntimeAdmissionError, match="unrelated drift"):
        runtime._component_states(changed, receipt, selected)


@pytest.mark.parametrize(
    "key,value",
    [
        ("created_at", "not-a-time"),
        ("created_at", "2026-09-14T00:00:00"),
        ("expires_after_seconds", 601),
        ("expires_after_seconds", 600.0),
    ],
)
def test_grant_rejects_invalid_original_expiry(
    key: str, value: object, tmp_path: Path
) -> None:
    selected, digest = profile()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, capture())
    approval_path = write_approval(
        tmp_path / "approval" / "review.json", receipt, digest
    )
    approval = json.loads(approval_path.read_text())
    approval[key] = value
    with pytest.raises(runtime.RuntimeAdmissionError):
        runtime._write_review_benchmark_grant(approval_path.parent, approval)
    assert not list(approval_path.parent.glob(".cre-capacity-benchmark-grant-*"))


def run_review_helper_offline(
    monkeypatch: pytest.MonkeyPatch, source: str, *args: Path | str
) -> bytes:
    """Exercise same-user helper code on temporary files."""
    original_lstat = os.lstat
    original_fstat = os.fstat

    output = io.BytesIO()
    with monkeypatch.context() as isolated:
        isolated.setattr(os, "lstat", original_lstat)
        isolated.setattr(os, "fstat", original_fstat)
        isolated.setattr(sys, "argv", ["helper", *(str(arg) for arg in args)])
        isolated.setattr(sys, "stdout", SimpleNamespace(buffer=output))
        isolated.setattr(runtime.signal, "signal", lambda *a: None)
        try:
            exec(compile(source, "<offline-review-helper>", "exec"), {})  # noqa: S102 - checked-in helper tested offline
        except SystemExit as exc:
            if exc.code != 0:
                raise
    return output.getvalue()


@pytest.mark.parametrize("failure", ["signal", "consumed-delete"])
def test_review_helper_destroys_grant_on_failure_after_creation(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, digest = profile()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, capture())
    approval_path = write_approval(
        tmp_path / "approval" / "review.json", receipt, digest
    )
    approval = json.loads(approval_path.read_text())
    grant_path = runtime._benchmark_grant_path(approval_path.parent, approval)
    recovery = approval_path.parent / (
        ".cre-capacity-consumption-" + "d" * 64 + ".json"
    )
    original_close, original_unlink = os.close, os.unlink

    if failure == "signal":

        def interrupted_close(fd: int) -> None:
            original_close(fd)
            if grant_path.exists() and grant_path.stat().st_size > 0:
                raise KeyboardInterrupt("helper SIGTERM after grant creation")

        monkeypatch.setattr(os, "close", interrupted_close)
        expected = KeyboardInterrupt
    else:

        def failed_consumed_unlink(path: str, *args: object, **kwargs: object) -> None:
            if ".consumed-" in str(path):
                raise PermissionError("consumed approval cannot be deleted")
            original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", failed_consumed_unlink)
        expected = PermissionError
    with pytest.raises(expected):
        run_review_helper_offline(
            monkeypatch, runtime.REVIEW_APPROVAL_CONSUMER, approval_path, recovery
        )
    assert not grant_path.exists()


@pytest.mark.parametrize("failure", [KeyboardInterrupt, runtime.RuntimeAdmissionError])
def test_consumer_recovers_grant_when_helper_response_is_lost(
    failure: type[BaseException], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, digest = profile()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, capture())
    approval_path = write_approval(
        tmp_path / "approval" / "review.json", receipt, digest
    )
    approval = json.loads(approval_path.read_text())
    grant_path = runtime._benchmark_grant_path(approval_path.parent, approval)

    def interrupted_consumer(path: Path, recovery_path: Path) -> bytes:
        run_review_helper_offline(
            monkeypatch, runtime.REVIEW_APPROVAL_CONSUMER, path, recovery_path
        )
        assert grant_path.exists()
        grant = json.loads(grant_path.read_text())
        assert grant["review_approval_created_at"] == approval["created_at"]
        assert grant["expires_after_seconds"] == 600
        raise failure("helper response unavailable")

    monkeypatch.setattr(runtime, "_consume_review_approval_bytes", interrupted_consumer)
    monkeypatch.setattr(
        runtime,
        "_recover_review_consumption",
        lambda path, *, discard: run_review_helper_offline(
            monkeypatch,
            runtime.REVIEW_CONSUMPTION_RECOVERY,
            path,
            "discard" if discard else "release",
        ),
    )
    with pytest.raises(failure, match="helper response unavailable"):
        runtime.consume_review_approval(approval_path, receipt, "bold-jll-128", digest)
    assert not grant_path.exists()
    assert not list(approval_path.parent.glob(".cre-capacity-consumption-*"))


def test_pending_signal_is_delivered_after_grant_ownership_is_assigned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, digest = profile()
    baseline = capture()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, baseline)
    receipt_path = tmp_path / "receipt.json"
    runtime.write_private(receipt_path, receipt)
    approval_path = write_approval(
        tmp_path / "approval" / "review.json", receipt, digest
    )
    events: list[str] = []
    mock_transition_authority(monkeypatch, tmp_path, events)
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runtime, "capture_runtime", lambda runner: baseline)
    original_consume = runtime.consume_review_approval

    def consume_then_cancel(*args: object) -> tuple[dict[str, object], Path]:
        result = original_consume(*args)
        handler = runtime.signal.getsignal(runtime.signal.SIGTERM)
        handler(runtime.signal.SIGTERM, None)
        return result

    monkeypatch.setattr(runtime, "consume_review_approval", consume_then_cancel)
    monkeypatch.setattr(
        runtime, "_compose_recreate", lambda *a, **kw: pytest.fail("cancelled apply")
    )
    with pytest.raises(KeyboardInterrupt, match="signal"):
        runtime.transition(
            receipt_path,
            "bold-jll-128",
            "candidate",
            execute=True,
            admission_out=tmp_path / "tasks/tmp/cre-capacity-transition-test/out.json",
            approval_path=approval_path,
        )
    assert events == ["lock-acquire", "grant-destroy", "lock-release"]
    assert not list(approval_path.parent.glob(".cre-capacity-benchmark-grant-*"))


def test_failed_full_capture_still_compensates_both_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, digest = profile()
    baseline = capture()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, baseline)
    receipt_path = tmp_path / "receipt.json"
    runtime.write_private(receipt_path, receipt)
    approval_path = write_approval(
        tmp_path / "approval" / "review.json", receipt, digest
    )
    events: list[str] = []
    mock_transition_authority(monkeypatch, tmp_path, events)
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    captures = iter(
        (baseline, runtime.RuntimeAdmissionError("browser is stopped"), baseline)
    )

    def live_capture(runner: object) -> runtime.RuntimeCapture:
        value = next(captures)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(runtime, "capture_runtime", live_capture)
    monkeypatch.setattr(
        runtime, "_capture_recovery_state", lambda before, runner: baseline
    )

    def compose(
        current: object, selected: object, state: str, runner: object, **kwargs: object
    ) -> None:
        events.append(f"browser-{state}")
        if state == "candidate":
            kwargs["mutation_observer"]()
            raise runtime.RuntimeMutationError("browser recreation failed")

    monkeypatch.setattr(runtime, "_compose_recreate", compose)
    monkeypatch.setattr(
        runtime,
        "_api_update",
        lambda selected, state, runner: events.append(f"api-{state}"),
    )
    with pytest.raises(runtime.RuntimeMutationError, match="browser recreation failed"):
        runtime.transition(
            receipt_path,
            "bold-jll-128",
            "candidate",
            execute=True,
            admission_out=tmp_path / "tasks/tmp/cre-capacity-transition-test/out.json",
            approval_path=approval_path,
        )
    assert events == [
        "lock-acquire",
        "browser-candidate",
        "grant-destroy",
        "browser-baseline",
        "api-baseline",
        "lock-release",
    ]


@pytest.mark.parametrize(
    "browser_present,drift",
    [
        (False, None),
        (True, None),
        (False, "api-id"),
        (True, "browser-env"),
        (False, "source"),
        (False, "host"),
    ],
)
def test_recovery_inspection_handles_missing_or_stopped_browser_but_rejects_drift(
    browser_present: bool, drift: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = capture()

    def container(component: str) -> dict[str, object]:
        public = before.public[component]
        env = before.api_env if component == "api" else before.browser_env
        mounts = [] if component == "browser" else [{"Source": "/approved-api-volume"}]
        public["env"] = runtime._env_receipt(env)
        public["mounts_sha256"] = runtime._hash(mounts)
        return {
            "Name": "/"
            + (
                runtime.API_CONTAINER
                if component == "api"
                else runtime.BROWSER_CONTAINER
            ),
            "Id": public["id"],
            "Image": public["image"],
            "Config": {"Env": [f"{key}={value}" for key, value in env.items()]},
            "Mounts": mounts,
            "HostConfig": {
                "NanoCpus": public["nano_cpus"],
                "Memory": public["memory_bytes"],
                "MemorySwap": public["memory_bytes"] + public["swap_bytes"],
                "PidsLimit": public["pids_limit"],
                "ShmSize": public["shm_bytes"],
                "PortBindings": public["port_bindings"],
                "NetworkMode": public["network_mode"],
                "SecurityOpt": public["security_opt"],
                "CapDrop": public["cap_drop"],
            },
        }

    api, browser = container("api"), container("browser")
    selected, digest = profile()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, before)
    if drift == "api-id":
        api["Id"] = "unexpected-replacement-api"
    if drift == "browser-env":
        browser["Config"]["Env"].append("UNAPPROVED_ENV=value")
    source_state = dict(before.public["repo"])
    if drift == "source":
        source_state["git_sha"] = "f" * 40
    monkeypatch.setattr(runtime, "_capture_source_state", lambda runner: source_state)

    def runner(argv: object, cwd: object, env: object) -> runtime.CommandResult:
        command = list(argv)
        if command[:3] == ["docker", "ps", "-a"]:
            names = [runtime.API_CONTAINER]
            if browser_present:
                names.append(runtime.BROWSER_CONTAINER)
            return runtime.CommandResult(0, "\n".join(names))
        if command[:2] == ["docker", "inspect"]:
            return runtime.CommandResult(
                0, json.dumps([api, browser] if browser_present else [api])
            )
        if command == ["orb", "config", "get", "memory_mib"]:
            return runtime.CommandResult(0, "32768")
        if command == ["orb", "status"]:
            return runtime.CommandResult(0, "Running")
        if command == ["docker", "context", "show"]:
            return runtime.CommandResult(
                0, "changed-context" if drift == "host" else "orbstack"
            )
        pytest.fail(f"recovery must not require liveness: {command}")

    observed = runtime._capture_recovery_state(before, runner)
    calls: list[str] = []
    monkeypatch.setattr(
        runtime, "_compose_recreate", lambda *a, **kw: calls.append("browser")
    )
    monkeypatch.setattr(runtime, "_api_update", lambda *a, **kw: calls.append("api"))
    monkeypatch.setattr(runtime, "capture_runtime", lambda runner: before)
    if drift is not None:
        with pytest.raises(
            runtime.RuntimeAdmissionError, match="unrelated runtime drift"
        ):
            runtime._compensate_candidate_attempt(observed, receipt, selected, runner)
        assert calls == []
    else:
        assert observed.browser_env == before.browser_env
        assert (
            runtime._compensate_candidate_attempt(observed, receipt, selected, runner)
            is before
        )
        assert calls == ["browser", "api"]


def test_compensation_requires_full_final_health_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = capture()
    selected, digest = profile()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, before)
    final = capture()
    final.public["settlement"]["browser_root_status"] = 500
    calls: list[str] = []
    monkeypatch.setattr(
        runtime, "_compose_recreate", lambda *a, **kw: calls.append("browser")
    )
    monkeypatch.setattr(runtime, "_api_update", lambda *a, **kw: calls.append("api"))
    monkeypatch.setattr(runtime, "capture_runtime", lambda runner: final)
    with pytest.raises(runtime.RuntimeCompensationError, match="could not be verified"):
        runtime._compensate_candidate_attempt(
            before, receipt, selected, lambda *a: runtime.CommandResult(0, "")
        )
    assert calls == ["browser", "api"]
