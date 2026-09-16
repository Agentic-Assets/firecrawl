"""Build immutable C10 plans from sealed v1 cohorts and reviewed adapters."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import cre_capacity_experiment as experiment

from .adapters import C10SourceAdapter, default_registry, verified_registry
from .authority import load_authority, repository_implementation_sha256
from .contracts import (
    ARM_SEQUENCE,
    EXPECTED_PLANE_COUNTS,
    P0_REQUESTED,
    P1_REQUESTED,
    PLAN_KIND,
    C10Error,
    require_sha256,
    sha256,
    validate_plan,
)
from .policy import DEFAULT_POLICY, load_policy

PROFILE_CONFIG = Path(__file__).parent.parent / "cre_capacity_c10_profiles_v1.json"
NO_WRITE = {
    "database_writes": 0,
    "cache_writes": 0,
    "status_writes": 0,
    "scheduler_writes": 0,
    "model_or_ocr_changes": 0,
}
P0_P1_MUTABLE_FIELDS = frozenset(
    {
        "browser_cpus",
        "global_pages",
        "jll_detail_concurrency",
        "browser_pids",
        "api_cpus",
    }
)
COHORT_HASH_FIELDS = (
    "schema_version",
    "config_sha256",
    "sampling",
    "sources",
    "planes",
    "aggregate",
)


def _verify_cohort_digest(cohort: Mapping[str, Any]) -> None:
    """Bind C10 admission to the source cohort's published v1 hash recipe."""
    if any(key not in cohort for key in COHORT_HASH_FIELDS):
        raise C10Error("C10 cohort is missing a v1 hash-bound field")
    require_sha256(cohort.get("config_sha256"), "cohort config")
    expected = sha256({key: cohort[key] for key in COHORT_HASH_FIELDS})
    if cohort.get("cohort_sha256") != expected:
        raise C10Error("C10 cohort digest does not bind its immutable membership")


def _verified_cohort_sources(
    cohort: Mapping[str, Any], policy: Mapping[str, Any]
) -> list[dict[str, Any]]:
    if (
        cohort.get("schema_version") != 1
        or cohort.get("kind") != "cre_capacity_multisource_v1_cohort"
        or cohort.get("aggregate", {}).get("state") != "ready_for_review"
    ):
        raise C10Error("C10 requires a complete v1 cohort ready for review")
    require_sha256(cohort.get("cohort_sha256"), "cohort")
    _verify_cohort_digest(cohort)
    sources = cohort.get("sources")
    if not isinstance(sources, list) or len(sources) != 20:
        raise C10Error("C10 cohort must retain exactly 20 sources")
    policy_by_key = {source["key"]: source for source in policy["sources"]}
    observed_keys = {
        source.get("source_key") for source in sources if isinstance(source, Mapping)
    }
    if observed_keys != set(policy_by_key):
        raise C10Error("C10 cohort source keys do not match the fixed policy")
    normalized: list[dict[str, Any]] = []
    plane_counts: Counter[str] = Counter()
    for source in sources:
        if not isinstance(source, Mapping):
            raise C10Error("C10 cohort source is invalid")
        key = source.get("source_key")
        policy_source = policy_by_key[key]
        if (
            source.get("plane") != policy_source["plane"]
            or source.get("core_state") != "ready"
        ):
            raise C10Error(
                f"C10 cohort source {key} is not ready for its declared plane"
            )
        fresh = source.get("fresh_enumeration")
        core = source.get("core")
        if (
            not isinstance(fresh, Mapping)
            or fresh.get("population_state") != "verified"
            or type(fresh.get("total_population")) is not int
            or fresh["total_population"] < 1
            or not isinstance(core, list)
            or len(core) != source.get("core_target_rows")
            or len(core) != source.get("core_selected_rows")
            or len(core) < 16
        ):
            raise C10Error(
                f"C10 cohort source {key} has incomplete immutable membership"
            )
        plane_counts[policy_source["plane"]] += 1
        normalized.append(
            {
                "key": key,
                "plane": policy_source["plane"],
                "family": policy_source["family"],
                "cohort_member_count": len(core),
                "cohort_member_sha256": sha256(core),
                "enumeration_receipt_sha256": fresh.get("receipt_sha256"),
            }
        )
    if dict(plane_counts) != EXPECTED_PLANE_COUNTS:
        raise C10Error("C10 cohort no longer satisfies the 12/8 plane floor")
    for source in normalized:
        require_sha256(source["enumeration_receipt_sha256"], "enumeration receipt")
    return normalized


def _profiles(profile_config: Path, names: Mapping[str, Any]) -> dict[str, Any]:
    p0, p0_digest = experiment.load_profile(profile_config, names["p0"])
    p1, p1_digest = experiment.load_profile(profile_config, names["p1"])
    if (
        p0_digest != p1_digest
        or p0["requested"] != P0_REQUESTED
        or p1["requested"] != P1_REQUESTED
    ):
        raise C10Error("C10 profile settings do not match the reviewed P0/P1 tuple")
    if p0["runtime_baseline"] != p1["runtime_baseline"]:
        raise C10Error("C10 P0/P1 must share the exact runtime baseline")
    changed = {
        key for key in P0_REQUESTED if p0["requested"][key] != p1["requested"][key]
    }
    if changed != P0_P1_MUTABLE_FIELDS:
        raise C10Error("C10 P0/P1 differ outside the resource whitelist")
    return {
        "p0": {"name": names["p0"], "requested": p0["requested"]},
        "p1": {"name": names["p1"], "requested": p1["requested"]},
        "config_sha256": p0_digest,
    }


def unsigned_plan(
    cohort: Mapping[str, Any],
    policy: Mapping[str, Any],
    adapter_implementation_sha256: Mapping[str, str],
    *,
    profile_config: Path = PROFILE_CONFIG,
) -> dict[str, Any]:
    """Build the canonical unsigned plan from already-reviewed adapter bytes."""
    expected = {source["key"] for source in policy["sources"]}
    if set(adapter_implementation_sha256) != expected:
        raise C10Error("C10 plan adapter implementations do not match the fixed policy")
    for key, digest in adapter_implementation_sha256.items():
        require_sha256(digest, f"C10 adapter {key} implementation")
    sources = _verified_cohort_sources(cohort, policy)
    profiles = _profiles(profile_config, policy["profiles"])
    return {
        "schema_version": 1,
        "kind": PLAN_KIND,
        "policy_sha256": policy["policy_sha256"],
        "cohort_sha256": cohort["cohort_sha256"],
        "implementation_sha256": sha256(dict(adapter_implementation_sha256)),
        "profiles": profiles,
        "sources": sources,
        "no_write": NO_WRITE,
        "arm_sequence": list(ARM_SEQUENCE),
    }


def admit_plan(
    cohort: Mapping[str, Any],
    *,
    policy_path: Path = DEFAULT_POLICY,
    profile_config: Path = PROFILE_CONFIG,
    registry: Mapping[str, C10SourceAdapter] | None = None,
) -> dict[str, Any]:
    """Return a sealed plan; source/runtime execution remains outside Wave 1."""
    policy = load_policy(policy_path)
    authority = load_authority()
    approved_cohort_sha256 = authority["approved_cohort_sha256"]
    if (
        approved_cohort_sha256 is None
        or cohort.get("cohort_sha256") != approved_cohort_sha256
    ):
        raise C10Error("C10 cohort is not approved by repository authority")
    adapters = verified_registry(
        policy, default_registry() if registry is None else registry
    )
    unsigned = unsigned_plan(
        cohort,
        policy,
        {key: repository_implementation_sha256(key) for key in sorted(adapters)},
        profile_config=profile_config,
    )
    plan = {**unsigned, "plan_sha256": sha256(unsigned)}
    validate_plan(plan)
    return plan
