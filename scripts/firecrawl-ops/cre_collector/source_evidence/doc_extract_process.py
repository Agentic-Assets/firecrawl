"""Supervised local AnyDoc bridge for a completed private fetch.

This bounds elapsed time for the process group, not native memory or CPU.
The caller owns policy revalidation and the retained copies in the workdir.
"""

import os
import re
import signal
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from cre_owned_processes import OwnedProcessGroup, settle_owned_groups
from source_evidence import (acquisition, automatic, doc_fetch_process, document_acquisition,
                             document_fetch_worker, events, private_export)
from source_evidence.document_process import DocumentProcessCleanupUnknown

MAX_ARTIFACT_BYTES = 6 * 4 * 1024 * 1024 + 65536
MAX_TIMEOUT_SECONDS = 120
REAP_GRACE_SECONDS = 5
COMPLETION_SCHEMA = "cre-document-conversion-complete/v1"
_FORMATS = {".pdf", ".doc", ".docx", ".docm", ".ppt", ".pps", ".pot", ".pptx", ".pptm",
            ".ppsx", ".ppsm", ".xls", ".xlsx", ".xlsm", ".xlsb", ".odt", ".ods", ".odp",
            ".rtf", ".epub", ".csv"}


def input_filename(receipt):
    """Derive the existing bounded local format hint from the observed URL."""
    suffix = Path(urlsplit(receipt["final_url"]).path).suffix.lower()
    return "document" + (suffix if suffix in _FORMATS else ".bin")


def _read_json(path, limit):
    raw = automatic._read_private(path, limit)
    if len(raw) > limit:
        raise ValueError("private document input exceeds bound")
    value, literals = acquisition.decode_json(raw, max_bytes=limit)
    if literals:
        raise ValueError("unexpected document numeric literals")
    return raw, value


def _directory_identity(path):
    fd = doc_fetch_process._private_directory(path)
    try:
        info = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_uid, item.st_mode)
        if identity(info) != identity(named):
            raise ValueError("private extraction directory changed")
        return identity(info)
    finally:
        os.close(fd)


def _fetch_binding(fetch_result, job_path, outdir, source_key):
    if (not isinstance(fetch_result, dict) or fetch_result.get("state") != "completed"
            or not isinstance(source_key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", source_key)):
        raise ValueError("invalid extraction request")
    job_path, outdir = Path(os.path.abspath(job_path)), Path(os.path.abspath(outdir))
    fetch_dir = Path(fetch_result.get("directory", ""))
    if not fetch_dir.is_absolute() or fetch_dir.parent != job_path.parent or outdir.parent != job_path.parent:
        raise ValueError("fetch and extraction paths must share the attempt directory")
    if outdir in (fetch_dir, job_path):
        raise ValueError("extraction directory must be distinct")
    parent = doc_fetch_process._private_directory(job_path.parent)
    fetch_fd = None
    try:
        fetch_fd = doc_fetch_process._private_directory(fetch_dir)
        before = os.fstat(fetch_fd)
        job_raw, job = _read_json(job_path, 16384)
        document_fetch_worker.validate_job(job)
        digest = events.sha256(job_raw)
        if digest != fetch_result.get("job_sha256"):
            raise ValueError("fetch job binding changed")
        _, wrapper = _read_json(fetch_dir / "receipt.json", 65536)
        if (not isinstance(wrapper, dict) or set(wrapper) != {"job_sha256", "receipt"}
                or wrapper["job_sha256"] != digest or events.encoded(wrapper["receipt"]) != events.encoded(fetch_result.get("receipt"))):
            raise ValueError("fetch receipt binding changed")
        receipt = wrapper["receipt"]
        body_path = fetch_dir / "document.bin"
        if fetch_result.get("body_path") != str(body_path) or receipt.get("outcome") != "acquired":
            raise ValueError("a completed acquired body is required")
        body = automatic._read_private(body_path, job["max_bytes"])
        if len(body) > job["max_bytes"]:
            raise ValueError("fetch body exceeds bound")
        selection = {"allowed_hosts": job["allowed_hosts"], "limits": {"max_redirects": job["max_redirects"],
                     "max_document_bytes": job["max_bytes"], "max_total_bytes": job["max_bytes"]}}
        document_acquisition._receipt_checked(receipt, body, {"url": job["url"]}, selection)
        named = os.stat(fetch_dir.name, dir_fd=parent, follow_symlinks=False)
        after = os.fstat(fetch_fd)
        identity = lambda info: (info.st_dev, info.st_ino, info.st_uid, info.st_mode)
        if identity(before) != identity(after) or identity(after) != identity(named):
            raise ValueError("fetch directory changed during readback")
        input_name = input_filename(receipt)
        provenance = events.encoded({"url": receipt["final_url"], "observed_at": receipt["finished_at"]})
        return outdir, receipt, body, digest, input_name, provenance
    finally:
        if fetch_fd is not None:
            os.close(fetch_fd)
        os.close(parent)


def validate_artifact(artifact, receipt, *, source_key, exit_code):
    """Validate source and converter outcome without running a converter."""
    if type(exit_code) is not int or exit_code not in (0, 2):
        raise ValueError("invalid converter exit code")
    try:
        source = artifact["source"]
        parser = artifact.get("parser", {})
        source_binding = {key: source.get(key) for key in ("url", "observed_at", "sha256", "byte_length")}
        expected_binding = {"url": receipt["final_url"], "observed_at": receipt["finished_at"],
                            "sha256": receipt["body_sha256"], "byte_length": receipt["body_bytes"]}
        if (events.encoded(source_binding) != events.encoded(expected_binding)
                or parser.get("name") != "@firecrawl/anydoc" or parser.get("version") != "0.2.4"
                or parser.get("config", {}).get("ocr") != "disabled"):
            raise ValueError("extraction disagrees with acquired source")
        planned = events.plan(artifact, source_key=source_key)
        events.validate_content_policy(planned)
        status = artifact["extraction"]["status"]
        if (status == "extracted") != (exit_code == 0) or status not in ("extracted", "ocr_needed", "failed"):
            raise ValueError("converter outcome does not match exit")
        return "success" if status == "extracted" else status
    except (KeyError, TypeError, AttributeError, RecursionError) as exc:
        raise ValueError("invalid document extraction artifact") from exc


def _input_checked(outdir, input_name, body):
    observed = automatic._read_private(outdir / input_name, len(body))
    if len(observed) != len(body) or events.sha256(observed) != events.sha256(body):
        raise ValueError("private conversion input changed")


def _publish_marker(outdir, marker, expected_identity):
    directory = doc_fetch_process._private_directory(outdir)
    try:
        info = os.fstat(directory)
        if (info.st_dev, info.st_ino, info.st_uid, info.st_mode) != expected_identity:
            raise ValueError("private extraction directory changed")
        fd = os.open("conversion-complete.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as stream:
            stream.write(events.encoded(marker))
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
        if _directory_identity(outdir) != expected_identity:
            raise ValueError("private extraction directory changed")
    finally:
        os.close(directory)


def run(fetch_result, job_path, outdir, *, node_executable, source_key, timeout_seconds=75):
    """Extract once from a rebound fetch; outcomes are not publisher assertions."""
    if (not isinstance(node_executable, str) or not os.path.isabs(node_executable)
            or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS):
        raise ValueError("invalid extraction request")
    outdir, receipt, body, digest, input_name, provenance = _fetch_binding(fetch_result, job_path, outdir, source_key)
    private_export.write(outdir, [(input_name, body), ("provenance.json", provenance)])
    workdir_identity = _directory_identity(outdir)
    artifact_path = outdir / "artifact.json"
    adapter = Path(__file__).resolve().parent.parent / "document_text" / "extract.mjs"
    command = [node_executable, str(adapter), str(outdir / input_name), str(outdir / "provenance.json"), str(artifact_path)]
    child = None
    try:
        child = subprocess.Popen(command, cwd=str(adapter.parent), env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        owned = OwnedProcessGroup.from_spawn(child)
    except OSError as error:
        if child is None:
            return {"state": "ambiguous", "directory": str(outdir)}
        raise DocumentProcessCleanupUnknown("document process ownership unavailable") from error
    except BaseException as error:
        raise DocumentProcessCleanupUnknown("document process spawn or ownership interrupted") from error

    timed_out = False
    interrupted = None
    try:
        code = child.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        code = None
    except BaseException as error:
        interrupted = error
        code = None
    try:
        settlement = settle_owned_groups([owned], grace_seconds=0, initial_signal=signal.SIGKILL,
                                         kill_seconds=REAP_GRACE_SECONDS)
    except BaseException as error:
        raise DocumentProcessCleanupUnknown("document process settlement unavailable") from (interrupted or error)
    if not settlement.settled:
        raise DocumentProcessCleanupUnknown("document process settlement unknown") from interrupted
    if interrupted is not None:
        raise interrupted
    if timed_out:
        return {"state": "timeout", "directory": str(outdir)}
    if code not in (0, 2):
        return {"state": "ambiguous", "directory": str(outdir)}
    try:
        if _directory_identity(outdir) != workdir_identity:
            raise ValueError("private extraction directory changed")
        raw, artifact = _read_json(artifact_path, MAX_ARTIFACT_BYTES)
        if _directory_identity(outdir) != workdir_identity:
            raise ValueError("private extraction directory changed")
        state = validate_artifact(artifact, receipt, source_key=source_key, exit_code=code)
        _input_checked(outdir, input_name, body)
        marker = {"schema_version": COMPLETION_SCHEMA, "job_sha256": digest, "source_key": source_key,
                  "artifact_sha256": events.sha256(raw), "exit_code": code, "input_filename": input_name}
        _publish_marker(outdir, marker, workdir_identity)
    except (ValueError, OSError, KeyError, TypeError, AttributeError, RecursionError):
        return {"state": "ambiguous", "directory": str(outdir)}
    return {"state": state, "directory": str(outdir),
            "artifact_path": str(artifact_path), "artifact": artifact, "input_path": str(outdir / input_name)}


def replay(fetch_result, job_path, outdir, *, source_key):
    """Revalidate a settled conversion marker without executing a converter."""
    outdir, receipt, body, digest, input_name, _ = _fetch_binding(fetch_result, job_path, outdir, source_key)
    initial = _directory_identity(outdir)
    _, marker = _read_json(outdir / "conversion-complete.json", 16384)
    keys = {"schema_version", "job_sha256", "source_key", "artifact_sha256", "exit_code", "input_filename"}
    if (not isinstance(marker, dict) or set(marker) != keys or marker["schema_version"] != COMPLETION_SCHEMA
            or marker["job_sha256"] != digest or marker["source_key"] != source_key
            or marker["input_filename"] != input_name or not re.fullmatch(r"document\.[a-z0-9]{1,5}", input_name)):
        raise ValueError("invalid conversion completion marker")
    artifact_path = outdir / "artifact.json"
    raw, artifact = _read_json(artifact_path, MAX_ARTIFACT_BYTES)
    if events.sha256(raw) != marker["artifact_sha256"]:
        raise ValueError("conversion artifact digest mismatch")
    _input_checked(outdir, input_name, body)
    state = validate_artifact(artifact, receipt, source_key=source_key, exit_code=marker["exit_code"])
    if _directory_identity(outdir) != initial:
        raise ValueError("private extraction directory changed")
    return {"state": state, "directory": str(outdir), "artifact_path": str(artifact_path),
            "artifact": artifact, "input_path": str(outdir / input_name)}
