import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cre_checkpoint_refresh as refresh


class MatthewsCleanupUnknownTests(unittest.TestCase):
    def test_serial_cleanup_unknown_is_not_source_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = refresh.new_manifest(root, git_sha="fixture", git_dirty=False,
                                            sources=("matthews",), page_cap=0, concurrency=2)
            with patch.object(refresh, "run_command", return_value=refresh.MATTHEWS_REDIRECT_CLEANUP_UNKNOWN_EXIT):
                with self.assertRaises(refresh.ProcessCleanupUnknown):
                    refresh.collect_source(root, manifest, "matthews", transactions=("sale", "lease"),
                                           page_cap=0, concurrency=2, attempts_this_run=3)
            state = manifest["sources"]["matthews"]
            self.assertEqual(state["state"], "collect_infrastructure_failed")
            self.assertEqual(len(state["attempts"]), 1)

    def test_cohort_worker_unknown_is_not_a_settled_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = dict(run_dir=root, attempt_log=root / "attempt.log", attempt_number=1,
                        generation_started_at="2026-01-01T00:00:00+00:00", page_cap=0,
                        concurrency=2, transactions=("sale", "lease"))
            with patch.object(refresh, "run_command", return_value=refresh.MATTHEWS_REDIRECT_CLEANUP_UNKNOWN_EXIT):
                rc = refresh.run_cohort_collection_worker("matthews", root / "out", **args)
            self.assertEqual(rc, refresh.COHORT_MATTHEWS_REDIRECT_CLEANUP_UNKNOWN_EXIT)
            self.assertNotIn(rc, refresh.COHORT_SETTLED_EXIT_CODES)
            argv = refresh.build_cohort_collect_worker_argv("matthews", root / "out", **args)
            with patch.object(refresh, "run_cohort_collection_worker", return_value=rc):
                self.assertEqual(refresh.main(argv[2:]), rc)

    def test_cohort_finalizer_preserves_unknown_fence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = refresh.new_manifest(root, git_sha="fixture", git_dirty=False,
                                            sources=("matthews",), page_cap=0, concurrency=2)
            attempt = {"number": 1, "started_at": manifest["started_at"], "finished_at": None,
                       "temporary_artifact": "matthews.tmp"}
            manifest["sources"]["matthews"]["attempts"].append(attempt)
            manifest["sources"]["matthews"]["state"] = "collecting"

            class Process:
                returncode = refresh.COHORT_MATTHEWS_REDIRECT_CLEANUP_UNKNOWN_EXIT

                def poll(self):
                    return self.returncode

            class Settled:
                gone = True

            item = refresh.CohortCollectionProcess(source="matthews", process=Process(),
                log_handle=io.StringIO(), tmp_artifact=root / "matthews.tmp", attempt=attempt,
                attempt_started_at=manifest["started_at"], owned_group=Settled())
            with self.assertRaises(refresh.ProcessCleanupUnknown):
                refresh._finalize_cohort_collection(root, manifest, item, candidate_only=True)
            self.assertIsNone(attempt["finished_at"])
