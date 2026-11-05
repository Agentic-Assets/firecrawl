"""Synthetic source producer reaches the owned Marcus partial path offline."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_refresh_pipeline as pipeline


def test_real_marcus_producer_reaches_partial_candidate_without_network(tmp_path, monkeypatch):
    collector = Path(__file__).resolve().parents[1]
    repo = tmp_path / "repo"
    target = repo / "scripts" / "firecrawl-ops" / "cre_collector"
    target.parent.mkdir(parents=True)
    shutil.copytree(collector, target, ignore=shutil.ignore_patterns("node_modules", "out", "__pycache__", ".pytest_cache", "*.pyc"))
    (target / "node_modules").symlink_to(collector / "node_modules", target_is_directory=True)
    (repo / ".gitignore").write_text("node_modules\nout\n")
    for args in (("init", "-q"), ("add", "."),
                 ("-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.test", "commit", "-qm", "fixture")):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, timeout=60)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    preload = tmp_path / "fake-fetch.mjs"
    preload.write_text("""
import net from "node:net"; import tls from "node:tls"; import http from "node:http"; import https from "node:https";
const deny=()=>{throw Error("NETWORK_DENIED")}; net.Socket.prototype.connect=deny; tls.connect=deny; http.request=deny; https.request=deny;
globalThis.fetch=async (input,init={})=>{
 const u=String(input);
 if(u.endsWith('/api/contentsearch/properties')) return Response.json({Results:{Properties:[{ActivityId:'a'}],TotalCount:2}});
 if(u.endsWith('/api/contentsearch/mapproperties')) return Response.json({Results:{Properties:[{ActivityId:'a',Latitude:34,Longitude:-118},{ActivityId:'b',Latitude:35,Longitude:-119}]}});
 if(u.endsWith('/api/contentsearch/mappropertydetail')) { const activity=JSON.parse(init.body).activityId, id=activity==='a'?'101':'102';
  const path='/properties/'+id+'/source-name'; return Response.json({Results:{ActivityId:activity,PropertyUrl:path,
   PropertyDetail:'<div class="mm-tile" data-dealid="'+id+'" data-activityid="'+activity+'"><a href="'+path+'"><h2>Fixture</h2></a></div>'}}); }
 if(u.startsWith('https://www.marcusmillichap.com/properties/')) return new Response('<html>unverified</html>',{status:200});
 throw Error('UNEXPECTED_REQUEST');
};
""")
    run_dir = tmp_path / "generation"
    run_dir.mkdir()
    lock = refresh.SharedLock(target / "out" / "daily" / ".cre.lock", recovery_required=True)
    with lock:
        try:
            manifest = refresh.new_manifest(run_dir, git_sha=sha, git_dirty=False, sources=("marcus-millichap",),
                page_cap=3, concurrency=1, pipeline=True, database_target={"algorithm": "sha256", "value": "b" * 64})
            manifest["preflight"]["shared_lock"] = str(lock.path)
            env = {**os.environ, "NODE_OPTIONS": "--import=" + preload.as_uri()}
            monkeypatch.setattr(refresh, "canonical_shared_lock_dir", lambda: lock.path)
            monkeypatch.setattr(refresh, "git_identity", lambda: (sha, False))
            monkeypatch.setattr(refresh, "safe_process_env", lambda *_: dict(env))
            monkeypatch.setattr(refresh, "COLLECTOR_DIR", target)

            def fake_gate(_run, state, source, _env):
                state["sources"][source]["gate"] = {"current_inventory_proof": {"emitted_count": 2, "held_count": 0}}
                return True

            def fake_database(_run, state, unit, _env, _previous):
                state["sources"]["marcus-millichap"]["readback"] = {"ok": True}
                unit.update(validation_path="synthetic", validation_sha256="c" * 64)
                return {"path": "synthetic", "sha256": "c" * 64, "sequence": 1}

            monkeypatch.setattr(pipeline, "preseal_source_gate", fake_gate)
            monkeypatch.setattr(pipeline, "run_database_unit", fake_database)
            failures = pipeline.run_bounded_pipeline(run_dir, manifest, ("marcus-millichap",), page_cap=3,
                concurrency=1, attempts_this_run=1, env_file=None, source_workers=1, owner_check=lock._require_authority)
            checkpoint = manifest["sources"]["marcus-millichap"]
            assert failures == []
            assert checkpoint["attempts"][0]["rc"] == 0
            rejected = json.loads((run_dir / checkpoint["attempts"][0]["rejected_artifact"]).read_text())
            assert len(rejected["listings"]) == 2
            assert checkpoint["marcus_inventory_worker"]["state"] == "settled"
            assert checkpoint["artifact"]["marcus_inventory_partial"] is True
            assert manifest["pipeline"]["source_results"]["marcus-millichap"]["partial"] is True
            candidate = json.loads((run_dir / checkpoint["artifact"]["path"]).read_text())
            assert candidate["detailQualified"] == 0 and candidate["detailErrors"] == 2
        finally:
            lock.clear_recovery_requirement()
