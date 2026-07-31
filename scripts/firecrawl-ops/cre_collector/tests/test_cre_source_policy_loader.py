"""Focused, no-network tests for ``cre_source_policy`` fail-closed loading."""

from __future__ import annotations

import json

import pytest

import cre_source_policy as source_policy


def _write_policy(tmp_path, policy, name="policy.json"):
    path = tmp_path / name
    path.write_text(json.dumps(policy), encoding="utf-8")
    return path


def test_default_policy_path_is_module_relative_and_loads_all_51_sources(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    assert source_policy.resolve_policy_path() == source_policy.POLICY_PATH
    policy = source_policy.load_source_policy()

    assert list(policy) == list(source_policy.SOURCE_TO_BROKERAGE)
    assert len(policy) == 51


def test_loader_rejects_invalid_json(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(source_policy.SourcePolicyValidationError, match="invalid JSON"):
        source_policy.load_source_policy(path)


def test_loader_rejects_missing_registry_key(tmp_path):
    policy = source_policy.load_source_policy()
    policy.pop("cbre")

    with pytest.raises(source_policy.SourcePolicyValidationError, match="source keys missing"):
        source_policy.load_source_policy(_write_policy(tmp_path, policy))


def test_loader_rejects_extra_or_out_of_order_registry_key(tmp_path):
    policy = source_policy.load_source_policy()
    policy["unexpected"] = policy["cbre"]

    with pytest.raises(source_policy.SourcePolicyValidationError, match="source keys missing=.*extra"):
        source_policy.load_source_policy(_write_policy(tmp_path, policy))


def test_loader_rejects_incomplete_entry_schema(tmp_path):
    policy = source_policy.load_source_policy()
    policy["cbre"].pop("detail_claim")

    with pytest.raises(source_policy.SourcePolicyValidationError, match="fields missing"):
        source_policy.load_source_policy(_write_policy(tmp_path, policy))


def test_loader_rejects_source_class_contract_drift(tmp_path):
    policy = source_policy.load_source_policy()
    policy["colliers"]["lifecycle_reconciliation_eligible"] = False

    with pytest.raises(source_policy.SourcePolicyValidationError, match="does not match"):
        source_policy.load_source_policy(_write_policy(tmp_path, policy))


def test_loader_keeps_mixed_source_canonical_evidence_and_provisional_namespace():
    policy = source_policy.load_source_policy()

    expected_evidence = {
        "cbre-dealflow": "authoritative_inventory_feed",
        "colliers": "strict_detail",
    }
    for source_key, inventory_definition in source_policy.INVENTORY_ONLY_SOURCE_DEFINITIONS.items():
        entry = policy[source_key]
        assert entry["evidence_class"] == expected_evidence[source_key]
        assert entry["canonical_claim"] in {"canonical_listing", "authoritative_inventory"}
        assert entry["inventory_only_namespace"] == inventory_definition["external_id_like"]
