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
        for kind in ("success", "stale", "duplicate-before", "duplicate-after", "unknown", "unknown-after", "bad-outcome", "refusal-uncertain"):
            external = "worker-" + kind
            url = "https://www.nmrk.com/properties/" + external
            item = {"sourceKey":"newmark","id":external,"url":url,
                    "description":"Original long description",
                    "contactsDetailed":[{"name":"Original Broker","email":"prior@example.test"}]}
            row = ingest.to_row(item, {}, ts)
            sql(ingest.build_sql([row],job,ts,set()))
            native = {"id":"native-"+kind,"slug":external,"externalWebsiteUrl":url,
                      "properties":[{"address":"10 Main Street","city":"Example",
                                     "stateDescription":"Florida","zip":"12345"}]}
            raw = {"sourceKey":"newmark","inventoryObservedAt":"prior-clock",
                   "latestInventoryObservation":{"rawNewmarkNimRecord":native}}
            sql("UPDATE credeals.cre_listings SET raw_data="+ingest.sql_lit(json.dumps(raw))+
                "::jsonb WHERE external_id="+ingest.sql_lit(external)+";")
            sql("INSERT INTO credeals.cre_enrichment_queue (brokerage_id,source_key,external_id,url,reason) "
                "SELECT id,'newmark',"+ingest.sql_lit(external)+","+ingest.sql_lit(url)+",'new' "
                "FROM credeals.cre_brokerages WHERE slug='newmark';")
            if kind == "duplicate-before":
                duplicate=ingest.to_row({"sourceKey":"newmark","id":"duplicate-before-other","url":url}, {}, ts)
                sql(ingest.build_sql([duplicate],job,ts,set()))
            prior_lifecycle=sql("SELECT coalesce(status::text,'<null>') || '|' || coalesce(deleted_at::text,'<null>') || '|' || coalesce(transaction_type,'<null>') FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(external)+";")
            prior_index=sql("SELECT to_jsonb(si)::text FROM credeals.cre_source_index si WHERE external_id="+ingest.sql_lit(external)+";")
            prior_events=sql("SELECT count(*) FROM credeals.cre_listing_events e JOIN credeals.cre_listings l ON l.id=e.listing_id WHERE l.external_id="+ingest.sql_lit(external)+";")
            # Actual source enricher; only its network transport is replaced by a sanitized fixture.
            js = r'''
import fs from "node:fs";
import {createGenericEnricher} from "./lib/enrich.ts";
const claimPath=process.env.FIXTURE_CLAIM, outPath=process.env.FIXTURE_OUT;
const claims=JSON.parse(fs.readFileSync(claimPath,"utf8")), item=claims.items[0], url=item.url;
const html=`<link rel="canonical" href="${url}"><meta property="og:url" content="${url}"><main><div class="property-detail"><div class="property-detail__title"><h1>10 Main Street</h1><p class="street-address">Example, Florida 12345</p></div><div class="property-detail__prose"><p>Property prose from source.</p></div><div class="property-detail__contact"><span class="property-detail__contact-name">Changed Name</span><a href="mailto:prior@example.test">Email</a></div><div class="property-detail__contact"><span class="property-detail__contact-name">New Broker</span><a href="mailto:new@example.test">Email</a></div></div></main>`;
const e=createGenericEnricher(async()=>({rawHtml:html,markdown:"",links:[],metadata:{statusCode:200,url,sourceURL:url}}),()=> "2026-11-04T00:01:00Z");
const result=await e.enrichWithEvidence([item]); if(result.listings.length!==1) throw Error("fixture not admitted");
const listings=result.listings.map(x=>({sourceKey:"newmark",...x}));
const artifact={runMeta:{mode:"enrich",startedAt:"2026-11-04T00:00:00Z",finishedAt:"2026-11-04T00:01:00Z"},
 sources:[{sourceKey:"newmark",supported:true,requested:1,listingsCollected:1,retainedObservations:result.retainedObservations}],
 listings,totalListings:1,brokers:[]};
fs.writeFileSync(outPath,JSON.stringify(artifact),{mode:0o600,flag:"wx"});
'''
            private = root / ("private-"+kind)
            private.mkdir(mode=0o700)
            observed = {}
            def substitute(args, *a, kind=kind, js=js, external=external, url=url, observed=observed, **kw):
                if isinstance(args,list) and "collect.ts" in args:
                    if kind == "duplicate-before":
                        raise AssertionError("prefilter must not call collector")
                    claim_path=args[args.index("--enrich-input")+1]
                    out_path=args[args.index("--out")+1]
                    result=run(["node","--import","tsx","--input-type=module","-e",js],
                               cwd=HERE,env={**os.environ,"FIXTURE_CLAIM":claim_path,"FIXTURE_OUT":out_path},
                               capture_output=True,text=True)
                    if result.returncode:
                        raise RuntimeError("synthetic collector failed: " + result.stderr)
                    if kind in {"stale", "refusal-uncertain"}:
                        sql("UPDATE credeals.cre_listings SET source_url='https://example.test/changed' WHERE external_id="+ingest.sql_lit(external)+";")
                    if kind=="duplicate-after":
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
            if kind=="success":
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

            elif kind in {"stale", "duplicate-before", "duplicate-after"}:
                assert rc==21 and queue=="1|true", (kind,rc,queue)
                assert sql("SELECT raw_data ? 'newmarkPageObservation' FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(external)+";")=="f"
                sql("DELETE FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(external)+";")
            else:
                assert rc==1 and queue=="0|false" and (kind=="refusal-uncertain" or observed.get("attempted")), (kind,rc,queue)
                present=sql("SELECT raw_data ? 'newmarkPageObservation' FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(external)+";")
                assert present == ("t" if kind in {"unknown-after", "bad-outcome"} else "f")
        stale_claim = json.loads(sql("SELECT to_jsonb(q)::text FROM credeals.cre_enrichment_queue q WHERE q.external_id='worker-unknown-after';"))
        sql("UPDATE credeals.cre_enrichment_queue SET claimed_at=claimed_at+interval '1 second' WHERE id="+
            ingest.sql_lit(stale_claim["id"])+"::uuid;")
        try:
            sql(worker.build_newmark_page_disposition_sql(stale_claim, "complete"))
            raise AssertionError("changed claim unexpectedly completed")
        except RuntimeError as exc:
            assert "Newmark page claim changed" in str(exc)
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
