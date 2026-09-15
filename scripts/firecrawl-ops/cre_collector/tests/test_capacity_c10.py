from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from capacity_c10 import adapters, admission, compare, contracts, policy, runner


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class VerifiedAdapter:
    fully_verified = True

    def __init__(self, key: str) -> None:
        self.key = key
        self.implementation_sha256 = _digest(key)

    def verify_enumeration(self, evidence: Mapping[str, object]) -> None:
        return None

    def verify_member(self, evidence: Mapping[str, object]) -> None:
        return None

    def classify_not_found(self, evidence: Mapping[str, object]) -> bool:
        return False


def _registry() -> dict[str, VerifiedAdapter]:
    return {
        source["key"]: VerifiedAdapter(source["key"])
        for source in policy.load_policy()["sources"]
    }


def _cohort() -> dict[str, object]:
    sources = []
    for source in policy.load_policy()["sources"]:
        members = [{"provider_id": f"{source['key']}-{index}"} for index in range(16)]
        sources.append(
            {
                "source_key": source["key"],
                "plane": source["plane"],
                "core_state": "ready",
                "core_target_rows": 16,
                "core_selected_rows": 16,
                "core": members,
                "fresh_enumeration": {
                    "population_state": "verified",
                    "total_population": 16,
                    "receipt_sha256": _digest(source["key"] + "enumeration"),
                },
            }
        )
    cohort: dict[str, object] = {
        "schema_version": 1,
        "kind": "cre_capacity_multisource_v1_cohort",
        "config_sha256": _digest("multisource-config"),
        "sampling": {"core_per_source": 16},
        "planes": {},
        "aggregate": {"state": "ready_for_review"},
        "sources": sources,
    }
    return _seal_cohort(cohort)


def _seal_cohort(cohort: dict[str, object]) -> dict[str, object]:
    cohort["cohort_sha256"] = contracts.sha256(
        {key: cohort[key] for key in admission.COHORT_HASH_FIELDS}
    )
    return cohort


def _plan() -> dict[str, object]:
    return admission.admit_plan(_cohort(), registry=_registry())


def _no_write() -> dict[str, object]:
    return {"no_write": dict(admission.NO_WRITE)}


def _arm(
    plan: Mapping[str, object], index: int, p0_rate: float = 100.0
) -> dict[str, object]:
    variant = contracts.ARM_SEQUENCE[index]
    rate = p0_rate if variant == "p0" else p0_rate * 1.2
    return {
        "plan_sha256": plan["plan_sha256"],
        "index": index,
        "variant": variant,
        "terminal": True,
        **_no_write(),
        "sources": [
            {
                "key": source["key"],
                "plane": source["plane"],
                "qualified": True,
                "qualified_rows_per_minute": rate,
            }
            for source in plan["sources"]
        ],
    }


def test_policy_is_hashed_fixed_and_preserves_twenty_source_twelve_eight_floor() -> (
    None
):
    loaded = policy.load_policy()
    assert loaded["policy_sha256"] == contracts.sha256(
        {key: value for key, value in loaded.items() if key != "policy_sha256"}
    )
    assert len(loaded["sources"]) == 20
    assert {source["plane"] for source in loaded["sources"]} == {
        "strict_detail",
        "authoritative_inventory",
    }
    assert sum(source["plane"] == "strict_detail" for source in loaded["sources"]) == 12
    assert (
        sum(
            source["plane"] == "authoritative_inventory" for source in loaded["sources"]
        )
        == 8
    )
    assert loaded["source_workers"] == 1


def test_default_registry_has_no_generic_or_admitting_adapter() -> None:
    loaded = policy.load_policy()
    registry = adapters.default_registry()
    assert set(registry) == {source["key"] for source in loaded["sources"]}
    assert not any(adapter.fully_verified for adapter in registry.values())
    with pytest.raises(contracts.C10Error, match="not fully verified"):
        adapters.verified_registry(loaded, registry)


def test_admission_binds_exact_cohort_profiles_and_implementation_manifest() -> None:
    plan = _plan()
    contracts.validate_plan(plan)
    assert plan["profiles"]["p0"]["name"] == "c10-p0"
    assert plan["profiles"]["p1"]["name"] == "c10-p1"
    assert plan["arm_sequence"] == list(contracts.ARM_SEQUENCE)
    assert len(plan["sources"]) == 20
    assert plan["no_write"] == admission.NO_WRITE


def test_admission_rejects_partial_cohort_even_with_verified_adapters() -> None:
    cohort = _cohort()
    cohort["sources"] = cohort["sources"][:-1]
    _seal_cohort(cohort)
    with pytest.raises(contracts.C10Error, match="exactly 20"):
        admission.admit_plan(cohort, registry=_registry())


def test_admission_rejects_underfilled_or_wrong_plane_cohort() -> None:
    cohort = _cohort()
    source = cohort["sources"][0]
    source["core"] = source["core"][:-1]
    source["core_selected_rows"] = 15
    source["core_target_rows"] = 15
    _seal_cohort(cohort)
    with pytest.raises(contracts.C10Error, match="incomplete immutable membership"):
        admission.admit_plan(cohort, registry=_registry())


def test_admission_rejects_a_cohort_whose_declared_hash_does_not_bind_membership() -> (
    None
):
    cohort = _cohort()
    cohort["sources"][0]["core"][0]["provider_id"] = "mutated-after-review"
    with pytest.raises(contracts.C10Error, match="digest"):
        admission.admit_plan(cohort, registry=_registry())


def test_c10_profiles_have_exact_p0_p1_whitelist() -> None:
    plan = _plan()
    p0 = plan["profiles"]["p0"]["requested"]
    p1 = plan["profiles"]["p1"]["requested"]
    changed = {key for key in p0 if p0[key] != p1[key]}
    assert changed == admission.P0_P1_MUTABLE_FIELDS
    assert {key: p1[key] for key in changed} == {
        "browser_cpus": 6,
        "global_pages": 10,
        "jll_detail_concurrency": 10,
        "browser_pids": 768,
        "api_cpus": 2,
    }


def test_one_use_arms_are_separate_from_the_immutable_plan() -> None:
    plan = _plan()
    session = contracts.new_session(plan)
    claimed = []
    for _ in contracts.ARM_SEQUENCE:
        next_arm = contracts.claim_next_arm(plan, session)
        session = next_arm["session"]
        claimed.append(next_arm["arm"]["variant"])
    assert tuple(claimed) == contracts.ARM_SEQUENCE
    with pytest.raises(contracts.C10Error, match="already been consumed"):
        contracts.claim_next_arm(plan, session)
    contracts.validate_plan(plan)


def test_serial_protocol_requires_settlement_and_p1_rollback_then_quarantines_failure() -> (
    None
):
    plan = _plan()
    session = runner.initial_session(plan)
    events: list[str] = []
    hooks = runner.SerialRunnerHooks(
        settle=lambda arm: (
            events.append(f"settle-{arm['variant']}")
            or {"state": "idle", "complete": True}
        ),
        rollback=lambda arm: events.append("rollback") or {"verified": True},
        quarantine=lambda reason: events.append(f"quarantine:{reason}"),
    )
    first = runner.run_one_arm_protocol(
        plan, session, hooks=hooks, run_arm=lambda arm: _no_write()
    )
    second = runner.run_one_arm_protocol(
        plan, first["session"], hooks=hooks, run_arm=lambda arm: _no_write()
    )
    assert first["arm"]["variant"] == "p0"
    assert second["arm"]["variant"] == "p1"
    assert events == ["settle-p0", "settle-p1", "rollback", "settle-p1"]

    failed_hooks = runner.SerialRunnerHooks(
        settle=lambda arm: {"state": "unknown", "complete": False},
        rollback=lambda arm: {"verified": True},
        quarantine=lambda reason: events.append("quarantined"),
    )
    with pytest.raises(contracts.C10Error, match="settlement"):
        runner.run_one_arm_protocol(
            plan, session, hooks=failed_hooks, run_arm=lambda arm: _no_write()
        )
    assert events[-1] == "quarantined"


def test_comparator_is_plane_separated_no_write_and_never_executable_adoption() -> None:
    plan = _plan()
    result = compare.compare(plan, [_arm(plan, index) for index in range(8)])
    assert result["state"] == "candidate_for_operator_review"
    assert result["adoptable"] is False
    assert result["cross_plane_aggregation"] == "not_computed_distinct_plane_estimands"
    assert set(result["planes"]) == {"strict_detail", "authoritative_inventory"}
    assert all(
        item["median_equal_source_gain_percent"] == 20.0
        for item in result["planes"].values()
    )


def test_comparator_rejects_missing_source_or_write_claim() -> None:
    plan = _plan()
    arms = [_arm(plan, index) for index in range(8)]
    arms[0]["sources"] = arms[0]["sources"][:-1]
    with pytest.raises(contracts.C10Error, match="exactly match"):
        compare.compare(plan, arms)
    arms = [_arm(plan, index) for index in range(8)]
    arms[0]["no_write"]["cache_writes"] = 1
    with pytest.raises(contracts.C10Error, match="no-write"):
        compare.compare(plan, arms)


def test_policy_loader_rejects_duplicate_source_or_nonserial_worker(
    tmp_path: Path,
) -> None:
    document = json.loads(policy.DEFAULT_POLICY.read_text())
    document["sources"][1]["key"] = document["sources"][0]["key"]
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(document))
    with pytest.raises(contracts.C10Error, match="unique"):
        policy.load_policy(path)
    document = json.loads(policy.DEFAULT_POLICY.read_text())
    document["source_workers"] = 2
    path.write_text(json.dumps(document))
    with pytest.raises(contracts.C10Error, match="serial"):
        policy.load_policy(path)
