import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

import cre_partial_page_custody as custody
import cre_checkpoint_refresh as refresh


class CbrePartialCustodyTests(unittest.TestCase):
    def make_case(self, root):
        state = custody.prepare(root, 1)
        attempt = {"number": 1, "cbre_partial": state}
        path = root / state["path"]
        header = {"schema": "cbre-partial-pages/v1", "sourceKey": "cbre",
                  "generation": root.name, "attempt": 1}
        payload = (json.dumps(header) + "\n" + json.dumps({"kind": "validated_page"}) + "\n").encode()
        path.write_bytes(payload)
        path.chmod(0o600)
        receipt = {"state": "sealed", "coverage": "unverified", "outcome": "source_error",
                   "sourceKey": "cbre", "generationId": root.name,
                   "attempt": 1, "path": state["path"], "observationRecords": 1, "bytes": len(payload),
                   "sha256": hashlib.sha256(payload).hexdigest()}
        main = root / "main.json"
        main.write_text(json.dumps({"sources": [{"sourceKey": "cbre",
                                                "error": "original failure",
                                                "partialCapture": receipt}]}))
        main.chmod(0o600)
        return attempt, path, main

    def test_settled_file_is_bound_to_original_failed_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempt, path, main = self.make_case(root)
            custody.settle(root, attempt, main)
            self.assertEqual(attempt["cbre_partial"]["state"], "sealed_local")
            self.assertTrue(path.exists())
            self.assertEqual(attempt["cbre_partial"]["main_sha256"], hashlib.sha256(main.read_bytes()).hexdigest())

    def test_tampering_and_missing_main_remain_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempt, path, main = self.make_case(root)
            path.write_bytes(path.read_bytes() + b"tamper")
            custody.settle(root, attempt, main)
            self.assertEqual(attempt["cbre_partial"]["state"], "unverified")
            self.assertTrue(path.exists())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempt, path, _ = self.make_case(root)
            custody.settle(root, attempt, None)
            self.assertEqual(attempt["cbre_partial"]["state"], "unverified")
            self.assertTrue(path.exists())

    def test_rejected_artifact_archival_keeps_sidecar_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempt, path, main = self.make_case(root)
            rejected, digest = refresh.archive_rejected_with_evidence(
                main, root, {}, "cbre", attempt)
            self.assertEqual(attempt["cbre_partial"]["state"], "sealed_local")
            self.assertTrue(path.exists())
            self.assertEqual(digest, attempt["cbre_partial"]["main_sha256"])
            self.assertEqual(json.loads((root / rejected).read_text())["sources"][0]["error"],
                             "original failure")

    def test_worker_env_is_bound_to_private_attempt_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            custody.prepare(root, 2)
            env, _ = refresh.fresh_source_env("cbre", root, base={"CRE_CBRE_PARTIAL_PATH": "/unsafe"},
                                              attempt_number=2)
            self.assertEqual(env["CRE_CBRE_PARTIAL_PATH"], str(custody.expected_path(root, 2)))
            self.assertEqual(env["CRE_CBRE_PARTIAL_ATTEMPT"], "2")
            other, _ = refresh.fresh_source_env("svn", root, base={"CRE_CBRE_PARTIAL_PATH": "/unsafe"})
            self.assertNotIn("CRE_CBRE_PARTIAL_PATH", other)

    def test_interrupted_attempt_is_preserved_without_assuming_settlement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempt, path, _ = self.make_case(root)
            attempt.update(finished_at=None, rc=None)
            custody.reconcile(root, {"sources": {"cbre": {"attempts": [attempt]}}})
            self.assertEqual(attempt["cbre_partial"]["state"], "unverified")
            self.assertTrue(path.exists())

    def test_resume_settled_rejected_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempt, path, main = self.make_case(root)
            rejected = root / "rejected" / "cbre-attempt-1.json"
            rejected.parent.mkdir()
            main.rename(rejected)
            attempt.update(finished_at="2026-01-01T00:00:00Z", rc=1)
            custody.reconcile(root, {"sources": {"cbre": {"attempts": [attempt]}}})
            self.assertEqual(attempt["cbre_partial"]["state"], "sealed_local")
            self.assertTrue(path.exists())

    def test_wrong_generation_and_linked_sidecar_are_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempt, path, main = self.make_case(root)
            data = json.loads(main.read_text())
            data["sources"][0]["partialCapture"]["generationId"] = "other"
            main.write_text(json.dumps(data))
            custody.settle(root, attempt, main)
            self.assertEqual(attempt["cbre_partial"]["state"], "unverified")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempt, path, main = self.make_case(root)
            os.link(path, root / "hardlink")
            custody.settle(root, attempt, main)
            self.assertEqual(attempt["cbre_partial"]["state"], "unverified")

    def test_malformed_main_shapes_are_unverified_without_raising(self):
        for value in (None, [], 42, {"sources": 42}, {"sources": "bad"},
                      {"sources": [{"sourceKey": "cbre", "partialCapture": ["bad"]}]}):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                attempt, path, main = self.make_case(root)
                main.write_text(json.dumps(value))
                custody.settle(root, attempt, main)
                self.assertEqual(attempt["cbre_partial"]["state"], "unverified")
                self.assertTrue(path.exists())

    def test_valid_sealed_partial_prefix_is_recorded_as_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempt, path, main = self.make_case(root)
            data = json.loads(main.read_text())
            data["sources"][0]["partialCapture"]["state"] = "sealed_partial"
            main.write_text(json.dumps(data))
            custody.settle(root, attempt, main)
            self.assertEqual(attempt["cbre_partial"]["state"], "sealed_local_partial")
            self.assertEqual(attempt["cbre_partial"]["outcome"], "source_error")
            self.assertTrue(path.exists())

    def test_existing_path_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, path, _ = self.make_case(root)
            self.assertEqual(custody.prepare(root, 1)["state"], "unavailable")
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
