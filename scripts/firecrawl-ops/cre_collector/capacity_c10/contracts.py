"""Canonical, no-write contracts shared by the C10 admission and comparator."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA_VERSION = 1
ARM_SEQUENCE = ("p0", "p1", "p1", "p0", "p1", "p0", "p0", "p1")
PAIR_SEQUENCE = (("p0", "p1"), ("p1", "p0"), ("p1", "p0"), ("p0", "p1"))
PLAN_KIND = "cre_capacity_c10_v1_plan"
SESSION_KIND = "cre_capacity_c10_v1_session"
SHA256 = re.compile(r"[0-9a-f]{64}")
PLANES = frozenset({"strict_detail", "authoritative_inventory"})
EXPECTED_PLANE_COUNTS = {"strict_detail": 12, "authoritative_inventory": 8}
PROFILE_NAMES = {"p0": "c10-p0", "p1": "c10-p1"}
P0_REQUESTED = {
    "browser_cpus": 2,
    "global_pages": 4,
    "jll_detail_concurrency": 4,
    "browser_pids": 384,
    "api_cpus": 1,
    "host_cpu_guard_percent": 90,
    "host_cpu_guard_seconds": 30,
    "host_cpu_sample_seconds": 2,
}
P1_REQUESTED = {
    **P0_REQUESTED,
    "browser_cpus": 6,
    "global_pages": 10,
    "jll_detail_concurrency": 10,
    "browser_pids": 768,
    "api_cpus": 2,
}
SOURCE_PLAN_FIELDS = {
    "key",
    "plane",
    "family",
    "cohort_member_count",
    "cohort_member_sha256",
    "enumeration_receipt_sha256",
}


class C10Error(ValueError):
    """A C10 safety or evidence invariant did not hold."""


_ADMISSION_TOKEN = object()
_COORDINATION_TOKEN = object()


class _AdmittedPlan(dict[str, Any]):
    """Process-local capability issued only after cohort and adapter admission."""

    __slots__ = ("_sealed_sha256", "_sealed")

    def __init__(self, value: Mapping[str, Any], token: object) -> None:
        if token is not _ADMISSION_TOKEN:
            raise C10Error("C10 admitted plans can only be issued by admission")
        super().__init__(json.loads(canonical_bytes(value)))
        object.__setattr__(self, "_sealed_sha256", sha256(dict(self)))
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("C10 admission capability is immutable")
        object.__setattr__(self, name, value)

    def admission_is_intact(self) -> bool:
        return self._sealed_sha256 == sha256(dict(self))


def _seal_admitted_plan(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Issue the opaque execution capability after ``admit_plan`` verifies inputs."""
    return _AdmittedPlan(value, _ADMISSION_TOKEN)


class _CoordinatedArm(dict[str, Any]):
    """Process-local capability issued from validated coordinator/ledger state."""

    __slots__ = ("_sealed_sha256",)

    def __init__(self, value: Mapping[str, Any], token: object) -> None:
        if token is not _COORDINATION_TOKEN:
            raise C10Error("C10 coordinated arms can only be issued internally")
        super().__init__(json.loads(canonical_bytes(value)))
        object.__setattr__(self, "_sealed_sha256", sha256(dict(self)))

    def coordination_is_intact(self) -> bool:
        return self._sealed_sha256 == sha256(dict(self))


def _seal_coordinated_arm(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Issue arm authority after coordinator or durable-ledger validation."""
    return _CoordinatedArm(value, _COORDINATION_TOKEN)


def require_coordinated_arm(value: Mapping[str, Any]) -> None:
    """Reject ordinary or mutated mappings at execution/comparison boundaries."""
    if type(value) is not _CoordinatedArm or not value.coordination_is_intact():
        raise C10Error("C10 arm is not authenticated coordinator evidence")


def canonical_bytes(value: Any) -> bytes:
    """Encode an evidence value with one stable, finite JSON representation."""
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError) as exc:
        raise C10Error("C10 evidence is not canonical JSON") from exc


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise C10Error(f"{label} must be a SHA-256 digest")
    return value


def require_no_write(value: Mapping[str, Any]) -> None:
    """Reject any arm/result that can be confused with a collector mutation."""
    expected = {
        "database_writes": 0,
        "cache_writes": 0,
        "status_writes": 0,
        "scheduler_writes": 0,
        "model_or_ocr_changes": 0,
    }
    observed = value.get("no_write")
    if not isinstance(observed, Mapping) or dict(observed) != expected:
        raise C10Error("C10 arm must prove the exact no-write contract")


def new_session(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Create the mutable one-use arm ledger separate from the immutable plan."""
    validate_plan(plan)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": SESSION_KIND,
        "plan_sha256": plan["plan_sha256"],
        "consumed_arm_indexes": [],
    }


def validate_session(plan: Mapping[str, Any], session: Mapping[str, Any]) -> None:
    """Require the canonical in-memory representation of a serial arm ledger."""
    validate_plan(plan)
    if (
        set(session)
        != {"schema_version", "kind", "plan_sha256", "consumed_arm_indexes"}
        or session.get("schema_version") != SCHEMA_VERSION
        or session.get("kind") != SESSION_KIND
        or session.get("plan_sha256") != plan["plan_sha256"]
        or not isinstance(session.get("consumed_arm_indexes"), list)
    ):
        raise C10Error("C10 session is not bound to this immutable plan")
    consumed = session["consumed_arm_indexes"]
    if any(type(index) is not int for index in consumed):
        raise C10Error("C10 session arm ledger is malformed")
    if consumed != list(range(len(consumed))) or len(consumed) > len(ARM_SEQUENCE):
        raise C10Error("C10 session arm ledger is not a canonical serial prefix")


def claim_next_arm(
    plan: Mapping[str, Any], session: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a new ledger and exactly one unconsumed arm; never reuse an arm."""
    validate_session(plan, session)
    consumed = session["consumed_arm_indexes"]
    next_index = next(
        (index for index in range(len(ARM_SEQUENCE)) if index not in consumed), None
    )
    if next_index is None:
        raise C10Error("all C10 arms have already been consumed")
    updated = dict(session)
    updated["consumed_arm_indexes"] = [*consumed, next_index]
    return {
        "session": updated,
        "arm": {
            "index": next_index,
            "variant": ARM_SEQUENCE[next_index],
            "pair_index": next_index // 2,
            "must_rollback_to_p0": ARM_SEQUENCE[next_index] == "p1",
        },
    }


def validate_plan(plan: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "kind",
        "policy_sha256",
        "cohort_sha256",
        "implementation_sha256",
        "profiles",
        "sources",
        "no_write",
        "arm_sequence",
        "plan_sha256",
    }
    if (
        set(plan) != required
        or plan.get("schema_version") != SCHEMA_VERSION
        or plan.get("kind") != PLAN_KIND
    ):
        raise C10Error("C10 plan schema is invalid")
    for key in (
        "policy_sha256",
        "cohort_sha256",
        "implementation_sha256",
        "plan_sha256",
    ):
        require_sha256(plan.get(key), key)
    if tuple(plan.get("arm_sequence", ())) != ARM_SEQUENCE:
        raise C10Error("C10 plan counterbalance is invalid")
    profiles = plan.get("profiles")
    if not isinstance(profiles, Mapping) or set(profiles) != {
        "p0",
        "p1",
        "config_sha256",
    }:
        raise C10Error("C10 plan profiles are invalid")
    require_sha256(profiles["config_sha256"], "profile config")
    for variant, expected in (("p0", P0_REQUESTED), ("p1", P1_REQUESTED)):
        profile = profiles.get(variant)
        if (
            not isinstance(profile, Mapping)
            or set(profile) != {"name", "requested"}
            or profile.get("name") != PROFILE_NAMES[variant]
            or profile.get("requested") != expected
        ):
            raise C10Error("C10 plan profiles do not match the fixed P0/P1 policy")
    sources = plan.get("sources")
    if (
        not isinstance(sources, list)
        or len(sources) != 20
        or any(not isinstance(source, Mapping) for source in sources)
    ):
        raise C10Error("C10 plan must retain exactly 20 sources")
    # Import lazily because policy loading itself uses canonical contract helpers.
    from .policy import load_policy

    fixed_policy = load_policy()
    if plan["policy_sha256"] != fixed_policy["policy_sha256"]:
        raise C10Error("C10 plan is not bound to the canonical fixed policy")
    policy_by_key = {source["key"]: source for source in fixed_policy["sources"]}
    source_keys = exact_source_keys(sources)
    if set(source_keys) != set(policy_by_key):
        raise C10Error("C10 plan source keys do not match the fixed policy")
    plane_counts: Counter[str] = Counter()
    for source in sources:
        if set(source) != SOURCE_PLAN_FIELDS:
            raise C10Error("C10 plan source schema is invalid")
        policy_source = policy_by_key[source["key"]]
        if (
            source.get("plane") != policy_source["plane"]
            or source.get("family") != policy_source["family"]
            or type(source.get("cohort_member_count")) is not int
            or source["cohort_member_count"] < 16
        ):
            raise C10Error("C10 plan source does not match the fixed policy")
        require_sha256(source.get("cohort_member_sha256"), "cohort member")
        require_sha256(source.get("enumeration_receipt_sha256"), "enumeration receipt")
        plane_counts[source["plane"]] += 1
    if dict(plane_counts) != EXPECTED_PLANE_COUNTS:
        raise C10Error("C10 plan no longer satisfies the fixed 12/8 plane allocation")
    require_no_write(plan)
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if sha256(unsigned) != plan["plan_sha256"]:
        raise C10Error("C10 plan digest does not match its immutable contents")
    if type(plan) is not _AdmittedPlan or not plan.admission_is_intact():
        raise C10Error("C10 plan lacks an authenticated cohort and adapter admission")


def exact_source_keys(values: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    keys = tuple(item.get("key") for item in values)
    if len(keys) != len(set(keys)) or any(not isinstance(key, str) for key in keys):
        raise C10Error("C10 source keys must be unique strings")
    return keys
