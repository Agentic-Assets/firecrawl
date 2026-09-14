"""Pure and mocked contracts for the CRE capacity runtime controller."""

from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import cre_capacity_experiment as experiment
import cre_capacity_runtime as runtime


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
        "approved_by": "root-review",
        "approved": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_after_seconds": runtime.RECEIPT_MAX_AGE_SECONDS,
    }
    runtime.write_private(path, approval)
    return path


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
    validations: list[bool] = []
    monkeypatch.setattr(
        runtime,
        "_compose_recreate",
        lambda *args, **kwargs: validations.append(kwargs["execute"]),
    )
    plan = runtime.transition(path, "bold-jll-128", "candidate", execute=False)
    assert plan["execute"] is False
    assert plan["commands"]["api"][0:2] == ["docker", "update"]
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
    monkeypatch.setattr(runtime, "capture_runtime", lambda runner: next(captures))
    monkeypatch.setattr(
        runtime,
        "_compose_recreate",
        lambda current, selected, state, runner: calls.append(("browser", state)),
    )
    monkeypatch.setattr(
        runtime,
        "_api_update",
        lambda selected, state, runner: calls.append(("api", state)),
    )
    result = runtime.transition(
        receipt_path,
        "bold-jll-128",
        "candidate",
        execute=True,
        runner=lambda argv, cwd, env: runtime.CommandResult(0, ""),
        admission_out=admission_path,
        approval_path=write_approval(
            tmp_path / "approval" / "root.json", receipt, digest
        ),
    )
    admission = json.loads(admission_path.read_text())
    assert result["verified"] is True
    assert calls == [("browser", "candidate"), ("api", "candidate")]
    assert admission["kind"] == runtime.ADMISSION_KIND
    assert admission["transition_receipt_sha256"] == receipt["receipt_sha256"]
    assert admission["writes"] == "forbidden"
    assert all(admission["checks"].values())


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


def test_candidate_execute_requires_bound_root_approval_before_mutation(
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
    with pytest.raises(runtime.RuntimeAdmissionError, match="root-review approval"):
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
    captures = iter((baseline, invalid_candidate, invalid_candidate, baseline))
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(runtime, "capture_runtime", lambda runner: next(captures))
    monkeypatch.setattr(
        runtime,
        "_compose_recreate",
        lambda current, selected, state, runner: calls.append(("browser", state)),
    )
    monkeypatch.setattr(
        runtime,
        "_api_update",
        lambda selected, state, runner: calls.append(("api", state)),
    )
    with pytest.raises(runtime.RuntimeAdmissionError, match="verification failed"):
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
            approval_path=write_approval(
                tmp_path / "approval" / "root.json", receipt, digest
            ),
        )
    assert calls == [
        ("browser", "candidate"),
        ("api", "candidate"),
        ("api", "baseline"),
        ("browser", "baseline"),
    ]


def test_rollback_accepts_stale_receipt_and_restores_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected, digest = profile()
    baseline, candidate, restored = capture(), capture("candidate"), capture()
    receipt = runtime._receipt_payload("bold-jll-128", selected, digest, baseline)
    receipt["created_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    receipt["receipt_sha256"] = runtime._hash(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    receipt_path = tmp_path / "receipt.json"
    runtime.write_private(receipt_path, receipt)
    captures = iter((candidate, restored))
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(runtime, "capture_runtime", lambda runner: next(captures))
    monkeypatch.setattr(
        runtime,
        "_compose_recreate",
        lambda current, selected, state, runner: calls.append(("browser", state)),
    )
    monkeypatch.setattr(
        runtime,
        "_api_update",
        lambda selected, state, runner: calls.append(("api", state)),
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
