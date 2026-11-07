import hashlib
import json
import unittest

from cbre_partial_projection import project


def payload(records):
    header = {"schema": "cbre-partial-pages/v1", "sourceKey": "cbre", "generation": "run-1", "attempt": 1}
    raw = ("\n".join(json.dumps(item) for item in [header, *records]) + "\n").encode()
    custody = {"state": "sealed_local", "coverage": "unverified", "outcome": "source_error",
               "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "observationRecords": len(records)}
    return raw, custody


def page(docs, **extra):
    return {"kind": "validated_page", "transaction": "sale", "page": 1, "pass": 1,
            "observedAt": "2026-01-01T00:00:00Z", "documents": docs, **extra}


def run(records, page_size=2):
    raw, custody = payload(records)
    return project(raw, generation="run-1", attempt=1, custody=custody, expected_page_size=page_size)


class ProjectionTests(unittest.TestCase):
    def test_valid_peer_and_malformed_document_keep_source_evidence_without_tenure(self):
        good = {"Common.PrimaryKey": "id-1", "Common.Aspects": None, "optional": None}
        response = {"DocumentCount": 2, "Documents": [[good, None]]}
        rejected = page([response], kind="cbre_rejected_page", rejection="document_validation",
                        qualification="unverified_page", rejectedDocumentsIndexBasis="documents_flat_1",
                        rejectedDocuments=[1], pageSize=2)
        result = run([rejected])
        self.assertEqual(result["candidates"][0]["source_id"], "id-1")
        self.assertEqual(result["candidates"][0]["observations"][0]["document"], good)
        self.assertEqual(result["held"][0]["reason"], "missing_identity")
        self.assertIsNone(result["held"][0]["document"])
        self.assertFalse(result["whole_source_coverage"])
        self.assertFalse(result["write_authorized"])
        self.assertNotIn("transaction_type", result["candidates"][0])

    def test_envelope_failure_keeps_entire_page_and_never_emits_peers(self):
        record = page([{"Documents": [{"Common.PrimaryKey": "x"}]}], kind="cbre_rejected_page",
                      rejection="page_envelope", qualification="unverified_page",
                      rejectedDocumentsIndexBasis="documents_flat_1", rejectedDocuments=[])
        result = run([record])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["held"][0]["record"], record)

    def test_duplicate_and_changed_identity_hold_all_observations(self):
        a = {"Common.PrimaryKey": "a", "name": "one"}
        b = {"Common.PrimaryKey": "b"}
        rejected = page([{"DocumentCount": 3, "Documents": [a, b, a]}], kind="cbre_rejected_page",
                        rejection="document_validation", qualification="unverified_page",
                        rejectedDocumentsIndexBasis="documents_flat_1", rejectedDocuments=[0, 2], pageSize=3)
        result = run([rejected, page([a], page=2), page([{"Common.PrimaryKey": "b", "name": "different"}], page=3)], page_size=3)
        self.assertEqual(result["candidates"], [])
        self.assertEqual([item["reason"] for item in result["held"]].count("duplicate_identity"), 2)
        self.assertEqual([item["reason"] for item in result["held"]].count("conflicting_identity"), 2)

    def test_distinct_passes_identical_identity_retain_observations(self):
        item = {"Common.PrimaryKey": "a"}
        result = run([page([item]), page([item], **{"pass": 2, "transaction": "lease"})])
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(len(result["candidates"][0]["observations"]), 2)

    def test_changed_bytes_invalid_custody_or_labels_refuse(self):
        raw, custody = payload([page([{"Common.PrimaryKey": "a"}])])
        for altered, binding in [(raw + b" ", custody), (raw, {**custody, "state": "pending"}),
                                 (raw, {**custody, "outcome": "source_returned"})]:
            with self.subTest(binding=binding), self.assertRaises(ValueError):
                project(altered, generation="run-1", attempt=1, custody=binding, expected_page_size=2)
        rejected = page([{"DocumentCount": 1, "Documents": [None]}], kind="cbre_rejected_page", rejection="document_validation",
                        qualification="unverified_page", rejectedDocumentsIndexBasis="documents_flat_1",
                        rejectedDocuments=[], pageSize=1)
        with self.assertRaises(ValueError):
            run([rejected], page_size=1)


class EnvelopeTests(unittest.TestCase):
    def test_mislabelled_bad_envelope_cannot_emit_candidate(self):
        record = page([{"DocumentCount": 100, "Documents": [{"Common.PrimaryKey": "valid"}, None]}],
                      kind="cbre_rejected_page", rejection="document_validation", qualification="unverified_page",
                      rejectedDocumentsIndexBasis="documents_flat_1", rejectedDocuments=[1], pageSize=500)
        result = run([record], page_size=500)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["held"][0]["reason"], "page_envelope_unverified")

    def test_missing_request_page_size_and_whitespace_identity_are_held(self):
        record = page([{"DocumentCount": 2, "Documents": [{"Common.PrimaryKey": "valid"}, None]}],
                      kind="cbre_rejected_page", rejection="document_validation", qualification="unverified_page",
                      rejectedDocumentsIndexBasis="documents_flat_1", rejectedDocuments=[1])
        result = run([record, page([{"Common.PrimaryKey": " id "}])])
        self.assertEqual(result["candidates"], [])
        self.assertEqual({item["reason"] for item in result["held"]}, {"page_envelope_unverified", "missing_identity"})


    def test_explicit_null_page_is_not_absent_metadata(self):
        record = page([{"DocumentCount": 2, "Page": None,
                        "Documents": [{"Common.PrimaryKey": "valid"}, None]}],
                      kind="cbre_rejected_page", rejection="document_validation", qualification="unverified_page",
                      rejectedDocumentsIndexBasis="documents_flat_1", rejectedDocuments=[1], pageSize=2)
        result = run([record])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["held"][0]["reason"], "page_envelope_unverified")


    def test_whitespace_identity_taints_normalized_peer(self):
        result = run([page([{"Common.PrimaryKey": " id "}]), page([{"Common.PrimaryKey": "id"}], page=2)])
        self.assertEqual(result["candidates"], [])
        self.assertEqual({item["reason"] for item in result["held"]}, {"missing_identity", "conflicting_identity"})


    def test_invalid_envelopes_taint_normalized_peer(self):
        for reason in ("page_envelope", "document_validation"):
            record = page([{"DocumentCount": 100, "Documents": [{"Common.PrimaryKey": " id "}, None]}],
                          kind="cbre_rejected_page", rejection=reason, qualification="unverified_page",
                          rejectedDocumentsIndexBasis="documents_flat_1", rejectedDocuments=[1], pageSize=500)
            result = run([record, page([{"Common.PrimaryKey": "id"}], page=2)], page_size=500)
            self.assertEqual(result["candidates"], [])
            self.assertIn("conflicting_identity", [item["reason"] for item in result["held"]])


if __name__ == "__main__":
    unittest.main()
