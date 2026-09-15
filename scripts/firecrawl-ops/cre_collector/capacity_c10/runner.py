"""Serial C10 runner protocol; Wave 1 intentionally has no live executor."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .contracts import (
    C10Error,
    claim_next_arm,
    new_session,
    require_no_write,
    validate_plan,
)


class SettlementHook(Protocol):
    def __call__(self, arm: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return complete, explicitly-idle local and remote settlement evidence."""


class RollbackHook(Protocol):
    def __call__(self, arm: Mapping[str, Any]) -> Mapping[str, Any]:
        """Restore and verify the exact P0 baseline after every P1 arm."""


class QuarantineHook(Protocol):
    def __call__(self, reason: str) -> None:
        """Retain the canonical lock when settlement or rollback is uncertain."""


@dataclass(frozen=True)
class SerialRunnerHooks:
    """Live wiring is injected later, never synthesized by a generic worker."""

    settle: SettlementHook
    rollback: RollbackHook
    quarantine: QuarantineHook


def _settlement_is_idle(value: Mapping[str, Any]) -> bool:
    return value.get("state") == "idle" and value.get("complete") is True


def run_one_arm_protocol(
    plan: Mapping[str, Any],
    session: Mapping[str, Any],
    *,
    hooks: SerialRunnerHooks,
    run_arm: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    """Exercise ownership/settlement/rollback ordering without network behavior.

    The future live coordinator must already hold the canonical `SharedLock` and
    prearm its active marker before calling this protocol.  This function has no
    filesystem, process, runtime, source, or database side effect itself.
    """
    validate_plan(plan)
    claimed = claim_next_arm(plan, session)
    arm = claimed["arm"]
    try:
        result = run_arm(arm)
        if not isinstance(result, Mapping):
            raise C10Error("C10 arm runner returned invalid evidence")
        require_no_write(result)
        settlement = hooks.settle(arm)
        if not isinstance(settlement, Mapping) or not _settlement_is_idle(settlement):
            raise C10Error("C10 arm settlement is unknown or non-idle")
        rollback = None
        if arm["must_rollback_to_p0"]:
            rollback = hooks.rollback(arm)
            if (
                not isinstance(rollback, Mapping)
                or rollback.get("verified") is not True
            ):
                raise C10Error("C10 P1 rollback is not verified")
            post_rollback = hooks.settle(arm)
            if not isinstance(post_rollback, Mapping) or not _settlement_is_idle(
                post_rollback
            ):
                raise C10Error("C10 post-rollback settlement is unknown or non-idle")
        return {
            "session": claimed["session"],
            "arm": arm,
            "result": dict(result),
            "rollback": rollback,
        }
    except BaseException as exc:
        hooks.quarantine(str(exc))
        raise


def initial_session(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Explicit name for the serial protocol's fresh one-use ledger."""
    return new_session(plan)
