"""Serial C10 runner protocol; Wave 1 intentionally has no live executor."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from cre_checkpoint_refresh import SharedLock

from . import admission
from .compare import validate_browser_arm
from .contracts import (
    C10Error,
    claim_next_arm,
    new_session,
    require_no_write,
    require_sha256,
    sha256,
    validate_plan,
)
from .session_store import DurableArmSessionStore


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


class BrowserArmHook(Protocol):
    def __call__(
        self, arm: Mapping[str, Any], scheduler_concurrency: int
    ) -> Mapping[str, Any]:
        """Return browser-only raw evidence; the coordinator seals it."""


@dataclass(frozen=True)
class C10CoordinatorHooks:
    """Explicit bindings for the future C10 runtime coordinator.

    The coordinator has no command-line entrypoint.  A caller must provide the
    existing runtime controller functions, a canonical SharedLock factory, and
    the browser executor.  That makes a real run an explicit operator-wired
    action rather than an accidental consequence of plan admission.
    """

    preflight: Callable[..., Mapping[str, Any]]
    transition: Callable[..., Mapping[str, Any]]
    run_browser_arm: BrowserArmHook
    settle: SettlementHook
    quarantine: QuarantineHook
    lock_factory: Callable[[Path], SharedLock]
    canonical_lock_path: Callable[[], Path]


@dataclass(frozen=True)
class C10CoordinatorPaths:
    """Private controller paths required for a single, explicitly armed arm."""

    session_path: Path
    receipt_path: Path
    approval_path: Path | None = None
    admission_out: Path | None = None


def _scheduler_concurrency(plan: Mapping[str, Any], arm: Mapping[str, Any]) -> int:
    requested = plan["profiles"][arm["variant"]]["requested"]
    concurrency = requested.get("jll_detail_concurrency")
    if concurrency not in {4, 10}:
        raise C10Error("C10 arm has no reviewed P0/P1 scheduler concurrency")
    return concurrency


def _runtime_fingerprints(
    plan: Mapping[str, Any],
    arm: Mapping[str, Any],
    receipt: Mapping[str, Any],
    transition: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Bind browser evidence to the profile and controller observation in use."""
    profile = plan["profiles"][arm["variant"]]
    baseline = receipt.get("baseline")
    if (
        receipt.get("profile") != profile["name"]
        or receipt.get("config_sha256") != plan["profiles"]["config_sha256"]
        or not isinstance(baseline, Mapping)
    ):
        raise C10Error("C10 runtime receipt does not bind the planned profile")
    receipt_sha256 = require_sha256(receipt.get("receipt_sha256"), "runtime receipt")
    profile_requested_sha256 = sha256(profile["requested"])
    snapshot = baseline.get("snapshot_sha256")
    transition_sha = baseline.get("transition_sha256")
    if transition is not None:
        if (
            transition.get("profile") != profile["name"]
            or transition.get("state") != "candidate"
            or transition.get("verified") is not True
        ):
            raise C10Error("C10 candidate transition is not verified")
        snapshot = transition.get("container_snapshot_sha256")
        transition_sha = transition.get("transition_sha256")
    return {
        "profile_config_sha256": require_sha256(
            plan["profiles"]["config_sha256"], "profile config"
        ),
        "profile_requested_sha256": require_sha256(
            profile_requested_sha256, "profile requested"
        ),
        "runtime_receipt_sha256": receipt_sha256,
        "container_snapshot_sha256": require_sha256(snapshot, "container snapshot"),
        "transition_sha256": require_sha256(transition_sha, "runtime transition"),
    }


def _sealed_browser_arm(
    plan: Mapping[str, Any],
    arm: Mapping[str, Any],
    raw: Mapping[str, Any],
    runtime_fingerprints: Mapping[str, str],
) -> dict[str, Any]:
    """Make the comparator's sole accepted browser evidence envelope.

    ``raw`` is intentionally narrow.  In particular it has no caller-supplied
    throughput field: qualified rows and monotonic arm timing are sealed before
    the comparator derives rates.
    """
    expected = {
        "started_monotonic_ns",
        "finished_monotonic_ns",
        "request",
        "scheduler",
        "sources",
    }
    if set(raw) != expected:
        raise C10Error("C10 browser arm raw evidence has an unexpected schema")
    evidence = {
        "kind": "cre_capacity_c10_browser_arm_evidence_v1",
        "plan_sha256": plan["plan_sha256"],
        "index": arm["index"],
        "variant": arm["variant"],
        "runtime": dict(runtime_fingerprints),
        **dict(raw),
    }
    return {**evidence, "evidence_sha256": sha256(evidence)}


def run_one_coordinated_arm(
    plan: Mapping[str, Any],
    session: Mapping[str, Any],
    *,
    paths: C10CoordinatorPaths,
    hooks: C10CoordinatorHooks,
) -> dict[str, Any]:
    """Run one explicitly armed C10 arm while holding the canonical lock.

    This is a coordinator skeleton, not a live executor: callers inject the
    browser transport and the established controller functions.  It keeps the
    same ``SharedLock`` across preflight, candidate transition, browser arm,
    settlement, P1 rollback, and quarantine.  Failure therefore cannot race a
    second experiment between a partial transition and its forensic handoff.
    """
    validate_plan(plan)
    lock_path = hooks.canonical_lock_path()
    lock = hooks.lock_factory(lock_path)
    if getattr(lock, "path", lock_path) != lock_path:
        raise C10Error("C10 coordinator did not receive the canonical SharedLock")
    lock.acquire()
    try:
        with DurableArmSessionStore(paths.session_path) as session_store:
            claimed = session_store.claim(plan, session)
            arm = claimed["arm"]
            profile = plan["profiles"][arm["variant"]]
            receipt = hooks.preflight(
                profile["name"],
                paths.receipt_path,
                profile_config=admission.PROFILE_CONFIG,
                experiment_kind="C10",
            )
            if not isinstance(receipt, Mapping):
                raise C10Error("C10 preflight did not return a runtime receipt")
            candidate_transition: Mapping[str, Any] | None = None
            if arm["variant"] == "p1":
                if paths.approval_path is None or paths.admission_out is None:
                    raise C10Error(
                        "C10 P1 arm requires explicit approval and admission paths"
                    )
                candidate_transition = hooks.transition(
                    paths.receipt_path,
                    profile["name"],
                    "candidate",
                    execute=True,
                    approval_path=paths.approval_path,
                    admission_out=paths.admission_out,
                    _held_shared_lock=lock,
                    profile_config=admission.PROFILE_CONFIG,
                    experiment_kind="C10",
                )
                if not isinstance(candidate_transition, Mapping):
                    raise C10Error("C10 candidate transition returned invalid evidence")
            raw = hooks.run_browser_arm(arm, _scheduler_concurrency(plan, arm))
            if not isinstance(raw, Mapping):
                raise C10Error("C10 browser arm returned invalid evidence")
            result = {
                "plan_sha256": plan["plan_sha256"],
                "index": arm["index"],
                "variant": arm["variant"],
                "terminal": True,
                "no_write": plan["no_write"],
                "sealed_browser_evidence": _sealed_browser_arm(
                    plan,
                    arm,
                    raw,
                    _runtime_fingerprints(plan, arm, receipt, candidate_transition),
                ),
            }
            validate_browser_arm(plan, result)
            settlement = hooks.settle(arm)
            if not isinstance(settlement, Mapping) or not _settlement_is_idle(
                settlement
            ):
                raise C10Error("C10 arm settlement is unknown or non-idle")
            rollback: Mapping[str, Any] | None = None
            if arm["must_rollback_to_p0"]:
                rollback = hooks.transition(
                    paths.receipt_path,
                    profile["name"],
                    "baseline",
                    execute=True,
                    _held_shared_lock=lock,
                    profile_config=admission.PROFILE_CONFIG,
                    experiment_kind="C10",
                )
                if (
                    not isinstance(rollback, Mapping)
                    or rollback.get("verified") is not True
                ):
                    raise C10Error("C10 P1 rollback is not verified")
                baseline = receipt["baseline"]
                if (
                    not isinstance(baseline, Mapping)
                    or rollback.get("container_snapshot_sha256")
                    != baseline.get("snapshot_sha256")
                    or rollback.get("transition_sha256")
                    != baseline.get("transition_sha256")
                ):
                    raise C10Error(
                        "C10 P1 rollback fingerprint does not restore baseline"
                    )
                post_rollback = hooks.settle(arm)
                if not isinstance(post_rollback, Mapping) or not _settlement_is_idle(
                    post_rollback
                ):
                    raise C10Error(
                        "C10 post-rollback settlement is unknown or non-idle"
                    )
            session_store.mark_terminal(plan, arm, result)
            return {
                "session": claimed["session"],
                "arm": arm,
                "result": result,
                "rollback": dict(rollback) if rollback is not None else None,
            }
    except BaseException as exc:
        # Preserve the canonical lock directory for the existing recovery path,
        # but invoke the injected quarantine hook while this lock is still held.
        lock.retain_on_exit = True
        hooks.quarantine(str(exc))
        raise
    finally:
        lock.release()


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
