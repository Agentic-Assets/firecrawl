"""Private, committed Colliers image-guard outcome in the existing ingest log."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from cre_ingest import artifact_run_identity_from_digests, child_count_regressed

SCHEMA = "colliers-image-transition-v1"
PREFIX = "CRE_COLLIERS_IMAGE_TRANSITION "
SOURCE = "colliers-main"
COUNTS = (
    "semantic_before",
    "semantic_retained_after",
    "raw_before",
    "raw_retained_after",
    "raw_new_after",
    "raw_after",
    "raw_retired_before",
    "staged_current",
    "current_without_images",
)


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


def valid_binding(binding):
    if not isinstance(binding, dict) or set(binding) != {
        "artifact_sha256",
        "artifact_bytes",
        "artifact_run_key",
        "generation_id",
        "generation_started_at",
        "database_target_sha256",
        "attempt_id",
    }:
        return False
    return (
        all(
            isinstance(binding[k], str) and re.fullmatch(r"[a-f0-9]{64}", binding[k])
            for k in ("artifact_sha256", "database_target_sha256")
        )
        and type(binding["artifact_bytes"]) is int
        and binding["artifact_bytes"] > 0
        and isinstance(binding["attempt_id"], str)
        and re.fullmatch(r"[a-f0-9]{32}", binding["attempt_id"]) is not None
        and all(
            isinstance(binding[k], str) and bool(binding[k])
            for k in ("artifact_run_key", "generation_id", "generation_started_at")
        )
    )


def matches_reports(receipt, raw_before, raw_after):
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema",
        "source_key",
        "binding",
        *COUNTS,
    }:
        return False
    if (
        receipt["schema"] != SCHEMA
        or receipt["source_key"] != SOURCE
        or not valid_binding(receipt["binding"])
        or any(type(receipt[k]) is not int or receipt[k] < 0 for k in COUNTS)
    ):
        return False
    return (
        receipt["raw_before"] == raw_before
        and receipt["raw_after"] == raw_after
        and receipt["raw_after"]
        == receipt["raw_retained_after"] + receipt["raw_new_after"]
        and receipt["raw_retired_before"] == 0
        and receipt["semantic_before"] <= receipt["raw_before"]
        and receipt["semantic_retained_after"] <= receipt["raw_retained_after"]
        and not child_count_regressed(
            receipt["semantic_before"], receipt["semantic_retained_after"]
        )
        and receipt["staged_current"] > 0
        and receipt["current_without_images"] == 0
    )


def read_outcome(path):
    """Hash the complete log and accept exactly one bounded post-COMMIT line."""
    path = Path(path)
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
                if not continued and chunk.startswith(PREFIX.encode()):
                    if len(chunk) > 8192 or not chunk.endswith(b"\n"):
                        raise ValueError("oversized image outcome")
                    outcomes.append(
                        json.loads(
                            chunk[len(PREFIX) :], object_pairs_hook=_unique_object
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


def expected_binding(run_dir, manifest, attempt_id):
    checkpoint = manifest["sources"][SOURCE]
    artifact = checkpoint["artifact"]
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
    return {
        "artifact_sha256": digest,
        "artifact_bytes": size,
        "artifact_run_key": artifact_run_identity_from_digests([digest])[0],
        "generation_id": manifest["run_id"],
        "generation_started_at": manifest["started_at"],
        "database_target_sha256": manifest["preflight"]["database_target"]["value"],
        "attempt_id": attempt_id,
    }


def verified_checkpoint(run_dir, manifest):
    """No receipt grants authority after recovery, nonzero, or uncertain completion."""
    try:
        checkpoint = manifest["sources"][SOURCE]
        ingest = checkpoint["ingest"]
        if (
            checkpoint["state"] != "ingested"
            or type(ingest["rc"]) is not int
            or ingest["rc"] != 0
            or not ingest["finished_at"]
            or not ingest["started_at"]
            or checkpoint.get("ingest_recovery")
            or ingest.get("recovered_from_exact_readback")
            or ingest.get("mark_missing") is not False
            or ingest.get("status_activation") is not False
        ):
            return None
        if ingest["log"] != "logs/colliers-main-ingest.log":
            return None
        evidence = ingest["image_transition"]
        observed = read_outcome(Path(run_dir) / ingest["log"])
        if (
            observed != evidence
            or evidence["outcome"]["staged_current"]
            != checkpoint["artifact"]["staged_unique"]
        ):
            return None
        binding = expected_binding(run_dir, manifest, ingest["attempt_id"])
        if not valid_binding(binding) or evidence["outcome"]["binding"] != binding:
            return None
        return evidence["outcome"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
