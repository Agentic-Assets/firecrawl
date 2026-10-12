#!/usr/bin/env bash
# Opt-in: exact generated queries/upsert expression, synthetic data, owned PG17.
# No external database URL, network, image pull, or persistent volume is used.
set -euo pipefail
COLLECTOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 - "$COLLECTOR_DIR" "$@" <<'PY'
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

collector = Path(sys.argv[1])
if sys.argv[2:] not in ([], ["--psf-only"]):
    sys.exit("Only --psf-only is accepted.")
owner = uuid.uuid4().hex
name = f"firecrawl-quality-contract-{owner}"
label = "firecrawl.quality-contract-owner"
container_id = None
creation_attempted = False
exit_status = 1


def docker(*args, timeout=10, input=None, check=True):
    result = subprocess.run(
        ["docker", *args], input=input, capture_output=True, text=True,
        timeout=timeout, check=False,
    )
    if check and result.returncode:
        raise RuntimeError("Disposable PostgreSQL Docker command failed.")
    return result


def cleanup():
    # Creation can succeed server-side even if the client response times out.
    # Resolve only this UUID's label-bound container; never remove by name.
    target = container_id
    if target is None:
        result = docker("inspect", name, timeout=5, check=False)
        if result.returncode:
            if creation_attempted:
                raise RuntimeError("Disposable PostgreSQL creation settlement is unknown.")
            return
        records = json.loads(result.stdout)
        if len(records) != 1 or records[0].get("Config", {}).get("Labels", {}).get(label) != owner:
            raise RuntimeError("Disposable PostgreSQL cleanup ownership is unknown.")
        target = records[0].get("Id")
    if not isinstance(target, str) or not re.fullmatch(r"[0-9a-f]{64}", target):
        raise RuntimeError("Disposable PostgreSQL cleanup identity is unknown.")
    docker("rm", "--force", target, timeout=15)
    print("Disposable PostgreSQL cleanup confirmed.")


def terminate(_signum, _frame):
    raise SystemExit(143)


signal.signal(signal.SIGTERM, terminate)
try:
    docker("info", timeout=5)
    image = docker("image", "inspect", os.environ.get("PG_IMAGE", "postgres:17"), timeout=5)
    image_id = json.loads(image.stdout)[0]["Id"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise RuntimeError("Cached PostgreSQL image identity is invalid.")
    creation_attempted = True
    created = docker(
        "create", "--pull", "never", "--name", name, "--label", f"{label}={owner}",
        "--network", "none", "--cpus", "1", "--memory", "512m", "--pids-limit", "128",
        "--tmpfs", "/var/lib/postgresql/data", "--env", "POSTGRES_HOST_AUTH_METHOD=trust",
        image_id, timeout=15,
    )
    container_id = created.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        container_id = None
        raise RuntimeError("Disposable PostgreSQL creation identity is unknown.")
    print(f"Owned disposable PostgreSQL: {container_id}; 1 CPU / 512MiB / 128 PIDs / no network.")
    docker("start", container_id, timeout=10)
    deadline = time.monotonic() + 30
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Disposable PostgreSQL readiness exceeded 30 seconds.")
        ready = docker("exec", container_id, "pg_isready", "-h", "127.0.0.1", "-U", "postgres",
                       timeout=min(2, remaining), check=False)
        if ready.returncode == 0:
            break
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    sys.path.insert(0, str(collector))
    spec = importlib.util.spec_from_file_location(
        "quality_contract", collector / "tests/test_cre_quality_consistency.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sql = module.postgres_contract_sql(psf_only="--psf-only" in sys.argv)
    result = docker("exec", "-i", container_id, "psql", "-X", "-h", "127.0.0.1", "-U", "postgres",
                    "-v", "ON_ERROR_STOP=1", "--quiet", input=sql, timeout=20, check=False)
    if result.returncode:
        print(result.stderr[:2000], file=sys.stderr)
        raise RuntimeError("Synthetic quality consistency SQL contract failed.")
    print(result.stdout.strip())
    exit_status = 0
except KeyboardInterrupt:
    exit_status = 130
except SystemExit as exc:
    exit_status = exc.code
except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
    print(f"Disposable quality contract failed: {type(exc).__name__}.", file=sys.stderr)
finally:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        cleanup()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        print("Disposable PostgreSQL cleanup could not be confirmed.", file=sys.stderr)
        exit_status = 1
sys.exit(exit_status)
PY
