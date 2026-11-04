"""Owned maintenance order and refusal tests; no host commands or live services."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cre_code_handoff_evidence import HandoffError


@pytest.fixture
def steps_case(tmp_path, monkeypatch):
    try:
        steps = importlib.import_module("cre_runtime_maintenance_steps")
    except ModuleNotFoundError:
        pytest.fail("owned maintenance step producer is missing")
    import cre_runtime_maintenance as admission

    old = {"sha": "a" * 40, "tree": "d" * 40}
    middle = {"sha": "b" * 40, "tree": "e" * 40}
    new = {"sha": "c" * 40, "tree": "f" * 40}
    runtime = tmp_path / "runtime"
    collector = runtime / "scripts/firecrawl-ops/cre_collector"
    collector.mkdir(parents=True)
    out = tmp_path / "operation"
    out.mkdir()
    projection = tmp_path / "projection"
    projection.mkdir()
    (projection / "configuration-projection.json").write_text("{}")
    for name in ("release", "observation", "authorization"):
        (tmp_path / name).write_text("{}")
    events = []
    state = {"fail": None, "recreated": False, "before_drift": False, "host_drift": False,
             "bad_marker": False, "docker_changed": False, "compose_changed": False}
    roots = {"native": "C:/runtime", "runtime": str(runtime)}
    packet = {"schema": "cre-runtime-maintenance-v1", "old": old, "native_before": middle,
              "new": new, "roots": roots, "install_scope": ["collector"],
              "tooling": new, "task_baseline": [{"key": key} for key in ("interra", "newmark", "qualified")],
              "programs": {"native_docker": "C:/Program Files/Docker/Docker/resources/bin/docker.exe"},
              "expected_images": {"api": "sha256:" + "9" * 64}, "image_tags": {"api": "qa:reviewed"},
              "expected_after_config": {"api": "2" * 64}, "projection_output": str(projection)}
    for role, before in (("native", middle), ("runtime", old)):
        packet[role + "_change"] = {"deployed": before, "selected": new, "allowed_paths": [],
                                    "objects": {}, "changed_dependency_paths": []}
    packet["runtime_change"]["changed_dependency_paths"] = ["scripts/firecrawl-ops/cre_collector/package.json"]
    args = SimpleNamespace(release=str(tmp_path / "release"), observation=str(tmp_path / "observation"),
                           authorization=str(tmp_path / "authorization"))

    class Git:
        def __init__(self):
            self.states = {str(steps.local_path(roots["native"])): middle, str(runtime): old}

        def clean(self, root, identity, **kwargs):
            assert self.states[str(steps.local_path(root))] == identity

        def text(self, root, *argv):
            current = self.states[str(steps.local_path(root))]
            if argv[0] == "status":
                return ""
            return current["tree"] if argv[-1] == "HEAD^{tree}" else current["sha"]

        def maintenance_fast_forward(self, root, scope, bundle, *, allowed_dependency_paths):
            events.append("git:" + ("native" if str(root).startswith("C:") else "runtime"))
            self.states[str(steps.local_path(root))] = new

    class Runner:
        def __init__(self, *unused):
            self.counter = 0

        def run(self, name, argv, **kwargs):
            self.counter += 1
            events.append(name)
            log = out / f"{self.counter:02d}-{name}.log"
            log.write_text("synthetic settled output\n")
            log.with_suffix(".json").write_text(json.dumps({"argv": argv, "exit_code": 0}))
            if state["fail"] == name:
                raise HandoffError("injected " + name)
            if name == "compose-up":
                state["recreated"] = True

        def native(self, mode, *unused, **kwargs):
            self.run("native-" + mode.lower(), ["mock-native", mode])
            if mode == "Hold":
                (out / "mutation-start.json").write_text(json.dumps({
                    "schema": "cre-runtime-maintenance-mutation-v1", "release_sha256": "0" * 64 if state["bad_marker"] else steps.sha(b"{}"),
                    "authorization_sha256": steps.sha(b"{}"), "operation_output": steps.canonical_path(str(out)),
                    "tooling": packet["tooling"], "fleet": packet["task_baseline"],
                }))
            if mode == "Verify":
                (out / "native-observation.json").write_text(json.dumps({
                    "observed_at": "2026-11-02T20:00:00Z", "tasks": [], "windows_resources": {},
                    "execution": {"fixture": True},
                }))

    class Evidence:
        def __init__(self):
            self.inputs = {"bundle": {"path": str(tmp_path / "bundle")},
                           "compose_override": {"path": "C:/private/reviewed-override.yaml"}}

        def recheck(self):
            events.append("recheck")

        def obj(self, role):
            assert role == "container_config_before"
            return container_rows(False)

    def container_rows(selected):
        rows = [{"service": name, "id": "old-" + name, "image": "old-image-" + name,
                 "started_at": "old-epoch", "config_sha256": "1" * 64,
                 "restart_policy": "unless-stopped", "status": "running"}
                for name in ("api", "playwright-service", "nuq-postgres", "redis", "rabbitmq")]
        if selected:
            rows[0].update(id="new-api", image=packet["expected_images"]["api"],
                           started_at="new-epoch", config_sha256="2" * 64)
        return rows

    def snapshot(docker):
        rows = container_rows(state["recreated"])
        if state["before_drift"]:
            rows[0]["config_sha256"] = "3" * 64
        return rows

    def docker(executable, argv):
        assert str(executable).endswith("docker.exe")
        assert argv == ["image", "inspect", "qa:reviewed"]
        return json.dumps([{"Id": packet["expected_images"]["api"]}]).encode()

    def readied(*unused, **kwargs):
        events.append("selected-readiness")
        rows = container_rows(True)
        if state["host_drift"]:
            rows[0]["id"] = "foreign"
        return {"containers": rows}

    def verify_docker(unused):
        if state["docker_changed"]:
            raise HandoffError("native Docker executable changed")

    def verify_compose(unused):
        if state["compose_changed"]:
            raise HandoffError("native Compose plugin changed")

    monkeypatch.setattr(steps, "Foreground", Runner)
    monkeypatch.setattr(steps, "assert_owned", lambda *a: events.append("owner"))
    monkeypatch.setattr(steps, "installed_dependencies", lambda *a: {"fixture": "locked"})
    monkeypatch.setattr(steps, "observe_containers", snapshot)
    monkeypatch.setattr(steps, "_docker", docker)
    monkeypatch.setattr(steps, "verify_native_docker", verify_docker, raising=False)
    monkeypatch.setattr(steps, "verify_native_compose", verify_compose, raising=False)
    monkeypatch.setattr(steps, "qualify_image_transition", lambda *a, **kw: events.append("images-qualified"))
    monkeypatch.setattr(admission, "maintenance_readiness", readied)
    monkeypatch.setattr(admission, "validate_maintenance_observation", lambda *a, **kw: events.append("selected-qualified"))
    checkpoint = SimpleNamespace(canonical_shared_lock_dir=lambda root: tmp_path / "lock")
    return SimpleNamespace(steps=steps, args=args, packet=packet, git=Git(), evidence=Evidence(),
                           modules=(checkpoint, None, None, None), lock=tmp_path / "lock", out=out,
                           events=events, state=state)


def invoke(case):
    return case.steps.run_maintenance_steps(case.args, b"{}", case.packet, case.evidence, case.git,
                                           case.modules, case.lock, case.out, lambda: case.events.append("admitted"))


def test_owned_steps_hold_then_update_verify_and_leave_jobs_held(steps_case):
    case = steps_case
    ready = invoke(case)
    mutations = [event for event in case.events if event.startswith(("native-", "git:", "npm-", "compose-"))]
    assert mutations.index("native-hold") < mutations.index("git:native") < mutations.index("git:runtime")
    assert mutations.index("npm-ci") < mutations.index("compose-up") < mutations.index("native-repin") < mutations.index("native-verify")
    assert all("restore" not in event and "enable" not in event for event in mutations)
    assert ready["schema"] == "cre-runtime-maintenance-ready-v1"
    assert ready["recovery_required_at_publication"] is True
    commands = [json.loads(Path(item["receipt"]["path"]).read_bytes())["argv"] for item in ready["commands"]]
    compose = next(argv for argv in commands if "compose" in argv)
    assert compose[1:3] == ["--host", "npipe:////./pipe/dockerDesktopLinuxEngine"]
    assert compose[compose.index("--project-name") + 1] == "firecrawl"
    assert compose[compose.index("--project-directory") + 1] == "C:/runtime"
    assert [compose[index + 1] for index, value in enumerate(compose) if value == "-f"] == [
        "C:/runtime/docker-compose.yaml", "C:/runtime/docker-compose.override.yaml", "C:/private/reviewed-override.yaml",
    ]
    assert "--no-deps" in compose and "--no-build" in compose and compose[-1] == "api"


@pytest.mark.parametrize("failure", ["npm-ci", "compose-up"])
def test_failed_mutation_never_repins_or_publishes_ready(steps_case, failure):
    case = steps_case
    case.state["fail"] = failure
    with pytest.raises(HandoffError, match="injected " + failure):
        invoke(case)
    assert "native-repin" not in case.events and "native-verify" not in case.events
    assert not (case.out / "maintenance-ready.json").exists()


def test_configuration_drift_after_admission_refuses_before_git_or_install(steps_case):
    case = steps_case
    case.state["before_drift"] = True
    with pytest.raises(HandoffError, match="prepared container configuration"):
        invoke(case)
    assert not any(event.startswith("git:") for event in case.events)
    assert "npm-ci" not in case.events


def test_selected_host_observation_must_match_actual_recreated_containers(steps_case):
    case = steps_case
    case.state["host_drift"] = True
    with pytest.raises(HandoffError, match="selected container readback"):
        invoke(case)
    assert not (case.out / "maintenance-ready.json").exists()


def test_changed_release_refuses_before_native_or_runtime_mutation(steps_case):
    case = steps_case
    Path(case.args.release).write_text('{"changed":true}')
    with pytest.raises(HandoffError, match="release changed during execution"):
        invoke(case)
    assert "native-hold" not in case.events
    assert not any(event.startswith("git:") for event in case.events)


def test_native_mutation_marker_must_bind_exact_operation_before_git(steps_case):
    case = steps_case
    case.state["bad_marker"] = True
    with pytest.raises(HandoffError, match="native mutation context differs"):
        invoke(case)
    assert not any(event.startswith("git:") for event in case.events)


def test_read_only_docker_uses_the_same_pinned_engine_as_projection(monkeypatch):
    steps = importlib.import_module("cre_runtime_maintenance_steps")
    observed = []

    def run(argv, **kwargs):
        observed.append(argv)
        return SimpleNamespace(returncode=0, stdout=b"[]")

    monkeypatch.setattr(steps.subprocess, "run", run)
    steps._docker("C:/Program Files/Docker/Docker/resources/bin/docker.exe", ["image", "inspect", "qa:reviewed"])
    assert observed[0][1:] == ["--host", "npipe:////./pipe/dockerDesktopLinuxEngine", "image", "inspect", "qa:reviewed"]


def test_changed_docker_executable_refuses_before_any_owned_step(steps_case):
    case = steps_case
    case.state["docker_changed"] = True
    with pytest.raises(HandoffError, match="native Docker executable changed"):
        invoke(case)
    assert "native-hold" not in case.events
    assert not any(event.startswith("git:") for event in case.events)


def test_changed_compose_plugin_refuses_before_any_owned_step(steps_case):
    case = steps_case
    case.state["compose_changed"] = True
    with pytest.raises(HandoffError, match="native Compose plugin changed"):
        invoke(case)
    assert "native-hold" not in case.events
    assert not any(event.startswith("git:") for event in case.events)
