"""Synthetic owner/worker proof in a disposable repository; no provider traffic."""
import copy
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_colliers_partial as partial
import pytest
from cre_checkpoint_refresh import SharedLock
from cre_partial_cursor import read_cursor


def command(*args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=True, timeout=60)


@pytest.mark.parametrize(("detail_id", "expected_state", "expected_cursor"),
                         [("a", "candidate", 1), ("different", "partial_held_no_write", 2)])
def test_worker_entry_requires_real_owned_authority_and_seals_fake_source(tmp_path, monkeypatch, detail_id, expected_state, expected_cursor):
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
    partial_dir = run_dir / "colliers-partial"
    partial_dir.mkdir(parents=True, mode=0o700)
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
    source_path = partial_dir / "attempt-1.jsonl"
    source_path.write_bytes(source)
    source_path.chmod(0o600)
    sentinel = tmp_path / "fake-fetch-called"
    mock = tmp_path / "fake-fetch.mjs"
    mock.write_text(
        "import {writeFileSync} from 'node:fs';\n"
        "globalThis.fetch = async () => { writeFileSync(" + json.dumps(str(sentinel)) + ", 'fake');"
        "return new Response(JSON.stringify({ProjectSummary:{ProjectId:" + json.dumps(detail_id) + "}}),"
        "{status:200,headers:{'content-type':'application/json'}}); };\n"
    )
    lock = SharedLock(target / "out" / "daily" / ".cre.lock", recovery_required=True)
    with lock:
        try:
            source_receipt = {"generation": "gen", "generation_started_at": "2026-11-04T14:00:00Z", "attempt": 1,
                "started_at": "2026-11-04T15:00:00Z", "finished_at": "2026-11-04T16:00:00Z",
                "bytes": len(source), "sha256": hashlib.sha256(source).hexdigest(), "main_sha256": "c" * 64,
                "producer_sha": sha, "database_target_sha256": "b" * 64}
            manifest = {"run_id": "gen", "started_at": source_receipt["generation_started_at"],
                "collector_git_sha": sha, "collector_git_dirty": False,
                "config": {"additive": True, "mark_missing": False, "status_activation": False,
                           "transactions": ["sale", "lease"], "max_items": 0, "sources": ["colliers"]},
                "sources": {"colliers": {"attempts": [{"number": 1}]}}}
            env = {**os.environ, "NODE_OPTIONS": "--import=" + mock.as_uri()}
            monkeypatch.setattr(refresh, "canonical_shared_lock_dir", lambda: lock.path)
            monkeypatch.setattr(refresh, "git_identity", lambda: (sha, False))
            monkeypatch.setattr(refresh, "safe_process_env", lambda: dict(env))
            monkeypatch.setattr(partial, "revalidate_source_receipt", lambda *_: source_receipt)
            snapshots = []
            def save(_run, value):
                snapshots.append(copy.deepcopy(value))
            def runner(argv, _log, *, env):
                child = subprocess.run(argv, cwd=target, env=env, capture_output=True, text=True, timeout=30, check=False)
                assert child.returncode == 0, child.stderr
                return child.returncode
            result = partial.run_window_worker(run_dir, manifest, lock, runner, save)
            assert sentinel.read_text() == "fake"
            receipt = json.loads((partial_path(run_dir) / "window-1-receipt.json").read_text())
            assert receipt["admitted"] == (1 if detail_id == "a" else 0)
            checkpoint = manifest["sources"]["colliers"]
            assert checkpoint["state"] == expected_state
            assert read_cursor(lock.path, "colliers", "b" * 64)[0] == expected_cursor
            assert snapshots[0]["sources"]["colliers"]["colliers_partial_worker"]["state"] == "dispatching"
            assert (result is not None) == (detail_id == "a")
            artifact = json.loads((partial_path(run_dir) / "window-1-artifact.json").read_text())
            assert artifact["sourceOutcome"] == "source_error"
        finally:
            lock.clear_recovery_requirement()


def partial_path(run_dir):
    return run_dir / "colliers-partial"
