"""Minimal sealed-C10 plan fixture shared by focused test modules."""

from __future__ import annotations

from typing import Any

from capacity_c10 import admission
from test_capacity_c10 import _cohort, _registry, _seal_cohort


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
    return admission.admit_plan(cohort, registry=_registry()), cohort
