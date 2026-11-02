"""Image recreation must use the reviewed configuration and overlay bytes."""

import pytest
import test_cre_code_handoff as fixtures

from cre_code_handoff_evidence import HandoffError
from cre_runtime_maintenance_build import reviewed_override


def test_image_review_binds_expected_configuration_and_compose_overlay():
    from cre_runtime_maintenance_transition import validate_maintenance_image_review

    tags = {"api": "firecrawl-api:qa-12345678"}
    packet = {"image_tags": tags, "expected_after_config": {"api": "a" * 64}}
    inputs = fixtures.Inputs()
    inputs.add("expected_after_config", {"api": "a" * 64})
    inputs.add("compose_override", reviewed_override(tags))
    validate_maintenance_image_review(packet, inputs.evidence(packet))
    packet["expected_after_config"] = {"api": "b" * 64}
    with pytest.raises(HandoffError):
        validate_maintenance_image_review(packet, inputs.evidence(packet))
    packet["expected_after_config"] = {"api": "a" * 64}
    inputs.data["compose_override"] = b"foreign override"
    with pytest.raises(HandoffError):
        validate_maintenance_image_review(packet, inputs.evidence(packet))


def test_projection_receipt_binds_all_reviewed_evidence(tmp_path):
    from cre_code_handoff_evidence import Evidence, sha
    from cre_runtime_maintenance_transition import (
        validate_maintenance_projection_review,
    )

    tags = {"api": "firecrawl-api:qa-12345678"}
    packet = {"new": {"sha": "a" * 40, "tree": "b" * 40}, "image_tags": tags,
              "expected_after_config": {"api": "a" * 64}, "native_docker_sha256": "c" * 64,
              "allowed_image_default_changes": {}, "projection_output": str(tmp_path)}
    def write(name, value):
        path = tmp_path / name
        raw = value if isinstance(value, bytes) else fixtures.encoded(value)
        path.write_bytes(raw)
        return {"path": str(path), "sha256": sha(raw), "bytes": len(raw)}
    values = {"container_config_before": [], "expected_after_config": packet["expected_after_config"],
              "compose_sources": {}, "compose_override": reviewed_override(tags)}
    refs = {key: write(key, value) for key, value in values.items()}
    evidence = Evidence({"inputs": [dict(role=key, **ref) for key, ref in refs.items()]},
                        lambda path: __import__("pathlib").Path(path).read_bytes())
    projection = {"schema": "cre-image-configuration-projection-v1", "identity": packet["new"],
                  "docker_sha256": packet["native_docker_sha256"], "changed_image_default_keys": {}, **refs}
    projection_ref = write("configuration-projection.json", projection)
    validate_maintenance_projection_review(packet, evidence, projection_ref)
    projection["identity"] = {"sha": "d" * 40, "tree": "e" * 40}
    projection_ref = write("configuration-projection.json", projection)
    with pytest.raises(HandoffError):
        validate_maintenance_projection_review(packet, evidence, projection_ref)
