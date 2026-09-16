"""The sole guarded production entrypoint for C10 host measurements.

No caller provides a browser callback, a request card, or a result/evidence
object. The host makes the only request graph from the admitted cohort and the
runtime controller remains the only component allowed to change P0/P1 state.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import cre_capacity_runtime as runtime
from cre_checkpoint_refresh import SharedLock, canonical_shared_lock_dir

from . import admission, compare, contracts
from .contracts import C10Error, require_sha256, sha256, validate_plan
from .host_session import (
    C10HostExecutionSession,
    C10SealedCardRegistry,
    C10SessionStore,
)


def _canonical_session_store(
    repo_root: Path, plan: Mapping[str, Any], session: Mapping[str, Any]
) -> C10SessionStore:
    """Derive the sole durable arm ledger; callers cannot select its path."""
    validate_plan(plan)
    root = canonical_shared_lock_dir(repo_root.resolve()).resolve()
    identity = sha256({"plan_sha256": plan["plan_sha256"], "session": session})
    return C10SessionStore(root / "c10-ledgers" / f"{identity}.json")


def _runtime_profile(plan: Mapping[str, Any], variant: str) -> Mapping[str, Any]:
    profile_name = plan["profiles"][variant]["name"]
    profile, digest = runtime.experiment.load_profile(
        admission.PROFILE_CONFIG, profile_name
    )
    if (
        digest != plan["profiles"]["config_sha256"]
        or profile["requested"] != plan["profiles"][variant]["requested"]
    ):
        raise C10Error("C10 runtime profile differs from the immutable plan")
    return profile


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise C10Error("C10 production deadline expired")
    return remaining


def _runtime_fingerprints(
    plan: Mapping[str, Any],
    arm: Mapping[str, Any],
    receipt: Mapping[str, Any],
    transition: Mapping[str, Any] | None,
) -> dict[str, str]:
    profile = plan["profiles"][arm["variant"]]
    baseline = receipt.get("baseline")
    if (
        receipt.get("profile") != profile["name"]
        or receipt.get("config_sha256") != plan["profiles"]["config_sha256"]
        or not isinstance(baseline, Mapping)
    ):
        raise C10Error("C10 runtime receipt does not bind the planned profile")
    snapshot, transition_sha = (
        baseline.get("snapshot_sha256"),
        baseline.get("transition_sha256"),
    )
    if transition is not None:
        if (
            transition.get("profile") != profile["name"]
            or transition.get("state") != "candidate"
            or transition.get("verified") is not True
        ):
            raise C10Error("C10 candidate transition is not verified")
        snapshot, transition_sha = (
            transition.get("container_snapshot_sha256"),
            transition.get("transition_sha256"),
        )
    return {
        "profile_config_sha256": require_sha256(
            plan["profiles"]["config_sha256"], "C10 profile config"
        ),
        "profile_requested_sha256": sha256(profile["requested"]),
        "runtime_receipt_sha256": require_sha256(
            receipt.get("receipt_sha256"), "C10 runtime receipt"
        ),
        "container_snapshot_sha256": require_sha256(snapshot, "C10 runtime snapshot"),
        "transition_sha256": require_sha256(transition_sha, "C10 runtime transition"),
    }


def _settle(
    profile: Mapping[str, Any], state: str, deadline: float
) -> Mapping[str, Any]:
    """Use the canonical runtime capture and full idle checks, never a hook."""
    _remaining(deadline)
    capture = runtime.capture_runtime(deadline=deadline)
    checks = runtime.evaluate_state(capture.public, profile, state)
    if not checks or not all(checks.values()):
        raise C10Error("C10 runtime settlement is unknown or non-idle")
    settlement = capture.public.get("settlement")
    if not isinstance(settlement, Mapping):
        raise C10Error("C10 runtime settlement evidence is unavailable")
    _remaining(deadline)
    return dict(settlement)


def _canonical_lock(repo_root: Path) -> SharedLock:
    lock_path = canonical_shared_lock_dir(repo_root).resolve()
    if lock_path != canonical_shared_lock_dir(runtime.REPO_ROOT).resolve():
        raise C10Error("C10 runtime and host do not share the canonical lock")
    return SharedLock(lock_path)


def execute_production_arm(
    *,
    repo_root: Path,
    plan: Mapping[str, Any],
    cohort: Mapping[str, Any],
    session: Mapping[str, Any],
    private_root: Path,
    runtime_receipt_path: Path,
    approval_path: Path | None = None,
    admission_out: Path | None = None,
    timeout_seconds: float = 120,
) -> Mapping[str, Any]:
    """Execute one C10 arm under its one durable claim and canonical lock.

    The p1 route consumes its approved runtime transition before the host can
    start, settles the authenticated 16-member browser cohort, restores P0,
    and proves post-rollback idleness before terminalizing. Any uncertainty
    retains the shared lock and writes a quarantine record.
    """
    validate_plan(plan)
    registry = C10SealedCardRegistry(plan, cohort)
    store = _canonical_session_store(repo_root, plan, session)
    store.assert_available(plan, session)
    host = C10HostExecutionSession(
        repo_root=repo_root,
        session_store=store,
        private_root=private_root,
        cards=registry,
    )
    deadline = time.monotonic() + timeout_seconds
    lock = _canonical_lock(repo_root.resolve())
    if lock.path.resolve() != host.lock_path:
        raise C10Error("C10 host and runtime canonical locks differ")
    lock.acquire()
    claim: Mapping[str, Any] | None = None
    candidate_transition: Mapping[str, Any] | None = None
    receipt: Mapping[str, Any] | None = None
    try:
        # Claim precedes any host, runtime, Compose, or provider activity.
        _remaining(deadline)
        claim = store.claim(plan, session)
        arm = claim["arm"]
        if not isinstance(arm, Mapping):
            raise C10Error("C10 durable claim arm is invalid")
        variant = arm.get("variant")
        if variant not in {"p0", "p1"}:
            raise C10Error("C10 durable claim variant is invalid")
        profile = _runtime_profile(plan, variant)
        receipt = runtime.preflight(
            plan["profiles"][variant]["name"],
            runtime_receipt_path,
            profile_config=admission.PROFILE_CONFIG,
            experiment_kind="C10",
            deadline=deadline,
        )
        if variant == "p1":
            if approval_path is None or admission_out is None:
                raise C10Error("C10 P1 execution requires approval and admission paths")
            candidate_transition = runtime.transition(
                runtime_receipt_path,
                plan["profiles"][variant]["name"],
                "candidate",
                execute=True,
                approval_path=approval_path,
                admission_out=admission_out,
                _held_shared_lock=lock,
                profile_config=admission.PROFILE_CONFIG,
                experiment_kind="C10",
                deadline=deadline,
            )
        host_result = host.execute(
            plan,
            session,
            timeout_seconds=_remaining(deadline),
            _claim=claim,
            _held_shared_lock=lock,
            _deadline=deadline,
        )
        _settle(profile, "candidate" if variant == "p1" else "baseline", deadline)
        rollback: Mapping[str, Any] | None = None
        if variant == "p1":
            rollback = runtime.transition(
                runtime_receipt_path,
                plan["profiles"][variant]["name"],
                "baseline",
                execute=True,
                _held_shared_lock=lock,
                profile_config=admission.PROFILE_CONFIG,
                experiment_kind="C10",
                deadline=deadline,
            )
            if (
                not isinstance(rollback, Mapping)
                or rollback.get("verified") is not True
            ):
                raise C10Error("C10 P1 rollback is not verified")
            baseline = receipt.get("baseline")
            if (
                not isinstance(baseline, Mapping)
                or rollback.get("container_snapshot_sha256")
                != baseline.get("snapshot_sha256")
                or rollback.get("transition_sha256")
                != baseline.get("transition_sha256")
            ):
                raise C10Error("C10 P1 rollback does not restore the receipt baseline")
            _settle(profile, "baseline", deadline)
        authenticated_arm = {
            "kind": compare.HOST_EVIDENCE_KIND,
            "plan_sha256": plan["plan_sha256"],
            "index": arm["index"],
            "variant": variant,
            "no_write": plan["no_write"],
            "runtime": _runtime_fingerprints(plan, arm, receipt, candidate_transition),
            "host_result": host_result,
        }
        compare.validate_authenticated_host_arm(plan, authenticated_arm)
        _remaining(deadline)
        terminal = store.record_terminal(claim, authenticated_arm)
        return {
            "session": terminal["claimed_session"],
            "claim": claim,
            "authenticated_arm": authenticated_arm,
            "terminal": terminal,
            "rollback": rollback,
            "comparison_state": "not_comparable_pending_authenticated_20_source_evidence",
        }
    except BaseException as exc:
        # A candidate may be live even if the host or settlement failed. Restore
        # through the canonical controller while the same authority lock is held.
        rollback_error: BaseException | None = None
        if candidate_transition is not None and receipt is not None:
            try:
                runtime.transition(
                    runtime_receipt_path,
                    str(receipt["profile"]),
                    "baseline",
                    execute=True,
                    _held_shared_lock=lock,
                    profile_config=admission.PROFILE_CONFIG,
                    experiment_kind="C10",
                    deadline=deadline,
                )
            except BaseException as rollback_exc:  # noqa: BLE001 - quarantine follows
                rollback_error = rollback_exc
        lock.retain_on_exit = True
        store.record_quarantine(
            claim,
            f"{type(exc).__name__}:{exc}"
            + (f"; rollback:{rollback_error}" if rollback_error else ""),
        )
        if rollback_error is not None:
            exc.add_note(f"C10 rollback also failed: {rollback_error}")
        raise
    finally:
        lock.release()


def execute_counterbalanced_sequence(
    *,
    repo_root: Path,
    plan: Mapping[str, Any],
    cohort: Mapping[str, Any],
    session: Mapping[str, Any],
    private_root: Path,
    runtime_receipt_root: Path,
    approval_root: Path,
    admission_root: Path,
    timeout_seconds: float = 120,
) -> Sequence[Mapping[str, Any]]:
    """Run the fixed 8-arm counterbalance with one distinct P1 approval per arm."""
    results: list[Mapping[str, Any]] = []
    current = session
    for index, variant in enumerate(contracts.ARM_SEQUENCE):
        suffix = f"arm-{index}"
        result = execute_production_arm(
            repo_root=repo_root,
            plan=plan,
            cohort=cohort,
            session=current,
            private_root=private_root / suffix,
            runtime_receipt_path=runtime_receipt_root / f"{suffix}.json",
            approval_path=approval_root / f"{suffix}.json" if variant == "p1" else None,
            admission_out=admission_root / f"{suffix}.json"
            if variant == "p1"
            else None,
            timeout_seconds=timeout_seconds,
        )
        current = result["session"]
        results.append(result)
    return results


def _read(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise C10Error(f"C10 {label} JSON cannot be read") from exc
    if not isinstance(value, Mapping):
        raise C10Error(f"C10 {label} JSON must be an object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true", help="permit guarded external work"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke", action="store_true", help="one sealed 16-member arm")
    mode.add_argument(
        "--counterbalanced", action="store_true", help="all eight fixed arms"
    )
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--runtime-receipt", type=Path, required=True)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--admission-out", type=Path)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    args = parser.parse_args(argv)
    plan, cohort, session = (
        _read(args.plan, "plan"),
        _read(args.cohort, "cohort"),
        _read(args.session, "session"),
    )
    validate_plan(plan)
    C10SealedCardRegistry(plan, cohort)
    _canonical_session_store(args.repo_root, plan, session).assert_available(
        plan, session
    )
    # This dry-run deliberately reads only sealed local inputs. It never calls
    # Docker, the runtime controller, the host sidecar, or a provider.
    selected = "counterbalanced" if args.counterbalanced else "smoke"
    if not args.execute:
        print(
            json.dumps(
                {
                    "state": "dry_run",
                    "mode": selected,
                    "external_calls": False,
                    "arm_sequence": plan["arm_sequence"]
                    if args.counterbalanced
                    else None,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.counterbalanced:
        if args.approval is None or args.admission_out is None:
            raise C10Error(
                "C10 counterbalance requires approval and admission directories"
            )
        result: Any = execute_counterbalanced_sequence(
            repo_root=args.repo_root.resolve(),
            plan=plan,
            cohort=cohort,
            session=session,
            private_root=args.private_root.resolve(),
            runtime_receipt_root=args.runtime_receipt.resolve(),
            approval_root=args.approval.resolve(),
            admission_root=args.admission_out.resolve(),
            timeout_seconds=args.timeout_seconds,
        )
    else:
        result = execute_production_arm(
            repo_root=args.repo_root.resolve(),
            plan=plan,
            cohort=cohort,
            session=session,
            private_root=args.private_root.resolve(),
            runtime_receipt_path=args.runtime_receipt.resolve(),
            approval_path=args.approval.resolve() if args.approval else None,
            admission_out=args.admission_out.resolve() if args.admission_out else None,
            timeout_seconds=args.timeout_seconds,
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
