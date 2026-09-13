"""Bounded, body-free performance evidence for CRE child commands.

This module is diagnostic only.  It never changes a command's arguments or
result, and failures to write optional performance evidence do not change the
underlying command outcome.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from cre_ingest import SOURCE_TO_BROKERAGE

SCHEMA_VERSION = 1
PERFORMANCE_KIND = "cre_command_performance"
PERFORMANCE_WARNING = (
    "warning: CRE performance telemetry degraded; acquisition continues"
)
MAX_RECORD_BYTES = 4 * 1024
MAX_JOURNAL_BYTES = 1024 * 1024
MAX_RUNTIME_BYTES = 64 * 1024
MAX_RUNTIME_OUTPUT_BYTES = 64 * 1024

_RUNTIME_CONTAINER_NAMES = (
    "firecrawl-api-1",
    "firecrawl-playwright-service-1",
)
_RUNTIME_INSPECT_FORMAT = (
    '{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},'
    '"started_at":{{json .State.StartedAt}},'
    '"nano_cpus":{{json .HostConfig.NanoCpus}},'
    '"memory":{{json .HostConfig.Memory}},'
    '"memory_swap":{{json .HostConfig.MemorySwap}},'
    '"pids_limit":{{json .HostConfig.PidsLimit}},'
    '"shm_size":{{json .HostConfig.ShmSize}},'
    '"ports":{{json .NetworkSettings.Ports}}}'
)

_RUN_ID_PATTERN = re.compile(
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{6}Z)(?:-[0-9a-f]{12})?\Z"
)
_COMMAND_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_SAFE_BASENAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}\Z")
_CONTAINER_ID_PATTERN = re.compile(r"[0-9a-f]{12,64}\Z")
_IMAGE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PORT_KEY_PATTERN = re.compile(
    r"(?P<port>[1-9][0-9]{0,4})/(?P<protocol>tcp|udp|sctp)\Z"
)
_PHASES = frozenset(
    {
        "collection",
        "healthcheck",
        "pre_validation",
        "source_gate",
        "dry_run",
        "aggregate_gate",
        "ingest",
        "ingest_recovery",
        "readback",
        "unknown",
    }
)
_MAX_CONFIGURED_INTEGER = 1_000_000_000
_CONFIGURED_FLAGS = {
    "--concurrency": "configured_concurrency",
    "--page-cap": "configured_page_cap",
    "--max-items": "configured_max_items",
}


class CommandRunner(Protocol):
    """The existing command runner contract wrapped by telemetry."""

    def __call__(
        self,
        argv: Sequence[str],
        log_path: Path,
        *,
        env: Mapping[str, str] | None = None,
    ) -> int: ...


def utc_now() -> str:
    """Return a compact UTC observation timestamp."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _validate_run_id(value: str) -> str:
    match = _RUN_ID_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("invalid performance run id")
    try:
        datetime.strptime(match.group("timestamp"), "%Y-%m-%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ValueError("invalid performance run timestamp") from exc
    return value


def _safe_basename(value: str) -> str:
    if _SAFE_BASENAME_PATTERN.fullmatch(value) is None or value in {".", ".."}:
        raise ValueError("unsafe performance filename")
    return value


def _argv_strings(argv: Sequence[str]) -> tuple[str, ...]:
    """Return only bounded strings suitable for diagnostic classification."""
    values: list[str] = []
    for index, value in enumerate(argv):
        if index >= 128:
            return ()
        if not isinstance(value, str) or len(value) > 2048:
            return ()
        values.append(value)
    return tuple(values)


def _executable_name(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def _is_collect_command(argv: Sequence[str]) -> bool:
    values = _argv_strings(argv)
    if len(values) < 3:
        return False
    return (
        _executable_name(values[0]) in {"npx", "npx.cmd"}
        and _executable_name(values[1]) in {"tsx", "tsx.cmd"}
        and _executable_name(values[2]) == "collect.ts"
    )


def _option_value(argv: tuple[str, ...], option: str) -> str | None:
    selected: str | None = None
    for index, value in enumerate(argv):
        if value.startswith(f"{option}="):
            selected = value[len(option) + 1 :]
        elif value == option and index + 1 < len(argv):
            selected = argv[index + 1]
    return selected


def _configured_numbers(argv: Sequence[str]) -> dict[str, int]:
    values = _argv_strings(argv)
    if not values:
        return {}
    configured: dict[str, int] = {}
    for option, field in _CONFIGURED_FLAGS.items():
        raw = _option_value(values, option)
        if raw is None or len(raw) > 10 or not raw.isascii() or not raw.isdigit():
            continue
        number = int(raw)
        if number <= _MAX_CONFIGURED_INTEGER:
            configured[field] = number
    return configured


def _known_source(argv: Sequence[str], command_log: str) -> str | None:
    values = _argv_strings(argv)
    candidate = _option_value(values, "--source") if values else None
    if candidate is None and values:
        candidate = _option_value(values, "--_cohort-collect-source")
    if candidate in SOURCE_TO_BROKERAGE:
        return candidate
    for source in sorted(SOURCE_TO_BROKERAGE, key=len, reverse=True):
        if command_log.startswith(f"{source}-"):
            return source
    return None


def _phase(argv: Sequence[str], command_log: str) -> str:
    if command_log == "healthcheck.log":
        return "healthcheck"
    if command_log == "pre-validation.log":
        return "pre_validation"
    if command_log == "aggregate-gate.log":
        return "aggregate_gate"
    if command_log == "validation.log":
        return "readback"
    if command_log.endswith("-ingest-recovery.log"):
        return "ingest_recovery"
    if command_log.endswith("-ingest-dry-run.log"):
        return "dry_run"
    if command_log.endswith("-ingest.log"):
        return "ingest"
    if command_log.endswith("-gate.log"):
        return "source_gate"
    if "-collect-attempt-" in command_log or _is_collect_command(argv):
        return "collection"

    values = _argv_strings(argv)
    executable_names = {_executable_name(value) for value in values[:3]}
    if "firecrawl_healthcheck.sh" in executable_names:
        return "healthcheck"
    if "cre_validate.py" in executable_names:
        return "readback"
    if "cre_gate.py" in executable_names:
        return "source_gate"
    if "cre_ingest.py" in executable_names:
        return "dry_run" if "--dry-run" in values else "ingest"
    return "unknown"


def _elapsed_seconds(started: float | None, finished: float | None) -> float | None:
    if (
        started is None
        or finished is None
        or not math.isfinite(started)
        or not math.isfinite(finished)
        or finished < started
    ):
        return None
    return round(finished - started, 6)


def _append_jsonl_record(path: Path, record: Mapping[str, object]) -> None:
    """Append one bounded record without following or blocking on file aliases."""
    encoded = (
        json.dumps(
            record,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    if len(encoded) > MAX_RECORD_BYTES:
        raise ValueError("performance record exceeds cap")

    path.parent.mkdir(parents=True, exist_ok=True)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("no-follow file opens are unavailable")
    flags = os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= no_follow
    fd = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("performance journal target is not a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OSError("performance journal is busy") from exc
        metadata = os.fstat(fd)
        os.fchmod(fd, 0o600)
        if metadata.st_size > MAX_JOURNAL_BYTES - len(encoded):
            raise OSError("performance journal exceeds cap")
        written = os.write(fd, encoded)
        if written != len(encoded):
            raise OSError("short performance journal write")
    finally:
        os.close(fd)


def _positive_bounded_int(
    value: object,
    *,
    maximum: int,
    allow_zero: bool = True,
    allow_negative_one: bool = False,
) -> int | None:
    if type(value) is not int:
        return None
    if allow_negative_one and value == -1:
        return value
    minimum = 0 if allow_zero else 1
    return value if minimum <= value <= maximum else None


def _runtime_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (OverflowError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _runtime_ports(value: object) -> list[str] | None:
    if not isinstance(value, Mapping) or len(value) > 32:
        return None
    ports: list[str] = []
    for raw_key, raw_bindings in value.items():
        if not isinstance(raw_key, str):
            return None
        matched = _PORT_KEY_PATTERN.fullmatch(raw_key)
        if matched is None or int(matched.group("port")) > 65535:
            return None
        prefix = f"{int(matched.group('port'))}/{matched.group('protocol')}"
        if raw_bindings is None:
            ports.append(f"{prefix}->")
            continue
        if not isinstance(raw_bindings, list) or len(raw_bindings) > 8:
            return None
        published: list[int] = []
        for binding in raw_bindings:
            if not isinstance(binding, Mapping):
                return None
            raw_public = binding.get("HostPort")
            if (
                not isinstance(raw_public, str)
                or len(raw_public) > 5
                or not raw_public.isascii()
                or not raw_public.isdigit()
            ):
                return None
            public = int(raw_public)
            if not 1 <= public <= 65535:
                return None
            published.append(public)
        if not published:
            ports.append(f"{prefix}->")
        else:
            ports.extend(f"{prefix}->{public}" for public in sorted(set(published)))
    return sorted(ports)


def _runtime_container(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    raw_name = value.get("name")
    name = raw_name.removeprefix("/") if isinstance(raw_name, str) else ""
    container_id = value.get("id")
    image = value.get("image")
    nano_cpus = _positive_bounded_int(
        value.get("nano_cpus"), maximum=4096 * 1_000_000_000
    )
    memory = _positive_bounded_int(value.get("memory"), maximum=2**63 - 1)
    memory_swap = _positive_bounded_int(
        value.get("memory_swap"),
        maximum=2**63 - 1,
        allow_negative_one=True,
    )
    raw_pids_limit = value.get("pids_limit")
    pids_limit = (
        None
        if raw_pids_limit is None
        else _positive_bounded_int(raw_pids_limit, maximum=10_000_000)
    )
    shm_size = _positive_bounded_int(value.get("shm_size"), maximum=2**63 - 1)
    ports = _runtime_ports(value.get("ports"))
    if (
        name not in _RUNTIME_CONTAINER_NAMES
        or not isinstance(container_id, str)
        or _CONTAINER_ID_PATTERN.fullmatch(container_id) is None
        or not isinstance(image, str)
        or _IMAGE_ID_PATTERN.fullmatch(image) is None
        or nano_cpus is None
        or memory is None
        or memory_swap is None
        or (raw_pids_limit is not None and pids_limit is None)
        or shm_size is None
        or ports is None
    ):
        return None
    return {
        "id": container_id,
        "name": name,
        "image": image,
        "started_at": _runtime_timestamp(value.get("started_at")),
        "cpu_limit": round(nano_cpus / 1_000_000_000, 9),
        "memory_limit_bytes": memory,
        "memory_swap_limit_bytes": memory_swap,
        "pids_limit": pids_limit,
        "shm_size_bytes": shm_size,
        "ports": ports,
    }


def _parse_runtime_inspect(output: str) -> list[dict[str, object]] | None:
    if not isinstance(output, str):
        return None
    if len(output) > MAX_RUNTIME_OUTPUT_BYTES:
        return None
    try:
        encoded_length = len(output.encode("utf-8"))
    except UnicodeError:
        return None
    if encoded_length > MAX_RUNTIME_OUTPUT_BYTES:
        return None
    rows: list[dict[str, object]] = []
    for line in output.splitlines():
        if not line or len(line) > MAX_RECORD_BYTES:
            return None
        try:
            parsed = json.loads(line)
        except (json.JSONDecodeError, RecursionError, UnicodeError):
            return None
        row = _runtime_container(parsed)
        if row is None:
            return None
        rows.append(row)
    if len(rows) != len(_RUNTIME_CONTAINER_NAMES) or {
        row["name"] for row in rows
    } != set(_RUNTIME_CONTAINER_NAMES):
        return None
    return sorted(rows, key=lambda row: str(row["name"]))


def _hardware_configuration() -> dict[str, int | None]:
    try:
        logical_cpu_count = os.cpu_count()
    except (OSError, RuntimeError):
        logical_cpu_count = None
    if type(logical_cpu_count) is not int or not 1 <= logical_cpu_count <= 4096:
        logical_cpu_count = None
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        memory_bytes = pages * page_size
    except (OSError, TypeError, ValueError):
        memory_bytes = None
    if type(memory_bytes) is not int or not 1 <= memory_bytes <= 2**63 - 1:
        memory_bytes = None
    return {
        "logical_cpu_count": logical_cpu_count,
        "memory_bytes": memory_bytes,
    }


def _atomic_write_runtime(path: Path, value: Mapping[str, object]) -> None:
    encoded = (
        json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    if len(encoded) > MAX_RUNTIME_BYTES:
        raise ValueError("runtime performance snapshot exceeds cap")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None and not stat.S_ISREG(metadata.st_mode):
        raise OSError("runtime performance target is not a regular file")

    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp = Path(raw_temp)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short runtime performance write")
            view = view[written:]
        os.close(fd)
        fd = -1
        os.replace(temp, path)
    finally:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)


def capture_runtime_configuration(run_dir: Path) -> dict[str, object] | None:
    """Write one bounded configuration snapshot without affecting workflow."""
    warning_emitted = False

    def warn_once() -> None:
        nonlocal warning_emitted
        if warning_emitted:
            return
        warning_emitted = True
        try:
            print(PERFORMANCE_WARNING, file=sys.stderr)
        except (OSError, TypeError, ValueError):
            return

    try:
        run_id = _validate_run_id(run_dir.name)
    except (TypeError, ValueError):
        warn_once()
        return None

    containers: list[dict[str, object]] = []
    availability = "available"
    unavailable_code: str | None = None
    inspect_argv = [
        "docker",
        "inspect",
        "--type",
        "container",
        "--format",
        _RUNTIME_INSPECT_FORMAT,
        *_RUNTIME_CONTAINER_NAMES,
    ]
    try:
        completed = subprocess.run(
            inspect_argv,
            capture_output=True,
            check=False,
            text=True,
            timeout=2.0,
        )
    except subprocess.TimeoutExpired:
        availability = "unavailable"
        unavailable_code = "inspect_timeout"
    except (OSError, RuntimeError, ValueError):
        availability = "unavailable"
        unavailable_code = "docker_unavailable"
    else:
        if completed.returncode != 0:
            availability = "unavailable"
            unavailable_code = "inspect_failed"
        else:
            parsed = _parse_runtime_inspect(completed.stdout)
            if parsed is None:
                availability = "unavailable"
                unavailable_code = "invalid_output"
            else:
                containers = parsed

    record: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "cre_runtime_performance",
        "run_id": run_id,
        "observed_at": utc_now(),
        "hardware": _hardware_configuration(),
        "containers": containers,
        "availability": availability,
        "unavailable_code": unavailable_code,
    }
    if unavailable_code is not None:
        warn_once()
    try:
        _atomic_write_runtime(run_dir / "runtime-performance.json", record)
    except (OSError, OverflowError, TypeError, UnicodeError, ValueError):
        warn_once()
        return {
            **record,
            "availability": "unavailable",
            "unavailable_code": "write_failed",
        }
    return record


def _base_record(
    *,
    run_id: str,
    command_id: str,
    command_log: str,
    phase: str,
    source: str | None,
    event: str,
    observed_at: str,
    elapsed_seconds: float | None,
    outcome: str,
    returncode: int | None,
    metrics_file: str | None,
    configured: Mapping[str, int],
) -> dict[str, object]:
    if phase not in _PHASES:
        phase = "unknown"
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": PERFORMANCE_KIND,
        "run_id": run_id,
        "command_id": command_id,
        "command_log": command_log,
        "phase": phase,
        "source": source,
        "event": event,
        "observed_at": observed_at,
        "elapsed_seconds": elapsed_seconds,
        "outcome": outcome,
        "returncode": returncode,
        "metrics_file": metrics_file,
        **configured,
    }


def run_observed_command(
    argv: Sequence[str],
    log_path: Path,
    *,
    env: Mapping[str, str] | None,
    runner: CommandRunner,
) -> int:
    """Run a command unchanged while recording bounded performance evidence."""
    warning_emitted = False

    def warn_once() -> None:
        nonlocal warning_emitted
        if warning_emitted:
            return
        warning_emitted = True
        try:
            print(PERFORMANCE_WARNING, file=sys.stderr)
        except (OSError, TypeError, ValueError):
            return

    try:
        command_id = uuid.uuid4().hex
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        command_id = ""
        warn_once()
    if _COMMAND_ID_PATTERN.fullmatch(command_id) is None:
        command_id = ""
        warn_once()
    journal_path = log_path.with_suffix(".performance.jsonl")
    collect_command = _is_collect_command(argv)
    metrics_path = (
        log_path.with_name(f"{log_path.stem}.{command_id}.scrape-performance.json")
        if collect_command and command_id
        else None
    )

    context_valid = bool(command_id)
    try:
        run_id = _validate_run_id(log_path.parent.parent.name)
        command_log = _safe_basename(log_path.name)
        metrics_file = (
            _safe_basename(metrics_path.name) if metrics_path is not None else None
        )
    except (TypeError, ValueError):
        context_valid = False
        run_id = ""
        command_log = ""
        metrics_file = None
        warn_once()

    phase = _phase(argv, command_log) if context_valid else "unknown"
    source = _known_source(argv, command_log) if context_valid else None
    configured = _configured_numbers(argv) if collect_command else {}
    try:
        raw_started_monotonic = time.monotonic()
    except (OSError, OverflowError, RuntimeError, ValueError):
        started_monotonic = None
        warn_once()
    else:
        started_monotonic = (
            raw_started_monotonic
            if isinstance(raw_started_monotonic, (int, float))
            and not isinstance(raw_started_monotonic, bool)
            and math.isfinite(raw_started_monotonic)
            else None
        )
        if started_monotonic is None:
            warn_once()

    def emit(
        *,
        event: str,
        elapsed: float | None,
        outcome: str,
        returncode: int | None,
    ) -> None:
        if not context_valid:
            return
        try:
            record = _base_record(
                run_id=run_id,
                command_id=command_id,
                command_log=command_log,
                phase=phase,
                source=source,
                event=event,
                observed_at=utc_now(),
                elapsed_seconds=elapsed,
                outcome=outcome,
                returncode=returncode,
                metrics_file=metrics_file,
                configured=configured,
            )
            _append_jsonl_record(journal_path, record)
        except (OSError, OverflowError, TypeError, ValueError):
            warn_once()

    emit(
        event="started",
        elapsed=0.0 if started_monotonic is not None else None,
        outcome="running",
        returncode=None,
    )

    runner_env: Mapping[str, str] | None = env
    if collect_command and context_valid and metrics_path is not None:
        runner_env = dict(os.environ if env is None else env)
        runner_env["CRE_PERFORMANCE_PATH"] = os.path.abspath(metrics_path)
        runner_env["CRE_PERFORMANCE_COMMAND_ID"] = command_id

    try:
        returncode = runner(argv, log_path, env=runner_env)
    except BaseException as exc:
        try:
            raw_finished_monotonic = time.monotonic()
        except (OSError, OverflowError, RuntimeError, ValueError):
            finished_monotonic = None
            warn_once()
        else:
            finished_monotonic = (
                raw_finished_monotonic
                if isinstance(raw_finished_monotonic, (int, float))
                and not isinstance(raw_finished_monotonic, bool)
                and math.isfinite(raw_finished_monotonic)
                else None
            )
        elapsed = _elapsed_seconds(started_monotonic, finished_monotonic)
        if elapsed is None:
            warn_once()
        emit(
            event="interrupted",
            elapsed=elapsed,
            outcome="interrupted"
            if isinstance(exc, KeyboardInterrupt)
            else "exception",
            returncode=None,
        )
        raise

    try:
        raw_finished_monotonic = time.monotonic()
    except (OSError, OverflowError, RuntimeError, ValueError):
        finished_monotonic = None
        warn_once()
    else:
        finished_monotonic = (
            raw_finished_monotonic
            if isinstance(raw_finished_monotonic, (int, float))
            and not isinstance(raw_finished_monotonic, bool)
            and math.isfinite(raw_finished_monotonic)
            else None
        )
    elapsed = _elapsed_seconds(started_monotonic, finished_monotonic)
    if elapsed is None:
        warn_once()
    recorded_returncode = (
        returncode
        if type(returncode) is int and -(2**31) <= returncode < 2**31
        else None
    )
    emit(
        event="finished",
        elapsed=elapsed,
        outcome="success" if returncode == 0 else "failed",
        returncode=recorded_returncode,
    )
    return returncode
