"""C10 sidecar lifecycle and host-child process contracts."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from capacity_c10 import contracts
from capacity_c10.host_session import C10HostExecutionSession, DockerComposeSidecar


def test_compose_overlay_is_rendered_before_start_and_owner_env_is_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    environments: list[object] = []

    class Result:
        def __init__(self, code: int, stdout: str = "") -> None:
            self.returncode, self.stdout = code, stdout

    def fake_run(command: list[str], **kwargs: object) -> Result:
        calls.append(command)
        environments.append(kwargs.get("env"))
        if "config" in command:
            return Result(
                0,
                '{"services":{"playwright-service-c10":{"cpus":"2","environment":{"MAX_CONCURRENT_PAGES":"4","C10_PROFILE_SHA256":"a"},"ports":[{"host_ip":"127.0.0.1","published":"4444","target":3004}]}}}',
            )
        return Result(0)

    monkeypatch.setattr("capacity_c10.host_sidecar.subprocess.run", fake_run)
    lifecycle = DockerComposeSidecar(tmp_path)
    lifecycle.start(
        {
            "C10_BROWSER_CPUS": "2",
            "MAX_CONCURRENT_PAGES": "4",
            "C10_PROFILE_SHA256": "a",
        },
        4444,
        time.monotonic() + 10,
    )
    lifecycle.stop(time.monotonic() + 10)

    assert all("docker-compose.yaml" in call for call in calls)
    assert any("config" in call for call in calls)
    assert any("rm" in call for call in calls)
    assert any("ps" in call for call in calls)
    assert environments[0] is environments[1] is environments[2] is environments[3]
    assert lifecycle._env_file is None


def test_signed_lease_intervals_reject_false_capacity_saturation() -> None:
    def evidence(
        start: int, end: int, active: int, capacity: int = 4
    ) -> dict[str, object]:
        return {
            "leaseStartMonotonicNs": str(start),
            "leaseEndMonotonicNs": str(end),
            "observedActivePages": active,
            "configuredCapacity": capacity,
        }

    serial = [evidence(index * 10, index * 10 + 9, 3) for index in range(16)]
    with pytest.raises(contracts.C10Error, match="exact P0/P1 target"):
        C10HostExecutionSession._verify_saturation(serial, 4)

    saturated = [evidence(0, 10, 4) for _ in range(4)] + [
        evidence(20 + index * 10, 29 + index * 10, 1) for index in range(12)
    ]
    C10HostExecutionSession._verify_saturation(saturated, 4)


def test_compose_partial_start_uses_same_environment_for_stop_and_quiescence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], object]] = []

    class Result:
        def __init__(self, code: int, stdout: str = "") -> None:
            self.returncode, self.stdout = code, stdout

    def fake_run(command: list[str], **kwargs: object) -> Result:
        calls.append((command, kwargs.get("env")))
        if "config" in command:
            return Result(
                0,
                '{"services":{"playwright-service-c10":{"cpus":"2","environment":{"MAX_CONCURRENT_PAGES":"4","C10_PROFILE_SHA256":"a"},"ports":[{"host_ip":"127.0.0.1","published":"4444","target":3004}]}}}',
            )
        if "up" in command:
            return Result(1)
        return Result(0)

    monkeypatch.setattr("capacity_c10.host_sidecar.subprocess.run", fake_run)
    lifecycle = DockerComposeSidecar(tmp_path)
    with pytest.raises(contracts.C10Error, match="startup failed"):
        lifecycle.start(
            {
                "C10_BROWSER_CPUS": "2",
                "MAX_CONCURRENT_PAGES": "4",
                "C10_PROFILE_SHA256": "a",
            },
            4444,
            time.monotonic() + 10,
        )
    assert any("rm" in command for command, _ in calls)
    assert any("ps" in command for command, _ in calls)
    assert len({id(environment) for _, environment in calls}) == 1
    assert lifecycle._env_file is None


def test_hung_child_is_process_group_killed_at_the_host_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class HungChild:
        pid = 7654
        returncode = None
        stdin = None
        stdout = None
        stderr = None

        def kill(self) -> None:
            raise AssertionError("process-group kill must be preferred")

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "capacity_c10.host_orchestration.subprocess.Popen",
        lambda *_args, **_kwargs: HungChild(),
    )
    monkeypatch.setattr(
        "capacity_c10.host_orchestration.os.killpg",
        lambda pid, signal: killed.append((pid, signal)),
    )
    session = object.__new__(C10HostExecutionSession)
    session.repo_root = tmp_path
    with pytest.raises(contracts.C10Error, match="pipes are unavailable"):
        session._run_child({}, time.monotonic() + 1)
    assert killed == [(7654, 9)]


def test_compose_stop_failure_removes_private_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(
        "capacity_c10.host_sidecar.subprocess.run", lambda *_args, **_kwargs: Result()
    )
    lifecycle = DockerComposeSidecar(tmp_path)
    env_file = tmp_path / "private.env"
    env_file.write_text("x=y\n", encoding="utf-8")
    lifecycle._env_file = env_file
    lifecycle._compose_env = {"C10_BROWSER_PRIVATE_ENV_FILE": str(env_file)}
    with pytest.raises(contracts.C10Error, match="removal was not confirmed"):
        lifecycle.stop(time.monotonic() + 10)
    assert lifecycle._env_file is None
    assert lifecycle._compose_env is None
    assert not env_file.exists()


def test_compose_rejects_a_stopped_container_with_secret_config_residue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Result:
        def __init__(self, stdout: str = "") -> None:
            self.returncode, self.stdout = 0, stdout

    def fake_run(command: list[str], **_: object) -> Result:
        return (
            Result('{"State":"exited","Config":{"Env":["C10_SECRET=x"]}}')
            if "ps" in command
            else Result()
        )

    monkeypatch.setattr("capacity_c10.host_sidecar.subprocess.run", fake_run)
    lifecycle = DockerComposeSidecar(tmp_path)
    env_file = tmp_path / "private.env"
    env_file.write_text("x=y\n", encoding="utf-8")
    lifecycle._env_file = env_file
    lifecycle._compose_env = {"C10_BROWSER_PRIVATE_ENV_FILE": str(env_file)}
    with pytest.raises(contracts.C10Error, match="Config.Env absence"):
        lifecycle.stop(time.monotonic() + 10)
    assert lifecycle._env_file is None
    assert not env_file.exists()
