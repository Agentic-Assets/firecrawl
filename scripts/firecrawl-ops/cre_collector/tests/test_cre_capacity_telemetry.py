"""Strict shared parsing contracts for CRE capacity settlement telemetry."""

from pathlib import Path

import cre_capacity_telemetry as telemetry
import pytest

RABBITMQ_3137_IDLE = (
    Path(__file__).with_name("fixtures") / "rabbitmq-3.13.7-idle.txt"
).read_text(encoding="utf-8")


def test_parse_rabbitmq_3137_sanitized_idle_fixture() -> None:
    assert telemetry.parse_rabbitmq_settlement(RABBITMQ_3137_IDLE) == {
        "rabbitmq_queue_count": 4,
        "rabbitmq_ready": 0,
        "rabbitmq_unacknowledged": 0,
    }


@pytest.mark.parametrize(
    "output",
    [
        "",
        "name messages_ready messages_unacked\nextract.jobs 0 0\n",
        "extract.jobs zero 0\n",
        "extract.jobs 0 0\nextract.jobs 0 0\n",
        "extract.jobs 0 0\nname messages_ready messages_unacknowledged\n",
    ],
)
def test_parse_rabbitmq_rejects_empty_or_malformed_evidence(output: str) -> None:
    with pytest.raises(telemetry.CapacityTelemetryError, match="RabbitMQ"):
        telemetry.parse_rabbitmq_settlement(output)


def test_parse_nuq_requires_each_exact_counter_once() -> None:
    valid = (
        "queue_crawl_finished_total|0\n"
        "queue_scrape_backlog_total|0\n"
        "queue_scrape_total|0\n"
    )
    assert telemetry.parse_nuq_settlement(valid) == {
        "nuq": {
            "queue_crawl_finished_total": 0,
            "queue_scrape_backlog_total": 0,
            "queue_scrape_total": 0,
        }
    }
    with pytest.raises(telemetry.CapacityTelemetryError, match="NuQ"):
        telemetry.parse_nuq_settlement(valid + "queue_scrape_total|0\n")
