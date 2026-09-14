"""Fail-closed runtime controller for the named CRE capacity experiment.

Preflight observations and verification are read-only; preflight writes only a
private receipt. Apply and rollback are dry-run by default. Candidate mutation
requires ``--execute``, a fresh machine receipt, and a one-use, externally
created operator-owned approval file. No command reads the repository ``.env``
file.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cre_capacity_experiment as experiment
import cre_checkpoint_refresh as checkpoint_refresh
from cre_checkpoint_refresh import (
    LockHeldError,
    SharedLock,
    canonical_shared_lock_dir,
)

SCHEMA_VERSION = 1
RECEIPT_KIND = "cre_capacity_runtime_transition"
ADMISSION_KIND = "cre_capacity_runtime_admission"
APPROVAL_KIND = "cre_capacity_review_approval"
BENCHMARK_GRANT_KIND = "cre_capacity_review_benchmark_grant"
RECEIPT_MAX_AGE_SECONDS = 600
RUNTIME_ENDPOINT_READY_SECONDS = 30
RUNTIME_ENDPOINT_RETRY_SECONDS = 1
API_CONTAINER = "firecrawl-api-1"
BROWSER_CONTAINER = "firecrawl-playwright-service-1"
RABBIT_CONTAINER = "firecrawl-rabbitmq-1"
NUQ_CONTAINER = "firecrawl-nuq-postgres-1"
REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yaml"
OVERRIDE_PATH = Path(__file__).with_name("cre_capacity_bold.override.yaml")
EXECUTION_INPUTS = {
    "runtime_controller": Path(__file__).resolve(),
    "profile_planner": Path(experiment.__file__).resolve(),
    "profile_config": experiment.DEFAULT_CONFIG.resolve(),
    "compose": COMPOSE_PATH,
    "candidate_override": OVERRIDE_PATH,
    "shared_lock": Path(checkpoint_refresh.__file__).resolve(),
}
PRIVATE_PAGE_KEY = "MAX_CONCURRENT_PAGES"
SHA_PATTERN = re.compile(r"[0-9a-f]{40,64}\Z")
NONCE_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
REVIEW_APPROVAL_MAX_BYTES = 64 * 1024
REVIEW_APPROVAL_CONSUMER = r"""
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import sys
from datetime import datetime, timezone

path = os.path.abspath(sys.argv[1])
parent = os.path.dirname(path)
recovery_path = os.path.abspath(sys.argv[2])
uid = os.getuid()
euid = os.geteuid()
if uid == 0 or euid == 0 or uid != euid:
    raise SystemExit("approval consumer requires a non-root unswitched operating account")
if (os.path.dirname(recovery_path) != parent or not re.fullmatch(
        r"\.cre-capacity-consumption-[0-9a-f]{64}\.json",
        os.path.basename(recovery_path))):
    raise SystemExit("consumption recovery path is invalid")
def interrupted(signum, frame):
    raise KeyboardInterrupt("approval consumption interrupted")
for signum in (signal.SIGINT, signal.SIGTERM):
    signal.signal(signum, interrupted)
grant_path = None
grant_created = False
file_stat = os.lstat(path)
parent_stat = os.lstat(parent)
if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
    raise SystemExit("approval is not a singly linked regular file")
if file_stat.st_uid != euid or stat.S_IMODE(file_stat.st_mode) != 0o600:
    raise SystemExit("approval is not operator-owned mode 0600")
if (not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != euid
        or stat.S_IMODE(parent_stat.st_mode) != 0o700):
    raise SystemExit("approval parent is not operator-owned mode 0700")
consumed = os.path.join(
    parent,
    "." + os.path.basename(path) + ".consumed-" + secrets.token_hex(16),
)
os.rename(path, consumed)
try:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(consumed, flags)
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != euid
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_nlink != 1):
            raise SystemExit("consumed approval ownership changed")
        chunks = []
        remaining = 65537
        while remaining:
            chunk = os.read(fd, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > 65536:
            raise SystemExit("approval is too large")
    finally:
        os.close(fd)
    try:
        approval = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SystemExit("approval JSON is invalid")
    sha = re.compile(r"[0-9a-f]{64}\Z")
    source_sha = re.compile(r"[0-9a-f]{40,64}\Z")
    if (not isinstance(approval, dict)
            or approval.get("schema_version") != 1
            or approval.get("kind") != "cre_capacity_review_approval"
            or not isinstance(approval.get("profile"), str)
            or not approval.get("profile")
            or not sha.fullmatch(str(approval.get("config_sha256")))
            or not sha.fullmatch(str(approval.get("transition_receipt_sha256")))
            or not source_sha.fullmatch(str(approval.get("source_git_sha")))
            or not sha.fullmatch(str(approval.get("nonce")))
            or not isinstance(approval.get("created_at"), str)
            or type(approval.get("expires_after_seconds")) is not int
            or approval.get("expires_after_seconds") != 600
            or approval.get("approved") is not True):
        raise SystemExit("approval bindings are invalid")
    try:
        created = datetime.fromisoformat(approval["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise ValueError("naive approval timestamp")
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if age < 0 or age > 600:
            raise ValueError("stale approval")
    except (TypeError, ValueError):
        raise SystemExit("approval timestamp is invalid or stale")
    nonce_hash = hashlib.sha256(
        json.dumps(
            approval["nonce"], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    grant = {
        "schema_version": 1,
        "kind": "cre_capacity_review_benchmark_grant",
        "profile": approval["profile"],
        "config_sha256": approval["config_sha256"],
        "transition_receipt_sha256": approval["transition_receipt_sha256"],
        "source_git_sha": approval["source_git_sha"],
        "review_approval_nonce_sha256": nonce_hash,
        "review_approval_created_at": approval["created_at"],
        "expires_after_seconds": 600,
        "approved": True,
    }
    grant_path = os.path.join(
        parent, ".cre-capacity-benchmark-grant-" + nonce_hash + ".json"
    )
    grant_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(
        os, "O_NOFOLLOW", 0
    )
    if os.path.lexists(grant_path):
        raise SystemExit("grant already exists")
    recovery_fd = os.open(recovery_path, grant_flags, 0o600)
    try:
        os.fchmod(recovery_fd, 0o600)
        recovery = json.dumps({"grant_name": os.path.basename(grant_path)}).encode()
        offset = 0
        while offset < len(recovery):
            offset += os.write(recovery_fd, recovery[offset:])
        os.fsync(recovery_fd)
    finally:
        os.close(recovery_fd)
    try:
        grant_fd = os.open(grant_path, grant_flags, 0o600)
        grant_created = True
        try:
            os.fchmod(grant_fd, 0o600)
            encoded = (
                json.dumps(grant, sort_keys=True, separators=(",", ":")).encode()
                + b"\n"
            )
            written = 0
            while written < len(encoded):
                written += os.write(grant_fd, encoded[written:])
            os.fsync(grant_fd)
        finally:
            os.close(grant_fd)
    except BaseException:
        if grant_created:
            try:
                os.unlink(grant_path)
            except FileNotFoundError:
                pass
        raise
    try:
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
    except BaseException:
        try:
            os.unlink(grant_path)
        except FileNotFoundError:
            pass
        raise
except BaseException:
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, signal.SIG_IGN)
    if grant_created:
        try:
            os.unlink(grant_path)
        except FileNotFoundError:
            pass
    raise
finally:
    try:
        os.unlink(consumed)
    except BaseException:
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, signal.SIG_IGN)
        if grant_created:
            try:
                os.unlink(grant_path)
            except FileNotFoundError:
                pass
        raise
"""
REVIEW_GRANT_DESTROYER = r"""
import os
import re
import stat
import sys

path = os.path.abspath(sys.argv[1])
parent = os.path.dirname(path)
name = os.path.basename(path)
uid = os.getuid()
euid = os.geteuid()
if uid == 0 or euid == 0 or uid != euid:
    raise SystemExit("grant destroyer requires a non-root unswitched operating account")
if not re.fullmatch(r"\.cre-capacity-benchmark-grant-[0-9a-f]{64}\.json", name):
    raise SystemExit("grant path is invalid")
try:
    file_stat = os.lstat(path)
except FileNotFoundError:
    raise SystemExit(0)
parent_stat = os.lstat(parent)
if (not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != euid
        or stat.S_IMODE(file_stat.st_mode) != 0o600 or file_stat.st_nlink != 1):
    raise SystemExit("grant file ownership is invalid")
if (not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != euid
        or stat.S_IMODE(parent_stat.st_mode) != 0o700):
    raise SystemExit("grant parent ownership is invalid")
os.unlink(path)
"""
REVIEW_CONSUMPTION_RECOVERY = r"""
import json
import os
import re
import stat
import sys

path = os.path.abspath(sys.argv[1])
discard = sys.argv[2] == "discard"
parent = os.path.dirname(path)
uid = os.getuid()
euid = os.geteuid()
if uid == 0 or euid == 0 or uid != euid:
    raise SystemExit("consumption recovery requires a non-root unswitched operating account")
if not re.fullmatch(r"\.cre-capacity-consumption-[0-9a-f]{64}\.json", os.path.basename(path)):
    raise SystemExit("consumption recovery path is invalid")
def validate(target, directory=False):
    value = os.lstat(target)
    if (value.st_uid != euid or stat.S_IMODE(value.st_mode) != (0o700 if directory else 0o600)
            or not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode))
            or (not directory and value.st_nlink != 1)):
        raise SystemExit("consumption recovery ownership is invalid")
validate(parent, True)
try:
    validate(path)
except FileNotFoundError:
    raise SystemExit(0)
with open(path, "rb") as handle:
    record = json.loads(handle.read(1024))
name = record.get("grant_name") if isinstance(record, dict) else None
if (set(record) != {"grant_name"} or not isinstance(name, str)
        or not re.fullmatch(r"\.cre-capacity-benchmark-grant-[0-9a-f]{64}\.json", name)):
    raise SystemExit("consumption recovery record is invalid")
if discard:
    grant = os.path.join(parent, name)
    try:
        validate(grant)
    except FileNotFoundError:
        pass
    else:
        os.unlink(grant)
os.unlink(path)
"""


class RuntimeAdmissionError(RuntimeError):
    """Runtime state cannot be admitted or changed safely."""


class RuntimeMutationError(RuntimeAdmissionError):
    """A runtime mutation command was issued but did not complete cleanly."""


class RuntimeOverlayCleanupError(RuntimeMutationError):
    """A resource command completed but its private overlay did not clean up."""


def _operator_uid() -> int:
    """Return the ordinary operating-account UID or fail on privilege switching."""
    uid = os.getuid()
    euid = os.geteuid()
    if uid == 0 or euid == 0 or uid != euid:
        raise RuntimeAdmissionError(
            "capacity approval requires a non-root unswitched operating account"
        )
    return euid


class RuntimeCompensationError(RuntimeAdmissionError):
    """Best-effort compensation finished with one or more uncertain steps."""

    def __init__(self, message: str, *, baseline_verified: bool) -> None:
        super().__init__(message)
        self.baseline_verified = baseline_verified


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str


CommandRunner = Callable[
    [Sequence[str], Path | None, Mapping[str, str] | None], CommandResult
]


@dataclass
class RuntimeCapture:
    public: dict[str, Any]
    browser_env: dict[str, str]
    api_env: dict[str, str]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _default_runner(
    argv: Sequence[str], cwd: Path | None = None, env: Mapping[str, str] | None = None
) -> CommandResult:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            capture_output=True,
            check=False,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeAdmissionError(
            f"runtime command unavailable: {Path(argv[0]).name}"
        ) from exc
    return CommandResult(completed.returncode, completed.stdout)


def _run(
    runner: CommandRunner,
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    result = runner(argv, cwd, env)
    if result.returncode != 0:
        raise RuntimeAdmissionError(f"runtime command failed: {Path(argv[0]).name}")
    return result.stdout


def _json_output(runner: CommandRunner, argv: Sequence[str]) -> Any:
    try:
        return json.loads(_run(runner, argv, cwd=REPO_ROOT))
    except json.JSONDecodeError as exc:
        raise RuntimeAdmissionError(f"invalid JSON from {Path(argv[0]).name}") from exc


def _env_map(container: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    config = container.get("Config")
    values = config.get("Env", []) if isinstance(config, Mapping) else []
    if not isinstance(values, list):
        raise RuntimeAdmissionError("container environment shape is invalid")
    for item in values:
        if not isinstance(item, str) or "=" not in item:
            raise RuntimeAdmissionError("container environment entry is invalid")
        key, value = item.split("=", 1)
        if not key or key in result:
            raise RuntimeAdmissionError("container environment keys are invalid")
        result[key] = value
    return result


def _env_receipt(values: Mapping[str, str]) -> dict[str, str | int]:
    return {
        "count": len(values),
        "keys_sha256": _hash(sorted(values)),
        "values_sha256": _hash({key: values[key] for key in sorted(values)}),
        "excluding_pages_sha256": _hash(
            {key: values[key] for key in sorted(values) if key != PRIVATE_PAGE_KEY}
        ),
    }


def _host_config(container: Mapping[str, Any]) -> Mapping[str, Any]:
    value = container.get("HostConfig")
    if not isinstance(value, Mapping):
        raise RuntimeAdmissionError("container host configuration is invalid")
    return value


def _swap_bytes(host: Mapping[str, Any]) -> int | None:
    memory, total = host.get("Memory"), host.get("MemorySwap")
    if type(memory) is int and type(total) is int and total >= memory:
        return total - memory
    return None


def _port_bindings(host: Mapping[str, Any]) -> object:
    return host.get("PortBindings")


def _container_public(
    container: Mapping[str, Any], env: Mapping[str, str]
) -> dict[str, Any]:
    host = _host_config(container)
    mounts = container.get("Mounts")
    return {
        "id": container.get("Id"),
        "image": container.get("Image"),
        "env": _env_receipt(env),
        "page_slots": env.get(PRIVATE_PAGE_KEY),
        "nano_cpus": host.get("NanoCpus"),
        "memory_bytes": host.get("Memory"),
        "swap_bytes": _swap_bytes(host),
        "pids_limit": host.get("PidsLimit"),
        "shm_bytes": host.get("ShmSize"),
        "port_bindings": _port_bindings(host),
        "network_mode": host.get("NetworkMode"),
        "mounts_sha256": _hash(mounts if isinstance(mounts, list) else []),
        "mount_count": len(mounts) if isinstance(mounts, list) else None,
        "security_opt": host.get("SecurityOpt"),
        "cap_drop": host.get("CapDrop"),
        "cgroup_memory_max": None,
        "cgroup_swap_max": None,
        "cgroup_memory_current": None,
    }


def _queue_json(url: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            raw = response.read(64 * 1024)
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise RuntimeAdmissionError("loopback queue endpoint unavailable") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeAdmissionError(
            "loopback queue endpoint returned invalid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeAdmissionError("loopback queue endpoint returned invalid data")
    return value


def _http_status(url: str) -> int:
    deadline = time.monotonic() + RUNTIME_ENDPOINT_READY_SECONDS
    last_error: BaseException | None = None
    while True:
        try:
            remaining = deadline - time.monotonic()
            with urllib.request.urlopen(
                url, timeout=min(10, max(0.1, remaining))
            ) as response:
                response.read(1)
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(RUNTIME_ENDPOINT_RETRY_SECONDS, remaining))
    raise RuntimeAdmissionError("loopback runtime endpoint unavailable") from last_error


def _queue_counts(payload: Mapping[str, Any]) -> tuple[int, int, int]:
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    values = tuple(
        data.get(key)
        for key in ("activeJobsInQueue", "waitingJobsInQueue", "jobsInQueue")
    )
    if any(type(value) is not int or value < 0 for value in values):
        raise RuntimeAdmissionError("queue counters are invalid")
    return values  # type: ignore[return-value]


def _cgroup_value(runner: CommandRunner, container: str, name: str) -> int | None:
    value = _run(
        runner, ["docker", "exec", container, "cat", f"/sys/fs/cgroup/{name}"]
    ).strip()
    if value == "max":
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeAdmissionError("container cgroup value is invalid") from exc
    if parsed < 0:
        raise RuntimeAdmissionError("container cgroup value is invalid")
    return parsed


def _settlement(runner: CommandRunner) -> dict[str, Any]:
    queue = _queue_json("http://127.0.0.1:3102/v2/team/queue-status")
    active = _queue_json("http://127.0.0.1:3102/v2/crawl/active")
    active_jobs, waiting_jobs, total_jobs = _queue_counts(queue)
    crawls = active.get("crawls")
    if crawls is None and isinstance(active.get("data"), Mapping):
        crawls = active["data"].get("crawls")
    rabbit = _run(
        runner,
        [
            "docker",
            "exec",
            RABBIT_CONTAINER,
            "rabbitmqctl",
            "list_queues",
            "name",
            "messages_ready",
            "messages_unacknowledged",
            "--quiet",
        ],
    ).splitlines()
    rabbit_counts: list[tuple[int, int]] = []
    for row in rabbit:
        parts = row.split()
        if len(parts) >= 3 and parts[-2:].count("0") == 2:
            rabbit_counts.append((0, 0))
        elif len(parts) >= 3 and parts[-2].isdigit() and parts[-1].isdigit():
            rabbit_counts.append((int(parts[-2]), int(parts[-1])))
    nuq = _run(
        runner,
        [
            "docker",
            "exec",
            NUQ_CONTAINER,
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-At",
            "-F",
            "|",
            "-c",
            (
                "select 'queue_scrape_total', count(*) from nuq.queue_scrape union all "
                "select 'queue_scrape_backlog_total', count(*) from nuq.queue_scrape_backlog union all "
                "select 'queue_crawl_finished_total', count(*) from nuq.queue_crawl_finished order by 1;"
            ),
        ],
    ).splitlines()
    nuq_counts: dict[str, int] = {}
    for row in nuq:
        parts = row.split("|")
        if len(parts) == 2 and parts[1].isdigit():
            nuq_counts[parts[0]] = int(parts[1])
    processes = _run(runner, ["/bin/ps", "-Ao", "command="])
    process_busy = any(
        marker in processes
        for marker in (
            "cre_checkpoint_series",
            "cre_capacity_benchmark",
            "collect.ts",
            "cre_daily_update",
            "cre_checkpoint_refresh",
        )
    )
    return {
        "api_root_status": _http_status("http://127.0.0.1:3102/"),
        "browser_root_status": _http_status("http://127.0.0.1:3103/"),
        "api": {"active": active_jobs, "waiting": waiting_jobs, "total": total_jobs},
        "active_crawls": len(crawls) if isinstance(crawls, list) else None,
        "rabbitmq_queue_count": len(rabbit_counts),
        "rabbitmq_ready": sum(value[0] for value in rabbit_counts),
        "rabbitmq_unacknowledged": sum(value[1] for value in rabbit_counts),
        "nuq": nuq_counts,
        "cre_process_active": process_busy,
    }


def _capture_source_state(runner: CommandRunner) -> dict[str, Any]:
    return {
        "git_sha": _run(runner, ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT).strip(),
        "dirty": bool(
            _run(runner, ["git", "status", "--porcelain"], cwd=REPO_ROOT).strip()
        ),
        "compose_sha256": _file_hash(COMPOSE_PATH),
        "override_sha256": _file_hash(OVERRIDE_PATH),
        "execution_inputs_sha256": {
            key: _file_hash(path) for key, path in EXECUTION_INPUTS.items()
        },
    }


def capture_runtime(runner: CommandRunner = _default_runner) -> RuntimeCapture:
    inspected = _json_output(
        runner, ["docker", "inspect", API_CONTAINER, BROWSER_CONTAINER]
    )
    if not isinstance(inspected, list) or len(inspected) != 2:
        raise RuntimeAdmissionError(
            "expected API and browser containers are not available"
        )
    by_name = {
        str(item.get("Name", "")).lstrip("/"): item
        for item in inspected
        if isinstance(item, Mapping)
    }
    if set(by_name) != {API_CONTAINER, BROWSER_CONTAINER}:
        raise RuntimeAdmissionError("runtime container identities are incomplete")
    api = by_name[API_CONTAINER]
    browser = by_name[BROWSER_CONTAINER]
    api_env, browser_env = _env_map(api), _env_map(browser)
    api_public, browser_public = (
        _container_public(api, api_env),
        _container_public(browser, browser_env),
    )
    for public, container in (
        (api_public, API_CONTAINER),
        (browser_public, BROWSER_CONTAINER),
    ):
        public["cgroup_memory_max"] = _cgroup_value(runner, container, "memory.max")
        public["cgroup_swap_max"] = _cgroup_value(runner, container, "memory.swap.max")
        public["cgroup_memory_current"] = _cgroup_value(
            runner, container, "memory.current"
        )
    try:
        orb_memory = int(_run(runner, ["orb", "config", "get", "memory_mib"]).strip())
        docker_memory = int(
            _run(runner, ["docker", "info", "--format", "{{.MemTotal}}"]).strip()
        )
    except ValueError as exc:
        raise RuntimeAdmissionError("runtime memory capacity is invalid") from exc
    public = {
        "repo": _capture_source_state(runner),
        "host": {
            "orb_status": _run(runner, ["orb", "status"]).strip(),
            "orbstack_memory_mib": orb_memory,
            "docker_context": _run(runner, ["docker", "context", "show"]).strip(),
            "docker_memtotal_bytes": docker_memory,
        },
        "api": api_public,
        "browser": browser_public,
        "settlement": _settlement(runner),
    }
    public["transition_sha256"] = transition_fingerprint(public)
    public["snapshot_sha256"] = snapshot_fingerprint(public)
    return RuntimeCapture(public, browser_env, api_env)


def _capture_recovery_state(
    before: RuntimeCapture, runner: CommandRunner
) -> RuntimeCapture:
    """Inspect identity/config without requiring a recreated browser to run.

    Only an in-flight candidate attempt may use its retained pre-mutation
    browser environment when Compose has removed that exact service. API
    identity, existing browser configuration, source inputs, and host identity
    are still checked live. This is never a successful verification snapshot.
    """
    names = _run(runner, ["docker", "ps", "-a", "--format", "{{.Names}}"])
    present = set(names.splitlines())
    if API_CONTAINER not in present:
        raise RuntimeAdmissionError("recovery cannot identify the preserved API")
    containers = [API_CONTAINER]
    if BROWSER_CONTAINER in present:
        containers.append(BROWSER_CONTAINER)
    inspected = _json_output(runner, ["docker", "inspect", *containers])
    if not isinstance(inspected, list) or len(inspected) != len(containers):
        raise RuntimeAdmissionError("recovery container inspection is incomplete")
    observed = {
        str(item.get("Name", "")).lstrip("/"): item
        for item in inspected
        if isinstance(item, Mapping)
    }
    if set(observed) != set(containers):
        raise RuntimeAdmissionError("recovery container identities are invalid")
    public = copy.deepcopy(before.public)
    api_env = _env_map(observed[API_CONTAINER])
    public["api"] = _container_public(observed[API_CONTAINER], api_env)
    browser_env = dict(before.browser_env)
    if BROWSER_CONTAINER in observed:
        browser_env = _env_map(observed[BROWSER_CONTAINER])
        public["browser"] = _container_public(observed[BROWSER_CONTAINER], browser_env)
    public["repo"] = _capture_source_state(runner)
    try:
        orb_memory = int(_run(runner, ["orb", "config", "get", "memory_mib"]).strip())
    except ValueError as exc:
        raise RuntimeAdmissionError("recovery host memory identity is invalid") from exc
    public["host"].update(
        {
            "orb_status": _run(runner, ["orb", "status"]).strip(),
            "orbstack_memory_mib": orb_memory,
            "docker_context": _run(runner, ["docker", "context", "show"]).strip(),
        }
    )
    return RuntimeCapture(public, browser_env, api_env)


def snapshot_fingerprint(public: Mapping[str, Any]) -> str:
    selected = {key: value for key, value in public.items() if key != "snapshot_sha256"}
    return _hash(selected)


def transition_fingerprint(public: Mapping[str, Any]) -> str:
    """Hash transition invariants while excluding volatile usage/queue samples."""
    selected = {
        key: value
        for key, value in public.items()
        if key not in {"snapshot_sha256", "transition_sha256", "settlement"}
    }
    for container_name in ("api", "browser"):
        container = selected.get(container_name)
        if isinstance(container, Mapping):
            selected[container_name] = {
                key: value
                for key, value in container.items()
                if key != "cgroup_memory_current"
            }
    repo = selected.get("repo")
    if isinstance(repo, Mapping):
        selected["repo"] = {key: value for key, value in repo.items() if key != "dirty"}
    return _hash(selected)


def _int(value: Any, field: str) -> int:
    if type(value) is not int:
        raise RuntimeAdmissionError(f"{field} is not an integer")
    return value


def evaluate_state(
    public: Mapping[str, Any], profile: Mapping[str, Any], state: str
) -> dict[str, bool]:
    runtime = profile["runtime_baseline"]
    requested = profile["requested"]
    host, api, browser, settlement, repo = (
        public["host"],
        public["api"],
        public["browser"],
        public["settlement"],
        public["repo"],
    )
    configured_bytes = (
        _int(runtime["orbstack_memory_mib"], "orbstack memory") * 1024 * 1024
    )
    minimum = (
        configured_bytes
        * _int(runtime["minimum_docker_memtotal_basis_points"], "memory basis points")
        // 10000
    )
    browser_cpu = (
        runtime["browser_cpus"] if state == "baseline" else requested["browser_cpus"]
    )
    browser_pages = (
        runtime["global_pages"] if state == "baseline" else requested["global_pages"]
    )
    browser_pids = (
        runtime["browser_pids"] if state == "baseline" else requested["browser_pids"]
    )
    api_cpu = runtime["api_cpus"] if state == "baseline" else requested["api_cpus"]
    nuq = settlement["nuq"]
    browser_memory = _int(runtime["browser_memory_bytes"], "browser memory")
    api_memory = _int(runtime["api_memory_bytes"], "api memory")
    return {
        "source_inputs_identified": bool(SHA_PATTERN.fullmatch(str(repo["git_sha"])))
        and isinstance(repo.get("execution_inputs_sha256"), Mapping)
        and set(repo["execution_inputs_sha256"]) == set(EXECUTION_INPUTS)
        and all(
            bool(re.fullmatch(r"[0-9a-f]{64}", str(value)))
            for value in repo["execution_inputs_sha256"].values()
        ),
        "orb_running": host["orb_status"] == "Running",
        "orb_memory_configured": host["orbstack_memory_mib"]
        == runtime["orbstack_memory_mib"],
        "docker_context": host["docker_context"] == "orbstack",
        "docker_usable_memory": type(host["docker_memtotal_bytes"]) is int
        and host["docker_memtotal_bytes"] >= minimum,
        "browser_cpu": browser["nano_cpus"] == browser_cpu * 1_000_000_000,
        "browser_pages": browser["page_slots"] == str(browser_pages),
        "browser_pids": browser["pids_limit"] == browser_pids,
        "browser_memory": browser["memory_bytes"] == runtime["browser_memory_bytes"]
        and browser["swap_bytes"] == runtime["browser_swap_bytes"]
        and browser["cgroup_memory_max"] == runtime["browser_memory_bytes"]
        and browser["cgroup_swap_max"] == runtime["browser_swap_bytes"],
        "browser_memory_headroom": type(browser["cgroup_memory_current"]) is int
        and 0 <= browser["cgroup_memory_current"] <= browser_memory * 9 // 10,
        "browser_shm": browser["shm_bytes"] == runtime["browser_shm_bytes"],
        "browser_port": browser["port_bindings"]
        == {"3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3103"}]},
        "browser_network": browser["network_mode"] == "firecrawl_backend",
        "browser_no_volumes": browser["mount_count"] == 0,
        "browser_security": browser["security_opt"] == ["no-new-privileges:true"]
        and browser["cap_drop"] == ["ALL"],
        "api_cpu": api["nano_cpus"] == api_cpu * 1_000_000_000,
        "api_memory": api["memory_bytes"] == runtime["api_memory_bytes"]
        and api["swap_bytes"] == runtime["api_swap_bytes"]
        and api["cgroup_memory_max"] == runtime["api_memory_bytes"]
        and api["cgroup_swap_max"] == runtime["api_swap_bytes"],
        "api_memory_headroom": type(api["cgroup_memory_current"]) is int
        and 0 <= api["cgroup_memory_current"] <= api_memory * 9 // 10,
        "api_port": isinstance(api["port_bindings"], Mapping)
        and any(
            entry.get("HostPort") == "3102"
            for entries in api["port_bindings"].values()
            if isinstance(entries, list)
            for entry in entries
            if isinstance(entry, Mapping)
        ),
        "api_network": api["network_mode"] == "firecrawl_backend",
        "api_endpoint": settlement["api_root_status"] == 200,
        "browser_endpoint": settlement["browser_root_status"] == 404,
        "api_queue_idle": settlement["api"] == {"active": 0, "waiting": 0, "total": 0},
        "active_crawls_idle": settlement["active_crawls"] == 0,
        "rabbitmq_idle": settlement["rabbitmq_queue_count"] > 0
        and settlement["rabbitmq_ready"] == 0
        and settlement["rabbitmq_unacknowledged"] == 0,
        "nuq_idle": set(nuq)
        == {
            "queue_scrape_total",
            "queue_scrape_backlog_total",
            "queue_crawl_finished_total",
        }
        and all(value == 0 for value in nuq.values()),
        "collector_idle": settlement["cre_process_active"] is False,
    }


def _receipt_payload(
    profile_name: str, profile: Mapping[str, Any], digest: str, capture: RuntimeCapture
) -> dict[str, Any]:
    checks = evaluate_state(capture.public, profile, "baseline")
    admitted = all(checks.values())
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "kind": RECEIPT_KIND,
        "profile": profile_name,
        "config_sha256": digest,
        "created_at": utc_now(),
        "expires_after_seconds": RECEIPT_MAX_AGE_SECONDS,
        "admitted": admitted,
        "checks": checks,
        "requested": profile["requested"],
        "baseline": capture.public,
        "apply_plan": {
            "browser": [
                "docker",
                "compose",
                "--env-file",
                "<private-compose-env>",
                "-f",
                str(COMPOSE_PATH),
                "-f",
                str(OVERRIDE_PATH),
                "-f",
                "<private-runtime-overlay>",
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--pull",
                "never",
                "--force-recreate",
                "playwright-service",
            ],
            "api": [
                "docker",
                "update",
                "--cpus",
                str(profile["requested"]["api_cpus"]),
                "--memory",
                str(profile["runtime_baseline"]["api_memory_bytes"]),
                "--memory-swap",
                str(profile["runtime_baseline"]["api_memory_bytes"]),
                API_CONTAINER,
            ],
        },
        "rollback_plan": {
            "browser": [
                "docker",
                "compose",
                "--env-file",
                "<private-compose-env>",
                "-f",
                str(COMPOSE_PATH),
                "-f",
                str(OVERRIDE_PATH),
                "-f",
                "<private-baseline-overlay>",
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--pull",
                "never",
                "--force-recreate",
                "playwright-service",
            ],
            "api": [
                "docker",
                "update",
                "--cpus",
                str(profile["runtime_baseline"]["api_cpus"]),
                "--memory",
                str(profile["runtime_baseline"]["api_memory_bytes"]),
                "--memory-swap",
                str(profile["runtime_baseline"]["api_memory_bytes"]),
                API_CONTAINER,
            ],
        },
    }
    receipt["receipt_sha256"] = _hash(receipt)
    return receipt


def _write_private_bytes(
    path: Path, encoded: bytes, *, refuse_existing: bool = False
) -> None:
    if refuse_existing and path.exists():
        raise RuntimeAdmissionError("receipt path already exists")
    if path.parent.exists():
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise RuntimeAdmissionError("private output parent is not a real directory")
        if stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
            raise RuntimeAdmissionError("private output parent must have mode 0700")
    else:
        path.parent.mkdir(parents=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, encoded)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        Path(temporary).unlink(missing_ok=True)


def write_private(
    path: Path, value: Mapping[str, Any], *, refuse_existing: bool = False
) -> None:
    _write_private_bytes(
        path, _canonical(value) + b"\n", refuse_existing=refuse_existing
    )


def _controller_output(path: Path) -> Path:
    allowed = (REPO_ROOT / "tasks" / "tmp").resolve()
    resolved = path.resolve()
    if resolved.parent.parent != allowed or not resolved.parent.name.startswith(
        "cre-capacity-transition-"
    ):
        raise RuntimeAdmissionError(
            "controller output must be under tasks/tmp/cre-capacity-transition-*"
        )
    return resolved


def preflight(
    profile_name: str, out: Path, runner: CommandRunner = _default_runner
) -> dict[str, Any]:
    out = _controller_output(out)
    profile, digest = experiment.load_profile(experiment.DEFAULT_CONFIG, profile_name)
    if profile["kind"] != "experiment":
        raise RuntimeAdmissionError("runtime transition requires an experiment profile")
    capture = capture_runtime(runner)
    receipt = _receipt_payload(profile_name, profile, digest, capture)
    write_private(out, receipt, refuse_existing=True)
    if not receipt["admitted"]:
        raise RuntimeAdmissionError(
            "runtime preflight failed; receipt retained for diagnosis"
        )
    return receipt


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise RuntimeAdmissionError("receipt timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeAdmissionError("receipt timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise RuntimeAdmissionError("receipt timestamp is invalid")
    return parsed.astimezone(timezone.utc)


def load_fresh_receipt(
    path: Path,
    profile_name: str,
    now: datetime | None = None,
    *,
    require_fresh: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    receipt = experiment._read_json(path)
    profile, digest = experiment.load_profile(experiment.DEFAULT_CONFIG, profile_name)
    supplied_receipt_hash = receipt.get("receipt_sha256")
    unsigned_receipt = {
        key: value for key, value in receipt.items() if key != "receipt_sha256"
    }
    if (
        receipt.get("schema_version") != SCHEMA_VERSION
        or receipt.get("kind") != RECEIPT_KIND
        or receipt.get("profile") != profile_name
        or receipt.get("config_sha256") != digest
        or receipt.get("admitted") is not True
        or not isinstance(supplied_receipt_hash, str)
        or supplied_receipt_hash != _hash(unsigned_receipt)
    ):
        raise RuntimeAdmissionError(
            "runtime receipt does not bind the selected profile"
        )
    checks = receipt.get("checks")
    baseline = receipt.get("baseline")
    try:
        recomputed_checks = (
            evaluate_state(baseline, profile, "baseline")
            if isinstance(baseline, Mapping)
            else {}
        )
        baseline_fingerprint_valid = (
            isinstance(baseline, Mapping)
            and baseline.get("snapshot_sha256") == snapshot_fingerprint(baseline)
            and baseline.get("transition_sha256") == transition_fingerprint(baseline)
        )
    except (KeyError, TypeError, RuntimeAdmissionError):
        recomputed_checks = {}
        baseline_fingerprint_valid = False
    if (
        not isinstance(checks, dict)
        or not checks
        or not all(value is True for value in checks.values())
        or checks != recomputed_checks
        or not baseline_fingerprint_valid
        or receipt.get("requested") != profile["requested"]
    ):
        raise RuntimeAdmissionError("runtime receipt is not admitted")
    current = now or datetime.now(timezone.utc)
    age = (current - _parse_time(receipt.get("created_at"))).total_seconds()
    if age < 0 or (require_fresh and age > RECEIPT_MAX_AGE_SECONDS):
        raise RuntimeAdmissionError("runtime receipt is stale")
    return receipt, profile, digest


def _validate_review_authority(path: Path) -> None:
    """Require a private file owned by the current operating account."""
    try:
        file_stat = path.lstat()
        parent_stat = path.parent.lstat()
    except OSError as exc:
        raise RuntimeAdmissionError("review approval path is unavailable") from exc
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
        raise RuntimeAdmissionError("review approval must be a regular private file")
    owner_uid = _operator_uid()
    if file_stat.st_uid != owner_uid or stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise RuntimeAdmissionError(
            "review approval file must be operator-owned mode 0600"
        )
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != owner_uid
        or stat.S_IMODE(parent_stat.st_mode) != 0o700
    ):
        raise RuntimeAdmissionError(
            "review approval directory must be operator-owned mode 0700"
        )


def _benchmark_grant_payload(approval: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "profile",
        "config_sha256",
        "transition_receipt_sha256",
        "source_git_sha",
        "nonce",
        "created_at",
        "expires_after_seconds",
    }
    if (
        approval.get("schema_version") != SCHEMA_VERSION
        or approval.get("kind") != APPROVAL_KIND
        or approval.get("approved") is not True
        or any(key not in approval for key in required)
        or not isinstance(approval.get("profile"), str)
        or not approval.get("profile")
        or not NONCE_PATTERN.fullmatch(str(approval.get("config_sha256")))
        or not NONCE_PATTERN.fullmatch(str(approval.get("transition_receipt_sha256")))
        or not SHA_PATTERN.fullmatch(str(approval.get("source_git_sha")))
        or not NONCE_PATTERN.fullmatch(str(approval.get("nonce")))
        or type(approval.get("expires_after_seconds")) is not int
        or approval.get("expires_after_seconds") != RECEIPT_MAX_AGE_SECONDS
    ):
        raise RuntimeAdmissionError("review approval grant bindings are invalid")
    _parse_time(approval.get("created_at"))
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": BENCHMARK_GRANT_KIND,
        "profile": approval["profile"],
        "config_sha256": approval["config_sha256"],
        "transition_receipt_sha256": approval["transition_receipt_sha256"],
        "source_git_sha": approval["source_git_sha"],
        "review_approval_nonce_sha256": _hash(approval["nonce"]),
        "review_approval_created_at": approval["created_at"],
        "expires_after_seconds": approval["expires_after_seconds"],
        "approved": True,
    }


def _benchmark_grant_path(parent: Path, approval: Mapping[str, Any]) -> Path:
    grant = _benchmark_grant_payload(approval)
    return parent / (
        f".cre-capacity-benchmark-grant-{grant['review_approval_nonce_sha256']}.json"
    )


def _write_review_benchmark_grant(parent: Path, approval: Mapping[str, Any]) -> Path:
    grant = _benchmark_grant_payload(approval)
    path = _benchmark_grant_path(parent, approval)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise RuntimeAdmissionError(
            "review benchmark grant could not be created exclusively"
        ) from exc
    try:
        os.fchmod(fd, 0o600)
        encoded = _canonical(grant) + b"\n"
        offset = 0
        while offset < len(encoded):
            offset += os.write(fd, encoded[offset:])
        os.fsync(fd)
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(fd)
    return path


def _consume_review_approval_bytes(path: Path, recovery_path: Path) -> bytes:
    """Read-and-destroy approval in an isolated same-user helper process."""
    _validate_review_authority(path)
    try:
        result = subprocess.run(
            [
                "/usr/bin/python3",
                "-c",
                REVIEW_APPROVAL_CONSUMER,
                str(path),
                str(recovery_path),
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeAdmissionError("review approval consumer is unavailable") from exc
    if result.returncode != 0:
        raise RuntimeAdmissionError("review approval consumption failed")
    return result.stdout


def _recover_review_consumption(path: Path, *, discard: bool) -> None:
    """Recover an exact helper transaction even if its response was lost."""
    _operator_uid()
    try:
        result = subprocess.run(
            [
                "/usr/bin/python3",
                "-c",
                REVIEW_CONSUMPTION_RECOVERY,
                str(path),
                "discard" if discard else "release",
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeAdmissionError(
            "review consumption recovery is unavailable"
        ) from exc
    if result.returncode != 0:
        raise RuntimeAdmissionError("review consumption recovery failed")


def _destroy_review_benchmark_grant(path: Path) -> None:
    """Destroy an unused review grant without exposing or reading its payload."""
    _operator_uid()
    try:
        result = subprocess.run(
            [
                "/usr/bin/python3",
                "-c",
                REVIEW_GRANT_DESTROYER,
                str(path),
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeAdmissionError(
            "review benchmark grant destroyer is unavailable"
        ) from exc
    if result.returncode != 0:
        raise RuntimeAdmissionError("review benchmark grant could not be destroyed")


def _validate_approval_payload(
    approval: Mapping[str, Any],
    receipt: Mapping[str, Any],
    profile_name: str,
    config_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    required = {
        "schema_version",
        "kind",
        "profile",
        "config_sha256",
        "transition_receipt_sha256",
        "source_git_sha",
        "approved_by",
        "approved",
        "created_at",
        "expires_after_seconds",
        "nonce",
    }
    if (
        set(approval) != required
        or approval.get("schema_version") != SCHEMA_VERSION
        or approval.get("kind") != APPROVAL_KIND
        or approval.get("profile") != profile_name
        or approval.get("config_sha256") != config_sha256
        or approval.get("transition_receipt_sha256") != receipt.get("receipt_sha256")
        or approval.get("source_git_sha")
        != receipt.get("baseline", {}).get("repo", {}).get("git_sha")
        or approval.get("approved_by") != "coordinating-review"
        or approval.get("approved") is not True
        or approval.get("expires_after_seconds") != RECEIPT_MAX_AGE_SECONDS
        or not NONCE_PATTERN.fullmatch(str(approval.get("nonce")))
    ):
        raise RuntimeAdmissionError(
            "review approval does not bind the admitted transition"
        )
    current = now or datetime.now(timezone.utc)
    age = (current - _parse_time(approval.get("created_at"))).total_seconds()
    if age < 0 or age > RECEIPT_MAX_AGE_SECONDS:
        raise RuntimeAdmissionError("review approval is stale")
    return dict(approval)


def consume_review_approval(
    path: Path,
    receipt: Mapping[str, Any],
    profile_name: str,
    config_sha256: str,
    now: datetime | None = None,
) -> tuple[dict[str, Any], Path]:
    """Atomically consume one operator-owned approval before issuing a mutation."""
    path = Path(os.path.abspath(path))
    recovery_path = path.parent / (
        f".cre-capacity-consumption-{secrets.token_hex(32)}.json"
    )
    grant_path: Path | None = None
    try:
        try:
            approval = json.loads(_consume_review_approval_bytes(path, recovery_path))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeAdmissionError(
                "review approval contains invalid JSON"
            ) from exc
        if not isinstance(approval, Mapping):
            raise RuntimeAdmissionError("review approval contains invalid JSON")
        grant_path = _benchmark_grant_path(path.parent, approval)
        validated = _validate_approval_payload(
            approval, receipt, profile_name, config_sha256, now
        )
        _recover_review_consumption(recovery_path, discard=False)
        return validated, grant_path
    except BaseException as primary_error:
        cleanup_errors: list[BaseException] = []
        with _defer_transition_signals():
            try:
                _recover_review_consumption(recovery_path, discard=True)
            except BaseException as exc:  # noqa: BLE001 - always attempt known grant cleanup
                cleanup_errors.append(exc)
            if grant_path is not None:
                try:
                    _destroy_review_benchmark_grant(grant_path)
                except BaseException as exc:  # noqa: BLE001 - retain both cleanup failures
                    cleanup_errors.append(exc)
        if cleanup_errors:
            error = RuntimeAdmissionError(
                "review approval failed and grant cleanup failed"
            )
            for cleanup_error in cleanup_errors:
                error.add_note(str(cleanup_error))
            raise error from primary_error
        raise


def _fsync_directory(path: Path) -> None:
    """Persist directory-entry changes or fail closed."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise RuntimeAdmissionError(
            "review approval consumption directory could not be made durable"
        ) from exc


def _record_review_approval_consumption(
    lock_path: Path,
    approval: Mapping[str, Any],
    receipt: Mapping[str, Any],
    config_sha256: str,
) -> Path:
    """Durably consume an approval nonce before any resource mutation."""
    operator_uid = _operator_uid()
    canonical_lock = lock_path.resolve()
    if canonical_lock.name != ".cre.lock" or canonical_lock.parent.name != "daily":
        raise RuntimeAdmissionError("canonical approval consumption path is invalid")
    consumption_root = canonical_lock.parent.parent / ".capacity-review-consumption"
    consumption_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        root_stat = consumption_root.lstat()
    except OSError as exc:
        raise RuntimeAdmissionError(
            "canonical approval consumption directory is unsafe"
        ) from exc
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != operator_uid
        or stat.S_IMODE(root_stat.st_mode) != 0o700
    ):
        raise RuntimeAdmissionError(
            "canonical approval consumption directory is unsafe"
        )
    _fsync_directory(consumption_root.parent)
    nonce_sha256 = _hash(approval["nonce"])
    marker = consumption_root / f"{nonce_sha256}.json"
    payload = (
        _canonical(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "cre_capacity_review_approval_consumption",
                "approval_sha256": _hash(approval),
                "profile": approval["profile"],
                "config_sha256": config_sha256,
                "transition_receipt_sha256": receipt["receipt_sha256"],
                "source_git_sha": approval["source_git_sha"],
                "review_approval_nonce_sha256": nonce_sha256,
                "review_approval_created_at": approval["created_at"],
                "expires_after_seconds": approval["expires_after_seconds"],
                "consumed_at": utc_now(),
                "pid": os.getpid(),
            }
        )
        + b"\n"
    )
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(marker, flags, 0o600)
    except FileExistsError as exc:
        raise RuntimeAdmissionError(
            "review approval was already consumed; a fresh review is required"
        ) from exc
    except OSError as exc:
        raise RuntimeAdmissionError(
            "review approval consumption could not be recorded"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != operator_uid
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise RuntimeAdmissionError("review approval consumption marker is unsafe")
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise RuntimeAdmissionError(
                    "review approval consumption write was short"
                )
            remaining = remaining[written:]
        os.fsync(descriptor)
    except BaseException:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)
    try:
        _fsync_directory(consumption_root)
    except RuntimeAdmissionError as exc:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        raise RuntimeAdmissionError(
            "review approval consumption could not be made durable"
        ) from exc
    return marker


def preservation_checks(
    current: RuntimeCapture, baseline: Mapping[str, Any], state: str
) -> dict[str, bool]:
    baseline_api, baseline_browser = baseline["api"], baseline["browser"]
    current_api, current_browser = current.public["api"], current.public["browser"]
    checks = {
        "api_identity": current_api["id"] == baseline_api["id"],
        "api_image": current_api["image"] == baseline_api["image"],
        "api_environment": current_api["env"] == baseline_api["env"],
        "api_ports": current_api["port_bindings"] == baseline_api["port_bindings"],
        "api_network": current_api["network_mode"] == baseline_api["network_mode"],
        "api_volumes": current_api["mounts_sha256"] == baseline_api["mounts_sha256"],
        "browser_image": current_browser["image"] == baseline_browser["image"],
        "browser_environment_except_pages": current_browser["env"][
            "excluding_pages_sha256"
        ]
        == baseline_browser["env"]["excluding_pages_sha256"],
        "browser_ports": current_browser["port_bindings"]
        == baseline_browser["port_bindings"],
        "browser_network": current_browser["network_mode"]
        == baseline_browser["network_mode"],
        "browser_volumes": current_browser["mounts_sha256"]
        == baseline_browser["mounts_sha256"],
        "browser_security": current_browser["security_opt"]
        == baseline_browser["security_opt"]
        and current_browser["cap_drop"] == baseline_browser["cap_drop"],
        "browser_shm": current_browser["shm_bytes"] == baseline_browser["shm_bytes"],
    }
    if state == "baseline":
        checks["browser_environment"] = (
            current_browser["env"] == baseline_browser["env"]
        )
    return checks


def _private_overlay(
    capture: RuntimeCapture, profile: Mapping[str, Any], state: str
) -> dict[str, Any]:
    runtime, requested = profile["runtime_baseline"], profile["requested"]
    browser_env = dict(capture.browser_env)
    browser_env[PRIVATE_PAGE_KEY] = str(
        runtime["global_pages"] if state == "baseline" else requested["global_pages"]
    )
    return {
        "services": {
            "playwright-service": {
                "environment": browser_env,
                "ports": ["127.0.0.1:3103:3000"],
                "cpus": runtime["browser_cpus"]
                if state == "baseline"
                else requested["browser_cpus"],
                "pids_limit": runtime["browser_pids"]
                if state == "baseline"
                else requested["browser_pids"],
                "shm_size": runtime["browser_shm_bytes"],
                "mem_limit": runtime["browser_memory_bytes"],
                "memswap_limit": runtime["browser_memory_bytes"],
            }
        }
    }


def _compose_recreate(
    capture: RuntimeCapture,
    profile: Mapping[str, Any],
    state: str,
    runner: CommandRunner,
    *,
    execute: bool = True,
    mutation_observer: Callable[[], None] | None = None,
) -> None:
    parent = Path(
        tempfile.mkdtemp(
            prefix="cre-capacity-private-", dir=REPO_ROOT / "tasks" / "tmp"
        )
    )
    os.chmod(parent, 0o700)
    overlay = parent / "runtime-overlay.json"
    compose_env = parent / "compose.env"
    mutation_issued = False
    primary_error: BaseException | None = None
    try:
        write_private(
            overlay, _private_overlay(capture, profile, state), refuse_existing=True
        )
        _write_private_bytes(
            compose_env, b"PLAYWRIGHT_HOST_PORT=3103\n", refuse_existing=True
        )
        command_env = {
            key: os.environ[key]
            for key in (
                "PATH",
                "HOME",
                "DOCKER_CONFIG",
                "DOCKER_HOST",
                "DOCKER_CONTEXT",
                "DOCKER_TLS_VERIFY",
                "DOCKER_CERT_PATH",
            )
            if key in os.environ
        }
        command_env["PLAYWRIGHT_HOST_PORT"] = "3103"
        prefix = [
            "docker",
            "compose",
            "--env-file",
            str(compose_env),
            "-f",
            str(COMPOSE_PATH),
            "-f",
            str(OVERRIDE_PATH),
            "-f",
            str(overlay),
        ]
        try:
            configured = json.loads(
                _run(
                    runner,
                    [*prefix, "config", "--format", "json"],
                    cwd=REPO_ROOT,
                    env=command_env,
                )
            )
        except json.JSONDecodeError as exc:
            raise RuntimeAdmissionError(
                "resolved browser candidate is invalid"
            ) from exc
        service = configured.get("services", {}).get("playwright-service", {})
        expected = _private_overlay(capture, profile, state)["services"][
            "playwright-service"
        ]
        target_port = [
            {
                "host_ip": "127.0.0.1",
                "mode": "ingress",
                "protocol": "tcp",
                "published": "3103",
                "target": 3000,
            }
        ]
        image_id = _run(
            runner,
            [
                "docker",
                "image",
                "inspect",
                "firecrawl-playwright-service:latest",
                "--format",
                "{{.Id}}",
            ],
            cwd=REPO_ROOT,
        ).strip()
        configured_env = service.get("environment")
        if not (
            isinstance(configured_env, Mapping)
            and _hash(dict(configured_env)) == _hash(expected["environment"])
            and service.get("ports") == target_port
            and service.get("cpus") == expected["cpus"]
            and service.get("pids_limit") == expected["pids_limit"]
            and str(service.get("shm_size")) == str(expected["shm_size"])
            and str(service.get("mem_limit")) == str(expected["mem_limit"])
            and str(service.get("memswap_limit")) == str(expected["memswap_limit"])
            and service.get("networks") == {"backend": None}
            and service.get("volumes") is None
            and service.get("tmpfs") == ["/tmp/.cache:noexec,nosuid,size=1g"]
            and service.get("security_opt") == ["no-new-privileges:true"]
            and service.get("cap_drop") == ["ALL"]
            and image_id == capture.public["browser"]["image"]
        ):
            raise RuntimeAdmissionError(
                "resolved browser candidate contains unapproved drift"
            )
        if execute:
            argv = [
                *prefix,
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--pull",
                "never",
                "--force-recreate",
                "playwright-service",
            ]
            mutation_issued = True
            if mutation_observer is not None:
                mutation_observer()
            try:
                _run(runner, argv, cwd=REPO_ROOT, env=command_env)
            except RuntimeAdmissionError as exc:
                raise RuntimeMutationError(
                    "browser recreation failed after mutation request"
                ) from exc
    except BaseException as exc:  # noqa: BLE001 - preserve interrupts through cleanup
        primary_error = exc
    finally:
        cleanup_errors: list[OSError] = []
        for path in (overlay, compose_env):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_errors.append(exc)
        try:
            parent.rmdir()
        except OSError as exc:
            cleanup_errors.append(exc)
        if primary_error is not None:
            for cleanup_error in cleanup_errors:
                primary_error.add_note(
                    f"private runtime overlay cleanup also failed: {cleanup_error}"
                )
            raise primary_error
        if cleanup_errors:
            error_type = (
                RuntimeOverlayCleanupError if mutation_issued else RuntimeAdmissionError
            )
            raise error_type(
                "private runtime overlay cleanup failed"
            ) from cleanup_errors[0]


def _api_update(
    profile: Mapping[str, Any],
    state: str,
    runner: CommandRunner,
    *,
    mutation_observer: Callable[[], None] | None = None,
) -> None:
    runtime, requested = profile["runtime_baseline"], profile["requested"]
    cpus = runtime["api_cpus"] if state == "baseline" else requested["api_cpus"]
    memory = runtime["api_memory_bytes"]
    if mutation_observer is not None:
        mutation_observer()
    try:
        _run(
            runner,
            [
                "docker",
                "update",
                "--cpus",
                str(cpus),
                "--memory",
                str(memory),
                "--memory-swap",
                str(memory),
                API_CONTAINER,
            ],
            cwd=REPO_ROOT,
        )
    except RuntimeAdmissionError as exc:
        raise RuntimeMutationError("API update failed after mutation request") from exc


def verify_capture(
    capture: RuntimeCapture,
    receipt: Mapping[str, Any],
    profile: Mapping[str, Any],
    state: str,
) -> dict[str, bool]:
    checks = evaluate_state(capture.public, profile, state)
    checks.update(preservation_checks(capture, receipt["baseline"], state))
    checks["source_inputs_unchanged"] = _repo_transition_invariants(
        capture.public["repo"]
    ) == _repo_transition_invariants(receipt["baseline"]["repo"])
    checks["host_identity_unchanged"] = all(
        capture.public["host"].get(key) == receipt["baseline"]["host"].get(key)
        for key in ("orb_status", "orbstack_memory_mib", "docker_context")
    )
    return checks


def _repo_transition_invariants(repo: Mapping[str, Any]) -> dict[str, Any]:
    """Return execution-relevant repo state; dirt is diagnostic by contract."""
    return {key: value for key, value in repo.items() if key != "dirty"}


def _component_states(
    current: RuntimeCapture,
    receipt: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> tuple[str, str]:
    """Classify only exact baseline/candidate component states.

    Shared topology, source inputs, host capacity, endpoints, and idle queues
    must remain admitted. Any third configuration is unrelated drift.
    """
    baseline = receipt["baseline"]
    common = evaluate_state(current.public, profile, "candidate")
    for key in ("browser_cpu", "browser_pages", "browser_pids", "api_cpu"):
        common.pop(key)
    preserved = preservation_checks(current, baseline, "candidate")
    exact_repo = _repo_transition_invariants(
        current.public["repo"]
    ) == _repo_transition_invariants(baseline["repo"])
    exact_host = all(
        current.public["host"].get(key) == baseline["host"].get(key)
        for key in ("orb_status", "orbstack_memory_mib", "docker_context")
    )
    if not (
        all(common.values()) and all(preserved.values()) and exact_repo and exact_host
    ):
        raise RuntimeAdmissionError(
            "runtime contains unrelated drift; rollback refused"
        )

    baseline_checks = evaluate_state(current.public, profile, "baseline")
    candidate_checks = evaluate_state(current.public, profile, "candidate")
    browser_keys = ("browser_cpu", "browser_pages", "browser_pids")
    browser_baseline = all(baseline_checks[key] for key in browser_keys)
    browser_candidate = all(candidate_checks[key] for key in browser_keys)
    api_baseline = baseline_checks["api_cpu"]
    api_candidate = candidate_checks["api_cpu"]
    if browser_baseline == browser_candidate or api_baseline == api_candidate:
        raise RuntimeAdmissionError(
            "runtime components are neither exact baseline nor exact candidate"
        )
    return (
        "baseline" if browser_baseline else "candidate",
        "baseline" if api_baseline else "candidate",
    )


def _canonical_transition_lock() -> Path:
    try:
        path = canonical_shared_lock_dir(REPO_ROOT).resolve()
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeAdmissionError("canonical CRE lock path is unavailable") from exc
    if path.name != ".cre.lock":
        raise RuntimeAdmissionError("canonical CRE lock path is invalid")
    return path


def _signal_as_interrupt(signum: int, _frame: object) -> None:
    raise KeyboardInterrupt(f"runtime transition interrupted by signal {signum}")


@contextmanager
def _transition_signal_handlers():
    previous: dict[int, Any] = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, _signal_as_interrupt)
    except ValueError as exc:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        raise RuntimeAdmissionError(
            "runtime execution must run in the main process thread"
        ) from exc
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@contextmanager
def _delay_transition_signals():
    """Deliver cancellation after ownership has crossed a helper boundary."""
    previous: dict[int, Any] = {}
    pending: list[int] = []

    def record(signum: int, _frame: object) -> None:
        pending.append(signum)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, record)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if pending:
        _signal_as_interrupt(pending[0], None)


@contextmanager
def _defer_transition_signals():
    previous: dict[int, Any] = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, signal.SIG_IGN)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _restore_baseline(
    current: RuntimeCapture,
    receipt: Mapping[str, Any],
    profile: Mapping[str, Any],
    runner: CommandRunner,
) -> RuntimeCapture:
    """Resume a safe rollback from baseline, candidate, or a mixed state."""
    browser_state, api_state = _component_states(current, receipt, profile)
    cleanup_error: RuntimeOverlayCleanupError | None = None
    if browser_state == "candidate":
        try:
            _compose_recreate(current, profile, "baseline", runner)
        except RuntimeOverlayCleanupError as exc:
            cleanup_error = exc
        current = capture_runtime(runner)
        observed_browser, observed_api = _component_states(current, receipt, profile)
        if observed_browser != "baseline" or observed_api != api_state:
            raise RuntimeAdmissionError(
                "browser rollback did not preserve the API component"
            )
    if api_state == "candidate":
        _api_update(profile, "baseline", runner)
        current = capture_runtime(runner)
    final_browser, final_api = _component_states(current, receipt, profile)
    checks = verify_capture(current, receipt, profile, "baseline")
    if (final_browser, final_api) != ("baseline", "baseline") or not all(
        checks.values()
    ):
        raise RuntimeAdmissionError("rollback verification failed")
    if cleanup_error is not None:
        raise cleanup_error
    return current


def _compensate_candidate_attempt(
    current: RuntimeCapture,
    receipt: Mapping[str, Any],
    profile: Mapping[str, Any],
    runner: CommandRunner,
) -> RuntimeCapture:
    """Reverse an issued candidate attempt even when its resources are partial."""
    baseline = receipt["baseline"]
    preserved = preservation_checks(current, baseline, "candidate")
    exact_repo = _repo_transition_invariants(
        current.public["repo"]
    ) == _repo_transition_invariants(baseline["repo"])
    exact_host = all(
        current.public["host"].get(key) == baseline["host"].get(key)
        for key in ("orb_status", "orbstack_memory_mib", "docker_context")
    )
    if not all(preserved.values()) or not exact_repo or not exact_host:
        raise RuntimeAdmissionError(
            "automatic rollback refused after unrelated runtime drift"
        )
    failures: list[BaseException] = []
    try:
        _compose_recreate(current, profile, "baseline", runner)
    except BaseException as exc:  # noqa: BLE001 - compensation must continue
        failures.append(exc)
    try:
        _api_update(profile, "baseline", runner)
    except BaseException as exc:  # noqa: BLE001 - verification still must run
        failures.append(exc)
    baseline_verified = False
    restored: RuntimeCapture | None = None
    try:
        restored = capture_runtime(runner)
        checks = verify_capture(restored, receipt, profile, "baseline")
        baseline_verified = all(checks.values())
    except BaseException as exc:  # noqa: BLE001 - preserve command failures
        failures.append(exc)
    if failures or not baseline_verified:
        outcome = (
            "baseline verified, but rollback commands reported uncertainty"
            if baseline_verified
            else "baseline could not be verified after both rollback attempts"
        )
        error = RuntimeCompensationError(outcome, baseline_verified=baseline_verified)
        for failure in failures[1:]:
            error.add_note(f"additional compensation failure: {failure}")
        raise error from (failures[0] if failures else None)
    assert restored is not None
    return restored


def transition(
    receipt_path: Path,
    profile_name: str,
    state: str,
    *,
    execute: bool,
    runner: CommandRunner = _default_runner,
    admission_out: Path | None = None,
    approval_path: Path | None = None,
) -> dict[str, Any]:
    receipt, profile, digest = load_fresh_receipt(
        receipt_path, profile_name, require_fresh=state == "candidate"
    )
    if execute and state == "candidate":
        if admission_out is None:
            raise RuntimeAdmissionError(
                "candidate execution requires a private admission output path"
            )
        admission_out = _controller_output(admission_out)
        if admission_out.exists():
            raise RuntimeAdmissionError("admission output path already exists")
        if approval_path is None:
            raise RuntimeAdmissionError(
                "candidate execution requires a coordinating-review approval file"
            )
    lock_path = _canonical_transition_lock()
    plan = {
        "profile": profile_name,
        "state": state,
        "execute": execute,
        "review_approval_required": state == "candidate",
        "canonical_lock": str(lock_path),
        "commands": receipt["apply_plan"]
        if state == "candidate"
        else receipt["rollback_plan"],
    }
    if not execute:
        current = capture_runtime(runner)
        baseline = receipt["baseline"]
        try:
            browser_state, api_state = _component_states(current, receipt, profile)
        except RuntimeAdmissionError as exc:
            if state == "candidate":
                raise RuntimeAdmissionError("runtime drifted after preflight") from exc
            raise
        if state == "candidate" and (
            current.public["transition_sha256"] != baseline["transition_sha256"]
            or (browser_state, api_state) != ("baseline", "baseline")
        ):
            raise RuntimeAdmissionError("runtime drifted after preflight")
        _compose_recreate(current, profile, state, runner, execute=False)
        if state == "baseline":
            plan["commands"] = {
                component: receipt["rollback_plan"][component]
                for component, observed in (
                    ("browser", browser_state),
                    ("api", api_state),
                )
                if observed == "candidate"
            }
        plan["observed_components"] = {
            "browser": browser_state,
            "api": api_state,
        }
        return plan

    mutation_issued = False
    review_grant_path: Path | None = None

    def mark_mutation() -> None:
        nonlocal mutation_issued
        mutation_issued = True

    with _transition_signal_handlers():
        try:
            lock = SharedLock(lock_path)
            with _defer_transition_signals():
                lock.acquire()
        except LockHeldError as exc:
            raise RuntimeAdmissionError(str(exc)) from exc
        try:
            current = capture_runtime(runner)
            baseline = receipt["baseline"]
            try:
                browser_state, api_state = _component_states(current, receipt, profile)
            except RuntimeAdmissionError as exc:
                if state == "candidate":
                    raise RuntimeAdmissionError(
                        "runtime drifted after preflight"
                    ) from exc
                raise
            if state == "candidate":
                if current.public["transition_sha256"] != baseline[
                    "transition_sha256"
                ] or (browser_state, api_state) != ("baseline", "baseline"):
                    raise RuntimeAdmissionError("runtime drifted after preflight")
                assert approval_path is not None
                with _delay_transition_signals():
                    approval, review_grant_path = consume_review_approval(
                        approval_path, receipt, profile_name, digest
                    )
                    _record_review_approval_consumption(
                        lock_path, approval, receipt, digest
                    )
                _compose_recreate(
                    current,
                    profile,
                    "candidate",
                    runner,
                    mutation_observer=mark_mutation,
                )
                mutation_issued = True
                _api_update(
                    profile,
                    "candidate",
                    runner,
                    mutation_observer=mark_mutation,
                )
                after = capture_runtime(runner)
                checks = verify_capture(after, receipt, profile, "candidate")
                if not all(checks.values()):
                    raise RuntimeAdmissionError("candidate verification failed")
            else:
                after = _restore_baseline(current, receipt, profile, runner)
                checks = verify_capture(after, receipt, profile, "baseline")

            result = {
                "profile": profile_name,
                "state": state,
                "checks": checks,
                "verified": True,
            }
            if state == "candidate" and admission_out is not None:
                admission = {
                    "schema_version": SCHEMA_VERSION,
                    "kind": ADMISSION_KIND,
                    "profile": profile_name,
                    "config_sha256": digest,
                    "source_git_sha": after.public["repo"]["git_sha"],
                    "transition_receipt_sha256": receipt["receipt_sha256"],
                    "review_approval_nonce_sha256": _hash(approval["nonce"]),
                    "review_approval_created_at": approval["created_at"],
                    "review_benchmark_grant_path": str(review_grant_path),
                    "created_at": utc_now(),
                    "expires_after_seconds": RECEIPT_MAX_AGE_SECONDS,
                    "admitted": True,
                    "writes": "forbidden",
                    "checks": checks,
                    "effective": after.public,
                }
                write_private(admission_out, admission, refuse_existing=True)
                result["admission_path"] = str(admission_out)
            return result
        except BaseException:
            grant_cleanup_error: BaseException | None = None
            if state == "candidate" and review_grant_path is not None:
                try:
                    with _defer_transition_signals():
                        _destroy_review_benchmark_grant(review_grant_path)
                except BaseException as cleanup_exc:  # noqa: BLE001 - keep compensating
                    grant_cleanup_error = cleanup_exc
            compensation_error: BaseException | None = None
            if state == "candidate" and mutation_issued:
                try:
                    with _defer_transition_signals():
                        admission_cleanup_error: OSError | None = None
                        if admission_out is not None:
                            try:
                                admission_out.unlink(missing_ok=True)
                            except OSError as exc:
                                admission_cleanup_error = exc
                        try:
                            rollback_capture = capture_runtime(runner)
                        except (RuntimeAdmissionError, OSError):
                            rollback_capture = _capture_recovery_state(current, runner)
                        _compensate_candidate_attempt(
                            rollback_capture, receipt, profile, runner
                        )
                        if admission_cleanup_error is not None:
                            raise RuntimeAdmissionError(
                                "baseline restored but admission cleanup failed"
                            ) from admission_cleanup_error
                except BaseException as rollback_exc:  # noqa: BLE001 - report both
                    compensation_error = rollback_exc
            if compensation_error is not None or grant_cleanup_error is not None:
                if isinstance(compensation_error, RuntimeCompensationError):
                    detail = f"automatic rollback failed: {compensation_error}"
                elif isinstance(compensation_error, RuntimeOverlayCleanupError):
                    detail = (
                        "automatic rollback restored resources but private "
                        "cleanup failed"
                    )
                elif compensation_error is not None:
                    detail = "automatic rollback verification failed"
                else:
                    detail = "unused review benchmark grant cleanup failed"
                failure = RuntimeAdmissionError(detail)
                if grant_cleanup_error is not None and compensation_error is not None:
                    failure.add_note(
                        f"review grant cleanup also failed: {grant_cleanup_error}"
                    )
                raise failure from (compensation_error or grant_cleanup_error)
            raise
        finally:
            with _defer_transition_signals():
                lock.release()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    pre = subparsers.add_parser("preflight")
    pre.add_argument("--profile", default="bold-jll-128")
    pre.add_argument("--out", type=Path, required=True)
    for command in ("apply", "rollback"):
        selected = subparsers.add_parser(command)
        selected.add_argument("--profile", default="bold-jll-128")
        selected.add_argument("--receipt", type=Path, required=True)
        selected.add_argument("--execute", action="store_true")
        if command == "apply":
            selected.add_argument("--admission-out", type=Path)
            selected.add_argument("--approval", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            result = preflight(args.profile, args.out)
        else:
            result = transition(
                args.receipt,
                args.profile,
                "candidate" if args.command == "apply" else "baseline",
                execute=args.execute,
                admission_out=getattr(args, "admission_out", None),
                approval_path=getattr(args, "approval", None),
            )
        print(json.dumps(result, sort_keys=True, indent=2))
    except (RuntimeAdmissionError, experiment.ProfileError, OSError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
