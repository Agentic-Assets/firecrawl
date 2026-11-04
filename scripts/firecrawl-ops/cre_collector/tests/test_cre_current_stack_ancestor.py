"""Resolve no-image predecessor to the actual reviewed image producer."""
import json

import pytest

import cre_current_stack as stack
import cre_runtime_maintenance_continuation as continuation
from cre_code_handoff_evidence import Evidence, HandoffError, sha


def test_image_source_follows_bound_no_image_predecessor_and_refuses_tampering(monkeypatch):
    leaf = {"schema": "cre-runtime-maintenance-v1", "expected_images": {"api": "image"}, "inputs": []}
    leaf_raw = json.dumps(leaf).encode()
    leaf_ref = {"role": "older", "path": "/leaf", "bytes": len(leaf_raw), "sha256": sha(leaf_raw)}
    prior = {"schema": "cre-runtime-maintenance-v1", "expected_images": {}, "image_tags": {},
             "install_scope": [], "continuation": {"failed_release": "older"}, "inputs": [leaf_ref]}
    prior_raw = json.dumps(prior).encode()
    initial_ref = {"role": "prior", "path": "/prior", "bytes": len(prior_raw), "sha256": sha(prior_raw)}
    packet = {"continuation": {"failed_release": "prior"}, "inputs": [initial_ref]}
    data = {"/leaf": leaf_raw, "/prior": prior_raw}
    evidence = Evidence(packet, data.__getitem__)
    checked = []
    monkeypatch.setattr(continuation, "validate_continuation_code", lambda p, g, r: checked.append(p))
    raw, resolved, _ = stack.resolve_image_source(packet, evidence, object(), "/repo")
    assert raw == leaf_raw and resolved == leaf and checked == [prior]
    data["/leaf"] = leaf_raw + b" "
    with pytest.raises(HandoffError):
        evidence.recheck()
