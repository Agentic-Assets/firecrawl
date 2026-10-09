from __future__ import annotations

import copy
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cre_checkpoint_series import series_config
from cre_reconcile_series_child import (
    ReconciliationError,
    _require_clean_checkout,
    main,
    validate_reconciliation,
)
from cre_resource_recovery import SeriesOwnershipLock

# Parent config shape written by the pinned 4c4cfafef series runner: it
# predates the resource_recovery block and the parent database_target binding.
PINNED_PARENT_CONFIG = {
    "sources": ["jll"],
    "transactions": ["sale", "lease"],
    "page_cap": 400,
    "concurrency": 3,
    "source_workers": 1,
    "attempts_per_source": 3,
    "max_resume_age_hours": 24.0,
    "host_cpu_guard": {
        "max_host_cpu_percent": 80.0,
        "sustain_seconds": 30.0,
        "sample_seconds": 5.0,
    },
    "nice": 10,
    "continue_source_local_failures": True,
}
# Child config the series passes to cre_checkpoint_refresh.py for that parent.
SERIES_CHILD_CONFIG = {
    "sources": ["jll"],
    "transactions": ["sale", "lease"],
    "max_items": 0,
    "page_cap": 400,
    "concurrency": 3,
    "source_workers": 1,
    "host_cpu_guard": {
        "max_host_cpu_percent": 80.0,
        "sustain_seconds": 30.0,
        "sample_seconds": 5.0,
        "action": "interrupt_and_checkpoint",
        "telemetry_failure_action": "interrupt_and_checkpoint",
    },
    "additive": True,
    "status_activation": False,
    "mark_missing": False,
    "admit_baseline_hold_additively": False,
}
DATABASE_TARGET = {"algorithm": "sha256", "fingerprint": "f" * 64}


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
            "config": copy.deepcopy(PINNED_PARENT_CONFIG),
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
            "started_at": "2026-09-22T10:00:00+00:00",
            "finished_at": "2026-09-22T19:00:00+00:00",
            "validation": {"rc": 0, "readback_ok": True},
            "config": copy.deepcopy(SERIES_CHILD_CONFIG),
            "preflight": {"database_target": dict(DATABASE_TARGET)},
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

    def _main(self, *, apply: bool, acknowledge_age: bool = False) -> tuple[int, str]:
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
        ]
        if apply:
            argv.append("--apply")
        if acknowledge_age:
            argv.append("--acknowledge-resume-age-override")
        stderr = io.StringIO()
        with (
            patch("sys.argv", argv),
            patch("cre_reconcile_series_child._require_clean_checkout"),
            redirect_stdout(io.StringIO()),
            redirect_stderr(stderr),
        ):
            rc = main()
        return rc, stderr.getvalue()

    def test_current_series_parent_config_is_admitted(self) -> None:
        self.parent["config"] = series_config(
            sources=["jll"],
            page_cap=400,
            concurrency=3,
            attempts_per_source=3,
            max_resume_age_hours=24.0,
            max_host_cpu_percent=80.0,
            cpu_sustain_seconds=30.0,
            cpu_sample_seconds=5.0,
            nice=10,
        )
        self.parent["database_target"] = dict(DATABASE_TARGET)
        self._write()
        self._validate()

    def test_child_config_drift_from_series_is_refused(self) -> None:
        for key, value in (
            ("page_cap", 60),
            ("admit_baseline_hold_additively", True),
            ("mark_missing", True),
        ):
            with self.subTest(key=key):
                self.child["config"] = copy.deepcopy(SERIES_CHILD_CONFIG)
                self.child["config"][key] = value
                self._write()
                with self.assertRaisesRegex(
                    ReconciliationError, "child configuration differs"
                ):
                    self._validate()

    def test_child_database_target_differing_from_series_is_refused(self) -> None:
        self.parent["database_target"] = dict(DATABASE_TARGET)
        self.child["preflight"]["database_target"] = {
            "algorithm": "sha256",
            "fingerprint": "0" * 64,
        }
        self._write()
        with self.assertRaisesRegex(ReconciliationError, "database target differs"):
            self._validate()

    def test_malformed_parent_attempt_is_refused_cleanly(self) -> None:
        self.parent["sources"]["jll"]["attempts"] = ["not-an-object"]
        self._write()
        with self.assertRaisesRegex(ReconciliationError, "failed child attempt"):
            self._validate()

    def test_malformed_parent_config_is_refused_cleanly(self) -> None:
        self.parent["config"] = ["jll"]
        self._write()
        with self.assertRaisesRegex(
            ReconciliationError, "parent manifest is malformed"
        ):
            self._validate()

    def _refused(self, pattern: str) -> None:
        self._write()
        with self.assertRaisesRegex(ReconciliationError, pattern):
            self._validate()

    def test_artifact_path_escape_is_refused(self) -> None:
        outside = Path(self.temp.name) / "outside.json"
        outside.write_bytes(b"immutable-listing-artifact")
        for label, path in (
            ("absolute", str(outside)),
            ("parent traversal", "../../../outside.json"),
            ("nested", "sources/../jll.json"),
            ("empty", ""),
        ):
            with self.subTest(label=label):
                self.child["sources"]["jll"]["artifact"]["path"] = path
                self._refused("artifact path escapes the child")

    def test_readback_count_differing_from_artifact_is_refused(self) -> None:
        self.child["sources"]["jll"]["readback"]["expected_staged_unique"] = 2
        self._refused("readback count differs")

    def test_parent_status_and_source_state_must_be_failed(self) -> None:
        for status in ("running", "complete", None):
            with self.subTest(status=status):
                self.parent["status"] = status
                self._refused("parent must be failed")
        self.parent["status"] = "failed"
        for state in ("complete", "pending", None):
            with self.subTest(state=state):
                self.parent["sources"]["jll"]["state"] = state
                self._refused("not failed_global")

    def test_last_attempt_with_zero_rc_is_refused(self) -> None:
        for attempts in ([{"rc": 0}], [{"rc": 1}, {"rc": 0}], [{"rc": None}], []):
            with self.subTest(attempts=attempts):
                self.parent["sources"]["jll"]["attempts"] = attempts
                self._refused("failed child attempt")

    def test_sha_and_schema_mismatches_are_refused(self) -> None:
        cases = (
            (self.parent, "collector_git_sha", "b" * 40, "parent schema or collector"),
            (self.parent, "schema_version", 2, "parent schema or collector"),
            (self.child, "collector_git_sha", "b" * 40, "child schema or collector"),
            (self.child, "schema_version", 1, "child schema or collector"),
        )
        for manifest, key, value, pattern in cases:
            with self.subTest(key=key, pattern=pattern):
                original = manifest[key]
                manifest[key] = value
                self._refused(pattern)
                manifest[key] = original

    def test_malformed_child_sub_objects_are_refused_cleanly(self) -> None:
        source = ("sources", "jll")
        cases = (
            ("validation", (), "validation"),
            ("sources", (), "sources"),
            ("ingest_recovery", source, "ingest_recovery"),
            ("ingest", source, "ingest"),
            ("readback", source, "readback"),
            ("artifact", source, "artifact"),
        )
        for key, parents, name in cases:
            for bad in (["x"], "x", 1, True, []):
                with self.subTest(key=key, bad=bad):
                    node = self.child
                    for parent_key in parents:
                        node = node[parent_key]
                    original = node[key]
                    node[key] = bad
                    self._refused(f"child {name} is malformed")
                    node[key] = original

    def test_resume_age_guard(self) -> None:
        self.child["finished_at"] = "2026-09-23T10:00:01+00:00"
        self._write()
        with self.assertRaisesRegex(ReconciliationError, "exceeds the series"):
            self._validate()
        validate_reconciliation(
            self.series,
            source="jll",
            expected_sha="a" * 40,
            expected_child_run="child-1",
            expected_artifact_sha256=self.artifact_sha,
            acknowledge_resume_age_override=True,
        )
        # Exactly at the limit is admitted.
        self.child["finished_at"] = "2026-09-23T10:00:00Z"
        self._write()
        self._validate()

    def test_resume_age_guard_fails_closed_on_bad_timestamps(self) -> None:
        for key, value in (
            ("started_at", None),
            ("finished_at", None),
            ("finished_at", "not-a-time"),
            ("finished_at", "2026-09-22T19:00:00"),
            ("finished_at", "2026-09-22T09:00:00+00:00"),
        ):
            with self.subTest(key=key, value=value):
                original = self.child[key]
                self.child[key] = value
                self._refused("child .*(missing|invalid|timezone|inconsistent)")
                self.child[key] = original

    def test_malformed_parent_resume_age_limit_is_refused(self) -> None:
        for bad in (0, -1, True, "24", float("inf")):
            with self.subTest(bad=bad):
                self.parent["config"]["max_resume_age_hours"] = bad
                self._write()
                with self.assertRaises(ReconciliationError):
                    self._validate()

    def test_cli_requires_acknowledgement_for_over_age_child(self) -> None:
        self.child["finished_at"] = "2026-09-24T10:00:00+00:00"
        self._write()
        rc, stderr = self._main(apply=True)
        self.assertEqual(rc, 1)
        self.assertIn("--acknowledge-resume-age-override", stderr)
        self.assertEqual(
            json.loads((self.series / "manifest.json").read_text())["sources"]["jll"][
                "state"
            ],
            "failed_global",
        )
        rc, _stderr = self._main(apply=True, acknowledge_age=True)
        self.assertEqual(rc, 0)
        recon = json.loads((self.series / "manifest.json").read_text())["sources"][
            "jll"
        ]["reconciliation"]
        self.assertIs(recon["resume_age_override_acknowledged"], True)

    def test_apply_preserves_previous_state_in_reconciliation(self) -> None:
        checkpoint = self.parent["sources"]["jll"]
        checkpoint["error"] = "ingest interrupted"
        checkpoint["checkpoint_status"] = "ingest_recovery_required"
        self._write()
        rc, _stderr = self._main(apply=True)
        self.assertEqual(rc, 0)
        updated = json.loads((self.series / "manifest.json").read_text())["sources"][
            "jll"
        ]
        self.assertEqual(updated["state"], "complete")
        self.assertIsNone(updated["error"])
        recon = updated["reconciliation"]
        self.assertEqual(recon["previous_state"], "failed_global")
        self.assertEqual(recon["previous_error"], "ingest interrupted")
        self.assertEqual(
            recon["previous_checkpoint_status"], "ingest_recovery_required"
        )
        self.assertIs(recon["resume_age_override_acknowledged"], False)

    def test_dry_run_leaves_parent_failed_and_unchanged(self) -> None:
        original_parent = (self.series / "manifest.json").read_bytes()
        rc, _stderr = self._main(apply=False)
        self.assertEqual(rc, 0)
        self.assertEqual((self.series / "manifest.json").read_bytes(), original_parent)

    def test_apply_refuses_while_series_lock_is_held(self) -> None:
        original_parent = (self.series / "manifest.json").read_bytes()
        with SeriesOwnershipLock(self.series / ".series.lock"):
            rc, stderr = self._main(apply=True)
        self.assertEqual(rc, 1)
        self.assertIn("owned by another foreground process", stderr)
        self.assertEqual((self.series / "manifest.json").read_bytes(), original_parent)

    def test_apply_refuses_parent_changed_after_review(self) -> None:
        def mutate_then_lock(lock: SeriesOwnershipLock) -> SeriesOwnershipLock:
            self.parent["sources"]["jll"]["error"] = "changed by another writer"
            self._write()
            SeriesOwnershipLock.acquire(lock)
            return lock

        with patch.object(SeriesOwnershipLock, "__enter__", mutate_then_lock):
            rc, stderr = self._main(apply=True)
        self.assertEqual(rc, 1)
        self.assertIn("manifest changed during review", stderr)
        updated = json.loads((self.series / "manifest.json").read_text())
        self.assertEqual(updated["sources"]["jll"]["state"], "failed_global")

    def test_apply_records_completion_without_changing_child(self) -> None:
        original_child = (self.child_dir / "manifest.json").read_bytes()
        rc, _stderr = self._main(apply=True)
        self.assertEqual(rc, 0)
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


class RequireCleanCheckoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "collector"
        self.repo.mkdir()
        self.series = self.repo / "out" / "checkpoint-series" / "series-1"
        self.series.mkdir(parents=True)

        def git(*args: str) -> str:
            return subprocess.check_output(
                ["git", "-C", str(self.repo), *args], text=True
            ).strip()

        git("init", "-q")
        (self.repo / ".gitignore").write_text("out/\n")
        (self.repo / "tracked.txt").write_text("pinned\n")
        git("add", ".")
        git(
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            "pinned",
        )
        self.sha = git("rev-parse", "HEAD")

    def test_clean_pinned_checkout_is_admitted(self) -> None:
        _require_clean_checkout(self.series, self.sha)

    def test_different_sha_is_refused(self) -> None:
        with self.assertRaisesRegex(ReconciliationError, "SHA or cleanliness"):
            _require_clean_checkout(self.series, "0" * 40)

    def test_dirty_checkout_is_refused(self) -> None:
        (self.repo / "tracked.txt").write_text("edited\n")
        with self.assertRaisesRegex(ReconciliationError, "SHA or cleanliness"):
            _require_clean_checkout(self.series, self.sha)

    def test_series_outside_checkpoint_layout_is_refused(self) -> None:
        elsewhere = self.repo / "series-1"
        elsewhere.mkdir()
        with self.assertRaisesRegex(ReconciliationError, "checkpoint layout"):
            _require_clean_checkout(elsewhere, self.sha)


if __name__ == "__main__":
    unittest.main()
