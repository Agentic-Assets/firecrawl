"""Hold-only failed predecessor joined to a genuine synthetic successor."""
import copy
import json
from datetime import timedelta

import pytest
import test_cre_continued_integrated as continued
from continued_hold_prefix_fixture import produce_hold_failure
from test_cre_continued_apply import assert_produced_transition_and_restoration
from test_cre_maintenance_integrated import run_actual_apply_steps
from test_cre_partial_full_join import (
    admitted,  # noqa: F401
    build_partial_successor,
    complete_case,  # noqa: F401
    continued_ready_case,  # noqa: F401
    current_inputs,  # noqa: F401
    lineage_case,  # noqa: F401
    qualified_ready_case,  # noqa: F401
    restoration_case,  # noqa: F401
    scheduler_principal_in_synthetic_xml,  # noqa: F401
)

import cre_current_stack
import cre_runtime_maintenance as maintenance
from cre_code_handoff_evidence import Evidence, HandoffError, sha
from cre_code_handoff_git import Git
from cre_maintenance_validation_context import LoadedMaintenance


def test_hold_only_failure_through_owned_successor_and_restoration(continued_ready_case, tmp_path, monkeypatch):  # noqa: F811
    case, prior, _ = build_partial_successor(continued_ready_case, tmp_path, monkeypatch, False)
    before = copy.deepcopy(case.before)
    before_time = case.now
    case.output.rmdir()
    work = tmp_path / "hold-prefix"
    work.mkdir()
    with monkeypatch.context() as scoped:
        failed = produce_hold_failure(work, prior, case.release, case.observation, case.authorization,
                                      case.output, case.evidence, before_time, scoped, case.source)
    selected_file = case.source.root / "scripts/firecrawl-ops/cre_collector/synthetic-ops-change.txt"
    selected_file.write_text("second synthetic successor\n")
    case.source.git("commit", "-am", "synthetic held-prefix successor")
    selected = {"sha": case.source.git("rev-parse", "HEAD"), "tree": case.source.git("rev-parse", "HEAD^{tree}")}
    packet = copy.deepcopy(prior)
    packet.update(new=selected, tooling=selected, development=selected, native_before=prior["new"],
                  runtime_before=prior["runtime_before"], task_baseline=failed.task_baseline)
    packet["build_source"]["identity"] = selected
    file_path = "scripts/firecrawl-ops/cre_collector/synthetic-ops-change.txt"
    real_git = Git("unused")
    for key in ("native", "runtime"):
        deployed = packet[key + "_before"]
        old = real_git.entries(case.source.root, deployed["sha"])
        new = real_git.entries(case.source.root, selected["sha"])
        packet[key + "_change"] = {"deployed": deployed, "selected": selected, "allowed_paths": [file_path],
                                   "objects": {file_path: {"before": old[file_path], "after": new[file_path]}},
                                   "changed_dependency_paths": []}
    case.inputs.rows = copy.deepcopy(prior["inputs"])
    for row in failed.roles:
        case.inputs.rows = [old for old in case.inputs.rows if old["role"] != row["role"]]
        case.inputs.rows.append(row)
        case.inputs.data[row["path"]] = case.evidence.read(row["path"])
    for role in ("native_allowed_paths_nul", "runtime_allowed_paths_nul"):
        continued.add_file_role(case.inputs, tmp_path / "hold-successor-roles", role, file_path.encode() + b"\0")
    model = copy.deepcopy(case.built.receipt)
    model["tooling_identity"] = selected
    continued.add_file_role(case.inputs, tmp_path / "hold-successor-model", "current_stack", model)
    model_row = next(row for row in case.inputs.rows if row["role"] == "current_stack")
    review = json.loads(case.evidence.read(case.built.refs["current_stack_review"]["path"]))
    review["subject"]["current_stack_sha256"] = model_row["sha256"]
    continued.add_file_role(case.inputs, tmp_path / "hold-successor-model", "current_stack_review", review)
    packet["continuation"] = {**failed.continuation, "current_stack": "current_stack", "current_stack_review": "current_stack_review"}
    case.now = failed.fresh_time + timedelta(seconds=3)
    continued.bind_qa(packet, case.inputs, tmp_path / "hold-successor-qa", selected, selected, case.now, "hold-qa")
    packet["inputs"] = copy.deepcopy(case.inputs.rows)
    evidence = Evidence(packet, case.evidence.read)
    maintenance.validate_maintenance_release(packet, evidence)
    launcher_stdout = work / "synthetic-launcher" / "03-apply.stdout.raw"
    launcher_stdout.write_bytes(b"tampered launcher stream")
    with pytest.raises(HandoffError):
        maintenance.validate_maintenance_release(packet, Evidence(packet, case.evidence.read))
    launcher_stdout.write_bytes(b"")
    case.packet, case.evidence, case.new = packet, evidence, selected
    case.before_identities = {"native": prior["new"], "runtime": prior["runtime_before"]}
    case.before = before
    case.before.update(**case.before_identities)
    case.before["tasks"] = [dict(row, state="Disabled", xml_sha256=sha(evidence.raw(row["xml"])))
                            for row in packet["task_baseline"]]
    case.release = tmp_path / "hold-successor-release.json"
    case.release_raw = continued.handoff.encoded(packet)
    case.release.write_bytes(case.release_raw)
    case.before.update(release_sha256=sha(case.release_raw), observed_at=case.now.isoformat(), qualified_at=case.now.isoformat())
    case.observation = tmp_path / "hold-successor-observation.json"
    case.observation.write_bytes(continued.handoff.encoded(case.before))
    body = tmp_path / "hold-successor-review.md"; body.write_bytes(b"Synthetic successor review.\n")
    review_path = tmp_path / "hold-successor-review.json"
    review_path.write_bytes(continued.handoff.encoded(continued.handoff.attestation(
        {"release_sha256": sha(case.release_raw), "tooling": selected}, body, body.read_bytes(), scope="runtime-maintenance")))
    auth = json.loads(case.authorization.read_bytes())
    case.output = tmp_path / "hold-successor-operation"; case.output.mkdir()
    auth.update(release_sha256=sha(case.release_raw), observation_sha256=sha(case.observation.read_bytes()),
                authorized_at=case.now.isoformat(), expires_at=(case.now + timedelta(seconds=299)).isoformat(),
                operation_output=str(case.output))
    auth["review"].update(path=str(review_path), sha256=sha(review_path.read_bytes()))
    case.authorization = tmp_path / "hold-successor-authorization.json"
    case.authorization.write_bytes(continued.handoff.encoded(auth))
    case.built.refs["current_stack"] = {key: model_row[key] for key in ("path", "sha256", "bytes")}
    monkeypatch.setattr(cre_current_stack, "capture_current_stack", lambda *_args, **_kwargs: case.built.refs["current_stack"])
    case.evidence._loaded_maintenance = LoadedMaintenance(case.release_raw, packet, evidence)
    run_actual_apply_steps(case, tmp_path, monkeypatch, False, with_ipc=True)
    assert_produced_transition_and_restoration(case, tmp_path)
