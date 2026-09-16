"""Fail-closed C10 P0/P1 comparison over sealed browser-rendered arm evidence."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from statistics import median
from typing import Any

from .contracts import (
    ARM_SEQUENCE,
    C10Error,
    require_no_write,
    require_sha256,
    sha256,
    validate_plan,
)

MIN_GAIN_PERCENT = 15.0
EVIDENCE_KIND = "cre_capacity_c10_browser_arm_evidence_v1"
HOST_EVIDENCE_KIND = "cre_capacity_c10_authenticated_host_arm_v1"


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise C10Error(f"{label} must be a positive integer")
    return value


def _browser_evidence_rates(
    plan: Mapping[str, Any], arm: Mapping[str, Any], index: int
) -> dict[str, float]:
    """Validate a sealed browser arm and derive, rather than accept, rates."""
    evidence = arm.get("sealed_browser_evidence")
    if not isinstance(evidence, Mapping):
        raise C10Error("C10 comparator requires sealed browser-rendered evidence")
    supplied = evidence.get("evidence_sha256")
    unsigned = {
        key: value for key, value in evidence.items() if key != "evidence_sha256"
    }
    if require_sha256(supplied, "browser evidence") != sha256(unsigned):
        raise C10Error("C10 browser evidence digest is invalid")
    required = {
        "kind",
        "plan_sha256",
        "index",
        "variant",
        "runtime",
        "started_monotonic_ns",
        "finished_monotonic_ns",
        "request",
        "scheduler",
        "sources",
        "evidence_sha256",
    }
    if set(evidence) != required or evidence.get("kind") != EVIDENCE_KIND:
        raise C10Error("C10 browser evidence schema is invalid")
    if (
        evidence.get("plan_sha256") != plan["plan_sha256"]
        or evidence.get("index") != index
        or evidence.get("variant") != ARM_SEQUENCE[index]
    ):
        raise C10Error("C10 browser evidence is not bound to the immutable arm")
    started = _positive_int(evidence.get("started_monotonic_ns"), "arm start")
    finished = _positive_int(evidence.get("finished_monotonic_ns"), "arm finish")
    if finished <= started:
        raise C10Error("C10 browser arm timing is not monotonic")
    request = evidence.get("request")
    if request != {"maxAge": 0, "storeInCache": False}:
        raise C10Error(
            "C10 browser evidence does not prove maxAge=0 and no cache write"
        )
    variant_profile = plan["profiles"][ARM_SEQUENCE[index]]
    runtime = evidence.get("runtime")
    if not isinstance(runtime, Mapping) or set(runtime) != {
        "profile_config_sha256",
        "profile_requested_sha256",
        "runtime_receipt_sha256",
        "container_snapshot_sha256",
        "transition_sha256",
    }:
        raise C10Error("C10 browser evidence runtime fingerprint schema is invalid")
    if runtime.get("profile_config_sha256") != plan["profiles"]["config_sha256"]:
        raise C10Error("C10 browser evidence uses a different profile configuration")
    if runtime.get("profile_requested_sha256") != sha256(variant_profile["requested"]):
        raise C10Error("C10 browser evidence uses a different requested profile")
    for key, value in runtime.items():
        require_sha256(value, f"C10 runtime {key}")
    scheduler = evidence.get("scheduler")
    configured = variant_profile["requested"].get("jll_detail_concurrency")
    if (
        configured not in {4, 10}
        or not isinstance(scheduler, Mapping)
        or set(scheduler)
        != {"configured_concurrency", "observed_max_active", "scheduled_member_count"}
    ):
        raise C10Error("C10 scheduler evidence is invalid")
    if (
        scheduler.get("configured_concurrency") != configured
        or scheduler.get("observed_max_active") != configured
        or type(scheduler.get("scheduled_member_count")) is not int
        or scheduler["scheduled_member_count"] < configured
    ):
        raise C10Error("C10 scheduler did not demonstrate the planned saturation")
    expected = {source["key"]: source["plane"] for source in plan["sources"]}
    sources = evidence.get("sources")
    if not isinstance(sources, list) or {
        source.get("key") for source in sources if isinstance(source, Mapping)
    } != set(expected):
        raise C10Error("C10 browser evidence does not exactly match the cohort")
    elapsed_minutes = (finished - started) / 60_000_000_000
    rates: dict[str, float] = {}
    for source in sources:
        if not isinstance(source, Mapping) or set(source) != {
            "key",
            "plane",
            "execution_mode",
            "engine",
            "client_attempts",
            "engine_attempts",
            "cache_read",
            "cache_write",
            "qualified_rows",
        }:
            raise C10Error("C10 browser source evidence schema is invalid")
        key = source.get("key")
        if (
            source.get("plane") != expected.get(key)
            or source.get("execution_mode") != "browser_rendered"
            or not isinstance(source.get("engine"), str)
            or not source["engine"]
            or source.get("client_attempts") != 1
            or source.get("engine_attempts") != 1
            or source.get("cache_read") is not False
            or source.get("cache_write") is not False
        ):
            raise C10Error("C10 source is not one browser-rendered no-cache attempt")
        rates[key] = (
            _positive_int(source.get("qualified_rows"), "qualified rows")
            / elapsed_minutes
        )
    return rates


def _validated_arm(
    plan: Mapping[str, Any], arm: Mapping[str, Any], index: int
) -> dict[str, float]:
    if (
        arm.get("plan_sha256") != plan["plan_sha256"]
        or arm.get("index") != index
        or arm.get("variant") != ARM_SEQUENCE[index]
        or arm.get("terminal") is not True
    ):
        raise C10Error("C10 arm is not bound to the immutable counterbalanced plan")
    require_no_write(arm)
    # Deliberately do not read a caller-provided scalar throughput field.
    return _browser_evidence_rates(plan, arm, index)


def validate_browser_arm(plan: Mapping[str, Any], arm: Mapping[str, Any]) -> None:
    """Reject an unsaturated or non-browser arm before it can become terminal."""
    index = arm.get("index")
    if type(index) is not int or index < 0 or index >= len(ARM_SEQUENCE):
        raise C10Error("C10 browser arm index is invalid")
    _validated_arm(plan, arm, index)


def validate_authenticated_host_arm(
    plan: Mapping[str, Any], arm: Mapping[str, Any]
) -> None:
    """Accept only a sealed host result for durable terminalization.

    The present host implements the JLL browser lane only. Its authenticated
    result is intentionally *not* coerced into the 20-source rate comparator:
    doing so would turn missing source evidence into invented qualified rows.
    This validator is the narrow handoff from the host to the durable arm
    ledger, and ``compare`` remains unavailable until all twenty sources have
    authenticated comparable evidence.
    """
    validate_plan(plan)
    required = {
        "kind",
        "plan_sha256",
        "index",
        "variant",
        "no_write",
        "runtime",
        "host_result",
    }
    if set(arm) != required or arm.get("kind") != HOST_EVIDENCE_KIND:
        raise C10Error("C10 authenticated host arm schema is invalid")
    index = arm.get("index")
    if (
        type(index) is not int
        or index < 0
        or index >= len(ARM_SEQUENCE)
        or arm.get("plan_sha256") != plan["plan_sha256"]
        or arm.get("variant") != ARM_SEQUENCE[index]
    ):
        raise C10Error("C10 authenticated host arm is not plan-bound")
    require_no_write(arm)
    runtime = arm.get("runtime")
    if not isinstance(runtime, Mapping) or set(runtime) != {
        "profile_config_sha256",
        "profile_requested_sha256",
        "runtime_receipt_sha256",
        "container_snapshot_sha256",
        "transition_sha256",
    }:
        raise C10Error("C10 authenticated host arm runtime is invalid")
    profile = plan["profiles"][ARM_SEQUENCE[index]]
    if runtime.get("profile_config_sha256") != plan["profiles"][
        "config_sha256"
    ] or runtime.get("profile_requested_sha256") != sha256(profile["requested"]):
        raise C10Error("C10 authenticated host arm runtime differs from the plan")
    for key, value in runtime.items():
        require_sha256(value, f"C10 host runtime {key}")
    host = arm.get("host_result")
    if not isinstance(host, Mapping) or set(host) != {
        "claim",
        "private_artifacts",
        "evidence_manifest_sha256",
        "binding",
    }:
        raise C10Error("C10 authenticated host result is invalid")
    claim = host.get("claim")
    artifacts = host.get("private_artifacts")
    binding = host.get("binding")
    if (
        not isinstance(claim, Mapping)
        or not isinstance(artifacts, list)
        or len(artifacts) != 17
        or not isinstance(binding, Mapping)
        or claim.get("plan_sha256") != plan["plan_sha256"]
        or not isinstance(claim.get("arm"), Mapping)
        or claim["arm"].get("index") != index
        or claim["arm"].get("variant") != ARM_SEQUENCE[index]
        or binding.get("planSha256") != plan["plan_sha256"]
        or binding.get("cohortSha256") != plan["cohort_sha256"]
    ):
        raise C10Error("C10 authenticated host result is not bound to the arm")
    require_sha256(host.get("evidence_manifest_sha256"), "C10 host manifest")
    for artifact in artifacts:
        if (
            not isinstance(artifact, Mapping)
            or set(artifact) != {"name", "sha256", "bytes"}
            or not isinstance(artifact.get("name"), str)
            or type(artifact.get("bytes")) is not int
            or artifact["bytes"] <= 0
        ):
            raise C10Error("C10 authenticated host artifact is invalid")
        require_sha256(artifact.get("sha256"), "C10 host artifact")


def compare(
    plan: Mapping[str, Any], arms: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Compare all four matched pairs; never make runtime adoption executable."""
    validate_plan(plan)
    if len(arms) != len(ARM_SEQUENCE):
        raise C10Error("C10 comparison requires all eight counterbalanced arms")
    rates = [_validated_arm(plan, arm, index) for index, arm in enumerate(arms)]
    plane_by_key = {source["key"]: source["plane"] for source in plan["sources"]}
    gains: dict[str, list[float]] = defaultdict(list)
    for left, right in zip(
        range(0, len(rates), 2), range(1, len(rates), 2), strict=True
    ):
        p0_index, p1_index = (
            (left, right) if ARM_SEQUENCE[left] == "p0" else (right, left)
        )
        for key, plane in plane_by_key.items():
            gains[plane].append(
                (rates[p1_index][key] / rates[p0_index][key] - 1.0) * 100
            )
    planes = {
        plane: {
            "median_equal_source_gain_percent": round(float(median(values)), 3),
            "pair_source_gains": values,
        }
        for plane, values in sorted(gains.items())
    }
    qualified = all(
        result["median_equal_source_gain_percent"] >= MIN_GAIN_PERCENT
        for result in planes.values()
    )
    return {
        "state": "candidate_for_operator_review"
        if qualified
        else "measured_not_adoptable",
        "adoptable": False,
        "minimum_gain_percent": MIN_GAIN_PERCENT,
        "cross_plane_aggregation": "not_computed_distinct_plane_estimands",
        "planes": planes,
    }
