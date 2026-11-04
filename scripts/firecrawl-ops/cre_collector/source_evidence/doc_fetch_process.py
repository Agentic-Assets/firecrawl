"""Supervise one private fetch job; no policy or acquisition authority is inferred."""

import os
import signal
import stat
import subprocess
import sys
from pathlib import Path

from cre_owned_processes import OwnedProcessGroup, settle_owned_groups
from source_evidence import acquisition, automatic, document_acquisition, document_fetch_worker, events
from source_evidence.document_process import DocumentProcessCleanupUnknown

STARTUP_GRACE_SECONDS = 5
REAP_GRACE_SECONDS = 5


def _private_directory(path):
    if not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "getuid")):
        raise RuntimeError("private document fetch requires POSIX")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
    except BaseException:
        os.close(fd)
        raise
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise ValueError("document attempt directory is not private")
    return fd


def run(job, job_path, outdir):
    """Run one child, then privately validate its receipt and optional bytes.

    Caller must first rederive acquisition policy and account for total bytes.
    A timeout or worker failure can occur after the remote GET was sent.
    """
    document_fetch_worker.validate_job(job)
    raw = events.encoded(job)
    if len(raw) > 16384:
        raise ValueError("document fetch job exceeds bound")
    path, output = Path(os.path.abspath(job_path)), Path(os.path.abspath(outdir))
    if (not path.name or not output.name or path.name in (".", "..") or output.name in (".", "..")
            or path.parent != output.parent or path.name == output.name):
        raise ValueError("job and output must be distinct children of the attempt directory")
    parent = _private_directory(path.parent)
    try:
        try:
            os.stat(output.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError("fetch output already exists")
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent)
        digest = events.sha256(raw)
        result = {"job_sha256": digest, "directory": str(output)}
        command = [sys.executable, "-m", "source_evidence.document_fetch_worker", "--job", str(path),
                   "--job-sha256", digest, "--out-dir", str(output)]
        process = None
        try:
            process = subprocess.Popen(command, cwd=str(Path(__file__).resolve().parent.parent),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                                       start_new_session=True)
            owned = OwnedProcessGroup.from_spawn(process)
        except OSError as error:
            if process is None:
                return {**result, "state": "worker_failed_ambiguous"}
            raise DocumentProcessCleanupUnknown("document fetch ownership unavailable") from error
        except BaseException as error:
            raise DocumentProcessCleanupUnknown("document fetch spawn or ownership interrupted") from error
        timed_out = False
        interrupted = None
        try:
            code = process.wait(timeout=job["timeout_seconds"] + STARTUP_GRACE_SECONDS)
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
            raise DocumentProcessCleanupUnknown("document fetch settlement unavailable") from (interrupted or error)
        if not settlement.settled:
            raise DocumentProcessCleanupUnknown("document fetch settlement unknown") from interrupted
        if interrupted is not None:
            raise interrupted
        if timed_out:
            return {**result, "state": "fetch_outcome_unknown"}
        if code != 0:
            return {**result, "state": "worker_failed_ambiguous"}
        directory = _private_directory(output)
        try:
            initial = os.fstat(directory)
            receipt_raw = automatic._read_private(output / "receipt.json", 65536)
            if len(receipt_raw) > 65536:
                raise ValueError("fetch receipt exceeds bound")
            wrapper, literals = acquisition.decode_json(receipt_raw, max_bytes=65536)
            if (literals or not isinstance(wrapper, dict) or set(wrapper) != {"job_sha256", "receipt"}
                    or wrapper["job_sha256"] != digest):
                raise ValueError("fetch receipt job binding mismatch")
            receipt = wrapper["receipt"]
            if not isinstance(receipt, dict):
                raise ValueError("malformed fetch receipt")
            body_path = output / "document.bin"
            if receipt.get("outcome") == "acquired":
                body = automatic._read_private(body_path, job["max_bytes"])
                if len(body) > job["max_bytes"]:
                    raise ValueError("fetch body exceeds bound")
            else:
                body = None
                try:
                    os.stat("document.bin", dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ValueError("failure receipt has unexpected body")
            selection = {"allowed_hosts": job["allowed_hosts"], "limits": {
                "max_redirects": job["max_redirects"], "max_document_bytes": job["max_bytes"],
                "max_total_bytes": job["max_bytes"]}}
            document_acquisition._receipt_checked(receipt, body, {"url": job["url"]}, selection)
            named = os.stat(output.name, dir_fd=parent, follow_symlinks=False)
            final = os.fstat(directory)
            if (initial.st_dev, initial.st_ino, initial.st_uid, initial.st_mode) != (
                    final.st_dev, final.st_ino, final.st_uid, final.st_mode) or (
                    named.st_dev, named.st_ino, named.st_uid, named.st_mode) != (
                    final.st_dev, final.st_ino, final.st_uid, final.st_mode):
                raise ValueError("fetch output directory changed during readback")
            return {**result, "state": "completed", "receipt": receipt,
                    "body_path": str(body_path) if body is not None else None}
        finally:
            os.close(directory)
    finally:
        os.close(parent)
