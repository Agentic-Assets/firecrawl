"""Synthetic owner/worker proof in a disposable repository; no provider traffic."""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from cre_checkpoint_refresh import SharedLock


def command(*args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=True, timeout=60)


def test_worker_entry_requires_real_owned_authority_and_seals_fake_source(tmp_path):
    collector = Path(__file__).resolve().parents[1]
    repo = tmp_path / "repo"
    target = repo / "scripts" / "firecrawl-ops" / "cre_collector"
    target.parent.mkdir(parents=True)
    shutil.copytree(collector, target, ignore=shutil.ignore_patterns(
        "node_modules", "out", "__pycache__", ".pytest_cache", "*.pyc"))
    (target / "node_modules").symlink_to(collector / "node_modules", target_is_directory=True)
    (repo / ".gitignore").write_text("node_modules\nout\n")
    command("git", "init", "-q", cwd=repo)
    command("git", "add", ".", cwd=repo)
    command("git", "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.test",
            "commit", "-qm", "fixture", cwd=repo)
    sha = command("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
    run_dir = tmp_path / "gen"
    partial = run_dir / "colliers-partial"
    partial.mkdir(parents=True, mode=0o700)
    common = {"transaction": "sale", "pass": 1, "page": 1, "start": 1}
    listing = {"success": True, "total": 2, "totalAvail": 2, "numProjects": 1,
               "html": '<li class="item"><span class="headline">Fixture</span><a href="/handler/modern.aspx?pv=a">View</a></li>'}
    mapping = {"success": True, "total": 2, "totalAvail": 2, "projectLocations": [{"ProjectId": "a"}]}
    rows = [
        {"schema": "colliers-partial-pages/v1", "sourceKey": "colliers", "generation": "gen", "attempt": 1},
        {**common, "pageSize": 1, "observedAt": "2026-11-04T15:30:00.000Z", "kind": "unqualified_response_pair",
         "documents": [{"list": json.dumps(listing), "map": json.dumps(mapping)}]},
        {**common, "observedAt": "2026-11-04T15:30:01.000Z", "kind": "parsed_page",
         "documents": [{"mapProjectId": "a", "detailPv": "a", "detailUrl": "https://my.rcm1.com/handler/modern.aspx?pv=a"}]},
        {**common, "start": 2, "pageSize": 1, "observedAt": "2026-11-04T15:30:02.000Z",
         "kind": "recovery_hole", "reason": "provider_failure", "documents": []},
    ]
    source = ("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)).encode()
    source_path = partial / "attempt-1.jsonl"
    source_path.write_bytes(source)
    source_path.chmod(0o600)
    sentinel = tmp_path / "fake-fetch-called"
    mock = tmp_path / "fake-fetch.mjs"
    mock.write_text(
        "import {writeFileSync} from 'node:fs';\n"
        "globalThis.fetch = async () => { writeFileSync(" + json.dumps(str(sentinel)) + ", 'fake');"
        "return new Response(JSON.stringify({ProjectSummary:{ProjectId:'a'}}),"
        "{status:200,headers:{'content-type':'application/json'}}); };\n"
    )
    lock = SharedLock(target / "out" / "daily" / ".cre.lock", recovery_required=True)
    with lock:
        try:
            request = {"generation": "gen", "generationStartedAt": "2026-11-04T14:00:00Z", "attempt": 1,
                       "startedAt": "2026-11-04T15:00:00Z", "finishedAt": "2026-11-04T16:00:00Z",
                       "sourceBytes": len(source), "sourceSha256": hashlib.sha256(source).hexdigest(),
                       "originalSourceMainSha256": "c" * 64, "targetSha256": "b" * 64, "producerSha": sha,
                       "cursor": 1, "ownerPid": os.getpid(), "runDir": str(run_dir),
                       "lockLease": lock.lease_token, "authorityToken": lock.authority_token}
            request_path = partial / "window-1-request.json"
            request_path.write_text(json.dumps(request))
            request_path.chmod(0o600)
            env = {**os.environ, "NODE_OPTIONS": "--import=" + mock.as_uri(),
                   "COLLIERS_PARTIAL_WORKER_REQUEST": str(request_path)}
            child = subprocess.run(["node", "--import", "tsx", "sources/colliers-partial-window-entry.ts"],
                                   cwd=target, env=env, capture_output=True, text=True, timeout=30, check=False)
            assert child.returncode == 0, child.stderr
            assert sentinel.read_text() == "fake"
            receipt = json.loads((partial / "window-1-receipt.json").read_text())
            assert receipt["admitted"] == 1 and receipt["held"] == 0
            artifact = json.loads((partial / "window-1-artifact.json").read_text())
            assert artifact["sourceOutcome"] == "source_error"
            assert artifact["listings"][0]["id"] == "a"
        finally:
            lock.clear_recovery_requirement()
