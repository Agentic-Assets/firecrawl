"""The sole guarded production entrypoint for C10 host measurements.

No caller provides a browser callback, a request card, or a result/evidence
object. The host makes the only request graph from the admitted cohort and the
runtime controller remains the only component allowed to change P0/P1 state.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import cre_capacity_runtime as runtime
from cre_checkpoint_refresh import SharedLock, canonical_shared_lock_dir

from . import admission, compare
from .contracts import C10Error, require_sha256, sha256, validate_plan
from .host_session import (
    C10HostExecutionSession,
    C10SealedCardRegistry,
    C10SessionStore,
)


def _canonical_session_store(
    repo_root: Path, plan: Mapping[str, Any]
) -> C10SessionStore:
    """Derive a stable owner-only sibling ledger, never inside the lock tree."""
    validate_plan(plan)
    lock_root = canonical_shared_lock_dir(repo_root.resolve()).resolve()
    root = lock_root.with_name(".cre-c10-ledger-v1")
    return C10SessionStore(root / f"{plan['plan_sha256']}.json")


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


def _claimed_arm_paths(
    arm: Mapping[str, Any],
    *,
    private_root: Path,
    runtime_receipt_root: Path,
    approval_root: Path | None,
    admission_root: Path | None,
) -> tuple[Path, Path, Path | None, Path | None]:
    """Derive every mutable path from the lock-held durable claim, never a caller index."""
    index, variant = arm.get("index"), arm.get("variant")
    if type(index) is not int or variant not in {"p0", "p1"}:
        raise C10Error("C10 durable claim arm is invalid")
    suffix = f"arm-{index}"
    if variant == "p0":
        return (
            private_root / suffix,
            runtime_receipt_root / f"{suffix}.json",
            None,
            None,
        )
    if approval_root is None or admission_root is None:
        raise C10Error("C10 P1 execution requires approval and admission roots")
    return (
        private_root / suffix,
        runtime_receipt_root / f"{suffix}.json",
        approval_root / f"{suffix}.json",
        admission_root / f"{suffix}.json",
    )


def _require_private_root(path: Path, label: str) -> None:
    """Require an existing, real, owner-only directory before a C10 claim."""
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise C10Error(f"C10 {label} root is unavailable") from exc
    owner_uid = os.getuid()
    if owner_uid == 0 or os.geteuid() != owner_uid:
        raise C10Error("C10 roots require a non-root unswitched operating account")
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise C10Error(f"C10 {label} root must be operator-owned mode 0700 directory")


def _require_absent_output(path: Path, label: str) -> None:
    if os.path.lexists(path):
        raise C10Error(f"C10 {label} output path already exists")


def _validate_claim_inputs(
    plan: Mapping[str, Any],
    arm: Mapping[str, Any],
    *,
    private_root: Path,
    runtime_receipt_root: Path,
    approval_root: Path | None,
    admission_root: Path | None,
) -> tuple[Path, Path, Path | None, Path | None]:
    """Validate root and exact arm output inputs before marker or claim."""
    _require_private_root(private_root, "private")
    _require_private_root(runtime_receipt_root, "runtime receipt")
    paths = _claimed_arm_paths(
        arm,
        private_root=private_root,
        runtime_receipt_root=runtime_receipt_root,
        approval_root=approval_root,
        admission_root=admission_root,
    )
    arm_private_root, receipt_path, approval_path, admission_path = paths
    _require_absent_output(receipt_path, "runtime receipt")
    variant = arm["variant"]
    if variant == "p0":
        if approval_root is not None:
            _require_private_root(approval_root, "approval")
            _require_absent_output(
                approval_root / f"arm-{arm['index']}.json", "P0 approval"
            )
        if admission_root is not None:
            _require_private_root(admission_root, "admission")
            _require_absent_output(
                admission_root / f"arm-{arm['index']}.json", "P0 admission"
            )
    else:
        assert approval_root is not None and admission_root is not None
        _require_private_root(approval_root, "approval")
        _require_private_root(admission_root, "admission")
        assert approval_path is not None and admission_path is not None
        try:
            runtime.validate_review_approval(
                approval_path,
                plan["profiles"]["p1"]["name"],
                plan["profiles"]["config_sha256"],
            )
        except runtime.RuntimeAdmissionError as exc:
            raise C10Error("C10 P1 approval is unavailable or invalid") from exc
        _require_absent_output(admission_path, "admission")
    return arm_private_root, receipt_path, approval_path, admission_path


def execute_production_arm(
    *,
    repo_root: Path,
    plan: Mapping[str, Any],
    cohort: Mapping[str, Any],
    private_root: Path,
    runtime_receipt_root: Path,
    approval_root: Path | None = None,
    admission_root: Path | None = None,
    timeout_seconds: float = 120,
) -> Mapping[str, Any]:
    """Execute one C10 arm under its one durable claim and canonical lock.

    The p1 route consumes its approved runtime transition before the host can
    start, settles the authenticated 16-member browser cohort, restores P0,
    and proves post-rollback idleness before terminalizing. Any uncertainty
    retains the shared lock and writes a quarantine record.
    """
    if type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= 120:
        raise C10Error("C10 timeout_seconds must be greater than 0 and at most 120")
    deadline = time.monotonic() + timeout_seconds
    validate_plan(plan)
    registry = C10SealedCardRegistry(plan, cohort)
    store = _canonical_session_store(repo_root, plan)
    lock = _canonical_lock(repo_root.resolve())
    lock.acquire()
    claim: Mapping[str, Any] | None = None
    candidate_transition: Mapping[str, Any] | None = None
    receipt: Mapping[str, Any] | None = None
    try:
        # Resolve the actual next arm while the canonical lock is held, then
        # reject unsafe roots or exact outputs before arming or claiming.
        store.assert_available(plan)
        next_index = store.next_arm_index(plan)
        pending_arm = {
            "index": next_index,
            "variant": plan["arm_sequence"][next_index],
        }
        (
            arm_private_root,
            runtime_receipt_path,
            approval_path,
            admission_out,
        ) = _validate_claim_inputs(
            plan,
            pending_arm,
            private_root=private_root,
            runtime_receipt_root=runtime_receipt_root,
            approval_root=approval_root,
            admission_root=admission_root,
        )
        _remaining(deadline)
    except BaseException:
        lock.release()
        raise
    try:
        # The canonical lock marker is the crash/reclaim boundary. A durable
        # sibling ledger alone is not enough because it survives outside the
        # lock tree while a dead owner could otherwise be stale-reclaimed.
        lock.arm_benchmark(
            {
                "kind": "cre_capacity_c10_v3",
                "plan_sha256": plan["plan_sha256"],
                "protocol_ledger_sha256": sha256(
                    {
                        "protocol": "cre_capacity_c10_v3",
                        "plan_sha256": plan["plan_sha256"],
                    }
                ),
            }
        )
        # Claim precedes any host, runtime, Compose, or provider activity.
        _remaining(deadline)
        claim = store.claim(plan)
        arm = claim["arm"]
        if not isinstance(arm, Mapping):
            raise C10Error("C10 durable claim arm is invalid")
        variant = arm.get("variant")
        if variant not in {"p0", "p1"}:
            raise C10Error("C10 durable claim variant is invalid")
        if (
            arm.get("index") != pending_arm["index"]
            or variant != pending_arm["variant"]
        ):
            raise C10Error("C10 lock-held claim differs from its resolved next arm")
        # A later arm cannot advance based on ledger metadata alone. Reopen
        # every completed predecessor's owner-only receipt root and prove its
        # ordered artifacts using this one lifecycle deadline.
        for prior_index in range(arm["index"]):
            _remaining(deadline)
            store.load_terminal(plan, prior_index, deadline=deadline)
            _remaining(deadline)
        host = C10HostExecutionSession(
            repo_root=repo_root,
            session_store=store,
            private_root=arm_private_root,
            cards=registry,
        )
        if lock.path.resolve() != host.lock_path:
            raise C10Error("C10 host and runtime canonical locks differ")
        profile = _runtime_profile(plan, variant)
        receipt = runtime.preflight(
            plan["profiles"][variant]["name"],
            runtime_receipt_path,
            profile_config=admission.PROFILE_CONFIG,
            experiment_kind="C10",
            deadline=deadline,
        )
        if variant == "p1":
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
        host_result = host._execute_locked_claim(
            plan,
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
        terminal = store.record_terminal(
            plan, claim, authenticated_arm, deadline=deadline
        )
        # The terminal ledger has committed and every required P1 restoration
        # has settled. Only now may ordinary lock reclamation resume.
        lock.disarm_benchmark()
        return {
            "next_arm_index": arm["index"] + 1,
            "claim": claim,
            "authenticated_arm": authenticated_arm,
            "terminal": terminal,
            "rollback": rollback,
            "comparison_state": "not_comparable_pending_authenticated_20_source_evidence",
        }
    except BaseException as exc:
        if claim is None and str(exc) == "all C10 protocol arms are already consumed":
            raise
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
    private_root: Path,
    runtime_receipt_root: Path,
    approval_root: Path,
    admission_root: Path,
    timeout_seconds: float = 120,
) -> Sequence[Mapping[str, Any]]:
    """Run the fixed 8-arm counterbalance with one distinct P1 approval per arm."""
    validate_plan(plan)
    results: list[Mapping[str, Any]] = []
    for _ in range(len(plan["arm_sequence"])):
        try:
            result = execute_production_arm(
                repo_root=repo_root,
                plan=plan,
                cohort=cohort,
                private_root=private_root,
                runtime_receipt_root=runtime_receipt_root,
                approval_root=approval_root,
                admission_root=admission_root,
                timeout_seconds=timeout_seconds,
            )
        except C10Error as exc:
            if str(exc) == "all C10 protocol arms are already consumed":
                break
            raise
        results.append(result)
        if result["next_arm_index"] == len(plan["arm_sequence"]):
            break
    return results


def _read(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise C10Error(f"C10 {label} JSON cannot be read") from exc
    if not isinstance(value, Mapping):
        raise C10Error(f"C10 {label} JSON must be an object")
    return value


def _validate_dry_run_paths(
    *,
    plan: Mapping[str, Any],
    store: C10SessionStore,
    private_root: Path,
    runtime_receipt_root: Path,
    approval_root: Path | None,
    admission_root: Path | None,
    counterbalanced: bool,
) -> int:
    """Validate canonical roots and the exact next durable arm without claiming."""
    for root, label in (
        (private_root, "private"),
        (runtime_receipt_root, "runtime receipt"),
    ):
        if not root.is_absolute() or (root.exists() and not root.is_dir()):
            raise C10Error(f"C10 dry-run requires an absolute {label} root directory")
    if not runtime_receipt_root.is_dir():
        raise C10Error("C10 dry-run requires an existing runtime receipt root")
    next_index = store.next_arm_index(plan)
    suffix = f"arm-{next_index}.json"
    if (runtime_receipt_root / suffix).exists():
        raise C10Error("C10 next-arm runtime receipt path already exists")
    needs_candidate = counterbalanced or plan["arm_sequence"][next_index] == "p1"
    if not needs_candidate:
        return next_index
    if approval_root is None or admission_root is None:
        raise C10Error("C10 candidate execution requires approval and admission roots")
    for root, label in ((approval_root, "approval"), (admission_root, "admission")):
        if not root.is_absolute() or not root.is_dir():
            raise C10Error(f"C10 candidate requires an existing absolute {label} root")
    if counterbalanced:
        for index, variant in enumerate(plan["arm_sequence"]):
            if variant == "p1" and not (approval_root / f"arm-{index}.json").is_file():
                raise C10Error("C10 counterbalance is missing a P1 approval artifact")
    elif not (approval_root / suffix).is_file():
        raise C10Error("C10 next-arm P1 approval artifact is missing")
    if (admission_root / suffix).exists():
        raise C10Error("C10 next-arm admission path already exists")
    return next_index


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    for legacy, canonical in {
        "--runtime-receipt": "--runtime-receipt-root",
        "--approval": "--approval-root",
        "--admission-out": "--admission-root",
    }.items():
        if any(item == legacy or item.startswith(f"{legacy}=") for item in arguments):
            raise C10Error(f"{legacy} is obsolete; use canonical {canonical}")
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
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument(
        "--runtime-receipt-root",
        type=Path,
        required=True,
        help="canonical root; C10 derives arm-N.json after its durable claim",
    )
    parser.add_argument(
        "--approval-root",
        type=Path,
        help="canonical P1 approval root; C10 derives arm-N.json after claim",
    )
    parser.add_argument(
        "--admission-root",
        type=Path,
        help="canonical P1 admission root; C10 derives arm-N.json after claim",
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    args = parser.parse_args(arguments)
    if not 0 < args.timeout_seconds <= 120:
        raise C10Error("C10 lifecycle timeout is outside its reviewed bound")
    plan, cohort = (
        _read(args.plan, "plan"),
        _read(args.cohort, "cohort"),
    )
    validate_plan(plan)
    C10SealedCardRegistry(plan, cohort)
    store = _canonical_session_store(args.repo_root, plan)
    store.assert_available(plan)
    # This dry-run deliberately reads only sealed local inputs. It never calls
    # Docker, the runtime controller, the host sidecar, or a provider.
    selected = "counterbalanced" if args.counterbalanced else "smoke"
    if not args.execute:
        next_index = _validate_dry_run_paths(
            plan=plan,
            store=store,
            private_root=args.private_root.resolve(),
            runtime_receipt_root=args.runtime_receipt_root.resolve(),
            approval_root=args.approval_root.resolve() if args.approval_root else None,
            admission_root=args.admission_root.resolve()
            if args.admission_root
            else None,
            counterbalanced=args.counterbalanced,
        )
        print(
            json.dumps(
                {
                    "state": "dry_run",
                    "mode": selected,
                    "external_calls": False,
                    "arm_sequence": plan["arm_sequence"]
                    if args.counterbalanced
                    else None,
                    "next_arm_index": next_index,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.counterbalanced:
        if args.approval_root is None or args.admission_root is None:
            raise C10Error(
                "C10 counterbalance requires approval and admission directories"
            )
        result: Any = execute_counterbalanced_sequence(
            repo_root=args.repo_root.resolve(),
            plan=plan,
            cohort=cohort,
            private_root=args.private_root.resolve(),
            runtime_receipt_root=args.runtime_receipt_root.resolve(),
            approval_root=args.approval_root.resolve(),
            admission_root=args.admission_root.resolve(),
            timeout_seconds=args.timeout_seconds,
        )
    else:
        result = execute_production_arm(
            repo_root=args.repo_root.resolve(),
            plan=plan,
            cohort=cohort,
            private_root=args.private_root.resolve(),
            runtime_receipt_root=args.runtime_receipt_root.resolve(),
            approval_root=args.approval_root.resolve() if args.approval_root else None,
            admission_root=args.admission_root.resolve()
            if args.admission_root
            else None,
            timeout_seconds=args.timeout_seconds,
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
