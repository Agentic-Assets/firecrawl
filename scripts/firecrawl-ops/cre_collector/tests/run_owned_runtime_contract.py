"""Opt-in cached-image Docker stop contract; no collector, database, or HTTP.

Run only from a separate git clone. Three disposable containers run sleep with
    no network/ports/persistent volumes; the unrelated sentinel remains running. No image
pull, existing project selection, or shared-runtime mutation is performed.
"""

import json
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cre_owned_runtime import DockerRuntime, capture_runtime, stop_runtime


def command(*args):
    return subprocess.check_output(["docker", *args], text=True, timeout=10).strip()


def main():
    image = "postgres:18.6-alpine"
    command("image", "inspect", image, "--format", "{{.Id}}")
    context = command("context", "show")
    token = uuid.uuid4().hex
    project = "firecrawl-cre-contract-" + token
    ids = []
    cleanup_errors = []
    try:
        for service in ("api", "playwright-service", "sentinel"):
            selected_project = project if service != "sentinel" else "sentinel-" + token
            container_id = command(
                "--context",
                context,
                "create",
                "--pull",
                "never",
                "--name",
                project + "-" + service,
                "--network",
                "none",
                "--cpus",
                "0.1",
                "--memory",
                "64m",
                "--pids-limit",
                "16",
                "--tmpfs",
                "/var/lib/postgresql",
                "--entrypoint",
                "sh",
                "--label",
                "com.docker.compose.project=" + selected_project,
                "--label",
                "com.docker.compose.service=" + service,
                "--label",
                "agenticassets.contract-owner=" + token,
                image,
                "-c",
                "exec sleep 86400",
            )
            ids.append(container_id)
            command("--context", context, "start", container_id)
        runtime = DockerRuntime(context)
        identity = capture_runtime(
            runtime,
            project,
            dict(zip(("api", "playwright-service"), ids[:2])),
        )
        result = stop_runtime(runtime, identity)
        sentinel_running = (
            command(
                "--context",
                context,
                "inspect",
                "--format",
                "{{.State.Running}}",
                ids[2],
            )
            == "true"
        )
        if not result["ok"] or not sentinel_running:
            raise RuntimeError("owned stop or sentinel preservation failed")
        print(
            json.dumps(
                {
                    "contract": "owned_runtime_stop",
                    "selected_services": 2,
                    "fresh_stopped": result["ok"],
                    "sentinel_running": sentinel_running,
                    "elapsed_seconds": result["elapsed_seconds"],
                }
            )
        )
    finally:
        # Only IDs returned by our successful create calls, never container names.
        for container_id in ids:
            try:
                subprocess.run(
                    [
                        "docker",
                        "--context",
                        context,
                        "rm",
                        "--force",
                        "--volumes",
                        container_id,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=True,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                cleanup_errors.append(type(exc).__name__)
        if cleanup_errors:
            raise RuntimeError("contract cleanup could not be confirmed")


if __name__ == "__main__":
    main()
