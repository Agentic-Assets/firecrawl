import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import cre_partial_page_custody as custody


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
                       "path": attempt["colliers_partial"]["path"], "pages": 1,
                       "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            main = root / "main.json"
            main.write_text(json.dumps({"sources": [{"sourceKey": "colliers", "error": "failed",
                                                     "partialCapture": receipt}]}))
            main.chmod(0o600)
            custody.settle(root, attempt, main, "colliers")
            self.assertEqual(attempt["colliers_partial"]["state"], "sealed_local")


if __name__ == "__main__":
    unittest.main()
