"""Prepared image receipts must bind actual command outputs and selected source."""

import copy
import hashlib
import io
import json
import subprocess
import tarfile

import pytest

from cre_code_handoff_evidence import HandoffError, sha


def test_prepared_images_require_a_bound_settled_build_and_inspection(monkeypatch, tmp_path):
    import cre_runtime_maintenance_build as build

    validate_prepared_images = build.validate_prepared_images

    identity = {"sha": "a" * 40, "tree": "b" * 40}
    source = tmp_path / "source"
    source.mkdir(); (source / ".git").mkdir()
    log = b"build complete\n"
    inspect = json.dumps({"Id": "sha256:" + "c" * 64,
                          "Config": {"Labels": {"org.opencontainers.image.revision": identity["sha"]}}}).encode()
    files = {"/log": log, "/stderr": b"", "/inspect": inspect, "/context": b"context"}
    def ref(path):
        return {"path": path, "sha256": sha(files[path]), "bytes": len(files[path])}
    monkeypatch.setattr(build, "_source_archive", lambda *_: b"archive")
    monkeypatch.setattr(build, "_verify_archive_tree", lambda *_: None)
    monkeypatch.setattr(build, "_context_tar", lambda *_: b"context")
    monkeypatch.setattr(build, "verify_native_docker", lambda *_: None)
    row = {"service": "api", "identity": identity, "tree": "d" * 40,
           "dockerfile": "apps/api/Dockerfile", "tag": "firecrawl-api:qa-candidate-abc",
           "image": "sha256:" + "c" * 64, "exit_code": 0, "stdout_eof": True,
           "stderr_eof": True, "log": ref("/log"), "stderr": ref("/stderr"), "inspect": ref("/inspect"),
           "context": ref("/context"), "started_at": "2026-01-01T00:00:00+00:00",
           "finished_at": "2026-01-01T00:01:00+00:00",
           "archive_sha256": sha(b"archive"),
           "command": ["/mnt/c/docker.exe", "--host", build.ENGINE, "build", "--progress", "plain", "--label",
                       "org.opencontainers.image.revision=" + identity["sha"], "--build-arg",
                       "GIT_SHA=" + identity["sha"], "-t", "firecrawl-api:qa-candidate-abc", "-"]}
    manifest = {"schema": "cre-prepared-images-v1", "identity": identity, "docker_sha256": "e" * 64,
                "source_root": str(source),
                "project_root": "C:/runtime", "images": [row]}
    class Evidence:
        def __init__(self):
            self.original_payloads = {}
        def obj(self, role):
            assert role == "prepared_images"
            return manifest
        def read(self, path):
            return files[path]
        def recheck(self):
            pass
    class Git:
        def hooks(self, _root):
            pass
        def command(self, *_args):
            return b""
        def text(self, root, *args):
            if args == ("rev-parse", "HEAD"):
                return identity["sha"]
            if args == ("rev-parse", "HEAD^{tree}"):
                return identity["tree"]
            assert args == ("rev-parse", identity["sha"] + ":apps/api")
            return "d" * 40
    packet = {"new": identity, "build_source": {"root": str(source), "identity": identity},
              "roots": {"development": "/other/dev", "runtime": "/other/runtime", "native": "C:/runtime"},
              "expected_images": {"api": row["image"]}, "image_tags": {"api": row["tag"]},
              "programs": {"native_docker": "C:/docker.exe"}, "native_docker_sha256": "e" * 64}
    validate_prepared_images(packet, Evidence(), Git())
    def unavailable_docker(*_):
        raise AssertionError("historical verification must not read the current Docker executable")
    monkeypatch.setattr(build, "verify_native_docker", unavailable_docker)
    build.validate_prepared_image_history(packet, Evidence(), Git(), "/later/repository")
    monkeypatch.setattr(build, "verify_native_docker", lambda *_: None)
    original = copy.deepcopy(row)
    for field, value in (("stderr_eof", False), ("exit_code", False), ("archive_sha256", "f" * 64),
                         ("image", "sha256:" + "d" * 64), ("command", ["docker", "build", "."])):
        row.clear(); row.update(copy.deepcopy(original)); row[field] = value
        with pytest.raises(HandoffError):
            validate_prepared_images(packet, Evidence(), Git())
    row.clear(); row.update(original)
    files["/context"] = b"tampered"
    with pytest.raises(HandoffError):
        validate_prepared_images(packet, Evidence(), Git())


def test_projection_allows_only_reviewed_image_defaults_without_exposing_values():
    from cre_runtime_maintenance_build import project_expected_configuration

    container = {"Id": "old", "Image": "old-image", "State": {"Status": "running", "StartedAt": "old"},
                 "Config": {"Env": ["NODE_VERSION=old", "MODEL=private"],
                            "Labels": {"com.docker.compose.service": "api",
                                       "com.docker.compose.project.config_files": "C:\\base.yaml"},
                            "User": "", "Entrypoint": None,
                            "Cmd": [], "WorkingDir": "/app"},
                 "HostConfig": {"RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
                                "Memory": 1, "NanoCpus": 2, "ShmSize": 3, "ReadonlyRootfs": False,
                                "Privileged": False, "SecurityOpt": [], "CapAdd": None, "CapDrop": [],
                                "PortBindings": {}, "NetworkMode": "bridge"},
                 "NetworkSettings": {"Networks": {}}, "Mounts": []}
    old = {"Config": {"Env": ["NODE_VERSION=old"], "Labels": {}, "Cmd": ["start"]}}
    new = {"Config": {"Env": ["NODE_VERSION=new"],
                      "Labels": {"org.opencontainers.image.revision": "a" * 40}, "Cmd": ["start"]}}
    result = project_expected_configuration(container, old, new, {"MODEL"}, {"NODE_VERSION"}, set(), "a" * 40,
                                            expected_config_files="C:\\base.yaml,C:\\reviewed.yaml", expected_replace="api-1")
    assert result["changed_keys"] == ["NODE_VERSION"]
    assert "private" not in repr(result)
    with pytest.raises(HandoffError):
        project_expected_configuration(container, old, new, {"NODE_VERSION"}, {"NODE_VERSION"}, set(), "a" * 40,
                                       expected_config_files="C:\\base.yaml,C:\\reviewed.yaml", expected_replace="api-1")
    new["Config"]["Cmd"] = ["different"]
    with pytest.raises(HandoffError):
        project_expected_configuration(container, old, new, set(), {"NODE_VERSION"}, set(), "a" * 40,
                                       expected_config_files="C:\\base.yaml,C:\\reviewed.yaml", expected_replace="api-1")


def test_projection_binds_reviewed_overlay_config_files_label():
    from cre_runtime_maintenance_build import project_expected_configuration
    from cre_runtime_maintenance_images import snapshot_container

    labels = {"com.docker.compose.service": "api", "com.docker.compose.project.config_files": "C:\\base.yaml"}
    container = {"Id": "old", "Image": "old-image", "State": {"Status": "running", "StartedAt": "old"},
                 "Config": {"Env": [], "Labels": labels, "User": "", "Entrypoint": None,
                            "Cmd": [], "WorkingDir": "/app"},
                 "HostConfig": {"RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
                                "Memory": 0, "NanoCpus": 0, "ShmSize": 0, "ReadonlyRootfs": False,
                                "Privileged": False, "SecurityOpt": [], "CapAdd": None, "CapDrop": [],
                                "PortBindings": {}, "NetworkMode": "bridge"},
                 "NetworkSettings": {"Networks": {}}, "Mounts": []}
    old = {"Config": {"Env": [], "Labels": {}}}
    new = {"Config": {"Env": [], "Labels": {"org.opencontainers.image.revision": "a" * 40}}}
    expected_files = "C:\\base.yaml,C:\\reviewed.yaml"
    projected = project_expected_configuration(container, old, new, set(), set(), set(), "a" * 40,
                                               expected_config_files=expected_files, expected_replace="api-1")
    expected = copy.deepcopy(container)
    expected["Config"]["Labels"]["com.docker.compose.project.config_files"] = expected_files
    expected["Config"]["Labels"]["org.opencontainers.image.revision"] = "a" * 40
    expected["Config"]["Labels"]["com.docker.compose.replace"] = "api-1"
    assert projected["config_sha256"] == snapshot_container(expected)["config_sha256"]
    prior = copy.deepcopy(container)
    prior["Config"]["Labels"]["com.docker.compose.replace"] = "earlier"
    with pytest.raises(HandoffError):
        project_expected_configuration(prior, old, new, set(), set(), set(), "a" * 40,
                                       expected_config_files=expected_files, expected_replace="api-1")
    conflicting = copy.deepcopy(new)
    conflicting["Config"]["Labels"]["com.docker.compose.replace"] = "unreviewed"
    with pytest.raises(HandoffError):
        project_expected_configuration(container, old, conflicting, set(), set(), set(), "a" * 40,
                                       expected_config_files=expected_files, expected_replace="api-1")


def test_prepare_builds_only_source_tar_with_exclusive_receipts(tmp_path, monkeypatch):
    import cre_runtime_maintenance_build as build

    repo, native = tmp_path / "source", tmp_path / "native"
    repo.mkdir(); (repo / ".git").mkdir(); native.mkdir()
    docker = tmp_path / "docker.exe"
    marker = tmp_path / "built"
    docker.write_text("#!/usr/bin/env python3\n"
                      "import json, pathlib, sys\n"
                      f"p=pathlib.Path({str(marker)!r})\n"
                      "if 'ls' in sys.argv: sys.exit(0)\n"
                      "if 'build' in sys.argv:\n"
                      " data=sys.stdin.buffer.read(); p.write_bytes(data); print('built'); sys.exit(0)\n"
                      "if not p.exists(): sys.exit(1)\n"
                      "print(json.dumps([{'Id':'sha256:'+'c'*64,'Config':{'Labels':"
                      "{'org.opencontainers.image.revision':'a'*40}}}]))\n")
    docker.chmod(0o755)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo("apps/api/Dockerfile"); data = b"FROM scratch\n"; info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    monkeypatch.setattr(build, "_source_archive", lambda *_: raw.getvalue())
    monkeypatch.setattr(build, "_verify_archive_tree", lambda *_: None)
    monkeypatch.setattr(build, "windows_path", lambda *_: "C:/native")
    class FakeGit:
        def hooks(self, *_):
            pass
        def text(self, _repo, *args):
            return "a" * 40 if args == ("rev-parse", "HEAD") else "b" * 40
        def command(self, *_args, **_kwargs):
            return b""
    selected = {"sha": "a" * 40, "tree": "b" * 40}
    packet = {"new": selected, "build_source": {"root": str(repo), "identity": selected},
              "roots": {"development": str(tmp_path / "dev"),
                        "runtime": str(tmp_path / "runtime"), "native": str(native)},
              "programs": {"native_docker": str(docker)}, "native_docker_sha256": sha(docker.read_bytes()),
              "image_tags": {"api": "firecrawl-api:qa-test-build"}}
    result = build.prepare_images(packet, FakeGit(), tmp_path / "proof")
    assert result["images"][0]["image"] == "sha256:" + "c" * 64
    assert marker.read_bytes() == (tmp_path / "proof" / "api-context.tar").read_bytes()
    assert (tmp_path / "proof" / "prepared-images.json").exists()
    with pytest.raises(FileExistsError):
        build.prepare_images(packet, FakeGit(), tmp_path / "proof")


def test_overlay_allows_only_selected_image_and_preserved_restart():
    from cre_runtime_maintenance_build import compare_compose_overlay, reviewed_override

    tags = {"api": "firecrawl-api:qa-review-123"}
    old = {"name": "firecrawl", "services": {"api": {"environment": {"MODEL": "private"}}}}
    new = {"name": "firecrawl", "services": {"api": {"environment": {"MODEL": "private"},
                                                        "image": tags["api"], "restart": "unless-stopped"}}}
    compare_compose_overlay(old, new, tags)
    assert b"private" not in reviewed_override(tags)
    new["services"]["api"]["environment"]["MODEL"] = "different"
    with pytest.raises(HandoffError):
        compare_compose_overlay(old, new, tags)


def test_archive_refuses_omitted_or_changed_tracked_blob():
    from cre_runtime_maintenance_build import _verify_archive_tree

    content = b"FROM scratch\n"
    oid = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content, usedforsecurity=False).hexdigest()
    class FakeGit:
        def command(self, *_args):
            return ("100644 blob " + oid + "\tapps/api/Dockerfile\0").encode()
    def archive(data):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            if data is not None:
                info = tarfile.TarInfo("apps/api/Dockerfile")
                info.size = len(data)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
        return raw.getvalue()
    selected = {"sha": "a" * 40}
    _verify_archive_tree(FakeGit(), "/repo", selected, "api", archive(content))
    for altered in (None, b"changed"):
        with pytest.raises(HandoffError):
            _verify_archive_tree(FakeGit(), "/repo", selected, "api", archive(altered))


def test_mutable_docker_limits_match_resolved_compose_and_reviewed_restart():
    from cre_runtime_maintenance_build import qualify_mutable_limits

    compose = {"cpus": 1, "mem_limit": "8589934592", "memswap_limit": "8589934592"}
    host = {"NanoCpus": 1000000000, "CpuPeriod": 0, "CpuQuota": 0, "CpuRealtimePeriod": 0,
            "CpuRealtimeRuntime": 0, "CpuShares": 0, "CpusetCpus": "", "CpusetMems": "",
            "Memory": 8589934592, "MemoryReservation": 0, "MemorySwap": 8589934592,
            "PidsLimit": None, "BlkioWeight": 0,
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0}}
    qualify_mutable_limits(compose, host)
    for key, value in (("Memory", 1), ("CpuQuota", 100), ("RestartPolicy", {"Name": "always", "MaximumRetryCount": 0})):
        changed = dict(host)
        changed[key] = value
        with pytest.raises(HandoffError):
            qualify_mutable_limits(compose, changed)


def test_docker_failure_remains_primary_when_postcheck_also_fails(monkeypatch):
    import cre_runtime_maintenance_build as build

    calls = []
    def verify(_packet):
        calls.append(True)
        if len(calls) == 2:
            raise HandoffError("executable changed")
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("docker", 1)
    monkeypatch.setattr(build, "verify_native_docker", verify)
    monkeypatch.setattr(build.subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        build._docker_run({"programs": {"native_docker": "C:/docker.exe"}}, ["info"], check=False)
