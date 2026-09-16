"""Contracts for the generic C10 Linux-runner environment preflight.

`c10_linux_preflight.py` proves the Linux-runner environment (platform,
PrivateReceiptStore, Docker socket, compose rendering) without ever starting
the C10 sidecar or touching the network. These tests run the real check
functions but replace `subprocess.run` so nothing here needs Docker
installed, and they never call `main()`'s docker-dependent checks for real.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "capacity_c10"
    / "tools"
    / "c10_linux_preflight.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("c10_linux_preflight", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def preflight(monkeypatch: pytest.MonkeyPatch):
    # The module inserts the collector root onto sys.path at import time;
    # reuse a fresh module object per test so monkeypatches never leak.
    module = _load_module()
    yield module
    sys.modules.pop("c10_linux_preflight", None)


def test_paths_resolve_to_the_real_collector_and_repo_roots(preflight):
    assert (preflight._COLLECTOR_ROOT / "capacity_c10").is_dir()
    assert (preflight._COLLECTOR_ROOT / "tests").is_dir()
    assert (preflight._REPO_ROOT / "docker-compose.yaml").is_file()
    assert (preflight._REPO_ROOT / "docker-compose.c10.yaml").is_file()


def test_check_linux_reports_actual_platform(preflight):
    result = preflight._check_linux()
    assert result["name"] == "linux_platform"
    assert result["detail"]["sys_platform"] == sys.platform
    assert result["ok"] == (sys.platform == "linux")


def test_check_private_receipt_store_runs_the_real_class(preflight, tmp_path):
    # On a non-Linux host this must fail closed with the same class's own
    # refusal, not a preflight-specific message -- proving the preflight
    # never reimplements or loosens that check.
    result = preflight._check_private_receipt_store()
    assert result["name"] == "private_receipt_store"
    if sys.platform != "linux":
        assert result["ok"] is False
        assert "Linux" in str(result["detail"])
    else:
        assert result["ok"] is True
        artifact = result["detail"]["artifact"]
        assert artifact["name"].endswith(".sealed")


def test_check_docker_socket_ok_on_success(preflight, monkeypatch):
    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["docker", "version"]
        return SimpleNamespace(returncode=0, stdout="linux/arm64\n", stderr="")

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    result = preflight._check_docker_socket()
    assert result == {"name": "docker_socket", "ok": True, "detail": "linux/arm64"}


def test_check_docker_socket_reports_missing_cli(preflight, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("no docker")

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    result = preflight._check_docker_socket()
    assert result["ok"] is False
    assert "docker CLI not found" in result["detail"]


def test_check_docker_socket_reports_timeout(preflight, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    result = preflight._check_docker_socket()
    assert result["ok"] is False
    assert "timed out" in result["detail"]


def test_check_c10_image_ok_when_inspect_succeeds(preflight, monkeypatch):
    def fake_run(cmd, **kwargs):
        assert cmd[:3] == ["docker", "image", "inspect"]
        assert preflight.C10_BROWSER_IMAGE in cmd
        return SimpleNamespace(returncode=0, stdout="sha256:deadbeef\n", stderr="")

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    result = preflight._check_c10_image()
    assert result["ok"] is True
    assert result["detail"] == "sha256:deadbeef"


def test_check_c10_image_fails_when_image_missing(preflight, monkeypatch):
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="No such image")

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    result = preflight._check_c10_image()
    assert result["ok"] is False
    assert "No such image" in result["detail"]


def test_check_compose_config_only_renders_never_starts(preflight, monkeypatch):
    captured = {}

    rendered = {
        "services": {preflight._COMPOSE_SERVICE: {"image": preflight.C10_BROWSER_IMAGE}}
    }

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return SimpleNamespace(returncode=0, stdout=json.dumps(rendered), stderr="")

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    result = preflight._check_compose_config()

    assert result["ok"] is True
    assert result["detail"]["image"] == preflight.C10_BROWSER_IMAGE
    # Never `up`, `start`, or any lifecycle-mutating subcommand.
    assert "config" in captured["cmd"]
    assert "up" not in captured["cmd"]
    assert captured["cwd"] == preflight._REPO_ROOT
    # Placeholder values only -- never the reviewed production resource
    # limits, which only DockerComposeSidecar.start may supply.
    assert captured["env"]["C10_BROWSER_HOST_PORT"] == "0"


def test_check_compose_config_fails_closed_on_wrong_image(preflight, monkeypatch):
    rendered = {"services": {preflight._COMPOSE_SERVICE: {"image": "wrong:tag"}}}

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(rendered), stderr="")

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    result = preflight._check_compose_config()
    assert result["ok"] is False


def test_main_exits_nonzero_when_any_check_fails(preflight, monkeypatch, capsys):
    monkeypatch.setattr(
        preflight,
        "_check_linux",
        lambda: {"name": "linux_platform", "ok": True, "detail": {}},
    )
    monkeypatch.setattr(
        preflight,
        "_check_private_receipt_store",
        lambda: {"name": "private_receipt_store", "ok": True, "detail": {}},
    )
    monkeypatch.setattr(
        preflight,
        "_check_docker_socket",
        lambda: {"name": "docker_socket", "ok": False, "detail": "x"},
    )
    monkeypatch.setattr(
        preflight,
        "_check_c10_image",
        lambda: {"name": "c10_image_present", "ok": True, "detail": "x"},
    )
    monkeypatch.setattr(
        preflight,
        "_check_compose_config",
        lambda: {"name": "compose_config_renders", "ok": True, "detail": {}},
    )
    exit_code = preflight.main([])
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert len(payload["checks"]) == 5


def test_main_exits_zero_when_every_check_passes(preflight, monkeypatch, capsys):
    for name in (
        "_check_linux",
        "_check_private_receipt_store",
        "_check_docker_socket",
        "_check_c10_image",
        "_check_compose_config",
    ):
        monkeypatch.setattr(
            preflight, name, lambda name=name: {"name": name, "ok": True, "detail": {}}
        )
    exit_code = preflight.main([])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
