"""Lyon bounded acquisition requires source-owned custody."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cre_checkpoint_refresh as refresh
import cre_partial_page_custody as custody


class LyonPartialCustodyTests(unittest.TestCase):
    def test_bounded_worker_receives_only_its_private_attempt_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            custody.prepare(root, 2, "lyon-stahl")
            inherited = {"CRE_LYON_STAHL_PARTIAL_PATH": "/unsafe", "CRE_CBRE_PARTIAL_PATH": "/unsafe"}
            env, _ = refresh.fresh_source_env("lyon-stahl", root, base=inherited, attempt_number=2,
                                               lyon_bounded_search=True)
            self.assertEqual(env["CRE_LYON_STAHL_PARTIAL_PATH"], str(custody.expected_path(root, 2, "lyon-stahl")))
            self.assertEqual(env["CRE_LYON_STAHL_PARTIAL_ATTEMPT"], "2")
            self.assertNotIn("CRE_CBRE_PARTIAL_PATH", env)
            serial, _ = refresh.fresh_source_env("lyon-stahl", root, base=inherited, attempt_number=2)
            self.assertNotIn("CRE_LYON_STAHL_PARTIAL_PATH", serial)

    def test_worker_fails_before_collection_when_custody_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(refresh, "run_command") as run:
                result = refresh.run_cohort_collection_worker(
                    "lyon-stahl", root / "output.json", run_dir=root, attempt_log=root / "attempt.log",
                    attempt_number=1, generation_started_at="2026-11-05T00:00:00Z",
                    page_cap=10, concurrency=1, transactions=["sale", "lease"])
            self.assertNotEqual(result, 0)
            run.assert_not_called()
            self.assertIn("no source request dispatched", (root / "attempt.log").read_text())
