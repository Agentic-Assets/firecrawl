"""Disposable full-ingest regression; never connects to an application database."""
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import cre_ingest as ingest


def command(*args, text=None, check=True):
    return subprocess.run(args, input=text, text=True, capture_output=True, check=check, timeout=120)


def main():
    name = "cbre-history-" + uuid.uuid4().hex[:12]
    started = False

    def sql(query):
        result = command("docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA",
                         "-v", "ON_ERROR_STOP=1", text=query, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip()

    try:
        started = True  # The unique name remains ours even if docker exits ambiguously.
        command("docker", "run", "-d", "--rm", "--pull=never", "--name", name, "--network=none",
                "--cpus=0.5", "--memory=512m", "--pids-limit=128", "--tmpfs",
                "/var/lib/postgresql/data:rw,size=256m", "-e", "POSTGRES_PASSWORD=synthetic-only", "postgres:17")
        for _ in range(100):
            logs = command("docker", "logs", name, check=False)
            ready = command("docker", "exec", name, "pg_isready", "-U", "postgres", check=False).returncode == 0
            if ready and (logs.stdout + logs.stderr).count("database system is ready to accept connections") >= 2:
                break
            time.sleep(.2)
        else:
            raise RuntimeError("disposable fixture not ready")
        directory = HERE.parent.parent / "sql"
        master = (directory / "000_run_all.sql").read_text()
        schema = re.sub(r"^\\i (\S+)\s*$", lambda m: (directory / m[1]).read_text(), master, flags=re.MULTILINE)
        schema += "\n" + "\n".join([
            "\\set CRE_APPROVE_LISTING_LIFECYCLE 1", "\\set CRE_LISTING_LIFECYCLE_OPERATOR synthetic",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-5235",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text(),
        ])
        sql(schema)
        jobs = [{"slug": "cbre", "discovered": 1, "saved": 1, "errors": 0, "notes": "synthetic"}]
        old = {"sourceKey": "cbre", "id": "fixture-a", "url": "https://example.test/a",
               "name": "Original", "transactionMode": "sale"}
        sql(ingest.build_sql([ingest.to_row(old, {}, "2026-11-04T14:00:00Z")],
                             jobs, "2026-11-04T14:00:00Z", set()))
        history = {"schema": "cbre-partial-observations/v1", "entries": [
            {"source": {"unknown": None, "nested": [1, None]}, "sha256": "a" * 64},
            {"source": {"newField": "keep"}, "sha256": "b" * 64}]}
        sql("UPDATE credeals.cre_listings SET raw_data=raw_data || jsonb_build_object('cbrePartialObservations', "
            + ingest.sql_lit(json.dumps(history)) + "::jsonb) WHERE external_id='fixture-a';")
        full = {**old, "name": "Refreshed"}
        for timestamp in ("2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z"):
            sql(ingest.build_sql([ingest.to_row(full, {}, timestamp)], jobs, timestamp, set()))
            row = json.loads(sql("SELECT json_build_object('raw',raw_data,'status',status,'deleted',deleted_at) "
                                 "FROM credeals.cre_listings WHERE external_id='fixture-a';"))
            assert row["raw"]["cbrePartialObservations"] == history
            assert row["raw"]["name"] == "Refreshed"
            assert row["status"] == "active" and row["deleted"] is None
        collision = {**full, "cbrePartialObservations": {"unexpected": True}}
        try:
            sql(ingest.build_sql([ingest.to_row(collision, {}, "2026-11-04T17:00:00Z")],
                                 jobs, "2026-11-04T17:00:00Z", set()))
            raise AssertionError("incoming reserved evidence collision accepted")
        except RuntimeError as exc:
            assert "CBRE partial evidence key collision" in str(exc)
        assert json.loads(sql("SELECT raw_data FROM credeals.cre_listings WHERE external_id='fixture-a';"))["cbrePartialObservations"] == history
        fresh = {**old, "id": "fixture-new", "url": "https://example.test/new"}
        sql(ingest.build_sql([ingest.to_row(fresh, {}, "2026-11-04T18:00:00Z")],
                             jobs, "2026-11-04T18:00:00Z", set()))
        assert sql("SELECT raw_data ? 'cbrePartialObservations' FROM credeals.cre_listings "
                   "WHERE external_id='fixture-new';") == "f"
        print("CBRE full-ingest partial-history regression passed")
    finally:
        if started:
            command("docker", "rm", "-f", name, check=False)


if __name__ == "__main__":
    main()
