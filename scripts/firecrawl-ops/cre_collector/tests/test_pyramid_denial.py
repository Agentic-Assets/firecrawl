"""Offline Pyramid denial propagation through real collector and checkpoint paths."""
import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import cre_checkpoint_refresh as refresh
import pytest

COLLECTOR = Path(__file__).resolve().parents[1]
PRELOAD = r"""
import fs from 'node:fs';
const calls=[];
globalThis.fetch=async(input)=>{
  const u=new URL(String(input));
  calls.push(u.hostname);
  if(u.hostname==='www.pyramidbrokerage.com') {
    if(process.env.PYRAMID_PARTIAL==='1' && calls.length<=2) {
      const rows=[246,249].map((type,i)=>({id:i+1,status:'publish',type:'pbc-listings',
        link:`https://www.pyramidbrokerage.com/listings/${i+1}/`, 'sale-type':[type],
        title:{rendered:'Property'}, content:{rendered:''}, _embedded:{'wp:term':[]}}));
      return Response.json(rows,{headers:{'X-WP-Total':'2','X-WP-TotalPages':'1'}});
    }
    return new Response('denied',{status:Number(process.env.PYRAMID_STATUS)});
  }
  throw Error('offline unexpected transport');
};
process.on('exit',()=>fs.writeFileSync(process.env.PYRAMID_CALLS,JSON.stringify(calls),{flag:'wx',mode:0o600}));
"""


@pytest.mark.parametrize("status,code", [(401, 76), (403, 77)])
@pytest.mark.parametrize("monitor", [False, True])
def test_real_cli_denial_stops_second_transaction(tmp_path, status, code, monitor):
    preload = tmp_path / "preload.mjs"
    preload.write_text(PRELOAD)
    calls = tmp_path / "calls.json"
    out = tmp_path / "artifact.json"
    result = subprocess.run(
        [shutil.which("node"), "--import", "tsx", "--import", str(preload), "collect.ts",
         "--source=pyramid-brokerage", "--transaction=both", "--max-items=0", f"--out={out}"]
        + (["--monitor"] if monitor else []), cwd=COLLECTOR,
        env={**os.environ, "PYRAMID_CALLS": str(calls), "PYRAMID_STATUS": str(status)},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == code, result.stderr
    assert json.loads(calls.read_text()) == ["www.pyramidbrokerage.com"]
    assert not out.exists()
    assert "collecting pyramid-brokerage/lease" not in result.stderr


@pytest.mark.parametrize("status,raw,settled", [(401, 76, 84), (403, 77, 85)])
def test_worker_mapping_and_serial_retry_suppression(tmp_path, monkeypatch, status, raw, settled):
    current = refresh.new_manifest(tmp_path, git_sha="synthetic", git_dirty=False,
                                   sources=("pyramid-brokerage",), page_cap=400, concurrency=2)
    calls = []
    monkeypatch.setattr(refresh, "run_command", lambda *a, **k: calls.append(a) or raw)
    for _ in range(2):
        assert refresh.collect_source(tmp_path, current, "pyramid-brokerage", transactions=("sale", "lease"),
                                      page_cap=400, concurrency=2, attempts_this_run=3) is None
    checkpoint = current["sources"]["pyramid-brokerage"]
    assert len(calls) == 1
    assert checkpoint["terminal_collection_failure"] == {
        "code": refresh.PYRAMID_DENIED_FAILURE_CODE, "http_status": status, "attempt_number": 1}
    assert refresh.has_terminal_collection_failure("pyramid-brokerage", checkpoint)
    rc = refresh.run_cohort_collection_worker("pyramid-brokerage", tmp_path / "out.json", run_dir=tmp_path,
        attempt_log=tmp_path / "attempt.log", attempt_number=1, generation_started_at="2026-10-19T14:47:00+00:00",
        page_cap=400, concurrency=2, transactions=("sale", "lease"))
    assert rc == settled
    assert refresh.run_cohort_collection_worker("cbre", tmp_path / "out2.json", run_dir=tmp_path,
        attempt_log=tmp_path / "attempt2.log", attempt_number=1, generation_started_at="2026-10-19T14:47:00+00:00",
        page_cap=400, concurrency=2, transactions=("sale", "lease")) == raw


def test_wrong_source_or_invalid_status_does_not_claim_pyramid_terminal():
    code = refresh.PYRAMID_DENIED_FAILURE_CODE
    assert not refresh.has_terminal_collection_failure("cbre", {"terminal_collection_failure": {"code": code, "http_status": 403}})
    assert not refresh.has_terminal_collection_failure("pyramid-brokerage", {"terminal_collection_failure": {"code": code, "http_status": 500}})


def test_partial_success_then_denial_does_not_publish_artifact(tmp_path):
    preload, calls, out = tmp_path / "preload.mjs", tmp_path / "calls.json", tmp_path / "artifact.json"
    preload.write_text(PRELOAD)
    result = subprocess.run([shutil.which("node"), "--import", "tsx", "--import", str(preload), "collect.ts",
        "--source=pyramid-brokerage", "--transaction=both", "--monitor", f"--out={out}"], cwd=COLLECTOR,
        env={**os.environ, "PYRAMID_CALLS": str(calls), "PYRAMID_STATUS": "403", "PYRAMID_PARTIAL": "1"},
        capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 77, result.stderr
    assert "pyramid-brokerage/sale: 1 listings" in result.stderr
    assert len(json.loads(calls.read_text())) == 3
    assert not out.exists()


def test_mixed_cli_skips_pyramid_lease_and_continues_to_next_source(tmp_path):
    preload, calls, out = tmp_path / "preload.mjs", tmp_path / "calls.json", tmp_path / "artifact.json"
    preload.write_text(PRELOAD)
    result = subprocess.run([shutil.which("node"), "--import", "tsx", "--import", str(preload), "collect.ts",
        "--source=pyramid-brokerage,essex-realty", "--transaction=both", "--monitor", f"--out={out}"], cwd=COLLECTOR,
        env={**os.environ, "PYRAMID_CALLS": str(calls), "PYRAMID_STATUS": "403"},
        capture_output=True, text=True, timeout=30, check=False)
    assert "collecting pyramid-brokerage/lease" not in result.stderr
    assert "collecting essex-realty/sale" in result.stderr
    assert json.loads(calls.read_text()).count("www.pyramidbrokerage.com") == 1


@pytest.mark.parametrize("status,exit_code", [(401, 84), (403, 85)])
def test_cohort_terminal_stops_retry_and_allows_peer(tmp_path, monkeypatch, status, exit_code):
    current = refresh.new_manifest(tmp_path, git_sha="synthetic", git_dirty=False,
        sources=("pyramid-brokerage", "hanley"), page_cap=400, concurrency=2, source_workers=2)
    started = []
    real_finalize = refresh._finalize_cohort_collection
    monkeypatch.setattr(refresh, "_require_completed_group_settlement", lambda _group: None)
    monkeypatch.setattr(refresh.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(refresh, "_terminate_cohort_processes", lambda _active: None)
    def start(_run, _manifest, source, **_kwargs):
        started.append(source)
        attempt = {"number": len(current["sources"][source]["attempts"]) + 1,
                   "started_at": current["started_at"], "finished_at": None,
                   "temporary_artifact": f"{source}.tmp"}
        current["sources"][source]["attempts"].append(attempt)
        current["sources"][source]["state"] = "collecting"
        class Process:
            returncode = exit_code if source == "pyramid-brokerage" else 0
            def poll(self):
                return self.returncode
        return refresh.CohortCollectionProcess(source=source, process=Process(), log_handle=io.StringIO(),
            tmp_artifact=tmp_path / f"{source}.tmp", attempt=attempt,
            attempt_started_at=current["started_at"], owned_group=object())
    def finalize(run, manifest, item):
        if item.source == "pyramid-brokerage":
            return real_finalize(run, manifest, item)
        manifest["sources"][item.source]["state"] = "validated"
        manifest["sources"][item.source]["artifact"] = {"path": "hanley.json"}
        item.log_handle.close()
        return True
    monkeypatch.setattr(refresh, "_start_cohort_collection", start)
    monkeypatch.setattr(refresh, "_finalize_cohort_collection", finalize)
    monkeypatch.setattr(refresh, "_manifest_checkpoint_artifact_valid", lambda _run, m, source:
        (tmp_path / "hanley.json", {}) if m["sources"][source].get("artifact") else None)
    assert refresh.prepare_sources_cohort(tmp_path, current, ("pyramid-brokerage", "hanley"),
        page_cap=400, concurrency=2, attempts_this_run=3, env_file=None, source_workers=2) == ["pyramid-brokerage"]
    assert started == ["pyramid-brokerage", "hanley"]
    assert current["sources"]["pyramid-brokerage"]["terminal_collection_failure"]["http_status"] == status
    assert current["sources"]["hanley"]["state"] == "validated"


@pytest.mark.parametrize("source,code,expected", [
    ("pyramid-brokerage", 84, 84), ("pyramid-brokerage", 85, 85), ("cbre", 84, 80),
])
def test_worker_main_preserves_only_pyramid_settled_code(tmp_path, monkeypatch, source, code, expected):
    monkeypatch.setattr(refresh, "run_cohort_collection_worker", lambda *a, **k: code)
    argv = refresh.build_cohort_collect_worker_argv(source, tmp_path / "out.json", run_dir=tmp_path,
        attempt_log=tmp_path / "worker.log", attempt_number=1, generation_started_at="2026-10-19T14:47:00+00:00",
        page_cap=400, concurrency=2, transactions=("sale", "lease"))
    assert refresh.main(argv[2:]) == expected


def test_mixed_cli_discards_partial_pyramid_rows_on_later_denial(tmp_path):
    preload, calls, out = tmp_path / "preload.mjs", tmp_path / "calls.json", tmp_path / "artifact.json"
    preload.write_text(PRELOAD)
    result = subprocess.run([shutil.which("node"), "--import", "tsx", "--import", str(preload), "collect.ts",
        "--source=pyramid-brokerage,essex-realty", "--transaction=both", "--monitor", f"--out={out}"], cwd=COLLECTOR,
        env={**os.environ, "PYRAMID_CALLS": str(calls), "PYRAMID_STATUS": "403", "PYRAMID_PARTIAL": "1"},
        capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert "pyramid-brokerage/sale: 1 listings" in result.stderr
    data = json.loads(out.read_text())
    assert not [row for row in data["listings"] if row.get("sourceKey") == "pyramid-brokerage"]
    pyramid = [row for row in data["sources"] if row.get("sourceKey") == "pyramid-brokerage"]
    assert len(pyramid) == 1 and pyramid[0]["error"] == "HTTP 403 access denied"
    assert data["brokers"] == []
    assert "collecting essex-realty/sale" in result.stderr


def test_mixed_full_keeps_sibling_listing_and_broker_after_pyramid_denial(tmp_path):
    preload, out = tmp_path / "mixed.mjs", tmp_path / "artifact.json"
    preload.write_text(r"""
import fs from 'node:fs';
const timer=globalThis.setTimeout;
globalThis.setTimeout=(fn,ms,...args)=>timer(fn,ms>=9000&&ms<=10000?0:ms,...args);
let inventoryCalls=0;
const base='https://essexrealtygroup.com/properties/';
const card=(slug,status)=>`<a class="property-card" href="${base}${slug}/"><span class="property-card__label">${status}</span><span class="property-info__address--address-1">${slug}</span><span class="property-info__address--address-2">Chicago, IL 60601</span></a>`;
globalThis.fetch=async(input)=>{
 const u=new URL(String(input));
 if(u.hostname==='srsre-next-412955565034.us-central1.run.app') return Response.json({total:1,properties:[{
   id:'srs-1',permalink:'/sale/fixture',apto_data:{SRS_Listings_ID__c:'srs-1',Availability__c:'Sale',
     related_brokers:[{name:'Sibling Broker',email:'sibling@example.com'}]}}]});
 if(u.hostname==='essexrealtygroup.com') {
   if(u.pathname==='/properties/' && !u.search) return new Response('<a class="allposts">ALL [1]</a>'+card('1157-west-18th-street','UNDER CONTRACT'));
   if(u.pathname==='/properties/' && u.search) return new Response('<a class="allposts">ALL [1]</a>'+card('closed-one','CLOSED'));
   if(u.pathname==='/properties-sitemap.xml') return new Response(`<urlset><url><loc>${base}1157-west-18th-street/</loc></url><url><loc>${base}closed-one/</loc></url></urlset>`);
   if(u.pathname==='/properties/1157-west-18th-street/') return new Response(fs.readFileSync('tests/fixtures/essex-page-15709.html','utf8').replace('</body>', '<section class="property-brokers"><div class="small-broker-card"><div class="small-broker-card__name"><a href="/team/jane/">Jane Test</a></div><div class="small-broker-card__contact"><a href="mailto:jane@example.com">Email</a></div></div></section></body>'));
 }
 if(u.hostname==='www.pyramidbrokerage.com') {
   if(u.pathname==='/wp-json/wp/v2/pbc-listings') {
     if(++inventoryCalls>2) return new Response('denied',{status:403});
     return Response.json([246,249].map((type,i)=>({id:i+1,status:'publish',type:'pbc-listings',
       link:`https://www.pyramidbrokerage.com/listings/${i+1}/`, 'sale-type':[type],
       title:{rendered:'Property'},content:{rendered:''},_embedded:{'wp:term':[]}})),
       {headers:{'X-WP-Total':'2','X-WP-TotalPages':'1'}});
   }
   if(u.pathname==='/listings/1/') return new Response(`<html><head><link rel="shortlink" href="https://www.pyramidbrokerage.com/?p=1"></head><body><main id="content"><section class="property-details"><ul><li><span class="name">Sale/Lease:</span><span class="value">Sale</span></li></ul></section><ul class="agent-contact-list"><li><div class="details"><a class="name" href="/agents/broker/">Pyramid Broker</a><a class="mail" href="mailto:pyramid@example.com">Email</a></div></li></ul></main></body></html>`);
 }
 throw Error('offline unexpected transport '+u.hostname+u.pathname);
};
""")
    result = subprocess.run([shutil.which("node"), "--import", "tsx", "--import", str(preload), "collect.ts",
        "--source=srs,pyramid-brokerage,essex-realty", "--transaction=both", f"--out={out}"], cwd=COLLECTOR,
        env={**os.environ}, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert "pyramid-brokerage/sale: 1 listings" in result.stderr
    data = json.loads(out.read_text())
    assert [row["sourceKey"] for row in data["listings"]] == ["srs", "essex-realty"]
    sibling = data["listings"][0]
    assert [data["brokers"][index]["name"] for index in sibling["brokerIds"]] == ["Sibling Broker"]
    assert data["listings"][1].get("contactsDetailed")
    assert all(row.get("name") != "Pyramid Broker" for row in data["brokers"])
    control_out = tmp_path / "pyramid-control.json"
    control = subprocess.run([shutil.which("node"), "--import", "tsx", "--import", str(preload), "collect.ts",
        "--source=pyramid-brokerage", "--transaction=sale", f"--out={control_out}"], cwd=COLLECTOR,
        env={**os.environ}, capture_output=True, text=True, timeout=30, check=False)
    assert control.returncode == 0, control.stderr
    assert any(row.get("name") == "Pyramid Broker" for row in json.loads(control_out.read_text())["brokers"])
    assert len([row for row in data["sources"] if row.get("sourceKey") == "pyramid-brokerage"]) == 1
    assert any(row.get("error") == "HTTP 403 access denied" for row in data["sources"])


def test_duplicate_source_keys_refuse_before_any_provider_request(tmp_path):
    preload, calls = tmp_path / "preload.mjs", tmp_path / "calls.json"
    preload.write_text(PRELOAD)
    result = subprocess.run([shutil.which("node"), "--import", "tsx", "--import", str(preload), "collect.ts",
        "--source=pyramid-brokerage,pyramid-brokerage", "--transaction=both", "--monitor"], cwd=COLLECTOR,
        env={**os.environ, "PYRAMID_CALLS": str(calls), "PYRAMID_STATUS": "403"},
        capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode != 0
    assert "duplicate source keys" in result.stderr
    assert json.loads(calls.read_text()) == []
