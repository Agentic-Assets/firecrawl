import json
import subprocess
import sys
from pathlib import Path


COLLECTOR = Path(__file__).resolve().parents[1]


def test_live_ingest_rejects_monitor_artifact_before_database_access(tmp_path):
    artifact = tmp_path / "monitor.json"
    artifact.write_text(
        json.dumps(
            {
                "runMeta": {
                    "mode": "monitor",
                    "startedAt": "2026-07-29T12:00:00Z",
                    "finishedAt": "2026-07-29T12:01:00Z",
                },
                "sources": [],
                "listings": [],
            }
        )
    )
    proc = subprocess.run(
        [sys.executable, "cre_ingest.py", "--in", str(artifact)],
        cwd=COLLECTOR,
        text=True,
        capture_output=True,
    )
    assert proc.returncode != 0
    assert "refusing live ingest for artifact mode 'monitor'" in proc.stderr
    assert "credentials:" not in proc.stderr


def test_dry_run_does_not_apply_live_mode_admission_gate(tmp_path):
    artifact = tmp_path / "monitor.json"
    artifact.write_text(
        json.dumps(
            {
                "runMeta": {
                    "mode": "monitor",
                    "startedAt": "2026-07-29T12:00:00Z",
                    "finishedAt": "2026-07-29T12:01:00Z",
                },
                "sources": [],
                "listings": [],
            }
        )
    )
    proc = subprocess.run(
        [
            sys.executable,
            "cre_ingest.py",
            "--in",
            str(artifact),
            "--dry-run",
            "--keep-artifacts",
            str(tmp_path / "sql"),
        ],
        cwd=COLLECTOR,
        text=True,
        capture_output=True,
    )
    assert proc.returncode != 0
    assert "nothing to ingest" in proc.stderr
    assert "refusing live ingest" not in proc.stderr
    assert "credentials:" not in proc.stderr
