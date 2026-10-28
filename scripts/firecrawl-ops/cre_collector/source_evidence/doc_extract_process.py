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

from source_evidence import (acquisition, automatic, doc_fetch_process, document_acquisition,
                             document_fetch_worker, events, private_export)

MAX_ARTIFACT_BYTES = 6 * 4 * 1024 * 1024 + 65536
MAX_TIMEOUT_SECONDS = 120
REAP_GRACE_SECONDS = 5
_FORMATS = {".pdf", ".doc", ".docx", ".docm", ".ppt", ".pps", ".pot", ".pptx", ".pptm",
            ".ppsx", ".ppsm", ".xls", ".xlsx", ".xlsm", ".xlsb", ".odt", ".ods", ".odp",
            ".rtf", ".epub", ".csv"}


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


def run(fetch_result, job_path, outdir, *, node_executable, source_key, timeout_seconds=75):
    """Extract once from a rebound fetch; outcomes are not publisher assertions."""
    if (not isinstance(fetch_result, dict) or fetch_result.get("state") != "completed"
            or not isinstance(node_executable, str) or not os.path.isabs(node_executable)
            or not isinstance(source_key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", source_key)
            or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS):
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
        suffix = Path(urlsplit(receipt["final_url"]).path).suffix.lower()
        suffix = suffix if suffix in _FORMATS else ".bin"
        input_name = "document" + suffix
        provenance = events.encoded({"url": receipt["final_url"], "observed_at": receipt["finished_at"]})
        private_export.write(outdir, [(input_name, body), ("provenance.json", provenance)])
    finally:
        if fetch_fd is not None:
            os.close(fetch_fd)
        os.close(parent)

    workdir_identity = _directory_identity(outdir)
    artifact_path = outdir / "artifact.json"
    adapter = Path(__file__).resolve().parent.parent / "document_text" / "extract.mjs"
    command = [node_executable, str(adapter), str(outdir / input_name), str(outdir / "provenance.json"), str(artifact_path)]
    try:
        child = subprocess.Popen(command, cwd=str(adapter.parent), env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
    except OSError:
        return {"state": "ambiguous", "directory": str(outdir)}

    timed_out = False
    process_error = False
    interrupted = None
    try:
        code = child.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        code = None
    except BaseException as error:
        process_error = True
        interrupted = error
        code = None
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        process_error = True
    if code is None:
        try:
            child.wait(timeout=REAP_GRACE_SECONDS)
        except (subprocess.TimeoutExpired, OSError):
            process_error = True
    if interrupted is not None:
        raise interrupted
    if process_error:
        return {"state": "ambiguous", "directory": str(outdir)}
    if timed_out:
        return {"state": "timeout", "directory": str(outdir)}
    if code not in (0, 2):
        return {"state": "ambiguous", "directory": str(outdir)}
    try:
        if _directory_identity(outdir) != workdir_identity:
            raise ValueError("private extraction directory changed")
        _, artifact = _read_json(artifact_path, MAX_ARTIFACT_BYTES)
        if _directory_identity(outdir) != workdir_identity:
            raise ValueError("private extraction directory changed")
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
        if (status == "extracted") != (code == 0) or status not in ("extracted", "ocr_needed", "failed"):
            raise ValueError("converter outcome does not match exit")
    except (ValueError, OSError, KeyError, TypeError, AttributeError, RecursionError):
        return {"state": "ambiguous", "directory": str(outdir)}
    return {"state": "success" if status == "extracted" else status, "directory": str(outdir),
            "artifact_path": str(artifact_path), "artifact": artifact, "input_path": str(outdir / input_name)}
