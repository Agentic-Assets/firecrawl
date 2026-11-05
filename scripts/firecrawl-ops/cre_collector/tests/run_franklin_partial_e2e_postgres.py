"""Owned no-TCP PostgreSQL end-to-end Franklin partial admission contract."""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path[:0] = [str(ROOT), str(HERE / "observation_store")]

from test_franklin_site_partial import mixed

import cre_checkpoint_refresh as refresh
import cre_franklin_site_admission as admission
from cre_ingest import database_target_fingerprint_from_url, psql_connection_env


@contextmanager
def disposable_cluster():
    name = "franklin-e2e-" + uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix="fpg-", dir="/tmp") as directory:
        root = Path(directory)
        root.chmod(0o777)  # Synthetic socket only; no TCP listener or production data.
        try:
            subprocess.run(["docker", "run", "-d", "--name", name, "--network=none",
                            "--cpus=0.5", "--memory=512m", "--pids-limit=128", "-v", f"{root}:/s",
                            "-e", "POSTGRES_PASSWORD=synthetic-only", "postgres:17",
                            "-c", "unix_socket_directories=/var/run/postgresql,/s", "-c", "listen_addresses=",
                            "-c", "unix_socket_permissions=0777"],
                           check=True, capture_output=True, text=True, timeout=30)
            for _ in range(100):
                logs = subprocess.run(["docker", "logs", name], check=False, capture_output=True, text=True)
                ready = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres", "-h", "/s"],
                                       check=False, capture_output=True).returncode == 0
                if ready and (logs.stdout + logs.stderr).count("database system is ready to accept connections") >= 2:
                    break
                time.sleep(.2)
            else:
                raise RuntimeError("disposable postgres not ready: " + (logs.stdout + logs.stderr)[-1500:])
            yield root
        finally:
            subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True, timeout=30)


def main():
    with disposable_cluster() as cluster_root:
        uri = f"postgresql://postgres@{quote(str(cluster_root), safe='')}:5432/postgres"
        target = database_target_fingerprint_from_url(uri)["value"]
        env_file = cluster_root / "synthetic.env"
        env_file.write_text("POSTGRES_URL=" + uri + "\n")
        env_file.chmod(0o600)
        directory = ROOT.parent / "sql"
        master = (directory / "000_run_all.sql").read_text()
        master = re.sub(r"^\\i (\S+)\s*$", lambda m: (directory / m[1]).read_text(), master, flags=re.MULTILINE)
        schema = master + "\n" + "\n".join([
            "\\set CRE_APPROVE_LISTING_LIFECYCLE 1",
            "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-5257",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text()])
        subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1"], input=schema, text=True,
                       env=psql_connection_env(uri), stdout=subprocess.DEVNULL, check=True, timeout=60)

        raw, _ = mixed()
        data = json.loads(raw)
        now = datetime.now(UTC)
        gen = (now - timedelta(minutes=3)).isoformat()
        attempt_start = (now - timedelta(minutes=2)).isoformat()
        started = (now - timedelta(minutes=1)).isoformat()
        observed = (now - timedelta(seconds=30)).isoformat()
        finished = now.isoformat()
        data["runMeta"].update(startedAt=started, finishedAt=finished,
                               freshness={"generationId": "run-1", "generationStartedAt": gen,
                                          "requireFreshDetails": True})
        data["sources"][0]["retainedObservations"][0]["observedAt"] = observed
        raw = json.dumps(data).encode()
        run = cluster_root / "run-1"
        original = run / "rejected" / "franklin-street-attempt-1.json"
        original.parent.mkdir(parents=True, mode=0o700)
        original.write_bytes(raw)
        original.chmod(0o600)
        attempt = {"number": 1, "rc": 80, "started_at": attempt_start, "finished_at": finished,
                   "rejected_artifact": "rejected/franklin-street-attempt-1.json",
                   "rejected_artifact_sha256": hashlib.sha256(raw).hexdigest()}
        checkpoint = {"attempts": [attempt], "state": "collecting"}
        manifest = {"run_id": "run-1", "started_at": gen, "collector_git_sha": "a" * 40, "collector_git_dirty": False,
                    "config": {"sources": ["franklin-street"], "transactions": ["sale", "lease"], "max_items": 0,
                               "additive": True, "mark_missing": False, "status_activation": False, "pipeline": False},
                    "scope": {"source_keys": ["franklin-street"]},
                    "preflight": {"database_target": {"algorithm": "sha256", "value": target}},
                    "sources": {"franklin-street": checkpoint}}
        old_git, old_save = refresh.git_identity, refresh.save_manifest
        refresh.git_identity = lambda: ("a" * 40, False)
        refresh.save_manifest = lambda *_args: None
        try:
            assert refresh.prepare_franklin_partial_candidate(run, manifest, attempt, candidate_only=False)
            path = run / checkpoint["artifact"]["path"]
            checkpoint["franklin_identity_probe"] = admission.run_identity_probe(
                path, manifest, env_file=str(env_file), process_env={"PATH": os.environ["PATH"]})
            gate_path = cluster_root / "gate.json"
            gate = subprocess.run([sys.executable, str(ROOT / "cre_gate.py"), "--in", str(path),
                                   "--apply", "--strict", "--out", str(gate_path),
                                   "--env-file", str(env_file), "--expected-db-target-sha256", target],
                                  check=False, capture_output=True, text=True, timeout=60, cwd=ROOT)
            if gate.returncode not in (0, 2):
                raise RuntimeError("fixture gate failed: " + gate.stderr)
            source_report = json.loads(gate_path.read_text())
            source_binding = admission.admit(path, manifest, source_report, code_sha="a" * 40, dirty=False,
                                             env={"PATH": os.environ["PATH"]}, phase="source")
            checkpoint["gate"] = {"current_inventory_proof": source_binding}
            aggregate_report = json.loads(gate_path.read_text())
            assert admission.admit(path, manifest, aggregate_report, code_sha="a" * 40, dirty=False,
                                   env={"PATH": os.environ["PATH"]}, phase="aggregate") == source_binding
            envelope = {"manifest": manifest, "source_report": source_report,
                        "aggregate_report": aggregate_report, "binding": source_binding, "attempt_id": "d" * 32}
            claims_dir = run / "claims"
            claims_dir.mkdir(mode=0o700)
            claims = claims_dir / "claims.json"
            claims.write_text(json.dumps(envelope))
            claims.chmod(0o600)
            args = SimpleNamespace(inputs=[str(path)], mark_missing=False, activate_status=False, require_strict_freshness=True,
                expected_db_target_sha256=target, checkpoint_ingest_attempt="d" * 32, jll_partial_claims=None,
                daum_verified_subset_claims=None, pyramid_verified_subset_claims=None, matthews_targeted_claims=None,
                newmark_page_attempt=None, newmark_page_claims=None, checkpoint_collector_git_sha=None,
                checkpoint_pyramid_code_sha=None, checkpoint_jll_code_sha=None, franklin_site_claims=str(claims),
                keep_artifacts=None, dry_run=False, env_file=str(env_file))
            admission.run_ingest(args)
            context = admission.ingest_subset_context(path, envelope, code_sha="a" * 40, dirty=False,
                                                       target_sha256=target, attempt_id="d" * 32,
                                                       env={"PATH": os.environ["PATH"]})
            result = admission.run_readback(path, manifest, context, target_sha256=target,
                                            env_file=str(env_file), process_env={"PATH": os.environ["PATH"]})
            assert result["ok"] is True and result["expected"] == 1
            assert source_binding["buildout_pending"] == 1 and source_binding["whole_source_coverage"] is False
            assert original.read_bytes() == raw
            print("Owned no-TCP Franklin partial end-to-end proof passed")
        finally:
            refresh.git_identity, refresh.save_manifest = old_git, old_save


if __name__ == "__main__":
    main()