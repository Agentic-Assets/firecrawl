"""Supervised text-only Poppler complement for explicitly eligible AnyDoc PDFs."""
import os
import re
import signal
import stat
import subprocess
import sys
from pathlib import Path

from cre_owned_processes import OwnedProcessGroup, settle_owned_groups
from source_evidence import automatic, doc_extract_process, events, private_export
from source_evidence.document_process import DocumentProcessCleanupUnknown

TIMEOUT_SECONDS = 140


def required(first, selection):
    if "pdf_complement" not in selection or not isinstance(first, dict) or first.get("source", {}).get("format") != "pdf":
        return False
    extraction = first.get("extraction", {})
    return (extraction.get("status") in ("extracted", "ocr_needed")
            or (extraction.get("status") == "failed" and extraction.get("error_code") == "unsupported"))


def retain_unresolved(first, complement, selection):
    return (selection["original_retention"] == "keep_unresolved_until_review"
            and (first.get("extraction", {}).get("status") != "extracted"
                 or (required(first, selection) and (not isinstance(complement, dict)
                     or complement.get("extraction", {}).get("status") != "extracted"
                     or "converter_failed_partial_output" in complement.get("extraction", {}).get("warnings", [])))))


def executable_identity(path):
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError("complement executable must be absolute")
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise ValueError("complement executable must be a regular executable, not a symlink")
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _best_effort_failed_registration(child):
    if child is None:
        return
    try:
        if child.poll() is None and os.getpgid(child.pid) == child.pid:
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=doc_extract_process.REAP_GRACE_SECONDS)
    except BaseException:
        pass


def validate(artifact, first, receipt, source_key, executable):
    first_source = first.get("source", {})
    extraction = artifact.get("extraction", {})
    parser = artifact.get("parser", {})
    if (not required(first, {"pdf_complement": {}})
            or artifact.get("schema_version") != "cre-document-text/v1"
            or artifact.get("source") != first_source or parser.get("name") != "poppler-pdftotext"
            or not isinstance(parser.get("version"), str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", parser["version"])
            or not isinstance(executable, str) or not os.path.isabs(executable)
            or parser.get("config") != {"executable": executable, "ocr": False, "timeout_seconds": 120,
                                        "max_output_file_bytes": 4 * 1024 * 1024, "max_text_bytes": 4 * 1024 * 1024}
            or extraction.get("relationship") != "complement_of"
            or extraction.get("previous_attempt_sha256") != events.sha256(events.encoded(first))
            or extraction.get("text_format") != "plain"):
        raise ValueError("invalid complementary parser artifact")
    status = first.get("extraction", {}).get("status")
    doc_extract_process.validate_artifact(first, receipt, source_key=source_key, exit_code=0 if status == "extracted" else 2)
    plan = events.plan(artifact, source_key=source_key)
    events.validate_content_policy(plan)
    return plan


def run(input_path, first, receipt, outdir, *, executable, source_key):
    status = first.get("extraction", {}).get("status")
    doc_extract_process.validate_artifact(first, receipt, source_key=source_key, exit_code=0 if status == "extracted" else 2)
    if not required(first, {"pdf_complement": {}}):
        raise ValueError("complement requires an eligible PDF")
    raw = automatic._read_private(input_path, 100 * 1024 * 1024)
    if len(raw) != receipt["body_bytes"] or events.sha256(raw) != receipt["body_sha256"]:
        raise ValueError("complement input disagrees with acquisition")
    identity = executable_identity(executable)
    private_export.write(outdir, [("anydoc.json", events.encoded(first))])
    adapter = Path(__file__).resolve().parent.parent / "document_text" / "ocr.py"
    output = outdir / "complement.json"
    command = [sys.executable, str(adapter), "--input", str(input_path), "--anydoc-attempt", str(outdir / "anydoc.json"),
               "--out", str(output), "--engine", "poppler", "--relationship", "complement", "--poppler-path", executable]
    child = None
    try:
        child = subprocess.Popen(command, cwd=str(adapter.parent), env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        owned = OwnedProcessGroup.from_spawn(child)
    except OSError as error:
        if child is None:
            raise ValueError("complement process could not start") from error
        _best_effort_failed_registration(child)
        raise DocumentProcessCleanupUnknown("complement ownership unavailable") from error
    except BaseException as error:
        _best_effort_failed_registration(child)
        raise DocumentProcessCleanupUnknown("complement spawn or ownership interrupted") from error
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
        raise DocumentProcessCleanupUnknown("complement settlement unavailable") from (interrupted or error)
    if not settlement.settled:
        raise DocumentProcessCleanupUnknown("complement settlement unknown") from interrupted
    if interrupted:
        raise interrupted
    if code not in (0, 2) or executable_identity(executable) != identity:
        raise ValueError("complement process outcome unknown")
    _, artifact = doc_extract_process._read_json(output, doc_extract_process.MAX_ARTIFACT_BYTES)
    validate(artifact, first, receipt, source_key, executable)
    if (artifact["extraction"]["status"] == "extracted") != (code == 0):
        raise ValueError("complement exit mismatch")
    return artifact
