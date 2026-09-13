"""Pure, bounded host-CPU cooldown primitives for checkpoint series recovery.

This module does not launch collectors, access a database, or own scheduling.
The foreground checkpoint-series process supplies the sampler, clock, sleeper,
and atomic progress callback so the wait remains deterministic in tests and
visible to the operator in production.
"""

from __future__ import annotations

import fcntl
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self, TextIO

SCHEMA_VERSION = 1
DEFAULT_MAX_RECOVERIES_PER_SOURCE = 0
MAX_RECOVERIES_PER_SOURCE = 3
DEFAULT_LOW_CPU_PERCENT = 60.0
DEFAULT_LOW_CPU_SECONDS = 30.0
DEFAULT_SAMPLE_SECONDS = 2.0
DEFAULT_MAX_COOLDOWN_SECONDS = 600.0
MAX_COOLDOWN_SECONDS = 600.0
DEFAULT_MAX_SERIES_COOLDOWN_SECONDS = 1800.0
MAX_SERIES_COOLDOWN_SECONDS = 1800.0
MAX_SAMPLE_GAP_MULTIPLIER = 1.5


def _is_finite(value: float) -> bool:
    try:
        return math.isfinite(value)
    except (TypeError, OverflowError):
        return False


class RecoveryError(RuntimeError):
    """A bounded resource recovery cannot proceed safely."""


class RecoveryBudgetExhausted(RecoveryError):
    """The per-wait or per-series cooldown budget is exhausted."""


class RecoveryTelemetryError(RecoveryError):
    """A required host-CPU sample is missing or invalid."""


class RecoveryEvidenceError(RecoveryError):
    """Required atomic progress evidence could not be persisted."""


class RecoveryCancelled(RecoveryError):
    """The foreground operator cancelled the cooldown."""


class RecoveryOwnershipError(RecoveryError):
    """Another foreground process owns this checkpoint series."""


@dataclass(frozen=True)
class RecoveryConfig:
    """Persisted opt-in cooldown configuration with fixed safety ceilings."""

    max_recoveries_per_source: int = DEFAULT_MAX_RECOVERIES_PER_SOURCE
    low_cpu_percent: float = DEFAULT_LOW_CPU_PERCENT
    low_cpu_seconds: float = DEFAULT_LOW_CPU_SECONDS
    sample_seconds: float = DEFAULT_SAMPLE_SECONDS
    max_cooldown_seconds: float = DEFAULT_MAX_COOLDOWN_SECONDS
    max_series_cooldown_seconds: float = DEFAULT_MAX_SERIES_COOLDOWN_SECONDS

    def validate(self) -> None:
        if (
            isinstance(self.max_recoveries_per_source, bool)
            or not isinstance(self.max_recoveries_per_source, int)
            or not 0 <= self.max_recoveries_per_source <= MAX_RECOVERIES_PER_SOURCE
        ):
            raise ValueError(
                "max recoveries per source must be between 0 and "
                f"{MAX_RECOVERIES_PER_SOURCE}"
            )
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in (
                self.low_cpu_percent,
                self.low_cpu_seconds,
                self.sample_seconds,
                self.max_cooldown_seconds,
                self.max_series_cooldown_seconds,
            )
        ):
            raise ValueError("resource recovery numeric configuration is malformed")
        if not _is_finite(self.low_cpu_percent) or not 0 < self.low_cpu_percent < 100:
            raise ValueError(
                "recovery low CPU percent must be finite and between 0 and 100"
            )
        if not _is_finite(self.low_cpu_seconds) or self.low_cpu_seconds <= 0:
            raise ValueError("recovery low CPU seconds must be finite and positive")
        if (
            not _is_finite(self.sample_seconds)
            or self.sample_seconds <= 0
            or self.sample_seconds > self.low_cpu_seconds
        ):
            raise ValueError(
                "recovery sample seconds must be finite, positive, and no greater "
                "than recovery low CPU seconds"
            )
        if (
            not _is_finite(self.max_cooldown_seconds)
            or self.max_cooldown_seconds <= 0
            or self.max_cooldown_seconds > MAX_COOLDOWN_SECONDS
        ):
            raise ValueError(
                "maximum recovery cooldown must be finite, positive, and no greater "
                f"than {MAX_COOLDOWN_SECONDS:g} seconds"
            )
        if (
            not _is_finite(self.max_series_cooldown_seconds)
            or self.max_series_cooldown_seconds <= 0
            or self.max_series_cooldown_seconds > MAX_SERIES_COOLDOWN_SECONDS
        ):
            raise ValueError(
                "maximum series recovery time must be finite, positive, and no greater "
                f"than {MAX_SERIES_COOLDOWN_SECONDS:g} seconds"
            )

    def as_dict(self) -> dict[str, int | float]:
        self.validate()
        return {
            "max_recoveries_per_source": self.max_recoveries_per_source,
            "low_cpu_percent": self.low_cpu_percent,
            "low_cpu_seconds": self.low_cpu_seconds,
            "sample_seconds": self.sample_seconds,
            "max_cooldown_seconds": self.max_cooldown_seconds,
            "max_series_cooldown_seconds": self.max_series_cooldown_seconds,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RecoveryConfig:
        try:
            recoveries = value["max_recoveries_per_source"]
            if isinstance(recoveries, bool) or not isinstance(recoveries, int):
                raise TypeError
            numeric = {
                key: value[key]
                for key in (
                    "low_cpu_percent",
                    "low_cpu_seconds",
                    "sample_seconds",
                    "max_cooldown_seconds",
                    "max_series_cooldown_seconds",
                )
            }
            if any(
                isinstance(item, bool) or not isinstance(item, (int, float))
                for item in numeric.values()
            ):
                raise TypeError
            config = cls(
                max_recoveries_per_source=recoveries,
                low_cpu_percent=float(numeric["low_cpu_percent"]),
                low_cpu_seconds=float(numeric["low_cpu_seconds"]),
                sample_seconds=float(numeric["sample_seconds"]),
                max_cooldown_seconds=float(numeric["max_cooldown_seconds"]),
                max_series_cooldown_seconds=float(
                    numeric["max_series_cooldown_seconds"]
                ),
            )
        except (KeyError, OverflowError, TypeError, ValueError) as exc:
            raise ValueError("resource recovery configuration is malformed") from exc
        config.validate()
        return config


@dataclass(frozen=True)
class CooldownProgress:
    """One redaction-safe cooldown observation for atomic parent evidence."""

    waited_seconds: float
    current_host_cpu_percent: float
    low_cpu_seconds: float
    remaining_cooldown_seconds: float
    remaining_series_wait_seconds: float
    observed_at: str

    def as_dict(self) -> dict[str, float | str]:
        return {
            "waited_seconds": round(self.waited_seconds, 3),
            "current_host_cpu_percent": round(self.current_host_cpu_percent, 2),
            "low_cpu_seconds": round(self.low_cpu_seconds, 3),
            "remaining_cooldown_seconds": round(self.remaining_cooldown_seconds, 3),
            "remaining_series_wait_seconds": round(
                self.remaining_series_wait_seconds, 3
            ),
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class CooldownResult:
    """Successful hysteresis result returned immediately before child resume."""

    waited_seconds: float
    final_host_cpu_percent: float
    low_cpu_seconds: float
    observed_at: str


def _valid_cpu_sample(value: Any) -> float:
    if isinstance(value, bool):
        raise RecoveryTelemetryError("host CPU sample is invalid")
    try:
        percent = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise RecoveryTelemetryError("host CPU sample is invalid") from exc
    if not math.isfinite(percent) or not 0.0 <= percent <= 100.0:
        raise RecoveryTelemetryError("host CPU sample is invalid")
    return percent


def wait_for_cpu_recovery(
    config: RecoveryConfig,
    *,
    already_waited_seconds: float,
    series_waited_seconds: float,
    sampler: Callable[[], float],
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    utc_now: Callable[[], str],
    on_progress: Callable[[CooldownProgress], None],
    cancelled: Callable[[], bool] = lambda: False,
) -> CooldownResult:
    """Wait for strict-below-threshold CPU over one continuous low window.

    Elapsed time uses only the injected monotonic clock. Previously persisted
    wait is charged against both budgets, while the continuous low window
    always restarts after a process restart because unobserved time is not
    admissible evidence.
    """
    config.validate()
    for name, value in (
        ("already waited seconds", already_waited_seconds),
        ("series waited seconds", series_waited_seconds),
    ):
        if not _is_finite(value) or value < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
    available = min(
        config.max_cooldown_seconds - already_waited_seconds,
        config.max_series_cooldown_seconds - series_waited_seconds,
    )
    if available <= 0:
        raise RecoveryBudgetExhausted("resource recovery wait budget is exhausted")

    started = monotonic()
    if not _is_finite(started):
        raise RecoveryTelemetryError("monotonic clock is invalid")
    low_since: float | None = None
    previous_observed: float | None = None

    while True:
        if cancelled():
            raise RecoveryCancelled("resource recovery was cancelled")
        sample_started = monotonic()
        if not _is_finite(sample_started) or sample_started < started:
            raise RecoveryTelemetryError("monotonic clock is invalid")
        if sample_started - started > available:
            raise RecoveryBudgetExhausted("resource recovery wait budget is exhausted")
        if (
            previous_observed is not None
            and sample_started - previous_observed
            > config.sample_seconds * MAX_SAMPLE_GAP_MULTIPLIER
        ):
            raise RecoveryTelemetryError("host CPU sample gap is stale")
        try:
            percent = _valid_cpu_sample(sampler())
        except RecoveryTelemetryError:
            raise
        except Exception as exc:
            raise RecoveryTelemetryError("host CPU sample is unavailable") from exc
        observed_monotonic = monotonic()
        if not _is_finite(observed_monotonic) or observed_monotonic < sample_started:
            raise RecoveryTelemetryError("monotonic clock is invalid")
        elapsed = observed_monotonic - started
        if elapsed > available:
            raise RecoveryBudgetExhausted("resource recovery wait budget is exhausted")
        if observed_monotonic - sample_started > config.sample_seconds:
            raise RecoveryTelemetryError("host CPU sample is stale")
        if (
            previous_observed is not None
            and observed_monotonic - previous_observed
            > config.sample_seconds * MAX_SAMPLE_GAP_MULTIPLIER
        ):
            raise RecoveryTelemetryError("host CPU sample gap is stale")
        previous_observed = observed_monotonic
        if cancelled():
            raise RecoveryCancelled("resource recovery was cancelled")

        if percent < config.low_cpu_percent:
            if low_since is None:
                low_since = observed_monotonic
        else:
            low_since = None
        low_elapsed = 0.0 if low_since is None else observed_monotonic - low_since
        waited = already_waited_seconds + elapsed
        progress = CooldownProgress(
            waited_seconds=waited,
            current_host_cpu_percent=percent,
            low_cpu_seconds=low_elapsed,
            remaining_cooldown_seconds=max(0.0, config.max_cooldown_seconds - waited),
            remaining_series_wait_seconds=max(
                0.0,
                config.max_series_cooldown_seconds - (series_waited_seconds + elapsed),
            ),
            observed_at=utc_now(),
        )
        try:
            on_progress(progress)
        except OSError as exc:
            raise RecoveryEvidenceError(
                "resource recovery progress evidence is unavailable"
            ) from exc

        if low_elapsed >= config.low_cpu_seconds:
            decision_monotonic = monotonic()
            if (
                not _is_finite(decision_monotonic)
                or decision_monotonic < observed_monotonic
            ):
                raise RecoveryTelemetryError("monotonic clock is invalid")
            if decision_monotonic - started > available:
                raise RecoveryBudgetExhausted(
                    "resource recovery wait budget is exhausted"
                )
            if decision_monotonic - observed_monotonic > config.sample_seconds:
                raise RecoveryTelemetryError("host CPU sample is stale")
            if cancelled():
                raise RecoveryCancelled("resource recovery was cancelled")
            return CooldownResult(
                waited_seconds=waited,
                final_host_cpu_percent=percent,
                low_cpu_seconds=low_elapsed,
                observed_at=progress.observed_at,
            )
        remaining = available - elapsed
        if remaining <= 0:
            raise RecoveryBudgetExhausted("resource recovery wait budget is exhausted")
        sleep_for = min(config.sample_seconds, remaining)
        if low_since is not None:
            sleep_for = min(
                sleep_for,
                max(0.0, config.low_cpu_seconds - low_elapsed),
            )
        sleep(sleep_for)


@dataclass
class SeriesOwnershipLock:
    """Nonblocking process-lifetime flock for one series manifest writer."""

    path: Path
    _handle: TextIO | None = field(default=None, init=False)

    def acquire(self) -> None:
        if self._handle is not None:
            raise RuntimeError("series ownership lock is already held")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BlockingIOError as exc:
            handle.close()
            raise RecoveryOwnershipError(
                "checkpoint series is owned by another foreground process"
            ) from exc
        except BaseException:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
            raise
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
