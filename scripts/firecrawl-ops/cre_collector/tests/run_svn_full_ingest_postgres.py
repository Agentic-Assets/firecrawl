"""Synthetic, network-none PG17 proof of the complete generated ingest transaction."""
import json
import re
import subprocess
import tempfile
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
    name = "svn-full-proof-" + uuid.uuid4().hex[:10]
    def sql(statement):
        return run("docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA",
                   "-v", "ON_ERROR_STOP=1", input=statement)
    try:
        run("docker", "run", "-d", "--rm", "--name", name, "--network=none", "--cpus=0.5",
            "--memory=512m", "--pids-limit=128", "-e", "POSTGRES_PASSWORD=synthetic-only", "postgres:17")
        # initdb starts and stops a temporary server; wait for the final server.
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
        item = {"sourceKey": "svn", "id": "synthetic", "url": "https://example.test/synthetic",
                "name": "Synthetic", "transactionType": "sale", "markdown": "new detail"}
        row = ingest.to_row(item, {}, "2026-11-04T00:00:00Z")
        job = [{"slug": "svn", "discovered": 1, "saved": 1, "errors": 0, "notes": "synthetic"}]
        sql(ingest.build_sql([row], job, "2026-11-04T00:00:00Z", set()))
        sql("""UPDATE credeals.cre_listings SET raw_data=
              '{"inventoryObservedAt":"old","latestInventoryObservation":{"latitude":1.0000000000000001}}'::jsonb;""")
        row["scraped_at"] = "2026-11-04T00:01:00Z"
        row["transaction_type"] = "sale"
        sql("UPDATE credeals.cre_listings SET status='inactive', transaction_type='lease', "
            "deleted_at='2026-11-03T01:02:03Z' WHERE external_id='synthetic';")
        prior_lifecycle = sql("SELECT status || '|' || deleted_at::text FROM credeals.cre_listings "
                              "WHERE external_id='synthetic';")
        sql("UPDATE credeals.cre_source_index SET observation_present=false, soft_deleted=true "
            "WHERE external_id='synthetic';")
        prior_index = json.loads(sql("SELECT to_jsonb(si)::text FROM credeals.cre_source_index si "
                                     "WHERE external_id='synthetic';"))
        prior_events = int(sql("SELECT count(*) FROM credeals.cre_listing_events;"))
        sql(ingest.build_sql([row], job, "2026-11-04T00:01:00Z", set(),
                             svn_enrichment_ids=(row["external_id"],)))
        after_index = json.loads(sql("SELECT to_jsonb(si)::text FROM credeals.cre_source_index si "
                                     "WHERE external_id='synthetic';"))
        assert after_index == prior_index, (prior_index, after_index)
        assert sql("SELECT status || '|' || deleted_at::text FROM credeals.cre_listings "
                   "WHERE external_id='synthetic';") == prior_lifecycle
        assert int(sql("SELECT count(*) FROM credeals.cre_listing_events;")) == prior_events
        assert sql("SELECT transaction_type FROM credeals.cre_listings WHERE external_id='synthetic';") == "lease"
        value = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings;"))
        assert value["inventoryObservedAt"] == "old"
        assert value["latestInventoryObservation"]["latitude"] == 1.0000000000000001
        assert value["markdown"] == "new detail"
        exact = sql("SELECT raw_data->'latestInventoryObservation'->>'latitude' FROM credeals.cre_listings;")
        assert exact == "1.0000000000000001"
        # Terminal and active parents retain their own lifecycle too.
        for status, deleted in (("sold", "2026-11-03T01:02:03Z"), ("active", None)):
            deleted_sql = "'"+deleted+"'::timestamptz" if deleted else "NULL"
            sql("UPDATE credeals.cre_listings SET status='"+status+"', deleted_at="+deleted_sql+
                " WHERE external_id='synthetic';")
            lifecycle = sql("SELECT to_jsonb(l)->'status', to_jsonb(l)->'deleted_at' "
                            "FROM credeals.cre_listings l WHERE external_id='synthetic';")
            sql(ingest.build_sql([row], job, "2026-11-04T00:01:00Z", set(),
                                 svn_enrichment_ids=(row["external_id"],)))
            assert sql("SELECT to_jsonb(l)->'status', to_jsonb(l)->'deleted_at' "
                       "FROM credeals.cre_listings l WHERE external_id='synthetic';") == lifecycle
        # An inactive but not deleted parent is preserved as well.
        sql("UPDATE credeals.cre_listings SET status='inactive', deleted_at=NULL WHERE external_id='synthetic';")
        sql(ingest.build_sql([row], job, "2026-11-04T00:01:00Z", set(),
                             svn_enrichment_ids=(row["external_id"],)))
        assert sql("SELECT status FROM credeals.cre_listings WHERE external_id='synthetic';") == "inactive"
        # A detail-only first observation can be retained without inventing active status or index.
        missing = ingest.to_row({"sourceKey":"svn","id":"missing-parent",
                                 "url":"https://example.test/missing-parent",
                                 "name":"Missing","markdown":"detail","transactionMode":"sale"}, {}, "2026-11-04T00:02:00Z")
        sql(ingest.build_sql([missing], job, "2026-11-04T00:02:00Z", set(),
                             svn_enrichment_ids=("missing-parent",)))
        assert sql("SELECT coalesce(status,'<null>') FROM credeals.cre_listings "
                   "WHERE external_id='missing-parent';") == "<null>"
        assert sql("SELECT count(*) FROM credeals.cre_source_index WHERE external_id='missing-parent';") == "0"
        assert sql("SELECT coalesce(transaction_type,'<null>') FROM credeals.cre_listings "
                   "WHERE external_id='missing-parent';") == "<null>"
        # A later full inventory observation establishes active status and tenure.
        missing["transaction_type"] = "lease"
        sql(ingest.build_sql([missing], job, "2026-11-04T00:03:00Z", set()))
        assert sql("SELECT status || '|' || transaction_type FROM credeals.cre_listings "
                   "WHERE external_id='missing-parent';") == "active|lease"
        assert sql("SELECT count(*) FROM credeals.cre_source_index WHERE external_id='missing-parent';") == "1"
        # Missing source-index evidence must not be fabricated by pure detail.
        sql("DELETE FROM credeals.cre_source_index WHERE external_id='synthetic';")
        sql(ingest.build_sql([row], job, "2026-11-04T00:02:00Z", set(),
                             svn_enrichment_ids=(row["external_id"],)))
        assert sql("SELECT count(*) FROM credeals.cre_source_index WHERE external_id='synthetic';") == "0"
        # Full/mixed SVN identity still advances its source index.
        sql(ingest.build_sql([row], job, "2026-11-04T00:03:00Z", set()))
        assert sql("SELECT count(*) FROM credeals.cre_source_index WHERE external_id='synthetic';") == "1"
        # A different brokerage sharing the same external ID keeps ordinary behavior.
        other = ingest.to_row({"sourceKey":"newmark","id":"synthetic",
                               "url":"https://example.test/other",
                               "name":"Other","transactionType":"sale"}, {},
                              "2026-11-04T00:04:00Z")
        sql(ingest.build_sql([other], [{"slug":"newmark","discovered":1,"saved":1,"errors":0,"notes":"synthetic"}],
                             "2026-11-04T00:04:00Z", set(),
                             svn_enrichment_ids=("synthetic",)))
        assert sql("SELECT count(*) FROM credeals.cre_source_index si JOIN credeals.cre_brokerages b "
                   "ON b.id=si.brokerage_id WHERE b.slug='newmark' AND si.external_id='synthetic';") == "1"
        # Exercise the real ingest CLI mode partition and merge in both input orders.
        with tempfile.TemporaryDirectory(prefix="svn-mixed-proof-") as temporary:
            for first in ("enrich", "full"):
                identity = "mixed-" + first
                paths = []
                for position, mode in enumerate((first, "full" if first == "enrich" else "enrich")):
                    path = Path(temporary) / f"{identity}-{position}.json"
                    path.write_text(json.dumps({
                        "runMeta": {"mode": mode, "startedAt": "2026-11-04T00:00:00Z",
                                    "finishedAt": "2026-11-04T00:01:00Z"},
                        "sources": [{"sourceKey":"svn"}],
                        "listings": [{"sourceKey":"svn", "id":identity,
                                      "url":f"https://example.test/{identity}",
                                      "name":"Synthetic",
                                      "transactionMode":"sale" if mode=="enrich" else "lease"}],
                    }))
                    paths.extend(["--in",str(path)])
                out = Path(temporary) / (identity + "-sql")
                run(sys.executable,str(HERE.parent / "cre_ingest.py"),*paths,
                    "--dry-run","--keep-artifacts",str(out),cwd=HERE.parent)
                sql((out / "ingest.sql").read_text())
                assert sql("SELECT status || '|' || transaction_type FROM credeals.cre_listings "
                           "WHERE external_id='"+identity+"';") == "active|lease"
                assert sql("SELECT count(*) FROM credeals.cre_source_index WHERE external_id='"
                           +identity+"';") == "1"
        print("PASS: pure SVN detail preserves index/event, lifecycle/tenure; mixed/full and foreign controls")

    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
