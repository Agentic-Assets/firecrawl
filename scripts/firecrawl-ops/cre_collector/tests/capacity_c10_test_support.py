"""Minimal sealed-C10 plan fixture shared by focused test modules."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from test_capacity_c10 import _cohort, _seal_cohort

from capacity_c10 import admission, contracts, policy
from capacity_c10.host_store import (
    C10SessionStore,
    _controller_ledger_authorization,
)


def controller_claim(
    store: C10SessionStore, plan: Mapping[str, Any]
) -> Mapping[str, Any]:
    deadline = time.monotonic() + 30
    _index, session = store._next(plan)
    arm = contracts.claim_next_arm(plan, session)["arm"]
    with _controller_ledger_authorization(
        "claim", plan, arm, attempt=None, deadline=deadline
    ):
        return store.claim(plan, deadline=deadline)


def controller_terminal(
    store: C10SessionStore,
    plan: Mapping[str, Any],
    claim: Mapping[str, Any],
    authenticated_arm: Mapping[str, Any],
) -> Mapping[str, Any]:
    deadline = time.monotonic() + 30
    arm, claim_id = claim.get("arm"), claim.get("claim_id")
    assert isinstance(arm, Mapping) and isinstance(claim_id, str)
    with _controller_ledger_authorization(
        "terminal", plan, arm, attempt=claim_id, deadline=deadline
    ):
        return store.record_terminal(plan, claim, authenticated_arm, deadline=deadline)


def sealed_jll_plan() -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the sealed JLL cohort used by host and production tests."""
    cohort = _cohort()
    jll = next(source for source in cohort["sources"] if source["source_key"] == "jll")
    for index, member in enumerate(jll["core"]):
        member["provider_id"] = str(index + 1)
        member["canonical_url"] = (
            f"https://property.jll.com/listings/member-{index + 1}"
        )
    _seal_cohort(cohort)
    loaded_policy = policy.load_policy()
    sources = admission._verified_cohort_sources(cohort, loaded_policy)
    profiles = admission._profiles(admission.PROFILE_CONFIG, loaded_policy["profiles"])
    unsigned = {
        "schema_version": 1,
        "kind": contracts.PLAN_KIND,
        "policy_sha256": loaded_policy["policy_sha256"],
        "cohort_sha256": cohort["cohort_sha256"],
        "implementation_sha256": "0" * 64,
        "profiles": profiles,
        "sources": sources,
        "no_write": admission.NO_WRITE,
        "arm_sequence": list(contracts.ARM_SEQUENCE),
    }
    return {**unsigned, "plan_sha256": contracts.sha256(unsigned)}, cohort
