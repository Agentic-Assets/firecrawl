"""Offline settled denial and unknown-cleanup routing for Kidder."""
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cre_checkpoint_refresh as refresh
import cre_partial_page_custody as custody


class KidderTerminalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def manifest(self, sources=("kidder-mathews",)):
        return refresh.new_manifest(self.root, git_sha="synthetic", git_dirty=False,
                                    sources=sources, page_cap=400, concurrency=2)

    def test_serial_stops_retry_for_each_settled_response(self):
        for status, raw in refresh.KIDDER_TERMINAL_EXITS.items():
            with self.subTest(status=status):
                manifest = self.manifest()
                with patch.object(refresh, "run_command", return_value=raw) as command:
                    for _ in range(2):
                        self.assertIsNone(refresh.collect_source(self.root, manifest, "kidder-mathews",
                            transactions=("sale", "lease"), page_cap=400, concurrency=2, attempts_this_run=3))
                checkpoint = manifest["sources"]["kidder-mathews"]
                self.assertEqual(command.call_count, 1)
                self.assertEqual(checkpoint["terminal_collection_failure"], {
                    "code": refresh.KIDDER_TERMINAL_CODE, "http_status": status, "attempt_number": 1})
                self.assertTrue(refresh.has_terminal_collection_failure("kidder-mathews", checkpoint))

    def test_terminal_403_archives_private_evidence_and_stops_retry(self):
        manifest = self.manifest()
        payload = b'{"sources":[{"sourceKey":"kidder-mathews","retainedObservations":[{"coverage":"partial_unknown"}]}]}'

        def collect(argv, *_args, **_kwargs):
            output = next(arg.removeprefix("--out=") for arg in argv if arg.startswith("--out="))
            Path(output).write_bytes(payload)
            return refresh.KIDDER_TERMINAL_EXITS[403]

        with patch.object(refresh, "run_command", side_effect=collect) as command:
            self.assertIsNone(refresh.collect_source(self.root, manifest, "kidder-mathews",
                transactions=("sale", "lease"), page_cap=400, concurrency=2, attempts_this_run=3))
        checkpoint = manifest["sources"]["kidder-mathews"]
        attempt = checkpoint["attempts"][0]
        archived = self.root / attempt["rejected_artifact"]
        self.assertEqual(command.call_count, 1)
        self.assertEqual(archived.read_bytes(), payload)
        self.assertEqual(archived.stat().st_mode & 0o777, 0o600)
        self.assertEqual(attempt["rejected_artifact_sha256"], refresh.sha256_file(archived))
        self.assertEqual(checkpoint["terminal_collection_failure"]["http_status"], 403)
        self.assertEqual(checkpoint["state"], "collect_failed")
        self.assertIsNone(checkpoint.get("artifact"))

    def test_kidder_attempt_spool_settles_only_with_bound_receipt(self):
        source = "kidder-mathews"
        state = custody.prepare(self.root, 1, source)
        attempt = {"number": 1, f"{source}_partial": state}
        path = custody.expected_path(self.root, 1, source)
        payload = (json.dumps({"schema": f"{source}-partial-pages/v1", "sourceKey": source,
                               "generation": self.root.name, "attempt": 1}) + "\n" +
                   json.dumps({"kind": "kidder_team_observation"}) + "\n").encode()
        path.write_bytes(payload)
        path.chmod(0o600)
        receipt = {"state": "sealed", "sourceKey": source, "generationId": self.root.name,
                   "attempt": 1, "path": state["path"], "coverage": "unverified",
                   "outcome": "source_error", "observationRecords": 1, "bytes": len(payload),
                   "sha256": hashlib.sha256(payload).hexdigest()}
        main = self.root / "main.json"
        main.write_text(json.dumps({"sources": [{"sourceKey": source, "error": "HTTP 403",
                                                 "partialCapture": receipt}]}))
        main.chmod(0o600)
        custody.settle(self.root, attempt, main, source)
        self.assertEqual(state["state"], "sealed_local")
        self.assertEqual(state["coverage"], "unverified")
        state["state"] = "pending"
        attempt.update(finished_at=None, rc=None)
        custody.reconcile(self.root, {"sources": {source: {"attempts": [attempt]}}}, source)
        self.assertEqual(state["state"], "unverified")
        self.assertTrue(path.exists())

    def test_unknown_cleanup_keeps_global_fence(self):
        manifest = self.manifest()
        with patch.object(refresh, "run_command", return_value=refresh.KIDDER_CLEANUP_UNKNOWN_EXIT) as command:
            with self.assertRaises(refresh.ProcessCleanupUnknown):
                refresh.collect_source(self.root, manifest, "kidder-mathews", transactions=("sale", "lease"),
                                       page_cap=400, concurrency=2, attempts_this_run=3)
        self.assertEqual(command.call_count, 1)
        checkpoint = manifest["sources"]["kidder-mathews"]
        self.assertEqual(checkpoint["state"], "collect_infrastructure_failed")
        self.assertFalse(refresh.has_terminal_collection_failure("kidder-mathews", checkpoint))
        self.assertNotIn(refresh.COHORT_KIDDER_CLEANUP_UNKNOWN_EXIT, refresh.COHORT_SETTLED_EXIT_CODES)

    def test_mixed_cohort_unknown_cleanup_keeps_prior_healthy_checkpoint(self):
        manifest = self.manifest(("kidder-mathews", "hanley"))
        healthy = manifest["sources"]["hanley"]
        healthy.update(state="validated", artifact={"path": "hanley.json", "sha256": "peer"})
        kidder = manifest["sources"]["kidder-mathews"]
        attempt = {"number": 1, "started_at": manifest["started_at"], "finished_at": None,
                   "temporary_artifact": "kidder.tmp"}
        kidder["attempts"].append(attempt)
        kidder["state"] = "collecting"

        class Process:
            def poll(self):
                return refresh.COHORT_KIDDER_CLEANUP_UNKNOWN_EXIT

        class Settled:
            gone = True

        item = refresh.CohortCollectionProcess(source="kidder-mathews", process=Process(),
            log_handle=io.StringIO(), tmp_artifact=self.root / "kidder.tmp", attempt=attempt,
            attempt_started_at=manifest["started_at"], owned_group=Settled())
        with self.assertRaises(refresh.ProcessCleanupUnknown):
            refresh._finalize_cohort_collection(self.root, manifest, item, candidate_only=True)
        self.assertIsNone(attempt["finished_at"])
        self.assertFalse(refresh.has_terminal_collection_failure("kidder-mathews", kidder))
        self.assertEqual(healthy["state"], "validated")
        self.assertEqual(healthy["artifact"]["sha256"], "peer")

    def test_worker_preserves_only_kidder_specific_settled_and_unknown_codes(self):
        for status, raw in refresh.KIDDER_TERMINAL_EXITS.items():
            with self.subTest(status=status), patch.object(refresh, "run_command", return_value=raw):
                args = dict(run_dir=self.root, attempt_log=self.root / "attempt.log", attempt_number=1,
                            generation_started_at="2026-10-19T14:47:00+00:00", page_cap=400,
                            concurrency=2, transactions=("sale", "lease"))
                self.assertEqual(refresh.run_cohort_collection_worker("kidder-mathews", self.root / "out", **args),
                                 refresh.COHORT_SETTLED_KIDDER_EXITS[status])
                self.assertEqual(refresh.run_cohort_collection_worker("cbre", self.root / "out", **args), raw)
        with patch.object(refresh, "run_command", return_value=refresh.KIDDER_CLEANUP_UNKNOWN_EXIT):
            self.assertEqual(refresh.run_cohort_collection_worker("kidder-mathews", self.root / "out",
                run_dir=self.root, attempt_log=self.root / "attempt.log", attempt_number=1,
                generation_started_at="2026-10-19T14:47:00+00:00", page_cap=400, concurrency=2,
                transactions=("sale", "lease")), refresh.COHORT_KIDDER_CLEANUP_UNKNOWN_EXIT)

    def test_cohort_marks_settled_source_terminal_and_keeps_unknown_unsettled(self):
        for status, exit_code in refresh.COHORT_SETTLED_KIDDER_EXITS.items():
            with self.subTest(status=status):
                manifest = self.manifest()
                checkpoint = manifest["sources"]["kidder-mathews"]
                attempt = {"number": 1, "started_at": manifest["started_at"], "finished_at": None,
                           "temporary_artifact": "kidder.tmp"}
                checkpoint["attempts"].append(attempt)
                checkpoint["state"] = "collecting"
                class Process:
                    returncode = exit_code
                    def poll(self): return self.returncode
                class Settled:
                    gone = True
                item = refresh.CohortCollectionProcess(source="kidder-mathews", process=Process(),
                    log_handle=io.StringIO(), tmp_artifact=self.root / "kidder.tmp", attempt=attempt,
                    attempt_started_at=manifest["started_at"], owned_group=Settled())
                self.assertFalse(refresh._finalize_cohort_collection(self.root, manifest, item, candidate_only=True))
                self.assertEqual(checkpoint["terminal_collection_failure"]["http_status"], status)
                self.assertTrue(refresh.has_terminal_collection_failure("kidder-mathews", checkpoint))
        self.assertFalse(refresh.has_terminal_collection_failure("cbre", {
            "terminal_collection_failure": {"code": refresh.KIDDER_TERMINAL_CODE, "http_status": 403}}))
        self.assertFalse(refresh.has_terminal_collection_failure("kidder-mathews", {
            "terminal_collection_failure": {"code": refresh.KIDDER_TERMINAL_CODE, "http_status": 500}}))

    def test_cohort_denial_stops_retry_but_healthy_peer_continues(self):
        manifest = self.manifest(("kidder-mathews", "hanley"))
        started = []
        original = refresh._finalize_cohort_collection

        def start(_run, _manifest, source, **_kwargs):
            started.append(source)
            checkpoint = manifest["sources"][source]
            attempt = {"number": len(checkpoint["attempts"]) + 1, "started_at": manifest["started_at"],
                       "finished_at": None, "temporary_artifact": f"{source}.tmp"}
            checkpoint["attempts"].append(attempt)
            checkpoint["state"] = "collecting"
            class Process:
                returncode = refresh.COHORT_SETTLED_KIDDER_EXITS[403] if source == "kidder-mathews" else 0
                def poll(self): return self.returncode
            class Settled:
                gone = True
            return refresh.CohortCollectionProcess(source=source, process=Process(), log_handle=io.StringIO(),
                tmp_artifact=self.root / f"{source}.tmp", attempt=attempt,
                attempt_started_at=manifest["started_at"], owned_group=Settled())

        def finalize(run, current, item):
            if item.source == "kidder-mathews":
                return original(run, current, item)
            current["sources"][item.source]["state"] = "validated"
            current["sources"][item.source]["artifact"] = {"path": "hanley.json"}
            item.log_handle.close()
            return True

        with patch.object(refresh, "_start_cohort_collection", side_effect=start), \
             patch.object(refresh, "_finalize_cohort_collection", side_effect=finalize), \
             patch.object(refresh, "_manifest_checkpoint_artifact_valid", side_effect=lambda _r, m, source:
                          (self.root / "hanley.json", {}) if m["sources"][source].get("artifact") else None), \
             patch.object(refresh, "_terminate_cohort_processes"), patch.object(refresh.time, "sleep"):
            self.assertEqual(refresh.prepare_sources_cohort(self.root, manifest, ("kidder-mathews", "hanley"),
                page_cap=400, concurrency=2, attempts_this_run=3, env_file=None, source_workers=2), ["kidder-mathews"])
        self.assertEqual(started, ["kidder-mathews", "hanley"])
        self.assertEqual(manifest["sources"]["hanley"]["state"], "validated")
        self.assertEqual(manifest["sources"]["kidder-mathews"]["terminal_collection_failure"]["http_status"], 403)

    def test_source_specific_settlement_cannot_be_claimed_by_peer(self):
        for status, rc in refresh.COHORT_SETTLED_KIDDER_EXITS.items():
            with self.subTest(status=status):
                self.assertTrue(refresh.cohort_exit_proves_settlement("kidder-mathews", rc))
                self.assertFalse(refresh.cohort_exit_proves_settlement("cbre", rc))
        self.assertFalse(refresh.cohort_exit_proves_settlement("kidder-mathews", refresh.COHORT_KIDDER_CLEANUP_UNKNOWN_EXIT))
        self.assertFalse(refresh.cohort_exit_proves_settlement("kidder-mathews", 999))
        self.assertTrue(refresh.cohort_exit_proves_settlement("cbre", refresh.COHORT_SETTLED_FAILURE_EXIT))

    def test_foreign_source_specific_exit_cannot_finalize(self):
        manifest = self.manifest(("cbre",))
        attempt = {"number": 1, "started_at": manifest["started_at"], "finished_at": None,
                   "temporary_artifact": "cbre.tmp"}
        manifest["sources"]["cbre"]["attempts"].append(attempt)
        manifest["sources"]["cbre"]["state"] = "collecting"
        class Process:
            returncode = refresh.COHORT_SETTLED_KIDDER_EXITS[403]
            def poll(self): return self.returncode
        class Settled:
            gone = True
        item = refresh.CohortCollectionProcess(source="cbre", process=Process(), log_handle=io.StringIO(),
            tmp_artifact=self.root / "cbre.tmp", attempt=attempt, attempt_started_at=manifest["started_at"],
            owned_group=Settled())
        with self.assertRaises(refresh.ProcessCleanupUnknown):
            refresh._finalize_cohort_collection(self.root, manifest, item, candidate_only=True)
        self.assertIsNone(attempt["finished_at"])

    def test_worker_main_does_not_normalize_kidder_cleanup_unknown(self):
        for code in [*refresh.COHORT_SETTLED_KIDDER_EXITS.values(), refresh.COHORT_KIDDER_CLEANUP_UNKNOWN_EXIT]:
            with self.subTest(code=code), patch.object(refresh, "run_cohort_collection_worker", return_value=code):
                argv = refresh.build_cohort_collect_worker_argv("kidder-mathews", self.root / "out",
                    run_dir=self.root, attempt_log=self.root / "worker.log", attempt_number=1,
                    generation_started_at="2026-10-19T14:47:00+00:00", page_cap=400, concurrency=2,
                    transactions=("sale", "lease"))
                self.assertEqual(refresh.main(argv[2:]), code)
                argv = refresh.build_cohort_collect_worker_argv("cbre", self.root / "out",
                    run_dir=self.root, attempt_log=self.root / "worker.log", attempt_number=1,
                    generation_started_at="2026-10-19T14:47:00+00:00", page_cap=400, concurrency=2,
                    transactions=("sale", "lease"))
                self.assertEqual(refresh.main(argv[2:]), refresh.COHORT_SETTLED_FAILURE_EXIT)


if __name__ == "__main__":
    unittest.main()
