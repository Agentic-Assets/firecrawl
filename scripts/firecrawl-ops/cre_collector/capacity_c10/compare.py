"""Pure, fail-closed C10 P0/P1 comparison over sealed arm summaries."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from statistics import median
from typing import Any

from .contracts import ARM_SEQUENCE, C10Error, require_no_write, validate_plan

MIN_GAIN_PERCENT = 15.0


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise C10Error(f"{label} must be a positive number")
    return float(value)


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
    sources = arm.get("sources")
    expected = {source["key"]: source["plane"] for source in plan["sources"]}
    if not isinstance(sources, list) or {
        item.get("key") for item in sources if isinstance(item, Mapping)
    } != set(expected):
        raise C10Error("C10 arm source evidence does not exactly match the cohort")
    rates: dict[str, float] = {}
    for source in sources:
        if (
            not isinstance(source, Mapping)
            or source.get("plane") != expected[source.get("key")]
            or source.get("qualified") is not True
        ):
            raise C10Error("C10 arm contains an unqualified source result")
        rates[source["key"]] = _number(
            source.get("qualified_rows_per_minute"), "source throughput"
        )
    return rates


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
