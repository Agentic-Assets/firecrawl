"""C10 sidecar lifecycle and host-child process contracts."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from capacity_c10 import contracts
from capacity_c10.host_orchestration import _C10HostTransport
from capacity_c10.host_session import DockerComposeSidecar


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
                '{"services":{"playwright-service-c10":{"cpus":"2","image":"firecrawl-playwright-service-c10:local","environment":{"MAX_CONCURRENT_PAGES":"4","C10_PROFILE_SHA256":"a"},"ports":[{"host_ip":"127.0.0.1","published":"4444","target":3004}]}}}',
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
        _C10HostTransport._verify_saturation(serial, 4)

    saturated = [evidence(0, 10, 4) for _ in range(4)] + [
        evidence(20 + index * 10, 29 + index * 10, 1) for index in range(12)
    ]
    _C10HostTransport._verify_saturation(saturated, 4)


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
                '{"services":{"playwright-service-c10":{"cpus":"2","image":"firecrawl-playwright-service-c10:local","environment":{"MAX_CONCURRENT_PAGES":"4","C10_PROFILE_SHA256":"a"},"ports":[{"host_ip":"127.0.0.1","published":"4444","target":3004}]}}}',
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
    session = object.__new__(_C10HostTransport)
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


def test_stop_with_expired_run_deadline_still_issues_rm_and_ps_and_removes_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1-a regression: at HEAD, ``stop`` bounded its subprocess calls with the
    caller's (possibly expired) run deadline and raised before issuing ``rm``
    at all. ``stop`` must use its own independent teardown budget instead, so
    an expired run deadline still issues both compose calls and proves
    absence."""
    calls: list[list[str]] = []

    class Result:
        def __init__(self, code: int, stdout: str = "") -> None:
            self.returncode, self.stdout = code, stdout

    def fake_run(command: list[str], **kwargs: object) -> Result:
        calls.append(command)
        return Result(0)

    monkeypatch.setattr("capacity_c10.host_sidecar.subprocess.run", fake_run)
    lifecycle = DockerComposeSidecar(tmp_path)
    env_file = tmp_path / "private.env"
    env_file.write_text("x=y\n", encoding="utf-8")
    lifecycle._env_file = env_file
    lifecycle._compose_env = {"C10_BROWSER_PRIVATE_ENV_FILE": str(env_file)}

    lifecycle.stop(time.monotonic() - 5)

    assert any("rm" in call for call in calls)
    assert any("ps" in call for call in calls)
    assert lifecycle._env_file is None
    assert lifecycle._compose_env is None
    assert not env_file.exists()


def test_start_failure_with_nearly_expired_run_deadline_still_issues_rm_and_ps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Partial-start cleanup (``start``'s ``except BaseException`` calling
    ``self.stop(deadline)``) must succeed even when the run deadline used for
    ``config``/``up`` is nearly expired, because teardown now uses its own
    independent budget rather than the caller's deadline."""
    calls: list[list[str]] = []

    class Result:
        def __init__(self, code: int, stdout: str = "") -> None:
            self.returncode, self.stdout = code, stdout

    def fake_run(command: list[str], **kwargs: object) -> Result:
        calls.append(command)
        if "config" in command:
            return Result(
                0,
                '{"services":{"playwright-service-c10":{"cpus":"2","image":"firecrawl-playwright-service-c10:local","environment":{"MAX_CONCURRENT_PAGES":"4","C10_PROFILE_SHA256":"a"},"ports":[{"host_ip":"127.0.0.1","published":"4444","target":3004}]}}}',
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
            # `config`/`up` still need positive time, so the deadline can't be
            # fully expired here, but it must be too tight for a caller-bound
            # teardown to complete its own rm+ps round trip.
            time.monotonic() + 0.05,
        )
    assert any("rm" in command for command in calls)
    assert any("ps" in command for command in calls)
    assert lifecycle._env_file is None


def test_stop_retries_once_after_a_failed_rm_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rm_calls: list[list[str]] = []

    class Result:
        def __init__(self, code: int, stdout: str = "") -> None:
            self.returncode, self.stdout = code, stdout

    def fake_run(command: list[str], **kwargs: object) -> Result:
        if "rm" in command:
            rm_calls.append(command)
            return Result(1 if len(rm_calls) == 1 else 0)
        return Result(0)

    monkeypatch.setattr("capacity_c10.host_sidecar.subprocess.run", fake_run)
    lifecycle = DockerComposeSidecar(tmp_path)
    env_file = tmp_path / "private.env"
    env_file.write_text("x=y\n", encoding="utf-8")
    lifecycle._env_file = env_file
    lifecycle._compose_env = {"C10_BROWSER_PRIVATE_ENV_FILE": str(env_file)}

    lifecycle.stop(time.monotonic() + 10)

    assert len(rm_calls) == 2
    assert lifecycle._env_file is None


def test_stop_all_attempts_failing_raises_with_compose_project_and_removes_env(
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
    lifecycle.compose_project = "c10-deadbeefdeadbeefdeadbeef"

    with pytest.raises(contracts.C10Error, match=r"compose project c10-"):
        lifecycle.stop(time.monotonic() + 10)

    assert lifecycle.compose_project == "c10-deadbeefdeadbeefdeadbeef"
    assert lifecycle._env_file is None
    assert not env_file.exists()


def test_start_issues_up_with_no_build_and_pull_never(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    class Result:
        def __init__(self, code: int, stdout: str = "") -> None:
            self.returncode, self.stdout = code, stdout

    def fake_run(command: list[str], **kwargs: object) -> Result:
        calls.append(command)
        if "config" in command:
            return Result(
                0,
                '{"services":{"playwright-service-c10":{"cpus":"2","image":"firecrawl-playwright-service-c10:local","environment":{"MAX_CONCURRENT_PAGES":"4","C10_PROFILE_SHA256":"a"},"ports":[{"host_ip":"127.0.0.1","published":"4444","target":3004}]}}}',
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

    up_calls = [command for command in calls if "up" in command]
    assert len(up_calls) == 1
    assert "--no-build" in up_calls[0]
    assert "--pull" in up_calls[0]
    assert "never" in up_calls[0]


def test_start_failure_with_unrecoverable_cleanup_leaves_teardown_unproven_for_next_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When `up` fails and the internal partial-start cleanup's own `rm` also
    fails on every attempt, `start` raises "partial-start cleanup failed" (as
    before). The sticky `_teardown_unproven` flag must then make a later,
    separate `stop()` call raise "removal was not confirmed" instead of
    returning silently, since absence was never proven."""

    class Result:
        def __init__(self, code: int, stdout: str = "") -> None:
            self.returncode, self.stdout = code, stdout

    def fake_run(command: list[str], **kwargs: object) -> Result:
        if "config" in command:
            return Result(
                0,
                '{"services":{"playwright-service-c10":{"cpus":"2","image":"firecrawl-playwright-service-c10:local","environment":{"MAX_CONCURRENT_PAGES":"4","C10_PROFILE_SHA256":"a"},"ports":[{"host_ip":"127.0.0.1","published":"4444","target":3004}]}}}',
            )
        # `up` fails, and the subsequent internal cleanup `rm` also fails on
        # every attempt: partial-start cleanup cannot prove absence.
        return Result(1)

    monkeypatch.setattr("capacity_c10.host_sidecar.subprocess.run", fake_run)
    lifecycle = DockerComposeSidecar(tmp_path)
    with pytest.raises(contracts.C10Error, match="partial-start cleanup failed"):
        lifecycle.start(
            {
                "C10_BROWSER_CPUS": "2",
                "MAX_CONCURRENT_PAGES": "4",
                "C10_PROFILE_SHA256": "a",
            },
            4444,
            time.monotonic() + 10,
        )
    with pytest.raises(contracts.C10Error, match="removal was not confirmed"):
        lifecycle.stop()


def test_second_stop_after_a_successful_stop_returns_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Result:
        def __init__(self, code: int, stdout: str = "") -> None:
            self.returncode, self.stdout = code, stdout

    def fake_run(command: list[str], **kwargs: object) -> Result:
        if "config" in command:
            return Result(
                0,
                '{"services":{"playwright-service-c10":{"cpus":"2","image":"firecrawl-playwright-service-c10:local","environment":{"MAX_CONCURRENT_PAGES":"4","C10_PROFILE_SHA256":"a"},"ports":[{"host_ip":"127.0.0.1","published":"4444","target":3004}]}}}',
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
    # Must return cleanly: the successful stop above cleared
    # `_teardown_unproven`, so a redundant second stop is a silent no-op.
    lifecycle.stop()


def test_rendered_identity_rejects_missing_or_wrong_image(tmp_path: Path) -> None:
    expected_environment = {
        "C10_BROWSER_CPUS": "2",
        "MAX_CONCURRENT_PAGES": "4",
        "C10_PROFILE_SHA256": "a",
    }
    ports = '[{"host_ip":"127.0.0.1","published":"4444","target":3004}]'
    environment_block = '{"MAX_CONCURRENT_PAGES":"4","C10_PROFILE_SHA256":"a"}'

    missing_image_rendered = (
        '{"services":{"playwright-service-c10":'
        f'{{"cpus":"2","environment":{environment_block},"ports":{ports}}}}}}}'
    )
    assert not DockerComposeSidecar._rendered_identity(
        missing_image_rendered, 4444, expected_environment
    )

    wrong_image_rendered = (
        '{"services":{"playwright-service-c10":'
        f'{{"cpus":"2","image":"some-other-image:latest","environment":{environment_block},"ports":{ports}}}}}}}'
    )
    assert not DockerComposeSidecar._rendered_identity(
        wrong_image_rendered, 4444, expected_environment
    )

    correct_image_rendered = (
        '{"services":{"playwright-service-c10":'
        f'{{"cpus":"2","image":"firecrawl-playwright-service-c10:local","environment":{environment_block},"ports":{ports}}}}}}}'
    )
    assert DockerComposeSidecar._rendered_identity(
        correct_image_rendered, 4444, expected_environment
    )
