"""Source-bound Transwestern image transition receipt validation."""

from __future__ import annotations

import re
from pathlib import Path

from cre_image_transition_log import artifact_binding, read_outcome as _read_outcome
from cre_image_transition_log import valid_binding as _valid_binding

SCHEMA = "transwestern-image-transition-v1"
PREFIX = "CRE_TRANSWESTERN_IMAGE_TRANSITION "
SOURCE = "transwestern"
LOG_PATH = "logs/transwestern-ingest.log"
COUNTS = (
    "raw_before",
    "raw_retained_after",
    "raw_new_after",
    "raw_after",
    "raw_retired_before",
    "protected_before",
    "protected_retained_after",
    "excluded_before",
    "staged_current",
    "current_source_primary",
    "historical_primary",
    "unassigned_primary",
)
_OUTCOME_FIELDS = {"schema", "source_key", "binding", *COUNTS, "image_state_sha256"}


def valid_binding(binding):
    """Require the exact artifact, generation, target, attempt and code pin."""
    return _valid_binding(binding, require_collector_git_sha=True)


def read_outcome(path):
    return _read_outcome(path, PREFIX)


def _valid_outcome_shape(receipt):
    return (
        isinstance(receipt, dict)
        and set(receipt) == _OUTCOME_FIELDS
        and receipt.get("schema") == SCHEMA
        and receipt.get("source_key") == SOURCE
        and valid_binding(receipt.get("binding"))
        and all(type(receipt.get(key)) is int and receipt[key] >= 0 for key in COUNTS)
        and isinstance(receipt.get("image_state_sha256"), str)
        and re.fullmatch(r"[a-f0-9]{64}", receipt["image_state_sha256"]) is not None
    )


def matches_reports(receipt, raw_before, raw_after):
    """Check outcome arithmetic against the two independently parsed raw reports."""
    if not _valid_outcome_shape(receipt):
        return False
    if type(raw_before) is not int or type(raw_after) is not int:
        return False
    return (
        receipt["raw_before"] == raw_before
        and receipt["raw_after"] == raw_after
        and receipt["raw_after"]
        == receipt["raw_retained_after"] + receipt["raw_new_after"]
        and receipt["raw_before"]
        == receipt["protected_before"] + receipt["excluded_before"]
        and receipt["protected_before"] == receipt["protected_retained_after"]
        and receipt["raw_retired_before"] == 0
        and receipt["raw_retained_after"] >= receipt["protected_retained_after"]
        and receipt["staged_current"] > 0
        and receipt["current_source_primary"]
        + receipt["historical_primary"]
        + receipt["unassigned_primary"]
        == receipt["staged_current"]
    )


def _readback_count(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"(?:0|[1-9][0-9]*)", value):
        return int(value)
    return None


def report_image_count(report):
    """Read the one canonical Transwestern image count from a validation report."""
    if not isinstance(report, dict):
        return None
    queries = report.get("queries")
    rows = queries.get("child_counts") if isinstance(queries, dict) else None
    if not isinstance(rows, list):
        return None
    matches = [
        row for row in rows
        if isinstance(row, dict)
        and row.get("source_key") == SOURCE
        and row.get("child_type") == "images"
    ]
    if len(matches) != 1:
        return None
    return _readback_count(matches[0].get("count"))


def matches_readback(receipt, rows):
    """Require the one-row full image-state count and digest query result."""
    if not _valid_outcome_shape(receipt) or not isinstance(rows, (list, tuple)):
        return False
    if len(rows) != 1 or not isinstance(rows[0], dict):
        return False
    row = rows[0]
    if set(row) != {"image_count", "image_state_sha256"}:
        return False
    return (
        _readback_count(row.get("image_count")) == receipt["raw_after"]
        and row.get("image_state_sha256") == receipt["image_state_sha256"]
    )


def expected_binding(run_dir, manifest, attempt_id):
    binding = artifact_binding(
        run_dir,
        manifest,
        SOURCE,
        attempt_id,
        include_collector_git_sha=True,
    )
    if not valid_binding(binding):
        raise ValueError("invalid Transwestern artifact binding")
    return binding


def verified_checkpoint(run_dir, manifest):
    """Return only an exact, successful, unrecovered Transwestern receipt."""
    try:
        checkpoint = manifest["sources"][SOURCE]
        ingest = checkpoint["ingest"]
        if (
            checkpoint["state"] != "ingested"
            or type(ingest["rc"]) is not int
            or ingest["rc"] != 0
            or not ingest["started_at"]
            or not ingest["finished_at"]
            or checkpoint.get("ingest_recovery")
            or ingest.get("recovered_from_exact_readback")
            or ingest.get("mark_missing") is not False
            or ingest.get("status_activation") is not False
            or ingest.get("log") != LOG_PATH
        ):
            return None
        evidence = ingest["image_transition"]
        observed = read_outcome(Path(run_dir) / LOG_PATH)
        receipt = evidence["outcome"]
        if (
            observed != evidence
            or not _valid_outcome_shape(receipt)
            or not matches_reports(receipt, receipt["raw_before"], receipt["raw_after"])
            or receipt["staged_current"] != checkpoint["artifact"]["staged_unique"]
            or receipt["binding"]
            != expected_binding(run_dir, manifest, ingest["attempt_id"])
        ):
            return None
        return receipt
    except (OSError, ValueError, KeyError, TypeError):
        return None
