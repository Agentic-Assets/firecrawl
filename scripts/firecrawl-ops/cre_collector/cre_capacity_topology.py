"""Public, Compose-derived loopback topology for CRE capacity tooling."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from cre_capacity_errors import RuntimeAdmissionError

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class CommandResult:
    """Minimal, public result shape for local topology commands."""

    returncode: int
    stdout: str


CommandRunner = Callable[
    [Sequence[str], Path | None, Mapping[str, str] | None], CommandResult
]


def default_command_runner(
    argv: Sequence[str],
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    *,
    timeout_seconds: float = 120,
) -> CommandResult:
    """Run a bounded local command for public topology resolution."""
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeAdmissionError(
            f"runtime command unavailable: {Path(argv[0]).name}"
        ) from exc
    return CommandResult(completed.returncode, completed.stdout)


def compose_loopback_endpoints(
    runner: CommandRunner,
    *,
    repo_root: Path = REPO_ROOT,
) -> dict[str, str]:
    """Resolve API and browser loopback URLs from rendered Compose topology."""
    try:
        result = runner(
            ["docker", "compose", "config", "--format", "json"], repo_root, None
        )
        if result.returncode != 0:
            raise RuntimeAdmissionError("runtime command failed: docker")
        configured = json.loads(result.stdout)
        services = configured["services"]
        endpoints: dict[str, str] = {}
        for label, service_name in (("api", "api"), ("browser", "playwright-service")):
            service = services[service_name]
            environment = service["environment"]
            target = int(environment["PORT"])
            match = next(
                (
                    port
                    for port in service["ports"]
                    if int(port["target"]) == target
                    and str(port.get("protocol", "tcp")) == "tcp"
                    and str(port.get("host_ip", "127.0.0.1"))
                    in {"127.0.0.1", "0.0.0.0", "::", ""}
                ),
                None,
            )
            if not isinstance(match, Mapping):
                raise KeyError(service_name)
            endpoints[label] = f"http://127.0.0.1:{int(match['published'])}"
    except RuntimeAdmissionError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeAdmissionError(
            "resolved Compose loopback endpoints are invalid"
        ) from exc
    return endpoints
