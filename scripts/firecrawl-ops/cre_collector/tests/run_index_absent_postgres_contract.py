#!/usr/bin/env python3
"""Opt-in AGENTIC-4179 synthetic PostgreSQL 17 contract; no external DB accepted.

Requires Docker, pytest fixtures and an already-cached image ID. Creates exactly
one network-none container, no ports/volumes/pulls, and removes its recorded ID.
"""

import argparse
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE)]
import cre_reconcile_listing_lifecycle as reconcile  # noqa: E402
from test_index_absent_retirement import bundle  # noqa: E402
from test_listing_lifecycle_remediation import BID, LID  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--proof-dir", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.image_id):
        parser.error("an exact cached image ID is required")
    args.proof_dir.mkdir(parents=True, exist_ok=True)
    checks = []
    container = None
    workers = []

    def docker(*argv, **kwargs):
        return subprocess.run(
            ["docker", *argv],
            capture_output=True,
            text=True,
            timeout=kwargs.pop("timeout", 15),
            **kwargs,
        )

    def sql(text, check=True):
        result = docker(
            "exec",
            "-i",
            container,
            "psql",
            "-X",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            input=text,
        )
        if check and result.returncode:
            raise AssertionError(result.stderr)
        return result

    def start(text, app):
        process = subprocess.Popen(
            [
                "docker",
                "exec",
                "-i",
                "-e",
                "PGAPPNAME=" + app,
                container,
                "psql",
                "-X",
                "-qAt",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "postgres",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        workers.append(process)
        process.stdin.write(text)
        process.stdin.close()
        process.stdin = None
        return process

    def wait_lock(app, mode):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = sql(
                "SELECT count(*) FROM pg_locks k JOIN pg_stat_activity a USING(pid) "
                f"WHERE a.application_name='{app}' AND k.mode='{mode}' AND k.granted "
                "AND k.relation='credeals.cre_source_index'::regclass;"
            )
            if result.stdout.strip() != "0":
                return
            time.sleep(0.02)
        raise AssertionError("owned synthetic connection did not acquire expected lock")

    try:
        assert docker("image", "inspect", args.image_id).returncode == 0
        created = docker(
            "run",
            "--pull=never",
            "-d",
            "--name",
            "cre4179-" + uuid.uuid4().hex[:12],
            "--network",
            "none",
            "--cpus",
            "1",
            "--memory",
            "512m",
            "--pids-limit",
            "128",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,size=128m",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            args.image_id,
        )
        assert created.returncode == 0, created.stderr
        container = created.stdout.strip()
        assert re.fullmatch(r"[0-9a-f]{64}", container)
        (args.proof_dir / "owned-container-id.txt").write_text(container + "\n")
        for _ in range(30):
            # pg_isready also accepts the image's temporary initialization server.
            initialized = (
                "PostgreSQL init process complete; ready for start up."
                in docker("logs", container).stdout
            )
            if (
                initialized
                and docker(
                    "exec", container, "pg_isready", "-U", "postgres", timeout=4
                ).returncode
                == 0
            ):
                break
            time.sleep(0.2)
        else:
            raise AssertionError("owned disposable PostgreSQL startup deadline")
        assert int(sql("SHOW server_version_num;").stdout.strip()) // 10000 == 17
        prior = (HERE / "run_listing_lifecycle_postgres_contract.sh").read_text()
        schema = prior.split("<<'SQL'\n", 1)[1].split("\nSQL", 1)[0]
        sql(
            schema
            + "\nALTER TABLE credeals.cre_listings ADD COLUMN scraped_at timestamptz;"
        )
        migration = (
            HERE.parent.parent / "sql/016_cre_listing_lifecycle.sql"
        ).read_text()
        # Synthetic disposable schema only, matching the existing contract runner.
        sql(
            "\\set CRE_APPROVE_LISTING_LIFECYCLE 1\n"
            "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman\n"
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-999999\n"
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'\n"
            + migration
        )
        views = (HERE.parent.parent / "sql/005_cre_views.sql").read_text()
        trigger_function = views[
            views.index(
                "CREATE OR REPLACE FUNCTION credeals.update_cre_listing_timestamp()"
            ) :
        ]
        trigger_function = trigger_function[: trigger_function.index("$$;") + 3]
        sql(
            trigger_function + "\nCREATE TRIGGER trg_cre_listings_updated_at "
            "BEFORE UPDATE ON credeals.cre_listings FOR EACH ROW "
            "EXECUTE FUNCTION credeals.update_cre_listing_timestamp();"
        )
        value = bundle(args.proof_dir)
        plan = reconcile.build_plan(
            value, evidence_base_dir=args.proof_dir, allow_index_absent_retirement=True
        )
        action = plan["actions"][0]
        generated = reconcile.build_apply_sql(
            plan["actions"], plan["plan_hash"], "cayman", "AGENTIC-999999"
        )
        (args.proof_dir / "generated.sql").write_text(generated)
        old = action["before"]["listing_updated_at"]

        def reset():
            sql(
                "TRUNCATE credeals.cre_listing_events,credeals.cre_source_index,"
                "credeals.cre_listing_price_history,credeals.cre_listings,"
                "credeals.cre_scrape_jobs,credeals.cre_brokerages RESTART IDENTITY CASCADE;"
                f"INSERT INTO credeals.cre_brokerages VALUES ('{BID}','svn');"
                "INSERT INTO credeals.cre_listings "
                "(id,brokerage_id,external_id,source_url,status,updated_at,scraped_at) "
                f"VALUES ('{LID}','{BID}','x','https://example.test/x','active','{old}','{old}');"
            )

        def state():
            return sql(
                "SELECT json_build_array(status,deleted_at IS NOT NULL,"
                "(SELECT count(*) FROM credeals.cre_source_index),"
                "(SELECT count(*) FROM credeals.cre_listing_events),"
                "(SELECT count(*) FROM credeals.cre_scrape_jobs)) "
                "FROM credeals.cre_listings;"
            ).stdout.strip()

        reset()
        sql(generated)
        first = state()
        sql(generated)
        assert state() == first
        assert json.loads(first) == ["inactive", True, 0, 1, 1]
        assert (
            sql(
                "SELECT event_type||':'||field||':'||old_value||':'||new_value||':'||"
                "(presence_generation IS NULL)::text FROM credeals.cre_listing_events;"
            ).stdout.strip()
            == "status_change:status:active:inactive:true"
        )
        sql(
            reconcile.build_finalize_sql(
                plan["actions"], plan["plan_hash"], "cayman", "AGENTIC-999999"
            )
        )
        checks.append("schema016_success_exact_replay_and_finalize")
        sql(
            "UPDATE credeals.cre_listing_events SET reconciliation_evidence_sha256='wrong';"
        )
        assert sql(generated, check=False).returncode != 0
        checks.append("replay_refuses_changed_audit_provenance")

        for field, drift in [
            ("id", "'44444444-4444-4444-8444-444444444444'::uuid"),
            ("external_id", "'changed-provider'"),
            ("updated_at", "now()"),
            ("scraped_at", "now()"),
            ("status", "'sold'"),
            ("deleted_at", "now()"),
        ]:
            reset()
            sql(f"UPDATE credeals.cre_listings SET {field}={drift};")
            before = state()
            assert sql(generated, check=False).returncode != 0
            assert state() == before
            checks.append("CAS_" + field)
        reset()
        sql(
            "CREATE FUNCTION credeals.fail_event() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'synthetic event fault'; END $$;"
            "CREATE TRIGGER fail_event BEFORE INSERT ON credeals.cre_listing_events "
            "FOR EACH ROW EXECUTE FUNCTION credeals.fail_event();"
        )
        before = state()
        assert sql(generated, check=False).returncode != 0
        assert state() == before
        sql(
            "DROP TRIGGER fail_event ON credeals.cre_listing_events; DROP FUNCTION credeals.fail_event();"
        )
        checks.append("event_fault_rolls_back_listing_and_job")
        reset()
        writer = start(
            f"BEGIN; INSERT INTO credeals.cre_source_index (brokerage_id,external_id) "
            f"VALUES ('{BID}','x'); SELECT pg_sleep(1); COMMIT;",
            "index-first",
        )
        wait_lock("index-first", "RowExclusiveLock")
        assert sql(generated, check=False).returncode != 0
        writer.communicate(timeout=5)
        assert writer.returncode == 0
        assert json.loads(state()) == ["active", False, 1, 0, 0]
        checks.append("index_insert_first_retirement_refused_without_partial_commit")
        reset()
        retire = start(
            generated.replace(
                "IN SHARE ROW EXCLUSIVE MODE;",
                "IN SHARE ROW EXCLUSIVE MODE; SELECT pg_sleep(1);",
            ),
            "retire-first",
        )
        wait_lock("retire-first", "ShareRowExclusiveLock")
        insert = sql(
            "SET lock_timeout='250ms'; INSERT INTO credeals.cre_source_index "
            f"(brokerage_id,external_id) VALUES ('{BID}','x');",
            check=False,
        )
        assert insert.returncode != 0 and "lock timeout" in insert.stderr
        retire.communicate(timeout=5)
        assert retire.returncode == 0
        assert json.loads(state()) == ["inactive", True, 0, 1, 1]
        checks.append("retirement_lock_blocks_monitor_style_insert")
        (args.proof_dir / "checks.json").write_text(json.dumps(checks, indent=2) + "\n")
        print(json.dumps({"passed": len(checks), "checks": checks}))
    finally:
        # Every owned SQL connection has statement/lock deadlines; no broad process kill.
        try:
            for worker in workers:
                if worker.poll() is None:
                    worker.communicate(timeout=10)
        finally:
            if container:
                removed = docker("rm", "-f", container)
                assert removed.returncode == 0
                assert docker("inspect", container).returncode != 0
                (args.proof_dir / "owned-container-removed.txt").write_text(
                    "verified absent\n"
                )


if __name__ == "__main__":
    main()
