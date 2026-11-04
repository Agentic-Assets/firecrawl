"""Actual collector/property-mode contracts with all network I/O replaced."""

from __future__ import annotations

import copy
import io
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_partial_page_custody as custody
import pytest


COLLECTOR = Path(__file__).resolve().parents[1]
PRELOAD = r"""
import fs from 'node:fs';
import dns from 'node:dns/promises';
import https from 'node:https';
import {EventEmitter} from 'node:events';
import {syncBuiltinESMExports} from 'node:module';
const mode=process.env.AVISON_CONTRACT_CASE;
const calls=[];
globalThis.fetch=async(input)=>{
  const u=new URL(String(input));
  if(u.hostname==='www.avisonyoung.us')return new Response(
    "<script>SharpLaunch.PSE.create('0123456789abcdef0123456789abcdef')</script>");
  if(u.hostname!=='pse-api.sharplaunch.com')throw Error('unexpected offline fetch');
  const items=u.searchParams.get('entity')==='website'
    ? ['sale','lease'].flatMap((tx,t)=>[0,1,2].map(i=>({
        id:9001+t*3+i,status:'active',state:'TX',city:'Austin',country:'US',
        address:'100 Fixture Street',name:'Fixture Property',transaction:[tx],
        url:`https://fixture-${tx}-${i}.sharplaunch.com/`,
        external_url:`https://www.avisonyoung.us/properties/fixture-${tx}-${i}`,
        team_member_ids:[]})))
    : (mode==='explicit-strict'?[{id:1,name:'Synthetic Contact'}]:[]);
  return new Response(JSON.stringify({items}),{headers:{'content-type':'application/json'}});
};
dns.lookup=async()=>[{address:'93.184.216.34',family:4}];
https.request=(url,options,callback)=>{
  const req=new EventEmitter();req.setTimeout=()=>req;
  req.destroy=error=>{req.emit('error',error);return req;};
  req.end=()=>queueMicrotask(()=>{
    const native=url.hostname.endsWith('.sharplaunch.com');
    const index=Number((url.hostname+url.pathname).match(/fixture-(?:sale|lease)-(\d)/)?.[1]);
    calls.push({native,index});
    let status=native?200:404;
    let body='<html><main class="property-detail"><h1>Fixture Property</h1>'+
      '<p>Current property details.</p></main></html>';
    if(mode==='reverse-partial')status=native?404:200;
    if(mode==='one-good-404' && index===1)status=404;
    if(mode==='one-good-403' && index===1)status=403;

    if(mode==='partial-503')status=native?200:503;
    if(mode==='both-404'||mode.startsWith('mixed-')){
      status=404;
      if(index===1&&mode==='mixed-schema'){
        status=200;body='<html><h1>Access denied</h1><p>captcha</p></html>';
      }
      if(index===1&&mode==='mixed-503'&&!native)status=503;
      if(index===1&&mode==='mixed-undefined'){
        req.emit('error',undefined);return;
      }
    }
    const res=new EventEmitter();res.statusCode=status;res.headers={};callback(res);
    res.emit('data',Buffer.from(status===200?body:'Not found'));res.emit('end');
  });return req;
};
syncBuiltinESMExports();
process.on('exit',()=>fs.writeFileSync(process.env.AVISON_CONTRACT_CALLS,
  JSON.stringify(calls),{flag:'wx',mode:0o600}));
"""


def command(preload, output):
    node = shutil.which("node")
    if node is None or not (COLLECTOR / "node_modules/tsx").exists():
        pytest.skip("actual collector contract requires existing Node/tsx dependencies")
    return [
        node,
        "--import",
        "tsx",
        "--import",
        str(preload),
        "collect.ts",
        "--source=avison-young",
        "--transaction=both",
        "--max-items=0",
        "--concurrency=2",
        f"--out={output}",
    ]


def prepare(tmp_path, case):
    preload = tmp_path / "offline.mjs"
    preload.write_text(PRELOAD)
    started = datetime.now(timezone.utc).isoformat()
    base = dict(os.environ, CRE_REQUIRE_FRESH_DETAILS="1")
    env, overrides = refresh.fresh_source_env(
        "avison-young", tmp_path, base=base, generation_started_at=started
    )
    assert "CRE_REQUIRE_FRESH_DETAILS" not in env
    assert overrides["CRE_REQUIRE_FRESH_DETAILS"] == "<unset>"
    assert env["CRE_REQUIRE_FRESH_PROPERTY_DETAILS"] == "1"
    assert env["AVISON_YOUNG_DETAIL_TRANSPORT"] == "direct"
    env.update(
        AVISON_CONTRACT_CASE=case, AVISON_CONTRACT_CALLS=str(tmp_path / "calls.json")
    )
    # Keep transient controls fast without changing their attempt budget.
    env["AVISON_YOUNG_DIRECT_DETAIL_RETRY_MS"] = "0"
    env.pop("AVISON_YOUNG_DETAIL_CONCURRENCY", None)
    if case == "explicit-strict":
        env["CRE_REQUIRE_FRESH_DETAILS"] = "1"
    return preload, started, env


def collect(tmp_path, case):
    preload, started, env = prepare(tmp_path, case)
    output = tmp_path / "artifact.json"
    result = subprocess.run(
        command(preload, output),
        cwd=COLLECTOR,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    (tmp_path / "collector.log").write_text(result.stdout + result.stderr)
    return result, output, started, json.loads((tmp_path / "calls.json").read_text())


@pytest.mark.parametrize("case", ["partial-404", "reverse-partial", "partial-503"])
def test_actual_default_property_mode_keeps_partial_alternate_and_degraded_team(
    tmp_path, case
):
    result, output, started, calls = collect(tmp_path, case)
    assert result.returncode == 0, result.stderr
    artifact = refresh.validate_source_artifact(
        output,
        "avison-young",
        started,
        expected_generation_id=tmp_path.name,
        expected_generation_started_at=started,
    )
    assert artifact["property_detail_freshness"] is True
    assert artifact["strict_freshness"] is False
    rows = json.loads(output.read_text())["listings"]
    assert len(rows) == 6
    for row in rows:
        assert row["preserveChildCollections"] is True
        assert row["detailObservedWithChildPreservation"] is True
        assert row["detailWarning"]
        assert not row.get("detailError")
        assert row["freshnessProvenance"]["cacheDisposition"] == "live"
    # Typed404 is already not retried in property mode; transient503 still is.
    assert len(calls) == (24 if case == "partial-503" else 12)
    broken = json.loads(output.read_text())
    broken["listings"][0].pop("detailObservedWithChildPreservation")
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(broken))
    with pytest.raises(
        refresh.ArtifactValidationError, match="inconsistent child-preservation"
    ):
        refresh.validate_source_artifact(invalid, "avison-young", started)


def test_actual_default_all404_is_terminal_without_artifact_or_new_queued_details(
    tmp_path,
):
    result, output, _started, calls = collect(tmp_path, "both-404")
    assert result.returncode == refresh.AVISON_YOUNG_TERMINAL_404_EXIT_CODE
    assert not output.exists()
    assert len(calls) == 8  # two active rows, two endpoints, two transaction passes
    assert {call["index"] for call in calls} == {0, 1}


@pytest.mark.parametrize("case", ["mixed-schema", "mixed-503", "mixed-undefined"])
def test_actual_default_mixed_invalid_detail_is_not_terminal404(tmp_path, case):
    result, output, _started, _calls = collect(tmp_path, case)
    assert result.returncode == 1
    assert not output.exists()


def test_actual_explicit_strict_all_urls_requirement_is_unchanged(tmp_path):
    result, output, _started, _calls = collect(tmp_path, "explicit-strict")
    assert result.returncode == refresh.AVISON_YOUNG_TERMINAL_404_EXIT_CODE
    assert not output.exists()


def test_actual_default_terminal_worker_and_coordinator_do_not_ingest_or_resume(
    tmp_path, monkeypatch
):
    preload, started, env = prepare(tmp_path, "both-404")
    for key in (
        "AVISON_CONTRACT_CASE",
        "AVISON_CONTRACT_CALLS",
        "AVISON_YOUNG_DIRECT_DETAIL_RETRY_MS",
    ):
        monkeypatch.setenv(key, env[key])
    monkeypatch.delenv("AVISON_YOUNG_DETAIL_CONCURRENCY", raising=False)
    output = tmp_path / "source.json"
    monkeypatch.setattr(
        refresh, "build_collect_argv", lambda *_a, **_k: command(preload, output)
    )
    rc = refresh.run_cohort_collection_worker(
        "avison-young",
        output,
        run_dir=tmp_path,
        attempt_log=tmp_path / "worker.log",
        attempt_number=1,
        generation_started_at=started,
        page_cap=400,
        concurrency=2,
        transactions=("sale", "lease"),
    )
    assert rc == refresh.COHORT_SETTLED_AVISON_TERMINAL_404_EXIT
    assert not output.exists()
    manifest = refresh.new_manifest(
        tmp_path,
        git_sha="synthetic",
        git_dirty=False,
        sources=("avison-young", "hanley"),
        page_cap=400,
        concurrency=2,
        pipeline=True,
    )
    manifest["sources"]["hanley"]["state"] = "verified"
    sibling = copy.deepcopy(manifest["sources"]["hanley"])
    attempt = {"number": 1, "started_at": started, "temporary_artifact": "source.json"}
    manifest["sources"]["avison-young"]["state"] = "collecting"
    manifest["sources"]["avison-young"]["attempts"].append(attempt)
    monkeypatch.setattr(
        refresh, "_require_completed_group_settlement", lambda _group: None
    )
    monkeypatch.setattr(
        refresh,
        "validate_source_artifact",
        lambda *_a, **_k: pytest.fail("terminal artifact validation attempted"),
    )

    class SettledProcess:
        returncode = rc

        def poll(self):
            return self.returncode

    item = refresh.CohortCollectionProcess(
        source="avison-young",
        process=SettledProcess(),
        log_handle=io.StringIO(),
        tmp_artifact=output,
        attempt=attempt,
        attempt_started_at=started,
        owned_group=object(),
    )
    assert refresh._finalize_cohort_collection(tmp_path, manifest, item) is False
    checkpoint = manifest["sources"]["avison-young"]
    assert checkpoint["state"] == "collect_failed"
    assert checkpoint["artifact"] is None
    assert checkpoint["terminal_collection_failure"]["http_status"] == 404
    assert manifest["sources"]["hanley"] == sibling
    assert not refresh._cohort_needs_collection(
        tmp_path, manifest, "avison-young", attempts_before=0, attempts_this_run=3
    )


@pytest.mark.parametrize("case,exit_code", [
    ("one-good-404", refresh.AVISON_YOUNG_TERMINAL_404_EXIT_CODE),
    ("one-good-403", 1),
])
def test_actual_failed_collector_preserves_validated_peers_in_private_custody(tmp_path, case, exit_code):
    preload, started, env = prepare(tmp_path, case)
    attempt = {"number": 1, "avison-young_partial": custody.prepare(tmp_path, 1, "avison-young")}
    env, _ = refresh.fresh_source_env("avison-young", tmp_path, base=env,
                                      generation_started_at=started, attempt_number=1)
    output = tmp_path / "artifact.json"
    result = subprocess.run(command(preload, output), cwd=COLLECTOR, env=env,
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == exit_code, result.stderr
    data = json.loads(output.read_text())
    assert data["runMeta"]["evidenceOnly"] is True
    assert data["listings"] == []
    assert all(source.get("error") for source in data["sources"])
    receipt = data["sources"][0]["partialCapture"]
    assert receipt["coverage"] == "unverified"
    assert receipt["outcome"] == "source_error"
    assert receipt["observationRecords"] >= 1
    captured_calls = json.loads((tmp_path / "calls.json").read_text())
    control = tmp_path / "without-spool"
    control.mkdir()
    control_result, control_output, _, control_calls = collect(control, case)
    assert control_result.returncode == exit_code
    assert not control_output.exists()
    assert captured_calls == control_calls
    rejected, _ = refresh.archive_rejected_with_evidence(
        output, tmp_path, {}, "avison-young", attempt)
    assert rejected and (tmp_path / rejected).is_file()
    assert attempt["avison-young_partial"]["state"] == "sealed_local"
    pages = (tmp_path / attempt["avison-young_partial"]["path"]).read_text().splitlines()
    records = [json.loads(page) for page in pages[1:]]
    assert all(record["kind"] == "validated_detail" for record in records)
    observed = [record["documents"][0] for record in records]
    assert all(row["sourceKey"] == "avison-young" for row in observed)
    assert all(row["freshnessProvenance"]["detailScope"] == "detail_page" for row in observed)
    assert all(str(row["id"]) in {"9001", "9003", "9004", "9006"} for row in observed)
    archived = tmp_path / rejected
    assert archived.stat().st_mode & 0o777 == 0o600
    resumed = {"number": 1, "finished_at": started, "rc": exit_code,
               "avison-young_partial": {"state": "pending", "path": attempt["avison-young_partial"]["path"]}}
    custody.reconcile(tmp_path, {"sources": {"avison-young": {"attempts": [resumed]}}}, "avison-young")
    assert resumed["avison-young_partial"]["state"] == "sealed_local"
    spool = tmp_path / attempt["avison-young_partial"]["path"]
    saved = spool.read_bytes()
    interrupted = {"number": 1, "finished_at": None, "rc": None,
                   "avison-young_partial": {"state": "pending", "path": attempt["avison-young_partial"]["path"]}}
    custody.reconcile(tmp_path, {"sources": {"avison-young": {"attempts": [interrupted]}}}, "avison-young")
    assert interrupted["avison-young_partial"]["state"] == "unverified"
    assert spool.read_bytes() == saved
    spool.write_bytes(saved + b"tamper")
    changed = {"number": 1, "avison-young_partial": {"state": "pending", "path": attempt["avison-young_partial"]["path"]}}
    custody.settle(tmp_path, changed, archived, "avison-young")
    assert changed["avison-young_partial"]["state"] == "unverified"
    with pytest.raises(refresh.ArtifactValidationError):
        refresh.validate_source_artifact(archived, "avison-young", started,
                                         expected_generation_id=tmp_path.name,
                                         expected_generation_started_at=started)
