"""Optional, bounded, body-free resource observations for foreground CRE work.

This is diagnostic evidence only. It owns no collector process, CPU guard,
database connection, Docker lifecycle, or scheduling authority.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from cre_performance import resolve_runtime_target, utc_now
from cre_runtime_observability import sanitize_context

SCHEMA_VERSION = 1
KIND = "cre_resource_usage"
MAX_RECORD_BYTES = 16 * 1024
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
MAX_COMMAND_BYTES = 64 * 1024
SAMPLE_BUDGET_SECONDS = 4.0
WARNING = "warning: CRE resource usage diagnostics degraded; workflow continues"
_VM_FIELDS = {
    "Pages free": "free_bytes",
    "Pages active": "active_bytes",
    "Pages inactive": "inactive_bytes",
    "Pages wired down": "wired_bytes",
    "Pages speculative": "speculative_bytes",
    "Pages purgeable": "purgeable_bytes",
    "Pages occupied by compressor": "compressor_bytes",
}
_VM_COUNTERS = {
    "Pageins": "pageins",
    "Pageouts": "pageouts",
    "Swapins": "swapins",
    "Swapouts": "swapouts",
    "Compressions": "compressions",
    "Decompressions": "decompressions",
}


def _number(value: object, maximum: float = 2**63 - 1) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= maximum else None


def parse_vm_stat(output: str) -> dict[str, int | None]:
    """Keep raw VM page categories; do not invent a used-memory percentage."""
    result: dict[str, int | None] = {
        "page_size_bytes": None,
        **dict.fromkeys(_VM_FIELDS.values()),
        **dict.fromkeys(_VM_COUNTERS.values()),
    }
    header = re.search(r"page size of (\d+) bytes", output)
    if header is None:
        return result
    page_size = int(header[1])
    if not 1024 <= page_size <= 1024 * 1024:
        return result
    result["page_size_bytes"] = page_size
    for name, value in re.findall(r"^([^:\n]+):\s+(\d+)\.\s*$", output, re.MULTILINE):
        number = int(value)
        if name in _VM_FIELDS and number * page_size < 2**63:
            result[_VM_FIELDS[name]] = number * page_size
        elif name in _VM_COUNTERS and number < 2**63:
            result[_VM_COUNTERS[name]] = number
    return result


def _bytes(value: str) -> int | None:
    matched = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*(B|kB|KB|KiB|M|MB|MiB|G|GB|GiB|T|TB|TiB)\s*", value
    )
    if matched is None:
        return None
    token = matched[2]
    power = {
        "B": 0,
        "kB": 1,
        "KB": 1,
        "KiB": 1,
        "M": 2,
        "MB": 2,
        "MiB": 2,
        "G": 3,
        "GB": 3,
        "GiB": 3,
        "T": 4,
        "TB": 4,
        "TiB": 4,
    }[token]
    base = 1024 if token in {"M", "G", "T"} or "i" in token else 1000
    number = float(matched[1]) * base**power
    return int(number) if _number(number) is not None else None


def parse_swap(output: str) -> dict[str, int | None]:
    result: dict[str, int | None] = dict.fromkeys(
        ("total_bytes", "used_bytes", "free_bytes")
    )
    for name in ("total", "used", "free"):
        matched = re.search(rf"\b{name}\s*=\s*(\d+(?:\.\d+)?[MGTB])\b", output)
        if matched:
            result[f"{name}_bytes"] = _bytes(matched[1])
    return result


def parse_owned_processes(output: str, root_pid: int) -> dict[str, object]:
    """Aggregate numeric ps evidence for this coordinator and descendants only."""
    rows: dict[int, tuple[int, float, int]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 4:
            return {"count": None, "rss_bytes": None, "cpu_percent_lifetime": None}
        try:
            pid, parent, cpu, rss = (
                int(parts[0]),
                int(parts[1]),
                float(parts[2]),
                int(parts[3]),
            )
        except ValueError:
            return {"count": None, "rss_bytes": None, "cpu_percent_lifetime": None}
        if (
            pid <= 0
            or parent < 0
            or _number(cpu, 409600) is None
            or not 0 <= rss < 2**53
            or pid in rows
        ):
            return {"count": None, "rss_bytes": None, "cpu_percent_lifetime": None}
        rows[pid] = (parent, cpu, rss)
    owned = {root_pid} if root_pid in rows else set()
    for _ in range(len(rows)):
        expanded = owned | {
            pid for pid, (parent, _, _) in rows.items() if parent in owned
        }
        if expanded == owned:
            break
        owned = expanded
    return {
        "count": len(owned) if owned else None,
        "rss_bytes": sum(rows[pid][2] * 1024 for pid in owned) if owned else None,
        "cpu_percent_lifetime": round(sum(rows[pid][1] for pid in owned), 3)
        if owned
        else None,
    }


def parse_docker_usage(
    output: str, containers: Sequence[Mapping[str, Any]]
) -> list[dict[str, object]]:
    """Project docker stats into numeric, exact-ID-bound records."""
    values = [json.loads(line) for line in output.splitlines()]
    if len(values) != len(containers):
        raise ValueError("invalid stats shape")
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            raise TypeError("invalid stats shape")
        short_id = value.get("ID")
        if (
            not isinstance(short_id, str)
            or re.fullmatch(r"[0-9a-f]{12,64}", short_id) is None
        ):
            raise ValueError("invalid stats identity")
        matches = [item for item in containers if str(item["id"]).startswith(short_id)]
        if len(matches) != 1 or short_id in seen:
            raise ValueError("invalid stats identity")
        seen.add(short_id)
        item = matches[0]
        cpu_raw = value.get("CPUPerc")
        cpu = None
        if isinstance(cpu_raw, str) and re.fullmatch(r"\d+(?:\.\d+)?%", cpu_raw):
            cpu = _number(float(cpu_raw[:-1]), 409600)
        row: dict[str, object] = {
            "role": "browser" if item["service"] == "playwright-service" else "api",
            "id": item["id"],
            "name": item["name"],
            "image": item["image"],
            "running": item["running"],
            "oom_killed": item.get("oom_killed"),
            "restart_count": item.get("restart_count"),
            "cpu_quota_cores": item["cpu_limit"] or None,
            "cpu_percent": cpu,
            "cpu_cores": cpu / 100 if cpu is not None else None,
            "pids": int(value["PIDs"])
            if isinstance(value.get("PIDs"), str)
            and value["PIDs"].isdigit()
            and len(value["PIDs"]) <= 8
            else None,
        }
        for field, first, second in (
            ("MemUsage", "memory_current_bytes", "memory_limit_bytes"),
            ("NetIO", "network_rx_bytes", "network_tx_bytes"),
            ("BlockIO", "block_read_bytes", "block_write_bytes"),
        ):
            raw = value.get(field)
            parts = raw.split("/") if isinstance(raw, str) else []
            row[first] = _bytes(parts[0]) if len(parts) == 2 else None
            row[second] = _bytes(parts[1]) if len(parts) == 2 else None
        result.append(row)
    if len({row["id"] for row in result}) != len(containers):
        raise ValueError("duplicate stats identity")
    return sorted(result, key=lambda row: str(row["role"]))


def _append_record(path: Path, record: Mapping[str, object]) -> None:
    encoded = (
        json.dumps(record, allow_nan=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()
    if len(encoded) > MAX_RECORD_BYTES:
        raise ValueError("resource record cap")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_APPEND
        | os.O_NONBLOCK
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
        ):
            raise OSError("unsafe resource journal")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        metadata = os.fstat(fd)
        if metadata.st_size > MAX_JOURNAL_BYTES - len(encoded):
            raise OSError("resource journal cap")
        os.fchmod(fd, 0o600)
        if os.write(fd, encoded) != len(encoded):
            raise OSError("short resource write")
    finally:
        os.close(fd)


class ResourceUsageSampler:
    """One independent optional observer; every instance owns one session."""

    def __init__(
        self,
        run_dir: Path,
        *,
        env: Mapping[str, str] | None = None,
        sample_seconds: float = 5.0,
        cpu_sampler: Callable[[], float] | None = None,
        context_provider: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        if (
            not isinstance(sample_seconds, (float, int))
            or isinstance(sample_seconds, bool)
            or not math.isfinite(sample_seconds)
            or not 1 <= sample_seconds <= 60
        ):
            raise ValueError("sample interval must be between 1 and 60 seconds")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}", run_dir.name) is None:
            raise ValueError("invalid resource run id")
        self.run_dir = run_dir
        self.env = dict(os.environ if env is None else env)
        self.sample_seconds = float(sample_seconds)
        self.cpu_sampler = cpu_sampler
        self.context_provider = context_provider
        self.session_id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._warned = False

    def _warn(self) -> None:
        if not self._warned:
            self._warned = True
            with contextlib.suppress(Exception):
                print(WARNING, file=sys.stderr)

    def _command(self, argv: list[str], deadline: float) -> str:
        remaining = min(2.5, deadline - time.monotonic())
        if remaining <= 0 or self.stop_event.is_set():
            raise TimeoutError("sample deadline")
        result = subprocess.run(
            argv,
            env=self.env,
            capture_output=True,
            check=False,
            text=True,
            timeout=remaining,
        )
        if result.returncode != 0 or len(result.stdout.encode()) > MAX_COMMAND_BYTES:
            raise ValueError("diagnostic command unavailable")
        return result.stdout

    def sample_once(self) -> dict[str, object] | None:
        """Take one bounded sample; absent measurements stay null."""
        started = time.monotonic()
        observed_at = utc_now()
        deadline = started + SAMPLE_BUDGET_SECONDS
        gaps: list[str] = []

        def read(label: str, argv: list[str]) -> str:
            try:
                return self._command(argv, deadline)
            except (
                OSError,
                subprocess.SubprocessError,
                TimeoutError,
                UnicodeError,
                ValueError,
            ):
                gaps.append(label)
                return ""

        cpu = None
        if self.cpu_sampler is not None:
            try:
                cpu = _number(self.cpu_sampler(), 100)
            except Exception:  # noqa: BLE001 - optional observer must not affect guard
                gaps.append("host_cpu")
        if cpu is None:
            gaps.append("host_cpu")
        vm = parse_vm_stat(read("vm_stat", ["/usr/bin/vm_stat"]))
        memory_text = read(
            "host_memory", ["/usr/sbin/sysctl", "-n", "hw.memsize"]
        ).strip()
        total_memory = (
            int(memory_text)
            if memory_text.isdigit()
            and len(memory_text) <= 19
            and int(memory_text) < 2**63
            else None
        )
        swap = parse_swap(read("swap", ["/usr/sbin/sysctl", "-n", "vm.swapusage"]))
        pressure_text = read(
            "memory_pressure",
            ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
        ).strip()
        pressure = {"1": "normal", "2": "warning", "4": "critical"}.get(pressure_text)
        try:
            load = [_number(value, 1_000_000) for value in os.getloadavg()]
        except OSError:
            load = [None, None, None]
            gaps.append("load")
        try:
            disk = shutil.disk_usage(self.run_dir)
            disk_values = {
                "total_bytes": disk.total,
                "used_bytes": disk.used,
                "free_bytes": disk.free,
            }
        except OSError:
            disk_values = dict.fromkeys(("total_bytes", "used_bytes", "free_bytes"))
            gaps.append("disk")
        processes = parse_owned_processes(
            read("processes", ["/bin/ps", "-Ao", "pid=,ppid=,%cpu=,rss="]), os.getpid()
        )
        try:
            target = resolve_runtime_target(self.env, deadline=deadline)
        except Exception:  # noqa: BLE001 - diagnostics never change caller outcome
            target = {
                "availability": "unavailable",
                "unavailable_code": "diagnostic_failed",
                "api_endpoint": None,
                "project": None,
                "containers": [],
            }
        # Retain selected identities/running/OOM evidence even when usage is unavailable.
        containers: list[dict[str, object]] = (
            parse_docker_usage(
                "\n".join(
                    json.dumps({"ID": row["id"]}) for row in target["containers"]
                ),
                target["containers"],
            )
            if target["containers"]
            else []
        )
        docker_memory = None
        if target["availability"] == "available":
            try:
                containers = parse_docker_usage(
                    read(
                        "docker_stats",
                        [
                            "docker",
                            "stats",
                            "--no-stream",
                            "--format",
                            "{{json .}}",
                            *[str(row["id"]) for row in target["containers"]],
                        ],
                    ),
                    target["containers"],
                )
                if any(
                    value is None
                    for row in containers
                    for key, value in row.items()
                    if key != "cpu_quota_cores"
                ):
                    gaps.append("docker_usage_fields")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                gaps.append("docker_stats")
            raw_memory = read(
                "docker_vm_memory", ["docker", "info", "--format", "{{.MemTotal}}"]
            ).strip()
            if (
                raw_memory.isdigit()
                and len(raw_memory) <= 19
                and int(raw_memory) < 2**63
            ):
                docker_memory = int(raw_memory)
        else:
            gaps.append("runtime_binding")
        try:
            context = sanitize_context(
                self.context_provider() if self.context_provider else None
            )
        except Exception:  # noqa: BLE001 - caller context is optional evidence
            context = {}
            gaps.append("context")
        if (
            total_memory is None
            or pressure is None
            or any(value is None for value in swap.values())
            or any(value is None for value in vm.values())
        ):
            gaps.append("host_memory_fields")
        if processes["count"] is None:
            gaps.append("process_fields")
        record: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "run_id": self.run_dir.name,
            "session_id": self.session_id,
            "observed_at": observed_at,
            "elapsed_seconds": round(max(0.0, started - self.started), 6),
            "sample_duration_seconds": round(max(0.0, time.monotonic() - started), 6),
            "sample_seconds": self.sample_seconds,
            "context": context,
            "host": {
                "cpu_percent": cpu,
                "logical_cpu_count": os.cpu_count(),
                "memory_total_bytes": total_memory,
                "vm": vm,
                "swap": swap,
                "memory_pressure": pressure,
                "load_1m": load[0],
                "load_5m": load[1],
                "load_15m": load[2],
                "disk": disk_values,
            },
            "processes": {
                **processes,
                "coverage": "coordinator_and_descendants_rss_sum_shared_pages_may_overlap",
                "cpu_basis": "ps_lifetime_average_percent",
            },
            "runtime": {
                "availability": target["availability"],
                "unavailable_code": target["unavailable_code"],
                "api_endpoint": target["api_endpoint"],
                "project": target["project"],
                "docker_vm_memory_total_bytes": docker_memory,
                "containers": containers,
            },
            "unavailable_fields": sorted(set(gaps)),
        }
        if gaps:
            self._warn()
        with self._write_lock:
            if self.stop_event.is_set():
                return None
            try:
                _append_record(self.run_dir / "resource-usage.jsonl", record)
            except (OSError, TypeError, UnicodeError, ValueError):
                self._warn()
        return record

    def _run(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.sample_once()
            except Exception:  # noqa: BLE001 - telemetry cannot affect workflow
                self._warn()
            elapsed = time.monotonic() - started
            if self.stop_event.wait(max(0.1, self.sample_seconds - elapsed)):
                break

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(
                target=self._run, name="cre-resource-usage", daemon=True
            )
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=SAMPLE_BUDGET_SECONDS + 0.5)


@contextlib.contextmanager
def observe_resource_usage(
    run_dir: Path,
    *,
    env: Mapping[str, str] | None = None,
    sample_seconds: float = 5.0,
    cpu_sampler: Callable[[], float] | None = None,
    context_provider: Callable[[], Mapping[str, Any]] | None = None,
) -> Iterator[None]:
    """Preserve caller outcome even when construction/start/stop diagnostics fail."""
    sampler = None
    try:
        sampler = ResourceUsageSampler(
            run_dir,
            env=env,
            sample_seconds=sample_seconds,
            cpu_sampler=cpu_sampler,
            context_provider=context_provider,
        )
        sampler.start()
    except Exception:  # noqa: BLE001 - diagnostic construction/start is optional
        with contextlib.suppress(Exception):
            print(WARNING, file=sys.stderr)
    try:
        yield
    finally:
        if sampler is not None:
            try:
                sampler.stop()
            except Exception:  # noqa: BLE001 - preserve caller exception and cleanup
                with contextlib.suppress(Exception):
                    print(WARNING, file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Save a bounded read-only host/runtime resource sample; no collector or database calls."
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=float, default=10.0)
    parser.add_argument("--sample-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)
    if not math.isfinite(args.duration_seconds) or not 1 <= args.duration_seconds <= 60:
        parser.error("duration-seconds must be between 1 and 60")
    if not math.isfinite(args.sample_seconds) or not 1 <= args.sample_seconds <= 60:
        parser.error("sample-seconds must be between 1 and 60")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    cpu_sampler = None
    try:
        from cre_checkpoint_refresh import DarwinCpuSampler

        cpu_sampler = DarwinCpuSampler()
    except Exception:  # noqa: BLE001 - non-Darwin hosts retain partial diagnostics
        with contextlib.suppress(Exception):
            print(WARNING, file=sys.stderr)
    with observe_resource_usage(
        args.out_dir, sample_seconds=args.sample_seconds, cpu_sampler=cpu_sampler
    ):
        time.sleep(args.duration_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
