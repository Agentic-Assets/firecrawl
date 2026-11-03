"""Legacy failed-02 Scheduler samples retain exact verified XML references."""
import copy

import pytest
from test_cre_code_handoff import encoded
from test_cre_code_restoration_execution import scheduler_case

from cre_code_handoff_evidence import Evidence, HandoffError, sha
from cre_code_restoration_evidence import restoration_execution


@pytest.mark.parametrize("fault", [None, "missing", "changed", "history", "reference", "added"])
def test_legacy_scheduler_references_are_verified_before_semantic_comparison(fault):
    packet, inputs, record, observed = scheduler_case("none")
    for row in observed["tasks"]:
        raw = inputs.data[row["xml"]]
        path = "/legacy/" + row["key"] + ".xml"
        inputs.data[path] = raw
        row["xml_sha256"] = sha(raw)
        row["xml_reference"] = {"path": path, "sha256": sha(raw), "bytes": len(raw)}
    record["tasks"] = copy.deepcopy(observed["tasks"])
    if fault == "missing": record["tasks"][0].pop("xml_reference")
    if fault == "changed": inputs.data[observed["tasks"][0]["xml_reference"]["path"]] += b"changed"
    if fault == "reference": record["tasks"][0]["xml_reference"]["path"] = "/legacy/other.xml"
    if fault == "added": record["tasks"][0]["unexpected"] = True
    if fault == "history": record["tasks"][0]["last_task_result"] += 1
    raw_record = encoded(record)
    inputs.data["/legacy/execution.json"] = raw_record
    observed["execution"] = {"path": "/legacy/execution.json", "sha256": sha(raw_record), "bytes": len(raw_record)}
    evidence = Evidence(packet, inputs.data.__getitem__)
    if fault is None:
        with pytest.raises(HandoffError): restoration_execution(packet, evidence, observed)
        restoration_execution(packet, evidence, observed, legacy_task_references=True)
    else:
        with pytest.raises(HandoffError): restoration_execution(packet, evidence, observed, legacy_task_references=True)
