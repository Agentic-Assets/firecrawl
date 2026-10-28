"""Prepare and read a private document seal; caller owns context authentication.

Readback verifies sealed metadata, not original bytes after their deletion. The
caller must rederive the context from policy, receipt and body, or authenticate
it against a previously sealed marker before relying on this helper.
"""

import json
import os
import stat

from source_evidence import cli, document_acquisition, events, private_export

MAX_FILE = cli.MAX_PLAN_BYTES


def _context_shape(context, artifact):
    if (not isinstance(context, dict) or context.get("schema_version") != document_acquisition.SCHEMA
            or not isinstance(context.get("receipt"), dict)):
        raise ValueError("invalid document context")
    receipt = context["receipt"]
    if set(receipt) != document_acquisition.RECEIPT_KEYS:
        raise ValueError("invalid document receipt shape")
    outcome = receipt.get("outcome")
    if outcome == "acquired":
        if (artifact is None or receipt.get("completion") != "complete" or receipt.get("failure_code") is not None
                or type(receipt.get("body_bytes")) is not int or receipt["body_bytes"] < 0
                or not isinstance(receipt.get("body_sha256"), str)):
            raise ValueError("invalid acquired context")
    elif outcome == "failed":
        if (artifact is not None or receipt.get("completion") != "incomplete"
                or receipt.get("failure_code") not in document_acquisition.FAILURES
                or receipt.get("body_bytes") is not None or receipt.get("body_sha256") is not None):
            raise ValueError("invalid failed context")
    else:
        raise ValueError("invalid document receipt outcome")


def prepare(context, artifact=None):
    """Return ordered canonical file bytes and their deterministic marker."""
    _context_shape(context, artifact)
    files = [("acquisition.json", events.encoded(context))]
    binding = plan = None
    if artifact is not None:
        if not isinstance(artifact, dict):
            raise ValueError("invalid converter artifact")
        enriched = {**artifact, "acquisition_context": context,
                    "acquisition_context_origin": "foreground_coordinator_not_converter"}
        binding, plan = document_acquisition.bind_extraction(context, enriched)
        files += [("converter-artifact.json", events.encoded(artifact)), ("artifact.json", events.encoded(enriched)),
                  ("binding.json", events.encoded(binding)), ("plan.json", events.encoded(plan))]
    if any(len(payload) > MAX_FILE for _, payload in files):
        raise ValueError("document seal file exceeds bound")
    marker = {"files": {name: events.sha256(payload) for name, payload in files},
              "plan_sha256": plan["plan_sha256"] if plan else None,
              "capture_id": binding["document_capture_id"] if binding else None}
    return files, marker


def write(outdir, context, artifact=None):
    files, marker = prepare(context, artifact)
    private_export.write(outdir, files)
    return marker


def read(outdir, *, expected_context, expected_marker=None):
    """Verify exact private file set and canonical bytes; do not claim body rehash."""
    if not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "getuid")):
        raise RuntimeError("document seal readback requires POSIX")
    directory = os.open(outdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("document seal directory is not private")
        names = set(os.listdir(directory))
        has_artifact = expected_context.get("receipt", {}).get("outcome") == "acquired"
        required = ({"acquisition.json", "converter-artifact.json", "artifact.json", "binding.json", "plan.json"}
                    if has_artifact else {"acquisition.json"})
        if names != required:
            raise ValueError("document seal file set mismatch")
        values = {}
        for name in sorted(required):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                before = os.fstat(fd)
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                        or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1 or before.st_size > MAX_FILE):
                    raise ValueError("invalid private seal file")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    raw = stream.read(MAX_FILE + 1)
                after = os.fstat(fd)
                named = os.stat(name, dir_fd=directory, follow_symlinks=False)
                identity = lambda item: (item.st_dev, item.st_ino, item.st_uid, item.st_mode, item.st_nlink,
                                         item.st_size, item.st_mtime_ns, item.st_ctime_ns)
                if len(raw) > MAX_FILE or identity(before) != identity(after) or identity(after) != identity(named):
                    raise ValueError("document seal file changed during readback")
                values[name] = raw
            finally:
                os.close(fd)
        artifact = json.loads(values["converter-artifact.json"]) if has_artifact else None
        files, marker = prepare(expected_context, artifact)
        if any(values[name] != payload for name, payload in files):
            raise ValueError("document seal bytes mismatch")
        if expected_marker is not None and events.encoded(expected_marker) != events.encoded(marker):
            raise ValueError("document seal marker mismatch")
        if set(os.listdir(directory)) != required:
            raise ValueError("document seal file set changed during readback")
        named_dir = os.stat(outdir, follow_symlinks=False)
        final_dir = os.fstat(directory)
        if (info.st_dev, info.st_ino, info.st_uid, info.st_mode) != (final_dir.st_dev, final_dir.st_ino, final_dir.st_uid, final_dir.st_mode) or (
                named_dir.st_dev, named_dir.st_ino, named_dir.st_uid, named_dir.st_mode) != (
                final_dir.st_dev, final_dir.st_ino, final_dir.st_uid, final_dir.st_mode):
            raise ValueError("document seal directory changed during readback")
        return {"artifact": artifact, "marker": marker, "original_bytes_reverified": False}
    finally:
        os.close(directory)
