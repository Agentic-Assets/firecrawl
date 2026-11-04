"""Private, committed Colliers image-guard outcome in the existing ingest log."""

from __future__ import annotations

from pathlib import Path

from cre_ingest import child_count_regressed
from cre_image_transition_log import artifact_binding, read_outcome as _read_outcome
from cre_image_transition_log import valid_binding as _valid_binding

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


def valid_binding(binding):
    # Colliers receipts deliberately retain their original seven-field contract.
    return _valid_binding(binding)


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
    return _read_outcome(path, PREFIX)


def expected_binding(run_dir, manifest, attempt_id):
    return artifact_binding(run_dir, manifest, SOURCE, attempt_id)


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
