"""Reviewed bytes must remain stable through maintenance admission."""

import json
from datetime import timedelta

import pytest
import test_cre_code_handoff as handoff
import test_cre_runtime_maintenance_admission as admission

from cre_code_handoff_evidence import HandoffError

admitted = admission.admitted
complete_case = admission.complete_case
lineage_case = admission.lineage_case
restoration_case = admission.restoration_case
maintenance_case = admission.maintenance_case


def test_authorization_rechecks_review_body_after_initial_read(maintenance_case):
    from cre_runtime_maintenance import validate_maintenance_authorization

    packet, evidence, inputs, _, path = maintenance_case
    now = handoff.NOW
    release = json.dumps(packet).encode()
    observation = handoff.host_observation()
    observation.update(schema="cre-runtime-maintenance-observation-v1", native=packet["native_before"],
                       runtime=packet["old"], observed_at=now.isoformat(), qualified_at=now.isoformat(),
                       release_sha256=handoff.sha(release),
                       ownership={"phase": "released-clear", "verified": True, "recovery_required": False},
                       resources_ok=True, queues_idle=True,
                       tasks=[dict(row, state="Disabled", xml_sha256=handoff.sha(inputs.data[path(row["xml"])]))
                              for row in packet["task_baseline"]])
    observed = json.dumps(observation).encode()
    body_path, review_path = "/synthetic/review.md", "/synthetic/review.json"
    body = b"reviewed original"
    review = handoff.encoded(handoff.attestation({"release_sha256": handoff.sha(release), "tooling": packet["tooling"]},
                                                 body_path, body, scope="runtime-maintenance"))
    auth = {"schema": "cre-runtime-maintenance-authorization-v1", "scope": "held-runtime-maintenance",
            "release_sha256": handoff.sha(release), "observation_sha256": handoff.sha(observed),
            "authorized_at": now.isoformat(), "expires_at": (now + timedelta(seconds=299)).isoformat(),
            "operation_output": "/synthetic/operation", "review": {"path": review_path, "sha256": handoff.sha(review),
            "reviewer": "independent", "approval_ref": "AGENTIC-test"}}
    reads = 0

    def reader(path_value):
        nonlocal reads
        if path_value == review_path:
            return review
        if path_value == body_path:
            reads += 1
            return body if reads == 1 else b"changed during admission"
        return inputs.data[path_value]

    evidence.read = reader
    with pytest.raises(HandoffError):
        validate_maintenance_authorization(release, packet, evidence, observed, auth, now,
                                           output="/synthetic/operation", review_reader=reader)
