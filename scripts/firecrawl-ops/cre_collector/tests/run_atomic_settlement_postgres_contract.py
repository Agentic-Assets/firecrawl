"""Opt-in disposable PostgreSQL contract for lock-first recovery snapshots.

Uses a private temporary socket and networkless owned container; never a live DB.
Run explicitly from an isolated checkout, not as part of the standard suite.
"""

import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import cre_validate

IMAGE = "sha256:2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3"
token = uuid.uuid4().hex
name = "cre-rollback-fence-" + token
root = pathlib.Path(tempfile.mkdtemp(prefix="cre-rollback-fence-"))
sock = root / "socket"
sock.mkdir(mode=0o777)
os.chmod(sock, 0o777)
owned = []
container = None
success = False


def run(args, **kw):
    return subprocess.run(
        args,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        **kw,
    )


def sql(query, app="probe"):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env["PGAPPNAME"] = app + "-" + token
    return run(
        [
            "/usr/bin/psql",
            "-X",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            str(sock),
            "-p",
            "5432",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-c",
            query,
        ],
        env=env,
    ).stdout.strip()


def spawn(script, app):
    path = root / (app + ".sql")
    path.write_text(script)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env["PGAPPNAME"] = app + "-" + token
    proc = subprocess.Popen(
        [
            "/usr/bin/psql",
            "-X",
            "-qA",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            str(sock),
            "-p",
            "5432",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-v",
            "CRE_VALIDATION_RESULTS=" + str(root / (app + ".tsv")),
            "-P",
            "pager=off",
            "-P",
            "footer=off",
            "-F",
            "	",
            "-A",
            "-f",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    owned.append(proc)
    return proc


def wait_for(query):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if sql(query) == "1":
            return
        time.sleep(0.05)
    raise RuntimeError("fixture state not reached")


try:
    result = run(
        [
            "docker",
            "run",
            "-d",
            "--pull=never",
            "--name",
            name,
            "--label",
            "cre.fixture=" + token,
            "--network",
            "none",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,size=128m",
            "--mount",
            "type=bind,src=" + str(sock) + ",dst=/fixture-socket",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "-e",
            "PGHOST=/fixture-socket",
            IMAGE,
            "-c",
            "unix_socket_directories=/var/run/postgresql,/fixture-socket",
            "-c",
            "listen_addresses=",
        ]
    )
    container = result.stdout.strip()
    inspect = json.loads(run(["docker", "inspect", container]).stdout)[0]
    assert inspect["Id"] == container and inspect["Config"]["Image"] == IMAGE
    assert (
        inspect["Config"]["Labels"]["cre.fixture"] == token
        and inspect["HostConfig"]["NetworkMode"] == "none"
    )
    assert not inspect["HostConfig"]["PortBindings"]
    mounts = inspect["Mounts"]
    assert (
        len(mounts) == 1
        and mounts[0]["Source"] == str(sock)
        and mounts[0]["Destination"] == "/fixture-socket"
    )
    assert root.resolve() == root and root.stat().st_uid == os.getuid()
    deadline = time.monotonic() + 25
    while True:
        try:
            logs = run(["docker", "logs", container])
            assert "PostgreSQL init process complete" in logs.stdout
            assert sql("SELECT 417391") == "417391"
            break
        except (subprocess.CalledProcessError, AssertionError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)
    key = "hashtextextended('credeals:listing-lifecycle:v1', 0)"
    sql(
        "CREATE SCHEMA credeals; CREATE TABLE credeals.cre_scrape_jobs (artifact_run_key text); CREATE TABLE fixture_generations (source_key text, generation_id text); CREATE FUNCTION delay_commit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(4); RETURN NEW; END $$; CREATE CONSTRAINT TRIGGER deferred_delay AFTER INSERT ON credeals.cre_scrape_jobs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION delay_commit();"
    )
    cre_validate.QUERIES["freshness_generations"] = (
        "SELECT source_key, generation_id FROM fixture_generations;"
    )
    source = "daum-commercial"
    key_a, key_b = "ingest:v1:" + "a" * 64, "ingest:v1:" + "b" * 64

    def fence_sql(artifact_key, generation):
        query = cre_validate.atomic_settlement_query(artifact_key, source, generation)
        return cre_validate._build_query_batch_script({"ordinary": "SELECT 42 AS sentinel;", "atomic_settlement": query})

    def fence_result(app):
        data = (root / (app + ".tsv")).read_text()
        rows = cre_validate.parse_query_batch(data, {"ordinary": "", "atomic_settlement": ""})
        assert rows["ordinary"] == [{"sentinel": "42"}]
        return rows["atomic_settlement"]

    writer = spawn(
        f"""BEGIN;
SELECT pg_advisory_xact_lock({key});
INSERT INTO credeals.cre_scrape_jobs VALUES ('{key_a}');
INSERT INTO fixture_generations VALUES ('daum-commercial','generation-a');
COMMIT;
""",
        "writer",
    )
    wait_for(
        "SELECT count(*) FROM pg_stat_activity WHERE application_name='writer-"
        + token
        + "' AND wait_event='PgSleep'"
    )
    writer.send_signal(signal.SIGKILL)
    writer.wait(timeout=5)
    start = time.monotonic()
    probe = spawn(fence_sql(key_a, "generation-a"), "probe-commit")
    probe.wait(timeout=20)
    probe_output = probe.stdout.read().decode()
    elapsed = time.monotonic() - start
    assert (
        probe.returncode == 0
        and fence_result("probe-commit")
        == [{"matching_jobs": "1", "matching_generations": "1"}]
        and elapsed > 1
    )
    print(
        json.dumps(
            {
                "case": "disconnected-delayed-commit",
                "fence_wait_seconds": round(elapsed, 3),
                "committed_count": 1,
                "passed": True,
            }
        )
    )
    blocker = spawn(
        "BEGIN;\nSELECT pg_advisory_xact_lock("
        + key
        + ");\nSELECT pg_sleep(5);\nCOMMIT;\n",
        "blocker",
    )
    wait_for(
        "SELECT count(*) FROM pg_stat_activity WHERE application_name='blocker-"
        + token
        + "' AND wait_event='PgSleep'"
    )
    queued = spawn(
        f"""BEGIN;
SELECT pg_advisory_xact_lock({key});
INSERT INTO credeals.cre_scrape_jobs VALUES ('{key_b}');
INSERT INTO fixture_generations VALUES ('daum-commercial','generation-b');
COMMIT;
""",
        "queued",
    )
    wait_for(
        "SELECT count(*) FROM pg_stat_activity WHERE application_name='queued-"
        + token
        + "' AND wait_event='advisory'"
    )
    queued.send_signal(signal.SIGKILL)
    queued.wait(timeout=5)
    start = time.monotonic()
    probe2 = spawn(fence_sql(key_b, "generation-b"), "probe-queued")
    blocker.wait(timeout=20)
    probe2.wait(timeout=20)
    probe2_output = probe2.stdout.read().decode()
    elapsed = time.monotonic() - start
    assert (
        blocker.returncode == 0
        and probe2.returncode == 0
        and fence_result("probe-queued")
        == [{"matching_jobs": "0", "matching_generations": "0"}]
        and elapsed > 1
    )
    wait_for(
        "SELECT CASE WHEN count(*)=0 THEN 1 ELSE 0 END FROM pg_stat_activity WHERE application_name='queued-"
        + token
        + "'"
    )
    assert (
        sql(
            "SELECT count(*) FROM credeals.cre_scrape_jobs WHERE artifact_run_key='"
            + key_b
            + "'"
        )
        == "0"
    )
    print(
        json.dumps(
            {
                "case": "disconnected-before-lock",
                "fence_wait_seconds": round(elapsed, 3),
                "committed_count": 0,
                "passed": True,
            }
        )
    )
    success = True
finally:
    alive = [p.pid for p in owned if p.poll() is None]
    if container and not alive and success:
        current = json.loads(run(["docker", "inspect", container]).stdout)[0]
        assert (
            current["Id"] == container
            and current["Config"]["Labels"]["cre.fixture"] == token
            and current["HostConfig"]["NetworkMode"] == "none"
        )
        run(["docker", "stop", "--time", "5", container])
        run(["docker", "rm", container])
        print(json.dumps({"fixture_evidence_directory": str(root)}))
        print(
            json.dumps(
                {
                    "cleanup": "exact-owned-container-removed-evidence-retained",
                    "passed": True,
                }
            )
        )
    else:
        print(
            json.dumps(
                {
                    "cleanup": "preserved-for-unknown-settlement",
                    "container": container,
                    "directory": str(root),
                    "live_owned_pids": alive,
                }
            )
        )
