"""Shared stable-file readers and artifact bindings for image receipts.

This module owns evidence transport mechanics only. Source policy and receipt
qualification remain in each source-specific outcome module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from cre_ingest import artifact_run_identity_from_digests

_LEGACY_BINDING_FIELDS = {
    "artifact_sha256",
    "artifact_bytes",
    "artifact_run_key",
    "generation_id",
    "generation_started_at",
    "database_target_sha256",
    "attempt_id",
}
_CODE_BINDING_FIELDS = _LEGACY_BINDING_FIELDS | {"collector_git_sha"}


def _identity(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate image outcome JSON key")
        result[key] = value
    return result


def valid_binding(binding, *, require_collector_git_sha=False):
    """Validate either the legacy seven-field or code-bound eight-field shape."""
    fields = _CODE_BINDING_FIELDS if require_collector_git_sha else _LEGACY_BINDING_FIELDS
    if not isinstance(binding, dict) or set(binding) != fields:
        return False
    valid = (
        all(
            isinstance(binding[key], str)
            and re.fullmatch(r"[a-f0-9]{64}", binding[key])
            for key in ("artifact_sha256", "database_target_sha256")
        )
        and type(binding["artifact_bytes"]) is int
        and binding["artifact_bytes"] > 0
        and isinstance(binding["attempt_id"], str)
        and re.fullmatch(r"[a-f0-9]{32}", binding["attempt_id"]) is not None
        and all(
            isinstance(binding[key], str) and bool(binding[key])
            for key in ("artifact_run_key", "generation_id", "generation_started_at")
        )
    )
    if require_collector_git_sha:
        valid = valid and isinstance(binding["collector_git_sha"], str) and (
            re.fullmatch(r"[a-f0-9]{40}", binding["collector_git_sha"]) is not None
        )
    return bool(valid)


def read_outcome(path, prefix):
    """Hash a complete log and accept exactly one bounded prefixed JSON line."""
    path = Path(path)
    prefix_bytes = prefix.encode("utf-8")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest, outcomes, size = hashlib.sha256(), [], 0
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("unsafe image outcome log")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            continued = False
            for chunk in iter(lambda: stream.readline(8193), b""):
                digest.update(chunk)
                size += len(chunk)
                if not continued and chunk.startswith(prefix_bytes):
                    if len(chunk) > 8192 or not chunk.endswith(b"\n"):
                        raise ValueError("oversized image outcome")
                    outcomes.append(
                        json.loads(
                            chunk[len(prefix_bytes) :], object_pairs_hook=_unique_object
                        )
                    )
                continued = not chunk.endswith(b"\n")
            after = os.fstat(fd)
        if _identity(before) != _identity(after) or _identity(after) != _identity(
            path.stat(follow_symlinks=False)
        ):
            raise ValueError("image outcome log changed")
    finally:
        os.close(fd)
    if len(outcomes) != 1:
        raise ValueError("missing or ambiguous image outcome")
    return {"outcome": outcomes[0], "log_sha256": digest.hexdigest(), "log_bytes": size}


def artifact_binding(
    run_dir, manifest, source, attempt_id, *, include_collector_git_sha=False
):
    """Bind a source artifact to its run, target and ingest attempt by exact bytes."""
    artifact = manifest["sources"][source]["artifact"]
    path = Path(run_dir) / artifact["path"]
    if not path.resolve().is_relative_to(Path(run_dir).resolve()):
        raise ValueError("transition artifact escaped run")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("unsafe transition artifact")
        hashed = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hashed.update(chunk)
                size += len(chunk)
        after = os.fstat(fd)
        if _identity(before) != _identity(after) or _identity(after) != _identity(
            path.stat(follow_symlinks=False)
        ):
            raise ValueError("transition artifact changed during read")
    finally:
        os.close(fd)
    digest = hashed.hexdigest()
    if digest != artifact["sha256"] or size != artifact["bytes"]:
        raise ValueError("transition artifact changed")
    binding = {
        "artifact_sha256": digest,
        "artifact_bytes": size,
        "artifact_run_key": artifact_run_identity_from_digests([digest])[0],
        "generation_id": manifest["run_id"],
        "generation_started_at": manifest["started_at"],
        "database_target_sha256": manifest["preflight"]["database_target"]["value"],
        "attempt_id": attempt_id,
    }
    if include_collector_git_sha:
        binding["collector_git_sha"] = manifest["collector_git_sha"]
    return binding
