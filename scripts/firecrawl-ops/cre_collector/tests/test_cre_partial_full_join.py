"""Synthetic second failure joined to the retained original operation chain."""

import copy
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
import test_cre_continued_integrated as continued
from continued_partial_fixture import produce_partial_failure
from test_cre_maintenance_integrated import run_actual_apply_steps

from cre_code_handoff_evidence import Evidence, HandoffError, sha

complete_case = continued.complete_case
lineage_case = continued.lineage_case
restoration_case = continued.restoration_case
qualified_ready_case = continued.qualified_ready_case
scheduler_principal_in_synthetic_xml = continued.scheduler_principal_in_synthetic_xml
current_inputs = continued.current_inputs
continued_ready_case = continued.continued_ready_case


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    original = continued.handoff.Inputs

    class AbsoluteInputs(original):
        def add(self, name, value):
            role = super().add(name, value)
            path = tmp_path / "base-inputs" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.data[role])
            self.rows[-1]["path"] = str(path)
            self.data[str(path)] = self.data[role]
            return role

    monkeypatch.setattr(continued.handoff, "Inputs", AbsoluteInputs)
    release, evidence, observation, inputs = continued.handoff.admitted.__wrapped__()
    for task in release["task_baseline"]:
        if task["key"] != "qualified":
            next_run = continued.handoff.NOW + timedelta(days=1)
            task["next_run_utc"] = next_run.isoformat()
            next(row for row in observation["tasks"] if row["key"] == task["key"])["next_run_utc"] = next_run.isoformat()
    return release, evidence, observation, inputs


@pytest.mark.parametrize("crossed", [False, True], ids=["unchanged", "qualified-crossed"])
def test_partial_failure_joins_real_prior_and_successor_admission(continued_ready_case, tmp_path, monkeypatch, crossed):
    import cre_runtime_maintenance as maintenance

    case = continued_ready_case
    prior = copy.deepcopy(case.packet)
    prior_path = tmp_path / "sealed-partial-prior.json"
    prior_raw = continued.handoff.encoded(prior)
    prior_path.write_bytes(prior_raw)
    prior_observation = copy.deepcopy(case.before)
    prior_observation["release_sha256"] = sha(prior_raw)
    observation = tmp_path / "sealed-partial-observation.json"
    observation.write_bytes(continued.handoff.encoded(prior_observation))
    body = tmp_path / "sealed-partial-review.md"
    body.write_bytes(b"Independent synthetic review of the partial predecessor.\n")
    review = tmp_path / "sealed-partial-review.json"
    review.write_bytes(continued.handoff.encoded(continued.handoff.attestation(
        {"release_sha256": sha(prior_raw), "tooling": prior["tooling"]}, body, body.read_bytes(),
        scope="runtime-maintenance")))
    authorization = copy.deepcopy(json.loads(case.authorization.read_bytes()))
    authorization.update(release_sha256=sha(prior_raw), observation_sha256=sha(observation.read_bytes()))
    authorization["review"]["path"] = str(review)
    authorization["review"]["sha256"] = sha(review.read_bytes())
    auth = tmp_path / "sealed-partial-authorization.json"
    auth.write_bytes(continued.handoff.encoded(authorization))
    prior_evidence = Evidence(prior, case.evidence.read)
    maintenance.validate_maintenance_release(prior, prior_evidence)
    maintenance.validate_maintenance_authorization(prior_raw, prior, prior_evidence, observation.read_bytes(),
                                                   authorization, case.now, output=case.output)
    case.output.rmdir()
    with monkeypatch.context() as scoped:
        partial = produce_partial_failure(tmp_path, prior, prior_path, observation, auth, case.output,
                                          prior_evidence, case.now, scoped)

    packet = copy.deepcopy(prior)
    packet["task_baseline"] = partial.task_baseline
    fresh_time = None
    if crossed:
        from cre_code_handoff_evidence import clock

        audit = tmp_path / "partial-audit"
        native = json.loads((audit / "native-observation.json").read_bytes())
        tasks = copy.deepcopy(native["tasks"])
        for task in tasks:
            task.pop("pin_state")
        baseline = next(row for row in packet["task_baseline"] if row["key"] == "qualified")
        qualified = next(row for row in tasks if row["key"] == "qualified")
        previous_next = clock(baseline["next_run_utc"])
        fresh_time = previous_next + timedelta(minutes=1)
        baseline["missed_runs"] += 1
        baseline["next_run_utc"] = (previous_next + timedelta(days=1)).isoformat()
        qualified.update(missed_runs=baseline["missed_runs"], next_run_utc=baseline["next_run_utc"])
        execution = json.loads((audit / "scheduler-observation.json").read_bytes())
        execution.update(started_at=(fresh_time - timedelta(seconds=2)).isoformat(),
                         finished_at=(fresh_time - timedelta(seconds=1)).isoformat(),
                         tasks=[{key: value for key, value in row.items() if key != "xml_reference"} for row in tasks])
        execution_path = tmp_path / "fresh-scheduler.json"
        execution_path.write_bytes(continued.handoff.encoded(execution))
        execution_raw = execution_path.read_bytes()
        fresh = {"schema": "cre-held-partial-maintenance-observation-v1", "observed_at": fresh_time.isoformat(),
                 "tasks": tasks, "frontends": [], "instances": [],
                 "execution": {"path": str(execution_path), "bytes": len(execution_raw), "sha256": sha(execution_raw)}}
    packet["runtime_before"] = prior["new"]
    packet["native_before"] = prior["new"]
    changed = case.source.root / "scripts/firecrawl-ops/cre_collector/synthetic-ops-change.txt"
    changed.write_text("synthetic successor\n")
    case.source.git("commit", "-am", "synthetic successor")
    selected = {"sha": case.source.git("rev-parse", "HEAD"),
                "tree": case.source.git("rev-parse", "HEAD^{tree}")}
    packet["new"] = selected
    packet["tooling"] = selected
    packet["development"] = selected
    packet["build_source"]["identity"] = selected
    from cre_code_handoff_git import Git

    git = Git("unused")
    before, after = git.entries(case.source.root, prior["new"]["sha"]), git.entries(case.source.root, selected["sha"])
    path = "scripts/firecrawl-ops/cre_collector/synthetic-ops-change.txt"
    scope = {"deployed": prior["new"], "selected": selected, "allowed_paths": [path],
             "objects": {path: {"before": before[path], "after": after[path]}}, "changed_dependency_paths": []}
    packet["native_change"] = copy.deepcopy(scope)
    packet["runtime_change"] = copy.deepcopy(scope)
    case.inputs.rows = copy.deepcopy(prior["inputs"])
    for row in partial.roles:
        case.inputs.rows = [old for old in case.inputs.rows if old["role"] != row["role"]]
        case.inputs.rows.append(row)
    if crossed:
        capture_dir = tmp_path / "fresh-capture"
        observation_dir = capture_dir / "observation"
        observation_dir.mkdir(parents=True)
        def write_ref(path, raw):
            path.write_bytes(raw)
            return {"path": str(path), "bytes": len(raw), "sha256": sha(raw)}
        fresh_ref = write_ref(observation_dir / "observation.json", continued.handoff.encoded(fresh))
        program_ref = write_ref(capture_dir / "program-copy.bin", b"synthetic native PowerShell binary")
        script_ref = write_ref(capture_dir / "capture.ps1", b"synthetic read-only capture script\n")
        stdout_ref = write_ref(capture_dir / "stdout.raw", b"synthetic settled capture\n")
        stderr_ref = write_ref(capture_dir / "stderr.raw", b"")
        names = ("CreCodeHandoff.ps1", "CreTaskContract.ps1", "CreRestorationExecution.ps1",
                 "CreRuntimeMaintenance.ps1", "CreNativeCapture.cs")
        sources = []
        for name in names:
            relative = "scripts/firecrawl-ops/cre_collector/windows/" + name
            raw = (case.source.root / relative).read_bytes()
            sources.append({"path": packet["roots"]["native"] + "/" + relative,
                            "bytes": len(raw), "sha256": sha(raw)})
        def stream(ref):
            return {"ObservedBytes": ref["bytes"], "CapturedBytes": ref["bytes"], "Eof": True,
                    **dict.fromkeys(("Truncated", "ReadFailed", "WriteFailed", "FlushFailed", "CloseFailed",
                                     "DrainTimedOut", "ReaderDidNotStop", "RunReferenceConflict", "Failed"), False)}
        receipt = {"schema": "cre-held-partial-maintenance-native-capture-v1", "source_identity": prior["new"],
                   "source_root": packet["roots"]["native"], "source_files": sources, "capture_script": script_ref,
                   "program": {**program_ref, "path": packet["programs"]["powershell"]},
                   "argv": ["-NoProfile", "-NonInteractive", "-File", str(capture_dir / "capture.ps1"),
                            "-Output", str(observation_dir)], "observation": fresh_ref,
                   "stdout": stdout_ref, "stderr": stderr_ref,
                   "started_at": (fresh_time - timedelta(seconds=1)).isoformat(),
                   "finished_at": (fresh_time + timedelta(seconds=1)).isoformat(),
                   "capture": {"StartAttempted": True, "Started": True, "StartFailed": False,
                               "ReadOnlyDeadlineExpired": False, "NativeExitCode": 0, "Failed": False,
                               "Stdout": stream(stdout_ref), "Stderr": stream(stderr_ref)}}
        capture_ref = write_ref(capture_dir / "receipt.json", continued.handoff.encoded(receipt))
        for role, ref in (("fresh_held", fresh_ref), ("fresh_capture", capture_ref), ("fresh_program", program_ref)):
            case.inputs.rows.append({"role": role, **ref})
    for role in ("native_allowed_paths_nul", "runtime_allowed_paths_nul"):
        continued.add_file_role(case.inputs, tmp_path / "successor-roles", role, path.encode() + b"\0")
    model = copy.deepcopy(case.built.receipt)
    model["tooling_identity"] = selected
    continued.add_file_role(case.inputs, tmp_path / "successor-model", "current_stack", model)
    model_row = next(row for row in case.inputs.rows if row["role"] == "current_stack")
    model_review = json.loads(case.evidence.read(case.built.refs["current_stack_review"]["path"]))
    model_review["subject"]["current_stack_sha256"] = model_row["sha256"]
    continued.add_file_role(case.inputs, tmp_path / "successor-model", "current_stack_review", model_review)
    packet["continuation"] = {**partial.continuation, "current_stack": "current_stack",
                              "current_stack_review": "current_stack_review"}
    if crossed:
        packet["continuation"]["fresh_held_observation"] = "fresh_held"
        packet["continuation"]["fresh_held_capture"] = "fresh_capture"
        packet["continuation"]["fresh_held_program"] = "fresh_program"
    continued.bind_qa(packet, case.inputs, tmp_path / "successor-qa", selected, selected, case.now, "successor-qa")
    benchmark_dir = os.environ.get("CRE_TEST_OWNER_BENCHMARK_DIR")
    if benchmark_dir:
        from cre_code_handoff_git import read_file

        benchmark_paths = sorted(Path(benchmark_dir).glob("payload-??"))
        assert len(benchmark_paths) == 4
        for index, path in enumerate(benchmark_paths):
            payload = read_file(path)
            case.inputs.rows.append({"role": f"synthetic-benchmark-{index}", "path": str(path),
                                     "bytes": len(payload), "sha256": sha(payload)})
        assert sum(path.stat().st_size for path in benchmark_paths) >= 247_000_000
        previous_read = case.evidence.read
        def read(value):
            return read_file(value) if str(value) in {str(path) for path in benchmark_paths} else previous_read(value)
        case.evidence.read = read
    packet["inputs"] = copy.deepcopy(case.inputs.rows)
    evidence = Evidence(packet, case.evidence.read)
    maintenance.validate_maintenance_release(packet, evidence)
    if crossed:
        stale_observation = dict(case.before, native=prior["new"], runtime=prior["new"])
        stale_observation["tasks"] = [dict(row, state="Disabled", xml_sha256=sha(evidence.raw(row["xml"])))
                                      for row in packet["task_baseline"]]
        with pytest.raises(HandoffError, match="fresh held observation follows maintenance observation"):
            maintenance.validate_maintenance_observation(packet, evidence, stale_observation, case.now)

    # Run the actual successor producer with injected host/process boundaries,
    # then feed its output to the maintained transition and restoration readers.
    case.now = fresh_time + timedelta(seconds=2) if crossed else case.now + timedelta(seconds=20)
    case.packet, case.evidence, case.new = packet, evidence, selected
    case.before_identity = prior["new"]
    for row in partial.roles:
        case.inputs.data[row["path"]] = case.evidence.read(row["path"])
    case.before.update(native=prior["new"], runtime=prior["new"])
    case.before["tasks"] = [dict(row, state="Disabled", xml_sha256=sha(evidence.raw(row["xml"])))
                            for row in packet["task_baseline"]]
    case.release = tmp_path / "successor-release.json"
    case.release_raw = continued.handoff.encoded(packet)
    case.release.write_bytes(case.release_raw)
    case.before["release_sha256"] = sha(case.release_raw)
    case.observation = tmp_path / "successor-observation.json"
    case.before.update(observed_at=case.now.isoformat(), qualified_at=case.now.isoformat())
    case.observation.write_bytes(continued.handoff.encoded(case.before))
    successor_body = tmp_path / "successor-review.md"
    successor_body.write_bytes(b"Independent synthetic successor review.\n")
    successor_review = tmp_path / "successor-review.json"
    successor_review.write_bytes(continued.handoff.encoded(continued.handoff.attestation(
        {"release_sha256": sha(case.release_raw), "tooling": selected}, successor_body,
        successor_body.read_bytes(), scope="runtime-maintenance")))
    case.output = tmp_path / "successor-operation"
    case.output.mkdir()
    authorization.update(release_sha256=sha(case.release_raw), observation_sha256=sha(case.observation.read_bytes()),
                         authorized_at=case.now.isoformat(), expires_at=(case.now + timedelta(seconds=299)).isoformat(),
                         operation_output=str(case.output))
    authorization["review"]["path"] = str(successor_review)
    authorization["review"]["sha256"] = sha(successor_review.read_bytes())
    case.authorization = tmp_path / "successor-authorization.json"
    case.authorization.write_bytes(continued.handoff.encoded(authorization))
    case.built.refs["current_stack"] = {key: model_row[key] for key in ("path", "sha256", "bytes")}
    from test_cre_continued_apply import assert_produced_transition_and_restoration

    import cre_current_stack

    monkeypatch.setattr(cre_current_stack, "capture_current_stack", lambda *_args, **_kwargs: case.built.refs["current_stack"])
    run_actual_apply_steps(case, tmp_path, monkeypatch, False, with_ipc=True)
    assert_produced_transition_and_restoration(case, tmp_path)
    if crossed:
        stdout = tmp_path / "fresh-capture" / "stdout.raw"
        original_stdout = stdout.read_bytes()
        stdout.write_bytes(b"tampered capture stream")
        with pytest.raises(HandoffError):
            maintenance.validate_maintenance_release(packet, Evidence(packet, case.evidence.read))
        stdout.write_bytes(original_stdout)
    repin_log = tmp_path / "operation" / "05-native-repin.log"
    repin_log.write_bytes(b"tampered failed-repin evidence")
    with pytest.raises(HandoffError):
        maintenance.validate_maintenance_release(packet, Evidence(packet, case.evidence.read))
