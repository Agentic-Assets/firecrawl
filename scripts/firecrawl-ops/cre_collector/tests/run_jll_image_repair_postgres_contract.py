#!/usr/bin/env python3
"""Run the isolated JLL repair PostgreSQL contracts with the cached image only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

IMAGE = "postgres:17@sha256:2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3"
LABEL_QA = "codex_jll_repair_qa"
LABEL_OWNER = "codex_jll_repair_owner"
TEST = "test_cre_repair_jll_images.py"


def run(args: list[str], *, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, capture_output=True, check=False, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {args[0]}: {result.stderr}")
    return result


def inspect(name: str) -> dict | None:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{json .}}", name],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode:
        error_text = (result.stderr or "").lower()
        if "no such object" in error_text or "no such container" in error_text:
            return None
        raise RuntimeError(f"docker inspect failed for {name}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_terminal_proof(proof_dir: Path, record: dict) -> None:
    path = proof_dir / "terminal-proof.json"
    temporary = proof_dir / ".terminal-proof.json.tmp"
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(record, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(proof_dir, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def run_pytest(command: list[str], *, cwd: Path, env: dict[str, str], log) -> int:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        return process.wait(timeout=900)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        raise
    except KeyboardInterrupt:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        raise


def full_scale_timing(log_path: Path) -> dict | None:
    marker = "JLL_REPAIR_SYNTHETIC_FULL_SCALE "
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(marker):
            record = json.loads(line[len(marker) :])
            if set(record) != {"parents", "images", "candidates", "apply_seconds", "restore_seconds"}:
                raise ValueError("full-scale timing record has an unexpected schema")
            return record
    return None


def owned_record(record: dict | None, name: str, owner: str, expected_id: str | None = None) -> bool:
    if record is None:
        return False
    labels = record["Config"].get("Labels") or {}
    return (
        record["Name"] == f"/{name}"
        and (expected_id is None or record["Id"] == expected_id)
        and labels.get(LABEL_QA) == "isolated-test-only"
        and labels.get(LABEL_OWNER) == owner
        and record["Config"].get("Image") == IMAGE
        and record["HostConfig"].get("NetworkMode") == "none"
        and record["HostConfig"].get("NanoCpus") == 500_000_000
        and record["HostConfig"].get("Memory") == 2 * 1024**3
        and record["HostConfig"].get("PidsLimit") == 64
        and list(record["HostConfig"].get("Tmpfs") or {}) == ["/var/lib/postgresql/data"]
        and "size=1536m" in record["HostConfig"]["Tmpfs"]["/var/lib/postgresql/data"]
        and not record["HostConfig"].get("PortBindings")
        and not record["HostConfig"].get("Binds")
        and all(
            not bindings
            for bindings in (record["NetworkSettings"].get("Ports") or {}).values()
        )
    )


def owned_container(name: str, owner: str, expected_id: str | None = None) -> bool:
    return owned_record(inspect(name), name, owner, expected_id)


def cleanup_owned_container(name: str, owner: str, expected_id: str | None) -> tuple[str, str | None]:
    try:
        record = inspect(name)
        if record is None:
            return "absent", None
        exact_id = record["Id"]
        if not owned_record(record, name, owner, exact_id) or (expected_id and exact_id != expected_id):
            return "refused_identity_mismatch", f"container identity/labels/config changed: {name}"

        cleanup = subprocess.run(
            ["docker", "rm", "--force", exact_id],
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        if cleanup.returncode:
            reconciled = inspect(exact_id)
            if reconciled is None:
                return "removed_after_uncertain_cli_result", None
            return "failed_still_present", cleanup.stderr.strip()
        if inspect(exact_id) is None:
            return "removed", None
        return "failed_still_present", f"container still exists after rm: {name}"
    except BaseException as error:
        return "cleanup_uncertain", f"{type(error).__name__}: {error}"


def main() -> int:
    def interrupt(_number, _frame):
        raise KeyboardInterrupt("runner interrupted; cleaning its exact isolated container")

    old_sigterm = signal.signal(signal.SIGTERM, interrupt)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full-scale",
        action="store_true",
        help="also run one synthetic 345,440-image apply/restore timing probe",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[4]
    test = Path(__file__).with_name(TEST)
    if not test.is_file():
        raise SystemExit(f"missing contract tests: {test}")

    image = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if image.returncode:
        raise SystemExit("the exact pinned PostgreSQL image is not cached; this runner never pulls images")
    if image.stdout.strip() != "sha256:2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3":
        raise SystemExit("cached PostgreSQL image ID differs from the pinned QA image")

    token = uuid.uuid4().hex
    name = f"codex-jll-repair-qa-{token}"
    scratch_root = root / ".scratch"
    scratch_root.mkdir(parents=True, exist_ok=True)
    if scratch_root.is_symlink() or not scratch_root.resolve().is_relative_to(root.resolve()):
        raise SystemExit("scratch root must resolve inside the repository")
    scratch_parent = scratch_root / f"{date.today().isoformat()}-jll-image-repair-postgres"
    scratch_parent.mkdir(parents=True, exist_ok=True)
    if scratch_parent.is_symlink() or not scratch_parent.resolve().is_relative_to(scratch_root.resolve()):
        raise SystemExit("proof parent must resolve inside the new scratch root")
    proof_dir = scratch_parent / token
    proof_dir.mkdir(mode=0o700)
    env = os.environ.copy()
    env["CRE_REPAIR_PG_TEST_CONTAINER"] = name
    env["CRE_REPAIR_PG_TEST_OWNER"] = token
    collector_root = root / "scripts" / "firecrawl-ops" / "cre_collector"
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(collector_root) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    env.pop("CRE_REPAIR_PG_TEST_FULL_SCALE", None)
    if args.full_scale:
        env["CRE_REPAIR_PG_TEST_FULL_SCALE"] = "1"

    created = True
    container_id = None
    status = 1
    terminal_error = None
    cleanup_state = "not_attempted"
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        created_result = run(
            [
                "docker", "run", "--detach", "--name", name,
                "--label", f"{LABEL_QA}=isolated-test-only",
                "--label", f"{LABEL_OWNER}={token}",
                "--network", "none", "--cpus", "0.50", "--memory", "2g",
                "--pids-limit", "64",
                "--tmpfs", "/var/lib/postgresql/data:rw,nosuid,nodev,size=1536m",
                "--env", "POSTGRES_PASSWORD=qa-only",
                "--env", "POSTGRES_DB=postgres",
                IMAGE,
            ]
        )
        container_id = created_result.stdout.strip()
        if not container_id or len(container_id) != 64 or not owned_container(name, token, container_id):
            raise RuntimeError("created container did not match the exact isolated QA contract")

        import time

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "postgres"],
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("isolated PostgreSQL did not become ready within 30 seconds")

        command = [
            sys.executable, "-m", "pytest", str(test), "-q", "-s", "--basetemp", str(proof_dir / "pytest-tmp")
        ]
        try:
            log_path = proof_dir / "runner.log"
            with log_path.open("w", encoding="utf-8") as log:
                completed_returncode = run_pytest(command, cwd=root, env=env, log=log)
                log.flush()
                os.fsync(log.fileno())
            status = completed_returncode
        except subprocess.TimeoutExpired:
            print("PostgreSQL contract pytest exceeded its 900-second deadline", file=sys.stderr)
            status = 124
            terminal_error = "TimeoutExpired: PostgreSQL contract pytest exceeded 900 seconds"
        if log_path.exists():
            print(log_path.read_text(encoding="utf-8"), end="")
    except BaseException as error:
        terminal_error = f"{type(error).__name__}: {error}"
        status = 130 if isinstance(error, KeyboardInterrupt) else 1
        print(terminal_error, file=sys.stderr)
    finally:
        if created:
            cleanup_state, cleanup_detail = cleanup_owned_container(name, token, container_id)
            if cleanup_state in {"removed", "removed_after_uncertain_cli_result"}:
                suffix = " (reconciled)" if cleanup_state == "removed_after_uncertain_cli_result" else ""
                print(f"removed exact owned isolated container {name}{suffix}")
            elif cleanup_state not in {"absent"}:
                detail = f"cleanup {cleanup_state}: {cleanup_detail}"
                terminal_error = f"{terminal_error}; {detail}" if terminal_error else detail
                status = status or 1
                print(detail, file=sys.stderr)
        log_path = proof_dir / "runner.log"
        try:
            scale_timing = full_scale_timing(log_path) if log_path.exists() else None
        except BaseException as error:
            scale_timing = None
            detail = f"timing record {type(error).__name__}: {error}"
            terminal_error = f"{terminal_error}; {detail}" if terminal_error else detail
            status = status or 1
        proof_record = {
            "schema": "jll-image-repair-postgres-runner/v1",
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "exit_code": status,
            "pytest_log_sha256": sha256_file(log_path) if log_path.exists() else None,
            "container_name": name,
            "container_id": container_id,
            "container_cleanup": cleanup_state,
            "container_removed_or_absent": cleanup_state in {"absent", "removed", "removed_after_uncertain_cli_result"},
            "image_reference": IMAGE,
            "image_id": "sha256:2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3",
            "full_scale_requested": bool(args.full_scale),
            "full_scale_timing": scale_timing,
            "source_sha256": sha256_file(collector_root / "cre_repair_jll_images.py"),
            "test_sha256": sha256_file(test),
            "runner_sha256": sha256_file(Path(__file__)),
            "error": terminal_error,
        }
        write_terminal_proof(proof_dir, proof_record)
        print(f"terminal proof: {proof_dir / 'terminal-proof.json'}")
    print(f"proof artifacts: {proof_dir}")
    signal.signal(signal.SIGTERM, old_sigterm)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
