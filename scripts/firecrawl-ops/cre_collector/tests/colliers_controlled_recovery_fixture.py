"""Exercise controlled Colliers recovery against the caller's disposable database.

The local HTTP health service, Docker health CLI and source responses are
synthetic. Series and checkpoint ownership, settlement receipts, PostgreSQL
readback, Git identity checks and cursor publication use the real code.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path


def run(root: Path, collector: Path, env_file: Path, psql: Path, fake_fetch: Path, target: str, count_jobs) -> None:
    fixture = root / "controlled"
    fixture.mkdir(mode=0o700)
    repo = fixture / "repo"
    destination = repo / "scripts" / "firecrawl-ops" / "cre_collector"
    destination.parent.mkdir(parents=True)
    shutil.copytree(collector, destination, ignore=shutil.ignore_patterns(
        "node_modules", "out", "__pycache__", ".pytest_cache", "*.pyc"))
    (destination / "node_modules").symlink_to(collector / "node_modules", target_is_directory=True)
    ops = destination.parent
    for filename in ("firecrawl_healthcheck.sh", "firecrawl_ingress_check.py"):
        shutil.copy2(collector.parent / filename, ops / filename)
    (repo / "apps" / "api").mkdir(parents=True)
    (repo / "apps" / "api" / ".fixture").write_text("synthetic\n")
    (repo / "docker-compose.yaml").write_text("services: {}\n")
    (repo / ".gitignore").write_text("node_modules\nout\n")

    def command(*args, **kwargs):
        return subprocess.run(args, check=True, capture_output=True, text=True, timeout=180, **kwargs)

    command("git", "-C", str(repo), "init", "-q")
    command("git", "-C", str(repo), "add", ".")
    command("git", "-C", str(repo), "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.test",
            "commit", "-qm", "synthetic producer")
    producer = command("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
    assert not command("git", "-C", str(repo), "status", "--porcelain").stdout

    # Docker health queries are not needed for a synthetic HTTP server; the
    # only actual Docker commands remain the caller's mount-free psql shim.
    bin_dir = fixture / "bin"
    bin_dir.mkdir(mode=0o700)
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\ncase \"$*\" in 'compose ps --no-trunc') echo synthetic; exit 0;; *) exit 1;; esac\n")
    docker.chmod(0o700)
    (bin_dir / "psql").symlink_to(psql)

    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"synthetic")

        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", "0")))
            data = b'{"success":true,"data":{"markdown":"synthetic"}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Only the test timing is injected. Once the second real synthetic ingest
    # is recorded, signal the real series owner before validation starts; its
    # actual stop handlers and checkpoint teardown must publish the receipts.
    hooks = fixture / "hooks"
    hooks.mkdir(mode=0o700)
    marker = fixture / "after-second-ingest"
    (hooks / "sitecustomize.py").write_text('''
import os, signal, sys, time
from pathlib import Path
if Path(sys.argv[0]).name == "cre_checkpoint_refresh.py" and not any(a.startswith("--_cohort") for a in sys.argv):
    def trace(frame, event, arg):
        if event != "call" or frame.f_code.co_name != "main" or Path(frame.f_globals.get("__file__", "")).name != "cre_checkpoint_refresh.py":
            return trace
        sys.settrace(None)
        import cre_checkpoint_refresh as refresh
        original = refresh.run_final_validation
        def inject(run_dir, manifest, *args, **kwargs):
            checkpoint = manifest.get("sources", {}).get("colliers", {})
            if len(checkpoint.get("partial_windows", [])) == 1 and checkpoint.get("state") == "ingested":
                Path(os.environ["COLLIERS_CONTROLLED_MARKER"]).write_text("settled ingest before validation")
                os.kill(os.getppid(), signal.SIGALRM)
                while True:
                    time.sleep(0.1)
            return original(run_dir, manifest, *args, **kwargs)
        refresh.run_final_validation = inject
        return None
    sys.settrace(trace)
''')
    env = dict(os.environ)
    env.update(PATH=str(bin_dir) + ":" + env.get("PATH", "/usr/bin:/bin"),
               PYTHONPATH=str(hooks), PSQL_BIN=str(psql), NODE_OPTIONS="--import=" + fake_fetch.as_uri(),
               FIRECRAWL_API_URL=f"http://127.0.0.1:{server.server_port}",
               COLLIERS_CONTROLLED_MARKER=str(marker), SYNTHETIC_DENIAL="0", SYNTHETIC_DRIFT="0",
               SYNTHETIC_JOURNAL="1", SYNTHETIC_ACCESS_STOP="0",
               SYNTHETIC_DETAIL_SENTINEL=str(fixture / "detail-called"))
    log = fixture / "series.log"
    try:
        argv = [sys.executable, str(destination / "cre_checkpoint_series.py"),
                "--sources", "colliers", "--pipeline", "--source-workers", "1", "--page-cap", "3",
                "--concurrency", "1", "--attempts-per-source", "1", "--nice", "15",
                "--work-deadline-seconds", "1800", "--env-file", str(env_file)]
        with log.open("w") as output:
            result = subprocess.run(argv, cwd=repo, env=env, stdout=output, stderr=subprocess.STDOUT,
                                    timeout=360, check=False)
        assert result.returncode == 124, f"controlled series exit {result.returncode}; see {log}"
        assert marker.is_file(), "second ingest interruption was not reached"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    series_root = destination / "out" / "checkpoint-series"
    histories = [p for p in series_root.iterdir() if p.is_dir() and (p / "manifest.json").is_file()]
    assert len(histories) == 1
    series_dir = histories[0]
    parent = json.loads((series_dir / "manifest.json").read_text())
    invocation = parent["latest_invocation_control"]["invocation_id"]
    receipt = json.loads((series_dir / ".series-control" / (invocation + ".json")).read_text())
    assert receipt["status"] == "deadline_interrupted" and receipt["local_settlement"] == "confirmed"
    child = series_dir / "runs" / receipt["last_checkpoint"]["run_id"]
    manifest = json.loads((child / "manifest.json").read_text())
    current = manifest["sources"]["colliers"]
    assert current["state"] == "ingested" and current["ingest"]["rc"] == 0
    assert len(current["partial_windows"]) == 1 and current.get("partial_cursor_settlement") is None
    assert manifest["pipeline"]["commit_outcome_unknown"] is False
    old, new = current["gate"]["current_inventory_proof"]["cursor"], current["gate"]["current_inventory_proof"]["next_cursor"]
    assert old != new

    # Move the synthetic repo to a genuine clean descendant before recovery.
    command("git", "-C", str(repo), "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.test",
            "commit", "--allow-empty", "-qm", "synthetic verifier")
    verifier = command("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
    assert command("git", "-C", str(repo), "merge-base", producer, verifier).stdout.strip() == producer
    assert not command("git", "-C", str(repo), "status", "--porcelain").stdout
    recovery_script = fixture / "recover.py"
    recovery_script.write_text('''
import json, sys
from pathlib import Path
from cre_checkpoint_refresh import SharedLock, canonical_shared_lock_dir, database_target_fingerprint
from cre_partial_cursor import read_cursor
from recovery.colliers_cursor import recover_prior_generation
owner_path = canonical_shared_lock_dir()
target = database_target_fingerprint(sys.argv[1])["value"]
marks = []
with SharedLock(owner_path, recovery_required=True) as owner:
    before = read_cursor(owner.path, "colliers", target)[0]
    report = recover_prior_generation(owner, expected_target=target, env_file=sys.argv[1],
        before_publish=lambda: marks.append("armed"), after_publish=lambda: marks.append("settled"))
    after = read_cursor(owner.path, "colliers", target)[0]
    again = recover_prior_generation(owner, expected_target=target, env_file=sys.argv[1],
        before_publish=lambda: marks.append("unexpected-armed"),
        after_publish=lambda: marks.append("unexpected-settled"))
    assert again is None
    print(json.dumps({"before": before, "after": after, "marks": marks, "report": report}))
    owner.clear_recovery_requirement()
''')
    recovery_env = dict(env)
    recovery_env.pop("NODE_OPTIONS", None)
    recovery_env["PYTHONPATH"] = str(destination)
    jobs_before_recovery = count_jobs()
    recovery_log = fixture / "recovery.log"
    with recovery_log.open("w") as output:
        recovered = subprocess.run([sys.executable, str(recovery_script), str(env_file)], cwd=repo,
            env=recovery_env, stdout=output, stderr=subprocess.STDOUT, timeout=180, check=False)
    assert recovered.returncode == 0, f"recovery exit {recovered.returncode}; see {recovery_log}"
    lines = recovery_log.read_text().splitlines()
    result = json.loads(lines[-1])
    assert (result["before"], result["after"], result["marks"]) == (old, new, ["armed", "settled"])
    assert count_jobs() == jobs_before_recovery, "recovery created another scrape job"
    assert result["report"]["target"] == target
    assert result["report"]["readback"]["outcome"] == "exact_commit"
    assert result["report"]["whole_source_coverage"] is False
    print(f"Controlled interrupted recovery and cursor CAS: PASS ({old} -> {new})", flush=True)