"""Admission keeps original failed-A evidence while supporting mixed checkout SHAs."""

import copy
from datetime import timedelta
from pathlib import Path

import pytest
import test_cre_code_handoff as handoff_fixtures
import test_cre_code_handoff_restoration as restoration_fixtures

from cre_code_handoff_evidence import HandoffError

restoration_case = restoration_fixtures.restoration_case
complete_case = restoration_fixtures.complete_case
lineage_case = restoration_fixtures.lineage_case
admitted = restoration_fixtures.admitted


def validate(packet, evidence):
    try:
        from cre_runtime_maintenance import validate_historical_admission
    except ImportError:
        pytest.fail("maintenance admission is missing")
    return validate_historical_admission(packet, evidence)


@pytest.fixture
def maintenance_case(restoration_case):
    packet, evidence, inputs, put, path = restoration_case
    inputs.add("historical_runtime_snapshot", inputs.data[path("runtime_snapshot")])
    old = copy.deepcopy(packet["code"])
    packet["programs"]["native_docker"] = "C:/Docker/docker.exe"
    packet.update(schema="cre-runtime-maintenance-v1", scope="held-runtime-maintenance",
                  old=old, new={"sha": "e" * 40, "tree": "f" * 40}, native_before=old)
    packet["build_source"] = {"root": "/home/cayman/synthetic-build-source", "identity": packet["new"]}
    failed_ref = next(row for row in inputs.rows if row["role"] == packet["failed_handoff"]["release"])
    packet["failed_handoff_release"] = {key: failed_ref[key] for key in ("path", "sha256", "bytes")}
    packet.pop("code")
    evidence = inputs.evidence(packet)
    return packet, evidence, inputs, put, path


@pytest.mark.parametrize("native", ["old", "new", "intermediate"])
def test_maintenance_admits_original_failed_a_and_both_expected_native_states(maintenance_case, native):
    packet, evidence, _, _, _ = maintenance_case
    packet["native_before"] = ({"sha": "1" * 40, "tree": "2" * 40} if native == "intermediate" else packet[native])
    validate(packet, evidence)


def test_maintenance_refuses_malformed_native_identity_before_operation(maintenance_case):
    packet, evidence, _, _, _ = maintenance_case
    packet["native_before"] = {"sha": "invalid", "tree": "2" * 40}
    with pytest.raises(HandoffError):
        validate(packet, evidence)


def test_maintenance_does_not_reclassify_original_failed_a(maintenance_case):
    packet, _, inputs, _, path = maintenance_case
    import json
    role = packet["failed_handoff"]["failure"]
    saved = json.loads(inputs.data[path(role)])
    saved["classification"] = "committed"
    inputs.data[path(role)] = json.dumps(saved).encode()
    with pytest.raises(HandoffError):
        validate(packet, inputs.evidence(packet))


def test_fresh_pre_mutation_observation_requires_exact_mixed_roots_and_held_tasks(maintenance_case):
    try:
        from cre_runtime_maintenance import validate_maintenance_observation
    except ImportError:
        pytest.fail("maintenance observation validator is missing")
    packet, evidence, inputs, _, path = maintenance_case
    packet["native_before"] = packet["new"]
    now = restoration_fixtures.NOW
    observation = handoff_fixtures.host_observation()
    observation.update(schema="cre-runtime-maintenance-observation-v1", native=packet["new"],
                       runtime=packet["old"], observed_at=now.isoformat(), qualified_at=now.isoformat(),
                       ownership={"phase": "released-clear", "verified": True, "recovery_required": False},
                       resources_ok=True, queues_idle=True,
                       tasks=[dict(row, state="Disabled", xml_sha256=handoff_fixtures.sha(inputs.data[path(row["xml"])]))
                              for row in packet["task_baseline"]])
    validate_maintenance_observation(packet, evidence, observation, now)
    stale = copy.deepcopy(observation)
    stale["observed_at"] = (now - timedelta(seconds=300)).isoformat()
    with pytest.raises(HandoffError):
        validate_maintenance_observation(packet, evidence, stale, now)
    observation["tasks"][0]["enabled"] = True
    with pytest.raises(HandoffError):
        validate_maintenance_observation(packet, evidence, observation, now)


def test_authentication_binds_review_observation_and_exclusive_output(maintenance_case, tmp_path):
    try:
        from cre_runtime_maintenance import validate_maintenance_authorization
    except ImportError:
        pytest.fail("maintenance authorization validator is missing")
    import json
    packet, evidence, inputs, _, path = maintenance_case
    evidence.read = lambda value: inputs.data[value] if value in inputs.data else Path(value).read_bytes()
    now = restoration_fixtures.NOW
    observation = handoff_fixtures.host_observation()
    observation.update(schema="cre-runtime-maintenance-observation-v1", native=packet["native_before"],
                       runtime=packet["old"], observed_at=now.isoformat(), qualified_at=now.isoformat(),
                       ownership={"phase": "released-clear", "verified": True, "recovery_required": False},
                       resources_ok=True, queues_idle=True,
                       tasks=[dict(row, state="Disabled", xml_sha256=handoff_fixtures.sha(inputs.data[path(row["xml"])]))
                              for row in packet["task_baseline"]])
    release_raw = json.dumps(packet).encode()
    observation["release_sha256"] = handoff_fixtures.sha(release_raw)
    observed_raw = json.dumps(observation).encode()
    body = tmp_path / "review.md"
    body.write_bytes(b"reviewed synthetic maintenance")
    subject = {"release_sha256": handoff_fixtures.sha(release_raw), "tooling": packet["tooling"]}
    review = tmp_path / "review.json"
    review.write_text(json.dumps(handoff_fixtures.attestation(subject, body, body.read_bytes(), scope="runtime-maintenance")))
    output = str(tmp_path / "exclusive-operation")
    auth = {"schema": "cre-runtime-maintenance-authorization-v1", "scope": "held-runtime-maintenance",
            "release_sha256": handoff_fixtures.sha(release_raw), "observation_sha256": handoff_fixtures.sha(observed_raw),
            "authorized_at": now.isoformat(), "expires_at": (now + timedelta(seconds=299)).isoformat(),
            "operation_output": output, "review": {"path": str(review), "sha256": handoff_fixtures.sha(review.read_bytes()),
                                                     "reviewer": "independent", "approval_ref": "AGENTIC-test"}}
    validate_maintenance_authorization(release_raw, packet, evidence, observed_raw, auth, now,
                                       output=output, review_reader=lambda p: Path(p).read_bytes())
    with pytest.raises(HandoffError):
        validate_maintenance_authorization(release_raw, packet, evidence, observed_raw, auth, now,
                                           output=output + "-other", review_reader=lambda p: Path(p).read_bytes())
    auth["review"]["reviewer"] = "different-reviewer"
    with pytest.raises(HandoffError):
        validate_maintenance_authorization(release_raw, packet, evidence, observed_raw, auth, now,
                                           output=output, review_reader=lambda p: Path(p).read_bytes())
    auth["review"]["reviewer"] = "independent"
    observation["release_sha256"] = "0" * 64
    forged = json.dumps(observation).encode()
    auth["observation_sha256"] = handoff_fixtures.sha(forged)
    with pytest.raises(HandoffError):
        validate_maintenance_authorization(release_raw, packet, evidence, forged, auth, now,
                                           output=output, review_reader=lambda p: Path(p).read_bytes())


def test_maintenance_preview_uses_explicit_selector_without_host_dispatch(maintenance_case, monkeypatch, capsys):
    import json

    import cre_code_handoff as operation
    packet, evidence, _, _, _ = maintenance_case
    raw = json.dumps(packet).encode()
    monkeypatch.setattr(operation, "load_release", lambda _: (raw, packet, evidence))
    monkeypatch.setattr(operation, "runtime_modules", lambda *_: pytest.fail("preview dispatched to host"))
    assert operation.main(["--maintenance-packet", "/synthetic/packet.json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["preview_only"] is True and result["execution"] is False and result["selected"] == packet["new"]


def test_owner_command_must_bind_exact_maintenance_inputs():
    try:
        from cre_code_handoff import validate_owner_command
    except ImportError:
        pytest.fail("maintenance owner command validation missing")
    script = "/tooling/cre_code_handoff.py"
    expected = {"--maintenance-packet": "/packet", "--output": "/operation",
                "--observation": "/observation", "--authorization": "/authorization"}
    command = [b"python3", script.encode(), b"--apply"]
    for flag, value in expected.items():
        command += [flag.encode(), value.encode()]
    validate_owner_command(command, script, maintenance_inputs=expected)
    with pytest.raises(HandoffError):
        validate_owner_command(command + [b"--output", b"/other"], script, maintenance_inputs=expected)
    with pytest.raises(HandoffError):
        validate_owner_command(command, script, maintenance_inputs={**expected, "--output": "/other"})


def test_release_refuses_unreviewed_dependency_install_scope(maintenance_case):
    try:
        from cre_runtime_maintenance import validate_maintenance_release
    except ImportError:
        pytest.fail("maintenance release validation missing")
    packet, evidence, _, _, _ = maintenance_case
    packet["install_scope"] = ["arbitrary/network-command"]
    with pytest.raises(HandoffError):
        validate_maintenance_release(packet, evidence)


def test_release_refuses_missing_native_docker_program(maintenance_case):
    from cre_runtime_maintenance import validate_historical_admission

    packet, evidence, _, _, _ = maintenance_case
    del packet["programs"]["native_docker"]
    with pytest.raises(HandoffError):
        validate_historical_admission(packet, evidence)


def test_reviewed_release_qualifies_actual_qa_and_preserved_failed_a(maintenance_case):
    import json

    from cre_runtime_maintenance import validate_maintenance_release

    packet, _, inputs, _, _ = maintenance_case
    path = "scripts/firecrawl-ops/cre_collector/fixture.py"
    packet.update(new={"sha": "e" * 40, "tree": "f" * 40}, install_scope=[], expected_images={})
    packet["build_source"]["identity"] = packet["new"]
    packet["qa"]["final_executed"] = packet["new"]
    packet["qa"]["gates"][0]["executed"] = packet["new"]
    for role, mutate in (("gate", lambda value: value.update(identity=packet["new"])),
                         ("qa-attestation", lambda value: value["subject"].update(qa_identity=packet["new"]))):
        value = json.loads(inputs.data[role])
        mutate(value)
        payload = json.dumps(value).encode()
        inputs.data[role] = payload
        row = next(row for row in inputs.rows if row["role"] == role)
        row.update(bytes=len(payload), sha256=handoff_fixtures.sha(payload))
    for key, old in (("native", packet["native_before"]), ("runtime", packet["old"])):
        packet[key + "_change"] = {"deployed": old, "selected": packet["new"], "allowed_paths": [path],
                                   "objects": {path: {"before": None, "after": {"mode": "100644", "type": "blob", "oid": "e" * 40}}},
                                   "changed_dependency_paths": []}
        inputs.add(key + "_allowed_paths_nul", path.encode() + b"\0")
    validate_maintenance_release(packet, inputs.evidence(packet))


def test_selected_observation_requires_actual_produced_snapshot(maintenance_case):
    from cre_runtime_maintenance import validate_maintenance_observation

    packet, evidence, inputs, put, path = maintenance_case
    now = restoration_fixtures.NOW
    tasks = []
    for row in packet["task_baseline"]:
        raw = inputs.data[path(row["xml"])].replace(packet["old"]["sha"].encode(), packet["new"]["sha"].encode())
        role = put("selected-" + row["key"], raw)
        reference = {"path": path(role), "sha256": handoff_fixtures.sha(raw), "bytes": len(raw)}
        actions = [{k: v.replace(packet["old"]["sha"], packet["new"]["sha"]) for k, v in action.items()}
                   for action in row["actions"]]
        tasks.append(dict(row, actions=actions, state="Disabled", xml_sha256=reference["sha256"], xml_reference=reference))
    observation = handoff_fixtures.host_observation()
    observation.update(schema="cre-runtime-maintenance-observation-v1", native=packet["new"], runtime=packet["new"],
                       observed_at=now.isoformat(), qualified_at=now.isoformat(),
                       ownership={"phase": "owned-maintenance", "verified": True, "recovery_required": True},
                       resources_ok=True, queues_idle=True, tasks=tasks)
    snapshot = {"containers": observation["containers"]}
    evidence = inputs.evidence(packet)
    validate_maintenance_observation(packet, evidence, observation, now, selected=True, owned=True,
                                     selected_snapshot=snapshot)
    with pytest.raises(HandoffError):
        validate_maintenance_observation(packet, evidence, observation, now, selected=True, owned=True)


def test_restarted_containers_use_fresh_snapshot_without_rewriting_failed_a(maintenance_case):
    import json

    packet, _, inputs, _, _ = maintenance_case
    old_role = next(row for row in inputs.rows if row["role"] == "runtime_snapshot")
    original_raw = inputs.data[old_role["path"]]
    assert inputs.data["historical_runtime_snapshot"] == original_raw
    changed = json.loads(original_raw)
    for row in changed["containers"]:
        row["started_at"] = "2026-11-02T20:09:01Z"
    fresh_raw = json.dumps(changed).encode()
    old_role.update(path="fresh-runtime-snapshot", bytes=len(fresh_raw), sha256=handoff_fixtures.sha(fresh_raw))
    inputs.data["fresh-runtime-snapshot"] = fresh_raw
    validate(packet, inputs.evidence(packet))
    assert inputs.data["historical_runtime_snapshot"] == original_raw


def test_observe_publishes_fresh_read_only_admission(maintenance_case, tmp_path, monkeypatch):
    import json

    import cre_code_handoff as operation

    packet, evidence, inputs, _, path = maintenance_case
    raw = json.dumps(packet).encode()
    now = restoration_fixtures.NOW
    host = handoff_fixtures.host_observation()
    host.update(native=packet["native_before"], runtime=packet["old"],
                ownership={"phase": "released-clear", "verified": True, "recovery_required": False},
                resources_ok=True, queues_idle=True)
    tasks = [dict(row, state="Disabled", xml_sha256=handoff_fixtures.sha(inputs.data[path(row["xml"])]))
             for row in packet["task_baseline"]]
    def native(self, mode, *args, **kwargs):
        assert mode == "Observe"
        operation.write_new(self.output / "native-observation.json", {
            "observed_at": now.isoformat(), "tasks": tasks, "windows_resources": host["windows_resources"]})
    monkeypatch.setattr(operation, "load_release", lambda _: (raw, packet, evidence))
    monkeypatch.setattr(operation, "runtime_modules", lambda *_: (object(), None, None, None))
    monkeypatch.setattr(operation.Foreground, "native", native)
    monkeypatch.setattr(operation, "now", lambda: now)
    import cre_runtime_maintenance as maintenance
    monkeypatch.setattr(maintenance, "maintenance_readiness", lambda *_args, **_kwargs: host, raising=False)
    output = tmp_path / "fresh-observation"
    assert operation.main(["--maintenance-packet", "/packet", "--observe", "--output", str(output)]) == 0
    observed = json.loads((output / "observation.json").read_text())
    assert observed["native"] == packet["native_before"] and observed["runtime"] == packet["old"]
    assert all(task["state"] == "Disabled" for task in observed["tasks"])


def test_authorize_publishes_one_bound_input_and_refuses_replay(maintenance_case, tmp_path, monkeypatch):
    import json

    import cre_code_handoff as operation

    packet, evidence, inputs, _, path = maintenance_case
    evidence.read = lambda value: inputs.data[value] if value in inputs.data else Path(value).read_bytes()
    for role, row in evidence.inputs.items():
        actual = tmp_path / ("input-" + role)
        actual.write_bytes(evidence.original[role])
        inputs.data[str(actual)] = evidence.original[role]
        row["path"] = str(actual)
    for original_path, payload in list(evidence.original_payloads.items()):
        if not original_path.startswith(("/", "C:/")):
            actual = tmp_path / ("payload-" + handoff_fixtures.sha(original_path.encode()))
            actual.write_bytes(payload)
            del evidence.original_payloads[original_path]
            evidence.original_payloads[str(actual)] = payload
    now = restoration_fixtures.NOW
    raw = json.dumps(packet).encode()
    release = tmp_path / "release.json"
    release.write_bytes(raw)
    observation = handoff_fixtures.host_observation()
    observation.update(schema="cre-runtime-maintenance-observation-v1", native=packet["native_before"],
                       runtime=packet["old"], observed_at=now.isoformat(), qualified_at=now.isoformat(),
                       ownership={"phase": "released-clear", "verified": True, "recovery_required": False},
                       resources_ok=True, queues_idle=True,
                       tasks=[dict(row, state="Disabled", xml_sha256=handoff_fixtures.sha(inputs.data[path(row["xml"])]))
                              for row in packet["task_baseline"]])
    observation["release_sha256"] = handoff_fixtures.sha(raw)
    observed = tmp_path / "observation.json"
    observed.write_text(json.dumps(observation))
    body = tmp_path / "review.md"
    body.write_bytes(b"synthetic review")
    review = tmp_path / "review.json"
    review.write_text(json.dumps(handoff_fixtures.attestation(
        {"release_sha256": handoff_fixtures.sha(raw), "tooling": packet["tooling"]},
        body, body.read_bytes(), scope="runtime-maintenance")))
    output, auth = tmp_path / "operation", tmp_path / "authorization.json"
    monkeypatch.setattr(operation, "load_release", lambda _: (raw, packet, evidence))
    monkeypatch.setattr(operation, "now", lambda: now)
    argv = ["--maintenance-packet", str(release), "--authorize", "--observation", str(observed),
            "--authorization", str(auth), "--review", str(review), "--reviewer", "independent",
            "--approval-ref", "AGENTIC-test", "--output", str(output)]
    assert operation.main(argv) == 0
    assert json.loads(auth.read_text())["operation_output"] == str(output)
    with pytest.raises(FileExistsError):
        operation.main(argv)


def test_owned_dependency_command_binds_maintenance_selected_identity(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    import cre_code_handoff as operation
    import cre_refresh_pipeline

    runtime = tmp_path / "runtime"
    collector = runtime / "scripts/firecrawl-ops/cre_collector"
    collector.mkdir(parents=True)
    selected = {"sha": "a" * 40, "tree": "b" * 40}
    packet = {"schema": "cre-runtime-maintenance-v1", "roots": {"runtime": str(runtime)},
              "new": selected, "programs": {"native_git": "/git"}}
    identities = []
    monkeypatch.setattr(operation, "Git", lambda *_: SimpleNamespace(clean=lambda _root, identity, **_: identities.append(identity)))
    monkeypatch.setattr(cre_refresh_pipeline, "command_poll_hook", lambda _poll: nullcontext())
    def command(_argv, log, *, env):
        log.write_text("synthetic dependency output")
        return 0
    checkpoint = SimpleNamespace(run_command=command, safe_process_env=dict, COLLECTOR_DIR=collector)
    runner = operation.Foreground(packet, checkpoint, tmp_path)
    receipt = runner.run("npm-ci", ["npm", "ci"], effective_cwd=collector)
    assert receipt["identity"] == selected and identities == [selected, selected]


def test_postreadback_rejects_missing_original_terminal_before_native_observation(maintenance_case, tmp_path, monkeypatch):
    import json

    import cre_code_handoff as operation

    packet, evidence, _, _, _ = maintenance_case
    monkeypatch.setattr(operation, "load_release", lambda _: (json.dumps(packet).encode(), packet, evidence))
    monkeypatch.setattr(operation, "runtime_modules", lambda *_: (object(), None, None, None))
    monkeypatch.setattr(operation.Foreground, "native", lambda *_args, **_kwargs: pytest.fail("native call before terminal"))
    with pytest.raises((HandoffError, FileNotFoundError), match="terminal"):
        operation.main(["--maintenance-packet", "/packet", "--postreadback",
                        "--operation", str(tmp_path / "missing"), "--output", str(tmp_path / "observation")])


def test_apply_selector_dispatches_only_with_bound_observation_and_authorization(maintenance_case, tmp_path, monkeypatch):
    import json

    import cre_code_handoff as operation
    import cre_runtime_maintenance as maintenance

    packet, evidence, _, _, _ = maintenance_case
    raw = json.dumps(packet).encode()
    monkeypatch.setattr(operation, "load_release", lambda _: (raw, packet, evidence))
    monkeypatch.setattr(operation, "runtime_modules", lambda *_: (object(), None, None, None))
    seen = []
    monkeypatch.setattr(maintenance, "apply_maintenance", lambda args, *_: seen.append((args.observation, args.authorization)))
    base = ["--maintenance-packet", "/packet", "--apply", "--output", str(tmp_path / "operation")]
    with pytest.raises(HandoffError):
        operation.main(base)
    assert seen == []
    assert operation.main(base + ["--observation", "/observation", "--authorization", "/authorization"]) == 0
    assert seen == [("/observation", "/authorization")]
