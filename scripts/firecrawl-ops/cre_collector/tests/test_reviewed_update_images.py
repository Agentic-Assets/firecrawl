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
            assert args[:7] == ("-c", "core.autocrlf=false", "-c", "core.eol=lf",
                                "archive", "--format=tar", selected["sha"])
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


def test_archive_overrides_crlf_conversion_and_verifies_real_git_blobs(tmp_path):
    from cre_code_handoff_git import Git
    from cre_runtime_maintenance_build import _verify_archive_tree

    def command(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    command("init", "--initial-branch=main")
    command("config", "user.name", "Fixture")
    command("config", "user.email", "fixture@example.invalid")
    (tmp_path / "apps/api").mkdir(parents=True)
    (tmp_path / ".gitattributes").write_text("* text=auto\n")
    (tmp_path / "apps/api/input.txt").write_bytes(b"first\nsecond\n")
    command("add", ".")
    command("commit", "-m", "fixture")
    commit = command("rev-parse", "HEAD")
    tree = command("rev-parse", "HEAD^{tree}")
    command("config", "core.autocrlf", "true")
    git = Git("/unused/native-git")
    default = git.command(tmp_path, "archive", "--format=tar", commit, "apps/api")
    with pytest.raises(HandoffError, match="archive differs"):
        _verify_archive_tree(git, tmp_path, {"sha": commit, "tree": tree}, "api", default)
    exact = git.command(tmp_path, "-c", "core.autocrlf=false", "-c", "core.eol=lf",
                        "archive", "--format=tar", commit, "apps/api")
    _verify_archive_tree(git, tmp_path, {"sha": commit, "tree": tree}, "api", exact)


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


def test_observe_current_project_retains_exact_file_stack_without_logging_secrets(monkeypatch, tmp_path):
    import cre_reviewed_update_images as direct
    import cre_runtime_maintenance_build as build

    files = ["C:/firecrawl/docker-compose.yaml", "C:/firecrawl/docker-compose.override.yaml",
             "C:/temp/maintenance.yaml"]
    services = ("api", "nuq-postgres", "playwright-service", "redis", "rabbitmq")
    def row(service):
        return {"Id": service, "Image": "image-" + service, "State": {"Status": "running", "StartedAt": "time"},
                "Config": {"Env": ["SECRET=private"], "Labels": {
                    "com.docker.compose.service": service, "com.docker.compose.project": "firecrawl",
                    "com.docker.compose.container-number": "1", "com.docker.compose.depends_on": "redis:service_started:false",
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
        if "image" in argv and "inspect" in argv:
            image_id = "image-new" if argv[-1] in {"fixture-selected", "firecrawl-api:qa-synthetic-02"} else "image-api"
            value = [{"Id": image_id, "Config": {"Env": [], "Labels": {}}}]
        else:
            value = [row(name) for name in services] if "inspect" in argv else {
                "name": "firecrawl", "services": {"api": {"image": "fixture-old", "environment": {}}}}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(value).encode())
    monkeypatch.setattr(direct, "read_file", lambda path: b"binary")
    monkeypatch.setattr(build, "read_file", lambda path: str(path).encode())
    monkeypatch.setattr(direct.subprocess, "run", run)
    result = direct.observe_current_project("C:/docker.exe", "C:/firecrawl", {"api", "playwright-service"})
    assert result["stack"]["files"] == files
    assert [calls[1][i + 1] for i, item in enumerate(calls[1]) if item == "-f"] == files
    assert "private" not in repr(result["snapshots"])
    assert len(result["compose_sha256"]) == 64

    # Exercise the real preparation-to-observation path with the Path object
    # supplied by the coordinator; only the image build is faked.
    selected = {"sha": "a" * 40, "tree": "b" * 40}
    monkeypatch.setattr(direct, "build_current_images", lambda *args, **kwargs: {
        "api": {"tag": "fixture", "image": "image-api",
                "source_sha": selected["sha"], "source_tree": selected["tree"]}})
    plan, overlay = direct.prepare_image_plan(
        None, Path("C:/firecrawl"), selected, {"api"}, "C:/docker.exe", "C:/firecrawl",
        Path("/mnt/c/fixture-override.yaml"), Path("C:/output"))
    assert plan["apply_services"] == [] and overlay is None
    assert plan["override_path"] == "C:/fixture-override.yaml"

    # A changed image must also plan a new overlay using the normalized path.
    monkeypatch.setattr(direct, "build_current_images", lambda *args, **kwargs: {
        "api": {"tag": "fixture-selected", "image": "image-new",
                "source_sha": selected["sha"], "source_tree": selected["tree"]}})
    original_overlay = direct.updated_image_override
    monkeypatch.setattr(direct, "updated_image_override", lambda previous, tags: b"fixture-overlay")
    monkeypatch.setattr(direct, "compare_compose_overlay", lambda *args: None)
    monkeypatch.setattr(direct, "qualify_fleet_projection", lambda *args, **kwargs: {
        "before": [], "expected_after_config": {"api": "1" * 64}})
    absent_path = Path("/mnt/c/Users/CSEAGRAVES2018/AppData/Local/Temp") / (tmp_path.name + "-override.json")
    output = tmp_path / "absent"
    output.mkdir()
    original_windows_path = direct.windows_path
    def windows_path(value):
        if str(value).startswith(str(tmp_path)):
            return "C:/fixture-output/" + Path(value).name
        return original_windows_path(value)
    monkeypatch.setattr(direct, "windows_path", windows_path)
    plan, overlay = direct.prepare_image_plan(
        None, Path("C:/firecrawl"), selected, {"api"}, "C:/docker.exe", "C:/firecrawl", absent_path, output)
    assert plan["apply_services"] == ["api"] and overlay == b"fixture-overlay"
    assert plan["override_path"].startswith("C:/Users/") and plan["previous_override_sha256"] is None

    # The existing, updater-owned overlay is resolved and verified too.
    monkeypatch.setattr(direct, "updated_image_override", original_overlay)
    monkeypatch.setattr(direct, "build_current_images", lambda *args, **kwargs: {
        "api": {"tag": "fixture", "image": "image-api",
                "source_sha": selected["sha"], "source_tree": selected["tree"]}})
    tag = "firecrawl-api:qa-synthetic-01"
    previous = direct.updated_image_override(None, {"api": tag})
    monkeypatch.setattr(direct, "read_file", lambda path:
                        previous if str(path).replace("\\", "/").endswith("/fixture-override.yaml") else b"binary")
    files.append("C:/fixture-override.yaml")
    plan, overlay = direct.prepare_image_plan(
        None, Path("C:/firecrawl"), selected, {"api"}, "C:/docker.exe", "C:/firecrawl",
        Path("/mnt/c/fixture-override.yaml"), Path("C:/output"))
    assert plan["apply_services"] == [] and overlay is None
    assert plan["override_path"] == "C:/fixture-override.yaml"

    monkeypatch.setattr(build, "read_file", lambda path:
                        previous if str(path).replace("\\", "/").endswith("/fixture-override.yaml")
                        else str(path).encode())
    monkeypatch.setattr(direct, "build_current_images", lambda *args, **kwargs: {
        "api": {"tag": "firecrawl-api:qa-synthetic-02", "image": "image-new",
                "source_sha": selected["sha"], "source_tree": selected["tree"]}})
    output = tmp_path / "existing"
    output.mkdir()
    plan, overlay = direct.prepare_image_plan(
        None, Path("C:/firecrawl"), selected, {"api"}, "C:/docker.exe", "C:/firecrawl",
        Path("/mnt/c/fixture-override.yaml"), output)
    assert plan["apply_services"] == ["api"]
    assert plan["previous_override_sha256"] == sha(previous)
    assert json.loads(overlay)["services"]["api"]["image"] == "firecrawl-api:qa-synthetic-02"

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
            operation = json.loads(plan.read_text()).get("mode", "up")
            receipt = {"stage": "ComposeRollback" if operation == "rollback" else "ComposeUp",
                       "plan_sha256": sha(plan.read_bytes()),
                       "owner_pid": os.getpid(), "generation_sha256": sha(b"fixture-generation"),
                       "completed": True, "docker_exit": 0}
            if mode == "failed":
                receipt["docker_exit"] = 7
            elif mode == "wrong-owner":
                receipt["owner_pid"] = os.getpid() + 1
            if mode != "missing":
                path.write_text(json.dumps(receipt))
            return {"success": 0, "failed": 1, "missing": 1, "signal": -15, "wrong-owner": 0}[mode]
        checkpoint = SimpleNamespace(
            _run_logged_command=run,
            run_command=lambda *a, **k: pytest.fail("collector telemetry wrapper used for Compose"),
            safe_process_env=dict,
        )
        return direct.run_current_compose(checkpoint, owner, lease, tmp_path, tmp_path / "control.py",
                                          "a" * 64, plan, tmp_path, tmp_path / "powershell.exe")
    execute("success")
    plan.write_text('{"mode":"rollback"}')
    execute("success")
    plan.write_text("{}")
    with pytest.raises(direct.ComposeCompletedFailure):
        execute("failed")
    for mode in ("missing", "signal", "wrong-owner"):
        with pytest.raises(RuntimeError, match="completion unknown"):
            execute(mode)
    assert len(calls) >= 10


@pytest.mark.parametrize("build_only", [False, True])
def test_prepared_plan_binds_selected_source_and_rechecks_live_state(monkeypatch, tmp_path, build_only):
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
            "containers": [{"Image": "old", "Config": {"Labels": {"com.docker.compose.service": service, "com.docker.compose.container-number": "1", "com.docker.compose.depends_on": "redis:service_started:false"}}}],
            "compose": {"name": "firecrawl", "services": {service: {"image": "old", "environment": {"SECRET": "private"}}}}}
    if build_only:
        live["compose"]["services"][service].pop("image")
        live["compose"]["services"][service]["build"] = {"context": "C:/firecrawl/apps/api", "dockerfile": "Dockerfile"}
    previous_ref = "firecrawl-api" if build_only else "old"
    item = {"tag": "firecrawl-api:qa-synthetic-01", "image": "new",
            "source_sha": selected["sha"], "source_tree": selected["tree"], "context_sha256": "c" * 64}
    current = [copy.deepcopy(live)]
    monkeypatch.setattr(direct, "observe_current_project", lambda *args: copy.deepcopy(current[0]))
    monkeypatch.setattr(direct, "build_current_images", lambda *args, **kwargs: {service: dict(item)})
    monkeypatch.setattr(direct, "windows_path", str)
    monkeypatch.setattr(direct, "local_path", Path)
    def docker_json(executable, native, binary_hash, *args):
        if args[0] == "image":
            return [old_image if args[-1] in {"old", previous_ref} else new_image]
        return {"name": "firecrawl", "services": {}}
    monkeypatch.setattr(direct, "_docker_json", docker_json)
    def snapshot(value):
        assert value["Config"]["Labels"]["com.docker.compose.depends_on"] == ""
        return {"config_sha256": "2" * 64}
    def projection(*args, **kwargs):
        assert kwargs == {"no_deps": True}
        return {"before": [{"service": service}], "expected_after_config": {service: "1" * 64}}
    monkeypatch.setattr(direct, "snapshot_container", snapshot)
    monkeypatch.setattr(direct, "compare_compose_overlay", lambda *args: None)
    monkeypatch.setattr(direct, "qualify_fleet_projection", projection)
    plan, overlay = direct.prepare_image_plan(None, tmp_path, selected, {service}, tmp_path / "docker",
                                              tmp_path, tmp_path / "stable.yaml", tmp_path)
    assert plan["selected"] == selected and plan["override_sha256"] == sha(overlay)
    assert plan["previous_images"] == {"api": {"ref": previous_ref, "image": "old"}}
    assert "private" not in repr(plan)
    direct.recheck_image_plan(plan, selected, tmp_path / "docker", tmp_path)
    def drifted(*args):
        if args[-1] == previous_ref:
            return [{"Id": "different"}]
        return docker_json(*args)
    monkeypatch.setattr(direct, "_docker_json", drifted)
    with pytest.raises(HandoffError, match="previous image reference changed"):
        direct.recheck_image_plan(plan, selected, tmp_path / "docker", tmp_path)
    if build_only:
        # Refuse a generated tag that no longer identifies the running image.
        with pytest.raises(HandoffError, match="previous Compose image reference does not identify"):
            direct.prepare_image_plan(None, tmp_path, selected, {service}, tmp_path / "docker",
                                      tmp_path, tmp_path / "stable.yaml", tmp_path)
    monkeypatch.setattr(direct, "_docker_json", docker_json)
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
                "com.docker.compose.service": "playwright-service", "com.docker.compose.container-number": "1", "com.docker.compose.depends_on": "redis:service_started:false"}}}],
            "compose": {"name": "firecrawl", "services": {"playwright-service": {"image": "old", "environment": {}}}}}
    item = {"tag": "firecrawl-playwright-service:qa-selected-01", "image": "new",
            "source_sha": selected["sha"], "source_tree": selected["tree"], "context_sha256": "c" * 64}
    monkeypatch.setattr(direct, "observe_current_project", lambda *args: copy.deepcopy(live))
    monkeypatch.setattr(direct, "build_current_images", lambda *args, **kwargs: {"playwright-service": dict(item)})
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
    monkeypatch.setattr(direct, "snapshot_container", lambda value: {"config_sha256": "2" * 64})
    monkeypatch.setattr(direct, "compare_compose_overlay", lambda *args: None)
    monkeypatch.setattr(direct, "qualify_fleet_projection", lambda *args, **kwargs: {
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
    plan = {"selected": selected, "services": ["api"], "apply_services": ["api"], "images": {"api": {
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
    plan["services"].append("playwright-service")
    plan["images"]["playwright-service"] = dict(plan["images"]["api"])
    def mixed_observation(docker, native, services, override):
        assert services == {"api"}
        return copy.deepcopy(current[0])
    monkeypatch.setattr(direct, "observe_current_project", mixed_observation)
    direct.verify_image_result(plan, selected, tmp_path / "docker", tmp_path)
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
    monkeypatch.setattr(direct, "_docker_json", lambda *args, **kwargs: {"status": "healthy", "activePages": 0})
    direct.assert_image_idle(tmp_path / "docker", tmp_path, {"api", "playwright-service"}, client, lambda _: counts)
    monkeypatch.setattr(direct, "_docker_json", lambda *args, **kwargs: {"status": "healthy", "activePages": 1})
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


def test_equal_built_image_id_skips_new_override_and_compose(monkeypatch, tmp_path):
    import copy

    import cre_reviewed_update_images as direct

    selected = {"sha": "a" * 40, "tree": "b" * 40}
    image = "sha256:" + "c" * 64
    live = {"docker_sha256": "d" * 64, "nuq_data_sha256": "7" * 64,
            "stack": {"files": ["C:/base.yaml"], "sha256": ["e" * 64]},
            "snapshots": [{"service": "api", "status": "running"}], "compose_sha256": "f" * 64,
            "containers": [{"Image": image, "Config": {"Labels": {"com.docker.compose.service": "api"}}}],
            "compose": {"name": "firecrawl", "services": {"api": {}}}}
    built = {"api": {"tag": "firecrawl-api:qa-synthetic-01", "image": image,
                     "source_sha": selected["sha"], "source_tree": selected["tree"],
                     "context_sha256": "c" * 64}}
    monkeypatch.setattr(direct, "observe_current_project", lambda *args: copy.deepcopy(live))
    monkeypatch.setattr(direct, "build_current_images", lambda *args: copy.deepcopy(built))
    monkeypatch.setattr(direct, "windows_path", str)
    plan, overlay = direct.prepare_image_plan(None, tmp_path, selected, {"api"}, tmp_path / "docker",
                                              tmp_path, tmp_path / "stable.yaml", tmp_path)
    assert plan["apply_services"] == [] and overlay is None
    assert not (tmp_path / "stable.yaml").exists()
    assert not (tmp_path / "compose-preview.yaml").exists()
    monkeypatch.setattr(direct, "_docker_json", lambda *args: [{"Id": image}])
    result = direct.verify_image_result(plan, selected, tmp_path / "docker", tmp_path)
    assert result["images"] == {"unchanged": ["api"], "replaced": []}

def test_restore_override_restores_prior_bytes_or_removes_only_owned_file(monkeypatch, tmp_path):
    import cre_reviewed_update_images as direct

    monkeypatch.setattr(direct, "local_path", Path)
    target = tmp_path / "stable.json"
    backup = tmp_path / "previous-compose-override.json"
    target.write_bytes(b"new")
    backup.write_bytes(b"previous")
    plan = {"override_path": str(target), "override_sha256": sha(b"new"),
            "previous_override_sha256": sha(b"previous")}
    direct.restore_image_override(plan, lambda: None, tmp_path)
    assert target.read_bytes() == b"previous"
    target.write_bytes(b"new")
    backup.write_bytes(b"altered")
    with pytest.raises(HandoffError, match="backup changed"):
        direct.restore_image_override(plan, lambda: None, tmp_path)
    assert target.read_bytes() == b"new"
    target.write_bytes(b"foreign")
    with pytest.raises(HandoffError, match="published Compose override changed"):
        direct.restore_image_override(plan, lambda: None, tmp_path)
    assert target.read_bytes() == b"foreign"
    target.write_bytes(b"new")
    plan["previous_override_sha256"] = None
    direct.restore_image_override(plan, lambda: None, tmp_path)
    assert not target.exists()

def test_selected_readiness_requires_running_api_and_successful_local_queue(monkeypatch):
    import cre_reviewed_update_images as direct

    plan = {"apply_services": ["api"]}
    states = iter([{"containers": {"api": "restarting"}}, {"containers": {"api": "running"}}])
    monkeypatch.setattr(direct, "verify_image_result", lambda *args: next(states))
    monkeypatch.setattr(direct.time, "sleep", lambda seconds: None)
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        def get_json(self, path):
            return {"message": "Firecrawl API"} if path == "/" else {"success": True}
    result = direct.wait_for_selected_images(plan, {}, None, None, Client, lambda: None)
    assert result["containers"]["api"] == "running"


def test_selected_readiness_does_not_hide_configuration_mismatch(monkeypatch):
    import cre_reviewed_update_images as direct

    def mismatch(*args):
        raise HandoffError("Compose configuration differs")
    monkeypatch.setattr(direct, "verify_image_result", mismatch)
    with pytest.raises(direct.SelectedImageUnready, match="transition differs"):
        direct.wait_for_selected_images({"apply_services": ["api"]}, {}, None, None, None, lambda: None)

def test_selected_readiness_timeout_and_owner_loss_are_distinct(monkeypatch):
    import cre_reviewed_update_images as direct

    monkeypatch.setattr(direct, "verify_image_result",
                        lambda *args, **kwargs: {"containers": {"api": "running"}})
    times = iter([0, 31])
    monkeypatch.setattr(direct.time, "monotonic", lambda: next(times))
    class Unavailable:
        def __init__(self, *args, **kwargs):
            pass
        def get_json(self, path):
            raise RuntimeError("unavailable")
    with pytest.raises(HandoffError, match="readiness timed out"):
        direct.wait_for_selected_images({"apply_services": ["api"]}, {}, None, None,
                                        Unavailable, lambda: None)
    monkeypatch.setattr(direct.time, "monotonic", lambda: 0)
    class Ready(Unavailable):
        def get_json(self, path):
            return {"message": "Firecrawl API"} if path == "/" else {"success": True}
    calls = []
    def owner():
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("owner lost")
    with pytest.raises(RuntimeError, match="owner lost"):
        direct.wait_for_selected_images({"apply_services": ["api"]}, {}, None, None, Ready, owner)

def test_selected_browser_readiness_uses_local_health(monkeypatch):
    import cre_reviewed_update_images as direct

    monkeypatch.setattr(direct, "verify_image_result",
                        lambda *args, **kwargs: {"containers": {"playwright-service": "running"}})
    monkeypatch.setattr(direct, "local_path", Path)
    calls = []
    def docker_json(*args):
        calls.append(args)
        return {"status": "healthy", "activePages": 1}
    monkeypatch.setattr(direct, "_docker_json", docker_json)
    result = direct.wait_for_selected_images(
        {"apply_services": ["playwright-service"], "docker_sha256": "a" * 64},
        {}, "/docker", "/native", None, lambda: None)
    assert result["containers"]["playwright-service"] == "running"
    assert "firecrawl-playwright-service-1" in calls[0]

def test_original_image_verification_rejects_unaffected_and_volume_drift(monkeypatch):
    import copy

    import cre_reviewed_update_images as direct

    selected = {"sha": "a" * 40, "tree": "b" * 40}
    before = [{"service": "api", "id": "old", "image": "old-image", "status": "running",
               "config_sha256": "old-config", "restart_policy": "always", "started_at": "old-time"},
              {"service": "nuq-postgres", "id": "nuq", "status": "running"}]
    restored = [{"service": "api", "id": "restored", "image": "old-image", "status": "running",
                 "config_sha256": "restored-config", "restart_policy": "always", "started_at": "new-time"},
                {"service": "nuq-postgres", "id": "nuq", "status": "running"}]
    plan = {"selected": selected, "services": ["api"], "apply_services": ["api"],
            "before": before, "docker_sha256": "a", "compose_stack": {"files": ["base"]},
            "nuq_data_sha256": "volume", "compose_sha256": "config",
            "previous_images": {"api": {"ref": "old-ref", "image": "old-image"}},
            "rollback_config": {"api": "restored-config"}, "override_path": "C:/override.yaml"}
    observed = {"snapshots": restored, "docker_sha256": "a", "stack": {"files": ["base"]},
                "nuq_data_sha256": "volume", "compose_sha256": "config"}
    monkeypatch.setattr(direct, "local_path", Path)
    monkeypatch.setattr(direct, "observe_current_project", lambda *args: copy.deepcopy(observed))
    monkeypatch.setattr(direct, "_docker_json", lambda *args: [{"Id": "old-image"}])
    assert direct.verify_original_images(plan, selected, "/docker", "/native")["restored"] == ["api"]
    observed["snapshots"][1]["id"] = "other"
    with pytest.raises(HandoffError, match="unaffected container changed"):
        direct.verify_original_images(plan, selected, "/docker", "/native")
    observed["snapshots"][1]["id"] = "nuq"
    observed["nuq_data_sha256"] = "other"
    with pytest.raises(HandoffError, match="original Compose configuration differs"):
        direct.verify_original_images(plan, selected, "/docker", "/native")

def test_partial_fleet_allows_missing_affected_but_rejects_unrelated_drift(monkeypatch):
    import copy

    import cre_reviewed_update_images as direct

    binary = b"binary"
    before = [{"service": name, "id": name, "image": "old", "status": "running",
               "restart_policy": "always", "config_sha256": "old-config"}
              for name in ("api", "playwright-service", "nuq-postgres", "redis", "rabbitmq")]
    plan = {"before": before, "apply_services": ["api"], "docker_sha256": sha(binary),
            "nuq_data_sha256": "volume", "previous_images": {"api": {"ref": "prior", "image": "old"}},
            "images": {"api": {"image": "new"}}, "expected_after_config": {"api": "new-config"}}
    rows = [dict(row, Name="/firecrawl-" + row["service"] + "-1",
                 Config={"Labels": {"com.docker.compose.project": "firecrawl",
                                    "com.docker.compose.service": row["service"]}})
            for row in before if row["service"] != "api"]
    monkeypatch.setattr(direct, "local_path", Path)
    monkeypatch.setattr(direct, "read_file", lambda path: binary)
    monkeypatch.setattr(direct, "snapshot_container", lambda row: {k: v for k, v in row.items() if k not in {"Config", "Name"}})
    monkeypatch.setattr(direct, "nuq_data_identity", lambda rows: "volume")
    monkeypatch.setattr(direct, "expected_compose_stack", lambda plan: [])
    monkeypatch.setattr(direct.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0, stdout=b"playwright-service nuq-postgres redis rabbitmq"))
    monkeypatch.setattr(direct, "_docker_json",
                        lambda *args: copy.deepcopy(rows) if "inspect" in args and "image" not in args else [{"Id": "old"}])
    direct.qualify_settled_partial_fleet(plan, "/docker", "/native", lambda: None)
    rows[0]["status"] = "exited"
    with pytest.raises(HandoffError, match="unrelated container changed"):
        direct.qualify_settled_partial_fleet(plan, "/docker", "/native", lambda: None)


def test_docker_observation_classifies_only_daemon_disconnect_or_timeout(monkeypatch, tmp_path):
    import cre_reviewed_update_images as direct

    binary = tmp_path / "docker"
    binary.write_bytes(b"docker")
    digest = sha(binary.read_bytes())
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, stdout=b"", stderr=stderr[0])
    monkeypatch.setattr(direct.subprocess, "run", run)
    stderr = [b"Cannot connect to the Docker daemon at npipe:local. Is the docker daemon running?"]
    with pytest.raises(direct.DockerObservationUnavailable):
        direct._docker_json(binary, tmp_path, digest, "inspect", "api")
    stderr[0] = b"Error: No such object: api"
    with pytest.raises(HandoffError, match="Docker observation failed") as failure:
        direct._docker_json(binary, tmp_path, digest, "inspect", "api")
    assert not isinstance(failure.value, direct.DockerObservationUnavailable)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 60)
    monkeypatch.setattr(direct.subprocess, "run", timeout)
    with pytest.raises(direct.DockerObservationUnavailable):
        direct._docker_json(binary, tmp_path, digest, "inspect", "api")
    binary.write_bytes(b"changed")
    with pytest.raises(HandoffError, match="executable changed"):
        direct._docker_json(binary, tmp_path, digest, "inspect", "api")


def test_readiness_retries_only_unavailable_observation(monkeypatch):
    import cre_reviewed_update_images as direct

    plan = {"apply_services": ["api"]}
    attempts = []
    def observe(*args):
        attempts.append(True)
        if len(attempts) == 1:
            raise direct.DockerObservationUnavailable("daemon unavailable")
        return {"containers": {"api": "running"}}
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        def get_json(self, path):
            return {"message": "Firecrawl API"} if path == "/" else {"success": True}
    monkeypatch.setattr(direct, "verify_image_result", observe)
    monkeypatch.setattr(direct.time, "sleep", lambda seconds: None)
    assert direct.wait_for_selected_images(plan, {}, None, None, Client, lambda: None)["containers"]["api"] == "running"
    assert len(attempts) == 2
    monkeypatch.setattr(direct, "verify_image_result", lambda *args: (_ for _ in ()).throw(
        direct.DockerObservationUnavailable("daemon unavailable")))
    times = iter([0, 31])
    monkeypatch.setattr(direct.time, "monotonic", lambda: next(times))
    with pytest.raises(direct.SelectedImageUnready, match="observation timed out"):
        direct.wait_for_selected_images(plan, {}, None, None, Client, lambda: None)
    monkeypatch.setattr(direct.time, "monotonic", lambda: 0)
    calls = []
    def owner():
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("owner lost")
    with pytest.raises(RuntimeError, match="owner lost"):
        direct.wait_for_selected_images(plan, {}, None, None, Client, owner)


def test_docker_observation_malformed_output_and_timeout_binary_change_fail(monkeypatch, tmp_path):
    import cre_reviewed_update_images as direct

    binary = tmp_path / "docker"
    binary.write_bytes(b"docker")
    digest = sha(binary.read_bytes())
    monkeypatch.setattr(direct.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0, stdout=b"not json", stderr=b""))
    with pytest.raises(json.JSONDecodeError):
        direct._docker_json(binary, tmp_path, digest, "inspect", "api")
    def timeout(*args, **kwargs):
        binary.write_bytes(b"changed")
        raise subprocess.TimeoutExpired(args[0], 60)
    monkeypatch.setattr(direct.subprocess, "run", timeout)
    with pytest.raises(HandoffError, match="executable changed"):
        direct._docker_json(binary, tmp_path, digest, "inspect", "api")


def test_original_observation_timeout_is_not_a_degraded_success(monkeypatch):
    import cre_reviewed_update_images as direct

    times = iter([0, 31])
    monkeypatch.setattr(direct.time, "monotonic", lambda: next(times))
    def unavailable():
        raise direct.DockerObservationUnavailable("daemon unavailable")
    with pytest.raises(direct.DockerObservationUnavailable):
        direct.wait_for_service_readiness({"apply_services": ["api"]}, None, None, None,
                                          lambda: None, unavailable)


def test_browser_hard_docker_error_propagates_and_unhealthy_retries(monkeypatch):
    import cre_reviewed_update_images as direct

    plan = {"apply_services": ["playwright-service"], "docker_sha256": "a" * 64}
    observed = {"containers": {"playwright-service": "running"}}
    monkeypatch.setattr(direct, "local_path", Path)
    monkeypatch.setattr(direct.time, "sleep", lambda seconds: None)
    def hard_error(*args):
        raise HandoffError("Docker executable changed")
    monkeypatch.setattr(direct, "_docker_json", hard_error)
    with pytest.raises(HandoffError, match="Docker executable changed"):
        direct.wait_for_service_readiness(plan, "/docker", "/native", None, lambda: None, lambda: observed)
    values = iter([{"status": "unhealthy"}, {"status": "healthy"}])
    monkeypatch.setattr(direct, "_docker_json", lambda *args: next(values))
    assert direct.wait_for_service_readiness(plan, "/docker", "/native", None, lambda: None,
                                             lambda: observed) == observed


def test_browser_daemon_unavailable_never_becomes_degraded_success(monkeypatch):
    import cre_reviewed_update_images as direct

    plan = {"apply_services": ["playwright-service"], "docker_sha256": "a" * 64}
    monkeypatch.setattr(direct, "local_path", Path)
    times = iter([0, 31])
    monkeypatch.setattr(direct.time, "monotonic", lambda: next(times))
    def unavailable(*args):
        raise direct.DockerObservationUnavailable("daemon unavailable")
    monkeypatch.setattr(direct, "_docker_json", unavailable)
    with pytest.raises(direct.DockerObservationUnavailable):
        direct.wait_for_service_readiness(plan, "/docker", "/native", None, lambda: None,
                                          lambda: {"containers": {"playwright-service": "running"}})
