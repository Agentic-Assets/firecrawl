"""Sealed source replay refuses changed or terminal evidence."""
import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cre_lyon_partial_checkpoint import replay_settled_source
from cre_partial_page_custody import prepare, settle
from tests.test_lyon_search_replay import spool


class LyonCheckpointTests(unittest.TestCase):
    def case(self, root):
        state = prepare(root, 1, "lyon-stahl")
        source_path = root / state["path"]
        row = {"permalink": "https://lyonstahl.com/properties/example/", "title": "Source value"}
        now = datetime.now(UTC)
        raw = spool([("all", json.dumps([row])), ("sale", "[]"), ("lease", "[]")])
        raw = raw.replace(b"2026-11-05T00:00:00Z", now.isoformat().encode())
        # Fixture is generated for generation g; replace only its header binding.
        header, rest = raw.split(b"\n", 1)
        header = json.loads(header)
        header["generation"] = root.name
        raw = json.dumps(header).encode() + b"\n" + rest
        source_path.write_bytes(raw)
        source_path.chmod(0o600)
        summary = {"schema": "lyon-search-summary-v1", "coverage": "partial_unknown", "stopped": False,
                   "failures": [], "candidates": 1, "held": 0}
        receipt = {"state": "sealed", "coverage": "unverified", "outcome": "source_error", "sourceKey": "lyon-stahl",
                   "generationId": root.name, "attempt": 1, "path": state["path"], "bytes": len(raw),
                   "observationRecords": 3, "sha256": hashlib.sha256(raw).hexdigest()}
        artifact = {"runMeta": {"evidenceOnly": True}, "listings": [], "sources": [
            {"sourceKey": "lyon-stahl", "transaction": "sale", "error": "Error: Lyon search is partial and requires source-bound admission",
             "retainedObservations": [summary], "partialCapture": receipt}]}
        rejected = root / "rejected"
        rejected.mkdir(mode=0o700)
        main = rejected / "lyon-stahl-attempt-1.json"
        main.write_text(json.dumps(artifact))
        main.chmod(0o600)
        attempt = {"number": 1, "started_at": (now - timedelta(seconds=2)).isoformat(), "finished_at": now.isoformat(),
                   "rc": 80, "lyon-stahl_partial": state, "rejected_artifact": str(main.relative_to(root)),
                   "rejected_artifact_sha256": hashlib.sha256(main.read_bytes()).hexdigest()}
        settle(root, attempt, main, "lyon-stahl")
        manifest = {"run_id": root.name, "started_at": (now - timedelta(seconds=3)).isoformat(),
                    "collector_git_sha": "a" * 40, "collector_git_dirty": False,
                    "preflight": {"database_target": {"algorithm": "sha256", "value": "b" * 64}},
                    "sources": {"lyon-stahl": {"attempts": [attempt]}}}
        return manifest, source_path, main

    def test_revalidates_sealed_source_and_preserves_raw_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _, _ = self.case(root)
            result = replay_settled_source(root, manifest, 1, "a" * 40, False)
            self.assertEqual(result["staged"][0]["external_id"], "example")
            self.assertEqual(result["records"][0]["occurrences"][0]["row"]["title"], "Source value")
            self.assertEqual(result["held"], [])

    def test_changed_attempt_owner_or_clock_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _, _ = self.case(root)
            with self.assertRaises(ValueError):
                replay_settled_source(root, manifest, 1, "c" * 40, False)
            attempt = manifest["sources"]["lyon-stahl"]["attempts"][0]
            attempt["started_at"] = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
            attempt["finished_at"] = (datetime.now(UTC) + timedelta(seconds=11)).isoformat()
            with self.assertRaises(ValueError):
                replay_settled_source(root, manifest, 1, "a" * 40, False)

    def test_changed_source_and_terminal_outcome_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, source, main = self.case(root)
            source.write_bytes(source.read_bytes() + b" ")
            with self.assertRaises(ValueError):
                replay_settled_source(root, manifest, 1, "a" * 40, False)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _, main = self.case(root)
            data = json.loads(main.read_text())
            data["runMeta"]["terminalCaptureFailure"] = True
            main.write_text(json.dumps(data))
            digest = hashlib.sha256(main.read_bytes()).hexdigest()
            attempt = manifest["sources"]["lyon-stahl"]["attempts"][0]
            attempt["rejected_artifact_sha256"] = digest
            attempt["lyon-stahl_partial"]["main_sha256"] = digest
            with self.assertRaises(ValueError):
                replay_settled_source(root, manifest, 1, "a" * 40, False)
