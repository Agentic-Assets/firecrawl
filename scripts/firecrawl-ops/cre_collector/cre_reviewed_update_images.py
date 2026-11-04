"""Direct selected-source image preparation for the reviewed updater."""

import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

from cre_code_handoff_evidence import (
    HandoffError,
    canonical_path,
    identity,
    json_bytes,
    require,
    sha,
)
from cre_code_handoff_git import local_path, read_file, windows_path
from cre_refresh_pipeline import command_poll_hook
from cre_runtime_maintenance_build import (
    ENGINE,
    SERVICES,
    _context_tar,
    _verify_archive_tree,
    compare_compose_overlay,
    current_compose_files,
    expected_overlay_config_files,
    qualify_fleet_projection,
    reviewed_override,
)
from cre_runtime_maintenance_images import (
    qualify_current_image_transition,
    snapshot_container,
)


def _docker_environment():
    environment = dict(os.environ)
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_TLS"):
        environment.pop(name, None)
    return environment


def _docker_json(executable, native, binary_hash, *args):
    require(sha(read_file(executable)) == binary_hash, "Docker executable changed")
    result = subprocess.run([str(executable), "--host", ENGINE, *args],
                            cwd=native, env=_docker_environment(), capture_output=True, timeout=60, check=False)
    require(result.returncode == 0 and len(result.stdout) <= 8 * 1024 * 1024
            and sha(read_file(executable)) == binary_hash, "Docker observation failed or changed")
    return json_bytes(result.stdout)


def nuq_data_identity(containers):
    """Bind the unchanged NuQ data mount, including its Docker volume name."""
    matches = [row for row in containers if isinstance(row, dict)
               and row.get("Config", {}).get("Labels", {}).get("com.docker.compose.service") == "nuq-postgres"]
    require(len(matches) == 1 and isinstance(matches[0].get("Mounts"), list), "NuQ data observation missing")
    mounts = [mount for mount in matches[0]["Mounts"]
              if isinstance(mount, dict) and mount.get("Destination") == "/var/lib/postgresql/data"]
    require(len(mounts) == 1 and mounts[0].get("Type") in {"volume", "bind"}
            and isinstance(mounts[0].get("Source"), str) and mounts[0]["Source"]
            and mounts[0].get("RW") is True, "NuQ data mount is uncertain")
    mount = mounts[0]
    if mount["Type"] == "volume":
        require(isinstance(mount.get("Name"), str) and mount["Name"], "NuQ volume name missing")
    value = {key: mount.get(key) for key in ("Type", "Name", "Source", "Destination", "RW", "Propagation")}
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def observe_current_project(docker, native_root, services):
    """Capture current Docker/Compose state in memory without exposing env values."""
    require(isinstance(services, set) and services and services <= {"api", "playwright-service"},
            "NuQ or unknown image update is not supported")
    executable, native = map(local_path, (docker, native_root))
    binary_hash = sha(read_file(executable))
    names = ["firecrawl-api-1", "firecrawl-nuq-postgres-1", "firecrawl-playwright-service-1",
             "firecrawl-redis-1", "firecrawl-rabbitmq-1"]
    containers = _docker_json(executable, native, binary_hash, "inspect", *names)
    require(isinstance(containers, list) and len(containers) == len(names), "container inventory differs")
    snapshots = [snapshot_container(value) for value in containers]
    require({row["service"] for row in snapshots}
            == {"api", "nuq-postgres", "playwright-service", "redis", "rabbitmq"},
            "container service inventory differs")
    stack = current_compose_files(containers, services, windows_path(native))
    argv = ["compose", "--project-name", "firecrawl", "--project-directory", windows_path(native)]
    for path in stack["files"]:
        argv += ["-f", windows_path(local_path(path))]
    compose = _docker_json(executable, native, binary_hash, *argv, "config", "--format", "json")
    require(isinstance(compose, dict) and compose.get("name") == "firecrawl",
            "resolved Compose project differs")
    encoded = json.dumps(compose, sort_keys=True, separators=(",", ":")).encode()
    return {"docker_sha256": binary_hash, "stack": stack, "snapshots": snapshots,
            "nuq_data_sha256": nuq_data_identity(containers),
            "compose_sha256": sha(encoded), "containers": containers, "compose": compose}


def build_current_images(git, source, selected, services, docker, native_root, compose, output):
    """Build uniquely tagged images from the reviewed tree before holding tasks."""
    require(isinstance(services, set) and services and services <= {"api", "playwright-service"},
            "NuQ or unknown image build is not supported")
    identity(selected)
    repo, native, executable, output = map(local_path, (source, native_root, docker, output))
    require(output.is_dir() and not output.is_symlink(), "private build output missing")
    git.clean(repo, selected, tracking=selected["sha"])
    binary_hash = sha(read_file(executable))
    environment = _docker_environment()
    result = {}
    for service in sorted(services):
        service_config = compose.get("services", {}).get(service) if isinstance(compose, dict) else None
        build = service_config.get("build") if isinstance(service_config, dict) else None
        expected_context = canonical_path(windows_path(native / SERVICES[service])).casefold()
        require(isinstance(build, dict) and set(build) == {"context", "dockerfile"}
                and canonical_path(build["context"]).casefold() == expected_context
                and build["dockerfile"] == "Dockerfile", "unsupported Compose build configuration")
        archive = git.command(repo, "archive", "--format=tar", selected["sha"], SERVICES[service])
        _verify_archive_tree(git, repo, selected, service, archive)
        context = _context_tar(archive, service)
        tag = "firecrawl-" + service + ":qa-reviewed-" + secrets.token_hex(12)
        existing = subprocess.run([str(executable), "--host", ENGINE, "image", "inspect", tag],
                                  cwd=native, env=environment, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=60, check=False)
        require(existing.returncode != 0 and sha(read_file(executable)) == binary_hash,
                "reviewed image tag already exists or Docker changed")
        command = [str(executable), "--host", ENGINE, "build", "--progress", "plain", "--label",
                   "org.opencontainers.image.revision=" + selected["sha"]]
        if service == "api":
            command += ["--build-arg", "GIT_SHA=" + selected["sha"]]
        command += ["-t", tag, "-"]
        require(sha(read_file(executable)) == binary_hash, "Docker executable changed")
        log = output / (service + "-build.log")
        with log.open("xb") as stream:
            built = subprocess.run(command, cwd=native, env=environment, input=context,
                                   stdout=stream, stderr=subprocess.STDOUT, timeout=3600, check=False)
        require(sha(read_file(executable)) == binary_hash and built.returncode == 0,
                "reviewed image build failed or Docker changed")
        observed = subprocess.run([str(executable), "--host", ENGINE, "image", "inspect", tag],
                                  cwd=native, env=environment, capture_output=True, timeout=60, check=False)
        require(observed.returncode == 0 and sha(read_file(executable)) == binary_hash,
                "reviewed image inspection failed or Docker changed")
        values = json_bytes(observed.stdout)
        require(isinstance(values, list) and len(values) == 1 and isinstance(values[0], dict),
                "reviewed image inspection invalid")
        image = values[0].get("Id")
        revision = values[0].get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision")
        require(isinstance(image, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", image)
                and revision == selected["sha"], "reviewed image identity differs")
        result[service] = {"tag": tag, "image": image, "source_sha": selected["sha"],
                           "source_tree": selected["tree"], "context_sha256": sha(context)}
    git.clean(repo, selected, tracking=selected["sha"])
    return result




def assert_image_idle(docker, native_root, services, client, idle_snapshot):
    """Require bounded queue and browser idleness before recreating services."""
    counts = idle_snapshot(client("http://127.0.0.1:3002", timeout_seconds=5))
    require(isinstance(counts, dict) and set(counts) == {
        "jobs_in_queue", "active_jobs_in_queue", "waiting_jobs_in_queue", "active_crawl_count"}
        and all(type(value) is int and value == 0 for value in counts.values()), "Firecrawl queues are not idle")
    if "playwright-service" in services:
        executable, native = map(local_path, (docker, native_root))
        binary_hash = sha(read_file(executable))
        script = ("fetch('http://127.0.0.1:'+(process.env.PORT||'3000')+'/health',"
                  "{signal:AbortSignal.timeout(3000)}).then(async r=>{if(!r.ok)throw Error('health');"
                  "process.stdout.write(await r.text())}).catch(()=>process.exit(1))")
        value = _docker_json(executable, native, binary_hash, "exec", "firecrawl-playwright-service-1",
                             "node", "-e", script)
        require(isinstance(value, dict) and value.get("status") == "healthy"
                and type(value.get("activePages")) is int and value["activePages"] == 0,
                "Playwright service is not idle")


def _image_environment(image):
    config = image.get("Config") if isinstance(image, dict) else None
    values = config.get("Env") if isinstance(config, dict) else None
    require(isinstance(values, list), "image defaults unavailable")
    result = {}
    for item in values:
        require(isinstance(item, str) and "=" in item, "invalid image default")
        key, value = item.split("=", 1)
        require(key and key not in result, "duplicate image default")
        result[key] = value
    return result


def updated_image_override(previous, tags):
    """Merge only updater-owned image entries in a stable JSON Compose override."""
    reviewed_override(tags)  # Validate selected service names and tag format.
    value = {"services": {}} if previous is None else json_bytes(previous)
    require(isinstance(value, dict) and set(value) == {"services"}
            and isinstance(value["services"], dict)
            and set(value["services"]) <= {"api", "playwright-service"},
            "existing updater override is not supported")
    services = dict(value["services"])
    for name, item in services.items():
        require(isinstance(item, dict) and set(item) == {"image", "restart"}
                and item["restart"] == "unless-stopped", "existing updater service differs")
        reviewed_override({name: item["image"]})
    for name, tag in tags.items():
        services[name] = {"image": tag, "restart": "unless-stopped"}
    return (json.dumps({"services": services}, sort_keys=True, separators=(",", ":")) + "\n").encode()


def prepare_image_plan(git, source, selected, services, docker, native_root, override_path, output):
    """Build and project selected images before any scheduled task is held."""
    before = observe_current_project(docker, native_root, services)
    built = build_current_images(git, source, selected, services, docker, native_root, before["compose"], output)
    after = observe_current_project(docker, native_root, services)
    for key in ("docker_sha256", "stack", "snapshots", "compose_sha256", "nuq_data_sha256"):
        require(before[key] == after[key], "current project changed during image build")
    executable, native = map(local_path, (docker, native_root))
    binary_hash = before["docker_sha256"]
    current = {value["Config"]["Labels"]["com.docker.compose.service"]: value for value in before["containers"]}
    old_images, new_images, allowed = {}, {}, {}
    for service in sorted(services):
        old_values = _docker_json(executable, native, binary_hash, "image", "inspect", current[service]["Image"])
        new_values = _docker_json(executable, native, binary_hash, "image", "inspect", built[service]["tag"])
        require(isinstance(old_values, list) and len(old_values) == 1
                and isinstance(new_values, list) and len(new_values) == 1
                and old_values[0].get("Id") == current[service]["Image"]
                and new_values[0].get("Id") == built[service]["image"], "image inspect identity changed")
        old_images[service], new_images[service] = old_values[0], new_values[0]
        explicit = set((before["compose"]["services"][service].get("environment") or {}).keys())
        old_env, new_env = _image_environment(old_images[service]), _image_environment(new_images[service])
        require(set(old_env) - explicit == set(new_env) - explicit, "image default keys changed")
        allowed[service] = sorted(key for key in old_env.keys() & new_env.keys()
                                  if key not in explicit and old_env[key] != new_env[key])
    tags = {name: row["tag"] for name, row in built.items()}
    override = local_path(override_path)
    override_key = canonical_path(windows_path(override)).casefold()
    stack = before["stack"]["files"]
    keys = [canonical_path(path).casefold() for path in stack]
    if override_key in keys:
        require(keys[-1] == override_key, "updater override is not final")
        previous = read_file(override)
        require(sha(previous) == before["stack"]["sha256"][-1], "updater override changed")
        preview_files = [*stack[:-1]]
    else:
        require(not os.path.lexists(override), "unowned updater override path exists")
        previous = None
        preview_files = list(stack)
    overlay = updated_image_override(previous, tags)
    preview = Path(output) / "compose-preview.yaml"
    with preview.open("xb") as stream:
        stream.write(overlay)
    argv = ["compose", "--project-name", "firecrawl", "--project-directory", windows_path(local_path(native_root))]
    for path in [*preview_files, windows_path(preview)]:
        argv += ["-f", windows_path(local_path(path))]
    projected_compose = _docker_json(executable, native, binary_hash, *argv, "config", "--format", "json")
    compare_compose_overlay(before["compose"], projected_compose, tags)
    packet = {"expected_images": {name: row["image"] for name, row in built.items()},
              "allowed_image_default_changes": allowed, "new": selected,
              "compose_files": before["stack"]["files"], "compose_override_path": str(override_path)}
    projected = qualify_fleet_projection(before["containers"], old_images, new_images, before["compose"], packet)
    plan = {"selected": selected, "services": sorted(services), "images": built,
            "docker_sha256": binary_hash, "compose_stack": before["stack"],
            "nuq_data_sha256": before["nuq_data_sha256"], "compose_sha256": before["compose_sha256"],
            "projected_compose_sha256": sha(json.dumps(projected_compose, sort_keys=True,
                                                       separators=(",", ":")).encode()),
            "before": projected["before"],
            "expected_after_config": projected["expected_after_config"], "override_sha256": sha(overlay),
            "override_path": windows_path(override),
            "previous_override_sha256": sha(previous) if previous is not None else None}
    return plan, overlay


def recheck_image_plan(plan, selected, docker, native_root):
    """Re-observe selected-bound project and image identities before mutation."""
    identity(selected)
    require(isinstance(plan, dict) and plan.get("selected") == selected
            and isinstance(plan.get("images"), dict)
            and all(item.get("source_sha") == selected["sha"] and item.get("source_tree") == selected["tree"]
                    for item in plan["images"].values() if isinstance(item, dict))
            and all(isinstance(item, dict) for item in plan["images"].values())
            and set(plan["images"]) == set(plan.get("services", [])),
            "image plan is not bound to selected source")
    observed = observe_current_project(docker, native_root, set(plan["services"]))
    require(observed["docker_sha256"] == plan["docker_sha256"]
            and observed["stack"] == plan["compose_stack"]
            and observed["nuq_data_sha256"] == plan["nuq_data_sha256"]
            and observed["compose_sha256"] == plan["compose_sha256"]
            and sorted(observed["snapshots"], key=lambda row: row["service"]) == plan["before"],
            "current image project changed before apply")
    executable, native = map(local_path, (docker, native_root))
    for item in plan["images"].values():
        values = _docker_json(executable, native, plan["docker_sha256"], "image", "inspect", item["tag"])
        require(isinstance(values, list) and len(values) == 1
                and values[0].get("Id") == item["image"], "built image tag changed")
    return observed


def expected_compose_stack(plan):
    """Bind each current file and the final updater override to exact bytes."""
    original = plan["compose_stack"]
    files = expected_overlay_config_files(",".join(original["files"]), plan["override_path"]).split(",")
    hashes = list(original["sha256"])
    require(len(original["files"]) == len(hashes), "Compose stack hash count differs")
    if len(files) == len(original["files"]):
        hashes[-1] = plan["override_sha256"]
    else:
        hashes.append(plan["override_sha256"])
    return [{"path": path, "sha256": digest} for path, digest in zip(files, hashes, strict=True)]


def verify_image_result(plan, selected, docker, native_root):
    """Verify the selected images, effective config and unaffected fleet."""
    identity(selected)
    require(plan.get("selected") == selected and set(plan["images"]) == set(plan["services"])
            and all(item["source_sha"] == selected["sha"] and item["source_tree"] == selected["tree"]
                    for item in plan["images"].values()), "image plan selected source differs")
    observed = observe_current_project(docker, native_root, set(plan["services"]))
    expected_stack = expected_compose_stack(plan)
    expected_files = [row["path"] for row in expected_stack]
    expected_hashes = [row["sha256"] for row in expected_stack]
    actual_stack = observed["stack"]
    require(observed["docker_sha256"] == plan["docker_sha256"]
            and observed["nuq_data_sha256"] == plan["nuq_data_sha256"]
            and [canonical_path(path).casefold() for path in actual_stack["files"]]
                == [canonical_path(path).casefold() for path in expected_files]
            and actual_stack["sha256"] == expected_hashes
            and observed["compose_sha256"] == plan["projected_compose_sha256"],
            "selected Compose configuration differs")
    executable, native = map(local_path, (docker, native_root))
    expected_images = {}
    for service, item in plan["images"].items():
        values = _docker_json(executable, native, plan["docker_sha256"], "image", "inspect", item["tag"])
        require(isinstance(values, list) and len(values) == 1 and values[0].get("Id") == item["image"],
                "selected image tag changed")
        expected_images[service] = item["image"]
    outcome = qualify_current_image_transition(plan["before"], observed["snapshots"], expected_images,
                                               expected_after_config=plan["expected_after_config"])
    return {"images": outcome, "containers": {row["service"]: row["status"] for row in observed["snapshots"]}}


def install_image_override(plan, overlay, owner, output):
    """Atomically publish the staged override under the held owner."""
    require(isinstance(overlay, bytes) and sha(overlay) == plan["override_sha256"],
            "staged Compose override changed")
    path = local_path(plan["override_path"])
    require(path.is_absolute() and all(not parent.is_symlink() for parent in (path, *path.parents)),
            "Compose override path alias")
    owner()
    path.parent.mkdir(parents=True, exist_ok=True)
    owner()
    require(all(not parent.is_symlink() for parent in (path, *path.parents)),
            "Compose override path changed")
    expected = plan["previous_override_sha256"]

    def previous_bytes():
        if expected is None:
            require(not os.path.lexists(path), "unowned Compose override appeared")
            return None
        data = read_file(path)
        require(sha(data) == expected, "previous Compose override changed")
        return data

    owner()
    previous = previous_bytes()
    if previous is not None:
        backup = Path(output) / "previous-compose-override.json"
        with backup.open("xb") as stream:
            stream.write(previous)
            stream.flush()
            os.fsync(stream.fileno())
    fd, temp_name = tempfile.mkstemp(prefix=".reviewed-compose-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(overlay)
            stream.flush()
            os.fsync(stream.fileno())
        owner()
        require(previous_bytes() == previous, "previous Compose override changed")
        os.replace(temp_name, path)
        owner()
        require(sha(read_file(path)) == plan["override_sha256"], "published Compose override differs")
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


class ComposeCompletedFailure(RuntimeError):
    """The native Compose process finished and reported a nonzero exit."""


def run_current_compose(checkpoint, owner, lease, development, control, control_sha, plan_path, output, powershell):
    """Run one owner-bound native Compose step; unknown outcomes retain the fence."""
    owner()
    raw = read_file(plan_path)
    plan_sha = sha(raw)
    stamp = str(time.time_ns())
    receipt_path = Path(output) / ("compose-" + stamp + ".json")
    log_path = Path(output) / ("compose-" + stamp + ".log")
    wrapper = Path(development) / "scripts/firecrawl-ops/cre_collector/windows/Invoke-CreReviewedCompose.ps1"
    argv = [str(powershell), "-NoProfile", "-NonInteractive", "-File", windows_path(wrapper),
            "-Plan", windows_path(plan_path), "-PlanSha", plan_sha,
            "-Control", windows_path(control), "-ControlSha", control_sha,
            "-OwnerPid", str(os.getpid()), "-Generation", lease.authority_generation,
            "-Receipt", windows_path(receipt_path)]
    deadline = time.monotonic() + 300

    def poll():
        owner()
        if time.monotonic() >= deadline:
            raise TimeoutError("Compose process timed out; retain recovery fence")

    with command_poll_hook(poll):
        rc = checkpoint.run_command(argv, log_path, env=checkpoint.safe_process_env())
    owner()
    require(read_file(plan_path) == raw, "Compose plan changed; retain recovery fence")
    expected = {"stage": "ComposeUp", "plan_sha256": plan_sha, "owner_pid": os.getpid(),
                "generation_sha256": sha(lease.authority_generation.encode()), "completed": True}
    try:
        data = read_file(receipt_path)
        receipt = json_bytes(data) if len(data) <= 4096 else None
    except (HandoffError, OSError, ValueError):
        receipt = None
    if (rc < 0 or not isinstance(receipt, dict)
            or set(receipt) != {*expected, "docker_exit"}
            or any(receipt.get(key) != value for key, value in expected.items())
            or type(receipt.get("owner_pid")) is not int
            or receipt.get("completed") is not True
            or type(receipt.get("docker_exit")) is not int):
        raise RuntimeError("Compose completion unknown; retain recovery fence")
    docker_exit = receipt["docker_exit"]
    if rc == 0 and docker_exit == 0:
        return
    if rc > 0 and docker_exit != 0:
        raise ComposeCompletedFailure("Compose finished with a nonzero exit")
    raise RuntimeError("Compose process and receipt disagree; retain recovery fence")
