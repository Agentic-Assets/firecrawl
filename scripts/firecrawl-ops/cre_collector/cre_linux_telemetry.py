"""Bounded Linux telemetry shared by admission guards and no-write calibration.

These observations cover the Linux kernel/VM and visible cgroup ancestors,
never Windows host utilization. Missing required kernel/controller evidence
is an error, not a zero-pressure sample. cgroup v1 is deliberately unsupported.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

PSI_SOME_WARNING = 10.0
PSI_FULL_CRITICAL = 2.0
AVAILABLE_WARNING = 0.10
AVAILABLE_CRITICAL = 0.05
SWAP_FREE_WARNING = 0.10
SWAP_AVAILABLE_WARNING = 0.20
CGROUP_USAGE_WARNING = 0.90
CGROUP_USAGE_CRITICAL = 0.95


def memory_policy() -> dict[str, float | str]:
    """Persist the exact Linux policy in immutable checkpoint configuration."""
    return {
        "basis": "linux_psi_memavailable_swap_cgroup_v2_policy_v1",
        "telemetry_scope": "linux_kernel_or_vm_and_visible_cgroups_not_windows_host",
        "psi_some_avg10_warning_percent": PSI_SOME_WARNING,
        "psi_full_avg10_critical_percent": PSI_FULL_CRITICAL,
        "available_warning_percent": AVAILABLE_WARNING * 100,
        "available_critical_percent": AVAILABLE_CRITICAL * 100,
        "swap_free_warning_percent": SWAP_FREE_WARNING * 100,
        "swap_available_warning_percent": SWAP_AVAILABLE_WARNING * 100,
        "cgroup_usage_warning_percent": CGROUP_USAGE_WARNING * 100,
        "cgroup_usage_critical_percent": CGROUP_USAGE_CRITICAL * 100,
        "cgroup_event_policy": "new_high_max_warn_new_oom_oom_kill_critical",
    }


class LinuxTelemetryError(RuntimeError):
    """Required Linux resource evidence cannot be trusted."""


def _read(path: Path, maximum: int = 65536) -> str:
    try:
        with path.open("r", encoding="ascii") as handle:
            value = handle.read(maximum + 1)
        if len(value) > maximum:
            raise LinuxTelemetryError("Linux resource evidence exceeds size bound")
        return value
    except (OSError, UnicodeError) as exc:
        raise LinuxTelemetryError("Linux resource evidence unavailable") from exc


def _uint(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,24}", value):
        raise LinuxTelemetryError("Linux resource counter invalid")
    return int(value)


def read_cpu_ticks(path: Path = Path("/proc/stat")) -> tuple[int, ...]:
    # Read only the aggregate line: many-core hosts can have large proc files.
    try:
        with path.open("r", encoding="ascii") as handle:
            line = handle.readline(1025)
    except (OSError, UnicodeError) as exc:
        raise LinuxTelemetryError("Linux CPU telemetry unavailable") from exc
    fields = line.split()
    if len(line) > 1024 or not 9 <= len(fields) <= 11 or fields[0] != "cpu":
        raise LinuxTelemetryError("Linux CPU telemetry invalid")
    counters = tuple(_uint(value) for value in fields[1:])
    # guest and guest_nice already contribute to user/nice; do not double-count.
    return counters[:8]


def cpu_percent(previous: tuple[int, ...], current: tuple[int, ...]) -> float:
    if len(previous) != len(current) or not 4 <= len(current) <= 8:
        raise LinuxTelemetryError("Linux CPU telemetry shape changed")
    if any(type(value) is not int or value < 0 for value in (*previous, *current)):
        raise LinuxTelemetryError("Linux CPU tick counter invalid")
    if any(right < left for left, right in zip(previous, current, strict=True)):
        # Linux counters are not Mach's wrapping uint32 counters. A reset,
        # hotplug discontinuity or decreasing iowait must stop admission.
        raise LinuxTelemetryError("Linux CPU tick counter moved backwards")
    delta = [right - left for left, right in zip(previous, current, strict=True)]
    total = sum(delta)
    if total <= 0:
        raise LinuxTelemetryError("Linux CPU tick counters did not advance")
    idle = delta[3] + (delta[4] if len(delta) > 4 else 0)
    return 100.0 * (total - idle) / total


def _psi(text: str) -> tuple[float, float]:
    values = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 5 or fields[0] not in {"some", "full"} or fields[0] in values:
            raise LinuxTelemetryError("Linux memory PSI invalid")
        parts = dict(item.split("=", 1) for item in fields[1:] if "=" in item)
        if set(parts) != {"avg10", "avg60", "avg300", "total"}:
            raise LinuxTelemetryError("Linux memory PSI invalid")
        try:
            averages = [float(parts[key]) for key in ("avg10", "avg60", "avg300")]
        except ValueError as exc:
            raise LinuxTelemetryError("Linux memory PSI invalid") from exc
        if any(not math.isfinite(v) or not 0 <= v <= 100 for v in averages):
            raise LinuxTelemetryError("Linux memory PSI invalid")
        _uint(parts["total"])
        values[fields[0]] = averages[0]
    if set(values) != {"some", "full"} or values["full"] > values["some"]:
        raise LinuxTelemetryError("Linux memory PSI invalid")
    return values["some"], values["full"]


def _psi_level(text: str) -> int:
    some, full = _psi(text)
    return 4 if full >= PSI_FULL_CRITICAL else 2 if some >= PSI_SOME_WARNING else 1


def _key_counters(text: str, required: set[str]) -> dict[str, int]:
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] in required:
            if len(fields) != 2 or fields[0] in result:
                raise LinuxTelemetryError("Linux memory counters invalid")
            result[fields[0]] = _uint(fields[1])
    if set(result) != required:
        raise LinuxTelemetryError("Linux memory counters missing")
    return result


def _unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)


def cgroup_directories(proc: Path, filesystem: Path = Path("/")) -> list[Path]:
    """Resolve membership against the actual mount root, including namespaces."""
    lines = _read(proc / "self/cgroup").splitlines()
    if len(lines) != 1 or not lines[0].startswith("0::/"):
        raise LinuxTelemetryError("Linux safety requires unified cgroup v2")
    membership = Path(lines[0][3:])
    if ".." in membership.parts:
        raise LinuxTelemetryError("Linux cgroup membership invalid")
    mounts = []
    for line in _read(proc / "self/mountinfo", 262144).splitlines():
        before, separator, after = line.partition(" - ")
        after_fields = after.split()
        if separator and after_fields and after_fields[0] == "cgroup2":
            fields = before.split()
            if len(fields) < 6:
                raise LinuxTelemetryError("Linux cgroup mount invalid")
            mounts.append(
                (Path(_unescape_mount(fields[3])), Path(_unescape_mount(fields[4])))
            )
    if len(mounts) != 1:
        raise LinuxTelemetryError("Linux cgroup mount unavailable or ambiguous")
    root, mount = mounts[0]
    if (
        not root.is_absolute()
        or not mount.is_absolute()
        or ".." in (*root.parts, *mount.parts)
    ):
        raise LinuxTelemetryError("Linux cgroup mount invalid")
    try:
        relative = membership.relative_to(root)
    except ValueError:
        # A cgroup namespace reports '/' relative to its own mounted subtree.
        if membership != Path("/"):
            raise LinuxTelemetryError("Linux cgroup membership outside mount") from None
        relative = Path(".")
    base = filesystem / str(mount).lstrip("/")
    leaf = base / relative
    directories = [leaf]
    while directories[-1] != base:
        directories.append(directories[-1].parent)
        if len(directories) > 64:
            raise LinuxTelemetryError("Linux cgroup ancestry exceeds bound")
    return directories


@dataclass
class LinuxMemorySampler:
    proc: Path = Path("/proc")
    filesystem: Path = Path("/")
    previous_events: dict[str, dict[str, int]] = field(default_factory=dict, init=False)
    previous_membership: tuple[str, ...] | None = field(default=None, init=False)
    last_sample: dict[str, object] | None = field(default=None, init=False)

    def __call__(self) -> int:
        # Publish only a complete, validated sample. Failures cannot reuse it.
        self.last_sample = None
        required = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
        rows = []
        for line in _read(self.proc / "meminfo").splitlines():
            name, _, remainder = line.partition(":")
            if name in required:
                fields = remainder.split()
                if len(fields) != 2 or fields[1] != "kB":
                    raise LinuxTelemetryError("Linux memory units invalid")
                rows.append(f"{name} {fields[0]}")
        info = _key_counters("\n".join(rows), required)
        total, available = info["MemTotal"], info["MemAvailable"]
        swap_total, swap_free = info["SwapTotal"], info["SwapFree"]
        if not total or available > total or swap_free > swap_total:
            raise LinuxTelemetryError("Linux memory totals invalid")
        host_psi_text = _read(self.proc / "pressure/memory")
        level = _psi_level(host_psi_text)
        host_some, host_full = _psi(host_psi_text)
        causes = []
        if host_some >= PSI_SOME_WARNING:
            causes.append("host_psi_some")
        if host_full >= PSI_FULL_CRITICAL:
            causes.append("host_psi_full")
        ratio = available / total
        level = max(
            level,
            4
            if ratio <= AVAILABLE_CRITICAL
            else 2
            if ratio <= AVAILABLE_WARNING
            else 1,
        )
        if ratio <= AVAILABLE_CRITICAL:
            causes.append("host_available_critical")
        elif ratio <= AVAILABLE_WARNING:
            causes.append("host_available_warning")
        # Old swap use alone says nothing about current pressure. Exhausted
        # swap becomes a warning only together with limited available memory.
        if (
            swap_total
            and swap_free / swap_total <= SWAP_FREE_WARNING
            and ratio <= SWAP_AVAILABLE_WARNING
        ):
            level = max(level, 2)
            causes.append("host_swap_low")
        events_now = {}
        cgroups = []
        directories = cgroup_directories(self.proc, self.filesystem)
        membership = tuple(str(directory) for directory in directories)
        if (
            self.previous_membership is not None
            and self.previous_membership != membership
        ):
            raise LinuxTelemetryError("Linux cgroup membership changed")
        for ancestor_index, directory in enumerate(directories):
            # The real hierarchy root has no memory.current/max/events. Any
            # non-root cgroup must expose all required memory-controller files.
            if not (directory / "memory.current").exists():
                if (
                    directory == directories[-1]
                    and (directory / "cgroup.controllers").exists()
                    and not (directory / "cgroup.type").exists()
                ):
                    if "memory" not in _read(directory / "cgroup.controllers").split():
                        raise LinuxTelemetryError(
                            "Linux cgroup memory controller unavailable"
                        )
                    continue
                raise LinuxTelemetryError("Linux cgroup memory controller unavailable")
            current = _uint(_read(directory / "memory.current").strip())
            maximum = _read(directory / "memory.max").strip()
            limit = None
            group_causes = []
            if maximum != "max":
                limit = _uint(maximum)
                if limit <= 0:
                    raise LinuxTelemetryError("Linux cgroup memory limit invalid")
                usage = current / limit
                level = max(
                    level,
                    4
                    if usage >= CGROUP_USAGE_CRITICAL
                    else 2
                    if usage >= CGROUP_USAGE_WARNING
                    else 1,
                )
                if usage >= CGROUP_USAGE_CRITICAL:
                    group_causes.append("cgroup_usage_critical")
                elif usage >= CGROUP_USAGE_WARNING:
                    group_causes.append("cgroup_usage_warning")
            group_psi_text = _read(directory / "memory.pressure")
            level = max(level, _psi_level(group_psi_text))
            group_some, group_full = _psi(group_psi_text)
            if group_some >= PSI_SOME_WARNING:
                group_causes.append("cgroup_psi_some")
            if group_full >= PSI_FULL_CRITICAL:
                group_causes.append("cgroup_psi_full")
            events = _key_counters(
                _read(directory / "memory.events"), {"high", "max", "oom", "oom_kill"}
            )
            key = str(directory)
            previous = self.previous_events.get(key)
            if previous is not None:
                if any(events[name] < previous[name] for name in events):
                    raise LinuxTelemetryError(
                        "Linux memory event counter moved backwards"
                    )
                if any(events[name] > previous[name] for name in ("oom", "oom_kill")):
                    level = 4
                elif any(events[name] > previous[name] for name in ("high", "max")):
                    level = max(level, 2)
                group_causes.extend(
                    f"cgroup_event_{name}"
                    for name in sorted(events)
                    if events[name] > previous[name]
                )
            events_now[key] = events
            cgroups.append(
                {
                    "ancestor_index": ancestor_index,
                    "current_bytes": current,
                    "limit_bytes": limit,
                    "psi_some_avg10_percent": group_some,
                    "psi_full_avg10_percent": group_full,
                    "events": dict(events),
                    "event_deltas": {
                        name: events[name] - previous[name] for name in sorted(events)
                    }
                    if previous is not None
                    else None,
                    "causes": group_causes,
                }
            )
            causes.extend(group_causes)
        self.previous_membership = membership
        self.previous_events = events_now
        self.last_sample = {
            "schema_version": 1,
            "pressure_level": level,
            "causes": sorted(set(causes)),
            "host": {
                "memory_total_bytes": total * 1024,
                "memory_available_bytes": available * 1024,
                "swap_total_bytes": swap_total * 1024,
                "swap_free_bytes": swap_free * 1024,
                "psi_some_avg10_percent": host_some,
                "psi_full_avg10_percent": host_full,
            },
            # Leaf first; at most the existing 64-ancestor bound. No path names.
            "cgroups": cgroups,
        }
        return level
