"""Source replay retains authentic rows and holds ambiguous identities."""
import hashlib
import json
import unittest

from cre_lyon_search_replay import _ENDPOINTS, replay_search, validate_source_artifact


def spool(pages):
    lines = [{"schema": "lyon-stahl-partial-pages/v1", "sourceKey": "lyon-stahl", "generation": "g", "attempt": 1}]
    for name, text in pages:
        clock = "2026-11-05T00:00:00Z"
        page = {"filter": name, "observedAt": clock, "responseText": text, "endpoint": _ENDPOINTS[name],
                "responseTextSha256": hashlib.sha256(text.encode()).hexdigest()}
        lines.append({"kind": "lyon_search_response", "transaction": "sale", "pass": ("all", "sale", "lease").index(name) + 1,
                      "page": 1, "observedAt": clock, "documents": [page]})
    return ("\n".join(json.dumps(line) for line in lines) + "\n").encode()


class LyonReplayTests(unittest.TestCase):
    def test_rows_remain_exact_and_both_occurrences_are_source_bound(self):
        url = "https://lyonstahl.com/?post_type=properties&p=40201"
        row = {"permalink": url, "title": "Observed", "price": None, "extra": {"x": [1, 2]}}
        data = json.dumps([row])
        records, held = replay_search(spool([("all", data), ("sale", data), ("lease", "not-json")]), "g", 1)
        self.assertEqual(held, [{"filter": "lease", "reason": "invalid_response"}])
        self.assertEqual(records[0]["sourceIdentity"], {"kind": "wordpress_post", "value": "40201"})
        self.assertEqual([o["row"] for o in records[0]["occurrences"]], [row, row])
        self.assertEqual(records[0]["occurrences"][0]["responseTextSha256"], hashlib.sha256(data.encode()).hexdigest())

    def test_collisions_and_unidentifiable_cards_hold_while_peer_survives(self):
        rows = [{"permalink": "https://lyonstahl.com/properties/a"},
                {"permalink": "https://lyonstahl.com/properties/a/"},
                {"permalink": "https://lyonstahl.com/properties/b/"}, {"title": "no id"}]
        records, held = replay_search(spool([("all", json.dumps(rows))]), "g", 1)
        self.assertEqual([r["sourceIdentity"]["value"] for r in records], ["b"])
        self.assertEqual([h["reason"] for h in held].count("identity_conflict"), 2)
        self.assertEqual([h["reason"] for h in held].count("invalid_identity"), 1)

    def test_replay_requires_summary_and_all_filter_outcomes_to_agree(self):
        raw = spool([("all", json.dumps([{"permalink": "https://lyonstahl.com/properties/a"}])),
                     ("lease", "not-json")])
        summary = {"schema": "lyon-search-summary-v1", "coverage": "partial_unknown", "stopped": False,
                   "failures": [{"filter": "sale", "reason": "acquisition_failed"},
                                {"filter": "lease", "reason": "invalid_response"}],
                   "candidates": 1, "held": 0}
        records, held = replay_search(raw, "g", 1, summary=summary)
        self.assertEqual(len(records), 1)
        self.assertEqual(held[0]["reason"], "invalid_response")
        summary["candidates"] = 2
        with self.assertRaises(ValueError):
            replay_search(raw, "g", 1, summary=summary)
        summary["candidates"] = 1
        summary["failures"].pop(0)
        with self.assertRaises(ValueError):
            replay_search(raw, "g", 1, summary=summary)

    def test_path_and_post_identity_namespace_collision_is_held(self):
        rows = [{"permalink": "https://lyonstahl.com/properties/wordpress:40201"},
                {"permalink": "https://lyonstahl.com/?post_type=properties&p=40201"},
                {"permalink": "https://lyonstahl.com/properties/good"}]
        records, held = replay_search(spool([("all", json.dumps(rows))]), "g", 1)
        self.assertEqual([r["sourceIdentity"]["value"] for r in records], ["good"])
        self.assertEqual([h["reason"] for h in held], ["identity_conflict", "identity_conflict"])

    def test_terminal_and_access_stops_cannot_be_admitted(self):
        summary = {"schema": "lyon-search-summary-v1", "coverage": "partial_unknown", "stopped": False,
                   "failures": [], "candidates": 1, "held": 0}
        artifact = {"runMeta": {"evidenceOnly": True}, "listings": [], "sources": [
            {"sourceKey": "lyon-stahl", "transaction": "sale",
             "error": "Error: Lyon search is partial and requires source-bound admission", "retainedObservations": [summary]}]}
        self.assertEqual(validate_source_artifact(artifact), summary)
        artifact["runMeta"]["terminalCaptureFailure"] = True
        with self.assertRaises(ValueError):
            validate_source_artifact(artifact)
        artifact["runMeta"].pop("terminalCaptureFailure")
        summary["stopped"] = True
        with self.assertRaises(ValueError):
            validate_source_artifact(artifact)
        summary["stopped"] = False
        summary["failures"] = [{"filter": "sale", "reason": "access_stopped"}]
        with self.assertRaises(ValueError):
            validate_source_artifact(artifact)

    def test_bad_hash_or_attempt_rejected(self):
        raw = spool([("all", "[]")])
        with self.assertRaises(ValueError):
            replay_search(raw.replace(b"[]", b"[ ]"), "g", 1)
        with self.assertRaises(ValueError):
            replay_search(raw, "g", 2)
