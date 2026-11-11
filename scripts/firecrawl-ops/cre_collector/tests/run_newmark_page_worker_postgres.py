"""Disposable network-none PostgreSQL proof of the actual one-item worker path."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import cre_enrich as worker
import cre_ingest as ingest


def main():
    root = Path(tempfile.mkdtemp(prefix="newmark-worker-proof-"))
    socket = root / "socket"
    socket.mkdir(mode=0o777)
    root.chmod(0o777)
    name = "nm-worker-proof-" + uuid.uuid4().hex[:8]
    run = subprocess.run
    def cmd(args, **kw):
        result=run(args, capture_output=True, text=True, **kw)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip()
    try:
        cmd(["docker", "run", "-d", "--rm", "--name", name, "--network=none",
             "--cpus=0.5", "--memory=512m", "-v", str(socket)+":/var/run/postgresql",
             "-e", "POSTGRES_PASSWORD=synthetic-only", "postgres:17", "-c", "listen_addresses="])
        for _ in range(100):
            logs=run(["docker","logs",name],capture_output=True,text=True)
            if ((logs.stdout + logs.stderr).count("database system is ready to accept connections") >= 2
                    and (socket / ".s.PGSQL.5432").exists()
                    and run(["pg_isready","-h",str(socket),"-U","postgres"],capture_output=True).returncode == 0):
                break
            time.sleep(.2)
        else:
            raise RuntimeError("disposable postgres not ready")
        def sql(q):
            return cmd(["psql","-h",str(socket),"-U","postgres","-d","postgres","-X","-q","-tA",
                        "-v","ON_ERROR_STOP=1"], input=q)
        directory = HERE.parent / "sql"
        master = (directory / "000_run_all.sql").read_text()
        master = re.sub(r"^\\i (\S+)\s*$", lambda m: (directory / m[1]).read_text(), master, flags=re.MULTILINE)
        schema = master + "\n" + "\n".join([
            "\\set CRE_APPROVE_LISTING_LIFECYCLE 1",
            "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-5257",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text()])
        sql(schema)
        url_db = "postgresql://postgres@" + quote(str(socket), safe="") + "/postgres"
        env_path = root / "synthetic.env"
        env_path.write_text("POSTGRES_URL=" + url_db + "\n")
        target = ingest.database_target_fingerprint_from_url(url_db)["value"]
        job = [{"slug":"newmark","discovered":1,"saved":1,"errors":0,"notes":"synthetic"}]
        ts = "2026-11-04T00:00:00Z"
        sql("INSERT INTO credeals.cre_enrichment_queue (brokerage_id,source_key,external_id,url,reason) SELECT id,'svn','other-source','https://example.test/other','new' FROM credeals.cre_brokerages WHERE slug='svn';")
        # Exercise the real claim statement with deliberately tied source rows.
        # Insert the denied-like row first so the old unspecified tie order
        # demonstrably chooses it; the new order chooses the lower-attempt peer.
        def seed_claims(rows):
            sql("DELETE FROM credeals.cre_enrichment_queue WHERE source_key='newmark';")
            for row in rows:
                row_id, attempt, priority, enqueued, claimed = row
                sql("INSERT INTO credeals.cre_enrichment_queue "
                    "(id,brokerage_id,source_key,external_id,url,reason,attempts,priority,enqueued_at,claimed_at) "
                    "SELECT '" + row_id + "', id, 'newmark', 'fixture-' || '" + row_id + "', "
                    "'https://example.test/' || '" + row_id + "', 'new', " + str(attempt) + ", "
                    + str(priority) + ", '" + enqueued + "', " + claimed + " "
                    "FROM credeals.cre_brokerages WHERE slug='newmark';")

        def claim_id():
            return sql(worker.build_claim_sql(1, source="newmark", source_page_witness=True)).splitlines()[-1].split("|")[0]

        high = "00000000-0000-0000-0000-000000000003"
        low_later = "00000000-0000-0000-0000-000000000002"
        low_first = "00000000-0000-0000-0000-000000000001"
        ts_old, ts_new = "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"
        seed_claims([(high, 2, 100, ts_old, "NULL"), (low_later, 0, 100, ts_old, "NULL"),
                     (low_first, 0, 100, ts_old, "NULL")])
        assert claim_id() == low_first, "attempt then ID must break otherwise equal ties"
        assert claim_id() == low_later, "unclaimed equal-attempt peer should be next"
        seed_claims([(high, 4, 99, ts_new, "NULL"), (low_first, 0, 100, ts_old, "NULL")])
        assert claim_id() == high, "priority must precede attempts"
        seed_claims([(high, 4, 100, ts_old, "NULL"), (low_first, 0, 100, ts_new, "NULL")])
        assert claim_id() == high, "enqueue time must precede attempts"
        seed_claims([(high, 4, 100, ts_old, "NULL"), (low_first, 0, 99, ts_old, "now() - interval '2 hours'")])
        assert claim_id() == high, "unclaimed rows must precede reclaimed rows"
        seed_claims([(high, 4, 100, ts_old, "now() - interval '2 hours'"),
                     (low_first, 0, 99, ts_old, "now() - interval '5 minutes'")])
        assert claim_id() == high, "cooldown must exclude recently claimed rows"
        sql("DELETE FROM credeals.cre_enrichment_queue WHERE source_key='newmark';")
        assert "other-source" in sql(worker.build_claim_sql(1, source="svn")), "other source must retain claim behavior"
        sql("UPDATE credeals.cre_enrichment_queue SET claimed_at=NULL WHERE source_key='svn';")

        for kind in ("success", "success-no-license", "full-success", "full-stale", "full-conflict", "full-conflict-long", "full-locality-conflict", "full-unrecognized", "stale", "duplicate-before", "generic-duplicate-before", "duplicate-collect-fail", "duplicate-claim-stale", "duplicate-after", "unknown", "unknown-after", "bad-outcome", "refusal-uncertain", "claim-stale", "claim-attempt-stale", "collect-fail-stale", "empty-stale", "deferred-stale", "generic-success", "generic-empty", "generic-whitespace", "generic-stale", "generic-url-changed", "generic-duplicate-after", "generic-missing", "generic-malformed", "generic-multiple", "generic-conflict", "generic-redirect", "generic-status", "generic-canonical", "generic-address", "generic-native-change", "generic-no-canonical", "generic-no-metadata", "generic-source-redirect", "generic-og-mismatch", "generic-equivalent", "generic-host-mismatch", "generic-404-mismatch", "generic-empty-page", "generic-403", "generic-403-omitted", "generic-403-stale"):
            if kind == "success-no-license":
                sql("ALTER TABLE credeals.cre_listing_contacts DROP COLUMN license;")
            external = "worker-" + kind
            url = "https://www.nmrk.com/properties/" + external
            item = {"sourceKey":"newmark","id":external,"url":url,
                    "name":"Original title", "description":"Original long description",
                    "contactsDetailed":[{"name":"Original Broker","email":"prior@example.test"}]}
            row = ingest.to_row(item, {}, ts)
            sql(ingest.build_sql([row],job,ts,set()))
            native = {"id":"native-"+kind,"slug":external,"externalWebsiteUrl":url,
                      "properties":[{"address":"10 Main Street","city":"Example",
                                     "stateDescription":"Florida","zip":"12345"}]}
            if kind == "generic-malformed":
                native["properties"][0]["address"] = None
            if kind == "generic-multiple":
                native["properties"].append(dict(native["properties"][0]))
            raw = {"sourceKey":"newmark","inventoryObservedAt":"prior-clock",
                   "latestInventoryObservation":{"rawNewmarkNimRecord":native}}
            if kind in {"generic-missing", "generic-no-canonical", "generic-no-metadata"}:
                raw["latestInventoryObservation"] = {}
            if kind == "generic-conflict":
                raw["rawNewmarkNimRecord"] = {**native, "id":"conflicting"}
            sql("UPDATE credeals.cre_listings SET raw_data="+ingest.sql_lit(json.dumps(raw))+
                "::jsonb WHERE external_id="+ingest.sql_lit(external)+";")
            sql("INSERT INTO credeals.cre_enrichment_queue (brokerage_id,source_key,external_id,url,reason) "
                "SELECT id,'newmark',"+ingest.sql_lit(external)+","+ingest.sql_lit(url)+",'new' "
                "FROM credeals.cre_brokerages WHERE slug='newmark';")
            if kind == "generic-success":
                sql("UPDATE credeals.cre_listings SET title=E'\\tOriginal title\\n', description=E' Original long description\\t' WHERE external_id="+ingest.sql_lit(external)+";")
            if kind == "generic-empty":
                sql("UPDATE credeals.cre_listings SET title='', description=NULL WHERE external_id="+ingest.sql_lit(external)+";")
            if kind == "generic-whitespace":
                sql("UPDATE credeals.cre_listings SET title=E'\\t\\n', description=E' \\n\\t ' WHERE external_id="+ingest.sql_lit(external)+";")
            if kind in {"duplicate-before", "generic-duplicate-before", "duplicate-collect-fail", "duplicate-claim-stale"}:
                duplicate=ingest.to_row({"sourceKey":"newmark","id":"duplicate-before-other-"+kind,"url":url}, {}, ts)
                sql(ingest.build_sql([duplicate],job,ts,set()))
                if kind == "duplicate-before":
                    sql("INSERT INTO credeals.cre_enrichment_queue (brokerage_id,source_key,external_id,url,reason) "
                        "SELECT id,'newmark','peer-after-ambiguous','https://www.nmrk.com/properties/peer','new' "
                        "FROM credeals.cre_brokerages WHERE slug='newmark';")
            if kind == "generic-403":
                sql("INSERT INTO credeals.cre_enrichment_queue (brokerage_id,source_key,external_id,url,reason) "
                    "SELECT id,'newmark','peer-after-403','https://www.nmrk.com/properties/peer-403','new' "
                    "FROM credeals.cre_brokerages WHERE slug='newmark';")
            prior_lifecycle=sql("SELECT coalesce(status::text,'<null>') || '|' || coalesce(deleted_at::text,'<null>') || '|' || coalesce(transaction_type,'<null>') FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(external)+";")
            prior_index=sql("SELECT to_jsonb(si)::text FROM credeals.cre_source_index si WHERE external_id="+ingest.sql_lit(external)+";")
            prior_events=sql("SELECT count(*) FROM credeals.cre_listing_events e JOIN credeals.cre_listings l ON l.id=e.listing_id WHERE l.external_id="+ingest.sql_lit(external)+";")
            # Actual source enricher; only its network transport is replaced by a sanitized fixture.
            js = r'''
import fs from "node:fs";
import {createGenericEnricher} from "./lib/enrich.ts";
const claimPath=process.env.FIXTURE_CLAIM, outPath=process.env.FIXTURE_OUT;
const claims=JSON.parse(fs.readFileSync(claimPath,"utf8")), item=claims.items[0], url=item.url;
const mode=process.env.FIXTURE_MODE;
let html=fs.readFileSync("./tests/fixtures/newmark-property-detail-sanitized.html","utf8").replaceAll("{{URL}}",url);
if (mode.startsWith("full-")) {
  html=html.replace("Example, Florida 12345", "10 Main Street, Example, Florida 12345")
    .replace("10 Main Street</h1>",
      (mode==="full-conflict" ? "20 Other Street" : mode==="full-conflict-long" ? "20 Other Street, Example, Florida 12345" : mode==="full-locality-conflict" ? "10 Main Street, Other, Florida 12345" : mode==="full-unrecognized" ? "Property at 20 Other Street" : "Marketing title")+"</h1>");
  if (!html.includes('>10 Main Street, Example, Florida 12345</p>')) throw Error('full-address fixture invalid');
  if (mode==="full-conflict-long" && !html.includes('>20 Other Street, Example, Florida 12345</h1>')) throw Error('conflicting heading fixture invalid');
  if (mode==="full-locality-conflict" && !html.includes('>10 Main Street, Other, Florida 12345</h1>')) throw Error('conflicting locality fixture invalid');
  if (mode==="full-unrecognized" && !html.includes('>Property at 20 Other Street</h1>')) throw Error('unrecognized heading fixture invalid');
}
if (["generic-no-canonical","generic-no-metadata","generic-empty-page"].includes(mode)) html="<html><body>No special page template.</body></html>";
if (mode.startsWith("generic-") && mode!=="generic-empty-page") html += '<script type="application/ld+json">{"@type":"RealEstateListing","name":"Fixture listing","description":"Fixture description"}</script>';
if (mode==="generic-canonical") html=html.replace('rel="canonical" href="'+url+'"', 'rel="canonical" href="https://www.nmrk.com/properties/other"');
if (mode==="generic-address") html=html.replace("10 Main Street", "20 Other Street");
if (mode==="generic-og-mismatch") html=html.replace('property="og:url" content="'+url+'"', 'property="og:url" content="https://www.nmrk.com/properties/other"');
if (mode==="generic-equivalent") html=html.replaceAll(url, url+"/?ref=example#details");
if (mode==="generic-404-mismatch") html=html.replace('rel="canonical" href="'+url+'"', 'rel="canonical" href="https://www.nmrk.com/"');
if (mode==="generic-403-omitted") html="x".repeat(300000);
const e=createGenericEnricher(async()=>({rawHtml:html,markdown:"",links:[],metadata:{
 ...(mode==="generic-no-metadata" ? {} : {statusCode:mode.startsWith("generic-403") ? 403 : ["generic-status","generic-404-mismatch"].includes(mode) ? 404 : 200,
 url:mode==="generic-redirect" ? "https://www.nmrk.com/properties/other"
     : mode==="generic-host-mismatch" ? url.replace("https://www.nmrk.com", "https://other.example")
     : mode==="generic-404-mismatch" ? "https://www.nmrk.com/" : mode==="generic-equivalent" ? url+"/?a=1#map" : url,
 sourceURL:mode==="generic-source-redirect" ? "https://www.nmrk.com/properties/other"
     : mode==="generic-equivalent" ? url+"?a=1#map" : url})}}),()=> "2026-11-04T00:01:00Z");
const result=await e.enrichWithEvidence([item]);
const listings=result.listings.map(x=>({sourceKey:"newmark",...x}));
const artifact={runMeta:{mode:"enrich",startedAt:"2026-11-04T00:00:00Z",finishedAt:"2026-11-04T00:01:00Z"},
 sources:[{sourceKey:"newmark",supported:true,requested:1,listingsCollected:listings.length,retainedObservations:result.retainedObservations}],
 listings,totalListings:listings.length,brokers:[]};
fs.writeFileSync(outPath,JSON.stringify(artifact),{mode:0o600,flag:"wx"});
'''
            private = root / ("private-"+kind)
            private.mkdir(mode=0o700)
            observed = {}
            def substitute(args, *a, kind=kind, js=js, external=external, url=url, observed=observed, **kw):
                if isinstance(args,list) and "collect.ts" in args:
                    claim_path=args[args.index("--enrich-input")+1]
                    out_path=args[args.index("--out")+1]
                    if kind == "duplicate-collect-fail":
                        return subprocess.CompletedProcess(args,1)
                    if kind == "collect-fail-stale":
                        sql("UPDATE credeals.cre_enrichment_queue SET claimed_at=claimed_at+interval '1 second' WHERE external_id="+ingest.sql_lit(external)+";")
                        return subprocess.CompletedProcess(args,1)
                    result=run(["node","--import","tsx","--input-type=module","-e",js],
                               cwd=HERE,env={**os.environ,"FIXTURE_CLAIM":claim_path,"FIXTURE_OUT":out_path,"FIXTURE_MODE":kind},
                               capture_output=True,text=True)
                    if result.returncode:
                        raise RuntimeError("synthetic collector failed: " + result.stderr)
                    if kind in {"duplicate-claim-stale", "generic-403-stale"}:
                        sql("UPDATE credeals.cre_enrichment_queue SET claimed_at=claimed_at+interval '1 second' WHERE external_id="+ingest.sql_lit(external)+";")
                    if kind == "claim-attempt-stale":
                        sql("UPDATE credeals.cre_enrichment_queue SET attempts=attempts+1 WHERE external_id="+ingest.sql_lit(external)+";")
                    if kind in {"claim-stale", "empty-stale", "deferred-stale", "generic-stale"}:
                        if kind in {"empty-stale", "deferred-stale"}:
                            data=json.loads(Path(out_path).read_text())
                            data["listings"]=[]
                            data["totalListings"]=0
                            if kind == "deferred-stale":
                                data["sources"][0]["error"]="synthetic failure"
                            Path(out_path).write_text(json.dumps(data))
                        sql("UPDATE credeals.cre_enrichment_queue SET claimed_at=claimed_at+interval '1 second' WHERE external_id="+ingest.sql_lit(external)+";")
                    if kind in {"generic-native-change", "full-stale"}:
                        sql("UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data, '{latestInventoryObservation,rawNewmarkNimRecord,id}', to_jsonb('changed'::text)) WHERE external_id="+ingest.sql_lit(external)+";")
                    if kind in {"stale", "refusal-uncertain", "generic-url-changed"}:
                        sql("UPDATE credeals.cre_listings SET source_url='https://example.test/changed' WHERE external_id="+ingest.sql_lit(external)+";")
                    if kind in {"duplicate-after", "generic-duplicate-after"}:
                        duplicate=ingest.to_row({"sourceKey":"newmark","id":"duplicate-after-other","url":url}, {}, ts)
                        sql(ingest.build_sql([duplicate],job,ts,set()))
                    return subprocess.CompletedProcess(args,0)
                if isinstance(args,list) and any(str(x).endswith("/cre_ingest.py") for x in args):
                    if kind=="unknown":
                        observed["attempted"]=True
                        return subprocess.CompletedProcess(args,1,stdout="")
                    if kind in {"unknown-after", "bad-outcome"}:
                        result=run(args,*a,**kw)
                        assert result.returncode==0
                        observed["attempted"]=True
                        return subprocess.CompletedProcess(args,1 if kind=="unknown-after" else 0,stdout="truncated")
                return run(args,*a,**kw)
            worker.subprocess.run=substitute
            original_query = worker._psql_query
            def query_with_uncertain_rollback(db_url, statement, kind=kind, original_query=original_query):
                if kind == "refusal-uncertain" and "artifact_run_key=" in statement:
                    return [("1",)]
                return original_query(db_url, statement)
            worker._psql_query = query_with_uncertain_rollback
            try:
                args=SimpleNamespace(batch=1,source="newmark",matthews_targeted_refresh=False,
                    artifact_out=str(private/"enriched.json"),expected_db_target_sha256=target,
                    env_file=str(env_path),dry_run=False,guarded_run_id=None)
                rc=worker.run(args)
            finally:
                worker.subprocess.run=run
                worker._psql_query=original_query
            queue=sql("SELECT attempts || '|' || (claimed_at IS NULL)::text FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(external)+";")
            if kind in {"generic-success", "generic-empty", "generic-whitespace", "generic-missing",
                        "generic-malformed", "generic-multiple", "generic-conflict", "generic-address",
                        "generic-native-change", "generic-no-canonical", "generic-no-metadata", "generic-equivalent"}:
                assert rc==0 and queue=="", (kind,rc,queue)
                assert sql("SELECT raw_data ? 'genericEnrich' FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(external)+";")=="t"
                values = json.loads(sql("SELECT json_build_object('title',title,'description',description,'evidence',raw_data->'newmarkGenericPageObservation')::text FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(external)+";"))
                assert values["title"] == ("Fixture listing" if kind in {"generic-empty", "generic-whitespace"} else "\tOriginal title\n" if kind == "generic-success" else "Original title"), values
                assert values["description"] == ("Fixture description" if kind in {"generic-empty", "generic-whitespace"} else " Original long description\t" if kind == "generic-success" else "Original long description"), values
                assert values["evidence"]["name"] == "Fixture listing" and values["evidence"]["description"] == "Fixture description", values
            elif kind == "generic-stale":
                assert rc==21 and queue=="0|false", (kind,rc,queue)
                assert sql("SELECT raw_data ? 'genericEnrich' FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(external)+";")=="f"
            elif kind in {"success", "success-no-license", "full-success"}:
                assert rc==0 and queue=="", (kind,rc,queue)
                assert sql("SELECT description FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(external)+";")=="Original long description"
                assert sql("SELECT raw_data->'newmarkPageObservation'->>'prose' FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(external)+";")=="Property prose from source."
                assert sql("SELECT count(*) FROM credeals.cre_listing_contacts c JOIN credeals.cre_listings l ON l.id=c.listing_id WHERE l.external_id="+ingest.sql_lit(external)+";")=="2"
                assert sql("SELECT c.name FROM credeals.cre_listing_contacts c JOIN credeals.cre_listings l ON l.id=c.listing_id WHERE l.external_id="+ingest.sql_lit(external)+" AND c.email='prior@example.test';")=="Original Broker"
                assert sql("SELECT coalesce(status::text,'<null>') || '|' || coalesce(deleted_at::text,'<null>') || '|' || coalesce(transaction_type,'<null>') FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(external)+";")==prior_lifecycle
                assert sql("SELECT to_jsonb(si)::text FROM credeals.cre_source_index si WHERE external_id="+ingest.sql_lit(external)+";")==prior_index
                assert sql("SELECT count(*) FROM credeals.cre_listing_events e JOIN credeals.cre_listings l ON l.id=e.listing_id WHERE l.external_id="+ingest.sql_lit(external)+";")==prior_events
                assert sql("SELECT raw_data->>'inventoryObservedAt' FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(external)+";")=="prior-clock"
                later=ingest.to_row({"sourceKey":"newmark","id":external,"url":url,
                    "name":"later source inventory","preserveChildCollections":True}, {}, "2026-11-04T00:02:00Z")
                sql(ingest.build_sql([later],job,"2026-11-04T00:02:00Z",set()))
                assert sql("SELECT raw_data->'newmarkPageObservation'->>'prose' FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(external)+";")=="Property prose from source."
                assert sql("SELECT count(*) FROM credeals.cre_listing_contacts c JOIN credeals.cre_listings l ON l.id=c.listing_id WHERE l.external_id="+ingest.sql_lit(external)+";")=="2"

            elif kind in {"stale", "full-stale", "duplicate-before", "generic-duplicate-before", "duplicate-collect-fail", "duplicate-claim-stale", "duplicate-after", "generic-url-changed", "generic-duplicate-after", "generic-redirect", "generic-source-redirect", "generic-og-mismatch", "generic-host-mismatch", "generic-canonical"}:
                assert rc==21 and queue=="0|false", (kind,rc,queue)
                assert sql("SELECT raw_data ? 'newmarkPageObservation' OR raw_data ? 'genericEnrich' FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(external)+";")=="f"
                if kind in {"duplicate-before", "generic-duplicate-before", "duplicate-claim-stale", "generic-redirect", "generic-source-redirect", "generic-og-mismatch", "generic-host-mismatch", "generic-canonical"}:
                    evidence = json.loads((private/"enriched.json").read_text())
                    observation = evidence["sources"][0]["retainedObservations"][0]
                    assert observation["claim"]["identity"] == "unverified" and observation["scrapedDoc"]["rawHtml"], observation
                    if kind == "duplicate-claim-stale":
                        assert sql("SELECT last_error IS NULL FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(external)+";") == "t"
                    if kind == "duplicate-before":
                        # Even once its cooldown expires, an unclaimed peer wins.
                        sql("UPDATE credeals.cre_enrichment_queue SET claimed_at=now()-interval '2 hours' WHERE external_id="+ingest.sql_lit(external)+";")
                        next_claim = sql(worker.build_claim_sql(1, source="newmark", source_page_witness=True))
                        assert "peer-after-ambiguous" in next_claim, next_claim
                        sql("DELETE FROM credeals.cre_enrichment_queue WHERE external_id='peer-after-ambiguous';")
                sql("DELETE FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(external)+";")
            elif kind in {"generic-403", "generic-403-omitted", "generic-403-stale"}:
                assert rc == 21 and queue == "0|false", (kind,rc,queue)
                assert sql("SELECT raw_data ? 'genericEnrich' FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(external)+";") == "f"
                evidence=json.loads((private/"enriched.json").read_text())["sources"][0]["retainedObservations"][0]
                assert evidence["scrapedDoc"]["metadata"]["statusCode"] == 403
                if kind == "generic-403-omitted":
                    assert evidence["omissions"]["rawHtml"] == "field_byte_limit"
                else:
                    assert evidence["scrapedDoc"]["rawHtml"]
                if kind == "generic-403-stale":
                    assert sql("SELECT last_error IS NULL FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(external)+";") == "t"
                else:
                    assert sql("SELECT last_error FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(external)+";") == "Newmark provider HTTP 403"
                if kind == "generic-403":
                    next_claim=sql(worker.build_claim_sql(1, source="newmark", source_page_witness=True))
                    assert "peer-after-403" in next_claim, next_claim
                    sql("DELETE FROM credeals.cre_enrichment_queue WHERE external_id='peer-after-403';")
                sql("DELETE FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(external)+";")
            elif kind in {"full-conflict", "full-conflict-long", "full-locality-conflict", "full-unrecognized", "generic-status", "generic-404-mismatch", "generic-empty-page"}:
                assert rc == 21 and queue == "1|true", (kind, rc, queue)
                evidence = json.loads((private/"enriched.json").read_text())
                assert evidence["sources"][0]["retainedObservations"][0]["scrapedDoc"]["rawHtml"]
                assert sql("SELECT raw_data ? 'genericEnrich' FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(external)+";") == "f"
                sql("DELETE FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(external)+";")
            elif kind in {"claim-stale", "claim-attempt-stale", "collect-fail-stale", "empty-stale", "deferred-stale"}:
                assert rc in (1,21) and queue==("1|false" if kind=="claim-attempt-stale" else "0|false"), (kind,rc,queue)
                assert sql("SELECT raw_data ? 'newmarkPageObservation' FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(external)+";")=="f"
            else:
                assert rc==1 and queue=="0|false" and (kind=="refusal-uncertain" or observed.get("attempted")), (kind,rc,queue)
                present=sql("SELECT raw_data ? 'newmarkPageObservation' FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(external)+";")
                assert present == ("t" if kind in {"unknown-after", "bad-outcome"} else "f")
        # Ordinary ingest keeps its existing incoming-wins behavior outside the guarded path.
        ordinary = ingest.to_row({"sourceKey":"newmark", "id":"ordinary-newmark",
                                  "url":"https://www.nmrk.com/properties/ordinary-newmark",
                                  "name":"Before", "description":"Before description"}, {}, ts)
        sql(ingest.build_sql([ordinary], job, ts, set()))
        ordinary_update = ingest.to_row({"sourceKey":"newmark", "id":"ordinary-newmark",
                                         "url":"https://www.nmrk.com/properties/ordinary-newmark",
                                         "name":"After", "description":"After description",
                                         "genericEnrich":{"hadJsonLd":True},
                                         "preserveChildCollections":True}, {}, ts)
        sql(ingest.build_sql([ordinary_update], job, ts, set()))
        assert sql("SELECT title || '|' || description FROM credeals.cre_listings WHERE external_id='ordinary-newmark';") == "After|After description"
        stale_claim = json.loads(sql("SELECT to_jsonb(q)::text FROM credeals.cre_enrichment_queue q WHERE q.external_id='worker-unknown-after';"))
        sql("UPDATE credeals.cre_enrichment_queue SET claimed_at=claimed_at+interval '1 second' WHERE id="+
            ingest.sql_lit(stale_claim["id"])+"::uuid;")
        assert sql(worker.build_newmark_page_disposition_sql(stale_claim, "complete")) == "0"
        assert sql("SELECT count(*) FROM credeals.cre_enrichment_queue WHERE id="+ingest.sql_lit(stale_claim["id"])+"::uuid;")=="1"
        assert sql("SELECT attempts || '|' || (claimed_at IS NULL)::text FROM credeals.cre_enrichment_queue WHERE source_key='svn';") == "0|true"
        print("PASS: actual source enricher and worker with disposable PostgreSQL: commit, stale/duplicate retry, unknown before/after commit, unrelated source untouched")
    finally:
        worker.subprocess.run=run
        subprocess.run(["docker","exec",name,"rm","-f","/var/run/postgresql/.s.PGSQL.5432","/var/run/postgresql/.s.PGSQL.5432.lock"],capture_output=True,check=False)
        subprocess.run(["docker","rm","-f",name],capture_output=True,check=False)
        shutil.rmtree(root)

if __name__=="__main__":
    main()
