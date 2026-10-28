"""Acquisition retention must preserve uncertainty and never imply canonical facts."""
import copy
import hashlib

import pytest
from source_evidence.events import encoded, plan, sha256, split_text, validate_admission


def document(text=None, status="extracted"):
    return {"schema_version": "cre-document-text/v1",
            "source": {"url": "https://example.org/public.pdf", "observed_at": "2026-10-28T10:00:00Z",
                       "sha256": "a" * 64, "byte_length": 500, "format": "pdf"},
            "parser": {"name": "test-local", "version": "1", "config": {"ocr": "disabled"}},
            "extraction": {"status": status, "coverage": "unknown", "text": text,
                           "text_sha256": hashlib.sha256(text.encode()).hexdigest() if text is not None else None,
                           "error_code": "needsOcr" if status == "ocr_needed" else None}}


def admission(value):
    return {"schema_version": "cre-source-evidence-admission/v1", "plan_sha256": value["plan_sha256"],
            "purpose": "private_unverified_evidence", "data_scope": "public_listing_source",
            "content_policy": "reviewed_for_private_retention", "source_keys": [value["source_key"]]}


def test_markdown_url_label_and_target_are_independently_scanned_across_chunks():
    text = "x" * 59_990 + "[https://label.example/file](https://target.example/file)"
    value = plan(document(text), source_key="public-documents")
    validate_admission(value, admission(value))
    assert "".join(row["extracted_text"] for row in value["events"][1:]) == text


@pytest.mark.parametrize("label,target", [
    ("https://jll.com/a", "https://target.example/a"),
    ("https://label.example/a", "https://jll.com/a"),
    ("https://label.example/a?token=private", "https://target.example/a"),
    ("https://label.example/a", "https://target.example/a?token=private"),
])
def test_markdown_url_label_and_target_keep_source_and_credential_refusals(label, target):
    value = plan(document(f"[{label}]({target})"), source_key="public-documents")
    with pytest.raises(ValueError):
        validate_admission(value, admission(value))


def test_malformed_bracketed_prose_url_is_still_refused():
    value = plan(document("https://[invalid-host/path"), source_key="public-documents")
    with pytest.raises(ValueError):
        validate_admission(value, admission(value))


def test_unicode_text_retained_exactly_with_order_and_full_digest():
    text = ("Lease economics: €9.50, 0.5%, 200 SF.\n" * 5000) + "FINAL SOURCE LINE"
    value = plan(document(text), source_key="public-documents")
    chunks = value["events"][1:]
    assert len(chunks) > 1
    assert "".join(event["extracted_text"] for event in chunks) == text
    assert [event["item_no"] for event in chunks] == list(range(1, len(chunks) + 1))
    assert all(len(event["extracted_text"].encode()) <= 60_000 for event in chunks)
    assert all(event["provenance"]["coverage"] == "unknown" for event in chunks)
    assert all(event["provenance"]["source_identity"] == "unverified" for event in chunks)
    assert chunks[-1]["extracted_text"].endswith("FINAL SOURCE LINE")
    assert chunks[0]["provenance"]["text_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert plan(document(text), source_key="public-documents") == value


@pytest.mark.parametrize("status", ["failed", "ocr_needed"])
def test_unsuccessful_extraction_is_still_retained(status):
    value = plan(document(None, status), source_key="public-documents")
    assert len(value["events"]) == 1
    event = value["events"][0]
    assert event["extracted_text"] is None
    assert event["document_url"] is None
    assert event["source_url"] == "https://example.org/public.pdf"
    assert event["facts"][0]["value"]["extraction"]["status"] == status


def test_over_fifty_partial_records_keep_original_conflicting_attributes():
    rows = [{"identityResolution": "unresolved", "sourcePointer": f"/inventory/{i}",
             "sourceFields": {"title": f"Source card {i}", "unknownDetail": {"a": [1, 2]}},
             "indexAttributes": [{"sourceOrdinal": 1, "literal": ["# of Spaces", "3 Spaces"]},
                                 {"sourceOrdinal": 2, "literal": ["# of Spaces", "unknown"]}]}
            for i in range(71)]
    artifact = {"schema": "buildout-private-observation-artifact-v1",
                "provenance": {"sourceKey": "svn"}, "records": rows,
                "summary": {"status": "partial", "quarantinedItems": 2}}
    value = plan(artifact, source_key="svn", observed_at="2026-10-28T10:00:00Z")
    assert len(value["events"]) == 72
    assert [event["facts"][0]["value"] for event in value["events"][1:]] == rows
    assert all(event["external_id"] is None for event in value["events"])
    assert value["events"][0]["facts"][0]["value"]["summary"]["status"] == "partial"
    later = plan(artifact, source_key="svn", observed_at="2026-10-28T11:00:00Z")
    assert later["events"][0]["capture_id"] != value["events"][0]["capture_id"]
    assert plan(artifact, source_key="svn", observed_at="2026-10-28T10:00:00Z") == value


def test_tampering_after_admission_fails_even_if_embedded_digest_unchanged():
    value = plan(document("original"), source_key="public-documents")
    receipt = admission(value)
    validate_admission(value, receipt)
    value["events"][1]["extracted_text"] = "changed"
    with pytest.raises(ValueError, match="digest mismatch"):
        validate_admission(value, receipt)


def test_rehashed_event_source_cannot_bypass_top_level_source_policy():
    value = plan(document("original"), source_key="public-documents")
    value["events"][1]["source_key"] = "jll"
    value["plan_sha256"] = sha256(encoded({k: v for k, v in value.items() if k != "plan_sha256"}))
    with pytest.raises(ValueError, match="disagree"):
        validate_admission(value, admission(value))


@pytest.mark.parametrize("url", ["https://www.jll.com/a.pdf", "https://jll.com./a.pdf"])
def test_jll_document_host_is_not_admitted_by_generic_source_label(url):
    artifact = document("source text")
    artifact["source"]["url"] = url
    value = plan(artifact, source_key="public-documents")
    with pytest.raises(ValueError, match="JLL URL"):
        validate_admission(value, admission(value))


def test_credential_query_is_rejected_without_printing_value():
    artifact = document("source text")
    artifact["source"]["url"] = "https://example.org/file?X-Amz-Signature=private-value"
    with pytest.raises(ValueError, match="credential-bearing") as error:
        plan(artifact, source_key="public-documents")
    assert "private-value" not in str(error.value)


@pytest.mark.parametrize("text", [
    "See https://JLL.COM./property for details.",
    "[listing](HTTPS://Sub.JllProperty.com/path).",
    "Source: https://example.org/a?token=private-value; details follow",
])
def test_embedded_unsafe_url_is_refused(text):
    value = plan(document(text), source_key="public-documents")
    with pytest.raises(ValueError):
        validate_admission(value, admission(value))


def test_unsafe_url_in_object_key_is_refused():
    artifact = document("ordinary text")
    artifact["parser"]["config"] = {"See https://JLL.com/listing": "metadata"}
    value = plan(artifact, source_key="public-documents")
    with pytest.raises(ValueError):
        validate_admission(value, admission(value))


def test_safe_markdown_url_is_accepted():
    value = plan(document("Read [details](https://example.org/property)."), source_key="public-documents")
    validate_admission(value, admission(value))


@pytest.mark.parametrize("prefix,suffix", [
    ("https://jl", "l.com/property"),
    ("https://example.org/path?to", "ken=secret"),
])
def test_urls_split_across_text_chunks_are_refused(prefix, suffix):
    text = "x" * (60000 - len(prefix)) + prefix + suffix
    value = plan(document(text), source_key="public-documents")
    assert len(value["events"]) > 2
    with pytest.raises(ValueError):
        validate_admission(value, admission(value))


@pytest.mark.parametrize("key,source", [("Source_Key", "Avison_Young"), ("SOURCEKEY", "JLL"),
                                       ("source-key", "JLL_Investor")])
def test_nested_protected_source_declarations_refuse_mismatched_label(key, source):
    artifact = document("ordinary text")
    artifact["parser"]["config"] = {"nested": {key: source}}
    value = plan(artifact, source_key="svn")
    with pytest.raises(ValueError, match="conflicting protected source"):
        validate_admission(value, admission(value))


def test_source_withholding_policy_and_org_material_cannot_use_general_receipt():
    value = plan(document("not for promotion"), source_key="jll")
    with pytest.raises(ValueError, match="source-specific"):
        validate_admission(value, admission(value))
    value = plan(document("public text"), source_key="public-documents")
    receipt = admission(value)
    receipt["data_scope"] = "private_deal_document"
    with pytest.raises(ValueError, match="admission"):
        validate_admission(value, receipt)


def test_bad_digest_and_false_coverage_are_refused():
    original = document("source text")
    for field, value in [("text_sha256", "0" * 64), ("coverage", "complete")]:
        bad = copy.deepcopy(original)
        bad["extraction"][field] = value
        with pytest.raises(ValueError):
            plan(bad, source_key="public-documents")
    with pytest.raises(ValueError):
        split_text("x" * (4 * 1024 * 1024 + 1))
