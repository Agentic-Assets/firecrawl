"""Fast enable rechecks preserve the original admission and trigger boundary."""
from datetime import timedelta

import pytest
import test_cre_code_handoff_restoration as fixtures

import cre_code_restoration_evidence as restoration
from cre_code_handoff_evidence import HandoffError

NOW = fixtures.NOW
admitted = fixtures.admitted
complete_case = fixtures.complete_case
lineage_case = fixtures.lineage_case
restoration_case = fixtures.restoration_case
zero_missed_packet = fixtures.zero_missed_packet


def test_enable_context_rechecks_bytes_packet_and_time_without_replaying_lineage(restoration_case, monkeypatch):
    packet, evidence, _ = zero_missed_packet(restoration_case)
    context = restoration.prepare_enable_context(packet, evidence, NOW)
    def no_replay(*_args, **_kwargs):
        raise AssertionError("historical lineage was replayed")
    monkeypatch.setattr(restoration, "validate_failed_handoff", no_replay)
    restoration.validate_enable_context(packet, evidence, context, NOW)
    with pytest.raises(HandoffError, match="trigger"):
        restoration.validate_enable_context(packet, evidence, context, NOW + timedelta(hours=2))
    packet["task_baseline"][0]["missed_runs"] = 1
    with pytest.raises(HandoffError):
        restoration.validate_enable_context(packet, evidence, context, NOW)


def test_enable_context_rejects_changed_retained_input(restoration_case):
    packet, evidence, inputs = zero_missed_packet(restoration_case)
    context = restoration.prepare_enable_context(packet, evidence, NOW)
    role = packet["failed_handoff"]["release"]
    inputs.data[evidence.inputs[role]["path"]] += b" "
    with pytest.raises(HandoffError):
        restoration.validate_enable_context(packet, evidence, context, NOW)


def test_enable_context_reads_clock_after_slow_evidence_recheck(restoration_case, monkeypatch):
    packet, evidence, _ = zero_missed_packet(restoration_case)
    context = restoration.prepare_enable_context(packet, evidence, NOW)
    times = [NOW]
    original_recheck = evidence.recheck
    def delayed_recheck():
        original_recheck()
        times[0] = NOW + timedelta(hours=2)
    monkeypatch.setattr(evidence, "recheck", delayed_recheck)
    with pytest.raises(HandoffError, match="trigger"):
        restoration.validate_enable_context(packet, evidence, context, lambda: times[0])
