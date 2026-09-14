"""Shared strict parsers for CRE capacity runtime settlement telemetry."""

from __future__ import annotations

from typing import Any

RABBITMQ_HEADER = ("name", "messages_ready", "messages_unacknowledged")
NUQ_COUNTERS = {
    "queue_scrape_total",
    "queue_scrape_backlog_total",
    "queue_crawl_finished_total",
}


class CapacityTelemetryError(ValueError):
    """A settlement command returned incomplete or malformed telemetry."""


def parse_rabbitmq_settlement(output: str) -> dict[str, int]:
    """Parse rabbitmqctl's optional header and exact queue counters."""
    rows = output.splitlines()
    if rows and tuple(rows[0].split()) == RABBITMQ_HEADER:
        rows = rows[1:]
    counts: list[tuple[int, int]] = []
    queue_names: set[str] = set()
    for row in rows:
        parts = row.rsplit(maxsplit=2)
        if (
            len(parts) != 3
            or not parts[0]
            or parts[0] in queue_names
            or not parts[1].isdigit()
            or not parts[2].isdigit()
        ):
            raise CapacityTelemetryError("RabbitMQ settlement telemetry is invalid")
        queue_names.add(parts[0])
        counts.append((int(parts[1]), int(parts[2])))
    if not counts:
        raise CapacityTelemetryError("RabbitMQ settlement telemetry is empty")
    return {
        "rabbitmq_queue_count": len(counts),
        "rabbitmq_ready": sum(item[0] for item in counts),
        "rabbitmq_unacknowledged": sum(item[1] for item in counts),
    }


def parse_nuq_settlement(output: str) -> dict[str, Any]:
    """Parse the exact NuQ table-count query response."""
    counts: dict[str, int] = {}
    for row in output.splitlines():
        parts = row.split("|")
        if (
            len(parts) != 2
            or parts[0] not in NUQ_COUNTERS
            or parts[0] in counts
            or not parts[1].isdigit()
        ):
            raise CapacityTelemetryError("NuQ settlement telemetry is invalid")
        counts[parts[0]] = int(parts[1])
    if set(counts) != NUQ_COUNTERS:
        raise CapacityTelemetryError("NuQ settlement telemetry is incomplete")
    return {"nuq": counts}
