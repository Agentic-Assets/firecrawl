"""Bind partial projection to exact settled source artifact and clocks."""
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC
from pathlib import Path
from unittest.mock import patch

from matthews_partial.checkpoint import (
    admit_gate,
    derive,
    gate_projection,
    prepare_candidate,
    recheck_candidate,
)
from tests.test_matthews_partial_fill import valid


def artifact():
    held = {"schema": "matthews-detail-redirect-observation/v1", "sourceKey": "matthews", "outcome": "held_temporary_redirect", "httpStatus": 307, "requestedTransaction": "sale", "requestedUrl": "https://www.matthews.com/properties/redirect"}
    completed = valid(); completed["observation"]["transactionType"] = "Sale"
    return {"runMeta": {"mode": "full", "transactions": ["sale", "lease"], "maxItemsPerSource": None,
        "startedAt": "2026-11-06T23:59:30Z", "finishedAt": "2026-11-07T00:01:30Z", "freshness": {
        "generationId": "fixture", "generationStartedAt": "2026-11-06T23:58:00Z", "requireFreshDetails": True}},
        "sources": [{"sourceKey": "matthews", "supported": True, "transaction": "sale",
            "error": "Error: Matthews strict sale partial: 1 temporary redirect(s) held; 0 detail parse or identity failure(s) held",
            "retainedObservations": [held, completed]},
            {"sourceKey": "matthews", "supported": True, "transaction": "lease", "listingsCollected": 0}],
        "listings": [], "totalListings": 0, "brokers": []}


def project(value):
    raw=json.dumps(value, separators=(",", ":")).encode()
    return raw, derive(raw, generation_id="fixture", generation_started_at="2026-11-06T23:58:00Z",
        attempt_started_at="2026-11-06T23:59:00Z", attempt_finished_at="2026-11-07T00:02:00Z")


class MatthewsCandidateTest(unittest.TestCase):
    def test_bound_partial_evidence_keeps_valid_peers_and_holds_invalid(self):
        item=artifact()
        bad=copy.deepcopy(valid()); bad["observation"]["id"]="bad"; bad["observation"]["transactionType"]="Sale"
        item["sources"][0]["retainedObservations"].append(bad)
        raw, candidate=project(item)
        self.assertEqual(candidate["source_artifact_sha256"],hashlib.sha256(raw).hexdigest())
        self.assertEqual(candidate["rows"][0]["external_id"],"example")
        self.assertEqual([x["reason"] for x in candidate["held"]], ["source_observation_held", "invalid_completed_observation"])
        self.assertFalse(candidate["mark_missing_safe"])

    def test_versioned_outcome_ignores_message_and_rejects_malformed_or_mismatched_receipts(self):
        data = artifact()
        partial = data["sources"][0]
        partial["matthewsPartialOutcome"] = {"schema": "matthews-partial-outcome/v1", "transaction": "sale",
            "temporaryRedirects": 1, "detailFailures": 0, "httpFailures": 0}
        partial["error"] = "Human wording can change without affecting admission"
        _, candidate = project(data)
        self.assertEqual(candidate["rows"][0]["external_id"], "example")
        for mutate in (lambda outcome: outcome.update(schema="matthews-partial-outcome/v2"),
                       lambda outcome: outcome.update(transaction="lease"),
                       lambda outcome: outcome.update(temporaryRedirects=True),
                       lambda outcome: outcome.update(extra="untrusted")):
            bad = copy.deepcopy(data)
            mutate(bad["sources"][0]["matthewsPartialOutcome"])
            with self.subTest(outcome=bad["sources"][0]["matthewsPartialOutcome"]), self.assertRaises(ValueError):
                project(bad)
        bad = copy.deepcopy(data)
        bad["sources"][0]["error"] = ""
        with self.assertRaises(ValueError):
            project(bad)

    def test_zero_reported_counts_keep_peer_when_held_identity_is_verified(self):
        data = artifact()
        partial = data["sources"][0]
        partial["matthewsPartialOutcome"] = {"schema": "matthews-partial-outcome/v1", "transaction": "sale",
            "temporaryRedirects": 0, "detailFailures": 0, "httpFailures": 0}
        _, candidate = project(data)
        self.assertEqual(candidate["rows"][0]["external_id"], "example")
        self.assertEqual(candidate["receipt_warnings"], [{"transaction": "sale",
            "reason": "held_receipt_counts_unverified", "reported": [0, 0, 0], "verified": [1, 0, 0]}])
        partial["retainedObservations"][0]["sourceKey"] = "other"
        with self.assertRaisesRegex(ValueError, "no verified held outcome"):
            project(data)

    def test_held_provenance_and_duplicate_are_isolated_from_valid_peer(self):
        data = artifact()
        original = data["sources"][0]["retainedObservations"][0]
        typed = {"schema": "matthews-partial-outcome/v1", "transaction": "sale",
                 "temporaryRedirects": 3, "detailFailures": 2, "httpFailures": 1}
        data["sources"][0]["matthewsPartialOutcome"] = typed
        detail = {"schema": "matthews-detail-observation/v1", "sourceKey": "other",
                  "requestedTransaction": "lease", "requestedUrl": "https://www.matthews.com/properties/wrong",
                  "httpStatus": 200}
        wrong_tx = {**original, "requestedTransaction": "lease",
                    "requestedUrl": "https://www.matthews.com/properties/wrong-tx"}
        wrong_source = {**original, "sourceKey": "other",
                        "requestedUrl": "https://www.matthews.com/properties/wrong-source"}
        duplicate = copy.deepcopy(original)
        http = {"schema": "matthews-detail-http-observation/v1", "sourceKey": "matthews",
                "requestedTransaction": "sale", "requestedUrl": "https://www.matthews.com/properties/http",
                "outcome": "held_http_error", "httpStatus": 404}
        data["sources"][0]["retainedObservations"].extend([detail, wrong_tx, wrong_source, duplicate, http])
        raw, candidate = project(data)
        self.assertEqual([x["external_id"] for x in candidate["rows"]], ["example"])
        self.assertEqual([x["reason"] for x in candidate["held"]],
            ["source_observation_held", "invalid_held_observation", "invalid_held_observation",
             "invalid_held_observation", "duplicate_held_identity", "source_observation_held"])
        self.assertEqual(candidate["receipt_warnings"][0]["reported"], [3, 2, 1])
        self.assertEqual(candidate["receipt_warnings"][0]["verified"], [1, 0, 1])
        self.assertEqual(len(gate_projection(raw, candidate)["listings"]), 1)
        # Old artifacts use the same provenance validation after legacy parsing.
        data["sources"][0].pop("matthewsPartialOutcome")
        data["sources"][0]["error"] = "Error: Matthews strict sale partial: 3 temporary redirect(s) held; 2 detail parse or identity failure(s) held; 1 settled HTTP detail failure(s) held"
        _, legacy = project(data)
        self.assertEqual(legacy["receipt_warnings"], candidate["receipt_warnings"])

    def test_completed_identity_also_held_isolated_from_unrelated_peer(self):
        data = artifact()
        held = data["sources"][0]["retainedObservations"][0]
        held["requestedUrl"] = "https://www.matthews.com/properties/example"
        peer = copy.deepcopy(data["sources"][0]["retainedObservations"][1])
        peer["observation"].update(id="peer", url="https://www.matthews.com/properties/peer",
                                   canonicalUrl="https://www.matthews.com/properties/peer")
        data["sources"][0]["retainedObservations"].append(peer)
        raw, candidate = project(data)
        self.assertEqual([x["external_id"] for x in candidate["rows"]], ["peer"])
        self.assertIn("held_identity_conflict", [x["reason"] for x in candidate["held"]])
        self.assertEqual([x["id"] for x in gate_projection(raw, candidate)["listings"]], ["peer"])

    def test_invalid_duplicate_does_not_poison_accepted_ordinal(self):
        item = artifact()
        bad = copy.deepcopy(item["sources"][0]["retainedObservations"][1])
        bad["observation"]["canonicalUrl"] = "https://www.matthews.com/properties/different"
        item["sources"][0]["retainedObservations"].append(bad)
        raw, candidate = project(item)
        projected = gate_projection(raw, candidate)
        self.assertEqual(len(projected["listings"]), 1)
        self.assertEqual(projected["listings"][0]["id"], "example")
        self.assertEqual(candidate["accepted_observations"], [{"transaction": "sale", "kind": "retained", "ordinal": 1}])

    def test_gate_projection_counts_partial_without_canonical_authority(self):
        from datetime import datetime

        from cre_artifact_scope import require_canonical_artifact
        from cre_checkpoint_refresh import _validate_source_artifact_local
        from cre_gate import count_artifacts, select_baseline_updates

        raw, candidate = project(artifact())
        gate = gate_projection(raw, candidate)
        with self.assertRaisesRegex(ValueError, "not a canonical inventory"):
            require_canonical_artifact(gate)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"gate.json"; path.write_text(json.dumps(gate))
            counts, errors, sources, _, rejects = count_artifacts([str(path)], True)
            stats = _validate_source_artifact_local(path, "matthews", "2026-11-06T23:59:00Z",
                require_strict_freshness=True, expected_generation_id="fixture",
                expected_generation_started_at="2026-11-06T23:58:00Z", now=datetime(2026,11,7,0,3,tzinfo=UTC))
            self.assertTrue(stats["matthews_partial"])
        self.assertEqual(counts,{"matthews":1})
        self.assertEqual(errors,{"matthews":"matthews_partial_incomplete"})
        self.assertEqual(sources,{"matthews"}); self.assertEqual(rejects,0)
        self.assertEqual(select_baseline_updates({"matthews":{"verdict":"first_seen","current_active":1}},errors,{}),[])
        changed = copy.deepcopy(gate); changed["sources"][0].pop("error")
        with self.assertRaisesRegex(ValueError,"gate projection invalid"):
            require_canonical_artifact(changed,allow_matthews_partial_gate=True)

    def test_refuses_uncertain_errors_stale_attempt_and_mutated_generation(self):
        mutations=(lambda d: d["sources"][0].update(error="Error: cleanup unknown"),
                   lambda d: d["runMeta"]["freshness"].update(generationId="other"),
                   lambda d: d["runMeta"].update(finishedAt="2026-11-07T00:03:00Z"),
                   lambda d: d["runMeta"].update(maxItemsPerSource=5))
        for mutate in mutations:
            data=artifact(); mutate(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                project(data)

    def test_candidate_is_derived_from_private_settled_attempt_and_refuses_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/"fixture"; (root/"rejected").mkdir(parents=True)
            source = artifact()
            source["sources"][0]["matthewsPartialOutcome"] = {"schema": "matthews-partial-outcome/v1",
                "transaction": "sale", "temporaryRedirects": 0, "detailFailures": 0, "httpFailures": 0}
            raw=json.dumps(source, separators=(",", ":")).encode()
            original=root/"rejected"/"matthews.json"; original.write_bytes(raw); os.chmod(original,0o600)
            attempt={"number": 1, "rc": 1, "started_at": "2026-11-06T23:59:00Z", "finished_at": "2026-11-07T00:02:00Z",
                "rejected_artifact": "rejected/matthews.json", "rejected_artifact_sha256": hashlib.sha256(raw).hexdigest()}
            manifest={"run_id":"fixture", "started_at":"2026-11-06T23:58:00Z", "collector_git_sha":"a"*40,
                "collector_git_dirty":False, "preflight":{"database_target":{"algorithm":"sha256","value":"b"*64}}, "scope":{}, "sources":{"matthews":{"attempts":[attempt]}}, "config":{
                "transactions":["sale","lease"], "max_items":0, "additive":True, "mark_missing":False,
                "status_activation":False, "sources":["matthews"]}}
            from cre_checkpoint_refresh import retain_matthews_partial_candidate
            attempt["rc"] = 80
            with patch("cre_checkpoint_refresh.git_identity", return_value=("a"*40, False)):
                retain_matthews_partial_candidate(root,manifest,attempt)
            result=attempt["matthews_partial_candidate"]
            self.assertEqual(result["state"],"sealed_unadmitted")
            self.assertEqual(result["original_sha256"],hashlib.sha256(raw).hexdigest())
            self.assertEqual(result["candidate_count"],1)
            candidate=root/result["path"]; self.assertEqual(candidate.stat().st_mode & 0o777,0o600)
            verified = recheck_candidate(root,manifest,attempt,git_identity=lambda:("a"*40,False),target_sha256="b"*64,settled_rcs=(0,1,80))
            self.assertEqual(verified["rows"][0]["external_id"],"example")
            gate_path = root/result["gate_path"]
            from datetime import datetime

            from cre_checkpoint_refresh import (
                _validate_source_artifact_local,
                promote_matthews_partial_candidate,
            )
            manifest["config"]["pipeline"]=False
            def validate_bound(path, *args, **kwargs):
                return _validate_source_artifact_local(path,*args,now=datetime(2026,11,7,0,3,tzinfo=UTC),**kwargs)
            with patch("cre_checkpoint_refresh.git_identity",return_value=("a"*40,False)), \
                 patch("cre_checkpoint_refresh.validate_source_artifact",side_effect=validate_bound), \
                 patch("cre_checkpoint_refresh.save_manifest"):
                promoted=promote_matthews_partial_candidate(root,manifest,attempt,candidate_only=False)
            self.assertEqual(promoted[0],gate_path)
            self.assertEqual(manifest["sources"]["matthews"]["state"],"validated")
            self.assertTrue(manifest["sources"]["matthews"]["artifact"]["matthews_partial"])
            def report():
                return {"generated_for_artifact":str(gate_path),"per_source":{"matthews":{"current_active":1,
                    "verdict":"hold","reason":"source pass error: matthews_partial_incomplete","mark_missing_safe":False}},
                    "summary":{"mode":"live","torow_errors":0,"baseline_updated":False,"baseline_update_sources":[],
                    "hold_sources":["matthews"],"mark_missing_safe_sources":[],"verdict_counts":{}},"per_brokerage":{}}
            source_report=report()
            proof=admit_gate(gate_path,root,manifest,source_report,git_identity=lambda:("a"*40,False),
                target_sha256="b"*64,phase="source",settled_rcs=(0,1,80))
            manifest["sources"]["matthews"]["gate"]={"current_inventory_proof":proof}
            aggregate=report()
            self.assertEqual(admit_gate(gate_path,root,manifest,aggregate,git_identity=lambda:("a"*40,False),
                target_sha256="b"*64,phase="aggregate",settled_rcs=(0,1,80)),proof)
            self.assertFalse(proof["whole_source_coverage"])
            self.assertEqual(proof["receipt_warnings"], result["receipt_warnings"])
            self.assertEqual(proof["receipt_warnings"][0]["reported"], [0, 0, 0])
            self.assertEqual(proof["receipt_warnings"][0]["verified"], [1, 0, 0])
            from reporting.partial_qualification import (
                matthews_partial_child,
                partial_member_report_matches,
            )
            manifest["config"]["pipeline"] = True
            manifest["sources"]["matthews"]["artifact"]["staged_unique"] = 1
            manifest["sources"]["matthews"]["gate"].update(verdict="ok_additive_verified_subset",mark_missing_safe=False,
                admission_scope="matthews_existing_parent_partial_v1")
            manifest["sources"]["matthews"]["subset_ingest_attempt_id"]="d"*32
            observation={"occupied_jobs":1,"matching_jobs":1,"verified_jobs":1,"held":[],"updated":["example"],
                "held_reasons":{},"artifact_sha256":result["gate_sha256"],"generation_id":"fixture",
                "database_target_sha256":"b"*64,"attempt_id":"d"*32,"candidate_count":1,"source_held_count":1,
                "whole_source_coverage":False}
            checkpoint=manifest["sources"]["matthews"]
            checkpoint["readback"]={"ok":True,"kind":"matthews_existing_parent_partial","whole_source_coverage":False,
                "observation":observation}
            self.assertTrue(matthews_partial_child(manifest))
            self.assertTrue(partial_member_report_matches({"matthews_partial_readback":observation},checkpoint,"matthews","fixture"))
            bad_report=copy.deepcopy(observation); bad_report["verified_jobs"]=0
            self.assertFalse(partial_member_report_matches({"matthews_partial_readback":bad_report},checkpoint,"matthews","fixture"))
            manifest["config"].pop("pipeline")
            from matthews_partial.worker import context
            envelope={"manifest":manifest,"source_report":source_report,"aggregate_report":aggregate,
                "binding":proof,"attempt_id":"d"*32}
            bound=context(gate_path,envelope,code_sha="a"*40,dirty=False,target_sha256="b"*64,attempt_id="d"*32)
            self.assertEqual(bound["rows"][0]["external_id"],"example")
            invalid=copy.deepcopy(envelope); invalid["aggregate_report"]["per_source"]["matthews"]["current_inventory_proof"]["candidate_count"]=99
            with self.assertRaises(ValueError):
                context(gate_path,invalid,code_sha="a"*40,dirty=False,target_sha256="b"*64,attempt_id="d"*32)
            with self.assertRaisesRegex(ValueError,"target changed"):
                recheck_candidate(root,manifest,attempt,git_identity=lambda:("a"*40,False),target_sha256="c"*64,settled_rcs=(0,1,80))
            candidate.write_bytes(candidate.read_bytes()+b" ")
            with self.assertRaisesRegex(ValueError,"evidence changed"):
                recheck_candidate(root,manifest,attempt,git_identity=lambda:("a"*40,False),target_sha256="b"*64,settled_rcs=(0,1,80))
            candidate.unlink(); original.write_bytes(raw+b" "); attempt["rc"] = 1
            with self.assertRaisesRegex(ValueError,"hash changed"):
                prepare_candidate(root,manifest,attempt,git_identity=lambda:("a"*40,False))

    def test_pipeline_archives_failed_candidate_and_seals_unadmitted_peers(self):
        from cre_refresh_pipeline import archive_failed_candidate

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixture"; (root / "sources").mkdir(parents=True)
            raw = json.dumps(artifact(), separators=(",", ":")).encode()
            candidate = root / "sources" / "matthews.json"; candidate.write_bytes(raw); os.chmod(candidate, 0o600)
            attempt = {"number": 1, "rc": 0, "started_at": "2026-11-06T23:59:00Z", "finished_at": "2026-11-07T00:02:00Z"}
            checkpoint = {"attempts": [attempt], "candidate": {"path": "sources/matthews.json"}, "state": "artifact_rejected"}
            manifest = {"run_id": "fixture", "started_at": "2026-11-06T23:58:00Z", "collector_git_sha": "a" * 40,
                "collector_git_dirty": False, "sources": {"matthews": checkpoint}, "config": {
                "transactions": ["sale", "lease"], "max_items": 0, "additive": True, "mark_missing": False,
                "status_activation": False, "sources": ["matthews"]}}
            with patch("cre_checkpoint_refresh.git_identity", return_value=("a" * 40, False)):
                archive_failed_candidate(root, manifest, "matthews")
            self.assertEqual(checkpoint["state"], "artifact_rejected")
            self.assertEqual(attempt["matthews_partial_candidate"]["state"], "sealed_unadmitted")
            self.assertEqual((root / attempt["rejected_artifact"]).read_bytes(), raw)
            self.assertFalse(candidate.exists())

    def test_direct_writer_without_inherited_owner_is_refused_before_claim_read(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            result=subprocess.run([sys.executable,str(root/"cre_ingest.py"),"--in",str(Path(directory)/"missing.json"),
                "--matthews-partial-claims",str(Path(directory)/"missing-claims.json"),"--matthews-partial-claims-sha256","c"*64,"--checkpoint-ingest-attempt","d"*32,
                "--expected-db-target-sha256","b"*64,"--require-strict-freshness","--dry-run"],
                cwd=root,env={"PATH":os.environ["PATH"]},text=True,capture_output=True,check=False,timeout=30)
            self.assertNotEqual(result.returncode,0)
            self.assertIn("descriptor is missing",result.stderr)
            self.assertNotIn("missing-claims.json",result.stderr)

    def test_settled_nonzero_reconciles_commit_rollback_or_uncertainty(self):
        import cre_checkpoint_refresh as refresh

        def make_manifest():
            return {"run_id":"fixture","collector_git_sha":"a"*40,"preflight":{"database_target":{"algorithm":"sha256","value":"b"*64}},
                "config":{"sources":["matthews"]},"sources":{"matthews":{
                "state":"dry_run_passed","artifact":{"path":"sources/matthews.json","sha256":"c"*64,"matthews_partial":True},
                "subset_ingest_attempt_id":"d"*32}}}
        for counts, outcome in (((0,0,0),"rollback"),((1,1,1),"commit"),((1,0,0),"unknown")):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root=Path(directory); manifest=make_manifest()
                observed={"occupied_jobs":counts[0],"matching_jobs":counts[1],"verified_jobs":counts[2],
                    "artifact_sha256":"c"*64,"generation_id":"fixture","database_target_sha256":"b"*64,"attempt_id":"d"*32}
                with patch("cre_checkpoint_refresh.subset_ingest_arguments",return_value=["--matthews-partial-claims",str(root/"claims.json")]), \
                     patch("matthews_partial.checkpoint._read_private",return_value=b"{}"), \
                     patch("matthews_partial.worker.context",return_value={}), \
                     patch("cre_checkpoint_refresh.git_identity",return_value=("a"*40,False)), \
                     patch("cre_checkpoint_refresh._run_nai_owned_command",return_value=1), \
                     patch("cre_checkpoint_refresh.matthews_partial_readback",return_value=observed), \
                     patch("cre_checkpoint_refresh.save_manifest"):
                    if outcome=="rollback":
                        with self.assertRaises(refresh.SettledIngestFailure):
                            refresh.ingest_source(root,manifest,"matthews",root/"sources/matthews.json",None)
                        self.assertTrue(refresh.verified_matthews_partial_rollback(root,manifest))
                    elif outcome=="commit":
                        refresh.ingest_source(root,manifest,"matthews",root/"sources/matthews.json",None)
                        self.assertEqual(manifest["sources"]["matthews"]["ingest_recovery"]["outcome"],"exact_commit")
                    else:
                        with self.assertRaisesRegex(refresh.GlobalStageError,"uncertain"):
                            refresh.ingest_source(root,manifest,"matthews",root/"sources/matthews.json",None)
                        self.assertEqual(manifest["sources"]["matthews"]["state"],"ingest_recovery_required")

    def test_serial_readback_timeout_preserves_global_recovery_authority(self):
        import cre_checkpoint_refresh as refresh

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {"run_id": "fixture", "collector_git_sha": "a" * 40,
                "preflight": {"database_target": {"algorithm": "sha256", "value": "b" * 64}},
                "config": {"sources": ["matthews"]}, "sources": {"matthews": {
                "state": "dry_run_passed", "artifact": {"path": "sources/matthews.json",
                "sha256": "c" * 64, "matthews_partial": True}, "subset_ingest_attempt_id": "d" * 32}}}
            path = root / ".cre.lock"
            owner = refresh.SharedLock(path, recovery_required=True)
            owner.acquire()
            try:
                with patch("cre_checkpoint_refresh.subset_ingest_arguments",return_value=["--matthews-partial-claims",str(root/"claims.json")]), \
                     patch("matthews_partial.checkpoint._read_private",return_value=b"{}"), \
                     patch("matthews_partial.worker.context",return_value={}), \
                     patch("cre_checkpoint_refresh.git_identity",return_value=("a"*40,False)), \
                     patch("cre_checkpoint_refresh._run_nai_owned_command",return_value=1), \
                     patch("cre_checkpoint_refresh.matthews_partial_readback",side_effect=TimeoutError("lock timeout")), \
                     patch("cre_checkpoint_refresh.save_manifest"):
                    with self.assertRaises(refresh.GlobalStageError):
                        refresh.ingest_source(root,manifest,"matthews",root/"sources/matthews.json",None)
                self.assertTrue(refresh.serial_write_outcome_unknown(manifest))
                # The same finalization condition in main must leave authority armed.
                if not refresh.serial_write_outcome_unknown(manifest):
                    owner.clear_recovery_requirement()
            finally:
                owner.release()
            authority = path.with_name(path.name + ".authority")
            self.assertTrue(authority.read_text().endswith("recovery-required\n"))
            with self.assertRaises(refresh.LockHeldError):
                refresh.SharedLock(path).acquire()

    def test_interrupted_unsettled_owner_never_replays(self):
        import cre_checkpoint_refresh as refresh

        manifest={"config":{},"sources":{"matthews":{"artifact":{"matthews_partial":True}}}}
        with (tempfile.TemporaryDirectory() as directory, patch("cre_checkpoint_refresh.save_manifest"),
              self.assertRaisesRegex(refresh.GlobalStageError,"settled-owner reconciliation")):
            refresh.recover_interrupted_ingest(Path(directory),manifest,"matthews",None)
        self.assertEqual(manifest["sources"]["matthews"]["state"],"ingest_recovery_required")

    def test_duplicate_identities_are_held_not_merged(self):
        data=artifact(); data["sources"][0]["retainedObservations"].append(copy.deepcopy(data["sources"][0]["retainedObservations"][1]))
        with self.assertRaisesRegex(ValueError,"no individually valid peers"):
            project(data)


if __name__ == "__main__":
    unittest.main()
