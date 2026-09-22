from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cre_reconcile_series_child import (
    ReconciliationError,
    main,
    validate_reconciliation,
)


class ReconcileSeriesChildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.series = Path(self.temp.name) / "series"
        self.child_dir = self.series / "runs" / "child-1"
        (self.child_dir / "sources").mkdir(parents=True)
        self.artifact = self.child_dir / "sources" / "jll.json"
        self.artifact.write_bytes(b"immutable-listing-artifact")
        self.artifact_sha = hashlib.sha256(self.artifact.read_bytes()).hexdigest()
        self.parent = {
            "schema_version": 1,
            "collector_git_sha": "a" * 40,
            "status": "failed",
            "config": {"sources": ["jll"]},
            "sources": {
                "jll": {
                    "state": "failed_global",
                    "checkpoint_run": "runs/child-1",
                    "attempts": [{"rc": 1}],
                }
            },
        }
        self.child = {
            "schema_version": 2,
            "collector_git_sha": "a" * 40,
            "run_id": "child-1",
            "status": "supported_scope_complete",
            "validation": {"rc": 0, "readback_ok": True},
            "config": {"sources": ["jll"]},
            "sources": {
                "jll": {
                    "state": "ingested",
                    "ingest_recovery": {
                        "outcome": "exact_rollback",
                        "replay_safe": True,
                    },
                    "ingest": {"rc": 0, "finished_at": "2026-09-22T19:00:00Z"},
                    "readback": {
                        "ok": True,
                        "generation_id": "child-1",
                        "expected_staged_unique": 1,
                    },
                    "artifact": {
                        "sha256": self.artifact_sha,
                        "path": "sources/jll.json",
                        "staged_unique": 1,
                    },
                }
            },
        }
        self._write()

    def _write(self) -> None:
        self.series.mkdir(exist_ok=True)
        (self.series / "manifest.json").write_text(json.dumps(self.parent))
        (self.child_dir / "manifest.json").write_text(json.dumps(self.child))

    def _validate(self) -> None:
        validate_reconciliation(
            self.series,
            source="jll",
            expected_sha="a" * 40,
            expected_child_run="child-1",
            expected_artifact_sha256=self.artifact_sha,
        )

    def test_completed_exact_child_is_admitted(self) -> None:
        self._validate()

    def test_rollback_without_completed_ingest_is_refused(self) -> None:
        self.child["sources"]["jll"]["ingest"]["rc"] = None
        self._write()
        with self.assertRaisesRegex(ReconciliationError, "completed live ingest"):
            self._validate()

    def test_wrong_child_binding_is_refused(self) -> None:
        self.parent["sources"]["jll"]["checkpoint_run"] = "runs/other"
        self._write()
        with self.assertRaisesRegex(ReconciliationError, "bound to the expected child"):
            self._validate()

    def test_child_path_traversal_is_refused(self) -> None:
        with self.assertRaisesRegex(ReconciliationError, "one exact directory name"):
            validate_reconciliation(
                self.series,
                source="jll",
                expected_sha="a" * 40,
                expected_child_run="../child-1",
                expected_artifact_sha256=self.artifact_sha,
            )

    def test_mutated_artifact_is_refused(self) -> None:
        self.artifact.write_bytes(b"changed")
        with self.assertRaisesRegex(
            ReconciliationError, "immutable artifact bytes differ"
        ):
            self._validate()

    def test_missing_exact_rollback_is_refused(self) -> None:
        self.child["sources"]["jll"]["ingest_recovery"]["replay_safe"] = False
        self._write()
        with self.assertRaisesRegex(ReconciliationError, "exact rollback evidence"):
            self._validate()

    def test_missing_generation_readback_is_refused(self) -> None:
        self.child["sources"]["jll"]["readback"]["ok"] = False
        self._write()
        with self.assertRaisesRegex(ReconciliationError, "generation readback"):
            self._validate()

    def test_final_validation_failure_is_refused(self) -> None:
        self.child["validation"]["readback_ok"] = False
        self._write()
        with self.assertRaisesRegex(ReconciliationError, "final validation"):
            self._validate()

    def test_non_series_success_status_is_refused(self) -> None:
        self.child["status"] = "additive_scope_complete_coverage_hold"
        self._write()
        with self.assertRaisesRegex(
            ReconciliationError, "expected child has not completed"
        ):
            self._validate()

    def test_apply_records_completion_without_changing_child(self) -> None:
        original_child = (self.child_dir / "manifest.json").read_bytes()
        argv = [
            "cre_reconcile_series_child.py",
            "--series-dir",
            str(self.series),
            "--source",
            "jll",
            "--expected-collector-sha",
            "a" * 40,
            "--expected-child-run",
            "child-1",
            "--expected-artifact-sha256",
            self.artifact_sha,
            "--apply",
        ]
        with (
            patch("sys.argv", argv),
            patch("cre_reconcile_series_child._require_clean_checkout"),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(), 0)
        updated = json.loads((self.series / "manifest.json").read_text())
        self.assertEqual(updated["status"], "failed")
        self.assertEqual(updated["sources"]["jll"]["state"], "complete")
        self.assertEqual(
            updated["sources"]["jll"]["checkpoint_status"],
            "supported_scope_complete",
        )
        self.assertEqual(
            (self.child_dir / "manifest.json").read_bytes(), original_child
        )


if __name__ == "__main__":
    unittest.main()
