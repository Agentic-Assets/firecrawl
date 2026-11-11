import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import cre_partial_page_custody as custody
import cre_checkpoint_refresh as refresh


class MatthewsPartialCustodyTests(unittest.TestCase):
    def test_checkpoint_path_and_settled_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = custody.prepare(root, 1, "matthews")
            attempt = {"number": 1, "matthews_partial": state}
            path = root / state["path"]
            header = {"schema": "matthews-partial-pages/v1", "sourceKey": "matthews",
                      "generation": root.name, "attempt": 1}
            payload = (json.dumps(header) + "\n" + json.dumps({"kind": "matthews_detail_outcome"}) + "\n").encode()
            path.write_bytes(payload)
            path.chmod(0o600)
            receipt = {"state": "sealed", "coverage": "unverified", "outcome": "source_error",
                       "sourceKey": "matthews", "generationId": root.name, "attempt": 1,
                       "path": state["path"], "observationRecords": 1, "bytes": len(payload),
                       "sha256": hashlib.sha256(payload).hexdigest()}
            main = root / "main.json"
            main.write_text(json.dumps({"sources": [{"sourceKey": "matthews", "error": "partial",
                                                     "partialCapture": receipt}]}))
            main.chmod(0o600)
            custody.settle(root, attempt, main, "matthews")
            self.assertEqual(state["state"], "sealed_local")
            self.assertEqual(state["coverage"], "unverified")
            custody.prepare(root, 2, "matthews")
            env, _ = refresh.fresh_source_env("matthews", root, attempt_number=2)
            self.assertEqual(env["CRE_MATTHEWS_PARTIAL_PATH"], str(custody.expected_path(root, 2, "matthews")))

    def test_interrupted_attempt_remains_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = custody.prepare(root, 1, "matthews")
            path = root / state["path"]
            path.write_text("partial\n")
            path.chmod(0o600)
            attempt = {"number": 1, "matthews_partial": state, "finished_at": None, "rc": None}
            custody.reconcile(root, {"sources": {"matthews": {"attempts": [attempt]}}}, "matthews")
            self.assertEqual(state["state"], "unverified")
            self.assertTrue(path.exists())
