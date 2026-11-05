import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_partial_page_custody as custody
from cre_colliers_partial_checkpoint import revalidate_source_receipt
from cre_owned_processes import OwnedProcessGroup


class ColliersPartialCustodyTest(unittest.TestCase):
    def test_failed_source_retains_attempt_bound_provisional_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempt = {"number": 1, "colliers_partial": custody.prepare(root, 1, "colliers")}
            path = root / attempt["colliers_partial"]["path"]
            header = {"schema": "colliers-partial-pages/v1", "sourceKey": "colliers",
                      "generation": root.name, "attempt": 1}
            payload = (json.dumps(header) + "\n" + json.dumps({"documents": [{"name": "Property"}]}) + "\n").encode()
            path.write_bytes(payload)
            path.chmod(0o600)
            receipt = {"state": "sealed", "coverage": "unverified", "outcome": "source_error",
                       "sourceKey": "colliers", "generationId": root.name, "attempt": 1,
                       "path": attempt["colliers_partial"]["path"], "observationRecords": 1,
                       "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            main = root / "main.json"
            main.write_text(json.dumps({"sources": [{"sourceKey": "colliers", "error": "failed",
                                                     "partialCapture": receipt}]}))
            main.chmod(0o600)
            custody.settle(root, attempt, main, "colliers")
            self.assertEqual(attempt["colliers_partial"]["state"], "sealed_local")

    def test_owned_nonzero_evidence_only_attempt_is_sealed_for_foreground(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "generation"
            root.mkdir()
            manifest = refresh.new_manifest(root, git_sha="a" * 40, git_dirty=False,
                sources=("colliers",), page_cap=400, concurrency=1, pipeline=True)
            manifest["preflight"] = {"database_target": {"algorithm": "sha256", "value": "b" * 64}}
            attempt, artifact, log_path, started = refresh._cohort_attempt(root, manifest, "colliers", collector_concurrency=1)
            spool_path = root / attempt["colliers_partial"]["path"]
            spool = (json.dumps({"schema": "colliers-partial-pages/v1", "sourceKey": "colliers",
                                 "generation": root.name, "attempt": 1}) + "\n" +
                     json.dumps({"kind": "fixture"}) + "\n").encode()
            receipt = {"state": "sealed", "coverage": "unverified", "outcome": "source_error",
                       "sourceKey": "colliers", "generationId": root.name, "attempt": 1,
                       "path": attempt["colliers_partial"]["path"], "observationRecords": 1,
                       "bytes": len(spool), "sha256": hashlib.sha256(spool).hexdigest()}
            main = {"listings": [], "runMeta": {"evidenceOnly": True},
                    "sources": [{"sourceKey": "colliers", "error": "provider page failed", "partialCapture": receipt}]}
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = log_path.open("a")
            code = ("import os,pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_bytes(bytes.fromhex(sys.argv[2])); "
                    "p.chmod(0o600); q=pathlib.Path(sys.argv[3]); q.write_text(sys.argv[4]); q.chmod(0o600); "
                    "sys.exit(int(sys.argv[5]))")
            process = subprocess.Popen([sys.executable, "-c", code, str(spool_path), spool.hex(), str(artifact),
                json.dumps(main), str(refresh.COHORT_SETTLED_FAILURE_EXIT)], stdout=log, stderr=log, start_new_session=True)
            item = refresh.CohortCollectionProcess("colliers", process, log, artifact, attempt, started,
                                                    OwnedProcessGroup.from_spawn(process))
            process.wait(timeout=15)
            self.assertFalse(refresh._finalize_cohort_collection(root, manifest, item, candidate_only=True))
            self.assertEqual(attempt["rc"], refresh.COHORT_SETTLED_FAILURE_EXIT)
            self.assertEqual(attempt["colliers_partial"]["state"], "sealed_local")
            self.assertEqual(revalidate_source_receipt(root, manifest, 1, "a" * 40, False)["outcome"], "source_error")


if __name__ == "__main__":
    unittest.main()
