"""Maintenance cannot treat a Scheduler execution as an idle interval."""

from datetime import timedelta

import pytest
import test_cre_code_handoff as fixtures
from test_cre_code_restoration_execution import scheduler_case

from cre_code_handoff_evidence import Evidence, HandoffError, sha


@pytest.mark.parametrize("outcome", ["none", "busy", "running"])
def test_maintenance_requires_bounded_scheduler_interval_without_execution(outcome):
    from cre_runtime_maintenance_transition import validate_maintenance_execution

    packet, inputs, record, observed = scheduler_case(outcome)
    if outcome == "none":
        for row in observed["tasks"]:
            row.update(enabled=False, state="Disabled")
        record["tasks"] = observed["tasks"]
    raw = fixtures.encoded(record)
    inputs.data["/synthetic/execution.json"] = raw
    observed["execution"] = {"path": "/synthetic/execution.json", "sha256": sha(raw), "bytes": len(raw)}
    evidence = Evidence(packet, inputs.data.__getitem__)
    if outcome == "none":
        validate_maintenance_execution(packet, evidence, observed, hold_at=fixtures.NOW, mutation_at=fixtures.NOW + timedelta(seconds=1),
                                       terminal_at=fixtures.NOW + timedelta(seconds=9))
    else:
        with pytest.raises(HandoffError):
            validate_maintenance_execution(packet, evidence, observed, hold_at=fixtures.NOW, mutation_at=fixtures.NOW + timedelta(seconds=1),
                                           terminal_at=fixtures.NOW + timedelta(seconds=9))


@pytest.mark.parametrize("change", [None, "late-watermark", "early-finish"])
def test_scheduler_interval_spans_mutation_through_terminal(change):
    from cre_runtime_maintenance_transition import validate_maintenance_execution

    packet, inputs, record, observed = scheduler_case("none")
    for row in observed["tasks"]:
        row.update(enabled=False, state="Disabled")
    record["tasks"] = observed["tasks"]
    raw = fixtures.encoded(record)
    inputs.data["/synthetic/execution.json"] = raw
    observed["execution"] = {"path": "/synthetic/execution.json", "sha256": sha(raw), "bytes": len(raw)}
    start = fixtures.NOW + timedelta(seconds=-1 if change == "late-watermark" else 1)
    end = fixtures.NOW + timedelta(seconds=11 if change == "early-finish" else 9)
    evidence = Evidence(packet, inputs.data.__getitem__)
    if change is None:
        validate_maintenance_execution(packet, evidence, observed, hold_at=fixtures.NOW, mutation_at=start, terminal_at=end)
    else:
        with pytest.raises(HandoffError):
            validate_maintenance_execution(packet, evidence, observed, hold_at=fixtures.NOW, mutation_at=start, terminal_at=end)


def test_scheduler_watermark_precedes_mutation_marker():
    from cre_runtime_maintenance_transition import validate_maintenance_execution

    packet, inputs, record, observed = scheduler_case("none")
    for row in observed["tasks"]:
        row.update(enabled=False, state="Disabled")
    record["tasks"] = observed["tasks"]
    raw = fixtures.encoded(record)
    inputs.data["/synthetic/execution.json"] = raw
    observed["execution"] = {"path": "/synthetic/execution.json", "sha256": sha(raw), "bytes": len(raw)}
    with pytest.raises(HandoffError):
        validate_maintenance_execution(packet, Evidence(packet, inputs.data.__getitem__), observed,
                                       hold_at=fixtures.NOW - timedelta(seconds=2),
                                       mutation_at=fixtures.NOW - timedelta(seconds=1),
                                       terminal_at=fixtures.NOW + timedelta(seconds=9))


def test_watermark_may_be_captured_during_hold_before_mutation():
    from cre_runtime_maintenance_transition import validate_maintenance_execution

    packet, inputs, record, observed = scheduler_case("none")
    for row in observed["tasks"]:
        row.update(enabled=False, state="Disabled")
    record["tasks"] = observed["tasks"]
    raw = fixtures.encoded(record)
    inputs.data["/synthetic/execution.json"] = raw
    observed["execution"] = {"path": "/synthetic/execution.json", "sha256": sha(raw), "bytes": len(raw)}
    validate_maintenance_execution(packet, Evidence(packet, inputs.data.__getitem__), observed,
                                   hold_at=fixtures.NOW - timedelta(seconds=1),
                                   mutation_at=fixtures.NOW + timedelta(seconds=1),
                                   terminal_at=fixtures.NOW + timedelta(seconds=9))
