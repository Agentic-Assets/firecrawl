"""Synthetic, network-none PG17 proof of the complete generated ingest transaction."""
import json
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
        sql(ingest.build_sql([row], job, "2026-11-04T00:01:00Z", set(),
                             svn_enrichment_ids=(row["external_id"],)))
        value = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings;"))
        assert value["inventoryObservedAt"] == "old"
        assert value["latestInventoryObservation"]["latitude"] == 1.0000000000000001
        assert value["markdown"] == "new detail"
        exact = sql("SELECT raw_data->'latestInventoryObservation'->>'latitude' FROM credeals.cre_listings;")
        assert exact == "1.0000000000000001"
        for preserve_children in (False, True):
            listing = {"sourceKey": "newmark", "id": "nm-synthetic",
                       "url": "https://example.test/newmark", "name": "Synthetic",
                       "transactionType": "sale", "markdown": "newmark detail",
                       "description": "retained prior detail",
                       "inventoryObservedAt": "2030-01-01T00:00:00Z",
                       "latestInventoryObservation": {"reported": "unqualified"},
                       "newmarkUnqualifiedInventoryContext": {"source": "original"}}
            if preserve_children:
                listing["preserveChildCollections"] = True
            newmark = ingest.to_row(listing, {}, "2026-11-04T00:00:00Z")
            nm_job = [{"slug": "newmark", "discovered": 1, "saved": 1, "errors": 0, "notes": "synthetic"}]
            sql(ingest.build_sql([newmark], nm_job, "2026-11-04T00:02:00Z", set()))
            sql("""UPDATE credeals.cre_listings SET raw_data=
                  '{"inventoryObservedAt":"old","latestInventoryObservation":{"latitude":1.0000000000000001}}'::jsonb
                  WHERE external_id='nm-synthetic';""")
            sql(ingest.build_sql([newmark], nm_job, "2026-11-04T00:03:00Z", set(),
                inventory_preserve_enrichment_ids={("newmark", newmark["external_id"])}))
            current = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='nm-synthetic';"))
            assert current["inventoryObservedAt"] == "old"
            assert current["latestInventoryObservation"] == {"latitude": 1.0000000000000001}
            assert current["markdown"] == "newmark detail"
            assert current["newmarkUnqualifiedInventoryContext"] == {"reported": {
                "inventoryObservedAt": "2030-01-01T00:00:00Z",
                "latestInventoryObservation": {"reported": "unqualified"},
                "newmarkUnqualifiedInventoryContext": {"source": "original"}}}
            assert current.get("detailObservedWithChildPreservation") is not True
            second_item = {k: v for k, v in listing.items() if k not in {
                "inventoryObservedAt", "latestInventoryObservation", "newmarkUnqualifiedInventoryContext", "description"}}
            second_item["markdown"] = "second detail"
            second = ingest.to_row(second_item, {}, "2026-11-04T00:03:30Z")
            sql(ingest.build_sql([second], nm_job, "2026-11-04T00:03:30Z", set(),
                inventory_preserve_enrichment_ids={("newmark", second["external_id"])}))
            repeated = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='nm-synthetic';"))
            assert repeated["description"] == "retained prior detail"
            assert repeated["markdown"] == "second detail"
            assert repeated["newmarkUnqualifiedInventoryContext"] == current["newmarkUnqualifiedInventoryContext"]
            third_item = {**second_item, "inventoryObservedAt": "2031-01-01T00:00:00Z"}
            third = ingest.to_row(third_item, {}, "2026-11-04T00:03:40Z")
            sql(ingest.build_sql([third], nm_job, "2026-11-04T00:03:40Z", set(),
                inventory_preserve_enrichment_ids={("newmark", third["external_id"])}))
            current = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='nm-synthetic';"))
            assert current["newmarkUnqualifiedInventoryContext"]["olderContextNotRetained"] is True
            assert "previous" not in current["newmarkUnqualifiedInventoryContext"]
            assert current["newmarkUnqualifiedInventoryContext"]["reported"] == {"inventoryObservedAt": "2031-01-01T00:00:00Z"}
            sql(ingest.build_sql([third], nm_job, "2026-11-04T00:03:50Z", set(),
                inventory_preserve_enrichment_ids={("newmark", third["external_id"])}))
            replay = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='nm-synthetic';"))
            assert replay["newmarkUnqualifiedInventoryContext"] == current["newmarkUnqualifiedInventoryContext"]
            fourth_item = {**second_item, "inventoryObservedAt": "2032-01-01T00:00:00Z"}
            fourth = ingest.to_row(fourth_item, {}, "2026-11-04T00:03:55Z")
            sql(ingest.build_sql([fourth], nm_job, "2026-11-04T00:03:55Z", set(),
                inventory_preserve_enrichment_ids={("newmark", fourth["external_id"])}))
            latest = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='nm-synthetic';"))
            assert latest["newmarkUnqualifiedInventoryContext"]["olderContextNotRetained"] is True
            assert "previous" not in latest["newmarkUnqualifiedInventoryContext"]
            assert latest["newmarkUnqualifiedInventoryContext"]["reported"] == {"inventoryObservedAt": "2032-01-01T00:00:00Z"}
            current = latest
            assert sql("SELECT raw_data->'latestInventoryObservation'->>'latitude' FROM credeals.cre_listings WHERE external_id='nm-synthetic';") == "1.0000000000000001"
            if preserve_children:
                inventory = ingest.to_row({"sourceKey": "newmark", "id": "nm-synthetic",
                    "url": "https://example.test/newmark", "name": "Inventory card",
                    "transactionType": "sale", "preserveChildCollections": True},
                    {}, "2026-11-04T00:05:00Z")
                sql(ingest.build_sql([inventory], nm_job, "2026-11-04T00:05:00Z", set()))
                after_inventory = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='nm-synthetic';"))
                assert after_inventory["markdown"] == "second detail"
                assert after_inventory["newmarkUnqualifiedInventoryContext"] == current["newmarkUnqualifiedInventoryContext"]
                assert after_inventory["inventoryObservedAt"] != "old"
            sql("UPDATE credeals.cre_listings SET raw_data='{}'::jsonb WHERE external_id='nm-synthetic';")
            sql(ingest.build_sql([newmark], nm_job, "2026-11-04T00:04:00Z", set(),
                inventory_preserve_enrichment_ids={("newmark", newmark["external_id"])}))
            without_prior = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='nm-synthetic';"))
            assert "inventoryObservedAt" not in without_prior
            assert "latestInventoryObservation" not in without_prior
            assert without_prior["markdown"] == "newmark detail"
        print("PASS: full generated SVN and Newmark generic/partial transactions preserve prior inventory and new detail")
    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
