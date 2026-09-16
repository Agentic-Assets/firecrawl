"""Fail-closed C10 P0/P1 comparison over sealed browser-rendered arm evidence."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
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
REVIEWED_BROWSER_ENGINE = "playwright"


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
    expected_scheduled_member_count = sum(
        source["cohort_member_count"] for source in plan["sources"]
    )
    if (
        scheduler.get("configured_concurrency") != configured
        or scheduler.get("observed_max_active") != configured
        or type(scheduler.get("scheduled_member_count")) is not int
        or scheduler["scheduled_member_count"] != expected_scheduled_member_count
    ):
        raise C10Error("C10 scheduler did not demonstrate the planned saturation")
    expected_sources = plan["sources"]
    expected = {source["key"]: source for source in expected_sources}
    sources = evidence.get("sources")
    if (
        not isinstance(sources, list)
        or len(sources) != len(expected)
        or any(not isinstance(source, Mapping) for source in sources)
    ):
        raise C10Error("C10 browser evidence does not exactly match the cohort")
    source_keys = [source.get("key") for source in sources]
    if any(not isinstance(key, str) for key in source_keys) or source_keys != [
        source["key"] for source in expected_sources
    ]:
        raise C10Error("C10 browser evidence does not exactly match the cohort")
    rates: dict[str, float] = {}
    previous_finished = started
    for source in sources:
        if not isinstance(source, Mapping) or set(source) != {
            "key",
            "plane",
            "cohort_member_count",
            "cohort_member_sha256",
            "scheduled_member_count",
            "scheduled_member_sha256",
            "execution_mode",
            "engine",
            "client_attempts",
            "engine_attempts",
            "cache_read",
            "cache_write",
            "started_monotonic_ns",
            "finished_monotonic_ns",
            "qualified_rows",
        }:
            raise C10Error("C10 browser source evidence schema is invalid")
        key = source.get("key")
        expected_source = expected.get(key)
        source_started = _positive_int(
            source.get("started_monotonic_ns"), "source start"
        )
        source_finished = _positive_int(
            source.get("finished_monotonic_ns"), "source finish"
        )
        if (
            expected_source is None
            or source.get("plane") != expected_source["plane"]
            or source.get("cohort_member_count")
            != expected_source["cohort_member_count"]
            or source.get("cohort_member_sha256")
            != expected_source["cohort_member_sha256"]
            or source.get("scheduled_member_count")
            != expected_source["cohort_member_count"]
            or source.get("scheduled_member_sha256")
            != expected_source["cohort_member_sha256"]
            or source.get("execution_mode") != "browser_rendered"
            or source.get("engine") != REVIEWED_BROWSER_ENGINE
            or source.get("client_attempts") != 1
            or source.get("engine_attempts") != 1
            or source.get("cache_read") is not False
            or source.get("cache_write") is not False
        ):
            raise C10Error("C10 source is not one browser-rendered no-cache attempt")
        if (
            source_finished <= source_started
            or source_started < started
            or source_finished > finished
            or source_started < previous_finished
        ):
            raise C10Error("C10 source timing is outside the serial browser arm")
        previous_finished = source_finished
        qualified_rows = source.get("qualified_rows")
        if (
            type(qualified_rows) is not int
            or qualified_rows < 0
            or qualified_rows > source["scheduled_member_count"]
        ):
            raise C10Error("qualified rows must be within the immutable cohort")
        rates[key] = qualified_rows / (
            (source_finished - source_started) / 60_000_000_000
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
    validate_plan(plan)
    index = arm.get("index")
    if type(index) is not int or index < 0 or index >= len(ARM_SEQUENCE):
        raise C10Error("C10 browser arm index is invalid")
    _validated_arm(plan, arm, index)


def compare(plan: Mapping[str, Any], session_store: Any) -> dict[str, Any]:
    """Compare all four matched pairs from the canonical durable arm ledger.

    Arbitrary mappings are deliberately not a comparison input. The store
    reopens its owner-only, hash-validated ledger while locked and returns the
    exact terminal results committed after coordinator settlement.
    """
    from .session_store import DurableArmSessionStore

    validate_plan(plan)
    if type(session_store) is not DurableArmSessionStore:
        raise C10Error("C10 comparison requires the canonical durable arm ledger")
    arms = session_store.terminal_results(plan)
    if len(arms) != len(ARM_SEQUENCE):
        raise C10Error("C10 comparison requires all eight counterbalanced arms")
    rates = [_validated_arm(plan, arm, index) for index, arm in enumerate(arms)]
    plane_by_key = {source["key"]: source["plane"] for source in plan["sources"]}
    gains: dict[str, list[float | None]] = defaultdict(list)
    zero_rate_counts: dict[str, int] = defaultdict(int)
    for left, right in zip(
        range(0, len(rates), 2), range(1, len(rates), 2), strict=True
    ):
        p0_index, p1_index = (
            (left, right) if ARM_SEQUENCE[left] == "p0" else (right, left)
        )
        for key, plane in plane_by_key.items():
            p0_rate = rates[p0_index][key]
            p1_rate = rates[p1_index][key]
            if p0_rate == 0 or p1_rate == 0:
                zero_rate_counts[plane] += 1
            gains[plane].append(
                None if p0_rate == 0 else (p1_rate / p0_rate - 1.0) * 100
            )
    planes = {}
    for plane, values in sorted(gains.items()):
        measurable = [value for value in values if value is not None]
        planes[plane] = {
            "median_equal_source_gain_percent": round(float(median(measurable)), 3)
            if measurable
            else None,
            "pair_source_gains": values,
            "zero_rate_count": zero_rate_counts[plane],
        }
    meets_gain_threshold = all(
        result["zero_rate_count"] == 0
        and result["median_equal_source_gain_percent"] >= MIN_GAIN_PERCENT
        for result in planes.values()
    )
    return {
        # This offline/library wave has no non-forgeable host attestation. It
        # may summarize a ledger for engineering analysis, but it must never
        # promote caller-controlled hooks or stores into an adoption candidate.
        "state": "offline_measurement_only",
        "adoptable": False,
        "meets_gain_threshold": meets_gain_threshold,
        "minimum_gain_percent": MIN_GAIN_PERCENT,
        "cross_plane_aggregation": "not_computed_distinct_plane_estimands",
        "planes": planes,
    }
