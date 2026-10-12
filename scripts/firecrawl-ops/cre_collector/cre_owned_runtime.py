"""Opt-in foreground ownership of an explicitly selected local CRE runtime.

This wrapper never starts/restarts containers, purges queues, or implies rollback.
The supported series command remains unchanged unless this wrapper is invoked.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from cre_performance import utc_now

MAX_BYTES = 64 * 1024
MAX_CONTAINERS = 12
INSPECT_FORMAT = (
    '{"id":{{json .Id}},"image":{{json .Image}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"restart_policy":{{json .HostConfig.RestartPolicy}},'
    '"started_at":{{json .State.StartedAt}},'
    '"restart_count":{{json .RestartCount}},"running":{{json .State.Running}},'
    '"restarting":{{json .State.Restarting}},"dead":{{json .State.Dead}},'
    '"oom_killed":{{json .State.OOMKilled}},"exit_code":{{json .State.ExitCode}},'
    '"finished_at":{{json .State.FinishedAt}},"ports":{{json .NetworkSettings.Ports}}}'
)
STATIC_FIELDS = ("id", "image", "project", "service", "restart_policy")
EPOCH_FIELDS = ("started_at", "restart_count")
STATE_FIELDS = (
    "running",
    "restarting",
    "dead",
    "oom_killed",
    "exit_code",
    "finished_at",
)
LOCK_ROOT = Path("/tmp") / f"cre-owned-runtime-{os.getuid()}"


class RuntimeUnavailable(ValueError):
    """Required ownership, admission, or fresh evidence is unavailable."""


def read_private_json(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        meta = os.fstat(fd)
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_uid != os.getuid()
            or meta.st_nlink != 1
            or meta.st_mode & 0o077
            or meta.st_size > MAX_BYTES
        ):
            raise RuntimeUnavailable("unsafe runtime identity file")
        raw = os.read(fd, MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise RuntimeUnavailable("runtime identity file exceeds cap")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise RuntimeUnavailable("invalid runtime identity file")
        return value
    finally:
        os.close(fd)


def write_private_json(path: Path, value: dict) -> None:
    """Create once; never replace an operator's identity or historical evidence."""
    raw = (json.dumps(value, allow_nan=False, sort_keys=True) + "\n").encode()
    if len(raw) > MAX_BYTES:
        raise RuntimeUnavailable("runtime evidence exceeds cap")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def bounded_command(argv: Sequence[str], timeout: float) -> tuple[int, str]:
    # Filtered Docker metadata only. A file avoids unbounded in-memory capture;
    # stderr (which may contain operator configuration) is never persisted.
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            list(argv),
            stdout=output,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        if output.tell() > MAX_BYTES:
            raise RuntimeUnavailable("Docker output exceeds cap")
        output.seek(0)
        return result.returncode, output.read(MAX_BYTES + 1).decode("utf-8")


def docker_command(argv: Sequence[str], timeout: float) -> tuple[int, str]:
    return bounded_command(["docker", *argv], timeout)


@contextlib.contextmanager
def runtime_lease(identity: dict):
    """Exclude duplicate cooperative owners across clones and identity copies."""
    LOCK_ROOT.mkdir(mode=0o700, exist_ok=True)
    meta = LOCK_ROOT.lstat()
    if (
        not stat.S_ISDIR(meta.st_mode)
        or meta.st_uid != os.getuid()
        or meta.st_mode & 0o077
    ):
        raise RuntimeUnavailable("unsafe runtime lease directory")
    descriptors = []
    try:
        for row in sorted(identity["containers"], key=lambda row: row["id"]):
            # Context aliases and overlapping service selections must not admit
            # two owners for the same daemon/container.
            key = hashlib.sha256(
                json.dumps(
                    [
                        identity["daemon"]["id"],
                        row["id"],
                    ]
                ).encode()
            ).hexdigest()
            fd = os.open(
                LOCK_ROOT / key,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
            descriptors.append(fd)
            meta = os.fstat(fd)
            if (
                not stat.S_ISREG(meta.st_mode)
                or meta.st_uid != os.getuid()
                or meta.st_nlink != 1
            ):
                raise RuntimeUnavailable("unsafe runtime lease")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        for fd in descriptors:
            os.close(fd)


class DockerRuntime:
    def __init__(self, context: str, runner: Callable = docker_command):
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", context) is None:
            raise RuntimeUnavailable("invalid Docker context")
        self.context = context
        self.runner = runner

    def command(self, args: Sequence[str], deadline: float) -> tuple[int, str]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeUnavailable("runtime deadline exhausted")
        return self.runner(["--context", self.context, *args], min(5.0, remaining))

    def identity(self, deadline: float) -> dict:
        rc, raw = self.command(
            [
                "context",
                "inspect",
                self.context,
                "--format",
                "{{json .Endpoints.docker.Host}}",
            ],
            deadline,
        )
        endpoint = json.loads(raw) if rc == 0 else None
        if not isinstance(endpoint, str) or not endpoint.startswith("unix:///"):
            raise RuntimeUnavailable("only local Unix Docker contexts are supported")
        rc, raw = self.command(
            [
                "info",
                "--format",
                '{"id":{{json .ID}},"version":{{json .ServerVersion}}}',
            ],
            deadline,
        )
        daemon = json.loads(raw) if rc == 0 else None
        if not isinstance(daemon, dict) or any(
            not isinstance(daemon.get(key), str) or not 1 <= len(daemon[key]) <= 256
            for key in ("id", "version")
        ):
            raise RuntimeUnavailable("Docker daemon identity unavailable")
        return {"context": self.context, "endpoint": endpoint, **daemon}

    def inspect(self, ids: list[str], deadline: float) -> dict[str, dict]:
        _rc, raw = self.command(["inspect", "--format", INSPECT_FORMAT, *ids], deadline)
        rows = [json.loads(line) for line in raw.splitlines()]
        result = {}
        for row in rows:
            if (
                not isinstance(row, dict)
                or row.get("id") not in ids
                or row["id"] in result
            ):
                raise RuntimeUnavailable("invalid container identity output")
            result[row["id"]] = row
        # Docker may return nonzero with valid output for a subset of IDs.
        # Missing IDs remain unknown; never discover a replacement by name.
        return result


def validate_identity(value: dict) -> dict:
    if value.get("schema_version") != 1 or value.get("kind") != "cre_owned_runtime":
        raise RuntimeUnavailable("unsupported runtime identity")
    DockerRuntime(value.get("daemon", {}).get("context", ""))
    project = value.get("project")
    rows = value.get("containers")
    if (
        not isinstance(project, str)
        or re.fullmatch(r"firecrawl-cre-[a-z0-9][a-z0-9_-]{0,95}", project) is None
        or not isinstance(rows, list)
        or not 1 <= len(rows) <= MAX_CONTAINERS
    ):
        raise RuntimeUnavailable("explicit dedicated CRE runtime selection required")
    seen_ids, seen_services = set(), set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or re.fullmatch(r"[0-9a-f]{64}", str(row.get("id"))) is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", str(row.get("image"))) is None
            or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", str(row.get("service")))
            is None
            or row.get("project") != project
            or not isinstance(row.get("restart_policy"), dict)
            or row["restart_policy"].get("Name")
            not in {"", "no", "always", "unless-stopped", "on-failure"}
            or type(row["restart_policy"].get("MaximumRetryCount")) is not int
            or row["restart_policy"]["MaximumRetryCount"] < 0
            or not isinstance(row.get("started_at"), str)
            or len(row["started_at"]) > 64
            or type(row.get("restart_count")) is not int
            or row["restart_count"] < 0
            or not valid_state(row)
            or not isinstance(row.get("ports"), dict)
            or row["id"] in seen_ids
            or row["service"] in seen_services
        ):
            raise RuntimeUnavailable("invalid selected container binding")
        seen_ids.add(row["id"])
        seen_services.add(row["service"])
    return value


def valid_state(row: dict) -> bool:
    return (
        all(type(row.get(key)) is bool for key in STATE_FIELDS[:4])
        and type(row.get("exit_code")) is int
        and isinstance(row.get("finished_at"), str)
        and len(row["finished_at"]) <= 64
    )


def stable_state(row: dict | None, *, running: bool) -> bool:
    return (
        row is not None
        and valid_state(row)
        and row["running"] is running
        and row["restarting"] is False
        and row["dead"] is False
    )


def capture_runtime(
    runtime: DockerRuntime, project: str, selected: dict[str, str]
) -> dict:
    if not 1 <= len(selected) <= MAX_CONTAINERS or any(
        re.fullmatch(r"[0-9a-f]{64}", item) is None for item in selected.values()
    ):
        raise RuntimeUnavailable("full explicit container IDs required")
    deadline = time.monotonic() + 15
    daemon = runtime.identity(deadline)
    observed = runtime.inspect(list(selected.values()), deadline)
    if any(
        observed.get(item, {}).get("service") != service
        for service, item in selected.items()
    ):
        raise RuntimeUnavailable("selected service identity mismatch")
    value = validate_identity(
        {
            "schema_version": 1,
            "kind": "cre_owned_runtime",
            "captured_at": utc_now(),
            "daemon": daemon,
            "project": project,
            "containers": list(observed.values()),
        }
    )
    if not all(stable_state(row, running=True) for row in value["containers"]):
        raise RuntimeUnavailable("selected runtime is not running")
    return value


def same_container(expected: dict, actual: dict | None, *, epoch: bool = False) -> bool:
    fields = STATIC_FIELDS + EPOCH_FIELDS if epoch else STATIC_FIELDS
    return actual is not None and all(
        expected.get(key) == actual.get(key) for key in fields
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise RuntimeUnavailable("runtime health redirect refused")


def health_worker(url: str) -> dict:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"/health", "/v2/team/queue-status"}
    ):
        raise RuntimeUnavailable("invalid runtime health URL")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(url, timeout=1.0) as response:
        raw = response.read(16 * 1024 + 1)
        if response.status != 200 or len(raw) > 16 * 1024:
            raise RuntimeUnavailable("runtime health unavailable")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeUnavailable("invalid runtime health")
    return value


def read_health(url: str, deadline: float) -> dict:
    # Separate bounded process also bounds trickling response/header reads.
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeUnavailable("runtime admission deadline exhausted")
    rc, raw = bounded_command(
        [sys.executable, str(Path(__file__).resolve()), "_health", url],
        min(2.0, remaining),
    )
    value = json.loads(raw) if rc == 0 else None
    if not isinstance(value, dict):
        raise RuntimeUnavailable("runtime health unavailable")
    return value


def bound_endpoint(origin: str, row: dict, container_port: str) -> str:
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.port is None
    ):
        raise RuntimeUnavailable("literal loopback runtime endpoint required")
    hosts = (
        {"127.0.0.1", "0.0.0.0", ""}
        if parsed.hostname == "127.0.0.1"
        else {"::1", "::"}
    )
    bindings = row["ports"].get(container_port) or []
    if not any(
        binding.get("HostIp") in hosts and binding.get("HostPort") == str(parsed.port)
        for binding in bindings
    ):
        raise RuntimeUnavailable("runtime endpoint is not bound to selected container")
    return origin.rstrip("/")


def admit_runtime(
    runtime: DockerRuntime,
    identity: dict,
    api: str,
    browser: str,
    health: Callable = read_health,
) -> None:
    validate_identity(identity)
    deadline = time.monotonic() + 15
    if runtime.identity(deadline) != identity["daemon"]:
        raise RuntimeUnavailable("Docker daemon identity changed")
    rows = runtime.inspect([row["id"] for row in identity["containers"]], deadline)
    if any(
        not same_container(row, rows.get(row["id"]), epoch=True)
        or rows[row["id"]].get("ports") != row["ports"]
        or not stable_state(rows[row["id"]], running=True)
        for row in identity["containers"]
    ):
        raise RuntimeUnavailable("selected runtime changed since pinning")
    services = {row["service"]: row for row in rows.values()}
    if not {"api", "playwright-service"} <= services.keys():
        raise RuntimeUnavailable(
            "API and browser ownership are required for series admission"
        )
    api = bound_endpoint(api, services["api"], "3002/tcp")
    browser = bound_endpoint(browser, services["playwright-service"], "3000/tcp")
    queue = health(api + "/v2/team/queue-status", deadline)
    queue = queue.get("data", queue)
    page = health(browser + "/health", deadline)
    if not isinstance(queue, dict) or any(
        type(queue.get(key)) is not int or queue[key] != 0
        for key in ("jobsInQueue", "activeJobsInQueue", "waitingJobsInQueue")
    ):
        raise RuntimeUnavailable("owned API queue is not provably idle")
    if (
        page.get("status") != "healthy"
        or type(page.get("activePages")) is not int
        or page["activePages"] != 0
    ):
        raise RuntimeUnavailable("owned browser is not provably idle")


def stop_runtime(
    runtime: DockerRuntime, identity: dict, *, seconds: float = 30.0
) -> dict:
    """Stop only pinned IDs; always describe every selected service, even unknown."""
    validate_identity(identity)
    if not math.isfinite(seconds) or not 15 <= seconds <= 120:
        raise RuntimeUnavailable("shutdown budget must be between 15 and 120 seconds")
    started = time.monotonic()
    deadline = started + seconds
    results = {
        row["id"]: {
            "id": row["id"],
            "service": row["service"],
            "state": "unknown",
            "unknown_reason": "fresh_stopped_state_not_confirmed",
        }
        for row in identity["containers"]
    }
    errors = []
    stop_rc = None
    try:
        if runtime.identity(deadline) != identity["daemon"]:
            raise RuntimeUnavailable("Docker daemon identity changed")
        before = runtime.inspect(list(results), deadline)
        owned = [
            row
            for row in identity["containers"]
            if same_container(row, before.get(row["id"]), epoch=True)
        ]
        if len(owned) != len(results):
            errors.append("container_identity_unavailable")
        if owned:
            try:
                # Reserve time for fresh readback even if the stop CLI stalls.
                stop_rc, _ = runtime.command(
                    ["stop", "--time", "3", *[row["id"] for row in owned]],
                    deadline - 10,
                )
                if stop_rc:
                    errors.append("stop_nonzero")
            except Exception as exc:  # noqa: BLE001 - cleanup must retain unknown evidence for any boundary failure
                errors.append("stop_" + type(exc).__name__)
        first = runtime.inspect(list(results), deadline)
        if deadline - time.monotonic() > 0.1:
            time.sleep(0.1)
        if runtime.identity(deadline) != identity["daemon"]:
            raise RuntimeUnavailable("Docker daemon identity changed during shutdown")
        second = runtime.inspect(list(results), deadline)
        for expected in identity["containers"]:
            one, two = first.get(expected["id"]), second.get(expected["id"])
            results[expected["id"]]["observations"] = [
                {key: row.get(key) for key in STATE_FIELDS + EPOCH_FIELDS}
                if row is not None
                else None
                for row in (one, two)
            ]
        for expected in owned:
            one, two = first.get(expected["id"]), second.get(expected["id"])
            if (
                same_container(expected, one, epoch=True)
                and same_container(expected, two, epoch=True)
                and stable_state(one, running=False)
                and stable_state(two, running=False)
            ):
                results[expected["id"]].update(
                    state="stopped", unknown_reason=None, observed_at=utc_now()
                )
    except Exception as exc:  # noqa: BLE001 - cleanup must retain unknown evidence for any boundary failure
        errors.append(type(exc).__name__)
    return {
        "finished_at": utc_now(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "ok": all(row["state"] == "stopped" for row in results.values()),
        "stop_rc": stop_rc,
        "errors": errors,
        "containers": list(results.values()),
        "scope": "fresh_observation_only_not_queue_deletion_or_rollback",
    }


def supervise(
    runtime: DockerRuntime,
    identity: dict,
    command: list[str],
    output: Path,
    *,
    api: str,
    browser: str,
    shutdown_seconds: float = 30,
) -> int:
    """Foreground supervision; cancellation initiates cleanup independently of child exit."""
    validate_identity(identity)
    if not math.isfinite(shutdown_seconds) or not 15 <= shutdown_seconds <= 120:
        raise RuntimeUnavailable("shutdown budget must be between 15 and 120 seconds")
    with runtime_lease(identity):
        return _supervise(
            runtime,
            identity,
            command,
            output,
            api=api,
            browser=browser,
            shutdown_seconds=shutdown_seconds,
        )


def _supervise(
    runtime: DockerRuntime,
    identity: dict,
    command: list[str],
    output: Path,
    *,
    api: str,
    browser: str,
    shutdown_seconds: float,
) -> int:
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    write_private_json(output / "identity.json", identity)
    received = None

    def receive(signum, _frame):
        nonlocal received
        received = received or signum

    previous = {
        sig: signal.signal(sig, receive) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    child = None
    collector_rc = None
    failure = None
    child_reaped = False
    try:
        admit_runtime(runtime, identity, api, browser)
        if received is None:
            environment = dict(
                os.environ,
                FIRECRAWL_API_URL=api,
                API_URL=api,
                COMPOSE_PROJECT_NAME=identity["project"],
                DOCKER_CONTEXT=identity["daemon"]["context"],
            )
            for key in ("DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
                environment.pop(key, None)
            # Keep canonical progress visible; the series owns its command logs.
            child = subprocess.Popen(command, start_new_session=True, env=environment)
            while received is None and child.poll() is None:
                time.sleep(0.1)
            if received is not None and child.poll() is None:
                child.send_signal(received)
            collector_rc = child.poll()
            child_reaped = collector_rc is not None
    except Exception as exc:  # noqa: BLE001 - cleanup must retain unknown evidence for any boundary failure
        failure = type(exc).__name__
    finally:
        # Runtime containment starts now, without waiting for an unresponsive
        # collector. Detached checkpoints remain the series owner's concern.
        cleanup = stop_runtime(runtime, identity, seconds=shutdown_seconds)
        if child is not None and not child_reaped:
            try:
                collector_rc = child.wait(timeout=1)
                child_reaped = True
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                    collector_rc = child.wait(timeout=2)
                    child_reaped = True
                except (OSError, subprocess.SubprocessError):
                    pass
            except (OSError, subprocess.SubprocessError):
                # Failure to reap is distinct from a successful runtime stop.
                pass
        result = {
            "schema_version": 1,
            "kind": "cre_owned_runtime_exit",
            "collector_rc": collector_rc,
            "collector_started": child is not None,
            "collector_leader_reaped": child_reaped,
            "signal": received,
            "failure_category": failure,
            "runtime_cleanup": cleanup,
            "identity_sha256": hashlib.sha256(
                json.dumps(identity, sort_keys=True).encode()
            ).hexdigest(),
            "finished_at": utc_now(),
        }
        try:
            write_private_json(output / "exit.json", result)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    if received is not None:
        return 128 + received
    if collector_rc is not None and collector_rc != 0:
        return collector_rc if collector_rc > 0 else 128 - collector_rc
    return 0 if child_reaped and cleanup["ok"] and failure is None else 1


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None and sys.argv[1:2] == ["_health"]:
        print(json.dumps(health_worker(sys.argv[2]), allow_nan=False))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    pin = sub.add_parser(
        "pin", help="capture explicit dedicated runtime ownership without mutation"
    )
    pin.add_argument("--context", required=True)
    pin.add_argument("--project", required=True)
    pin.add_argument(
        "--container", action="append", required=True, help="service=full-container-ID"
    )
    pin.add_argument("--out", type=Path, required=True)
    run = sub.add_parser(
        "run", help="opt in to stop/readback around the existing series command"
    )
    run.add_argument("--identity", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True, help="new evidence directory")
    run.add_argument("--api-url", required=True)
    run.add_argument("--browser-url", required=True)
    run.add_argument("--shutdown-seconds", type=float, default=30)
    run.add_argument("series_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.action == "pin":
        pairs = [item.split("=", 1) for item in args.container]
        if any(len(pair) != 2 for pair in pairs) or len(dict(pairs)) != len(pairs):
            parser.error("each unique service needs one explicit full container ID")
        identity = capture_runtime(
            DockerRuntime(args.context), args.project, dict(pairs)
        )
        write_private_json(args.out, identity)
        return 0
    identity = validate_identity(read_private_json(args.identity))
    series_args = (
        args.series_args[1:] if args.series_args[:1] == ["--"] else args.series_args
    )
    command = [
        sys.executable,
        str(Path(__file__).with_name("cre_checkpoint_series.py")),
        *series_args,
    ]
    return supervise(
        DockerRuntime(identity["daemon"]["context"]),
        identity,
        command,
        args.out,
        api=args.api_url,
        browser=args.browser_url,
        shutdown_seconds=args.shutdown_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
