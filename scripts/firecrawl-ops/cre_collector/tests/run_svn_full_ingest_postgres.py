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
                sql("""UPDATE credeals.cre_listings SET raw_data=
                      '{"preserveChildCollections":true,"markdown":"stale partial"}'::jsonb
                      WHERE external_id='nm-synthetic';""")
                sql(ingest.build_sql([inventory], nm_job, "2026-11-04T00:05:30Z", set()))
                unmarked = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='nm-synthetic';"))
                assert "markdown" not in unmarked
            sql("UPDATE credeals.cre_listings SET raw_data='{}'::jsonb WHERE external_id='nm-synthetic';")
            sql(ingest.build_sql([newmark], nm_job, "2026-11-04T00:04:00Z", set(),
                inventory_preserve_enrichment_ids={("newmark", newmark["external_id"])}))
            without_prior = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='nm-synthetic';"))
            assert "inventoryObservedAt" not in without_prior
            assert "latestInventoryObservation" not in without_prior
            assert without_prior["markdown"] == "newmark detail"
        # Realistic NIM wrapper -> generic detail -> NIM inventory, with no
        # manually manufactured prior wrapper or freshness marker.
        base = {"sourceKey": "newmark", "id": "nm-sequence",
                "url": "https://example.test/nm-sequence", "name": "NIM card",
                "transactionType": "sale", "preserveChildCollections": True}
        inventory1 = ingest.to_row(base, {}, "2026-11-04T00:10:00Z")
        sql(ingest.build_sql([inventory1], nm_job, "2026-11-04T00:10:00Z", set()))
        original = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='nm-sequence';"))
        assert original["latestInventoryObservation"]["preserveChildCollections"] is True
        detail = ingest.to_row({"sourceKey": "newmark", "id": "nm-sequence",
            "url": "https://example.test/nm-sequence", "name": "Detail",
            "transactionType": "sale", "markdown": "generic detail",
            "inventoryObservedAt": "2035-01-01T00:00:00Z"}, {}, "2026-11-04T00:11:00Z")
        sql(ingest.build_sql([detail], nm_job, "2026-11-04T00:11:00Z", set(),
            inventory_preserve_enrichment_ids={("newmark", detail["external_id"])}))
        enriched = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='nm-sequence';"))
        assert enriched["markdown"] == "generic detail"
        assert enriched["inventoryObservedAt"] == original["inventoryObservedAt"]
        assert enriched.get("detailObservedWithChildPreservation") is not True
        inventory2 = ingest.to_row({**base, "name": "new NIM card"}, {}, "2026-11-04T00:12:00Z")
        sql(ingest.build_sql([inventory2], nm_job, "2026-11-04T00:12:00Z", set()))
        final = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='nm-sequence';"))
        assert final["markdown"] == "generic detail"
        assert final["newmarkUnqualifiedInventoryContext"] == enriched["newmarkUnqualifiedInventoryContext"]
        assert final["inventoryObservedAt"] != original["inventoryObservedAt"]
        assert final.get("detailObservedWithChildPreservation") is not True
        print("PASS: full generated SVN and Newmark generic/partial transactions preserve prior inventory and new detail")
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
        # Full inventory continues to revive inactive rows, preserve terminal status,
        # and clear the deletion marker in both cases.
        for status, expected in (("inactive", "active"), ("sold", "sold")):
            sql("UPDATE credeals.cre_listings SET status='" + status +
                "', deleted_at='2026-11-03T01:02:03Z' WHERE external_id='synthetic';")
            sql(ingest.build_sql([row], job, "2026-11-04T00:03:00Z", set()))
            assert sql("SELECT status || '|' || (deleted_at IS NULL)::text "
                       "FROM credeals.cre_listings WHERE external_id='synthetic';") == expected + "|true"
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
        for route in ("published", "generic"):
            identity = "nm-lifecycle-" + route
            url = "https://example.test/" + identity
            card = {"sourceKey": "newmark", "id": identity, "url": url,
                    "name": "NIM inventory", "transactionMode": "lease", "preserveChildCollections": True}
            parent = ingest.to_row(card, {}, "2026-11-04T00:00:00Z")
            nm_job = [{"slug": "newmark", "discovered": 1, "saved": 1, "errors": 0, "notes": "synthetic"}]
            sql(ingest.build_sql([parent], nm_job, "2026-11-04T00:00:00Z", set()))
            sql("UPDATE credeals.cre_listings SET status='inactive',deleted_at='2026-11-04T00:01:00Z', transaction_type='lease' WHERE external_id="
                + ingest.sql_lit(identity) + "; UPDATE credeals.cre_source_index SET observation_present=false,soft_deleted=true WHERE external_id="
                + ingest.sql_lit(identity) + ";")
            def snap():
                return json.loads(sql("SELECT jsonb_build_object('parent',(SELECT jsonb_build_object('status',status,'deleted_at',deleted_at,'transaction_type',transaction_type) "
                    "FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(identity)+"), 'idx',(SELECT to_jsonb(i) FROM credeals.cre_source_index i WHERE external_id="
                    +ingest.sql_lit(identity)+"),'events',(SELECT count(*) FROM credeals.cre_listing_events e JOIN credeals.cre_listings l ON l.id=e.listing_id WHERE l.external_id="
                    +ingest.sql_lit(identity)+"))::text;"))
            before = snap()
            item = {"sourceKey":"newmark","id":identity,"url":url,"name":"detail","markdown":"detail observed",
                    "transactionMode": "sale" if route == "generic" else None}
            if route == "published":
                item["preserveChildCollections"] = True
            detail = ingest.to_row(item, {}, "2026-11-04T00:02:00Z")
            sql(ingest.build_sql([detail], nm_job, "2026-11-04T00:02:00Z", set(),
                inventory_preserve_enrichment_ids={("newmark",detail["external_id"])}))
            after = snap()
            assert after == before, (route, before, after)
            assert sql("SELECT markdown FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(identity)+";") == "detail observed"
            newer = ingest.to_row(card, {}, "2026-11-04T00:03:00Z")
            sql(ingest.build_sql([newer], nm_job, "2026-11-04T00:03:00Z", set()))
            fresh = snap()
            assert fresh["parent"]["status"] == "active" and fresh["parent"]["deleted_at"] is None
            assert fresh["parent"]["transaction_type"] == "lease"
            assert fresh["idx"]["observation_present"] is True and fresh["idx"]["soft_deleted"] is False
            assert fresh["events"] == before["events"] + 1
        missing = ingest.to_row({"sourceKey":"newmark","id":"nm-no-parent",
                                 "url":"https://example.test/nm-no-parent","name":"detail only",
                                 "markdown":"observed detail","transactionMode":"sale"},
                                {}, "2026-11-04T00:04:00Z")
        sql(ingest.build_sql([missing], nm_job, "2026-11-04T00:04:00Z", set(),
            inventory_preserve_enrichment_ids={("newmark", missing["external_id"])}))
        assert sql("SELECT coalesce(status::text,'<null>') || '|' || coalesce(transaction_type,'<null>') "
                   "FROM credeals.cre_listings WHERE external_id='nm-no-parent';") == "<null>|<null>"
        assert sql("SELECT count(*) FROM credeals.cre_source_index WHERE external_id='nm-no-parent';") == "0"
        assert sql("SELECT markdown FROM credeals.cre_listings WHERE external_id='nm-no-parent';") == "observed detail"
        inventory = ingest.to_row({"sourceKey":"newmark","id":"nm-no-parent",
            "url":"https://example.test/nm-no-parent","name":"fresh NIM", "transactionMode":"lease"},
            {}, "2026-11-04T00:05:00Z")
        sql(ingest.build_sql([inventory], nm_job, "2026-11-04T00:05:00Z", set()))
        assert sql("SELECT status || '|' || transaction_type FROM credeals.cre_listings WHERE external_id='nm-no-parent';") == "active|lease"
        assert sql("SELECT count(*) FROM credeals.cre_source_index WHERE external_id='nm-no-parent';") == "1"
        # Same external ID in two brokerages: Newmark detail must not suppress
        # the independent full SVN inventory row in this very transaction.
        shared = "synthetic-cross-source"
        nm_cross = ingest.to_row({"sourceKey":"newmark","id":shared,
            "url":"https://example.test/nm-shared","name":"NM detail","markdown":"real detail"},
            {}, "2026-11-04T00:06:00Z")
        svn_cross = ingest.to_row({"sourceKey":"svn","id":shared,
            "url":"https://example.test/svn-shared","name":"SVN full","transactionMode":"lease"},
            {}, "2026-11-04T00:06:00Z")
        sql(ingest.build_sql([nm_cross, svn_cross], nm_job+job, "2026-11-04T00:06:00Z", set(),
            inventory_preserve_enrichment_ids={("newmark", shared)}))
        assert sql("SELECT source_key FROM credeals.cre_source_index WHERE external_id='synthetic-cross-source';") == "svn"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='synthetic-cross-source';") == "2"
        print("PASS: Newmark generic/published detail preserves lifecycle, tenure, index and events; later NIM can reappear or qualify unknown parents")

    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
