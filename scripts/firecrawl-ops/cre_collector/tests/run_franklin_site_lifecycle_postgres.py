"""Disposable PostgreSQL proof for Franklin site namespace retirement."""
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import cre_ingest as ingest  # noqa: E402


def run(*args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f"fixture command failed: {result.stderr}")
    return result.stdout.strip()


def main():
    name = "franklin-site-proof-" + uuid.uuid4().hex[:10]

    def sql(statement):
        return run("docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA",
                   "-v", "ON_ERROR_STOP=1", input=statement)

    try:
        run("docker", "run", "-d", "--rm", "--name", name, "--network=none", "--cpus=0.5",
            "--memory=512m", "--pids-limit=128", "-e", "POSTGRES_PASSWORD=synthetic-only", "postgres:17")
        for _ in range(100):
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"],
                                   capture_output=True).returncode == 0
            if ready and (logs.stdout + logs.stderr).count("database system is ready to accept connections") >= 2:
                break
            time.sleep(.2)
        else:
            raise RuntimeError("disposable postgres not ready")
        directory = HERE.parent.parent / "sql"
        master = (directory / "000_run_all.sql").read_text()
        master = re.sub(r"^\\i (\S+)\s*$", lambda m: (directory / m[1]).read_text(), master, flags=re.M)
        schema = master + "\n" + "\n".join([
            "\\set CRE_APPROVE_LISTING_LIFECYCLE 1",
            "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-5257",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text()])
        sql(schema)
        def row(source, identity):
            return ingest.to_row({"sourceKey": source, "id": identity,
                                  "url": "https://example.test/" + identity,
                                  "name": "Synthetic", "transactionMode": "sale"},
                                 {}, "2026-11-04T00:00:00Z")
        site = row("franklin-street", "franklin-site:sale:synthetic")
        old = row("franklin-street", "old")
        retained = row("franklin-street", "retained")
        other = row("svn", "franklin-site:sale:synthetic")
        jobs = [{"slug": slug, "discovered": 2, "saved": 2, "errors": 0, "notes": "synthetic"}
                for slug in ("franklin-street", "svn")]
        sql(ingest.build_sql([site, old, retained, other], jobs, "2026-11-04T00:00:00Z", set()))
        sql(ingest.build_sql([retained], jobs, "2026-11-05T00:00:00Z",
                             {"franklin-street", "svn"}))
        result = sql("""SELECT b.slug || '|' || l.external_id || '|' || COALESCE(l.deleted_at::text, 'present')
                        FROM credeals.cre_listings l JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id
                        ORDER BY b.slug,l.external_id;""").splitlines()
        assert any(x == 'franklin-street|franklin-site:sale:synthetic|present' for x in result), result
        assert any(x == 'franklin-street|retained|present' for x in result), result
        assert any(x.startswith('franklin-street|old|') and not x.endswith('|present') for x in result), result
        assert any(x.startswith('svn|franklin-site:sale:synthetic|') and not x.endswith('|present') for x in result), result
        print("Disposable full ingest lifecycle proof passed")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


if __name__ == "__main__":
    main()