"""Bind partial projection to exact settled source artifact and clocks."""
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cre_matthews_partial_checkpoint import derive, prepare_candidate
from tests.test_matthews_partial_fill import valid


def artifact():
    held = {"schema": "matthews-detail-redirect-observation/v1", "sourceKey": "matthews", "outcome": "held_temporary_redirect", "httpStatus": 307, "requestedTransaction": "sale"}
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
            raw=json.dumps(artifact(), separators=(",", ":")).encode()
            original=root/"rejected"/"matthews.json"; original.write_bytes(raw); os.chmod(original,0o600)
            attempt={"number": 1, "rc": 1, "started_at": "2026-11-06T23:59:00Z", "finished_at": "2026-11-07T00:02:00Z",
                "rejected_artifact": "rejected/matthews.json", "rejected_artifact_sha256": hashlib.sha256(raw).hexdigest()}
            manifest={"run_id":"fixture", "started_at":"2026-11-06T23:58:00Z", "collector_git_sha":"a"*40,
                "collector_git_dirty":False, "sources":{"matthews":{"attempts":[attempt]}}, "config":{
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

    def test_duplicate_identities_are_held_not_merged(self):
        data=artifact(); data["sources"][0]["retainedObservations"].append(copy.deepcopy(data["sources"][0]["retainedObservations"][1]))
        with self.assertRaisesRegex(ValueError,"no individually valid peers"):
            project(data)


if __name__ == "__main__":
    unittest.main()
