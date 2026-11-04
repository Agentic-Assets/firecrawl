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
        # Exercise the real TS generic mapper and the generated ingest transaction.
        # The source detail is sparse: old physical children must survive, while
        # distinct newly observed children remain additive and new parents insert.
        mapper = """import {parseGenericJsonLd} from './lib/enrich.ts';
const item={sourceKey:'newmark',externalId:'nm-child-existing',url:'https://example.test/nm-child-existing'};
const html='<script type="application/ld+json">{"@type":"RealEstateListing","name":"New detail"}</script>';
console.log(JSON.stringify({sourceKey:'newmark',...parseGenericJsonLd(item,html)}));"""
        generic = json.loads(run("node", "--import", "tsx", "--input-type=module", "-e", mapper,
                                 cwd=str(HERE.parent)))
        child_fields = {
            "contactsDetailed": [{"name": "Prior Broker", "email": "prior@example.test"}],
            "documents": [{"url": "https://example.test/prior.pdf", "title": "Prior"}],
            "photos": ["https://example.test/prior.jpg"],
            "media": [{"url": "https://example.test/prior-video", "mediaType": "video"}],
            "links": [{"url": "https://example.test/prior-link", "linkType": "other"}],
        }
        new_child_fields = {
            "contactsDetailed": [{"name": "New Broker", "email": "new@example.test"}],
            "documents": [{"url": "https://example.test/new.pdf", "title": "New"}],
            "photos": ["https://example.test/new.jpg"],
            "media": [{"url": "https://example.test/new-video", "mediaType": "video"}],
            "links": [{"url": "https://example.test/new-link", "linkType": "other"}],
        }
        child_tables = ("contacts", "documents", "images", "media", "links")
        def child_ids(external_id):
            return {table: set(sql("SELECT c.id FROM credeals.cre_listing_" + table +
                " c JOIN credeals.cre_listings l ON l.id=c.listing_id WHERE l.external_id='" +
                external_id + "' ORDER BY c.id;").splitlines()) for table in child_tables}
        seed = ingest.to_row({**generic, **child_fields, "transactionType": "sale"}, {}, "2026-11-04T00:07:00Z")
        sql(ingest.build_sql([seed], nm_job, "2026-11-04T00:07:00Z", set()))
        before = child_ids("nm-child-existing")
        assert all(len(ids) == 1 for ids in before.values()), before
        # Real generic producer + prior inventory-only NIM marker. Retain both
        # source facts and the exact inventory observation/clock together.
        prior_raw = {"sourceKey": "newmark", "preserveChildCollections": True,
                     "inventoryObservedAt": "2026-11-03T01:02:03Z",
                     "latestInventoryObservation": {"nativeId": "prior-native", "sentinel": "keep"},
                     "priorTopLevel": {"value": "keep"}}
        sql("UPDATE credeals.cre_listings SET raw_data=" + ingest.sql_lit(json.dumps(prior_raw)) +
            "::jsonb WHERE external_id='nm-child-existing';")
        sparse = ingest.to_row(generic, {}, "2026-11-04T00:08:00Z")
        sql(ingest.build_sql([sparse], nm_job, "2026-11-04T00:08:00Z", set(),
            inventory_preserve_enrichment_ids={("newmark", "nm-child-existing")}))
        assert child_ids("nm-child-existing") == before, "sparse generic detail deleted prior children"
        after_raw = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings "
                                   "WHERE external_id='nm-child-existing';"))
        assert after_raw.get("inventoryObservedAt") == prior_raw["inventoryObservedAt"], after_raw
        assert after_raw.get("latestInventoryObservation") == prior_raw["latestInventoryObservation"], after_raw
        assert after_raw.get("priorTopLevel") == prior_raw["priorTopLevel"], after_raw
        assert after_raw.get("genericEnrich", {}).get("hadJsonLd") is True, after_raw
        enriched = ingest.to_row({**generic, **new_child_fields}, {}, "2026-11-04T00:09:00Z")
        sql(ingest.build_sql([enriched], nm_job, "2026-11-04T00:09:00Z", set(),
            inventory_preserve_enrichment_ids={("newmark", "nm-child-existing")}))
        after = child_ids("nm-child-existing")
        assert all(before[t] < after[t] and len(after[t]) == 2 for t in child_tables), (before, after)
        new_id = "nm-child-new"
        new_row = ingest.to_row({**generic, **new_child_fields, "id": new_id,
            "url": "https://example.test/nm-child-new"}, {}, "2026-11-04T00:10:00Z")
        sql(ingest.build_sql([new_row], nm_job, "2026-11-04T00:10:00Z", set(),
            inventory_preserve_enrichment_ids={("newmark", new_id)}))
        assert all(len(ids) == 1 for ids in child_ids(new_id).values()), child_ids(new_id)
        empty_id = "nm-child-new-empty"
        empty_row = ingest.to_row({**generic, "id": empty_id,
            "url": "https://example.test/nm-child-new-empty"}, {}, "2026-11-04T00:11:00Z")
        sql(ingest.build_sql([empty_row], nm_job, "2026-11-04T00:11:00Z", set(),
            inventory_preserve_enrichment_ids={("newmark", empty_id)}))
        assert all(not ids for ids in child_ids(empty_id).values()), child_ids(empty_id)
        empty_raw = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings "
                                   "WHERE external_id='nm-child-new-empty';"))
        assert "inventoryObservedAt" not in empty_raw, empty_raw
        assert "latestInventoryObservation" not in empty_raw, empty_raw
        assert empty_raw.get("genericEnrich", {}).get("hadJsonLd") is True, empty_raw
        assert sql("SELECT coalesce(status::text,'<null>') || '|' || coalesce(transaction_type,'<null>') "
                   "FROM credeals.cre_listings WHERE external_id='nm-child-new-empty';") == "<null>|<null>"
        assert sql("SELECT count(*) FROM credeals.cre_source_index WHERE external_id='nm-child-new-empty';") == "0"
        later = ingest.to_row({"sourceKey": "newmark", "id": empty_id,
            "url": "https://example.test/nm-child-new-empty", "name": "Inventory",
            "transactionMode": "lease", "preserveChildCollections": True,
            "inventoryObservedAt": "2026-11-04T00:12:00Z"}, {}, "2026-11-04T00:12:00Z")
        sql(ingest.build_sql([later], nm_job, "2026-11-04T00:12:00Z", set()))
        later_raw = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings "
                                   "WHERE external_id='nm-child-new-empty';"))
        assert later_raw.get("inventoryObservedAt", "").replace("+00:00", "Z") == "2026-11-04T00:12:00Z", later_raw
        assert later_raw.get("latestInventoryObservation", {}).get("name") == "Inventory", later_raw
        assert later_raw.get("genericEnrich", {}).get("hadJsonLd") is True, later_raw
        assert sql("SELECT status || '|' || transaction_type FROM credeals.cre_listings "
                   "WHERE external_id='nm-child-new-empty';") == "active|lease"
        assert sql("SELECT count(*) FROM credeals.cre_source_index WHERE external_id='nm-child-new-empty';") == "1"
        new_children_before_inventory = child_ids(new_id)
        later_with_children = ingest.to_row({"sourceKey": "newmark", "id": new_id,
            "url": "https://example.test/nm-child-new", "name": "Inventory with children",
            "transactionMode": "sale", "preserveChildCollections": True,
            "inventoryObservedAt": "2026-11-04T00:13:00Z"}, {}, "2026-11-04T00:13:00Z")
        sql(ingest.build_sql([later_with_children], nm_job, "2026-11-04T00:13:00Z", set()))
        assert child_ids(new_id) == new_children_before_inventory
        new_later_raw = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings "
                                       "WHERE external_id='nm-child-new';"))
        assert new_later_raw.get("genericEnrich", {}).get("hadJsonLd") is True, new_later_raw
        assert new_later_raw.get("latestInventoryObservation", {}).get("name") == "Inventory with children"
        # First-seen detail fields are unqualified context, for both producers.
        for kind in ("generic", "published"):
            first_id = "nm-first-" + kind
            reported = {"inventoryObservedAt": "2030-01-01T00:00:00Z",
                        "latestInventoryObservation": {"claimed": "source"},
                        "newmarkUnqualifiedInventoryContext": {"original": "context"}}
            first = {"sourceKey": "newmark", "id": first_id,
                     "url": "https://example.test/" + first_id, "name": "First detail",
                     "preserveChildCollections": True, "markdown": "detail text", **reported,
                     **new_child_fields}
            if kind == "generic":
                first["genericEnrich"] = {"hadJsonLd": True}
            row = ingest.to_row(first, {}, "2026-11-04T00:14:00Z")
            sql(ingest.build_sql([row], nm_job, "2026-11-04T00:14:00Z", set(),
                inventory_preserve_enrichment_ids={("newmark", first_id)}))
            raw = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='" + first_id + "';"))
            assert "inventoryObservedAt" not in raw and "latestInventoryObservation" not in raw, raw
            assert raw["newmarkUnqualifiedInventoryContext"] == {"reported": reported}, raw
            assert all(len(ids) == 1 for ids in child_ids(first_id).values())
            assert sql("SELECT coalesce(status::text,'<null>') || '|' || coalesce(transaction_type,'<null>') FROM credeals.cre_listings WHERE external_id='" + first_id + "';") == "<null>|<null>"
            assert sql("SELECT count(*) FROM credeals.cre_source_index WHERE external_id='" + first_id + "';") == "0"
            inventory = ingest.to_row({"sourceKey": "newmark", "id": first_id,
                "url": first["url"], "name": "Later inventory", "transactionMode": "sale",
                "preserveChildCollections": True}, {}, "2026-11-04T00:15:00Z")
            old_children = child_ids(first_id)
            sql(ingest.build_sql([inventory], nm_job, "2026-11-04T00:15:00Z", set()))
            raw = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='" + first_id + "';"))
            assert raw["newmarkUnqualifiedInventoryContext"] == {"reported": reported}, raw
            assert raw["latestInventoryObservation"]["name"] == "Later inventory", raw
            assert raw["markdown"] == "detail text", raw
            assert child_ids(first_id) == old_children
            assert sql("SELECT status || '|' || transaction_type FROM credeals.cre_listings WHERE external_id='" + first_id + "';") == "active|sale"
            assert sql("SELECT count(*) FROM credeals.cre_source_index WHERE external_id='" + first_id + "';") == "1"
        # Real published-detail producer, with injected sanitized page and no network.
        published_mapper = """import {createNewmarkPublishedDetailEnricher} from './sources/newmark-published-enrich.ts';
const f=value=>({state:'value',value}), missing={state:'missing'};
const item={sourceKey:'newmark',externalId:'published-real',url:'https://www.nmrk.com/properties/published-real',
 sourcePublishedDetail:{status:'observed',schema:'newmark-nim-link-witness/v1',sourceKey:'newmark',
 sourcePath:'raw_data.rawNewmarkNimRecord',recordId:'native-id',slug:'published-real',
 url:'https://my.rcm1.com/handler/modern.aspx?pv=opaque',inventoryObservedAt:null,
 sourceName:f('Example Property'),sourceProperty:{selection:'single',fields:{address:f('10 Main Street'),
 city:f('Example'),stateAbbreviation:f('FL'),stateDescription:missing,zip:f('12345'),countryCode:missing}}}};
const data={name:'Example Property',addresses:{street:'10 Main Street',city:'Example',state:'FL',zip:'12345'},
 photos:[{type:'image',imageUrl:'/image.jpg'}]};
const e=createNewmarkPublishedDetailEnricher(async()=>({rawHtml:'<script>var data = '+JSON.stringify(data)+'</script>',
 markdown:'',links:[],metadata:{statusCode:200}}),()=> '2026-11-04T00:16:00Z');
const out=await e.enrichWithEvidence([item]);
if(out.listings.length!==1) throw Error('published producer did not emit matched detail');
console.log(JSON.stringify({sourceKey:'newmark',...out.listings[0]}));"""
        published = json.loads(run("node", "--import", "tsx", "--input-type=module", "-e",
                                   published_mapper, cwd=str(HERE.parent)))
        assert published["preserveChildCollections"] is True and "genericEnrich" not in published
        published_row = ingest.to_row(published, {}, "2026-11-04T00:16:00Z")
        sql(ingest.build_sql([published_row], nm_job, "2026-11-04T00:16:00Z", set(),
            inventory_preserve_enrichment_ids={("newmark", "published-real")}))
        published_raw = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='published-real';"))
        assert "inventoryObservedAt" not in published_raw and "latestInventoryObservation" not in published_raw
        assert len(child_ids("published-real")["images"]) == 1
        assert sql("SELECT coalesce(status::text,'<null>') || '|' || coalesce(transaction_type,'<null>') FROM credeals.cre_listings WHERE external_id='published-real';") == "<null>|<null>"
        assert sql("SELECT count(*) FROM credeals.cre_source_index WHERE external_id='published-real';") == "0"
        published_children = child_ids("published-real")
        inventory = ingest.to_row({"sourceKey": "newmark", "id": "published-real", "url": published["url"],
            "name": "Published later inventory", "transactionMode": "sale",
            "preserveChildCollections": True}, {}, "2026-11-04T00:17:00Z")
        sql(ingest.build_sql([inventory], nm_job, "2026-11-04T00:17:00Z", set()))
        published_raw = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='published-real';"))
        assert published_raw["latestInventoryObservation"]["name"] == "Published later inventory"
        assert child_ids("published-real") == published_children
        assert sql("SELECT status || '|' || transaction_type FROM credeals.cre_listings WHERE external_id='published-real';") == "active|sale"
        assert sql("SELECT count(*) FROM credeals.cre_source_index WHERE external_id='published-real';") == "1"
        # Newmark page attempt: mutation only under a current unique source association.
        page_id = "page-guard-example"
        page_url = "https://www.nmrk.com/properties/page-guard-example"
        witness = {"recordId": "native-1", "slug": page_id, "sourceUrl": page_url,
                   "address": "10 Main Street", "city": "Example", "state": "Florida", "zip": "12345"}
        native = {"id": "native-1", "slug": page_id, "externalWebsiteUrl": page_url,
                  "properties": [{"address": "10 Main Street", "city": "Example",
                                  "stateDescription": "Florida", "zip": "12345"}]}
        seed_page = ingest.to_row({"sourceKey": "newmark", "id": page_id, "url": page_url,
                                   "description": "long prior description", **child_fields},
                                  {}, "2026-11-04T00:18:00Z")
        sql(ingest.build_sql([seed_page], nm_job, "2026-11-04T00:18:00Z", set()))
        raw = {"sourceKey": "newmark", "inventoryObservedAt": "old-clock",
               "latestInventoryObservation": {"rawNewmarkNimRecord": native}}
        sql("UPDATE credeals.cre_listings SET raw_data=" + ingest.sql_lit(json.dumps(raw)) +
            "::jsonb WHERE external_id='" + page_id + "';")
        initial_children = child_ids(page_id)
        page = ingest.to_row({"sourceKey": "newmark", "id": page_id, "url": page_url,
            "preserveChildCollections": True,
            "newmarkPageObservation": {"witness": witness, "prose": "source prose"},
            **new_child_fields,
            "contactsDetailed": [{"name": "Changed name", "email": "prior@example.test"},
                                 {"name": "New Broker", "email": "new@example.test"}]},
            {}, "2026-11-04T00:19:00Z")
        import cre_enrich
        import newmark_claim_witness
        sql("INSERT INTO credeals.cre_enrichment_queue (brokerage_id,source_key,external_id,url,reason) SELECT b.id,'newmark','" +
            page_id + "','" + page_url + "','new' FROM credeals.cre_brokerages b WHERE b.slug='newmark';")
        claim_result = sql(cre_enrich.build_claim_sql(1, source="newmark", source_page_witness=True))
        witness_value = claim_result.split("|")[-2]
        actual_witness = newmark_claim_witness.parse_page(witness_value)
        assert actual_witness == witness, actual_witness
        claimed = cre_enrich._parse_claimed([tuple(claim_result.split("|"))], source_page_witness=True)[0]
        page_claim = {"id": claimed["id"], "claimedAt": claimed["claimed_at"],
            "attempts": claimed["attempts"], "brokerageId": claimed["brokerage_id"],
            "externalId": page_id, "url": page_url}
        attempt = "a" * 64
        result = sql(ingest.build_sql([page], nm_job, "2026-11-04T00:19:00Z", set(),
            inventory_preserve_enrichment_ids={("newmark", page_id)}, newmark_page_attempt=attempt, newmark_page_claim=page_claim))
        assert "CRE_NEWMARK_PAGE_V1 " + attempt + " committed" in result
        raw_after = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='" + page_id + "';"))
        assert raw_after["inventoryObservedAt"] == "old-clock"
        assert raw_after["latestInventoryObservation"] == raw["latestInventoryObservation"]
        assert raw_after["newmarkPageObservation"]["prose"] == "source prose"
        assert sql("SELECT description FROM credeals.cre_listings WHERE external_id='" + page_id + "';") == "long prior description"
        assert all(initial_children[t] < child_ids(page_id)[t] for t in child_tables)
        assert sql("SELECT c.name FROM credeals.cre_listing_contacts c JOIN credeals.cre_listings l ON l.id=c.listing_id WHERE l.external_id='" + page_id + "' AND c.email='prior@example.test';") == "Prior Broker"
        sql("UPDATE credeals.cre_listings SET source_url='https://example.test/changed' WHERE external_id='" + page_id + "';")
        before_refusal = sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='" + page_id + "';")
        result = sql(ingest.build_sql([page], nm_job, "2026-11-04T00:20:00Z", set(),
            inventory_preserve_enrichment_ids={("newmark", page_id)}, newmark_page_attempt="b" * 64, newmark_page_claim=page_claim))
        assert "CRE_NEWMARK_PAGE_V1 " + "b" * 64 + " refused" in result
        assert sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='" + page_id + "';") == before_refusal
        sql("UPDATE credeals.cre_listings SET source_url=" + ingest.sql_lit(page_url) +
            " WHERE external_id='" + page_id + "';")
        duplicate = ingest.to_row({"sourceKey": "newmark", "id": "page-duplicate", "url": page_url},
                                  {}, "2026-11-04T00:21:00Z")
        sql(ingest.build_sql([duplicate], nm_job, "2026-11-04T00:21:00Z", set()))
        before_refusal = sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='" + page_id + "';")
        result = sql(ingest.build_sql([page], nm_job, "2026-11-04T00:22:00Z", set(),
            inventory_preserve_enrichment_ids={("newmark", page_id)}, newmark_page_attempt="c" * 64, newmark_page_claim=page_claim))
        assert "CRE_NEWMARK_PAGE_V1 " + "c" * 64 + " refused" in result
        assert sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='" + page_id + "';") == before_refusal
        print("PASS: real generic Newmark mapper and generated SQL preserve old children and add new children")
        print("PASS: Newmark generic/published detail preserves lifecycle, tenure, index and events; later NIM can reappear or qualify unknown parents")

    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
