"""Fail-closed runtime controller for the named CRE capacity experiment.

Preflight observations and verification are read-only; preflight writes only a
private receipt. Apply and rollback are dry-run by default. Candidate mutation
requires ``--execute``, a fresh machine receipt, and a separate root-review
attestation. No command reads the repository ``.env`` file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cre_capacity_experiment as experiment

SCHEMA_VERSION = 1
RECEIPT_KIND = "cre_capacity_runtime_transition"
ADMISSION_KIND = "cre_capacity_runtime_admission"
APPROVAL_KIND = "cre_capacity_root_approval"
RECEIPT_MAX_AGE_SECONDS = 600
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
}
PRIVATE_PAGE_KEY = "MAX_CONCURRENT_PAGES"
SHA_PATTERN = re.compile(r"[0-9a-f]{40,64}\Z")


class RuntimeAdmissionError(RuntimeError):
    """Runtime state cannot be admitted or changed safely."""


class RuntimeMutationError(RuntimeAdmissionError):
    """A runtime mutation command was issued but did not complete cleanly."""


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
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            response.read(1)
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise RuntimeAdmissionError("loopback runtime endpoint unavailable") from exc


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
    git_sha = _run(runner, ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT).strip()
    dirty = bool(_run(runner, ["git", "status", "--porcelain"], cwd=REPO_ROOT).strip())
    public = {
        "repo": {
            "git_sha": git_sha,
            "dirty": dirty,
            "compose_sha256": _file_hash(COMPOSE_PATH),
            "override_sha256": _file_hash(OVERRIDE_PATH),
            "execution_inputs_sha256": {
                key: _file_hash(path) for key, path in EXECUTION_INPUTS.items()
            },
        },
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


def load_root_approval(
    path: Path,
    receipt: Mapping[str, Any],
    profile_name: str,
    config_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeAdmissionError("root approval must be a regular private file")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeAdmissionError("root approval file must have mode 0600")
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise RuntimeAdmissionError("root approval directory must be private")
    approval = experiment._read_json(path)
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
        or approval.get("approved_by") != "root-review"
        or approval.get("approved") is not True
        or approval.get("expires_after_seconds") != RECEIPT_MAX_AGE_SECONDS
    ):
        raise RuntimeAdmissionError(
            "root approval does not bind the admitted transition"
        )
    current = now or datetime.now(timezone.utc)
    age = (current - _parse_time(approval.get("created_at"))).total_seconds()
    if age < 0 or age > RECEIPT_MAX_AGE_SECONDS:
        raise RuntimeAdmissionError("root approval is stale")
    return approval


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
) -> None:
    parent = Path(
        tempfile.mkdtemp(
            prefix="cre-capacity-private-", dir=REPO_ROOT / "tasks" / "tmp"
        )
    )
    os.chmod(parent, 0o700)
    overlay = parent / "runtime-overlay.json"
    compose_env = parent / "compose.env"
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
            try:
                _run(runner, argv, cwd=REPO_ROOT, env=command_env)
            except RuntimeAdmissionError as exc:
                raise RuntimeMutationError(
                    "browser recreation failed after mutation request"
                ) from exc
    finally:
        overlay.unlink(missing_ok=True)
        compose_env.unlink(missing_ok=True)
        try:
            parent.rmdir()
        except OSError as exc:
            raise RuntimeAdmissionError(
                "private runtime overlay cleanup failed"
            ) from exc


def _api_update(profile: Mapping[str, Any], state: str, runner: CommandRunner) -> None:
    runtime, requested = profile["runtime_baseline"], profile["requested"]
    cpus = runtime["api_cpus"] if state == "baseline" else requested["api_cpus"]
    memory = runtime["api_memory_bytes"]
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


def verify_capture(
    capture: RuntimeCapture,
    receipt: Mapping[str, Any],
    profile: Mapping[str, Any],
    state: str,
) -> dict[str, bool]:
    checks = evaluate_state(capture.public, profile, state)
    checks.update(preservation_checks(capture, receipt["baseline"], state))
    return checks


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
                "candidate execution requires a root-review approval file"
            )
        load_root_approval(approval_path, receipt, profile_name, digest)
    current = capture_runtime(runner)
    baseline = receipt["baseline"]
    if state == "candidate":
        current_checks = verify_capture(current, receipt, profile, "baseline")
        if current.public["transition_sha256"] != baseline[
            "transition_sha256"
        ] or not all(current_checks.values()):
            raise RuntimeAdmissionError("runtime drifted after preflight")
    else:
        candidate_checks = verify_capture(current, receipt, profile, "candidate")
        if not all(candidate_checks.values()):
            raise RuntimeAdmissionError(
                "rollback requires the exact admitted candidate state"
            )
    plan = {
        "profile": profile_name,
        "state": state,
        "execute": execute,
        "root_approval_required": state == "candidate",
        "commands": receipt["apply_plan"]
        if state == "candidate"
        else receipt["rollback_plan"],
    }
    if not execute:
        _compose_recreate(current, profile, state, runner, execute=False)
        return plan
    mutation_started = False
    try:
        _compose_recreate(current, profile, state, runner)
        mutation_started = True
        _api_update(profile, state, runner)
        after = capture_runtime(runner)
        checks = verify_capture(after, receipt, profile, state)
        if not all(checks.values()):
            raise RuntimeAdmissionError(f"{state} verification failed")
    except Exception as exc:
        if state == "candidate" and (
            mutation_started or isinstance(exc, RuntimeMutationError)
        ):
            rollback_capture = capture_runtime(runner)
            preserve = preservation_checks(rollback_capture, baseline, "candidate")
            if not preserve["browser_environment_except_pages"]:
                raise RuntimeAdmissionError(
                    "automatic rollback refused after unrelated environment drift"
                )
            _api_update(profile, "baseline", runner)
            _compose_recreate(rollback_capture, profile, "baseline", runner)
            restored = capture_runtime(runner)
            rollback_checks = verify_capture(restored, receipt, profile, "baseline")
            if not all(rollback_checks.values()):
                raise RuntimeAdmissionError(
                    "automatic rollback verification failed"
                ) from exc
        raise
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
