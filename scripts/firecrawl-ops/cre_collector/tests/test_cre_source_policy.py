"""No-network parity checks for the declarative CRE source-policy contract."""

from __future__ import annotations

import json
import re
from pathlib import Path

import cre_ingest


COLLECTOR = Path(__file__).resolve().parent.parent
POLICY_PATH = COLLECTOR / "data" / "cre-source-policy.json"
FIELDS = {
    "evidence_class",
    "canonical_claim",
    "detail_claim",
    "child_contract",
    "inventory_only_namespace",
    "lifecycle_reconciliation_eligible",
}


def _typescript_source_keys() -> list[str]:
    text = (COLLECTOR / "types.ts").read_text(encoding="utf-8")
    match = re.search(r"export const SOURCE_KEYS = \[(.*?)\] as const;", text, re.S)
    assert match, "SOURCE_KEYS tuple not found in types.ts"
    return re.findall(r'"([a-z0-9-]+)"', match.group(1))


def _policy() -> dict[str, dict[str, object]]:
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    assert isinstance(policy, dict)
    return policy


def _expected_policy(source_key: str) -> dict[str, object]:
    inventory_definition = cre_ingest.INVENTORY_ONLY_SOURCE_DEFINITIONS.get(source_key)
    if inventory_definition:
        if source_key == "cbre-dealflow":
            return {
                "evidence_class": "authoritative_inventory_feed",
                "canonical_claim": "authoritative_inventory",
                "detail_claim": "current_inventory_only",
                "child_contract": "preserve_existing_children",
                "inventory_only_namespace": inventory_definition["external_id_like"],
                "lifecycle_reconciliation_eligible": True,
            }
        return {
            "evidence_class": "strict_detail",
            "canonical_claim": "canonical_listing",
            "detail_claim": "current_strict_detail",
            "child_contract": "replace_from_fresh_detail",
            "inventory_only_namespace": inventory_definition["external_id_like"],
            "lifecycle_reconciliation_eligible": True,
        }
    if source_key in cre_ingest.AUTHORITATIVE_INVENTORY_FEED_SOURCE_KEYS:
        return {
            "evidence_class": "authoritative_inventory_feed",
            "canonical_claim": "authoritative_inventory",
            "detail_claim": "current_inventory_only",
            "child_contract": (
                "preserve_existing_children"
                if source_key in cre_ingest.CHILD_PRESERVING_AUTHORITATIVE_FEED_SOURCE_KEYS
                else "replace_from_fresh_detail"
            ),
            "inventory_only_namespace": None,
            "lifecycle_reconciliation_eligible": True,
        }
    if source_key == "avison-young":
        return {
            "evidence_class": "property_detail",
            "canonical_claim": "canonical_listing",
            "detail_claim": "current_property_detail",
            "child_contract": "replace_from_fresh_detail",
            "inventory_only_namespace": None,
            "lifecycle_reconciliation_eligible": True,
        }
    assert source_key in cre_ingest.STRICT_FRESHNESS_SOURCE_KEYS
    return {
        "evidence_class": "strict_detail",
        "canonical_claim": "canonical_listing",
        "detail_claim": "current_strict_detail",
        "child_contract": "replace_from_fresh_detail",
        "inventory_only_namespace": None,
        "lifecycle_reconciliation_eligible": True,
    }


def test_cre_source_policy_has_exact_51_key_registry_parity():
    policy = _policy()
    ts_keys = _typescript_source_keys()
    ingest_keys = list(cre_ingest.SOURCE_TO_BROKERAGE)

    assert ts_keys == ingest_keys
    assert list(policy) == ts_keys
    assert len(policy) == 51


def test_cre_source_policy_has_no_implicit_defaults_and_matches_source_contracts():
    policy = _policy()
    for source_key in _typescript_source_keys():
        actual = policy[source_key]
        assert set(actual) == FIELDS
        assert actual == _expected_policy(source_key)
