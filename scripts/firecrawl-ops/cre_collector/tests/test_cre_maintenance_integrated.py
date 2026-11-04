"""Synthetic external boundaries around the actual maintenance producer chain."""

import json
import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_cre_maintenance_chain as chain

import cre_code_handoff as operation
import cre_code_handoff_git as git_module
import cre_refresh_pipeline
import cre_runtime_maintenance as maintenance
import cre_runtime_maintenance_steps as steps
import cre_runtime_maintenance_transition as transition
from cre_code_handoff_evidence import sha

ORIGINAL_POLL_HOOK = cre_refresh_pipeline.command_poll_hook

admitted = chain.admitted
complete_case = chain.complete_case
lineage_case = chain.lineage_case
restoration_case = chain.restoration_case
qualified_ready_case = chain.qualified_ready_case
scheduler_principal_in_synthetic_xml = chain.scheduler_principal_in_synthetic_xml


def run_actual_apply_steps(case, tmp_path, monkeypatch, with_images, *, with_ipc=False, drive=None):
    case.output.rmdir()
    container_state = {"recreated": False}
    if with_images:
        from cre_code_handoff_evidence import Evidence
        from cre_runtime_maintenance_build import reviewed_override

        services = ("playwright-service", "api", "nuq-postgres", "redis", "rabbitmq")
        for service, row in zip(services, case.normalized, strict=True):
            row["service"] = service
        case.packet["expected_images"] = {"api": "sha256:" + "9" * 64}
        case.packet["image_tags"] = {"api": "firecrawl-api:qa-abcdefgh"}
        case.packet["expected_after_config"] = {"api": "2" * 64}
        case.packet["native_docker_sha256"] = "4" * 64
        case.packet["allowed_image_default_changes"] = {"api": []}
        case.packet["projection_output"] = str(tmp_path / "projection")
        original_role = next(row for row in case.inputs.rows if row["role"] == "container_config_before")
        original = json.dumps(case.normalized).encode()
        case.inputs.data[original_role["path"]] = original
        original_role.update(bytes=len(original), sha256=sha(original))
        case.inputs.add("expected_after_config", case.packet["expected_after_config"])
        case.inputs.add("compose_override", reviewed_override(case.packet["image_tags"]))
        case.inputs.add("compose_sources", {"synthetic": True})
        projection = tmp_path / "projection"
        projection.mkdir()
        refs = {}
        for role in ("container_config_before", "expected_after_config", "compose_sources", "compose_override"):
            payload = case.inputs.data[role]
            file = projection / (role + ".bin")
            file.write_bytes(payload)
            refs[role] = {"path": str(file), "sha256": sha(payload), "bytes": len(payload)}
        (projection / "configuration-projection.json").write_text(json.dumps({
            "schema": "cre-image-configuration-projection-v1", "identity": case.new,
            "docker_sha256": case.packet["native_docker_sha256"],
            "changed_image_default_keys": case.packet["allowed_image_default_changes"], **refs}))
        case.packet["inputs"] = case.inputs.rows
        case.release_raw = json.dumps(case.packet).encode()
        case.release.write_bytes(case.release_raw)
        case.before["release_sha256"] = sha(case.release_raw)
        case.observation.write_text(json.dumps(case.before))
        review = case.authorization.parent / "review.json"
        attestation = json.loads(review.read_bytes())
        attestation["subject"]["release_sha256"] = sha(case.release_raw)
        review.write_text(json.dumps(attestation))
        auth = json.loads(case.authorization.read_bytes())
        auth.update(release_sha256=sha(case.release_raw), observation_sha256=sha(case.observation.read_bytes()))
        auth["review"]["sha256"] = sha(review.read_bytes())
        case.authorization.write_text(json.dumps(auth))
        case.evidence = Evidence(case.packet, lambda name: case.inputs.data[name] if name in case.inputs.data else Path(name).read_bytes())
    runtime = tmp_path / "runtime"
    collector = runtime / "scripts/firecrawl-ops/cre_collector"
    (collector / "node_modules").mkdir(parents=True)
    dependencies = {}
    for name in ("package.json", "package-lock.json", "node_modules/.package-lock.json"):
        (collector / name).write_bytes(name.encode())
        dependencies[name] = sha(name.encode())
    original_local = git_module.local_path
    def local(value):
        text = str(value)
        if text == case.packet["roots"]["runtime"]:
            return runtime
        return original_local(value)
    monkeypatch.setattr(git_module, "local_path", local)
    monkeypatch.setattr(steps, "local_path", local)
    monkeypatch.setattr(operation, "local_path", local)
    if "continuation" in case.packet:
        native_path = lambda value: str(value)
    else:
        native_path = lambda value: "C:/synthetic/" + Path(str(value)).name
    monkeypatch.setattr(operation, "windows_path", native_path)
    monkeypatch.setattr(steps, "windows_path", native_path)
    monkeypatch.setattr(operation, "now", lambda: case.now)
    monkeypatch.setattr(steps, "now", lambda: case.now)
    if not with_ipc:
        monkeypatch.setattr(cre_refresh_pipeline, "command_poll_hook", lambda _poll: nullcontext())
    else:
        monkeypatch.setattr(cre_refresh_pipeline, "command_poll_hook", ORIGINAL_POLL_HOOK)
    monkeypatch.setattr(git_module.Git, "maintenance_ancestry", lambda *_args: None)
    monkeypatch.setattr(transition, "validate_maintenance_git_history", lambda *args: None)
    monkeypatch.setattr(transition, "validate_maintenance_dependency_history", lambda *args: None)
    monkeypatch.setattr(transition, "validate_maintenance_tracking_history", lambda *args: None)
    monkeypatch.setattr(maintenance, "validate_prepared_images", lambda *_args: None)
    monkeypatch.setattr(maintenance, "validate_current_projection", lambda *_args: None)
    monkeypatch.setattr(maintenance, "validate_operation_paths", lambda *_args: None)
    monkeypatch.setattr(steps, "verify_native_docker", lambda *_args: None)
    monkeypatch.setattr(steps, "verify_native_compose", lambda *_args: None)
    def observed_containers(*_args):
        rows = [dict(row) for row in case.normalized]
        if with_images and container_state["recreated"]:
            row = next(row for row in rows if row["service"] == "api")
            row.update(id="new-api", image=case.packet["expected_images"]["api"],
                       started_at="new-epoch", config_sha256="2" * 64)
        return rows
    monkeypatch.setattr(steps, "observe_containers", observed_containers)
    monkeypatch.setattr(steps, "_docker", lambda *_args: json.dumps([{"Id": case.packet["expected_images"]["api"]}]).encode())
    monkeypatch.setattr(steps, "installed_dependencies", lambda *_args: dependencies)
    monkeypatch.setattr(steps, "assert_owned", lambda *_args: None)
    monkeypatch.setattr(operation, "assert_owned", lambda *_args, **_kwargs: None)
    if not with_ipc:
        # The legacy fixture uses synthetic relative role paths.
        monkeypatch.setattr(operation, "owned_boundary", lambda *_args, **_kwargs: nullcontext(None))
    else:
        import cre_runtime_maintenance_build as build

        real_boundary = operation.owned_boundary
        def boundary(*args, **kwargs):
            value = real_boundary(*args, **kwargs)
            check = value.check
            def counted(mode):
                check(mode)
                container_state.setdefault("ipc_modes", []).append(mode)
            value.check = counted
            container_state["boundary"] = value
            return value
        monkeypatch.setattr(operation, "owned_boundary", boundary)
        monkeypatch.setattr(operation, "runtime_modules", lambda *_args: None)
        monkeypatch.setattr(operation, "boundary_executables", lambda *_args: ())
        monkeypatch.setattr(operation, "verify_boundary_peer", lambda *_args: True)
        monkeypatch.setattr(build, "verify_native_docker", lambda *_args: None)
        monkeypatch.setattr(build, "verify_native_compose", lambda *_args: None)
    monkeypatch.setattr(operation, "owned_authority", lambda *_args: {
        "pid": os.getpid(), "token": "a" * 32, "generation": "b" * 32, "recovery_required": True,
        "authority_identity": [1, 2], "directory_identity": [1, 3]})
    monkeypatch.setattr(operation, "released_authority", lambda *_args: ([os.getpid(), "a" * 32, "c" * 43, False], [1, 2]))
    monkeypatch.setattr(operation, "lock_clear", lambda *_args, **_kwargs: None)
    if with_images:
        import cre_runtime_maintenance_build

        monkeypatch.setattr(cre_runtime_maintenance_build, "validate_prepared_image_history", lambda *_args: None)
    def readiness(_packet, _modules, _git, *, selected=False, owned=False):
        host = dict(case.before)
        host["containers"] = [dict(row) for row in case.before["containers"]]
        if with_images and selected:
            host["containers"][1].update(id="new-api", image=case.packet["expected_images"]["api"], started_at="new-epoch")
        before_identity = getattr(case, "before_identity", case.old)
        before = getattr(case, "before_identities", {"native": before_identity, "runtime": before_identity})
        return dict(host, native=case.new if selected else before["native"], runtime=case.new if selected else before["runtime"],
                    ownership={"phase": "owned-maintenance" if owned else "released-clear",
                               "verified": True, "recovery_required": owned})
    monkeypatch.setattr(maintenance, "maintenance_readiness", readiness)
    before_identity = getattr(case, "before_identity", case.old)
    before = getattr(case, "before_identities", {"native": before_identity, "runtime": before_identity})
    states = {str(local(case.packet["roots"][key])): before[key] for key in ("native", "runtime")}
    class Git:
        def development(self, *_args): pass
        def maintenance_ancestry(self, *_args): pass
        def entries(self, root, commit): return git_module.Git("unused").entries(root, commit)
        def command(self, root, *args, **kwargs): return git_module.Git("unused").command(root, *args, **kwargs)
        def clean(self, root, identity, **_kwargs): assert states[str(local(root))] == identity
        def maintenance_fast_forward(self, root, _scope, _bundle, **_kwargs): states[str(local(root))] = case.new
        def text(self, root, *args):
            if str(local(root)) not in states:
                return git_module.Git("unused").text(root, *args)
            state = states[str(local(root))]
            if args[0] == "status": return ""
            return state["tree"] if args[-1] == "HEAD^{tree}" else state["sha"]
    monkeypatch.setattr(operation, "Git", lambda *_args: Git())
    original_inputs = case.inputs.data
    sid = "S-1-5-21-111111111-222222222-333333333-1001"
    def ref(path):
        raw = path.read_bytes()
        return {"path": str(path), "sha256": sha(raw), "bytes": len(raw)}
    def command(argv, log, *, env):
        log.write_bytes(b"synthetic settled output\n")
        if "compose" in argv:
            container_state["recreated"] = True
        if "-Mode" not in argv:
            return 0
        mode = argv[argv.index("-Mode") + 1]
        if with_ipc and mode != "Observe":
            boundary = container_state["boundary"]
            count = {"Hold": 3, "Repin": 5, "Verify": 2}[mode]
            program = ("from cre_owned_handoff_boundary import request_boundary;import json,sys;"
                       "b=json.loads(sys.argv[3]);"
                       "[request_boundary(sys.argv[1],int(sys.argv[2]),b,sys.argv[4],lambda:None,lambda:None) "
                       "for _ in range(int(sys.argv[5]))]")
            child = subprocess.Popen([sys.executable, "-c", program, str(boundary.path), str(os.getpid()),
                                      json.dumps(boundary.binding), mode, str(count)])
            if cre_refresh_pipeline.wait_with_poll_hook(child) != 0:
                return 1
        if mode == "Observe":
            output = Path(argv[argv.index("-Output") + 1])
            tasks = []
            for row in case.packet["task_baseline"]:
                raw = original_inputs[case.path(row["xml"])]
                path = output / (row["key"] + "-observed.xml")
                path.write_bytes(raw)
                tasks.append(dict(row, state="Disabled", xml_sha256=sha(raw), xml_reference=ref(path)))
            (output / "native-observation.json").write_text(json.dumps({"observed_at": case.now.isoformat(),
                "tasks": tasks, "windows_resources": case.before["windows_resources"]}))
        if mode == "Hold":
            for row in case.packet["task_baseline"]:
                (case.output / (row["key"] + "-before.xml")).write_bytes(original_inputs[case.path(row["xml"])])
            lower = {"schema": "cre-restoration-watermark-v1", "channel": "Microsoft-Windows-TaskScheduler/Operational",
                     "computer": "synthetic-host", "record_id": 100, "record_sha256": "0" * 64,
                     "captured_at": case.now.isoformat()}
            (case.output / "scheduler-watermark.json").write_text(json.dumps(lower))
            (case.output / "mutation-start.json").write_text(json.dumps({
                "schema": "cre-runtime-maintenance-mutation-v1", "release_sha256": sha(case.release_raw),
                "authorization_sha256": sha(case.authorization.read_bytes()), "operation_output": str(case.output),
                "tooling": case.packet["tooling"], "fleet": case.packet["task_baseline"], "entered_at": case.now.isoformat()}))
        if mode == "Repin":
            for row in case.packet["task_baseline"]:
                before_pin = row.get("pin_before", case.old["sha"])
                raw = original_inputs[case.path(row["xml"])].replace(before_pin.encode(), case.new["sha"].encode())
                (case.output / (row["key"] + "-final.xml")).write_bytes(raw)
        if mode == "Verify":
            tasks = []
            for row in case.packet["task_baseline"]:
                final = case.output / (row["key"] + "-final.xml")
                file = case.output / (row["key"] + "-observed.xml")
                file.write_bytes(final.read_bytes())
                before_pin = row.get("pin_before", case.old["sha"])
                actions = [{k: v.replace(before_pin, case.new["sha"]) for k, v in item.items()} for item in row["actions"]]
                tasks.append(dict(row, state="Disabled", actions=actions, xml_sha256=sha(file.read_bytes()), xml_reference=ref(file)))
            watermark = case.output / "scheduler-watermark.json"
            lower = json.loads(watermark.read_bytes())
            record = {"schema": "cre-restoration-execution-v1", "watermark": lower, "watermark_reference": ref(watermark),
                      "channel": lower["channel"], "computer": lower["computer"], "upper_record_id": 100,
                      "started_at": case.now.isoformat(), "finished_at": case.now.isoformat(), "events": [], "instances": [],
                      "principals": [{"key": row["key"], "user_id": "test-user", "sid": sid,
                                      "aliases": ["test-user", sid, "HOST\\test-user"]} for row in tasks],
                      "observer_sid": sid, "observer_is_admin": False,
                      "tasks": [{k: v for k, v in row.items() if k != "xml_reference"} for row in tasks]}
            execution = case.output / "scheduler-execution.json"
            execution.write_text(json.dumps(record))
            (case.output / "native-observation.json").write_text(json.dumps({"observed_at": case.now.isoformat(),
                "tasks": tasks, "windows_resources": case.before["windows_resources"], "execution": ref(execution)}))
        return 0
    class Lease:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def clear_recovery_requirement(self): pass
    checkpoint = SimpleNamespace(SharedLock=lambda *_args, **_kwargs: Lease(),
                                 canonical_shared_lock_dir=lambda *_args: tmp_path / "lock",
                                 safe_process_env=dict, _run_logged_command=command, COLLECTOR_DIR=collector)
    args = SimpleNamespace(release=str(case.release), maintenance_packet=str(case.release), restoration_packet=None,
                           observation=str(case.observation),
                           authorization=str(case.authorization), output=str(case.output))
    if drive is None:
        maintenance.apply_maintenance(args, case.release_raw, case.packet, case.evidence, Git(), (checkpoint, None, None, None))
    else:
        drive(args, Git(), (checkpoint, None, None, None))
    if with_ipc:
        assert container_state["ipc_modes"] == ["Hold"] * 3 + ["Repin"] * 5 + ["Verify"] * 2
    assert json.loads((case.output / "maintenance-ready.json").read_bytes())["recovery_required_at_publication"] is True
    assert json.loads((case.output / "terminal.json").read_bytes())["status"] == "committed"


@pytest.mark.parametrize("with_images", [False, True])
def test_actual_apply_steps_and_ready_validator_join(qualified_ready_case, tmp_path, monkeypatch, with_images):
    run_actual_apply_steps(qualified_ready_case, tmp_path, monkeypatch, with_images)
