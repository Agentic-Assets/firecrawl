"""Fail-closed loader for the declarative CRE source-policy registry.

This module is intentionally not wired into collection or ingestion yet.  It
provides one deterministic policy path and rejects any incomplete or malformed
policy before a future consumer can rely on it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from cre_ingest import (
    AUTHORITATIVE_INVENTORY_FEED_SOURCE_KEYS,
    CHILD_PRESERVING_AUTHORITATIVE_FEED_SOURCE_KEYS,
    INVENTORY_ONLY_SOURCE_DEFINITIONS,
    SOURCE_TO_BROKERAGE,
    STRICT_FRESHNESS_SOURCE_KEYS,
)


POLICY_PATH: Final = Path(__file__).resolve().parent / "data" / "cre-source-policy.json"
MIXED_CANONICAL_INVENTORY_SOURCE_KEYS: Final = frozenset(
    INVENTORY_ONLY_SOURCE_DEFINITIONS
)
POLICY_FIELDS: Final = frozenset(
    {
        "evidence_class",
        "canonical_claim",
        "detail_claim",
        "child_contract",
        "inventory_only_namespace",
        "lifecycle_reconciliation_eligible",
    }
)


class SourcePolicyValidationError(ValueError):
    """Raised when the declarative policy is not safe to consume."""


def resolve_policy_path(path: str | Path | None = None) -> Path:
    """Return an absolute policy path without depending on the caller's cwd."""
    return POLICY_PATH if path is None else Path(path).expanduser().resolve()


def _error(path: Path, message: str) -> SourcePolicyValidationError:
    return SourcePolicyValidationError(f"invalid CRE source policy {path}: {message}")


def _validate_entry(path: Path, source_key: str, entry: object) -> dict[str, object]:
    if not isinstance(entry, dict):
        raise _error(path, f"{source_key} must be an object")
    if set(entry) != POLICY_FIELDS:
        missing = sorted(POLICY_FIELDS - set(entry))
        extra = sorted(set(entry) - POLICY_FIELDS)
        raise _error(path, f"{source_key} fields missing={missing} extra={extra}")

    evidence_class = entry["evidence_class"]
    canonical_claim = entry["canonical_claim"]
    detail_claim = entry["detail_claim"]
    child_contract = entry["child_contract"]
    inventory_namespace = entry["inventory_only_namespace"]
    lifecycle_eligible = entry["lifecycle_reconciliation_eligible"]
    string_fields = {
        "evidence_class": evidence_class,
        "canonical_claim": canonical_claim,
        "detail_claim": detail_claim,
        "child_contract": child_contract,
    }
    if any(not isinstance(value, str) or not value for value in string_fields.values()):
        raise _error(path, f"{source_key} requires nonempty string claim fields")
    if type(lifecycle_eligible) is not bool:
        raise _error(path, f"{source_key} lifecycle_reconciliation_eligible must be boolean")

    inventory_definition = INVENTORY_ONLY_SOURCE_DEFINITIONS.get(source_key)
    if source_key in MIXED_CANONICAL_INVENTORY_SOURCE_KEYS:
        if source_key == "cbre-dealflow":
            expected = {
                "evidence_class": "authoritative_inventory_feed",
                "canonical_claim": "authoritative_inventory",
                "detail_claim": "current_inventory_only",
                "child_contract": "preserve_existing_children",
                "inventory_only_namespace": inventory_definition["external_id_like"],
                "lifecycle_reconciliation_eligible": True,
            }
        else:
            expected = {
                "evidence_class": "strict_detail",
                "canonical_claim": "canonical_listing",
                "detail_claim": "current_strict_detail",
                "child_contract": "replace_from_fresh_detail",
                "inventory_only_namespace": inventory_definition["external_id_like"],
                "lifecycle_reconciliation_eligible": True,
            }
    elif source_key in AUTHORITATIVE_INVENTORY_FEED_SOURCE_KEYS:
        expected = {
            "evidence_class": "authoritative_inventory_feed",
            "canonical_claim": "authoritative_inventory",
            "detail_claim": "current_inventory_only",
            "child_contract": (
                "preserve_existing_children"
                if source_key in CHILD_PRESERVING_AUTHORITATIVE_FEED_SOURCE_KEYS
                else "replace_from_fresh_detail"
            ),
            "inventory_only_namespace": None,
            "lifecycle_reconciliation_eligible": True,
        }
    elif source_key == "avison-young":
        expected = {
            "evidence_class": "property_detail",
            "canonical_claim": "canonical_listing",
            "detail_claim": "current_property_detail",
            "child_contract": "replace_from_fresh_detail",
            "inventory_only_namespace": None,
            "lifecycle_reconciliation_eligible": True,
        }
    elif source_key in STRICT_FRESHNESS_SOURCE_KEYS:
        expected = {
            "evidence_class": "strict_detail",
            "canonical_claim": "canonical_listing",
            "detail_claim": "current_strict_detail",
            "child_contract": "replace_from_fresh_detail",
            "inventory_only_namespace": None,
            "lifecycle_reconciliation_eligible": True,
        }
    else:
        raise _error(path, f"{source_key} has no explicit source-class contract")

    if entry != expected:
        raise _error(path, f"{source_key} does not match its source-class contract")
    return entry


def load_source_policy(path: str | Path | None = None) -> dict[str, dict[str, object]]:
    """Load the complete policy registry, rejecting all partial or invalid data."""
    policy_path = resolve_policy_path(path)
    try:
        raw = policy_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _error(policy_path, f"cannot read policy: {exc}") from exc
    try:
        policy = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _error(policy_path, f"invalid JSON: {exc.msg}") from exc
    if not isinstance(policy, dict):
        raise _error(policy_path, "top level must be an object")

    expected_keys = list(SOURCE_TO_BROKERAGE)
    if list(policy) != expected_keys:
        missing = sorted(set(expected_keys) - set(policy))
        extra = sorted(set(policy) - set(expected_keys))
        raise _error(policy_path, f"source keys missing={missing} extra={extra} or out of order")
    return {
        source_key: _validate_entry(policy_path, source_key, policy[source_key])
        for source_key in expected_keys
    }
