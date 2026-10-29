"""Supervise one policy-bound local Docling JSON supplement."""
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

TIMEOUT_SECONDS = 180
COMPLETION_SCHEMA = "cre-docling-json-complete/v1"


def required(first, complement, selection):
    return ("docling_json" in selection and doc_complement_process.required(first, selection)
            and isinstance(complement, dict))


def retain_unresolved(first, complement, docling, selection):
    if selection["original_retention"] != "keep_unresolved_until_review" or not required(first, complement, selection):
        return False
    extraction = (docling or {}).get("extraction", {})
    return (extraction.get("status") != "extracted" or extraction.get("omitted_pages") != 0
            or extraction.get("unrepresented_requested_pages") != 0
            or any(w != "coverage_unknown" for w in extraction.get("warnings", [])))


def validate(artifact, first, complement, receipt, source_key, policy, *, require_current_worker=True):
    from document_text.docling_json import artifact as native
    doc_complement_process.validate(complement, first, receipt, source_key,
        complement.get("parser", {}).get("config", {}).get("executable"))
    parser, extraction = artifact.get("parser", {}), artifact.get("extraction", {})
    config = parser.get("config", {})
    worker = Path(__file__).resolve().parent.parent / "document_text" / "docling_json" / "worker.py"
    worker_sha = config.get("worker_sha256") if isinstance(config, dict) else None
    expected = {"image": "quay.io/docling-project/docling-serve-cpu@sha256:528f52f5ce56be1739df117560512ffaa483be53934bf5b0458b4ca088b1a6b0",
        "worker_sha256": worker_sha, "network": "none", "device": "cpu", "cpu": 1, "memory_bytes": 3 * 1024**3,
        "max_pages": policy["max_pages"], "max_text_bytes": events.MAX_TEXT_BYTES, "document_timeout_seconds": 90,
        "client_timeout_seconds": 120, "ocr": False, "tables": True, "table_mode": "accurate", "generated_images": False}
    if (not isinstance(config, dict) or not isinstance(policy, dict) or type(policy.get("max_pages")) is not int
            or not 1 <= policy["max_pages"] <= 1000
            or any(type(config.get(k)) is not bool for k in ("ocr", "tables", "generated_images"))
            or any(type(config.get(k)) is not int for k in ("cpu", "memory_bytes", "max_pages",
                                                          "max_text_bytes", "document_timeout_seconds", "client_timeout_seconds"))
            or not isinstance(worker_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", worker_sha)
            or (require_current_worker and worker_sha != events.sha256(worker.read_bytes())) or config != expected
            or artifact.get("source") != first.get("source") or artifact.get("schema_version") != "cre-document-text/v1"
            or parser.get("name") != "docling-document-json" or parser.get("version") != "2.95.0"
            or extraction.get("text_format") != "docling_json" or extraction.get("coverage") != "unknown"
            or ("mode" in policy and policy["mode"] != "docling_json_v1")
            or extraction.get("relationship") != "complement_of"
            or extraction.get("prior_attempt_sha256") != [events.sha256(events.encoded(first)), events.sha256(events.encoded(complement))]
            or extraction.get("previous_attempt_sha256") != events.sha256(events.encoded(complement))):
        raise ValueError("invalid Docling JSON artifact binding")
    if extraction.get("status") == "extracted":
        text = extraction.get("text")
        if (not isinstance(text, str) or len(text.encode("utf-8")) > events.MAX_TEXT_BYTES
                or events.sha256(text.encode()) != extraction.get("text_sha256")
                or extraction.get("error_code") is not None or type(extraction.get("converter_exit_code")) is not int
                or extraction.get("converter_exit_code") != 0):
            raise ValueError("invalid Docling JSON text")
        count, interval = extraction.get("page_count"), extraction.get("page_range")
        if (type(count) is not int or count < 1 or not isinstance(interval, list) or len(interval) != 2
                or type(interval[0]) is not int or interval[0] != 1 or type(interval[1]) is not int or interval[1] != min(count, policy["max_pages"])):
            raise ValueError("invalid Docling JSON page scope")
        _, missing = native.validate_native(text.encode(), first_page=1, last_page=interval[1])
        warnings = extraction.get("warnings")
        expected = ["coverage_unknown"] + (["pages_omitted"] if count > interval[1] else [])
        if "converter_partial" in (warnings or []):
            expected.append("converter_partial")
        if missing:
            expected.append("requested_pages_unrepresented")
        if (type(extraction.get("omitted_pages")) is not int or extraction.get("omitted_pages") != count - interval[1]
                or type(extraction.get("unrepresented_requested_pages")) is not int
                or extraction.get("unrepresented_requested_pages") != missing or warnings != expected):
            raise ValueError("invalid Docling JSON page accounting")
    elif (extraction.get("status") != "failed" or extraction.get("text") is not None
          or extraction.get("text_sha256") is not None or not isinstance(extraction.get("error_code"), str)
          or any(extraction.get(k) is not None for k in ("page_count", "page_range", "omitted_pages", "unrepresented_requested_pages"))
          or not isinstance(extraction.get("warnings"), list)
          or any(not isinstance(w, str) for w in extraction.get("warnings", []))):
        raise ValueError("invalid Docling JSON failure")
    plan = events.plan(artifact, source_key=source_key)
    events.validate_content_policy(plan)
    return plan


def run(input_path, first, complement, receipt, outdir, *, source_key, policy, prior_dir):
    raw = automatic._read_private(input_path, 100 * 1024 * 1024)
    if len(raw) != receipt["body_bytes"] or events.sha256(raw) != receipt["body_sha256"]:
        raise ValueError("Docling JSON input disagrees with acquisition")
    adapter = Path(__file__).resolve().parent.parent / "document_text" / "docling_json" / "adapter.py"
    command = [sys.executable, str(adapter), "--input", str(input_path),
               "--anydoc-attempt", str(prior_dir / "converter-artifact.json"),
               "--poppler-attempt", str(prior_dir / "complement-converter-artifact.json"),
               "--out-dir", str(outdir), "--max-pages", str(policy["max_pages"])]
    child = None
    try:
        child = subprocess.Popen(command, cwd=str(adapter.parent), env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        owned = OwnedProcessGroup.from_spawn(child)
    except BaseException as error:
        doc_complement_process._best_effort_failed_registration(child)
        if child is None and isinstance(error, OSError):
            raise ValueError("Docling JSON process could not start") from error
        raise DocumentProcessCleanupUnknown("Docling JSON ownership unavailable") from error
    interrupted = None
    try:
        code = child.wait(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        code = None
    except BaseException as error:  # noqa: BLE001 - settlement must run on interruption.
        interrupted, code = error, None
    try:
        settlement = settle_owned_groups([owned], grace_seconds=0, initial_signal=signal.SIGKILL,
                                         kill_seconds=doc_extract_process.REAP_GRACE_SECONDS)
    except BaseException as error:  # noqa: BLE001 - uncertainty must remain fenced.
        raise DocumentProcessCleanupUnknown("Docling JSON settlement unavailable") from (interrupted or error)
    if not settlement.settled or code is None:
        raise DocumentProcessCleanupUnknown("Docling JSON process or container settlement unknown") from interrupted
    if interrupted:
        raise interrupted
    if type(code) is not int or code not in (0, 2):
        raise DocumentProcessCleanupUnknown("Docling JSON process or container outcome unknown")
    try:
        raw, artifact = doc_extract_process._read_json(outdir / "docling-json.json", doc_extract_process.MAX_ARTIFACT_BYTES)
        if "container_cleanup_unconfirmed" in artifact.get("extraction", {}).get("warnings", []):
            raise DocumentProcessCleanupUnknown("Docling JSON container cleanup unknown")
        settlement_sha = _verify_settlement(outdir, artifact, first, complement, receipt)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise DocumentProcessCleanupUnknown("Docling JSON output or settlement unavailable") from error
    validate(artifact, first, complement, receipt, source_key, policy)
    if (artifact["extraction"]["status"] == "extracted") != (code == 0):
        raise ValueError("Docling JSON exit mismatch")
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
        raise DocumentProcessCleanupUnknown("Docling JSON container settlement receipt unavailable") from error
    nonce = intent.get("nonce") if isinstance(intent, dict) else None
    if (not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce)
            or events.encoded(intent) != events.encoded({"schema_version": "cre-docling-json-owner/v1", "name": "cre-docling-json-" + nonce,
                "nonce": nonce, "source_sha256": receipt["body_sha256"],
                "prior_attempt_sha256": [events.sha256(events.encoded(first)), events.sha256(events.encoded(complement))],
                "worker_sha256": artifact["parser"]["config"]["worker_sha256"],
                "config_sha256": events.sha256(events.encoded(artifact["parser"]["config"])), "uid": os.getuid()})
            or not isinstance(settled, dict) or set(settled) != {"schema_version", "intent_sha256", "container_id", "state"}
            or settled.get("schema_version") != "cre-docling-json-settlement/v1"
            or settled.get("intent_sha256") != events.sha256(intent_raw) or settled.get("state") != "removed_verified"
            or not isinstance(settled.get("container_id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", settled["container_id"])):
        raise DocumentProcessCleanupUnknown("Docling JSON container settlement receipt invalid or missing")
    return events.sha256(settled_raw)


def _verify_inputs(input_path, prior_dir, first, complement, receipt):
    body = automatic._read_private(input_path, receipt["body_bytes"])
    if len(body) != receipt["body_bytes"] or events.sha256(body) != receipt["body_sha256"]:
        raise ValueError("Docling JSON input changed")
    for name, expected in (("converter-artifact.json", first), ("complement-converter-artifact.json", complement)):
        raw = automatic._read_private(prior_dir / name, doc_extract_process.MAX_ARTIFACT_BYTES)
        if raw != events.encoded(expected):
            raise ValueError("Docling JSON prior artifact changed")


def _publish_completion(outdir, marker, identity):
    from source_evidence import private_export
    if doc_extract_process._directory_identity(outdir) != identity:
        raise ValueError("Docling JSON output directory changed")
    private_export.write(outdir / "docling-completion", [("marker.json", events.encoded(marker))])
    if doc_extract_process._directory_identity(outdir) != identity:
        raise ValueError("Docling JSON output directory changed")


def replay(input_path, first, complement, receipt, outdir, *, source_key, policy, prior_dir):
    """Verify the coordinator's settled marker and exact inputs without starting a process."""
    identity = doc_extract_process._directory_identity(outdir)
    completion = outdir / "docling-completion"
    completion_identity = doc_extract_process._directory_identity(completion)
    if set(os.listdir(completion)) != {"marker.json"}:
        raise ValueError("Docling JSON completion file set changed")
    _, marker = doc_extract_process._read_json(completion / "marker.json", 16384)
    raw, artifact = doc_extract_process._read_json(outdir / "docling-json.json", doc_extract_process.MAX_ARTIFACT_BYTES)
    _verify_inputs(input_path, prior_dir, first, complement, receipt)
    validate(artifact, first, complement, receipt, source_key, policy)
    code = 0 if artifact["extraction"]["status"] == "extracted" else 2
    settlement_sha = _verify_settlement(outdir, artifact, first, complement, receipt)
    if (events.encoded(marker) != events.encoded(_completion_marker(raw, artifact, first, complement, receipt, source_key, policy, code, settlement_sha))
            or doc_extract_process._directory_identity(outdir) != identity
            or doc_extract_process._directory_identity(completion) != completion_identity
            or set(os.listdir(completion)) != {"marker.json"}):
        raise ValueError("Docling JSON completion marker or artifact changed")
    return artifact
