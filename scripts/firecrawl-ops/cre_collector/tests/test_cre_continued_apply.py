"""Actual no-image continuation producer joined to retained stack evidence."""

import copy
import json
from datetime import timedelta
from pathlib import Path

import pytest
import test_cre_continued_integrated as continued
import test_cre_maintenance_chain as chain
from test_cre_maintenance_integrated import run_actual_apply_steps

import cre_current_stack
from cre_code_handoff_evidence import Evidence, HandoffError, sha

admitted = continued.admitted
complete_case = continued.complete_case
lineage_case = continued.lineage_case
restoration_case = continued.restoration_case
qualified_ready_case = continued.qualified_ready_case
scheduler_principal_in_synthetic_xml = continued.scheduler_principal_in_synthetic_xml
current_inputs = continued.current_inputs
continued_ready_case = continued.continued_ready_case


def test_actual_continued_apply_uses_real_stack_and_retained_predecessor(continued_ready_case, tmp_path, monkeypatch):
    case = continued_ready_case
    # Only the Docker/OS observation boundary is substituted. The real live
    # stack validator consumes the same sealed model and runs all retained checks.
    monkeypatch.setattr(cre_current_stack, "capture_current_stack",
                        lambda *args, **kwargs: case.built.refs["current_stack"])
    run_actual_apply_steps(case, tmp_path, monkeypatch, False)
    ready_path = case.output / "maintenance-ready.json"
    ready = json.loads(ready_path.read_bytes())
    terminal = json.loads((case.output / "terminal.json").read_bytes())
    assert not list(case.output.glob("*-npm-ci.json"))
    assert not list(case.output.glob("*-compose-up.json"))
    independent = tmp_path / "continued-independent"
    independent.mkdir()

    def write(path, value):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        path.write_bytes(raw)
        return path

    def ref(path):
        raw = path.read_bytes()
        return {"path": str(path), "sha256": sha(raw), "bytes": len(raw)}

    stdout = write(Path(str(case.output) + ".stdout.raw"), b"synthetic native output")
    stderr = write(Path(str(case.output) + ".stderr.raw"), b"")
    completion = write(Path(str(case.output) + ".native.json"), {
        "schema": "cre-runtime-maintenance-native-completion-v1",
        "release_sha256": sha(case.release_raw), "authorization_sha256": sha(case.authorization.read_bytes()),
        "operation_output": str(case.output), "stdout_sha256": sha(stdout.read_bytes()),
        "stderr_sha256": sha(stderr.read_bytes()),
        "capture": {"Started": True, "Failed": False, "NativeExitCode": 0,
                    "Stdout": {"Eof": True, "Truncated": False}, "Stderr": {"Eof": True, "Truncated": False}},
    })
    observed = copy.deepcopy(ready["readiness"])
    for row in observed["tasks"]:
        target = write(independent / (row["key"] + "-observed.xml"), Path(row["xml_reference"]["path"]).read_bytes())
        row["xml_reference"] = ref(target)
    execution = json.loads(Path(observed["execution"]["path"]).read_bytes())
    execution["started_at"] = (case.now + timedelta(seconds=1)).isoformat()
    execution["finished_at"] = (case.now + timedelta(seconds=2)).isoformat()
    execution["tasks"] = [{key: value for key, value in row.items() if key != "xml_reference"}
                          for row in observed["tasks"]]
    execution_path = write(independent / "scheduler-execution.json", execution)
    containers = write(independent / "container-snapshot.json", case.normalized)
    observed.update(ownership={"phase": "released-clear", "verified": True, "recovery_required": False},
                    observed_at=(case.now + timedelta(seconds=3)).isoformat(),
                    qualified_at=(case.now + timedelta(seconds=4)).isoformat(), execution=ref(execution_path),
                    terminal_sha256=sha((case.output / "terminal.json").read_bytes()),
                    native_completion_sha256=sha(completion.read_bytes()),
                    released_authority=terminal["released_authority"], operation_output=str(case.output),
                    container_snapshot=ref(containers))
    write(independent / "observation.json", observed)
    from cre_code_restoration_evidence import validate_restoration
    from cre_runtime_maintenance_transition import (
        make_maintenance_transition,
        validate_maintenance_transition,
    )

    transition = make_maintenance_transition(case.release, case.output, independent, case.evidence.read)
    validated = validate_maintenance_transition(transition, case.evidence.read)
    assert validated["old"] == case.old and validated["new"] == case.new
    restoration = chain.restored_packet(case, transition)
    validate_restoration(restoration, Evidence(restoration, case.evidence.read))


def test_changed_stack_model_refuses_before_owned_apply(continued_ready_case, tmp_path, monkeypatch):
    case = continued_ready_case
    model = case.built.refs["current_stack"]["path"]
    with open(model, "ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(HandoffError):
        run_actual_apply_steps(case, tmp_path, monkeypatch, False)
    assert not case.output.exists()
