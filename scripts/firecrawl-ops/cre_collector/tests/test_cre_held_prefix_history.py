"""Original A baseline survives a held-only failure after partial repin."""
import json

import pytest

from cre_code_handoff_evidence import TASKS, Evidence, HandoffError, sha
from cre_runtime_maintenance_transition import historical_held_tasks


def test_historical_task_walk_uses_sealed_original_xml_without_synthesizing():
    old = {"sha": "a" * 40, "tree": "b" * 40}
    data = {}
    def ref(role, path, value):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        data[path] = raw
        return {"role": role, "path": path, "bytes": len(raw), "sha256": sha(raw)}
    leaf_rows = []
    inputs = []
    for key in TASKS:
        inputs.append(ref("xml-" + key, "/leaf/" + key, ("original " + key).encode()))
        leaf_rows.append({"key": key, "xml": "xml-" + key})
    leaf = {"schema": "cre-runtime-maintenance-v1", "old": old, "task_baseline": leaf_rows, "inputs": inputs}
    mid = {"schema": "cre-runtime-maintenance-v1", "old": old,
           "task_baseline": [{"key": key, "pin_before": "c" * 40} for key in TASKS],
           "continuation": {"failed_release": "leaf"}, "inputs": [ref("leaf", "/leaf.json", leaf)]}
    packet = {"old": old, "task_baseline": [{"key": key, "pin_before": "c" * 40} for key in TASKS],
              "continuation": {"failed_release": "mid"}, "inputs": [ref("mid", "/mid.json", mid)]}
    evidence = Evidence(packet, data.__getitem__)
    original, rows = historical_held_tasks(packet, evidence, data.__getitem__)
    assert original.raw(rows["interra"]["xml"]) == b"original interra"
    assert "/leaf.json" in evidence.original_payloads
    data["/leaf.json"] += b"changed"
    with pytest.raises(HandoffError): evidence.recheck()
