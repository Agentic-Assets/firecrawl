"""Real bounded source capture reaches the owned partial pipeline offline."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_refresh_pipeline as pipeline
import pytest


@pytest.mark.parametrize("denied", [False, True])
def test_lyon_bounded_capture_reaches_partial_pipeline(tmp_path, monkeypatch, denied):
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
const timer=globalThis.setTimeout; globalThis.setTimeout=(fn,ms,...args)=>timer(fn,ms>=9000&&ms<=10000?1:ms,...args);
globalThis.fetch=async input=>{
 const u=String(input);
 if(!u.startsWith("https://lyonstahl.com/wp-admin/admin-ajax.php?")) throw Error("UNEXPECTED_REQUEST");
 if(process.env.SYNTHETIC_DENIAL==="1" && u.includes("Status=for-sale")) return new Response("denied",{status:403});
 return Response.json([{permalink:"https://lyonstahl.com/?post_type=properties&p=40201",title:"Fixture",status:null}]);
};
""")
    run_dir = tmp_path / "generation"
    run_dir.mkdir()
    lock = refresh.SharedLock(target / "out" / "daily" / ".cre.lock", recovery_required=True)
    with lock:
        try:
            manifest = refresh.new_manifest(run_dir, git_sha=sha, git_dirty=False, sources=("lyon-stahl",),
                page_cap=3, concurrency=1, pipeline=True, database_target={"algorithm": "sha256", "value": "b" * 64})
            manifest["preflight"]["shared_lock"] = str(lock.path)
            env = {**os.environ, "NODE_OPTIONS": "--import=" + preload.as_uri(), "SYNTHETIC_DENIAL": "1" if denied else "0"}
            monkeypatch.setattr(refresh, "canonical_shared_lock_dir", lambda: lock.path)
            monkeypatch.setattr(refresh, "git_identity", lambda: (sha, False))
            monkeypatch.setattr(refresh, "safe_process_env", lambda *_: dict(env))
            monkeypatch.setattr(refresh, "COLLECTOR_DIR", target)

            def fake_gate(_run, state, source, _env):
                state["sources"][source]["gate"] = {"current_inventory_proof": {"emitted_count": 1, "held_count": 0}}
                return True

            def fake_database(_run, state, unit, _env, _previous):
                state["sources"]["lyon-stahl"]["readback"] = {"ok": True}
                unit.update(validation_path="synthetic", validation_sha256="c" * 64)
                return {"path": "synthetic", "sha256": "c" * 64, "sequence": 1}

            monkeypatch.setattr(pipeline, "preseal_source_gate", fake_gate)
            monkeypatch.setattr(pipeline, "run_database_unit", fake_database)
            failures = pipeline.run_bounded_pipeline(run_dir, manifest, ("lyon-stahl",), page_cap=3,
                concurrency=1, attempts_this_run=1, env_file=None, source_workers=1, owner_check=lock._require_authority)
            checkpoint = manifest["sources"]["lyon-stahl"]
            assert checkpoint["attempts"][0]["rc"] == refresh.COHORT_SETTLED_FAILURE_EXIT
            rejected = json.loads((run_dir / checkpoint["attempts"][0]["rejected_artifact"]).read_text())
            assert rejected["listings"] == [] and rejected["runMeta"]["evidenceOnly"] is True
            if denied:
                assert failures == ["lyon-stahl"]
                assert checkpoint["lyon_partial_worker"]["state"] == "source_held"
            else:
                assert failures == []
                assert checkpoint["lyon_partial_worker"]["state"] == "settled"
                assert checkpoint["artifact"]["lyon_partial"] is True
                assert manifest["pipeline"]["source_results"]["lyon-stahl"]["partial"] is True
        finally:
            lock.clear_recovery_requirement()
