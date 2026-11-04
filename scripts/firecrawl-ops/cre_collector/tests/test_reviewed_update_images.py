"""Focused fixtures for the direct reviewed image path."""

import json
import os
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from cre_code_handoff_evidence import HandoffError, sha


def test_build_current_images_binds_selected_context_and_refuses_unknown(monkeypatch, tmp_path):
    import cre_reviewed_update_images as build

    selected = {"sha": "a" * 40, "tree": "b" * 40}
    class FakeGit:
        def __init__(self):
            self.checks = 0
        def clean(self, root, identity, *, tracking):
            assert identity == selected and tracking == selected["sha"]
            self.checks += 1
        def command(self, root, *args):
            assert args[:3] == ("archive", "--format=tar", selected["sha"])
            return b"reviewed archive"
    git = FakeGit()
    monkeypatch.setattr(build, "read_file", lambda path: b"docker binary")
    monkeypatch.setattr(build, "_verify_archive_tree", lambda *args: None)
    monkeypatch.setattr(build, "_context_tar", lambda archive, service: ("context-" + service).encode())
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if "build" in argv:
            assert kwargs["input"] == b"context-api"
            return subprocess.CompletedProcess(argv, 0)
        assert argv[-3:-1] == ["image", "inspect"]
        if kwargs.get("stdout") == subprocess.DEVNULL:
            return subprocess.CompletedProcess(argv, 1)
        value = [{"Id": "sha256:" + "c" * 64, "Config": {"Labels": {
            "org.opencontainers.image.revision": selected["sha"]}}}]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(value).encode())
    monkeypatch.setattr(build.subprocess, "run", run)
    compose = {"services": {"api": {"build": {"context": "C:/firecrawl/apps/api", "dockerfile": "Dockerfile"}}}}
    result = build.build_current_images(git, tmp_path, selected, {"api"}, "C:/docker.exe",
                                        "C:/firecrawl", compose, tmp_path)
    assert result["api"]["image"] == "sha256:" + "c" * 64
    assert result["api"]["context_sha256"] == sha(b"context-api")
    assert result["api"]["source_sha"] == selected["sha"]
    assert git.checks == 2
    assert len(calls) == 3 and "--build-arg" in calls[1]
    with pytest.raises(HandoffError, match="NuQ"):
        build.build_current_images(git, tmp_path, selected, {"nuq-postgres"}, "C:/docker.exe",
                                   "C:/firecrawl", compose, tmp_path)
    compose["services"]["api"]["build"]["args"] = {"UNREVIEWED": "value"}
    with pytest.raises(HandoffError, match="build configuration"):
        build.build_current_images(git, tmp_path, selected, {"api"}, "C:/docker.exe",
                                   "C:/firecrawl", compose, tmp_path)


def test_build_current_images_failure_stops_before_inspection(monkeypatch, tmp_path):
    import cre_reviewed_update_images as build

    class Git:
        def clean(self, *args, **kwargs): pass
        def command(self, *args): return b"archive"
    monkeypatch.setattr(build, "read_file", lambda path: b"docker binary")
    monkeypatch.setattr(build, "_verify_archive_tree", lambda *args: None)
    monkeypatch.setattr(build, "_context_tar", lambda *args: b"context")
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1)
    monkeypatch.setattr(build.subprocess, "run", run)
    selected = {"sha": "a" * 40, "tree": "b" * 40}
    compose = {"services": {"api": {"build": {"context": "C:/firecrawl/apps/api", "dockerfile": "Dockerfile"}}}}
    with pytest.raises(HandoffError, match="build failed"):
        build.build_current_images(Git(), tmp_path, selected, {"api"}, "C:/docker.exe",
                                   "C:/firecrawl", compose, tmp_path)
    assert len(calls) == 2


def test_observe_current_project_retains_exact_file_stack_without_logging_secrets(monkeypatch):
    import cre_reviewed_update_images as direct
    import cre_runtime_maintenance_build as build

    files = ["C:/firecrawl/docker-compose.yaml", "C:/firecrawl/docker-compose.override.yaml",
             "C:/temp/maintenance.yaml"]
    services = ("api", "nuq-postgres", "playwright-service", "redis", "rabbitmq")
    def row(service):
        return {"Id": service, "Image": "image-" + service, "State": {"Status": "running", "StartedAt": "time"},
                "Config": {"Env": ["SECRET=private"], "Labels": {
                    "com.docker.compose.service": service, "com.docker.compose.project": "firecrawl",
                    "com.docker.compose.project.working_dir": "C:/firecrawl",
                    "com.docker.compose.project.config_files": ",".join(files)},
                    "User": "", "Entrypoint": None, "Cmd": None, "WorkingDir": ""},
                "HostConfig": {"RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
                               "Memory": 0, "NanoCpus": 0, "ShmSize": 0, "ReadonlyRootfs": False,
                               "Privileged": False, "SecurityOpt": None, "CapAdd": None, "CapDrop": None,
                               "PortBindings": {}, "NetworkMode": "bridge"},
                "Mounts": ([{"Type": "volume", "Name": "volume-name", "Source": "/private/data",
                             "Destination": "/var/lib/postgresql/data", "RW": True, "Propagation": ""}]
                           if service == "nuq-postgres" else []),
                "NetworkSettings": {"Networks": {}}}
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        value = [row(name) for name in services] if "inspect" in argv else {"name": "firecrawl", "services": {}}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(value).encode())
    monkeypatch.setattr(direct, "read_file", lambda path: b"binary")
    monkeypatch.setattr(build, "read_file", lambda path: str(path).encode())
    monkeypatch.setattr(direct.subprocess, "run", run)
    result = direct.observe_current_project("C:/docker.exe", "C:/firecrawl", {"api", "playwright-service"})
    assert result["stack"]["files"] == files
    assert [calls[1][i + 1] for i, item in enumerate(calls[1]) if item == "-f"] == files
    assert "private" not in repr(result["snapshots"])
    assert len(result["compose_sha256"]) == 64
    with pytest.raises(HandoffError, match="NuQ"):
        direct.observe_current_project("C:/docker.exe", "C:/firecrawl", {"nuq-postgres"})


def test_native_compose_completion_requires_matching_receipt(monkeypatch, tmp_path):
    import cre_reviewed_update_images as direct

    plan = tmp_path / "plan.json"
    plan.write_text('{"selected":"fixture"}')
    calls = []
    lease = SimpleNamespace(authority_generation="fixture-generation")
    monkeypatch.setattr(direct, "windows_path", str)
    monkeypatch.setattr(direct, "command_poll_hook", lambda poll: nullcontext())
    def owner():
        calls.append("owner")
    def execute(mode):
        def run(argv, log, env):
            path = Path(argv[argv.index("-Receipt") + 1])
            receipt = {"stage": "ComposeUp", "plan_sha256": sha(plan.read_bytes()),
                       "owner_pid": os.getpid(), "generation_sha256": sha(b"fixture-generation"),
                       "completed": True, "docker_exit": 0}
            if mode == "failed":
                receipt["docker_exit"] = 7
            elif mode == "wrong-owner":
                receipt["owner_pid"] = os.getpid() + 1
            if mode != "missing":
                path.write_text(json.dumps(receipt))
            return {"success": 0, "failed": 1, "missing": 1, "signal": -15, "wrong-owner": 0}[mode]
        checkpoint = SimpleNamespace(run_command=run, safe_process_env=dict)
        return direct.run_current_compose(checkpoint, owner, lease, tmp_path, tmp_path / "control.py",
                                          "a" * 64, plan, tmp_path, tmp_path / "powershell.exe")
    execute("success")
    with pytest.raises(direct.ComposeCompletedFailure):
        execute("failed")
    for mode in ("missing", "signal", "wrong-owner"):
        with pytest.raises(RuntimeError, match="completion unknown"):
            execute(mode)
    assert len(calls) >= 10


def test_prepared_plan_binds_selected_source_and_rechecks_live_state(monkeypatch, tmp_path):
    import copy

    import cre_reviewed_update_images as direct

    selected = {"sha": "a" * 40, "tree": "b" * 40}
    service = "api"
    old_image = {"Id": "old", "Config": {"Env": ["VERSION=old"], "Labels": {}}}
    new_image = {"Id": "new", "Config": {"Env": ["VERSION=new"], "Labels": {
        "org.opencontainers.image.revision": selected["sha"]}}}
    live = {"docker_sha256": "d" * 64, "nuq_data_sha256": "7" * 64,
            "stack": {"files": ["C:/base.yaml"], "sha256": ["e" * 64]},
            "snapshots": [{"service": service}], "compose_sha256": "f" * 64,
            "containers": [{"Image": "old", "Config": {"Labels": {"com.docker.compose.service": service}}}],
            "compose": {"name": "firecrawl", "services": {service: {"environment": {"SECRET": "private"}}}}}
    item = {"tag": "firecrawl-api:qa-synthetic-01", "image": "new",
            "source_sha": selected["sha"], "source_tree": selected["tree"], "context_sha256": "c" * 64}
    current = [copy.deepcopy(live)]
    monkeypatch.setattr(direct, "observe_current_project", lambda *args: copy.deepcopy(current[0]))
    monkeypatch.setattr(direct, "build_current_images", lambda *args: {service: dict(item)})
    monkeypatch.setattr(direct, "windows_path", str)
    monkeypatch.setattr(direct, "local_path", Path)
    def docker_json(executable, native, binary_hash, *args):
        if args[0] == "image":
            return [old_image if args[-1] == "old" else new_image]
        return {"name": "firecrawl", "services": {}}
    monkeypatch.setattr(direct, "_docker_json", docker_json)
    monkeypatch.setattr(direct, "compare_compose_overlay", lambda *args: None)
    monkeypatch.setattr(direct, "qualify_fleet_projection", lambda *args: {
        "before": [{"service": service}], "expected_after_config": {service: "1" * 64}})
    plan, overlay = direct.prepare_image_plan(None, tmp_path, selected, {service}, tmp_path / "docker",
                                              tmp_path, tmp_path / "stable.yaml", tmp_path)
    assert plan["selected"] == selected and plan["override_sha256"] == sha(overlay)
    assert "private" not in repr(plan)
    direct.recheck_image_plan(plan, selected, tmp_path / "docker", tmp_path)
    wrong = {"sha": "9" * 40, "tree": selected["tree"]}
    with pytest.raises(HandoffError, match="selected source"):
        direct.recheck_image_plan(plan, wrong, tmp_path / "docker", tmp_path)
    current[0]["stack"]["sha256"][0] = "9" * 64
    with pytest.raises(HandoffError, match="changed before apply"):
        direct.recheck_image_plan(plan, selected, tmp_path / "docker", tmp_path)


def test_stable_override_preserves_unmodified_service_and_refuses_foreign_content():
    import cre_reviewed_update_images as direct

    api = "firecrawl-api:qa-first-selected"
    browser = "firecrawl-playwright-service:qa-second-selected"
    original = direct.updated_image_override(None, {"api": api})
    updated = direct.updated_image_override(original, {"playwright-service": browser})
    value = json.loads(updated)
    assert value["services"]["api"] == {"image": api, "restart": "unless-stopped"}
    assert value["services"]["playwright-service"]["image"] == browser
    with pytest.raises(HandoffError):
        direct.updated_image_override(b'{"services":{"api":{"environment":{"X":"Y"}}}}',
                                      {"playwright-service": browser})
    with pytest.raises(HandoffError):
        direct.updated_image_override(b'{"services":{"nuq-postgres":{"image":"x","restart":"unless-stopped"}}}',
                                      {"api": api})


def test_prepared_plan_substitutes_stable_overlay_in_preview(monkeypatch, tmp_path):
    import copy

    import cre_reviewed_update_images as direct

    selected = {"sha": "a" * 40, "tree": "b" * 40}
    stable = tmp_path / "stable.yaml"
    previous = direct.updated_image_override(None, {"api": "firecrawl-api:qa-previous-01"})
    stable.write_bytes(previous)
    live = {"docker_sha256": "d" * 64, "nuq_data_sha256": "7" * 64,
            "stack": {"files": ["/tmp/base.yaml", str(stable)], "sha256": ["e" * 64, sha(previous)]},
            "snapshots": [{"service": "playwright-service"}], "compose_sha256": "f" * 64,
            "containers": [{"Image": "old", "Config": {"Labels": {
                "com.docker.compose.service": "playwright-service"}}}],
            "compose": {"name": "firecrawl", "services": {"playwright-service": {"environment": {}}}}}
    item = {"tag": "firecrawl-playwright-service:qa-selected-01", "image": "new",
            "source_sha": selected["sha"], "source_tree": selected["tree"], "context_sha256": "c" * 64}
    monkeypatch.setattr(direct, "observe_current_project", lambda *args: copy.deepcopy(live))
    monkeypatch.setattr(direct, "build_current_images", lambda *args: {"playwright-service": dict(item)})
    monkeypatch.setattr(direct, "windows_path", str)
    monkeypatch.setattr(direct, "local_path", Path)
    calls = []
    def docker_json(executable, native, binary_hash, *args):
        calls.append(args)
        if args[0] == "image":
            return [{"Id": args[-1] if args[-1] == "old" else "new",
                     "Config": {"Env": [], "Labels": {"org.opencontainers.image.revision": selected["sha"]}}}]
        return {"name": "firecrawl", "services": {}}
    monkeypatch.setattr(direct, "_docker_json", docker_json)
    monkeypatch.setattr(direct, "compare_compose_overlay", lambda *args: None)
    monkeypatch.setattr(direct, "qualify_fleet_projection", lambda *args: {
        "before": [{"service": "playwright-service"}], "expected_after_config": {"playwright-service": "1" * 64}})
    plan, overlay = direct.prepare_image_plan(None, tmp_path, selected, {"playwright-service"},
                                              tmp_path / "docker", tmp_path, stable, tmp_path)
    preview_args = calls[-1]
    assert str(stable) not in preview_args
    assert str(tmp_path / "compose-preview.yaml") in preview_args
    assert json.loads(overlay)["services"]["api"]["image"] == "firecrawl-api:qa-previous-01"
    assert plan["previous_override_sha256"] == sha(previous)


def test_install_override_preserves_prior_bytes_and_checks_owner(monkeypatch, tmp_path):
    import cre_reviewed_update_images as direct

    monkeypatch.setattr(direct, "local_path", Path)
    target = tmp_path / "stable.json"
    target.write_bytes(b"previous")
    plan = {"override_path": str(target), "override_sha256": sha(b"new"),
            "previous_override_sha256": sha(b"previous")}
    calls = []
    direct.install_image_override(plan, b"new", lambda: calls.append("owner"), tmp_path)
    assert target.read_bytes() == b"new"
    assert (tmp_path / "previous-compose-override.json").read_bytes() == b"previous"
    assert len(calls) >= 4


def test_install_override_rejects_collision_and_owner_loss(monkeypatch, tmp_path):
    import cre_reviewed_update_images as direct

    monkeypatch.setattr(direct, "local_path", Path)
    target = tmp_path / "stable.json"
    plan = {"override_path": str(target), "override_sha256": sha(b"new"),
            "previous_override_sha256": None}
    target.write_bytes(b"unowned")
    with pytest.raises(HandoffError, match="unowned"):
        direct.install_image_override(plan, b"new", lambda: None, tmp_path)
    assert target.read_bytes() == b"unowned"
    target.unlink()
    def owner():
        raise RuntimeError("owner lost")
    with pytest.raises(RuntimeError, match="owner lost"):
        direct.install_image_override(plan, b"new", owner, tmp_path)
    assert not target.exists()


def test_verify_image_result_binds_selected_stack_and_image(monkeypatch, tmp_path):
    import copy

    import cre_reviewed_update_images as direct

    selected = {"sha": "a" * 40, "tree": "b" * 40}
    plan = {"selected": selected, "services": ["api"], "images": {"api": {
                "source_sha": selected["sha"], "source_tree": selected["tree"], "image": "new", "tag": "tag"}},
            "compose_stack": {"files": ["C:/base.yaml"], "sha256": ["c" * 64]},
            "override_path": "C:/override.yaml", "override_sha256": "d" * 64,
            "docker_sha256": "e" * 64, "nuq_data_sha256": "7" * 64, "projected_compose_sha256": "f" * 64,
            "before": [{"service": "api"}], "expected_after_config": {"api": "1" * 64}}
    observation = {"docker_sha256": "e" * 64, "nuq_data_sha256": "7" * 64, "stack": {
        "files": ["C:/base.yaml", "C:/override.yaml"], "sha256": ["c" * 64, "d" * 64]},
        "compose_sha256": "f" * 64, "snapshots": [{"service": "api", "status": "running"}]}
    current = [copy.deepcopy(observation)]
    monkeypatch.setattr(direct, "observe_current_project", lambda *args: copy.deepcopy(current[0]))
    monkeypatch.setattr(direct, "_docker_json", lambda *args: [{"Id": "new"}])
    monkeypatch.setattr(direct, "qualify_current_image_transition", lambda *args, **kwargs: {
        "unchanged": [], "replaced": ["api"]})
    result = direct.verify_image_result(plan, selected, tmp_path / "docker", tmp_path)
    assert result["images"]["replaced"] == ["api"]
    wrong = {"sha": "9" * 40, "tree": selected["tree"]}
    with pytest.raises(HandoffError, match="selected source"):
        direct.verify_image_result(plan, wrong, tmp_path / "docker", tmp_path)
    current[0]["stack"]["sha256"][-1] = "9" * 64
    with pytest.raises(HandoffError, match="configuration differs"):
        direct.verify_image_result(plan, selected, tmp_path / "docker", tmp_path)


def test_image_idle_uses_queue_and_browser_quiescence(monkeypatch, tmp_path):
    import cre_reviewed_update_images as direct

    counts = dict.fromkeys(("jobs_in_queue", "active_jobs_in_queue", "waiting_jobs_in_queue", "active_crawl_count"), 0)
    client = lambda *args, **kwargs: object()
    monkeypatch.setattr(direct, "read_file", lambda path: b"docker")
    monkeypatch.setattr(direct, "_docker_json", lambda *args: {"status": "healthy", "activePages": 0})
    direct.assert_image_idle(tmp_path / "docker", tmp_path, {"api", "playwright-service"}, client, lambda _: counts)
    monkeypatch.setattr(direct, "_docker_json", lambda *args: {"status": "healthy", "activePages": 1})
    with pytest.raises(HandoffError, match="Playwright"):
        direct.assert_image_idle(tmp_path / "docker", tmp_path, {"playwright-service"}, client, lambda _: counts)
    counts["jobs_in_queue"] = 1
    with pytest.raises(HandoffError, match="queues"):
        direct.assert_image_idle(tmp_path / "docker", tmp_path, {"api"}, client, lambda _: counts)


def test_nuq_data_identity_includes_volume_name_without_revealing_it():
    import cre_reviewed_update_images as direct

    row = {"Config": {"Labels": {"com.docker.compose.service": "nuq-postgres"}},
           "Mounts": [{"Type": "volume", "Name": "name-one", "Source": "/private/data",
                       "Destination": "/var/lib/postgresql/data", "RW": True, "Propagation": ""}]}
    first = direct.nuq_data_identity([row])
    row["Mounts"][0]["Name"] = "name-two"
    assert direct.nuq_data_identity([row]) != first
    assert "private" not in first
    row["Mounts"][0]["RW"] = False
    with pytest.raises(HandoffError):
        direct.nuq_data_identity([row])
