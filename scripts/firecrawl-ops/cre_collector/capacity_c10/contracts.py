"""Canonical, no-write contracts shared by the C10 admission and comparator."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA_VERSION = 1
ARM_SEQUENCE = ("p0", "p1", "p1", "p0", "p1", "p0", "p0", "p1")
PAIR_SEQUENCE = (("p0", "p1"), ("p1", "p0"), ("p1", "p0"), ("p0", "p1"))
PLAN_KIND = "cre_capacity_c10_v1_plan"
SESSION_KIND = "cre_capacity_c10_v1_session"
SHA256 = re.compile(r"[0-9a-f]{64}")


class C10Error(ValueError):
    """A C10 safety or evidence invariant did not hold."""


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
    if not isinstance(plan.get("profiles"), Mapping) or set(plan["profiles"]) != {
        "p0",
        "p1",
        "config_sha256",
    }:
        raise C10Error("C10 plan profiles are invalid")
    require_sha256(plan["profiles"]["config_sha256"], "profile config")
    if not isinstance(plan.get("sources"), list) or len(plan["sources"]) != 20:
        raise C10Error("C10 plan must retain exactly 20 sources")
    require_no_write(plan)
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if sha256(unsigned) != plan["plan_sha256"]:
        raise C10Error("C10 plan digest does not match its immutable contents")


def exact_source_keys(values: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    keys = tuple(item.get("key") for item in values)
    if len(keys) != len(set(keys)) or any(not isinstance(key, str) for key in keys):
        raise C10Error("C10 source keys must be unique strings")
    return keys
