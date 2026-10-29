"""Supervise the opt-in, bounded sparse PDF OCR attempt."""
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

from cre_owned_processes import OwnedProcessGroup, settle_owned_groups

from source_evidence import (
    automatic,
    doc_complement_process,
    doc_extract_process,
    events,
)
from source_evidence.document_process import DocumentProcessCleanupUnknown

TIMEOUT_SECONDS = 360
COMPLETION_SCHEMA = "cre-sparse-ocr-complete/v1"


def required(first, complement, selection):
    policy = selection.get("sparse_ocr")
    if not policy or not doc_complement_process.required(first, selection) or not isinstance(complement, dict):
        return False
    return policy["strategy"] == "all_pages" or not any(
        isinstance(item.get("extraction", {}).get("text"), str)
        and item["extraction"]["text"].strip() for item in (first, complement))


def retain_unresolved(first, complement, ocr, selection):
    if doc_complement_process.retain_unresolved(first, complement, selection):
        return True
    if selection["original_retention"] != "keep_unresolved_until_review" or not required(first, complement, selection):
        return False
    extraction = (ocr or {}).get("extraction", {})
    return (extraction.get("status") != "extracted" or extraction.get("omitted_pages") != 0
            or extraction.get("layout_omitted_pages") != 0
            or any(w != "coverage_unknown" for w in extraction.get("warnings", [])))


def validate(artifact, first, complement, receipt, source_key, policy):
    doc_complement_process.validate(complement, first, receipt, source_key,
                                    complement.get("parser", {}).get("config", {}).get("executable"))
    extraction = artifact.get("extraction", {})
    parser = artifact.get("parser", {})
    config = parser.get("config", {})
    worker = Path(__file__).resolve().parent.parent / "document_text" / "sparse_ocr_worker.py"
    fixed = {"image": "quay.io/docling-project/docling-serve-cpu@sha256:528f52f5ce56be1739df117560512ffaa483be53934bf5b0458b4ca088b1a6b0",
             "pypdfium2": "5.8.0", "pdfium": "149.0.7825.0", "tesseract": "4.1.1",
             "language": "eng", "psm": 11, "osd": False, "dpi": 200, "max_pixels_per_page": 25_000_000,
             "max_pages": policy["max_pages"], "per_page_timeout_seconds": 60, "timeout_seconds": 210,
             "max_text_bytes": 4 * 1024 * 1024, "max_layout_bytes": 1024 * 1024, "network": "none", "cpu": 1,
             "memory_bytes": 3 * 1024**3, "trigger_policy": policy["strategy"],
             "worker_sha256": events.sha256(worker.read_bytes())}
    if (artifact.get("schema_version") != "cre-document-text/v1" or artifact.get("source") != first.get("source")
            or parser.get("name") != "pdfium-tesseract-sparse" or parser.get("version") != "1"
            or config != fixed
            or extraction.get("relationship") != "complement_of"
            or extraction.get("previous_attempt_sha256") != events.sha256(events.encoded(complement))
            or extraction.get("text_format") != "plain" or "container_cleanup_unconfirmed" in extraction.get("warnings", [])):
        raise ValueError("invalid sparse OCR artifact")
    pages, count, omitted = extraction.get("pages"), extraction.get("page_count"), extraction.get("omitted_pages")
    if (not isinstance(pages, list) or len(pages) > policy["max_pages"]
            or (count is None and (pages or omitted is not None))
            or (count is not None and (type(count) is not int or count < len(pages)
                or type(omitted) is not int or omitted != count - len(pages)))
            or any(not isinstance(p, dict) or p.get("page") != i for i, p in enumerate(pages, 1))):
        raise ValueError("invalid sparse OCR page accounting")
    layout_omitted = extraction.get("layout_omitted_pages")
    if (count is None and layout_omitted is not None) or (count is not None and (
            type(layout_omitted) is not int or not 0 <= layout_omitted <= count)):
        raise ValueError("invalid sparse OCR layout accounting")
    layout_size = 0
    missing_layout = 0
    text_end = 0
    discarded = "whitespace_only_text_discarded" in extraction.get("warnings", [])
    if discarded and (extraction.get("status") == "extracted" or extraction.get("text") is not None):
        raise ValueError("invalid discarded OCR whitespace")
    text_raw = extraction.get("text", "")
    text_raw = text_raw.encode("utf-8") if isinstance(text_raw, str) else b""
    for page in pages:
        start, end = page.get("text_start_byte"), page.get("text_end_byte")
        if type(start) is not int or type(end) is not int or start != text_end or end < start:
            raise ValueError("invalid OCR page text boundaries")
        page_bytes = page.get("text_bytes", 0)
        if (type(page_bytes) is not int or page_bytes < 0
                or (not discarded and page_bytes != end - start)
                or (discarded and (start != 0 or end != 0))):
            raise ValueError("invalid OCR page text bytes")
        if not discarded:
            try:
                text_raw[start:end].decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("OCR page splits a UTF-8 character") from error
        text_end = end
        layout = page.get("layout_tsv")
        if layout is None:
            if "layout_sha256" in page:
                raise ValueError("orphan OCR layout hash")
            missing_layout += 1
        else:
            if not isinstance(layout, str):
                raise ValueError("invalid OCR layout")
            raw_layout = layout.encode("utf-8")
            if (page.get("layout_bytes") != len(raw_layout)
                    or page.get("layout_sha256") != events.sha256(raw_layout)):
                raise ValueError("OCR layout hash or size mismatch")
            layout_size += len(raw_layout)
            if page.get("layout_error"):
                missing_layout += 1
    text = extraction.get("text")
    if text_end != (len(text.encode("utf-8")) if isinstance(text, str) else 0):
        raise ValueError("OCR page boundaries disagree with text")
    metadata_size = len(json.dumps(pages, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    warnings = extraction.get("warnings")
    if (not isinstance(warnings, list) or any(not isinstance(warning, str) for warning in warnings)
            or "coverage_unknown" not in warnings
            or (omitted and "pages_omitted" not in warnings)
            or (any(page.get("error") or page.get("worker_exit") != 0 for page in pages)
                and "page_failure_or_partial_output" not in warnings)
            or (extraction.get("converter_exit_code") not in (None, 0)
                and "worker_failed_partial_output" not in warnings)
            or (extraction.get("status") == "extracted" and extraction.get("converter_exit_code") is None
                and "worker_timeout_partial_output" not in warnings)):
        raise ValueError("invalid sparse OCR warnings")
    if (metadata_size > 2 * 1024 * 1024 or text_end + layout_size > 4 * 1024 * 1024
            or layout_size > 1024 * 1024 or (count is not None and layout_omitted != missing_layout + count - len(pages))
            or (layout_omitted and "layout_omitted" not in extraction.get("warnings", []))):
        raise ValueError("invalid OCR layout bounds or omission")
    plan = events.plan(artifact, source_key=source_key)
    events.validate_content_policy(plan)
    return plan


def run(input_path, first, complement, receipt, outdir, *, source_key, policy, prior_dir):
    raw = automatic._read_private(input_path, 100 * 1024 * 1024)
    if len(raw) != receipt["body_bytes"] or events.sha256(raw) != receipt["body_sha256"]:
        raise ValueError("sparse OCR input disagrees with acquisition")
    adapter = Path(__file__).resolve().parent.parent / "document_text" / "sparse_ocr.py"
    command = [sys.executable, str(adapter), "--input", str(input_path),
               "--anydoc-attempt", str(prior_dir / "converter-artifact.json"),
               "--poppler-attempt", str(prior_dir / "complement-converter-artifact.json"),
               "--out-dir", str(outdir), "--max-pages", str(policy["max_pages"]),
               "--trigger-policy", policy["strategy"]]
    child = None
    try:
        child = subprocess.Popen(command, cwd=str(adapter.parent), env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        owned = OwnedProcessGroup.from_spawn(child)
    except BaseException as error:
        doc_complement_process._best_effort_failed_registration(child)
        if child is None and isinstance(error, OSError):
            raise ValueError("sparse OCR process could not start") from error
        raise DocumentProcessCleanupUnknown("sparse OCR ownership unavailable") from error
    interrupted = None
    try:
        code = child.wait(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        code = None
    except BaseException as error:
        interrupted, code = error, None
    try:
        settlement = settle_owned_groups([owned], grace_seconds=0, initial_signal=signal.SIGKILL,
                                         kill_seconds=doc_extract_process.REAP_GRACE_SECONDS)
    except BaseException as error:
        raise DocumentProcessCleanupUnknown("sparse OCR settlement unavailable") from (interrupted or error)
    if not settlement.settled or code is None:
        raise DocumentProcessCleanupUnknown("sparse OCR process or container settlement unknown") from interrupted
    if interrupted:
        raise interrupted
    if code not in (0, 2):
        raise DocumentProcessCleanupUnknown("sparse OCR process or container outcome unknown")
    try:
        raw, artifact = doc_extract_process._read_json(outdir / "sparse-ocr.json", doc_extract_process.MAX_ARTIFACT_BYTES)
        if "container_cleanup_unconfirmed" in artifact.get("extraction", {}).get("warnings", []):
            raise DocumentProcessCleanupUnknown("sparse OCR container cleanup unknown")
        settlement_sha = _verify_settlement(outdir, artifact, first, complement, receipt)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise DocumentProcessCleanupUnknown("sparse OCR output or settlement unavailable") from error
    validate(artifact, first, complement, receipt, source_key, policy)
    if (artifact["extraction"]["status"] == "extracted") != (code == 0):
        raise ValueError("sparse OCR exit mismatch")
    identity = doc_extract_process._directory_identity(outdir)
    _verify_inputs(input_path, prior_dir, first, complement, receipt)
    marker = _completion_marker(raw, artifact, first, complement, receipt, source_key, policy, code, settlement_sha)
    _publish_completion(outdir, marker, identity)
    return artifact


def _completion_marker(raw, artifact, first, complement, receipt, source_key, policy, code, settlement_sha):
    return {"schema_version": COMPLETION_SCHEMA, "artifact_sha256": events.sha256(raw),
            "anydoc_sha256": events.sha256(events.encoded(first)),
            "complement_sha256": events.sha256(events.encoded(complement)),
            "receipt_sha256": events.sha256(events.encoded(receipt)), "source_key": source_key,
            "policy_sha256": events.sha256(events.encoded(policy)), "exit_code": code,
            "worker_sha256": artifact["parser"]["config"]["worker_sha256"], "settlement_sha256": settlement_sha}


def _verify_settlement(outdir, artifact, first, complement, receipt):
    try:
        intent_raw, intent = doc_extract_process._read_json(outdir / "owner-intent.json", 16384)
        settled_raw, settled = doc_extract_process._read_json(outdir / "owner-settled.json", 16384)
    except (OSError, ValueError, TypeError) as error:
        raise DocumentProcessCleanupUnknown("OCR container settlement receipt unavailable") from error
    nonce = intent.get("nonce") if isinstance(intent, dict) else None
    if (not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce)
            or intent != {"schema_version": "cre-sparse-ocr-owner/v1", "name": "cre-sparse-" + nonce,
                "nonce": nonce, "source_sha256": receipt["body_sha256"],
                "prior_attempt_sha256": [events.sha256(events.encoded(first)), events.sha256(events.encoded(complement))],
                "worker_sha256": artifact["parser"]["config"]["worker_sha256"],
                "config_sha256": events.sha256(events.encoded(artifact["parser"]["config"])), "uid": os.getuid()}
            or not isinstance(settled, dict) or set(settled) != {"schema_version", "intent_sha256", "container_id", "state"}
            or settled.get("schema_version") != "cre-sparse-ocr-settlement/v1"
            or settled.get("intent_sha256") != events.sha256(intent_raw) or settled.get("state") != "removed_verified"
            or not isinstance(settled.get("container_id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", settled["container_id"])):
        raise DocumentProcessCleanupUnknown("OCR container settlement receipt invalid or missing")
    return events.sha256(settled_raw)


def _verify_inputs(input_path, prior_dir, first, complement, receipt):
    body = automatic._read_private(input_path, receipt["body_bytes"])
    if len(body) != receipt["body_bytes"] or events.sha256(body) != receipt["body_sha256"]:
        raise ValueError("OCR input changed")
    for name, expected in (("converter-artifact.json", first), ("complement-converter-artifact.json", complement)):
        raw = automatic._read_private(prior_dir / name, doc_extract_process.MAX_ARTIFACT_BYTES)
        if raw != events.encoded(expected):
            raise ValueError("OCR prior artifact changed")


def _publish_completion(outdir, marker, identity):
    from source_evidence import private_export
    if doc_extract_process._directory_identity(outdir) != identity:
        raise ValueError("OCR output directory changed")
    private_export.write(outdir / "ocr-completion", [("marker.json", events.encoded(marker))])
    if doc_extract_process._directory_identity(outdir) != identity:
        raise ValueError("OCR output directory changed")


def replay(input_path, first, complement, receipt, outdir, *, source_key, policy, prior_dir):
    """Verify the coordinator's settled marker and exact inputs without starting a process."""
    identity = doc_extract_process._directory_identity(outdir)
    completion = outdir / "ocr-completion"
    completion_identity = doc_extract_process._directory_identity(completion)
    if set(os.listdir(completion)) != {"marker.json"}:
        raise ValueError("OCR completion file set changed")
    _, marker = doc_extract_process._read_json(completion / "marker.json", 16384)
    raw, artifact = doc_extract_process._read_json(outdir / "sparse-ocr.json", doc_extract_process.MAX_ARTIFACT_BYTES)
    _verify_inputs(input_path, prior_dir, first, complement, receipt)
    validate(artifact, first, complement, receipt, source_key, policy)
    code = 0 if artifact["extraction"]["status"] == "extracted" else 2
    settlement_sha = _verify_settlement(outdir, artifact, first, complement, receipt)
    if (marker != _completion_marker(raw, artifact, first, complement, receipt, source_key, policy, code, settlement_sha)
            or doc_extract_process._directory_identity(outdir) != identity
            or doc_extract_process._directory_identity(completion) != completion_identity
            or set(os.listdir(completion)) != {"marker.json"}):
        raise ValueError("OCR completion marker or artifact changed")
    return artifact
