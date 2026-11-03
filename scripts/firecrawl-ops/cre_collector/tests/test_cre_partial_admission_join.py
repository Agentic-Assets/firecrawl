"""Mixed task pins do not rewrite the original failed-A validation input."""

import copy

import pytest
import test_cre_continued_integrated as continued

from cre_code_handoff_evidence import Evidence, HandoffError, sha
from cre_runtime_maintenance import (
    validate_historical_admission,
    validate_maintenance_release,
)

admitted = continued.admitted
complete_case = continued.complete_case
lineage_case = continued.lineage_case
restoration_case = continued.restoration_case
qualified_ready_case = continued.qualified_ready_case
scheduler_principal_in_synthetic_xml = continued.scheduler_principal_in_synthetic_xml
current_inputs = continued.current_inputs
continued_ready_case = continued.continued_ready_case


def test_mixed_baseline_reuses_original_a_only_from_retained_predecessor(continued_ready_case):
    case = continued_ready_case
    packet = copy.deepcopy(case.packet)
    raw = case.release.read_bytes()
    packet["inputs"].append({"role": "partial_prior", "path": str(case.release),
                             "bytes": len(raw), "sha256": sha(raw)})
    packet["continuation"] = {"failed_release": "partial_prior"}
    packet["task_baseline"][0]["pin_before"] = case.new["sha"]
    packet["task_baseline"][0]["actions"][0]["arguments"] = "different mixed task action"
    evidence = Evidence(packet, case.evidence.read)
    validate_historical_admission(packet, evidence)
    with pytest.raises(HandoffError):
        validate_maintenance_release(packet, evidence)
