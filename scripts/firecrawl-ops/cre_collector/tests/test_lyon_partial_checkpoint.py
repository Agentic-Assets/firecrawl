"""Sealed source replay refuses changed or terminal evidence."""
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from cre_lyon_partial_checkpoint import candidate_data, candidate_structure, replay_settled_source, run_replay_worker, validate_candidate, verify_candidate_artifact, run_identity_probe, load_probe_preimage, admit_search, ingest_subset_context, run_partial_readback, LyonSourceProbeRefusal
from cre_partial_page_custody import prepare, settle
from cre_stop_guarded_tier import StopRefused
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

    def test_candidate_projects_source_identity_without_status_inference(self):
        from cre_gate import count_artifacts

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _, _ = self.case(root)
            source = replay_settled_source(root, manifest, 1, "a" * 40, False)
            candidate = candidate_data(source, manifest["started_at"])
            self.assertEqual(validate_candidate(candidate, source, manifest["started_at"]), source["staged"])
            self.assertEqual(candidate_structure(candidate), source)
            candidate["listings"][0]["id"] = "forged"
            with self.assertRaises(ValueError):
                validate_candidate(candidate, source, manifest["started_at"])
            candidate = candidate_data(source, manifest["started_at"])
            listing = candidate["listings"][0]
            self.assertNotIn("status", listing)
            self.assertNotIn("transactionType", listing)
            self.assertEqual(listing["lyonSearchRecord"], source["records"][0])
            path = root / "candidate.json"
            path.write_text(json.dumps(candidate))
            current, errors, sources, _, rejects = count_artifacts([str(path)], True)
            self.assertEqual(current, {"lyon-stahl": 1})
            self.assertEqual(errors, {"lyon-stahl": "original source pass was partial"})
            self.assertEqual(sources, {"lyon-stahl"})
            self.assertEqual(rejects, 0)

    def test_owned_replay_worker_produces_bound_private_candidate(self):
        import cre_checkpoint_refresh as refresh

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _, _ = self.case(root)
            manifest["config"] = {"additive": True, "mark_missing": False, "status_activation": False,
                                  "transactions": ["sale", "lease"], "max_items": 0, "sources": ["lyon-stahl"]}
            lock_path = root / "lock"
            with patch.object(refresh, "canonical_shared_lock_dir", return_value=lock_path),                  patch.object(refresh, "git_identity", return_value=("a" * 40, False)),                  refresh.SharedLock(lock_path) as owner:
                path = run_replay_worker(root, manifest, owner, lambda *_: None)
                self.assertEqual(path.stat().st_mode & 0o077, 0)
                self.assertEqual(manifest["sources"]["lyon-stahl"]["state"], "candidate")
                self.assertEqual(json.loads(path.read_text())["totalListings"], 1)
                stats = refresh._validate_source_artifact_local(path, "lyon-stahl", manifest["sources"]["lyon-stahl"]["attempts"][0]["started_at"],
                    expected_generation_id=root.name, expected_generation_started_at=manifest["started_at"])
                self.assertIs(stats["lyon_partial"], True)
                self.assertIs(stats["strict_freshness"], False)
                checkpoint = manifest["sources"]["lyon-stahl"]
                checkpoint["artifact"] = {**stats, "path": str(path.relative_to(root))}
                verified = verify_candidate_artifact(path, manifest, current_code_sha="a" * 40, current_git_dirty=False)
                self.assertEqual(len(verified["staged"]), 1)
                import cre_ingest as ingest
                import cre_validate
                preimage = [{"external_id": "example", "listing": None, "source_index": None, "children": {"contacts": [], "documents": [], "images": []}}]
                output = chr(9).join(["staged", "joined", "held", "preimage"]) + chr(10)
                output += chr(9).join(["1", "1", "[]", json.dumps(preimage)]) + chr(10)
                result = SimpleNamespace(returncode=0, stdout=output)
                with patch.object(ingest, "load_db_url", return_value=("synthetic", None)),                      patch.object(ingest, "database_target_fingerprint_from_url", return_value={"value": "b" * 64}),                      patch.object(ingest, "find_psql", return_value="synthetic"),                      patch.object(cre_validate, "_run_psql_script", return_value=result):
                    probe = run_identity_probe(path, manifest, None)
                    bad = SimpleNamespace(returncode=0, stdout=chr(9).join(["staged", "joined", "held", "preimage"]) + chr(10)
                                          + chr(9).join(["1", "1", "[]", "not-json"]) + chr(10))
                    with patch.object(cre_validate, "_run_psql_script", return_value=bad), self.assertRaises(LyonSourceProbeRefusal):
                        run_identity_probe(path, manifest, None)
                    with patch.object(cre_validate, "_run_psql_script", return_value=SimpleNamespace(returncode=1)), self.assertRaises(RuntimeError):
                        run_identity_probe(path, manifest, None)
                self.assertEqual(load_probe_preimage(root, 1, probe), preimage)
                checkpoint["lyon_identity_probe"] = probe
                manifest["scope"] = {}
                report = {"generated_for_artifact": str(path), "summary": {"mode": "live", "torow_errors": 0,
                    "baseline_updated": False, "baseline_update_sources": [], "hold_sources": ["lyon-stahl"],
                    "mark_missing_safe_sources": [], "mark_missing_safe_brokerages": [], "verdict_counts": {}},
                    "per_source": {"lyon-stahl": {"verdict": "first_seen",
                    "reason": "no baseline row; cannot gate (first sight); a source pass also errored this run",
                    "mark_missing_safe": False, "current_active": 1}}}
                import cre_lyon_partial_checkpoint as lyon_checkpoint
                def fake_gate_command(argv, _log, env=None):
                    Path(argv[argv.index("--out") + 1]).write_text(json.dumps(report))
                    return 0
                with patch.object(refresh, "run_command", side_effect=fake_gate_command),                      patch.object(lyon_checkpoint, "run_identity_probe", side_effect=LyonSourceProbeRefusal("bad partition")),                      self.assertRaises(refresh.LyonPartialProbeHold):
                    refresh.gate_source(root, manifest, "lyon-stahl", path, None)
                with patch.object(refresh, "run_command", side_effect=fake_gate_command),                      patch.object(lyon_checkpoint, "run_identity_probe", side_effect=RuntimeError("shared target failure")),                      self.assertRaises(refresh.GlobalStageError):
                    refresh.gate_source(root, manifest, "lyon-stahl", path, None)
                aggregate = copy.deepcopy(report)
                binding = admit_search(path, manifest, report, current_code_sha="a" * 40,
                                       current_git_dirty=False, phase="source")
                self.assertIs(binding["whole_source_coverage"], False)
                self.assertEqual(binding["emitted_count"], 1)
                checkpoint["gate"] = {"current_inventory_proof": binding}
                self.assertEqual(admit_search(path, manifest, aggregate, current_code_sha="a" * 40,
                                              current_git_dirty=False, phase="aggregate"), binding)
                mixed_manifest = copy.deepcopy(manifest)
                mixed_manifest["config"].update(pipeline=True, sources=["lyon-stahl", "colliers"])
                mixed_manifest["sources"]["colliers"] = {"artifact": {"path": "colliers-partial/candidate.json"}}
                mixed_report = copy.deepcopy(aggregate)
                peer_path = str(root / "colliers-partial" / "candidate.json")
                mixed_report["generated_for_artifact"] = [str(path), peer_path]
                peer = {"verdict": "ok_additive_verified_subset", "mark_missing_safe": False,
                        "admission_scope": "colliers_partial_window_v1", "current_inventory_proof": {"source_key": "colliers"}}
                mixed_report["per_source"]["colliers"] = copy.deepcopy(peer)
                admit_search(path, mixed_manifest, mixed_report, current_code_sha="a" * 40,
                             current_git_dirty=False, phase="aggregate")
                self.assertEqual(mixed_report["scope"]["kind"], "collector_registry_verified_subset")
                self.assertEqual(mixed_report["per_source"]["colliers"], peer)
                envelope = {"manifest": manifest, "source_report": report, "aggregate_report": aggregate,
                            "binding": binding, "attempt_id": "e" * 32}
                context = ingest_subset_context(path, envelope, current_code_sha="a" * 40, current_git_dirty=False,
                                                expected_database_target_sha256="b" * 64, attempt_id="e" * 32)
                self.assertEqual(context["staged"], verified["staged"])
                result = SimpleNamespace(returncode=0, stdout=chr(9).join(["jobs", "observed", "mismatches"]) + chr(10)
                                         + chr(9).join(["1", "1", "0"]) + chr(10))
                with patch.object(ingest, "load_db_url", return_value=("synthetic", None)),                      patch.object(ingest, "database_target_fingerprint_from_url", return_value={"value": "b" * 64}),                      patch.object(ingest, "find_psql", return_value="synthetic"),                      patch.object(cre_validate, "_run_psql_script", return_value=result):
                    readback = run_partial_readback(path, envelope, code_sha="a" * 40, target="b" * 64,
                                                    attempt_id="e" * 32, env_file=None)
                self.assertTrue(readback["ok"])
                self.assertTrue(readback["child_collections_verified"])
                claims_path = root / "lyon-stahl-partial" / "claims.json"
                claims_path.write_text(json.dumps(envelope))
                claims_path.chmod(0o600)
                dry_dir = root / "dry"
                shim = "import cre_checkpoint_refresh as c; c.git_identity=lambda: (" + repr("a" * 40) + ",False); import cre_ingest; cre_ingest.main()"
                command = [sys.executable, "-c", shim, "--in", str(path), "--dry-run", "--lyon-partial-claims", str(claims_path),
                           "--require-strict-freshness", "--expected-db-target-sha256", "b" * 64, "--checkpoint-ingest-attempt", "e" * 32,
                           "--keep-artifacts", str(dry_dir)]
                cli = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
                self.assertEqual(cli.returncode, 0, cli.stderr)
                sql_path = dry_dir / "ingest.sql"
                self.assertEqual(sql_path.stat().st_mode & 0o077, 0)
                self.assertIn("lyonSearchObservations", sql_path.read_text())
                if hasattr(self, "capture_sql"):
                    self.capture_sql.append((sql_path.read_text(), context))
                no_claim = subprocess.run([sys.executable, "cre_ingest.py", "--in", str(path), "--dry-run"],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
                self.assertNotEqual(no_claim.returncode, 0)
                self.assertIn("without guarded checkpoint admission", no_claim.stderr)
                changed = copy.deepcopy(aggregate)
                changed["per_source"]["lyon-stahl"]["mark_missing_safe"] = True
                with self.assertRaises(ValueError):
                    admit_search(path, manifest, changed, current_code_sha="a" * 40,
                                 current_git_dirty=False, phase="aggregate")
                probe_path = root / probe["preimage_path"]
                probe_path.write_bytes(probe_path.read_bytes() + b" ")
                with self.assertRaises(StopRefused):
                    load_probe_preimage(root, 1, probe)
                path.write_bytes(path.read_bytes() + b" ")
                with self.assertRaises(StopRefused):
                    verify_candidate_artifact(path, manifest, current_code_sha="a" * 40, current_git_dirty=False)
                with self.assertRaises(ValueError):
                    run_replay_worker(root, manifest, owner, lambda *_: None)

    def test_preseal_source_hold_and_target_change_fence(self):
        import cre_checkpoint_refresh as refresh
        import cre_refresh_pipeline as pipeline

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _, _ = self.case(root)
            candidate = root / "candidate.json"
            candidate.write_text("{}")
            manifest["sources"]["lyon-stahl"]["artifact"] = {"path": "candidate.json", "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()}
            with patch.object(refresh, "git_identity", return_value=("a" * 40, False)),                  patch.object(refresh, "save_manifest"), patch.object(pipeline, "require_pipeline_owner"),                  patch.object(refresh, "gate_source", side_effect=refresh.LyonPartialProbeHold("invalid source")):
                self.assertFalse(pipeline.preseal_source_gate(root, manifest, "lyon-stahl", None))
                self.assertEqual(manifest["sources"]["lyon-stahl"]["partial_probe_hold"], "source_identity_partition_invalid")
                def change_target(*_args):
                    manifest["preflight"]["database_target"]["value"] = "c" * 64
                    raise refresh.LyonPartialProbeHold("invalid source")
                with patch.object(refresh, "gate_source", side_effect=change_target), self.assertRaises(refresh.GlobalStageError):
                    pipeline.preseal_source_gate(root, manifest, "lyon-stahl", None)

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
