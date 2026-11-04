"""Validate source-bound prepared-image evidence for runtime maintenance.

This module deliberately does not authorize or perform a runtime transition.
Only a settled producer can supply the referenced command and inspect evidence.
"""

import argparse
import copy
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tarfile
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath

from cre_code_handoff_evidence import (
    HandoffError,
    canonical_path,
    identity,
    require,
    sha,
    unique,
)
from cre_code_handoff_git import Git, local_path, read_file, windows_path
from cre_runtime_maintenance_images import (
    qualify_environment_transition,
    snapshot_container,
)

SERVICES = {
    "api": "apps/api",
    "playwright-service": "apps/playwright-service-ts",
    "nuq-postgres": "apps/nuq-postgres",
}
ENGINE = "npipe:////./pipe/dockerDesktopLinuxEngine"


def verify_native_docker(packet):
    """Bind all native Docker calls to the reviewed, unaliased executable bytes."""
    expected = packet.get("native_docker_sha256")
    require(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected),
            "native Docker executable digest required")
    require(sha(read_file(packet["programs"]["native_docker"])) == expected,
            "native Docker executable changed")


def _docker_run(packet, args, **kwargs):
    verify_native_docker(packet)
    try:
        result = subprocess.run([str(local_path(packet["programs"]["native_docker"])), "--host", ENGINE, *args],  # noqa: PLW1510
                                **kwargs)
    except BaseException as original:
        try:
            verify_native_docker(packet)
        except (HandoffError, OSError) as secondary:
            if hasattr(original, "add_note"):
                original.add_note("Native Docker executable postcheck failed: " + str(secondary))
        raise
    else:
        verify_native_docker(packet)
        return result


def _read_compose_binary(binding):
    """Read the vendor-installed plugin after qualifying every NTFS hardlink."""
    require(isinstance(binding, dict) and set(binding) == {"path", "sha256", "aliases"}
            and isinstance(binding["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", binding["sha256"])
            and isinstance(binding["aliases"], list) and binding["aliases"], "native Compose plugin binding required")
    path = local_path(binding["path"])
    native = windows_path(path)
    aliases = [canonical_path(value).casefold() for value in binding["aliases"]]
    require(len(set(aliases)) == len(aliases) and canonical_path(native).casefold() in aliases
            and all(value.startswith("c:/program files/docker/") for value in aliases),
            "Compose plugin aliases are outside the reviewed vendor installation")
    require(all(not p.is_symlink() for p in (path, *path.parents)), "Compose plugin path alias refused")

    def observe_links():
        result = subprocess.run(["/mnt/c/Windows/System32/fsutil.exe", "hardlink", "list", native],
                                check=False, capture_output=True, timeout=30)
        require(result.returncode == 0, "Compose plugin hardlink discovery failed")
        lines = result.stdout.decode().splitlines()
        found = [canonical_path(native[:2] + line).casefold() for line in lines if line]
        require(len(found) == len(set(found)) and set(found) == set(aliases), "Compose plugin hardlinks differ")

    observe_links()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == len(aliases)
                and before.st_size <= 128 * 1024 * 1024, "Compose plugin file identity differs")
        for alias in binding["aliases"]:
            item = local_path(alias)
            require(all(not p.is_symlink() for p in (item, *item.parents)), "Compose hardlink alias refused")
            same = item.stat()
            require((same.st_dev, same.st_ino, same.st_nlink) == (before.st_dev, before.st_ino, before.st_nlink),
                    "Compose hardlink identity differs")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read()
        after, named = os.fstat(fd), path.lstat()
        fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        require(all(getattr(before, field) == getattr(after, field) == getattr(named, field) for field in fields),
                "Compose plugin changed during read")
    finally:
        os.close(fd)
    observe_links()
    require(sha(raw) == binding["sha256"], "native Compose plugin changed")
    return raw


def verify_native_compose(packet):
    """Bind the actual Docker-discovered Compose plugin and its reviewed bytes."""
    binding = packet.get("native_compose")
    _read_compose_binary(binding)
    root = local_path(packet["roots"]["native"])
    result = _docker_run(packet, ["info", "--format", "{{json .ClientInfo.Plugins}}"],
                         check=False, cwd=root, capture_output=True, timeout=60)
    require(result.returncode == 0, "native Compose discovery failed")
    try:
        plugins = json.loads(result.stdout)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("native Compose discovery returned invalid JSON") from exc
    require(isinstance(plugins, list), "native Compose discovery missing")
    matches = [value for value in plugins if isinstance(value, dict) and value.get("Name") == "compose"]
    require(len(matches) == 1 and isinstance(matches[0].get("Path"), str)
            and canonical_path(matches[0]["Path"]).casefold() == canonical_path(binding["path"]).casefold()
            and isinstance(matches[0].get("Version"), str), "discovered Compose plugin differs")
    version = matches[0]["Version"].removeprefix("v")
    require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version),
            "native Compose version invalid")
    _read_compose_binary(binding)
    return version


def _write_new(path, data):
    with open(path, "xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return {"path": str(path), "sha256": sha(data), "bytes": len(data)}


def _source_archive(repo, selected, service):
    return subprocess.run(["git", "-C", str(repo), "archive", selected["sha"], SERVICES[service]],
                          check=True, capture_output=True, timeout=120).stdout


def _verify_archive_tree(git, repo, selected, service, archive):
    """Refuse export-ignore/substitution and prove every tracked blob exactly."""
    prefix = SERVICES[service] + "/"
    expected = {}
    for row in git.command(repo, "ls-tree", "-rz", "--full-tree", selected["sha"], "--", SERVICES[service]).split(b"\0"):
        if not row:
            continue
        meta, raw_path = row.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        path = raw_path.decode()
        require(kind == "blob" and mode in {"100644", "100755"} and path.startswith(prefix),
                "unsupported tracked build input")
        expected[path] = (mode, oid)
    actual = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        for member in source:
            if member.isdir():
                continue
            require(member.isfile() and member.name.startswith(prefix) and member.name not in actual,
                    "unexpected archive input")
            data = source.extractfile(member).read()
            blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data, usedforsecurity=False).hexdigest()
            actual[member.name] = ("100755" if member.mode & 0o111 else "100644", blob)
    require(actual == expected and expected, "source archive differs from tracked Git tree")


def _context_tar(archive, service):
    """Build immutable Docker stdin bytes directly from the selected Git archive."""
    prefix = PurePosixPath(SERVICES[service])
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source, \
            tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as dest:
        for member in source:
            path = PurePosixPath(member.name)
            if prefix not in path.parents:
                continue
            require(".." not in path.parts and (member.isdir() or member.isfile()),
                    "unsupported source archive member")
            info = tarfile.TarInfo(path.relative_to(prefix).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            info.mode = 0o755 if member.isdir() or member.mode & 0o111 else 0o644
            if member.isdir():
                info.type = tarfile.DIRTYPE
                dest.addfile(info)
            else:
                content = source.extractfile(member).read()
                info.size = len(content)
                dest.addfile(info, io.BytesIO(content))
    return output.getvalue()


def prepare_images(packet, git, output):
    """Build unique, non-serving images from a clean selected ext4 checkout.

    The caller must separately review the resulting artifacts and qualify a
    projected container configuration. This function never runs Compose up.
    """
    selected = packet["new"]
    identity(selected)
    build_source = packet.get("build_source")
    require(isinstance(build_source, dict) and set(build_source) == {"root", "identity"}
            and build_source["identity"] == selected, "build source binding differs")
    repo = local_path(build_source["root"])
    native_root = local_path(packet["roots"]["native"])
    runtime = local_path(packet["roots"]["runtime"])
    development = local_path(packet["roots"]["development"])
    docker = local_path(packet["programs"]["native_docker"])
    require(not str(repo).startswith("/mnt/") and repo.is_absolute() and (repo / ".git").is_dir()
            and not (repo / ".git").is_symlink() and docker.is_file() and native_root.is_dir()
            and str(docker).lower().endswith(".exe"),
            "ext4 source and existing native project/docker required")
    require(all(not p.is_symlink() for p in (repo, *repo.parents, native_root, *native_root.parents,
                                              docker, *docker.parents)), "source or native path alias refused")
    require(all(repo != other and repo not in other.parents and other not in repo.parents
                for other in (native_root, runtime, development)), "build source role must be separate")
    git.hooks(repo)
    verify_native_docker(packet)
    require(git.text(repo, "rev-parse", "HEAD") == selected["sha"]
            and git.text(repo, "rev-parse", "HEAD^{tree}") == selected["tree"]
            and not git.command(repo, "status", "--porcelain=v1", "--untracked-files=all"),
            "selected source checkout must be exact and clean")
    output = Path(output)
    require(output.is_absolute() and not str(output).startswith("/mnt/")
            and all(not p.is_symlink() for p in output.parents), "exclusive ext4 output required")
    require(repo not in output.parents and output not in repo.parents,
            "build output must be separate from source")
    output.mkdir(mode=0o700)
    require(not output.is_symlink(), "output alias refused")
    tags = packet.get("image_tags")
    require(isinstance(tags, dict) and tags and set(tags) <= set(SERVICES), "invalid build service set")
    rows = []
    for service, tag in sorted(tags.items()):
        require(isinstance(tag, str) and re.fullmatch(r"firecrawl-" + re.escape(service)
                + r":qa-[a-z0-9][a-z0-9-]{7,80}", tag), "non-production unique tag required")
        listed = _docker_run(packet, ["image", "ls", "--format", "{{.Repository}}:{{.Tag}}"],
                             check=False, cwd=native_root, capture_output=True, timeout=60)
        require(listed.returncode == 0 and tag not in listed.stdout.decode().splitlines(),
                "prepared image tag listing failed or tag already exists")
        existing = _docker_run(packet, ["image", "inspect", tag], check=False, cwd=native_root,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        require(existing.returncode != 0, "prepared image tag already exists")
        archive = _source_archive(repo, selected, service)
        _verify_archive_tree(git, repo, selected, service, archive)
        context = _context_tar(archive, service)
        context_ref = _write_new(output / (service + "-context.tar"), context)
        command = [str(docker), "--host", ENGINE, "build", "--progress", "plain", "--label",
                   "org.opencontainers.image.revision=" + selected["sha"]]
        if service == "api":
            command += ["--build-arg", "GIT_SHA=" + selected["sha"]]
        command += ["-t", tag, "-"]
        started = datetime.now(UTC).isoformat()
        result = _docker_run(packet, command[3:], check=False, cwd=native_root, input=context, capture_output=True, timeout=3600)
        finished = datetime.now(UTC).isoformat()
        stdout = _write_new(output / (service + "-stdout.log"), result.stdout)
        stderr = _write_new(output / (service + "-stderr.log"), result.stderr)
        require(result.returncode == 0, "prepared image build failed")
        verify_native_docker(packet)
        inspected = _docker_run(packet, ["image", "inspect", tag], cwd=native_root,
                                check=True, capture_output=True, timeout=60)
        values = json.loads(inspected.stdout)
        require(isinstance(values, list) and len(values) == 1, "image inspect did not return one image")
        value = values[0]
        image = value.get("Id")
        revision = value.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision")
        require(isinstance(image, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", image)
                and revision == selected["sha"], "image inspect identity mismatch")
        safe_inspect = json.dumps({"Id": image, "Config": {"Labels": {
            "org.opencontainers.image.revision": revision}}}, sort_keys=True).encode()
        inspect_ref = _write_new(output / (service + "-inspect.json"), safe_inspect)
        require(_source_archive(repo, selected, service) == archive, "source archive changed")
        rows.append({"service": service, "identity": selected,
                     "tree": git.text(repo, "rev-parse", selected["sha"] + ":" + SERVICES[service]),
                     "dockerfile": SERVICES[service] + "/Dockerfile", "tag": tag, "image": image,
                     "exit_code": result.returncode, "stdout_eof": True, "stderr_eof": True,
                     "log": stdout, "stderr": stderr, "inspect": inspect_ref, "context": context_ref,
                     "archive_sha256": sha(archive), "command": command,
                     "started_at": started, "finished_at": finished})
    require(git.text(repo, "rev-parse", "HEAD") == selected["sha"]
            and git.text(repo, "rev-parse", "HEAD^{tree}") == selected["tree"]
            and not git.command(repo, "status", "--porcelain=v1", "--untracked-files=all"),
            "selected source changed during build")
    verify_native_docker(packet)
    manifest = {"schema": "cre-prepared-images-v1", "identity": selected,
                "docker_sha256": packet["native_docker_sha256"],
                "source_root": str(repo),
                "project_root": windows_path(native_root), "images": rows}
    _write_new(output / "prepared-images.json", json.dumps(manifest, sort_keys=True, indent=2).encode())
    return manifest


def main():
    parser = argparse.ArgumentParser(description="Prepare source-bound images without changing running services")
    parser.add_argument("--spec", required=True, help="reviewed build specification JSON")
    parser.add_argument("--output", required=True, help="new exclusive ext4 evidence directory")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--prepare", action="store_true", help="perform the reviewed image builds")
    modes.add_argument("--observe-config", action="store_true", help="capture a read-only native configuration projection")
    args = parser.parse_args()
    packet = json.loads(Path(args.spec).read_bytes())
    identity(packet["new"])
    require(isinstance(packet.get("image_tags"), dict) and packet["image_tags"], "build tags required")
    if args.observe_config:
        result = capture_fleet_projection(packet, args.output)
        print(json.dumps({"mode": "observed-config", "receipt": result["receipt"]}))
        return
    if not args.prepare:
        print(json.dumps({"mode": "preview", "selected": packet["new"],
                          "services": sorted(packet["image_tags"]), "output": args.output}))
        return
    result = prepare_images(packet, Git(packet["programs"]["native_git"]), args.output)
    print(json.dumps({"mode": "prepared", "services": sorted(row["service"] for row in result["images"]),
                      "manifest": str(Path(args.output) / "prepared-images.json")}))


def qualify_mutable_limits(compose, host):
    """Refuse drift in Docker update's mutable resource and restart settings.

    This supports the current Compose profile and refuses unsupported nondefault
    CPU, cpuset, memory-reservation and block-I/O settings rather than guessing.
    """
    require(isinstance(compose, dict) and isinstance(host, dict), "mutable limit inputs missing")
    unsupported = ("cpu_period", "cpu_quota", "cpu_rt_period", "cpu_rt_runtime", "cpu_shares", "cpuset",
                   "mem_reservation", "blkio_config")
    require(all(compose.get(key) in (None, 0, "", {}) for key in unsupported),
            "unsupported Compose mutable limit")
    for key in ("CpuPeriod", "CpuQuota", "CpuRealtimePeriod", "CpuRealtimeRuntime", "CpuShares",
                "MemoryReservation", "BlkioWeight"):
        require(type(host.get(key)) is int and host[key] == 0, "live unsupported mutable limit differs")
    require(host.get("CpusetCpus") == "" and host.get("CpusetMems") == "",
            "live cpuset limit differs")
    try:
        cpu = Decimal(str(compose.get("cpus") or 0)) * 1000000000
        memory = int(compose.get("mem_limit") or 0)
        swap_value = compose.get("memswap_limit")
        require(swap_value is not None or memory == 0, "implicit memory swap limit refused")
        swap = int(swap_value or 0)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("invalid resolved mutable limit") from exc
    require(cpu.is_finite() and cpu == cpu.to_integral_value() and cpu >= 0 and memory >= 0 and swap >= -1
            and type(host.get("NanoCpus")) is int and host["NanoCpus"] == int(cpu)
            and type(host.get("Memory")) is int and host["Memory"] == memory
            and type(host.get("MemorySwap")) is int and host["MemorySwap"] == swap,
            "live CPU or memory limit differs")
    pids = compose.get("pids_limit")
    require((pids is None and host.get("PidsLimit") is None)
            or (type(pids) is int and host.get("PidsLimit") == pids), "live pids limit differs")
    require(host.get("RestartPolicy") == {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "live restart policy differs")


def project_expected_configuration(container, old_image, new_image, explicit_keys, allowed_changed,
                                   explicit_labels, selected_sha, *, expected_config_files, expected_replace):
    """Compute a private pre-mutation projection from actual Docker inspect data.

    Raw environment and mount values remain in memory. This refuses image
    configuration drift other than the expressly reviewed image-default keys.
    """
    require(all(isinstance(v, dict) and isinstance(v.get("Config"), dict)
                for v in (container, old_image, new_image)), "incomplete Docker inspection")
    old, new = copy.deepcopy(old_image["Config"]), copy.deepcopy(new_image["Config"])
    old_env, new_env = old.pop("Env", None), new.pop("Env", None)
    old_labels, new_labels = old.pop("Labels", None) or {}, new.pop("Labels", None) or {}
    require(isinstance(old_labels, dict) and isinstance(new_labels, dict), "invalid image labels")
    live_labels = container["Config"].get("Labels")
    require(isinstance(live_labels, dict), "live container labels missing")
    generated = {"com.docker.compose.replace", "com.docker.compose.project.config_files"}
    require(isinstance(explicit_labels, set) and not generated.intersection(old_labels)
            and not generated.intersection(new_labels)
            and live_labels.get("com.docker.compose.replace") in (None, expected_replace)
            and not generated.intersection(explicit_labels),
            "Compose-generated image or prior replacement label requires separate review")
    for key in ("com.docker.compose.project", "com.docker.compose.service", "com.docker.compose.version"):
        previous, current = old_labels.pop(key, None), new_labels.pop(key, None)
        require((previous is None or previous == live_labels.get(key))
                and (current is None or current == live_labels.get(key)),
                "image Compose label differs from live project")
    old_revision = old_labels.pop("org.opencontainers.image.revision", None)
    new_revision = new_labels.pop("org.opencontainers.image.revision", None)
    require(isinstance(selected_sha, str) and re.fullmatch(r"[0-9a-f]{40}", selected_sha)
            and new_revision == selected_sha and isinstance(explicit_labels, set)
            and "org.opencontainers.image.revision" not in explicit_labels,
            "image revision label is not independently qualified")
    require(old == new and old_labels == new_labels, "unreviewed image configuration changed")
    require(isinstance(allowed_changed, set), "reviewed image default scope missing")
    require(isinstance(new_env, list), "new image defaults missing")
    defaults = {}
    for item in new_env:
        require(isinstance(item, str) and "=" in item, "invalid new image default")
        key, value = item.split("=", 1)
        require(key and key not in defaults, "duplicate image default")
        defaults[key] = value
    projected = copy.deepcopy(container)
    old_live = container["Config"].get("Env")
    require(isinstance(old_live, list), "live container environment missing")
    new_live = []
    for item in old_live:
        require(isinstance(item, str) and "=" in item, "invalid live container environment")
        key = item.split("=", 1)[0]
        require(key not in allowed_changed or key in defaults, "reviewed default missing")
        new_live.append(key + "=" + defaults[key] if key in allowed_changed else item)
    changed = qualify_environment_transition(old_live, new_live, old_env, new_env,
                                             explicit_keys, allowed_changed)
    projected["Config"]["Env"] = new_live
    labels = projected["Config"].get("Labels")
    require(isinstance(labels, dict) and labels.get("org.opencontainers.image.revision") == old_revision,
            "live revision label differs from prior image")
    require(isinstance(labels.get("com.docker.compose.project.config_files"), str)
            and isinstance(expected_config_files, str) and expected_config_files
            and isinstance(expected_replace, str) and expected_replace,
            "reviewed Compose config-file label missing")
    labels["com.docker.compose.project.config_files"] = expected_config_files
    labels["com.docker.compose.replace"] = expected_replace
    labels["org.opencontainers.image.revision"] = new_revision
    return {"config_sha256": snapshot_container(projected)["config_sha256"], "changed_keys": changed}


def current_compose_files(containers, services, native_root):
    """Read the affected services' current Compose stack and its file bytes."""
    require(isinstance(containers, list) and isinstance(services, set) and services
            and services <= set(SERVICES), "invalid Compose service scope")
    expected_root = canonical_path(native_root).casefold()
    rows = {}
    for container in containers:
        config = container.get("Config") if isinstance(container, dict) else None
        labels = config.get("Labels") if isinstance(config, dict) else None
        require(isinstance(labels, dict), "Compose labels missing")
        service = labels.get("com.docker.compose.service")
        if service not in services:
            continue
        require(service not in rows and labels.get("com.docker.compose.project") == "firecrawl"
                and canonical_path(labels.get("com.docker.compose.project.working_dir")).casefold() == expected_root,
                "Compose project or service identity differs")
        text = labels.get("com.docker.compose.project.config_files")
        require(isinstance(text, str) and text, "Compose config files missing")
        files = text.split(",")
        keys = [canonical_path(path).casefold() for path in files]
        require(len(set(keys)) == len(keys), "duplicate Compose config-file path")
        rows[service] = (files, keys)
    require(set(rows) == services, "affected Compose services missing")
    first = rows[min(services)]
    require(all(row[1] == first[1] for row in rows.values()), "affected Compose overlay stacks differ")
    # Bytes may contain secrets; return only their digests alongside original
    # file names, never their contents.
    return {"files": first[0], "sha256": [sha(read_file(path)) for path in first[0]]}


def expected_overlay_config_files(current_files, override_path):
    """Append a new override once, or reuse an existing final override."""
    require(isinstance(current_files, str) and current_files and isinstance(override_path, str)
            and override_path, "Compose config-file paths missing")
    files = current_files.split(",")
    require(all(files), "Compose config-file paths invalid")
    normalized = [canonical_path(path).casefold() for path in files]
    override = canonical_path(override_path).casefold()
    require(len(normalized) == len(set(normalized)), "duplicate Compose config-file path")
    if override in normalized:
        require(normalized[-1] == override, "updater override is not the final Compose file")
        return current_files
    return current_files + "," + override_path


def qualify_fleet_projection(containers, old_images, new_images, compose, packet):
    """Derive, rather than accept, five-container prestate and expected hashes.

    All inspect and Compose objects are supplied by the owning fresh observation;
    raw configuration is never returned or written by this function.
    """
    require(isinstance(containers, list) and len(containers) == 5 and isinstance(compose, dict),
            "five inspected containers and resolved Compose required")
    services = {"api", "playwright-service", "nuq-postgres", "redis", "rabbitmq"}
    before = [snapshot_container(value) for value in containers]
    indexed = unique(before, "service")
    require(set(indexed) == services and all(row["status"] == "running" for row in before),
            "running Compose fleet differs")
    require(all(indexed[name]["restart_policy"] == "unless-stopped" for name in packet.get("expected_images", {})),
            "reviewed restart policy differs")
    require(all(value.get("Config", {}).get("Labels", {}).get("com.docker.compose.project") == compose.get("name")
                for value in containers), "container Compose project differs")
    expected = packet.get("expected_images")
    allowed = packet.get("allowed_image_default_changes")
    require(isinstance(expected, dict) and expected and set(expected) <= set(SERVICES)
            and isinstance(allowed, dict) and set(allowed) == set(expected)
            and set(old_images) == set(new_images) == set(expected), "image projection scope differs")
    actual = {value["Config"]["Labels"]["com.docker.compose.service"]: value for value in containers}
    result = {}
    for service, image in expected.items():
        old, new = old_images[service], new_images[service]
        require(old.get("Id") == indexed[service]["image"] and new.get("Id") == image,
                "inspected image does not match selected fleet")
        service_config = compose.get("services", {}).get(service)
        require(isinstance(service_config, dict), "resolved service missing")
        qualify_mutable_limits(service_config, actual[service].get("HostConfig"))
        environment, labels = service_config.get("environment") or {}, service_config.get("labels") or {}
        require(isinstance(environment, dict) and isinstance(labels, dict)
                and isinstance(allowed[service], list) and len(set(allowed[service])) == len(allowed[service]),
                "resolved explicit configuration missing")
        live_env = {}
        for entry in actual[service]["Config"]["Env"]:
            require(isinstance(entry, str) and "=" in entry, "invalid live environment")
            key, value = entry.split("=", 1)
            require(key not in live_env, "duplicate live environment")
            live_env[key] = value
        require(all(value is not None and key in live_env and live_env[key] == str(value)
                    for key, value in environment.items()), "pending explicit environment change refused")
        require(all(key in actual[service]["Config"]["Labels"]
                    and actual[service]["Config"]["Labels"][key] == str(value)
                    for key, value in labels.items()), "pending explicit label change refused")
        current_files = actual[service]["Config"]["Labels"].get("com.docker.compose.project.config_files")
        require(isinstance(current_files, str)
                and [canonical_path(path).casefold() for path in current_files.split(",")]
                == [canonical_path(path).casefold() for path in packet["compose_files"]],
                "running Compose config-file labels differ")
        reviewed_path = windows_path(local_path(packet["compose_override_path"])).replace("/", "\\")
        expected_files = expected_overlay_config_files(current_files, reviewed_path)
        number = actual[service]["Config"]["Labels"].get("com.docker.compose.container-number")
        require(isinstance(number, str) and re.fullmatch(r"[1-9][0-9]*", number)
                and actual[service].get("Name") == "/" + compose["name"] + "-" + service + "-" + number,
                "running Compose container name differs")
        result[service] = project_expected_configuration(actual[service], old, new, set(environment),
                                                         set(allowed[service]), set(labels), packet["new"]["sha"],
                                                         expected_config_files=expected_files,
                                                         expected_replace=service + "-" + number)
    return {"before": sorted(before, key=lambda row: row["service"]),
            "expected_after_config": {name: row["config_sha256"] for name, row in result.items()},
            "changed_image_default_keys": {name: row["changed_keys"] for name, row in result.items()}}


def reviewed_override(tags):
    """Construct the only allowed Compose overlay: selected image and restart."""
    require(isinstance(tags, dict) and tags and set(tags) <= set(SERVICES), "invalid override services")
    lines = ["services:"]
    for service, tag in sorted(tags.items()):
        require(isinstance(tag, str) and re.fullmatch(r"firecrawl-" + re.escape(service)
                + r":qa-[a-z0-9][a-z0-9-]{7,80}", tag), "invalid prepared image tag")
        lines += [f"  {service}:", f"    image: {json.dumps(tag)}", "    restart: unless-stopped"]
    return ("\n".join(lines) + "\n").encode()


def compare_compose_overlay(before, after, tags):
    """Refuse every resolved Compose change except reviewed image/restart."""
    old, new = copy.deepcopy(before), copy.deepcopy(after)
    require(isinstance(old, dict) and isinstance(new, dict), "resolved Compose configuration missing")
    for service, tag in tags.items():
        old_service = old.get("services", {}).get(service)
        new_service = new.get("services", {}).get(service)
        require(isinstance(old_service, dict) and isinstance(new_service, dict)
                and new_service.get("image") == tag and new_service.get("restart") == "unless-stopped",
                "resolved overlay image or restart differs")
        for key in ("image", "restart"):
            old_service.pop(key, None)
            new_service.pop(key, None)
    require(old == new, "Compose overlay changes unreviewed configuration")


def capture_fleet_projection(packet, output):
    """Read the native Compose project and publish a sanitized configuration plan.

    This is read-only except for new evidence files. Docker inspect and resolved
    Compose configuration can contain secrets and remain only in memory.
    """
    root = local_path(packet["roots"]["native"])
    native_root = windows_path(root)
    docker = local_path(packet["programs"]["native_docker"])
    require(root.is_dir() and docker.is_file() and str(docker).lower().endswith(".exe")
            and all(not p.is_symlink() for p in (root, *root.parents, docker, *docker.parents)),
            "native project or Docker executable unavailable")
    verify_native_docker(packet)
    compose_version = verify_native_compose(packet)
    output = Path(output)
    require(output.is_absolute() and str(output).startswith("/mnt/")
            and all(not p.is_symlink() for p in output.parents)
            and root not in output.parents, "exclusive native-mapped output outside runtime required")
    output.mkdir(mode=0o700)
    expected_files = [native_root + "/docker-compose.yaml", native_root + "/docker-compose.override.yaml"]
    require(packet.get("compose_files") == expected_files
            and all((root / name).is_file() and not (root / name).is_symlink()
                    for name in ("docker-compose.yaml", "docker-compose.override.yaml")),
            "original native Compose files differ")
    original_compose = {path: read_file(path) for path in expected_files}
    repo = local_path(packet["roots"]["development"])
    require(repo.is_absolute() and (repo / ".git").is_dir(),
            "selected source checkout unavailable")
    git = Git(packet["programs"]["native_git"])
    require(git.text(repo, "rev-parse", packet["new"]["sha"] + "^{tree}") == packet["new"]["tree"],
            "selected Compose commit tree differs")
    selected_compose = git.command(repo, "show", packet["new"]["sha"] + ":docker-compose.yaml")
    require(selected_compose == original_compose[expected_files[0]],
            "selected Compose source differs from running project")
    def run_json(args):
        if args and args[0] == "compose":
            require(verify_native_compose(packet) == compose_version, "native Compose version changed")
        result = _docker_run(packet, args, check=False, cwd=root, capture_output=True, timeout=120)
        if args and args[0] == "compose":
            require(verify_native_compose(packet) == compose_version, "native Compose version changed")
        require(result.returncode == 0, "native Docker read failed")
        try:
            return json.loads(result.stdout)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("invalid native Docker JSON") from exc

    listed = _docker_run(packet, ["ps", "--no-trunc", "-q", "--filter", "label=com.docker.compose.project=firecrawl"],
                         check=False, cwd=root, capture_output=True, timeout=60)
    require(listed.returncode == 0, "native Docker fleet listing failed")
    ids = listed.stdout.decode().split()
    require(len(ids) == 5 and len(set(ids)) == 5, "native Compose fleet differs")
    containers = run_json(["inspect", *ids])
    require(isinstance(containers, list) and len(containers) == 5
            and {row.get("Id") for row in containers} == set(ids), "fleet inspection differs")
    for row in containers:
        labels = row.get("Config", {}).get("Labels", {})
        require(labels.get("com.docker.compose.project.working_dir", "").replace("\\", "/").lower()
                == native_root.lower(), "Compose project root differs")
        require(labels.get("com.docker.compose.version") == compose_version,
                "live container Compose version differs")
    compose = ["compose", "--project-directory", native_root, "--project-name", "firecrawl"]
    for path in expected_files:
        compose += ["-f", path]
    before = run_json(compose + ["config", "--format", "json"])
    require(before.get("name") == "firecrawl", "resolved Compose project differs")
    require(verify_native_compose(packet) == compose_version, "native Compose version changed")
    hashes = _docker_run(packet, compose + ["config", "--hash", "*"], check=False, cwd=root,
                         capture_output=True, timeout=120)
    require(verify_native_compose(packet) == compose_version, "native Compose version changed")
    require(hashes.returncode == 0, "Compose configuration hash read failed")
    hash_rows = [line.split() for line in hashes.stdout.decode().splitlines()]
    require(all(len(row) == 2 and re.fullmatch(r"[0-9a-f]{64}", row[1]) for row in hash_rows)
            and len({row[0] for row in hash_rows}) == len(hash_rows), "Compose configuration hashes invalid")
    hash_map = dict(hash_rows)
    for row in containers:
        labels = row["Config"]["Labels"]
        service = labels.get("com.docker.compose.service")
        require(labels.get("com.docker.compose.config-hash") == hash_map.get(service),
                "running container was created from a different Compose configuration")
    tags = packet.get("image_tags")
    require(isinstance(packet.get("expected_images"), dict)
            and isinstance(tags, dict) and set(tags) == set(packet["expected_images"]),
            "selected image and tag scopes differ")
    overlay = reviewed_override(tags)
    override_path = output / "maintenance.override.yaml"
    override_ref = _write_new(override_path, overlay)
    reviewed_path = local_path(packet["compose_override_path"])
    require(reviewed_path.is_absolute() and str(reviewed_path).startswith("/mnt/") and root not in reviewed_path.parents,
            "reviewed Compose overlay path must be native mapped and outside runtime")
    require(read_file(reviewed_path) == overlay, "reviewed Compose overlay bytes differ")
    native_override = windows_path(reviewed_path)
    after = run_json(compose + ["-f", native_override, "config", "--format", "json"])
    compare_compose_overlay(before, after, tags)
    by_service = {row["Config"]["Labels"]["com.docker.compose.service"]: row for row in containers}
    old_images, new_images = {}, {}
    for service, image_id in packet["expected_images"].items():
        old = run_json(["image", "inspect", by_service[service]["Image"]])
        new = run_json(["image", "inspect", tags[service]])
        require(isinstance(old, list) and len(old) == 1 and isinstance(new, list) and len(new) == 1
                and new[0].get("Id") == image_id, "image inspect changed")
        old_images[service], new_images[service] = old[0], new[0]
    projection = qualify_fleet_projection(containers, old_images, new_images, before, packet)
    rechecked = run_json(["inspect", *ids])
    require(isinstance(rechecked, list) and len(rechecked) == 5
            and sorted((snapshot_container(value) for value in rechecked), key=lambda row: row["service"])
            == projection["before"], "fleet changed during projection")
    for service, tag in tags.items():
        selected_image = run_json(["image", "inspect", tag])
        require(isinstance(selected_image, list) and len(selected_image) == 1
                and selected_image[0].get("Id") == packet["expected_images"][service],
                "prepared tag changed during projection")
    require(all(read_file(path) == raw for path, raw in original_compose.items()),
            "original Compose source changed during projection")
    verify_native_docker(packet)
    require(verify_native_compose(packet) == compose_version, "native Compose version changed")
    before_ref = _write_new(output / "container-config-before.json",
                            json.dumps(projection["before"], sort_keys=True, indent=2).encode())
    expected_ref = _write_new(output / "expected-after-config.json",
                              json.dumps(projection["expected_after_config"], sort_keys=True, indent=2).encode())
    sources = {path: {"sha256": sha(raw), "bytes": len(raw)} for path, raw in original_compose.items()}
    sources["config_hashes"] = {name: hash_map[name] for name in sorted(by_service)}
    sources["compose_plugin"] = {**packet["native_compose"], "version": compose_version}
    sources_ref = _write_new(output / "compose-sources.json", json.dumps(sources, sort_keys=True, indent=2).encode())
    result = {"schema": "cre-image-configuration-projection-v1", "identity": packet["new"],
              "docker_sha256": packet["native_docker_sha256"],
              "container_config_before": before_ref, "expected_after_config": expected_ref,
              "compose_override": override_ref, "compose_sources": sources_ref,
              "changed_image_default_keys": projection["changed_image_default_keys"]}
    receipt = _write_new(output / "configuration-projection.json", json.dumps(result, sort_keys=True, indent=2).encode())
    return {**result, "receipt": receipt}


def validate_fleet_projection(packet, evidence, observed):
    """Compare a newly observed projection with immutable reviewed evidence."""
    require(isinstance(observed, dict) and observed.get("schema") == "cre-image-configuration-projection-v1"
            and observed.get("identity") == packet["new"]
            and observed.get("docker_sha256") == packet.get("native_docker_sha256"),
            "fresh image projection identity differs")
    verify_native_docker(packet)
    for role in ("container_config_before", "expected_after_config", "compose_override", "compose_sources"):
        current = _payload(evidence, observed[role])
        require(current == evidence.raw(role), f"fresh {role} differs from reviewed evidence")
    require(evidence.raw("compose_override") == reviewed_override(packet["image_tags"]),
            "reviewed Compose override differs")
    require(canonical_path(evidence.inputs["compose_override"]["path"]).casefold()
            == canonical_path(packet["compose_override_path"]).casefold(), "reviewed Compose overlay path differs")
    require(evidence.obj("expected_after_config") == packet.get("expected_after_config"),
            "packet expected configuration differs from derived evidence")
    require(observed.get("changed_image_default_keys") == packet.get("allowed_image_default_changes"),
            "reviewed image-default scope differs")
    evidence.recheck()
    verify_native_docker(packet)


def _payload(evidence, ref):
    require(isinstance(ref, dict) and set(ref) == {"path", "sha256", "bytes"}
            and isinstance(ref["path"], str) and ref["path"]
            and isinstance(ref["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", ref["sha256"])
            and type(ref["bytes"]) is int and ref["bytes"] >= 0,
            "invalid prepared-image artifact reference")
    raw = evidence.read(ref["path"])
    require(len(raw) == ref["bytes"] and sha(raw) == ref["sha256"],
            "prepared-image artifact changed")
    prior = evidence.original_payloads.get(ref["path"])
    require(prior is None or prior == raw, "prepared-image artifact reused with different bytes")
    evidence.original_payloads[ref["path"]] = raw
    return raw


def _validate_prepared_images(packet, evidence, git, source_repo, *, live):
    selected = packet["new"]
    identity(selected)
    build_source = packet.get("build_source")
    require(isinstance(build_source, dict) and set(build_source) == {"root", "identity"}
            and build_source["identity"] == selected, "build source binding differs")
    build_root = local_path(build_source["root"])
    repo = local_path(source_repo)
    if live:
        others = [local_path(packet["roots"][name]) for name in ("development", "native", "runtime")]
        require(repo == build_root and repo.is_absolute() and not str(repo).startswith("/mnt/")
                and (repo / ".git").is_dir() and not (repo / ".git").is_symlink()
                and all(not p.is_symlink() for p in (repo, *repo.parents))
                and all(repo != p and repo not in p.parents and p not in repo.parents for p in others),
                "clean standalone build source role required")
        git.hooks(repo)
        require(git.text(repo, "rev-parse", "HEAD") == selected["sha"]
                and git.text(repo, "rev-parse", "HEAD^{tree}") == selected["tree"]
                and not git.command(repo, "status", "--porcelain=v1", "--untracked-files=all"),
                "build source no longer matches selected identity")
    images = packet.get("expected_images")
    tags = packet.get("image_tags")
    require(isinstance(images, dict) and images and isinstance(tags, dict)
            and set(images) == set(tags) and set(images) <= set(SERVICES),
            "prepared-image service set differs")
    manifest = evidence.obj("prepared_images")
    if live:
        verify_native_docker(packet)
    require(isinstance(manifest, dict) and set(manifest) == {"schema", "identity", "project_root", "images", "docker_sha256", "source_root"}
            and manifest["schema"] == "cre-prepared-images-v1"
            and manifest["identity"] == selected
            and manifest["docker_sha256"] == packet["native_docker_sha256"]
            and manifest["source_root"] == str(build_root)
            and manifest["project_root"] == packet["roots"]["native"],
            "prepared-image manifest identity differs")
    rows = unique(manifest["images"], "service")
    require(set(rows) == set(images), "prepared-image inventory differs")
    for service, row in rows.items():
        require(set(row) == {"service", "identity", "tree", "dockerfile", "tag", "image", "exit_code",
                             "stdout_eof", "stderr_eof", "log", "stderr", "inspect", "context", "archive_sha256",
                             "command", "started_at", "finished_at"},
                "prepared-image row shape differs")
        tag, image = tags[service], images[service]
        require(isinstance(tag, str) and re.fullmatch(r"firecrawl-[a-z-]+:qa-[a-z0-9][a-z0-9-]{7,80}", tag)
                and isinstance(image, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", image),
                "invalid prepared image identity")
        require(row["identity"] == selected and row["tag"] == tag and row["image"] == image
                and row["dockerfile"] == SERVICES[service] + "/Dockerfile"
                and type(row["exit_code"]) is int and row["exit_code"] == 0
                and row["stdout_eof"] is True and row["stderr_eof"] is True,
                "prepared-image build did not settle")
        try:
            started = datetime.fromisoformat(row["started_at"])
            finished = datetime.fromisoformat(row["finished_at"])
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid prepared image clocks") from exc
        require(started.utcoffset() is not None and finished.utcoffset() is not None and started <= finished,
                "prepared image clocks differ")
        require(row["tree"] == git.text(repo, "rev-parse",
                                          selected["sha"] + ":" + SERVICES[service]),
                "prepared-image context tree differs")
        archive = _source_archive(repo, selected, service)
        _verify_archive_tree(git, repo, selected, service, archive)
        require(row["archive_sha256"] == sha(archive), "prepared source archive differs")
        if callable(getattr(git, "record_archive", None)):
            git.record_archive(repo, selected, service, row["archive_sha256"])
        require(_payload(evidence, row["context"]) == _context_tar(archive, service),
                "prepared Docker context differs from selected source")
        command = [str(local_path(packet["programs"]["native_docker"])), "--host", ENGINE,
                   "build", "--progress", "plain",
                   "--label", "org.opencontainers.image.revision=" + selected["sha"]]
        if service == "api":
            command += ["--build-arg", "GIT_SHA=" + selected["sha"]]
        command += ["-t", tag, "-"]
        require(row["command"] == command, "prepared build command differs")
        _payload(evidence, row["log"])
        _payload(evidence, row["stderr"])
        try:
            inspected = json.loads(_payload(evidence, row["inspect"]))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("invalid image inspect evidence") from exc
        require(isinstance(inspected, dict) and inspected.get("Id") == image
                and inspected.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision")
                == selected["sha"], "prepared-image inspect identity differs")
    evidence.recheck()
    if live:
        verify_native_docker(packet)
        require(git.text(repo, "rev-parse", "HEAD") == selected["sha"]
                and git.text(repo, "rev-parse", "HEAD^{tree}") == selected["tree"]
                and not git.command(repo, "status", "--porcelain=v1", "--untracked-files=all"),
                "build source changed during validation")
    return manifest


def validate_prepared_images(packet, evidence, git):
    """Qualify a reviewed image receipt against fresh source and Docker identity."""
    return _validate_prepared_images(packet, evidence, git, packet["build_source"]["root"], live=True)


def validate_prepared_image_history(packet, evidence, git, source_repo):
    """Revalidate retained image artifacts against historical Git objects only."""
    return _validate_prepared_images(packet, evidence, git, source_repo, live=False)


if __name__ == "__main__":
    main()
