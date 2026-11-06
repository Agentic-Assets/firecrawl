"""Synthetic multi-window pipeline against an isolated real PostgreSQL schema."""
import copy
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
def command(*args, text=None, check=True, env=None):
    return subprocess.run(args, input=text, text=True, capture_output=True, check=check, timeout=120, env=env)


def main():
    import cre_ingest as ingest
    from tests.test_colliers_partial_failed_collector_pipeline import (
        test_evidence_only_collector_failure_reaches_partial_pipeline as exercise_pipeline,
    )

    name = "colliers-multi-" + uuid.uuid4().hex[:12]
    # Synthetic artifacts stay on the host; SQL is streamed into the container.
    root = Path(tempfile.mkdtemp(prefix="colliers-multi-"))
    os.environ["TMPDIR"] = str(root)
    tempfile.tempdir = str(root)
    print(f"synthetic fixture evidence: {root}", flush=True)
    # The daemon is outside the test network namespace. Pin a local Unix socket,
    # an empty private Docker config and a local image; never contact a registry.
    docker_config = root / "docker-config"
    docker_config.mkdir(mode=0o700)
    docker_env = {"PATH": "/usr/bin:/bin", "DOCKER_CONFIG": str(docker_config),
                  "DOCKER_HOST": "unix:///var/run/docker.sock"}
    docker_binary = Path("/usr/bin/docker").resolve(strict=True)
    binary_info = docker_binary.stat()
    if (not stat.S_ISREG(binary_info.st_mode) or binary_info.st_uid != 0 or binary_info.st_gid != 0
            or binary_info.st_mode & stat.S_IWOTH or not os.access(docker_binary, os.X_OK)):
        raise RuntimeError("Docker executable ownership or permissions are unsafe")
    label_value = uuid.uuid4().hex
    label = "agentic-assets.colliers-fixture=" + label_value
    container_id = None

    def docker(*args, text=None, check=True):
        return command(str(docker_binary), *args, text=text, check=check, env=docker_env)

    def sql(query):
        result = docker("exec", "-i", container_id, "psql", "-U", "postgres", "-X", "-q", "-tA",
                         "-v", "ON_ERROR_STOP=1", text=query, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip()

    try:
        socket_path = Path("/var/run/docker.sock")
        socket_info = socket_path.lstat()
        if not stat.S_ISSOCK(socket_info.st_mode):
            raise RuntimeError("local Docker daemon socket is not a Unix socket")
        image_id = docker("image", "inspect", "--format", "{{.Id}}", "postgres:17").stdout.strip()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise RuntimeError("local PostgreSQL image identity is invalid")
        try:
            created = docker("run", "-d", "--rm", "--pull=never", "--name", name, "--label", label,
                    "--network=none", "--cpus=0.5", "--memory=512m", "--pids-limit=128",
                    "--tmpfs", "/var/lib/postgresql/data:rw,size=256m",
                    "-e", "POSTGRES_PASSWORD=synthetic-only", image_id)
        except (subprocess.SubprocessError, OSError):
            print(f"Container creation outcome unknown; possible orphan name={name} label={label}", flush=True)
            raise
        candidate_id = created.stdout.strip()
        if not re.fullmatch(r"[a-f0-9]{64}", candidate_id):
            raise RuntimeError(f"container ID unknown; possible orphan name={name} label={label}")
        container_id = candidate_id
        for _ in range(100):
            logs = docker("logs", container_id, check=False)
            ready = docker("exec", container_id, "pg_isready", "-U", "postgres", check=False).returncode == 0
            if ready and (logs.stdout + logs.stderr).count("database system is ready to accept connections") >= 2:
                break
            time.sleep(.2)
        else:
            raise RuntimeError("disposable database not ready")
        directory = HERE.parent.parent / "sql"
        master = (directory / "000_run_all.sql").read_text()
        master = re.sub(r"^\\i (\S+)\s*$", lambda m: (directory / m[1]).read_text(), master, flags=re.MULTILINE)
        schema = master + "\n" + "\n".join([
            "\\set CRE_APPROVE_LISTING_LIFECYCLE 1", "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-5261",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text()])
        # This external market view is counted by full validation but is not
        # created by the collector-owned schema migrations.
        schema += "\nCREATE VIEW credeals.v_cre_market_summary AS SELECT 1 WHERE false;\n"
        sql(schema)
        # Match the real search smoke prerequisites with unrelated active rows.
        sql("""
INSERT INTO credeals.cre_listings
    (brokerage_id, external_id, source_url, canonical_url, status, title,
     address, city, state, property_type, transaction_type, raw_data)
SELECT id, 'synthetic-industrial', 'https://example.test/seed/industrial',
       'https://example.test/seed/industrial', 'active', 'Industrial Lee',
       '100 National Avenue', 'Dallas', 'TX', 'industrial', 'sale', '{}'::jsonb
FROM credeals.cre_brokerages WHERE slug='lee-associates';
INSERT INTO credeals.cre_listings
    (brokerage_id, external_id, source_url, canonical_url, status, title,
     city, state, property_type, transaction_type, raw_data)
SELECT id, 'synthetic-office', 'https://example.test/seed/office',
       'https://example.test/seed/office', 'active', 'Office',
       'Dallas', 'TX', 'office', 'sale', '{}'::jsonb
FROM credeals.cre_brokerages WHERE slug='lee-associates';
""")
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id IN ('synthetic-industrial','synthetic-office')") == "2"
        for query in ("'industrial',NULL,'TX',NULL,'sale'", "'office',NULL,NULL,NULL,'sale'",
                      "'Lee',NULL,NULL,NULL,NULL", "'National Avenue',NULL,NULL,NULL,NULL"):
            assert int(sql(f"SELECT count(*) FROM credeals.search_cre_listings({query})")) > 0
        # Retain synthetic artifacts and child logs on either success or failure.
        db_url = "postgresql://postgres:synthetic-only@localhost/postgres"
        env_file = root / "synthetic.env"
        env_file.write_text("POSTGRES_URL_NON_POOLING=" + db_url + "\n", encoding="utf8")
        env_file.chmod(0o600)
        shim = root / "psql"
        # psql's validation \o target is a host-only private file. Keep the
        # container mount-free: write that one result to a random container
        # file, then stream it back into the already-open host file.
        shim.write_text(f"""#!/usr/bin/env python3
import os, re, stat, subprocess, sys, uuid
from pathlib import Path
args = sys.argv[1:]
root = Path({str(root)!r})
result_fd = None
for i, arg in enumerate(args):
    if arg.startswith("CRE_VALIDATION_RESULTS="):
        if i == 0 or args[i - 1] != "-v" or result_fd is not None:
            raise SystemExit("invalid validation result argument")
        path = Path(arg.split("=", 1)[1])
        if path.parent.parent != root or path.name != "query-results.tsv" or not re.fullmatch(
                r"cre-validate-results-[a-z0-9_]+", path.parent.name):
            raise SystemExit("validation result outside private fixture root")
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise SystemExit("validation result directory is not private")
            result_fd = os.open(path.name, os.O_WRONLY | os.O_NOFOLLOW, dir_fd=directory)
            info = os.fstat(result_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise SystemExit("validation result file is not private and owned")
            os.ftruncate(result_fd, 0)
        finally:
            os.close(directory)
        remote = "/tmp/colliers-validation-" + uuid.uuid4().hex
        args[i] = "CRE_VALIDATION_RESULTS=" + remote
if "-f" in args and args[args.index("-f") + 1] != "-":
    i = args.index("-f") + 1
    fd = os.open(args[i], os.O_RDONLY)
    os.dup2(fd, 0)
    os.close(fd)
    args[i] = "-"
env = {docker_env!r}
docker = {str(docker_binary)!r}
identity = {container_id!r}
base = [docker, "exec", "-i", "--user", str(os.getuid()) + ":" + str(os.getgid()), identity]
run = subprocess.run(base + ["psql", "-U", "postgres", *args], env=env)
if run.returncode != 0:
    raise SystemExit(run.returncode)
if result_fd is not None:
    try:
        with os.fdopen(result_fd, "wb") as output:
            result_fd = None
            copy = subprocess.run(base + ["cat", remote], env=env, stdout=output)
        if copy.returncode != 0:
            raise SystemExit(copy.returncode)
    finally:
        if result_fd is not None:
            os.close(result_fd)
""", encoding="utf8")
        shim.chmod(0o700)
        database = {"target": ingest.database_target_fingerprint_from_url(db_url), "psql": shim, "env_file": env_file}
        (root / "fixture").mkdir()
        with pytest.MonkeyPatch.context() as patch:
            exercise_pipeline(root / "fixture", patch, False, journal=True, database=database)
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id LIKE 'id%' AND status IS NULL") == "200"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id LIKE 'id%' AND status IS NOT NULL") == "0"
        windows = database["manifest"]["sources"]["colliers"]["partial_windows"]
        assert [window["state"] for window in windows] == ["verified", "verified", "settled_no_write"]
        # Reuse the actual saved claims and source artifact from the first committed
        # window while a different, clean verifier is current. The claim rederiver
        # and read-only PostgreSQL query are real; no source context is mocked.
        import cre_checkpoint_refresh as refresh
        historical = copy.deepcopy(database["manifest"])
        historical["sources"]["colliers"] = copy.deepcopy(windows[0]["checkpoint"])
        original = historical["collector_git_sha"]
        # The pipeline fixture creates an isolated Git repository for the
        # original producer. Advance it with an empty commit so the verifier
        # identity and ancestry are real rather than synthetic SHA stubs.
        synthetic_repo = root / "fixture" / "repo"
        assert command("git", "-C", str(synthetic_repo), "rev-parse", "HEAD").stdout.strip() == original
        command("git", "-C", str(synthetic_repo), "-c", "user.name=Synthetic",
                "-c", "user.email=synthetic@example.test", "commit", "--allow-empty", "-qm", "verifier")
        verifier = command("git", "-C", str(synthetic_repo), "rev-parse", "HEAD").stdout.strip()
        assert verifier != original
        assert command("git", "-C", str(synthetic_repo), "merge-base", original, verifier).stdout.strip() == original
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(refresh, "REPO_ROOT", synthetic_repo)
            assert refresh.git_identity() == (verifier, False)
            patch.setattr(ingest, "find_psql", lambda: str(shim))
            proof = refresh.colliers_partial_readback(
                root / "fixture" / "generation", historical, str(env_file),
                historical_producer_sha=original)
        assert proof["ok"] is True and proof["outcome"] == "exact_commit"
        assert proof["expected"] == 100
        print("Two real database windows, settled rotation and historical readback: PASS")
        from tests.colliers_controlled_recovery_fixture import run as run_controlled_recovery
        run_controlled_recovery(root, HERE.parent, env_file, shim,
                                root / "fixture" / "fake-fetch.mjs", database["target"]["value"],
                                lambda: sql("SELECT count(*) FROM credeals.cre_scrape_jobs"))
    finally:
        if container_id is not None:
            inspected = docker("inspect", "--format",
                '{{.Id}} {{.Name}} {{index .Config.Labels "agentic-assets.colliers-fixture"}}',
                container_id, check=False)
            expected = f"{container_id} /{name} {label_value}"
            if inspected.returncode != 0 or inspected.stdout.strip() != expected:
                raise RuntimeError("refusing cleanup: created container identity cannot be confirmed")
            docker("rm", "-f", container_id)
            absent = docker("inspect", container_id, check=False)
            if absent.returncode == 0 or "no such object" not in absent.stderr.lower():
                raise RuntimeError("cannot confirm owned synthetic container is absent")
            docker("info", "--format", "{{.ID}}")
            print(f"confirmed cleanup of synthetic container {container_id}", flush=True)


if __name__ == "__main__":
    main()
