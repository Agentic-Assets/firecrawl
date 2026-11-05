"""Disposable PostgreSQL proof for source-scoped monitor queue rearming."""
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import cre_ingest as ingest  # noqa: E402
import cre_monitor as monitor  # noqa: E402
from test_monitor import _derive, _gfin, _idx  # noqa: E402


def run(*args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def main():
    container = "svn-requeue-proof-" + uuid.uuid4().hex[:10]
    def sql(value):
        return run("docker", "exec", "-i", container, "psql", "-U", "postgres", "-X", "-q", "-tA",
                   "-v", "ON_ERROR_STOP=1", input=value)
    try:
        run("docker", "run", "-d", "--rm", "--name", container, "--network=none", "--cpus=0.5",
            "--memory=512m", "--pids-limit=128", "-e", "POSTGRES_PASSWORD=synthetic-only", "postgres:17")
        for _ in range(100):
            logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
            ready = subprocess.run(["docker", "exec", container, "pg_isready", "-U", "postgres"],
                                   capture_output=True).returncode == 0
            if ready and (logs.stdout + logs.stderr).count("database system is ready to accept connections") >= 2:
                break
            time.sleep(.2)
        else:
            raise RuntimeError("postgres not ready")
        directory = ROOT.parent / "sql"
        schema = (directory / "000_run_all.sql").read_text()
        schema = re.sub(r"^\\i (\S+)\s*$", lambda m: (directory / m[1]).read_text(), schema, flags=re.M)
        schema += "\n" + "\n".join([
            "\\set CRE_APPROVE_LISTING_LIFECYCLE 1",
            "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-5257",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text()])
        sql(schema)
        ids = ["change", "claimed", "ready", "unchanged", "reappear", "foreign", "reappear-new", "reappear-inactive", "reappear-deleted", "change-new", "new-reason", "both-reasons", "active-sibling", "url-only", "foreign-index"]
        for eid in ids:
            row = ingest.to_row({"sourceKey":"svn", "id":eid, "url":"https://example.test/old-"+eid,
                                 "name":"Synthetic", "transactionMode":"sale"}, {}, "2026-11-04T00:00:00Z")
            sql(ingest.build_sql([row], [{"slug":"svn","discovered":1,"saved":1,"errors":0,"notes":"test"}],
                                 "2026-11-04T00:00:00Z", set()))
        broker = sql("SELECT id FROM credeals.cre_brokerages WHERE slug='svn';")
        old = {eid:_gfin(eid, source_key="svn", status="active", sale_price_usd=100,
                         url="https://example.test/old-"+eid) for eid in ids}
        for eid in ids:
            si = old[eid]
            sql("UPDATE credeals.cre_source_index SET source_key="+ingest.sql_lit("lee-associates" if eid=="foreign-index" else "svn")+", fingerprint="+ingest.sql_lit(si["fingerprint"])+
                ", observed_status='active', observation_present="+("false" if eid.startswith("reappear") else "true")+
                ", soft_deleted="+("true" if eid.startswith("reappear") else "false")+" WHERE external_id="+ingest.sql_lit(eid)+";")
            if eid in {"reappear-new", "reappear-inactive", "reappear-deleted", "change-new"}:
                if eid == "reappear-inactive":
                    sql("UPDATE credeals.cre_listings SET status='inactive' WHERE external_id='reappear-inactive';")
                if eid == "reappear-deleted":
                    sql("UPDATE credeals.cre_listings SET deleted_at=now() WHERE external_id='reappear-deleted';")
                continue
            sql("INSERT INTO credeals.cre_enrichment_queue (brokerage_id,source_key,external_id,url,reason,attempts,last_error,claimed_at) VALUES ("+
                ingest.sql_lit(broker)+"::uuid,"+ingest.sql_lit("lee-associates" if eid=="foreign" else "svn")+","+ingest.sql_lit(eid)+","+ingest.sql_lit(si["url"])+
                ","+ingest.sql_lit("new" if eid=="new-reason" else "changed")+","+("1" if eid=="ready" else "5")+",'prior failure',"+("now()" if eid=="claimed" else "NULL")+");")
            if eid in {"both-reasons", "active-sibling"}:
                sql("INSERT INTO credeals.cre_enrichment_queue (brokerage_id,source_key,external_id,url,reason,attempts,last_error) VALUES ("+
                    ingest.sql_lit(broker)+"::uuid,'svn',"+ingest.sql_lit(eid)+","+ingest.sql_lit(si["url"])+
                    ",'new',"+("1" if eid=="active-sibling" else "5")+",'other failure');")
        current={}
        prior={}
        listings={}
        for eid in ids:
            price=100 if eid in {"unchanged","reappear","reappear-new","reappear-inactive","reappear-deleted","url-only"} else 200
            g=_gfin(eid, source_key="svn", status="active", sale_price_usd=price,
                    url=("https://example.test/old-" if eid=="unchanged" else "https://example.test/new-")+eid)
            current[(broker,eid)]=g
            index=_idx(old[eid], soft_deleted=eid.startswith("reappear"), observed_status="active")
            index["observation_present"]=not eid.startswith("reappear")
            index["presence_generation"]=0
            if eid=="foreign-index":
                index["source_key"]="lee-associates"
            prior[(broker,eid)]=index
            listing_id=sql("SELECT id FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(eid)+";")
            listings[(broker,eid)]={"id":listing_id,"status":"inactive" if eid=="reappear-inactive" else "active","deleted":eid=="reappear-deleted"}
        lee_row=ingest.to_row({"sourceKey":"lee-associates", "id":"lee-control",
                               "url":"https://example.test/lee", "transactionMode":"sale"}, {}, "2026-11-04T00:00:00Z")
        sql(ingest.build_sql([lee_row], [{"slug":"lee-associates","discovered":1,"saved":1,"errors":0,"notes":"test"}],
                             "2026-11-04T00:00:00Z", set()))
        lee_bid=sql("SELECT id FROM credeals.cre_brokerages WHERE slug='lee-associates';")
        lee_old=_gfin("lee-control", source_key="lee-associates", status="active", sale_price_usd=100,
                       url="https://example.test/lee")
        sql("UPDATE credeals.cre_source_index SET source_key='lee-associates', fingerprint="+ingest.sql_lit(lee_old["fingerprint"])+
            ", observed_status='active', observation_present=true WHERE external_id='lee-control';")
        sql("INSERT INTO credeals.cre_enrichment_queue (brokerage_id,source_key,external_id,url,reason,attempts,last_error) VALUES ("+
            ingest.sql_lit(lee_bid)+"::uuid,'lee-associates','lee-control','https://example.test/lee','changed',5,'lee failure');")
        current[(lee_bid,"lee-control")]=_gfin("lee-control",source_key="lee-associates",status="active",sale_price_usd=200,
                                                url="https://example.test/lee-new")
        prior[(lee_bid,"lee-control")]=_idx(lee_old,observed_status="active")
        listings[(lee_bid,"lee-control")]={"id":sql("SELECT id FROM credeals.cre_listings WHERE external_id='lee-control';"),
                                          "status":"active","deleted":False}
        # A healthy unchanged non-SVN monitor needs no queue privileges.
        # Execute the real generated transaction under a restricted role, not
        # merely a string assertion against the emitted SQL.
        sql("CREATE ROLE monitor_without_queue BYPASSRLS; GRANT USAGE ON SCHEMA credeals TO monitor_without_queue; "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA credeals TO monitor_without_queue; "
            "REVOKE ALL ON credeals.cre_enrichment_queue FROM monitor_without_queue;")
        assert sql("SELECT has_table_privilege('monitor_without_queue', "
                   "'credeals.cre_enrichment_queue', 'SELECT');") == "f"
        lee_key=(lee_bid,"lee-control")
        lee_snapshot={lee_key:lee_old}
        lee_prior={lee_key:prior[lee_key]}
        lee_listings={lee_key:listings[lee_key]}
        lee_events,lee_new,lee_changed,lee_marks,_=_derive(lee_snapshot,lee_prior,lee_listings)
        assert not lee_events and not lee_new and not lee_changed and not lee_marks
        lee_sql=monitor.build_write_sql([lee_old],lee_events,lee_new,lee_changed,lee_marks,
                                        str(uuid.uuid4()),"2026-11-04T00:59:00Z","fixture",
                                        ["lee-associates"],prior_index=lee_prior,prior_listings=lee_listings,
                                        brokerage_by_slug={"lee-associates":lee_bid})
        sql("SET ROLE monitor_without_queue;\n"+lee_sql)
        assert sql("SELECT fingerprint FROM credeals.cre_source_index WHERE external_id='lee-control';")==lee_old["fingerprint"]
        events,new,changed,marks,_=_derive(current,prior,listings)
        assert (broker,"svn","reappear") not in changed
        assert (broker,"svn","unchanged") not in changed
        run_id=str(uuid.uuid4())
        statement=monitor.build_write_sql(list(current.values()),events,new,changed,marks,run_id,
                                          "2026-11-04T01:00:00Z","fixture",["svn"],prior_index=prior,prior_listings=listings,brokerage_by_slug={"svn":broker})
        monitor_output=sql(statement)
        marker="CRE_MONITOR_SVN_SUPPRESSED_V1 "
        records=[line[len(marker):] for line in monitor_output.splitlines() if line.startswith(marker)]
        assert len(records)==1 and json.loads(records[0])==[[broker,"claimed"],[broker,"foreign"],[broker,"foreign-index"]], records
        assert sql("SELECT status FROM credeals.cre_scrape_jobs WHERE id="+ingest.sql_lit(run_id)+"::uuid;")=="partial"
        result=sql("SELECT external_id || '|' || attempts || '|' || (claimed_at IS NOT NULL)::text || '|' || url || '|' || COALESCE(last_error,'<null>') FROM credeals.cre_enrichment_queue ORDER BY external_id;").splitlines()
        values={line.split("|")[0]:line.split("|")[1:] for line in result}
        assert values["change"]==["0","false","https://example.test/new-change","prior failure"],values
        assert values["url-only"]==["0","false","https://example.test/new-url-only","prior failure"],values
        assert values["reappear"]==["0","false","https://example.test/new-reappear","prior failure"],values
        assert values["reappear-new"][0:3]==["0","false","https://example.test/new-reappear-new"],values
        assert "reappear-inactive" not in values and "reappear-deleted" not in values,values
        assert values["change-new"][0:3]==["0","false","https://example.test/new-change-new"],values
        assert values["new-reason"]==["0","false","https://example.test/new-new-reason","prior failure"],values
        assert sql("SELECT reason || '|' || attempts FROM credeals.cre_enrichment_queue WHERE external_id='both-reasons' ORDER BY reason;").splitlines()==["changed|0","new|5"]
        assert sql("SELECT reason || '|' || attempts FROM credeals.cre_enrichment_queue WHERE external_id='active-sibling' ORDER BY reason;").splitlines()==["changed|5","new|1"]
        assert values["foreign-index"]==["5","false","https://example.test/old-foreign-index","prior failure"],values
        assert sql("SELECT source_key FROM credeals.cre_source_index WHERE external_id='foreign-index';")=="lee-associates"
        assert values["claimed"][0:2]==["5","true"],values
        assert values["ready"]==["1","false","https://example.test/new-ready","prior failure"],values
        assert values["unchanged"][0]=="5" and values["foreign"][0]=="5",values
        assert values["lee-control"]==["5","false","https://example.test/lee","lee failure"],values
        # Old snapshots must fail before modifying the index or queue.
        sql("UPDATE credeals.cre_enrichment_queue SET attempts=5 WHERE external_id='change';")
        replay=sql(statement)
        assert "CRE_MONITOR_SVN_REPLAY_V1" in replay.splitlines()
        assert sql("SELECT status FROM credeals.cre_scrape_jobs WHERE id="+ingest.sql_lit(run_id)+"::uuid;")=="partial"
        assert sql("SELECT attempts FROM credeals.cre_enrichment_queue WHERE external_id='change';")=="5"
        sql("UPDATE credeals.cre_source_index SET fingerprint="+ingest.sql_lit(lee_old["fingerprint"])+" WHERE external_id='lee-control';")
        stale=monitor.build_write_sql(list(current.values()),events,new,changed,marks,str(uuid.uuid4()),
                                      "2026-11-04T01:01:00Z","fixture",["svn"],prior_index=prior,prior_listings=listings,brokerage_by_slug={"svn":broker})
        sql(stale)
        assert sql("SELECT fingerprint FROM credeals.cre_source_index WHERE external_id='change';")==current[(broker,"change")]["fingerprint"]
        assert sql("SELECT fingerprint FROM credeals.cre_source_index WHERE external_id='lee-control';")==current[(lee_bid,"lee-control")]["fingerprint"]
        # A claimed old generation leaves the old fingerprint in place while
        # peers progress. Worker completion cannot consume the newer change.
        assert sql("SELECT fingerprint FROM credeals.cre_source_index WHERE external_id='claimed';")==old["claimed"]["fingerprint"]
        claimed_id=sql("SELECT id FROM credeals.cre_enrichment_queue WHERE external_id='claimed';")
        key=(broker,"claimed")
        claim_listing=listings[key]["id"]
        assert sql("SELECT count(*) FROM credeals.cre_listing_events WHERE listing_id="+ingest.sql_lit(claim_listing)+"::uuid;")=="0"
        pending_events,pending_new,pending_changed,pending_marks,_=_derive(
            {key:current[key]},{key:prior[key]},{key:listings[key]})
        pending=monitor.build_write_sql([current[key]],pending_events,pending_new,pending_changed,pending_marks,
                                       str(uuid.uuid4()),"2026-11-04T01:01:20Z","fixture",["svn"],
                                       prior_index={key:prior[key]},prior_listings={key:listings[key]},
                                       brokerage_by_slug={"svn":broker})
        sql(pending)
        assert sql("SELECT count(*) FROM credeals.cre_listing_events WHERE listing_id="+ingest.sql_lit(claim_listing)+"::uuid;")=="0"
        from cre_enrich import build_complete_sql
        sql(build_complete_sql([claimed_id]))
        follow_events,follow_new,follow_changed,follow_marks,_=_derive(
            {key:current[key]},{key:prior[key]},{key:listings[key]})
        follow=monitor.build_write_sql([current[key]],follow_events,follow_new,follow_changed,follow_marks,
                                      str(uuid.uuid4()),"2026-11-04T01:01:30Z","fixture",["svn"],
                                      prior_index={key:prior[key]},prior_listings={key:listings[key]},brokerage_by_slug={"svn":broker})
        sql(follow)
        assert sql("SELECT url || '|' || attempts FROM credeals.cre_enrichment_queue WHERE external_id='claimed';")=="https://example.test/new-claimed|0"
        # An actual concurrent queue owner holds the shared advisory lock and
        # claims the row before the monitor can rearm it.
        sql("UPDATE credeals.cre_source_index SET fingerprint="+ingest.sql_lit(old["change"]["fingerprint"])+
            ", url='https://example.test/old-change' WHERE external_id='change';")
        race_key=(broker,"change")
        race_events,race_new,race_changed,race_marks,_=_derive(
            {race_key:current[race_key]},{race_key:prior[race_key]},{race_key:listings[race_key]})
        race=monitor.build_write_sql([current[race_key]],race_events,race_new,race_changed,race_marks,str(uuid.uuid4()),
                                     "2026-11-04T01:02:00Z","fixture",["svn"],
                                     prior_index={race_key:prior[race_key]},prior_listings={race_key:listings[race_key]},brokerage_by_slug={"svn":broker})
        holder=subprocess.Popen(["docker","exec","-i","-e","PGAPPNAME=svn-requeue-holder",container,
                                 "psql","-U","postgres","-X","-q","-tA","-v","ON_ERROR_STOP=1"],
                                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        holder.stdin.write("BEGIN; SELECT pg_advisory_xact_lock(1687068469); "
                           "UPDATE credeals.cre_enrichment_queue SET claimed_at=now() WHERE external_id='change'; "
                           "SELECT pg_sleep(3); COMMIT;")
        holder.stdin.close()
        for _ in range(50):
            if sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='svn-requeue-holder' AND wait_event='PgSleep';")=="1":
                break
            time.sleep(.05)
        else:
            raise AssertionError("synthetic concurrent owner did not reach hold")
        sql(race)
        if holder.wait(timeout=10)!=0:
            raise RuntimeError("synthetic concurrent owner failed")
        assert sql("SELECT attempts || '|' || (claimed_at IS NOT NULL)::text FROM credeals.cre_enrichment_queue WHERE external_id='change';")=="5|true"
        # Distinct A->B and A->C read snapshots: whichever transaction commits
        # first is retained, and the competing old-A transaction is refused.
        for number, winner in enumerate((200, 300)):
            eid="competing-"+str(number)
            old_url="https://example.test/"+eid
            initial=ingest.to_row({"sourceKey":"svn","id":eid,"url":old_url,"transactionMode":"sale"},
                                  {}, "2026-11-04T00:00:00Z")
            sql(ingest.build_sql([initial], [{"slug":"svn","discovered":1,"saved":1,"errors":0,"notes":"test"}],
                                 "2026-11-04T00:00:00Z", set()))
            old_group=_gfin(eid,source_key="svn",status="active",sale_price_usd=100,url=old_url)
            sql("UPDATE credeals.cre_source_index SET source_key='svn',observed_status='active',observation_present=true,fingerprint="+
                ingest.sql_lit(old_group["fingerprint"])+" WHERE external_id="+ingest.sql_lit(eid)+";")
            sql("INSERT INTO credeals.cre_enrichment_queue (brokerage_id,source_key,external_id,url,reason,attempts,last_error) VALUES ("+
                ingest.sql_lit(broker)+"::uuid,'svn',"+ingest.sql_lit(eid)+","+ingest.sql_lit(old_url)+",'changed',5,'before');")
            key=(broker,eid)
            prior_one=_idx(old_group,observed_status="active")
            prior_one.update(observation_present=True,presence_generation=0)
            listing_one={"id":sql("SELECT id FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(eid)+";"),
                         "status":"active","deleted":False}
            statements={}
            for price in (200,300):
                new_group=_gfin(eid,source_key="svn",status="active",sale_price_usd=price,
                                url=old_url+"-"+str(price))
                ev,en,ec,dm,_=_derive({key:new_group},{key:prior_one},{key:listing_one})
                statements[price]=(new_group,monitor.build_write_sql(
                    [new_group],ev,en,ec,dm,str(uuid.uuid4()),"2026-11-04T02:00:00Z",
                    "fixture",["svn"],prior_index={key:prior_one},prior_listings={key:listing_one},brokerage_by_slug={"svn":broker}))
            sql(statements[winner][1])
            loser=300 if winner==200 else 200
            sql(statements[loser][1])
            assert sql("SELECT fingerprint FROM credeals.cre_source_index WHERE external_id="+ingest.sql_lit(eid)+";")==statements[winner][0]["fingerprint"]
            assert sql("SELECT url FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(eid)+";")==old_url+"-"+str(winner)
        # An unchanged A->A snapshot and a changed A->B snapshot are safe
        # in either commit order; B must be the final index and queue URL.
        for number, changed_first in enumerate((False, True)):
            eid="unchanged-competing-"+str(number)
            old_url="https://example.test/"+eid
            initial=ingest.to_row({"sourceKey":"svn","id":eid,"url":old_url,"transactionMode":"sale"},
                                  {}, "2026-11-04T00:00:00Z")
            sql(ingest.build_sql([initial], [{"slug":"svn","discovered":1,"saved":1,"errors":0,"notes":"test"}],
                                 "2026-11-04T00:00:00Z", set()))
            old_group=_gfin(eid,source_key="svn",status="active",sale_price_usd=100,url=old_url)
            sql("UPDATE credeals.cre_source_index SET source_key='svn',observed_status='active',observation_present=true,fingerprint="+
                ingest.sql_lit(old_group["fingerprint"])+" WHERE external_id="+ingest.sql_lit(eid)+";")
            key=(broker,eid)
            p0=_idx(old_group,observed_status="active")
            p0.update(observation_present=True,presence_generation=0)
            l0={"id":sql("SELECT id FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(eid)+";"),
                "status":"active","deleted":False}
            statements={}
            for changed in (False, True):
                group=_gfin(eid,source_key="svn",status="active",sale_price_usd=200 if changed else 100,
                            url=old_url+"-new" if changed else old_url)
                ev,en,ec,dm,_=_derive({key:group},{key:p0},{key:l0})
                statements[changed]=(group,monitor.build_write_sql(
                    [group],ev,en,ec,dm,str(uuid.uuid4()),"2026-11-04T03:00:00Z",
                    "fixture",["svn"],prior_index={key:p0},prior_listings={key:l0},brokerage_by_slug={"svn":broker}))
            sql(statements[changed_first][1])
            sql(statements[not changed_first][1])
            assert sql("SELECT fingerprint FROM credeals.cre_source_index WHERE external_id="+ingest.sql_lit(eid)+";")==statements[True][0]["fingerprint"]
            assert sql("SELECT url FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(eid)+";")==old_url+"-new"
        # A first-seen SVN listing with no source-index row follows the
        # existing new-listing event/queue path and is not suppressed.
        first_eid="first-seen-no-index"
        first_url="https://example.test/first-seen"
        first=ingest.to_row({"sourceKey":"svn","id":first_eid,"url":first_url,"transactionMode":"sale"},
                            {}, "2026-11-04T00:00:00Z")
        sql(ingest.build_sql([first], [{"slug":"svn","discovered":1,"saved":1,"errors":0,"notes":"test"}],
                             "2026-11-04T00:00:00Z", set()))
        sql("DELETE FROM credeals.cre_source_index WHERE external_id='first-seen-no-index';")
        first_key=(broker,first_eid)
        first_group=_gfin(first_eid,source_key="svn",status="active",url=first_url)
        first_listing={"id":sql("SELECT id FROM credeals.cre_listings WHERE external_id='first-seen-no-index';"),
                       "status":"active","deleted":False}
        fe,fn,fc,fm,_=_derive({first_key:first_group},{},{first_key:first_listing},baseline=set())
        first_sql=monitor.build_write_sql([first_group],fe,fn,fc,fm,str(uuid.uuid4()),
                                          "2026-11-04T03:30:00Z","fixture",["svn"],
                                          prior_index={},prior_listings={first_key:first_listing},
                                          brokerage_by_slug={"svn":broker})
        output=sql(first_sql)
        assert "CRE_MONITOR_SVN_SUPPRESSED_V1 []" in output
        assert sql("SELECT source_key FROM credeals.cre_source_index WHERE external_id='first-seen-no-index';")=="svn"
        assert sql("SELECT reason || '|' || attempts FROM credeals.cre_enrichment_queue WHERE external_id='first-seen-no-index';")=="new|0"

        # First-seen observations must not lose a newer generation to an old
        # in-flight or exhausted "new" queue row.
        for mode in ("claimed", "exhausted", "changed-exhausted"):
            first_eid="first-existing-"+mode
            peer_eid="first-peer-"+mode
            groups={}
            rows={}
            for eid in (first_eid, peer_eid):
                url="https://example.test/"+eid
                initial=ingest.to_row({"sourceKey":"svn","id":eid,"url":url,"transactionMode":"sale"},
                                      {}, "2026-11-04T00:00:00Z")
                sql(ingest.build_sql([initial], [{"slug":"svn","discovered":1,"saved":1,"errors":0,"notes":"test"}],
                                     "2026-11-04T00:00:00Z", set()))
                sql("DELETE FROM credeals.cre_source_index WHERE external_id="+ingest.sql_lit(eid)+";")
                groups[(broker,eid)]=_gfin(eid,source_key="svn",status="active",url=url+"-fresh")
                rows[(broker,eid)]={"id":sql("SELECT id FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(eid)+";"),
                                    "status":"active","deleted":False}
            sql("INSERT INTO credeals.cre_enrichment_queue (brokerage_id,source_key,external_id,url,reason,attempts,claimed_at,last_error) VALUES ("+
                ingest.sql_lit(broker)+"::uuid,'svn',"+ingest.sql_lit(first_eid)+",'https://example.test/old',"+
                ingest.sql_lit("changed" if mode=="changed-exhausted" else "new")+","+
                ("1,now()" if mode=="claimed" else "5,NULL")+",'earlier error');")
            ev,en,ec,dm,_=_derive(groups,{},rows,baseline=set())
            statement=monitor.build_write_sql(list(groups.values()),ev,en,ec,dm,str(uuid.uuid4()),
                                              "2026-11-04T03:40:00Z","fixture",["svn"],
                                              prior_index={},prior_listings=rows,brokerage_by_slug={"svn":broker})
            result=sql(statement)
            assert sql("SELECT reason || '|' || attempts || '|' || url FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(peer_eid)+";") == "new|0|https://example.test/"+peer_eid+"-fresh"
            if mode=="claimed":
                assert json.loads([line.split(" ",1)[1] for line in result.splitlines() if line.startswith("CRE_MONITOR_SVN_SUPPRESSED_V1 ")][0]) == [[broker,first_eid]]
                assert sql("SELECT count(*) FROM credeals.cre_source_index WHERE external_id="+ingest.sql_lit(first_eid)+";")=="0"
                assert sql("SELECT count(*) FROM credeals.cre_listing_events WHERE listing_id="+ingest.sql_lit(rows[(broker,first_eid)]["id"])+"::uuid;")=="0"
                sql("DELETE FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(first_eid)+";")
                key=(broker,first_eid)
                ev,en,ec,dm,_=_derive({key:groups[key]},{},{key:rows[key]},baseline=set())
                follow=monitor.build_write_sql([groups[key]],ev,en,ec,dm,str(uuid.uuid4()),
                                               "2026-11-04T03:41:00Z","fixture",["svn"],
                                               prior_index={},prior_listings={key:rows[key]},brokerage_by_slug={"svn":broker})
                sql(follow)
            else:
                assert "CRE_MONITOR_SVN_SUPPRESSED_V1 []" in result
                assert sql("SELECT last_error FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(first_eid)+";")=="earlier error"
            assert sql("SELECT count(*) FROM credeals.cre_source_index WHERE external_id="+ingest.sql_lit(first_eid)+";")=="1"
            assert sql("SELECT reason || '|' || attempts || '|' || url FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(first_eid)+";") == ("changed" if mode=="changed-exhausted" else "new")+"|0|https://example.test/"+first_eid+"-fresh"

        # A uniqueness collision with a completed or foreign row is not work
        # this monitor can own. Preserve it and defer only the affected source.
        for indexed in (False, True):
            for owner in ("done", "foreign"):
                eid="collision-"+("indexed" if indexed else "first")+"-"+owner
                peer="collision-peer-"+("indexed" if indexed else "first")+"-"+owner
                groups={}
                rows={}
                previous={}
                for name in (eid, peer):
                    url="https://example.test/"+name
                    initial=ingest.to_row({"sourceKey":"svn","id":name,"url":url,"transactionMode":"sale"},
                                          {}, "2026-11-04T00:00:00Z")
                    sql(ingest.build_sql([initial], [{"slug":"svn","discovered":1,"saved":1,"errors":0,"notes":"test"}],
                                         "2026-11-04T00:00:00Z", set()))
                    old_group=_gfin(name,source_key="svn",status="active",sale_price_usd=100,url=url)
                    new_group=_gfin(name,source_key="svn",status="active",sale_price_usd=200,url=url+"-fresh")
                    if name==eid and indexed:
                        sql("UPDATE credeals.cre_source_index SET source_key='svn',observed_status='active',observation_present=true,fingerprint="+
                            ingest.sql_lit(old_group["fingerprint"])+" WHERE external_id="+ingest.sql_lit(name)+";")
                        previous[(broker,name)]=_idx(old_group,observed_status="active")
                        previous[(broker,name)].update(observation_present=True,presence_generation=0)
                    else:
                        sql("DELETE FROM credeals.cre_source_index WHERE external_id="+ingest.sql_lit(name)+";")
                    groups[(broker,name)]=new_group
                    rows[(broker,name)]={"id":sql("SELECT id FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(name)+";"),
                                         "status":"active","deleted":False}
                reason="changed" if indexed else "new"
                sql("INSERT INTO credeals.cre_enrichment_queue (brokerage_id,source_key,external_id,url,reason,attempts,done_at,last_error) VALUES ("+
                    ingest.sql_lit(broker)+"::uuid,"+ingest.sql_lit("svn" if owner=="done" else "lee-associates")+","+
                    ingest.sql_lit(eid)+",'https://example.test/occupied',"+ingest.sql_lit(reason)+",5,"+
                    ("now()" if owner=="done" else "NULL")+",'preserve collision');")
                ev,en,ec,dm,_=_derive(groups,previous,rows,baseline=set())
                statement=monitor.build_write_sql(list(groups.values()),ev,en,ec,dm,str(uuid.uuid4()),
                                                  "2026-11-04T03:45:00Z","fixture",["svn"],
                                                  prior_index=previous,prior_listings=rows,brokerage_by_slug={"svn":broker})
                result=sql(statement)
                suppressed=json.loads([line.split(" ",1)[1] for line in result.splitlines() if line.startswith("CRE_MONITOR_SVN_SUPPRESSED_V1 ")][0])
                assert suppressed==[[broker,eid]], (indexed,owner,suppressed)
                assert sql("SELECT source_key || '|' || reason || '|' || url || '|' || last_error FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(eid)+";")==("svn" if owner=="done" else "lee-associates")+"|"+reason+"|https://example.test/occupied|preserve collision"
                assert sql("SELECT count(*) FROM credeals.cre_listing_events WHERE listing_id="+ingest.sql_lit(rows[(broker,eid)]["id"])+"::uuid;")=="0"
                prior_count="1" if indexed else "0"
                assert sql("SELECT count(*) FROM credeals.cre_source_index WHERE external_id="+ingest.sql_lit(eid)+";")==prior_count
                if indexed:
                    assert sql("SELECT fingerprint FROM credeals.cre_source_index WHERE external_id="+ingest.sql_lit(eid)+";")==previous[(broker,eid)]["fingerprint"]
                assert sql("SELECT reason FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(peer)+";")=="new"
                assert "CRE_MONITOR_SVN_REPLAY_V1" in sql(statement)
                assert sql("SELECT count(*) FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(eid)+";")=="1"
                # External owner clears its blocker; a later fresh monitor scan
                # can now queue the preserved source observation.
                sql("DELETE FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(eid)+";")
                key=(broker,eid)
                prior_one={key:previous[key]} if indexed else {}
                ev,en,ec,dm,_=_derive({key:groups[key]},prior_one,{key:rows[key]},baseline=set())
                follow=monitor.build_write_sql([groups[key]],ev,en,ec,dm,str(uuid.uuid4()),
                                               "2026-11-04T03:46:00Z","fixture",["svn"],
                                               prior_index=prior_one,prior_listings={key:rows[key]},brokerage_by_slug={"svn":broker})
                sql(follow)
                assert sql("SELECT reason || '|' || attempts || '|' || url FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(eid)+";")==reason+"|0|https://example.test/"+eid+"-fresh"

        # A separate usable SVN item can carry the new observation even if
        # another source occupies the "changed" reason.
        eid="collision-own-usable"
        url="https://example.test/"+eid
        initial=ingest.to_row({"sourceKey":"svn","id":eid,"url":url,"transactionMode":"sale"},
                              {}, "2026-11-04T00:00:00Z")
        sql(ingest.build_sql([initial], [{"slug":"svn","discovered":1,"saved":1,"errors":0,"notes":"test"}],
                             "2026-11-04T00:00:00Z", set()))
        old_group=_gfin(eid,source_key="svn",status="active",sale_price_usd=100,url=url)
        new_group=_gfin(eid,source_key="svn",status="active",sale_price_usd=200,url=url+"-fresh")
        sql("UPDATE credeals.cre_source_index SET source_key='svn',observed_status='active',observation_present=true,fingerprint="+
            ingest.sql_lit(old_group["fingerprint"])+" WHERE external_id="+ingest.sql_lit(eid)+";")
        for source,reason in (("lee-associates","changed"),("svn","new")):
            sql("INSERT INTO credeals.cre_enrichment_queue (brokerage_id,source_key,external_id,url,reason,attempts,last_error) VALUES ("+
                ingest.sql_lit(broker)+"::uuid,"+ingest.sql_lit(source)+","+ingest.sql_lit(eid)+",'https://example.test/occupied',"+
                ingest.sql_lit(reason)+",1,'preserve');")
        key=(broker,eid)
        p0=_idx(old_group,observed_status="active")
        p0.update(observation_present=True,presence_generation=0)
        l0={"id":sql("SELECT id FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(eid)+";"),"status":"active","deleted":False}
        ev,en,ec,dm,_=_derive({key:new_group},{key:p0},{key:l0},baseline=set())
        usable=monitor.build_write_sql([new_group],ev,en,ec,dm,str(uuid.uuid4()),
                                       "2026-11-04T03:47:00Z","fixture",["svn"],
                                       prior_index={key:p0},prior_listings={key:l0},brokerage_by_slug={"svn":broker})
        assert "CRE_MONITOR_SVN_SUPPRESSED_V1 []" in sql(usable)
        assert sql("SELECT source_key || '|' || url FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(eid)+" ORDER BY source_key;").splitlines()==[
            "lee-associates|https://example.test/occupied","svn|"+url+"-fresh"]
        assert sql("SELECT fingerprint FROM credeals.cre_source_index WHERE external_id="+ingest.sql_lit(eid)+";")==new_group["fingerprint"]

        # Drive actual monitor.main against PostgreSQL while losing post-COMMIT
        # stdout or failing the local report write. Exact artifact replay sees
        # the existing job and must make no writes even after the claim clears.
        from contextlib import ExitStack
        from unittest.mock import patch
        import tempfile
        for mode in ("missing_receipt", "report_failure", "replay_complete", "partial_success", "replay_conflict"):
            eid="report-"+mode
            source_url="https://example.test/"+eid
            initial=ingest.to_row({"sourceKey":"svn","id":eid,"url":source_url,"transactionMode":"sale"},
                                  {}, "2026-11-04T00:00:00Z")
            sql(ingest.build_sql([initial], [{"slug":"svn","discovered":1,"saved":1,"errors":0,"notes":"test"}],
                                 "2026-11-04T00:00:00Z", set()))
            old_group=_gfin(eid,source_key="svn",status="active",sale_price_usd=100,url=source_url)
            group=_gfin(eid,source_key="svn",status="active",sale_price_usd=200,url=source_url+"-new")
            sql("UPDATE credeals.cre_source_index SET source_key='svn',observed_status='active',observation_present=true,fingerprint="+
                ingest.sql_lit(old_group["fingerprint"])+" WHERE external_id="+ingest.sql_lit(eid)+";")
            sql("INSERT INTO credeals.cre_enrichment_queue (brokerage_id,source_key,external_id,url,reason,attempts,claimed_at) VALUES ("+
                ingest.sql_lit(broker)+"::uuid,'svn',"+ingest.sql_lit(eid)+","+ingest.sql_lit(source_url)+
                ",'changed',1,"+("NULL" if mode in {"replay_complete", "replay_conflict"} else "now()")+");")
            key=(broker,eid)
            p0=_idx(old_group,observed_status="active")
            p0.update(observation_present=True,presence_generation=0)
            l0={"id":sql("SELECT id FROM credeals.cre_listings WHERE external_id="+ingest.sql_lit(eid)+";"),
                "status":"active","deleted":False}
            run_id=str(uuid.uuid4())
            run_key="monitor:v1:"+uuid.uuid4().hex
            real_run=subprocess.run
            calls=[0]
            def bridge(argv, **kwargs):
                if argv[0]!="fixture-psql":
                    return real_run(argv, **kwargs)
                payload=Path(argv[-1]).read_text()
                result=real_run(["docker","exec","-i",container,"psql","-U","postgres","-X","-q","-tA",
                                 "-v","ON_ERROR_STOP=1"],input=payload,capture_output=True,text=True)
                calls[0]+=1
                stdout="" if mode=="missing_receipt" and calls[0]==1 else result.stdout
                return subprocess.CompletedProcess(argv,result.returncode,stdout=stdout,stderr=result.stderr)
            with tempfile.TemporaryDirectory(prefix="svn-monitor-report-") as tmp:
                out=Path(tmp)/"missing"/"summary.json" if mode=="report_failure" else Path(tmp)/"summary.json"
                args=["cre_monitor.py","--in","fixture.json","--apply","--expected-db-target-sha256","a"*64,
                      "--out",str(out),"--quiet"]
                with ExitStack() as stack:
                    for attr,value in {
                        "load_artifact_groups":lambda _: ({"one":group},"2026-11-04T00:00:00Z",{"svn":1},0,set()),
                        "finalize_group":lambda g:g,
                        "artifact_run_identity":lambda _: (run_key,run_id),
                        "load_db_url":lambda _: ("postgresql://fixture/test","fixture"),
                        "assert_expected_database_target":lambda *_:None,
                        "load_prior_state":lambda *_: ({"svn":broker},{key:p0},{key:l0},{}),
                        "find_psql":lambda:"fixture-psql",
                        "psql_connection_args":lambda _:[],
                        "psql_connection_env":lambda _:{},
                    }.items():
                        stack.enter_context(patch.object(monitor,attr,value))
                    stack.enter_context(patch.object(sys,"argv",args))
                    stack.enter_context(patch.object(subprocess,"run",side_effect=bridge))
                    if mode=="partial_success":
                        assert monitor.main() == 20
                        assert out.exists() and json.loads(out.read_text())["status"]=="partial"
                        assert sql("SELECT status FROM credeals.cre_scrape_jobs WHERE id="+ingest.sql_lit(run_id)+"::uuid;")=="partial"
                        original_report=out.read_bytes()
                        sql("DELETE FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(eid)+";")
                        assert monitor.main() == 22
                        assert out.read_bytes()==original_report
                        continue
                    if mode=="replay_conflict":
                        assert monitor.main() == 0
                        original_report=out.read_bytes()
                        sql("UPDATE credeals.cre_scrape_jobs SET artifact_run_key='different-key' WHERE id="+ingest.sql_lit(run_id)+"::uuid;")
                        try:
                            monitor.main()
                            raise AssertionError("conflicting job was accepted as a settled replay")
                        except SystemExit as exc:
                            assert "outcome or report is unproven" in str(exc),str(exc)
                        assert out.read_bytes()==original_report
                        continue
                    if mode=="replay_complete":
                        assert monitor.main() == 0
                        assert out.exists() and json.loads(out.read_text())["status"]=="complete"
                        original_report=out.read_bytes()
                        assert sql("SELECT status FROM credeals.cre_scrape_jobs WHERE id="+ingest.sql_lit(run_id)+"::uuid;")=="completed"
                        sql("UPDATE credeals.cre_enrichment_queue SET claimed_at=now() WHERE external_id="+ingest.sql_lit(eid)+";")
                        assert monitor.main() == 22
                        assert out.read_bytes()==original_report
                        assert sql("SELECT status FROM credeals.cre_scrape_jobs WHERE id="+ingest.sql_lit(run_id)+"::uuid;")=="completed"
                        assert sql("SELECT (claimed_at IS NOT NULL)::text FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(eid)+";")=="true"
                        continue
                    try:
                        monitor.main()
                        raise AssertionError("report failure was not surfaced")
                    except SystemExit as exc:
                        expected="outcome or report is unproven" if mode=="missing_receipt" else "committed but its report could not be saved"
                        assert expected in str(exc),str(exc)
                    assert not out.exists()
                    assert sql("SELECT status FROM credeals.cre_scrape_jobs WHERE id="+ingest.sql_lit(run_id)+"::uuid;")=="partial"
                    sql("DELETE FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(eid)+";")
                    assert monitor.main() == 22
                    assert sql("SELECT count(*) FROM credeals.cre_enrichment_queue WHERE external_id="+ingest.sql_lit(eid)+";")=="0"
                    assert sql("SELECT fingerprint FROM credeals.cre_source_index WHERE external_id="+ingest.sql_lit(eid)+";")==old_group["fingerprint"]
                    assert not out.exists()
        print("PASS: competing snapshots, mixed peers, claimed deferral, foreign-index scope, lost postcommit output, report failure and safe replay refusal")
    finally:
        subprocess.run(["docker","rm","-f",container],capture_output=True,check=False)


if __name__ == "__main__":
    main()
