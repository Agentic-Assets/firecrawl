import copy
import unittest

from cbre_partial_target import identity_probe_sql, partition_existing, partition_sidecar
from tests.test_cbre_partial_projection import page, payload


def projection(*identities):
    return {"source_key": "cbre", "generation": "run-1", "attempt": 1, "sidecar_sha256": "a" * 64,
            "write_authorized": False, "whole_source_coverage": False,
            "candidates": [{"source_id": key, "observations": [{"document": {"Common.PrimaryKey": key}}]}
                           for key in identities], "held": [{"reason": "missing_identity", "document": None}]}


def probe(identity, *, raw=None, deleted=None):
    return {"external_id": identity, "brokerage_id": "broker-1", "listing": {
        "id": "row-" + identity, "brokerage_id": "broker-1", "external_id": identity,
        "source_url": "https://stored.example/property", "deleted_at": deleted,
        "status": "active", "title": "existing title", "raw_data": raw if raw is not None else
        {"sourceKey": "cbre", "id": identity, "prior": "preserve"}}}


class TargetTests(unittest.TestCase):
    def test_existing_exact_witness_retains_source_and_entire_preimage(self):
        p = projection("a")
        row = probe("a")
        before = copy.deepcopy(row)
        result = partition_existing(p, [row])
        self.assertEqual(row, before)
        self.assertEqual(result["provisional_existing"][0]["target_preimage"], row["listing"])
        self.assertEqual(result["provisional_existing"][0]["source"], p["candidates"][0])
        self.assertEqual(result["source_held"], p["held"])
        self.assertEqual(result["attempt"], 1)
        self.assertFalse(result["write_authorized"])
        self.assertFalse(result["whole_source_coverage"])

    def test_exact_sidecar_to_target_partition_keeps_peer_and_invalid_evidence(self):
        source = page([{"DocumentCount": 2, "Documents": [{"Common.PrimaryKey": "a"}, None]}],
                      kind="cbre_rejected_page", rejection="document_validation", qualification="unverified_page",
                      rejectedDocumentsIndexBasis="documents_flat_1", rejectedDocuments=[1], pageSize=2)
        raw, custody = payload([source])
        result = partition_sidecar(raw, generation="run-1", attempt=1, custody=custody,
                                   expected_page_size=2, probe_rows=[probe("a")])
        self.assertEqual(result["provisional_existing"][0]["source_id"], "a")
        self.assertEqual(result["source_held"][0]["reason"], "missing_identity")
        self.assertFalse(result["write_authorized"])
        with self.assertRaises(ValueError):
            partition_sidecar(raw + b"x", generation="run-1", attempt=1, custody=custody,
                              expected_page_size=2, probe_rows=[probe("a")])

    def test_missing_retired_foreign_and_conflicting_witnesses_are_held(self):
        p = projection("a", "b", "c", "d")
        rows = [{"external_id": "a", "brokerage_id": "broker-1", "listing": None},
                probe("b", deleted="2026-01-01"),
                probe("c", raw={"sourceKey": "other", "id": "c"}),
                probe("d", raw={"primary": {"sourceKey": "cbre", "id": "d"},
                                "secondary_pass": {"sourceKey": "cbre", "id": "other"}})]
        result = partition_existing(p, rows)
        self.assertEqual(result["provisional_existing"], [])
        self.assertEqual([item["reason"] for item in result["held"]], ["new_parent_without_source_url",
                         "target_retired", "target_source_witness_invalid", "target_source_witness_invalid"])
        self.assertEqual(result["held"][0]["source"], p["candidates"][0])

    def test_merged_matching_witnesses_and_reserved_history(self):
        p = projection("a", "b")
        rows = [probe("a", raw={"primary": {"sourceKey": "cbre", "id": "a"},
                                "secondary_pass": {"sourceKey": "cbre", "id": "a"}}),
                probe("b", raw={"sourceKey": "cbre", "id": "b", "cbrePartialObservations": []})]
        result = partition_existing(p, rows)
        self.assertEqual([item["source_id"] for item in result["provisional_existing"]], ["a"])
        self.assertEqual(result["held"][0]["reason"], "target_history_unverified")

    def test_missing_preimage_lifecycle_field_is_held(self):
        row = probe("a")
        del row["listing"]["deleted_at"]
        result = partition_existing(projection("a"), [row])
        self.assertEqual(result["provisional_existing"], [])
        self.assertEqual(result["held"][0]["reason"], "target_identity_invalid")

    def test_unbound_scope_and_identity_injection_are_refused(self):
        for p in (projection("x' OR true --"), {**projection("a"), "write_authorized": True},
                  {**projection("a"), "whole_source_coverage": True}):
            with self.subTest(p=p), self.assertRaises(ValueError):
                identity_probe_sql(p)
        with self.assertRaises(ValueError):
            partition_existing(projection("a", "b"), [probe("a"), probe("a")])

    def test_sql_is_read_only_and_uses_broker_scoped_exact_id(self):
        sql = identity_probe_sql(projection("a", "b"))
        self.assertIn("REPEATABLE READ READ ONLY", sql)
        self.assertIn("b.slug='cbre'", sql)
        self.assertIn("l.brokerage_id=b.id AND l.external_id=m.external_id", sql)
        self.assertNotIn("INSERT ", sql)
        self.assertNotIn("UPDATE ", sql)
        self.assertNotIn("DELETE ", sql)
        self.assertTrue(sql.rstrip().endswith("ROLLBACK;"))


if __name__ == "__main__":
    unittest.main()
