"""Required platform-selected memory pressure guard for foreground checkpoints.

The guard owns no child or database operation. Its injected trip callback uses
the checkpoint's existing signal/owned-process cleanup protocol. Free pages,
old swap usage and Docker's cache-adjusted memory display are not stop signals.
"""

from __future__ import annotations

import math
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cre_performance import utc_now
from cre_linux_telemetry import LinuxMemorySampler, LinuxTelemetryError, memory_policy
from cre_resource_usage import _append_record
from cre_runtime_observability import sanitize_context

DEFAULT_MEMORY_SUSTAIN_SECONDS = 20.0
DEFAULT_MEMORY_SAMPLE_SECONDS = 2.0
PRESSURE_NORMAL = 1
PRESSURE_WARNING = 2
PRESSURE_CRITICAL = 4


class MemoryTelemetryError(RuntimeError):
    """Required memory safety evidence is unavailable or stale."""


class MemoryEvidenceError(MemoryTelemetryError):
    """A required memory safety journal record could not be saved."""


def memory_guard_config(
    sustain_seconds: float = DEFAULT_MEMORY_SUSTAIN_SECONDS,
    sample_seconds: float = DEFAULT_MEMORY_SAMPLE_SECONDS,
) -> dict[str, Any]:
    for value in (sustain_seconds, sample_seconds):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("memory guard intervals must be finite numbers")
    if not 0 < sustain_seconds <= 30 or not 0.25 <= sample_seconds <= min(
        5, sustain_seconds
    ):
        raise ValueError(
            "memory sustain must be within (0,30]s; sample within [0.25,5]s and no greater than sustain"
        )
    config = {
        "warning_sustain_seconds": sustain_seconds,
        "sample_seconds": sample_seconds,
        "critical_action": "interrupt_immediately",
        "preflight": "require_normal_pressure",
        "action": "interrupt_and_checkpoint",
        "telemetry_failure_action": "interrupt_and_checkpoint",
        "basis": "darwin_dispatch_pressure_level_1_normal_2_warning_4_critical",
    }
    if sys.platform.startswith("linux"):
        config.update(memory_policy())
    return config


def read_darwin_memory_pressure(*, timeout: float = 1.0) -> int:
    """Read only the userspace dispatch level, never the kernel's internal enum."""
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        if (
            result.returncode != 0
            or result.stdout.strip() not in {"1", "2", "4"}
            or len(result.stdout) > 32
        ):
            raise MemoryTelemetryError("host memory pressure unavailable")
        return int(result.stdout.strip())
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise MemoryTelemetryError("host memory pressure unavailable") from exc


def read_memory_pressure(
    *, timeout: float = 1.0, linux_sampler: LinuxMemorySampler | None = None
) -> int:
    if sys.platform == "darwin":
        return read_darwin_memory_pressure(timeout=timeout)
    if sys.platform.startswith("linux"):
        try:
            return (linux_sampler or LinuxMemorySampler())()
        except LinuxTelemetryError as exc:
            raise MemoryTelemetryError(str(exc)) from exc
    raise MemoryTelemetryError("host memory telemetry unsupported on this platform")


@dataclass
class HostMemoryGuard:
    log_path: Path
    on_trip: Callable[..., None]
    sustain_seconds: float = DEFAULT_MEMORY_SUSTAIN_SECONDS
    sample_seconds: float = DEFAULT_MEMORY_SAMPLE_SECONDS
    sampler: Callable[[], int] | None = None
    context_provider: Callable[[], Mapping[str, Any]] | None = None
    high_since: float | None = field(default=None, init=False)
    previous_observed: float | None = field(default=None, init=False)
    tripped: bool = field(default=False, init=False)
    stop_event: threading.Event = field(default_factory=threading.Event, init=False)
    thread: threading.Thread | None = field(default=None, init=False)
    linux_sampler: LinuxMemorySampler = field(default_factory=LinuxMemorySampler, init=False)
    linux_sample_evidence: dict[str, object] | None = field(default=None, init=False)

    def config(self) -> dict[str, Any]:
        return memory_guard_config(self.sustain_seconds, self.sample_seconds)

    def observe(self, pressure: int, *, observed_monotonic: float) -> str | None:
        if type(pressure) is not int or pressure not in (
            PRESSURE_NORMAL,
            PRESSURE_WARNING,
            PRESSURE_CRITICAL,
        ):
            raise MemoryTelemetryError("host memory pressure invalid")
        if not math.isfinite(observed_monotonic) or (
            self.previous_observed is not None
            and (
                observed_monotonic <= self.previous_observed
                or observed_monotonic - self.previous_observed
                > self.sample_seconds * 1.5
            )
        ):
            raise MemoryTelemetryError("host memory pressure sample stale")
        self.previous_observed = observed_monotonic
        if pressure == PRESSURE_CRITICAL:
            return "host_memory_critical"
        if pressure == PRESSURE_NORMAL:
            self.high_since = None
            return None
        if self.high_since is None:
            self.high_since = observed_monotonic
        if observed_monotonic - self.high_since >= self.sustain_seconds:
            return "host_memory_warning_sustained"
        return None

    def _write(self, *, pressure: int | None, reason_code: str | None) -> None:
        context = sanitize_context(
            self.context_provider() if self.context_provider else None
        )
        diagnostics: dict[str, Any] = {}
        if (
            pressure in (PRESSURE_WARNING, PRESSURE_CRITICAL)
            and self.sampler is None
            and sys.platform.startswith("linux")
            and self.linux_sample_evidence is not None
            and self.linux_sample_evidence["pressure_level"] == pressure
        ):
            diagnostics["linux_memory_sample"] = self.linux_sample_evidence
        _append_record(
            self.log_path,
            {
                "schema_version": 1,
                "kind": "cre_host_memory_guard",
                "observed_at": utc_now(),
                "pressure_level": pressure,
                "state": "tripped"
                if reason_code
                else "warning"
                if self.high_since is not None
                else "ok",
                "reason_code": reason_code,
                # Policy lives in the manifest/preflight. Compact ticks leave
                # room for >24h at the default 2s cadence within the 16MiB cap.
                "context": {
                    key: value[:32]
                    for key, value in context.items()
                    if key in {"phase", "source"} and isinstance(value, str)
                },
                **diagnostics,
            },
        )

    def _trip(
        self,
        code: str,
        *,
        pressure: int | None,
        telemetry_valid: bool,
        evidence_valid: bool = True,
    ) -> None:
        # A stop request prevents the next tick, but must not discard a danger
        # decision already observed by an in-flight sample during quiescence.
        if self.tripped:
            return
        self.tripped = True
        try:
            self._write(pressure=pressure, reason_code=code)
        except Exception:  # noqa: BLE001 - stopping must survive required-log failure
            evidence_valid = False
        self.on_trip(
            reason_code=code,
            pressure=pressure,
            telemetry_valid=telemetry_valid,
            evidence_valid=evidence_valid,
        )

    def sample_once(self, *, admission: bool = False) -> int:
        self.linux_sample_evidence = None
        linux_sample = self.sampler is None and sys.platform.startswith("linux")
        if linux_sample:
            self.linux_sampler.last_sample = None
        started = time.monotonic()
        pressure = (
            self.sampler()
            if self.sampler
            else read_memory_pressure(
                timeout=min(1.0, self.sample_seconds), linux_sampler=self.linux_sampler
            )
        )
        observed = time.monotonic()
        if observed - started > min(1.0, self.sample_seconds):
            raise MemoryTelemetryError("host memory pressure read stale")
        code = self.observe(pressure, observed_monotonic=observed)
        if linux_sample:
            self.linux_sample_evidence = self.linux_sampler.last_sample
        if admission and pressure != PRESSURE_NORMAL:
            code = "host_memory_start_blocked"
        if code:
            self._trip(code, pressure=pressure, telemetry_valid=True)
        else:
            try:
                self._write(pressure=pressure, reason_code=None)
            except Exception as exc:
                raise MemoryEvidenceError(
                    "host memory guard evidence unavailable"
                ) from exc
        return pressure

    def _run(self) -> None:
        while not self.stop_event.wait(self.sample_seconds):
            try:
                self.sample_once()
            except Exception as exc:  # noqa: BLE001 - every required sampler failure stops work
                self._trip(
                    "host_memory_evidence_failed"
                    if isinstance(exc, MemoryEvidenceError)
                    else "host_memory_telemetry_failed",
                    pressure=None,
                    telemetry_valid=isinstance(exc, MemoryEvidenceError),
                )
            if self.tripped:
                return

    def preflight(self) -> int:
        self.config()
        try:
            return self.sample_once(admission=True)
        except Exception as exc:
            self._trip(
                "host_memory_evidence_failed"
                if isinstance(exc, MemoryEvidenceError)
                else "host_memory_telemetry_failed",
                pressure=None,
                telemetry_valid=isinstance(exc, MemoryEvidenceError),
            )
            raise

    def start(self) -> None:
        self.config()
        if self.tripped or self.thread is not None:
            raise RuntimeError("host memory guard cannot be restarted")
        self.thread = threading.Thread(
            target=self._run, name="cre-host-memory-guard", daemon=True
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=max(1.0, self.sample_seconds + 1.5))
