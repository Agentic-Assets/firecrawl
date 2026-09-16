"""Generic Linux-runner environment preflight for the C10 host coordinator.

This is deliberately NOT JLL-specific and NOT a shortcut into
``execute_jll_admission_collection`` or any other admission action. It proves
three independent, reviewable facts about the environment this process is
running in, all without starting the C10 browser sidecar or making any
provider request:

1. This process is on Linux, so ``capacity_c10.host_store.PrivateReceiptStore``
   (intentionally Linux-only, and never to be weakened) will accept a fresh
   root here.
2. A fresh owner-0700 root can actually be created, sealed into, and read back
   through that exact class -- not a reimplementation of it.
3. The host Docker daemon is reachable (Docker-outside-of-Docker over the
   bind-mounted socket) and the C10 compose overlay renders the loopback
   sidecar service with the expected image reference, from this container's
   filesystem view of the repository.

It never calls ``docker compose ... up``: rendering config is sufficient to
prove the compose files, env-var contract, and working directory all line up
from inside the Linux runner, without touching the network at all (not even
the sidecar's own loopback health check). Exit code is 0 only when every
check passes; each failure is reported independently so a partial environment
problem (e.g. missing image, but Linux and docker fine) is diagnosable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# This script lives at capacity_c10/tools/, two levels under cre_collector/.
_COLLECTOR_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _COLLECTOR_ROOT.parents[2]

sys.path.insert(0, str(_COLLECTOR_ROOT))

C10_BROWSER_IMAGE = "firecrawl-playwright-service-c10:local"
_COMPOSE_SERVICE = "playwright-service-c10"


def _check_linux() -> dict[str, Any]:
    ok = (
        sys.platform == "linux"
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
    )
    return {
        "name": "linux_platform",
        "ok": ok,
        "detail": {"sys_platform": sys.platform},
    }


def _check_private_receipt_store() -> dict[str, Any]:
    try:
        from capacity_c10.host_store import PrivateReceiptStore
    except Exception as exc:  # noqa: BLE001 - report, don't crash the whole run
        return {"name": "private_receipt_store", "ok": False, "detail": str(exc)}
    tmp_root = Path(tempfile.mkdtemp(prefix="c10-preflight-")) / "receipts"
    try:
        with PrivateReceiptStore.create(tmp_root) as store:
            artifact = store.seal_json("preflight-probe", {"ok": True})
            body = store.read_sealed(artifact)
            payload = json.loads(body)
            descriptor = store.descriptor()
        ok = payload == {"ok": True} and bool(descriptor["id"])
        return {
            "name": "private_receipt_store",
            "ok": ok,
            "detail": {"root": str(tmp_root), "artifact": artifact},
        }
    except Exception as exc:  # noqa: BLE001
        return {"name": "private_receipt_store", "ok": False, "detail": str(exc)}
    finally:
        shutil.rmtree(tmp_root.parent, ignore_errors=True)


def _check_docker_socket() -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return {
            "name": "docker_socket",
            "ok": False,
            "detail": "docker CLI not found on PATH",
        }
    except subprocess.TimeoutExpired:
        return {
            "name": "docker_socket",
            "ok": False,
            "detail": "docker version timed out",
        }
    ok = result.returncode == 0
    return {
        "name": "docker_socket",
        "ok": ok,
        "detail": (result.stdout or result.stderr).strip(),
    }


def _check_c10_image() -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", C10_BROWSER_IMAGE, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return {
            "name": "c10_image_present",
            "ok": False,
            "detail": "docker CLI not found",
        }
    except subprocess.TimeoutExpired:
        return {
            "name": "c10_image_present",
            "ok": False,
            "detail": "docker image inspect timed out",
        }
    ok = result.returncode == 0 and bool(result.stdout.strip())
    return {
        "name": "c10_image_present",
        "ok": ok,
        "detail": result.stdout.strip() or result.stderr.strip(),
    }


def _check_compose_config() -> dict[str, Any]:
    """Render (never start) the C10 sidecar service from this container's cwd.

    Uses placeholder resource-limit values purely so compose interpolation
    succeeds; a real run supplies the reviewed values itself
    (``DockerComposeSidecar.start`` in ``host_sidecar.py``), never this
    script.
    """
    env = {
        **os.environ,
        "C10_BROWSER_PRIVATE_ENV_FILE": "/dev/null",
        "MAX_CONCURRENT_PAGES": "1",
        "C10_PROFILE_SHA256": "0" * 64,
        "C10_BROWSER_CPUS": "1",
        "C10_BROWSER_PIDS": "100",
        "C10_BROWSER_HOST_PORT": "0",
    }
    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                "docker-compose.yaml",
                "-f",
                "docker-compose.c10.yaml",
                "config",
                "--format",
                "json",
                _COMPOSE_SERVICE,
            ],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        return {
            "name": "compose_config_renders",
            "ok": False,
            "detail": "docker CLI not found",
        }
    except subprocess.TimeoutExpired:
        return {
            "name": "compose_config_renders",
            "ok": False,
            "detail": "docker compose config timed out",
        }
    if result.returncode != 0:
        return {
            "name": "compose_config_renders",
            "ok": False,
            "detail": result.stderr.strip(),
        }
    try:
        rendered = json.loads(result.stdout)
        service = rendered["services"][_COMPOSE_SERVICE]
        ok = service.get("image") == C10_BROWSER_IMAGE
    except Exception as exc:  # noqa: BLE001
        return {"name": "compose_config_renders", "ok": False, "detail": str(exc)}
    return {
        "name": "compose_config_renders",
        "ok": ok,
        "detail": {"image": service.get("image"), "repo_root": str(_REPO_ROOT)},
    }


def _check_canonical_lock_domain() -> dict[str, Any]:
    """Fail when this process is marked as an untrusted CRE lock domain.

    `fcntl.flock` does not coordinate between a macOS host process and a
    process reached through an OrbStack bind mount (see
    LOCK_AUTHORITY_RECOVERY.md). `docker-compose.c10-runner.yaml` sets
    CRE_LOCK_DOMAIN_UNTRUSTED on this container for exactly that reason, and
    `cre_checkpoint_refresh.SharedLock.acquire` refuses whenever it is set.
    Preflight must report failure here too, so it can never say `ok` while
    this container is unsafe for any lock-holding live work.
    """
    untrusted_domain = os.environ.get("CRE_LOCK_DOMAIN_UNTRUSTED", "")
    ok = not untrusted_domain
    return {
        "name": "canonical_lock_domain",
        "ok": ok,
        "detail": {
            "CRE_LOCK_DOMAIN_UNTRUSTED": untrusted_domain or None,
            "note": (
                "unset"
                if ok
                else "this environment cannot safely hold the canonical CRE "
                "lock (flock does not coordinate across the host/container "
                "boundary); SharedLock.acquire also refuses"
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    del argv
    checks = [
        _check_linux(),
        _check_private_receipt_store(),
        _check_docker_socket(),
        _check_c10_image(),
        _check_compose_config(),
        _check_canonical_lock_domain(),
    ]
    overall_ok = all(check["ok"] for check in checks)
    print(json.dumps({"ok": overall_ok, "checks": checks}, indent=2, sort_keys=True))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
