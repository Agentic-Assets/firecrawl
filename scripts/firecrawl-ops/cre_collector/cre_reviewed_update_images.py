"""Direct selected-source image preparation for the reviewed updater."""

import copy
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


class DockerObservationUnavailable(HandoffError):
    """Docker did not provide an observation; no state conclusion is possible."""


def _docker_json(executable, native, binary_hash, *args):
    require(sha(read_file(executable)) == binary_hash, "Docker executable changed")
    try:
        result = subprocess.run([str(executable), "--host", ENGINE, *args],
                                cwd=native, env=_docker_environment(), capture_output=True, timeout=60, check=False)
    except subprocess.TimeoutExpired as error:
        require(sha(read_file(executable)) == binary_hash, "Docker executable changed")
        raise DockerObservationUnavailable("Docker observation timed out") from error
    require(sha(read_file(executable)) == binary_hash, "Docker executable changed")
    if result.returncode != 0 and result.stderr.startswith(b"Cannot connect to the Docker daemon at "):
        raise DockerObservationUnavailable("Docker daemon unavailable")
    require(result.returncode == 0 and len(result.stdout) <= 8 * 1024 * 1024,
            "Docker observation failed or changed")
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


def observe_current_project(docker, native_root, services, override_path=None):
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
    scope = {"api", "playwright-service"} if override_path is not None else services
    stack = current_compose_files(containers, scope, windows_path(native), override_path=override_path)
    if override_path is not None and canonical_path(stack["files"][-1]).casefold() == canonical_path(override_path).casefold():
        override_bytes = read_file(local_path(override_path))
        value = json_bytes(override_bytes)
        entries = value.get("services") if isinstance(value, dict) else None
        require(isinstance(entries, dict) and entries, "reviewed Compose override is empty or invalid")
        name = next(iter(entries))
        require(isinstance(entries[name], dict) and isinstance(entries[name].get("image"), str),
                "reviewed Compose override entry is invalid")
        updated_image_override(override_bytes, {name: entries[name]["image"]})
        live = {row["Config"]["Labels"]["com.docker.compose.service"]: row for row in containers}
        for name, entry in entries.items():
            images = _docker_json(executable, native, binary_hash, "image", "inspect", entry["image"])
            require(isinstance(images, list) and len(images) == 1 and images[0].get("Id") == live[name]["Image"],
                    "reviewed Compose override image differs from running service")
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
        archive = git.command(repo, "-c", "core.autocrlf=false", "-c", "core.eol=lf",
                              "archive", "--format=tar", selected["sha"], SERVICES[service])
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
                  "process.stdout.write(await r.text())}).catch(()=>process.stdout.write(JSON.stringify({status:\"unhealthy\"})))")
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
    override_path = windows_path(local_path(override_path))
    before = observe_current_project(docker, native_root, services, override_path)
    built = build_current_images(git, source, selected, services, docker, native_root, before["compose"], output)
    after = observe_current_project(docker, native_root, services, override_path)
    for key in ("docker_sha256", "stack", "snapshots", "compose_sha256", "nuq_data_sha256"):
        require(before[key] == after[key], "current project changed during image build")
    executable, native = map(local_path, (docker, native_root))
    binary_hash = before["docker_sha256"]
    current = {value["Config"]["Labels"]["com.docker.compose.service"]: value for value in before["containers"]}
    changed = {name for name in services if built[name]["image"] != current[name]["Image"]}
    if not changed:
        return ({"selected": selected, "services": sorted(services), "apply_services": [], "images": built,
                 "docker_sha256": binary_hash, "compose_stack": before["stack"],
                 "nuq_data_sha256": before["nuq_data_sha256"], "compose_sha256": before["compose_sha256"],
                 "before": sorted(before["snapshots"], key=lambda row: row["service"]),
                 "override_path": windows_path(local_path(override_path))}, None)
    old_images, new_images, allowed, previous_images, rollback_config = {}, {}, {}, {}, {}
    for service in sorted(changed):
        old_values = _docker_json(executable, native, binary_hash, "image", "inspect", current[service]["Image"])
        new_values = _docker_json(executable, native, binary_hash, "image", "inspect", built[service]["tag"])
        require(isinstance(old_values, list) and len(old_values) == 1
                and isinstance(new_values, list) and len(new_values) == 1
                and old_values[0].get("Id") == current[service]["Image"]
                and new_values[0].get("Id") == built[service]["image"], "image inspect identity changed")
        old_images[service], new_images[service] = old_values[0], new_values[0]
        service_config = before["compose"]["services"][service]
        # Compose uses <project>-<service> for a build-only service.
        # Rollback retains the original files and therefore selects this tag.
        previous_ref = service_config.get("image") if "image" in service_config else "firecrawl-" + service
        require(isinstance(previous_ref, str) and previous_ref and not previous_ref.startswith("-"),
                "previous Compose image reference unavailable")
        resolved = _docker_json(executable, native, binary_hash, "image", "inspect", previous_ref)
        require(isinstance(resolved, list) and len(resolved) == 1
                and resolved[0].get("Id") == current[service]["Image"],
                "previous Compose image reference does not identify the running image")
        previous_images[service] = {"ref": previous_ref, "image": current[service]["Image"]}
        rollback_container = copy.deepcopy(current[service])
        labels = rollback_container["Config"]["Labels"]
        number = labels.get("com.docker.compose.container-number")
        require(isinstance(number, str) and re.fullmatch(r"[1-9][0-9]*", number),
                "previous Compose container number unavailable")
        labels["com.docker.compose.replace"] = service + "-" + number
        labels["com.docker.compose.project.config_files"] = ",".join(before["stack"]["files"])
        rollback_config[service] = snapshot_container(rollback_container)["config_sha256"]
        explicit = set((before["compose"]["services"][service].get("environment") or {}).keys())
        old_env, new_env = _image_environment(old_images[service]), _image_environment(new_images[service])
        require(set(old_env) - explicit == set(new_env) - explicit, "image default keys changed")
        allowed[service] = sorted(key for key in old_env.keys() & new_env.keys()
                                  if key not in explicit and old_env[key] != new_env[key])
    tags = {name: built[name]["tag"] for name in changed}
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
    packet = {"expected_images": {name: built[name]["image"] for name in changed},
              "allowed_image_default_changes": allowed, "new": selected,
              "compose_files": before["stack"]["files"], "compose_override_path": str(override_path)}
    projected = qualify_fleet_projection(before["containers"], old_images, new_images, before["compose"], packet)
    plan = {"selected": selected, "services": sorted(services), "apply_services": sorted(changed), "images": built,
            "docker_sha256": binary_hash, "compose_stack": before["stack"],
            "nuq_data_sha256": before["nuq_data_sha256"], "compose_sha256": before["compose_sha256"],
            "projected_compose_sha256": sha(json.dumps(projected_compose, sort_keys=True,
                                                       separators=(",", ":")).encode()),
            "before": projected["before"], "previous_images": previous_images,
            "rollback_config": rollback_config,
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
    observed = observe_current_project(docker, native_root, set(plan["services"]), plan["override_path"])
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
    for item in plan.get("previous_images", {}).values():
        values = _docker_json(executable, native, plan["docker_sha256"], "image", "inspect", item["ref"])
        require(isinstance(values, list) and len(values) == 1
                and values[0].get("Id") == item["image"], "previous image reference changed")
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
    if not plan["apply_services"]:
        observed = recheck_image_plan(plan, selected, docker, native_root)
        return {"images": {"unchanged": sorted(plan["services"]), "replaced": []},
                "containers": {row["service"]: row["status"] for row in observed["snapshots"]}}
    observed = observe_current_project(docker, native_root, set(plan["apply_services"]), plan["override_path"])
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


def qualify_settled_partial_fleet(plan, docker, native_root, owner):
    """Allow a rollback only if every observed container remains within the sealed plan."""
    owner()
    executable, native = map(local_path, (docker, native_root))
    require(sha(read_file(executable)) == plan["docker_sha256"], "Docker executable changed")
    result = subprocess.run(
        [str(executable), "--host", ENGINE, "ps", "-aq", "--no-trunc",
         "--filter", "label=com.docker.compose.project=firecrawl"],
        cwd=native, env=_docker_environment(), capture_output=True, timeout=60, check=False)
    owner()
    require(result.returncode == 0 and sha(read_file(executable)) == plan["docker_sha256"],
            "partial fleet observation failed")
    ids = result.stdout.decode("ascii").split()
    require(len(ids) == len(set(ids)) and len(ids) <= 5, "partial fleet identities differ")
    containers = _docker_json(executable, native, plan["docker_sha256"], "inspect", *ids) if ids else []
    owner()
    require(isinstance(containers, list) and len(containers) == len(ids), "partial fleet inspection differs")
    snapshots = [snapshot_container(row) for row in containers]
    actual = {row["service"]: row for row in snapshots}
    before = {row["service"]: row for row in plan["before"]}
    affected = set(plan["apply_services"])
    require(len(actual) == len(snapshots) and set(actual) <= set(before)
            and {row["id"] for row in snapshots} == set(ids)
            and set(before) - affected <= set(actual), "unrelated fleet is incomplete")
    require(all(row["Config"]["Labels"].get("com.docker.compose.project") == "firecrawl"
                and row.get("Name") == "/firecrawl-" + row["Config"]["Labels"].get("com.docker.compose.service", "") + "-1"
                for row in containers), "partial fleet project differs")
    require(nuq_data_identity(containers) == plan["nuq_data_sha256"], "NuQ data mount differs")
    for name, row in actual.items():
        if name not in affected:
            require(row == before[name], "unrelated container changed")
        elif row["id"] == before[name]["id"]:
            require(row["image"] == before[name]["image"]
                    and row["config_sha256"] == before[name]["config_sha256"]
                    and row["restart_policy"] == before[name]["restart_policy"]
                    and row["status"] in {"running", "exited", "restarting", "created", "dead"},
                    "original container changed")
        else:
            require(row["image"] == plan["images"][name]["image"]
                    and row["config_sha256"] == plan["expected_after_config"][name]
                    and row["restart_policy"] == before[name]["restart_policy"],
                    "affected container is outside selected transition")
    for item in expected_compose_stack(plan):
        require(sha(read_file(local_path(item["path"]))) == item["sha256"], "Compose file changed")
    for item in plan["previous_images"].values():
        values = _docker_json(executable, native, plan["docker_sha256"], "image", "inspect", item["ref"])
        require(isinstance(values, list) and len(values) == 1 and values[0].get("Id") == item["image"],
                "previous image reference changed")
    owner()


def verify_original_images(plan, selected, docker, native_root):
    """Prove original image identities, stack, data mount and unaffected containers."""
    identity(selected)
    require(plan.get("selected") == selected, "image plan selected source differs")
    observed = observe_current_project(docker, native_root, set(plan["services"]), plan["override_path"])
    require(observed["docker_sha256"] == plan["docker_sha256"]
            and observed["stack"] == plan["compose_stack"]
            and observed["nuq_data_sha256"] == plan["nuq_data_sha256"]
            and observed["compose_sha256"] == plan["compose_sha256"],
            "original Compose configuration differs")
    previous = {row["service"]: row for row in plan["before"]}
    actual = {row["service"]: row for row in observed["snapshots"]}
    require(len(previous) == len(plan["before"]) and len(actual) == len(observed["snapshots"])
            and set(previous) == set(actual), "original fleet differs")
    executable, native = map(local_path, (docker, native_root))
    for name, after in actual.items():
        before = previous[name]
        if name not in plan["apply_services"]:
            require(before == after, "unaffected container changed")
            continue
        image = plan["previous_images"][name]
        values = _docker_json(executable, native, plan["docker_sha256"], "image", "inspect", image["ref"])
        require(isinstance(values, list) and len(values) == 1 and values[0].get("Id") == image["image"]
                and after["image"] == image["image"]
                and after["status"] in {"running", "exited", "restarting", "created", "dead"}
                and after["restart_policy"] == before["restart_policy"],
                "original image or container state differs")
        expected_config = before["config_sha256"] if before["id"] == after["id"] else plan["rollback_config"][name]
        require(after["config_sha256"] == expected_config, "restored container configuration differs")
    return {"restored": sorted(plan["apply_services"]),
            "containers": {name: row["status"] for name, row in actual.items()}}


class SelectedImageUnready(HandoffError):
    """A settled selected image transition did not become locally ready."""


def wait_for_service_readiness(plan, docker, native_root, client, owner, observe, *, timeout_seconds=30):
    """Check running state and bounded endpoints without claiming scrape readiness."""
    require(type(timeout_seconds) is int and 0 < timeout_seconds <= 60, "invalid readiness timeout")
    affected = set(plan["apply_services"])
    require(affected and affected <= {"api", "playwright-service"}, "invalid readiness services")
    deadline = time.monotonic() + timeout_seconds
    while True:
        owner()
        try:
            result = observe()
        except DockerObservationUnavailable:
            owner()
            if time.monotonic() >= deadline:
                raise
            time.sleep(min(2, max(0, deadline - time.monotonic())))
            continue
        try:
            if not all(result["containers"][name] == "running" for name in affected):
                raise RuntimeError("selected container is not running")
            if "api" in affected:
                api = client("http://127.0.0.1:3002", timeout_seconds=5)
                if not (api.get_json("/").get("message") == "Firecrawl API"
                        and api.get_json("/v2/team/queue-status").get("success") is True):
                    raise RuntimeError("selected API is unavailable")
            if "playwright-service" in affected:
                executable, native = map(local_path, (docker, native_root))
                script = ("fetch('http://127.0.0.1:'+(process.env.PORT||'3000')+'/health',"
                          "{signal:AbortSignal.timeout(3000)}).then(async r=>{if(!r.ok)throw Error('health');"
                          "process.stdout.write(await r.text())}).catch(()=>process.stdout.write(JSON.stringify({status:\"unhealthy\"})))")
                value = _docker_json(executable, native, plan["docker_sha256"], "exec",
                                     "firecrawl-playwright-service-1", "node", "-e", script)
                if not isinstance(value, dict) or value.get("status") != "healthy":
                    raise RuntimeError("selected Playwright is unavailable")
        except DockerObservationUnavailable:
            owner()
            if time.monotonic() >= deadline:
                raise
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        except HandoffError:
            raise
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        else:
            owner()
            return result

def wait_for_selected_images(plan, selected, docker, native_root, client, owner, *, timeout_seconds=30):
    def observe():
        try:
            return verify_image_result(plan, selected, docker, native_root)
        except DockerObservationUnavailable:
            raise
        except (HandoffError, OSError, subprocess.TimeoutExpired) as error:
            raise SelectedImageUnready("selected image transition differs") from error

    try:
        result = wait_for_service_readiness(plan, docker, native_root, client, owner, observe,
                                            timeout_seconds=timeout_seconds)
    except DockerObservationUnavailable as error:
        raise SelectedImageUnready("selected image observation timed out") from error
    if result is None:
        raise SelectedImageUnready("selected image readiness timed out")
    return result


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


def restore_image_override(plan, owner, output):
    """Restore only the exact prior override after a settled Compose outcome."""
    path = local_path(plan["override_path"])
    require(path.is_absolute() and all(not parent.is_symlink() for parent in (path, *path.parents)),
            "Compose override path alias")
    owner()
    require(sha(read_file(path)) == plan["override_sha256"], "published Compose override changed")
    expected = plan["previous_override_sha256"]
    if expected is None:
        owner()
        require(sha(read_file(path)) == plan["override_sha256"], "published Compose override changed")
        path.unlink()
        owner()
        require(not os.path.lexists(path), "Compose override restoration differs")
        return
    previous = read_file(Path(output) / "previous-compose-override.json")
    require(sha(previous) == expected, "previous Compose override backup changed")
    fd, temp_name = tempfile.mkstemp(prefix=".reviewed-compose-restore-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(previous)
            stream.flush()
            os.fsync(stream.fileno())
        owner()
        require(sha(read_file(path)) == plan["override_sha256"], "published Compose override changed")
        os.replace(temp_name, path)
        owner()
        require(sha(read_file(path)) == expected, "Compose override restoration differs")
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


class ComposeCompletedFailure(RuntimeError):
    """The native Compose process finished and reported a nonzero exit."""


def run_current_compose(checkpoint, owner, lease, development, control, control_sha, plan_path, output, powershell):
    """Run one owner-bound native Compose step; unknown outcomes retain the fence."""
    owner()
    raw = read_file(plan_path)
    plan = json_bytes(raw)
    mode = plan.get("mode", "up")
    require(mode in {"up", "rollback"}, "invalid Compose mode")
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
        rc = checkpoint._run_logged_command(argv, log_path, env=checkpoint.safe_process_env())
    owner()
    require(read_file(plan_path) == raw, "Compose plan changed; retain recovery fence")
    expected = {"stage": "ComposeRollback" if mode == "rollback" else "ComposeUp",
                "plan_sha256": plan_sha, "owner_pid": os.getpid(),
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
