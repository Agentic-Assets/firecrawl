"""Mixed task pins must be backed by exact observed partial-operation bytes."""
import copy

import pytest
from test_cre_code_handoff import NEW, OLD, Inputs, xml

import cre_runtime_maintenance_continuation as continuation
from cre_code_handoff_evidence import TASKS, HandoffError, sha


@pytest.mark.parametrize("fault", [None, "wrong-pin", "xml-drift", "history-drift", "missing-final", "missed", "desired"])
def test_actual_partial_repin_join_preserves_each_held_task(fault):
    inputs = Inputs()
    before, current, observed = [], [], {}
    operation = "/synthetic/failed-operation"
    for key, name in TASKS.items():
        old_xml = xml(OLD["sha"], enabled=False)
        old_role = inputs.add("old-" + key, old_xml)
        baseline = {"key": key, "task_path": "\\CRE\\", "task_name": name, "enabled": False,
                    "desired_enabled": True, "missed_runs": 23, "last_task_result": 1 if key == "qualified" else 0,
                    "last_run_utc": "2026-10-22T12:00:00Z", "next_run_utc": "2026-11-04T12:00:00Z",
                    "actions": [{"execute": "pwsh", "arguments": OLD["sha"], "working_directory": "C:/runtime"}],
                    "xml": old_role}
        before.append(baseline)
        after_xml = old_xml.replace(OLD["sha"].encode(), NEW["sha"].encode()) if key == "interra" else old_xml
        new_role = inputs.add("new-" + key, after_xml)
        row = copy.deepcopy(baseline)
        row.update(xml=new_role, pin_before=NEW["sha"] if key == "interra" else OLD["sha"])
        if key == "interra": row["actions"][0]["arguments"] = NEW["sha"]
        current.append(row)
        readback = dict(row, state="Disabled", xml_sha256=sha(after_xml), pin_state="selected" if key == "interra" else "original")
        readback.pop("xml"); readback.pop("pin_before"); readback.pop("desired_enabled")
        readback["xml_reference"] = {"path": "/audit/" + key + ".xml", "sha256": sha(after_xml), "bytes": len(after_xml)}
        observed["/audit/" + key + ".xml"] = after_xml
        observed[operation + "/" + key + "-before.xml"] = old_xml
        if key == "interra": observed[operation + "/interra-final.xml"] = after_xml
        observed.setdefault("tasks", []).append(readback)
    packet = {"old": OLD, "task_baseline": current}
    prior = {"old": OLD, "new": NEW, "task_baseline": before}
    evidence = inputs.evidence(packet)
    if fault == "wrong-pin": current[0]["pin_before"] = OLD["sha"]
    if fault == "xml-drift": observed["/audit/interra.xml"] += b" "
    if fault == "history-drift": observed["tasks"][0]["last_task_result"] = 2
    if fault == "missed": current[0]["missed_runs"] = 22
    if fault == "desired": current[0]["desired_enabled"] = False
    if fault == "missing-final": del observed[operation + "/interra-final.xml"]
    if fault:
        with pytest.raises(HandoffError): continuation.validate_partial_task_lineage(packet, prior, evidence, observed, operation)
    else:
        continuation.validate_partial_task_lineage(packet, prior, evidence, observed, operation)
