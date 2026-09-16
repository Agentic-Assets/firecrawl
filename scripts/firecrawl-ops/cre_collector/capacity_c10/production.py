"""The sole guarded production entrypoint for C10 host measurements.

No caller provides a browser callback, a request card, or a result/evidence
object. The host makes the only request graph from the admitted cohort and the
runtime controller remains the only component allowed to change P0/P1 state.
"""

from __future__ import annotations

import argparse
import base64
import contextvars
import json
import os
import secrets
import stat
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import cre_capacity_runtime as runtime
from cre_checkpoint_refresh import SharedLock, canonical_shared_lock_dir

from . import admission, compare
from .admission_chain import _open_private_root, _write_private_json
from .admission_controller import (
    JLL_ADMISSION_COLLECTION_MAX_SECONDS,
    _JllAdmissionController,
)
from .authority import repository_implementation_sha256
from .contracts import C10Error, require_sha256, sha256, validate_plan
from .host_orchestration import _C10HostTransport, _key_id
from .host_session import (
    C10SealedCardRegistry,
    C10SessionStore,
)
from .host_sidecar import C10EphemeralKeys, DockerComposeSidecar
from .host_store import PrivateReceiptStore, _controller_ledger_authorization
from .jll_admission import JLL_ADMISSION_QUARANTINE_NAME, JLL_SELECTION_RULE

# JLL admission budgets are separate so a slow `up`/listener start cannot eat
# the per-card collection budget.  Explicit upper bound of one admission action:
# startup (180 s) + collection (17 x 30 s + 60 s = 570 s) + teardown (60 s,
# host_sidecar._TEARDOWN_BUDGET_SECONDS) = 810 s, plus at most 5 s to reap a
# killed controller child (admission_controller._CHILD_REAP_SECONDS) = 815 s.
# The image is prebuilt; a run never builds or pulls.
JLL_ADMISSION_STARTUP_MAX_SECONDS = 180.0
_JLL_ADMISSION_KIND = "cre_capacity_c10_jll_admission"

# This context is populated only by the lexical production-controller scope
# after preflight and, for P1, the approved candidate transition. A supplied
# durable claim and canonical lock are therefore insufficient to start a host.
_ACTIVE_PRODUCTION_ACTION: contextvars.ContextVar[object | None] = (
    contextvars.ContextVar("c10_active_production_action", default=None)
)


def _execute_authorized_jll_admission_action(
    *,
    repo_root: Path,
    receipt_root: Path,
    endpoint: str,
    binding: Mapping[str, str],
    adapter_implementation_sha256: str,
    deadline: float,
    authority: object,
    keys: C10EphemeralKeys,
    profile_sha256: str,
) -> Mapping[str, Any]:
    """The only supported route from production authority into JLL collection.

    This is intentionally private until an operator-facing admission command
    can construct the admitted intent and sidecar lifecycle in one scope.  It
    prevents an importable receipt helper from becoming a browser-work bypass.
    """
    if _ACTIVE_PRODUCTION_ACTION.get() is not authority:
        raise C10Error(
            "JLL admission action requires active production-controller authority"
        )
    # The bridge transport deliberately reuses the same one-capability child
    # used by the admitted P0/P1 host.  It has no session store, cards, or
    # executable lifecycle method, so it cannot become an alternate host API.
    bridge_host = object.__new__(_C10HostTransport)
    bridge_host.repo_root = repo_root.resolve()
    controller = _JllAdmissionController(
        repo_root.resolve(),
        receipt_root,
        endpoint,
        bridge_host._run_child,
        profile_sha256,
    )
    return controller._run(
        binding=binding,
        adapter_implementation_sha256=adapter_implementation_sha256,
        timeout_seconds=min(JLL_ADMISSION_COLLECTION_MAX_SECONDS, _remaining(deadline)),
        keys=keys,
    )


def _c10_ledger_root(repo_root: Path) -> Path:
    """The owner-only sibling ledger root, never inside the lock tree."""
    return (
        canonical_shared_lock_dir(repo_root.resolve())
        .resolve()
        .with_name(".cre-c10-ledger-v1")
    )


def _quarantine_jll_admission(
    *,
    repo_root: Path,
    lock: SharedLock,
    run_id: str,
    receipt_root: Path,
    compose_project: str | None,
    cleanup_error: BaseException,
) -> None:
    """Fail closed when sidecar teardown cannot be proven.

    The canonical lock keeps its armed benchmark marker (non-reclaimable until
    operator recovery), a durable record names the exact compose project, and
    the receipt root is marked so the offline builder refuses it.
    """
    lock.retain_on_exit = True
    reason = str(cleanup_error)
    record = {
        "schema_version": 1,
        "kind": f"{_JLL_ADMISSION_KIND}_quarantine",
        "state": "quarantined",
        "run_id": run_id,
        "selection_rule": JLL_SELECTION_RULE,
        "receipt_root": str(receipt_root.resolve()),
        "compose_project": compose_project,
        "compose_service": "playwright-service-c10",
        "reason_sha256": sha256({"reason": reason}),
        "recovery": {
            "automatic_reclaim": "disabled_benchmark_marker_retained",
            "required_evidence": "docker ps --all --filter label=com.docker.compose.project=<compose_project> is empty",
            "required_action": "operator removes the exact project containers, discards this receipt root, then follows LOCK_AUTHORITY_RECOVERY.md",
        },
    }
    try:
        ledger = C10SessionStore(
            _c10_ledger_root(repo_root) / f"jll-admission-{run_id}.json"
        )
        try:
            ledger._write_new_record(
                f"jll-admission-{run_id}.quarantine",
                record,
                error="C10 JLL admission quarantine evidence could not be persisted",
            )
        finally:
            ledger.close()
    except BaseException as record_error:  # noqa: BLE001 - lock stays retained
        cleanup_error.add_note(
            f"JLL admission ledger quarantine failed: {record_error}"
        )
    try:
        with _open_private_root(receipt_root, "receipt root") as root:
            _write_private_json(root, JLL_ADMISSION_QUARANTINE_NAME, record)
    except BaseException as record_error:  # noqa: BLE001 - lock stays retained
        cleanup_error.add_note(
            f"JLL admission receipt-root quarantine failed: {record_error}"
        )


def _require_timeout(value: object, maximum: float, label: str) -> float:
    if type(value) not in {int, float} or not 0 < float(value) <= maximum:  # type: ignore[arg-type]
        raise C10Error(f"JLL admission {label} timeout is outside its reviewed bound")
    return float(value)  # type: ignore[arg-type]


def execute_jll_admission_collection(
    *,
    repo_root: Path,
    receipt_root: Path,
    binding: Mapping[str, str],
    adapter_implementation_sha256: str,
    startup_timeout_seconds: float = JLL_ADMISSION_STARTUP_MAX_SECONDS,
    collection_timeout_seconds: float = JLL_ADMISSION_COLLECTION_MAX_SECONDS,
) -> Mapping[str, Any]:
    """Run the bounded JLL admission bridge without entering P0/P1 calibration.

    This is the sole supported provider-facing admission action.  It holds the
    canonical CRE lock (so it never overlaps a P0/P1 arm or collector run),
    starts a fresh loopback-only C10 sidecar from the prebuilt image at a
    deliberately non-calibration capacity of one, then delegates only to the
    private active-controller action.  Startup, collection, and teardown have
    separate bounds.  It does not create a session claim, alter authority, or
    touch collector state.  Members are never a caller input: the source
    selector chooses them from the signed enumeration and the controller
    independently recomputes them.  The receipt root must be a fresh, empty,
    provisioned owner-0700 leaf.  If sidecar teardown cannot be proven the
    lock is retained and the run is quarantined for operator recovery.
    """
    startup_seconds = _require_timeout(
        startup_timeout_seconds, JLL_ADMISSION_STARTUP_MAX_SECONDS, "startup"
    )
    collection_seconds = _require_timeout(
        collection_timeout_seconds, JLL_ADMISSION_COLLECTION_MAX_SECONDS, "collection"
    )
    # Refuse invalid intent before a lock, sidecar, provider attempt, or
    # private file is created.  The child repeats the checks at the trust
    # boundary and the offline builder rechecks the adapter digest.
    require_sha256(adapter_implementation_sha256, "JLL adapter implementation")
    expected_binding = {
        "planSha256",
        "cohortSha256",
        "policySha256",
        "sourceSha256",
        "armSha256",
        "implementationSha256",
    }
    if set(binding) != expected_binding:
        raise C10Error("JLL admission binding is incomplete")
    for label, digest in binding.items():
        require_sha256(digest, f"JLL admission {label}")
    if adapter_implementation_sha256 != repository_implementation_sha256("jll"):
        raise C10Error(
            "JLL admission adapter digest does not match the repository implementation"
        )
    lock = _canonical_lock(repo_root.resolve())
    lock.acquire()
    try:
        run_id = secrets.token_hex(16)
        try:
            # The armed marker is the crash boundary: a dead owner cannot be
            # stale-reclaimed while a sidecar may still exist.
            lock.arm_benchmark(
                {
                    "kind": _JLL_ADMISSION_KIND,
                    "selection_rule": JLL_SELECTION_RULE,
                    "run_id": run_id,
                    "receipt_root": str(receipt_root.resolve()),
                }
            )
        except BaseException:
            lock.retain_on_exit = True
            raise
        sidecar = DockerComposeSidecar(repo_root)
        attempted = False
        result: Mapping[str, Any] | None = None
        failure: BaseException | None = None
        try:
            startup_deadline = time.monotonic() + startup_seconds
            bridge_host = object.__new__(_C10HostTransport)
            bridge_host.repo_root = repo_root.resolve()
            keys = bridge_host._keys(startup_deadline)
            requested = {"global_pages": 1, "browser_cpus": 2, "browser_pids": 384}
            port = bridge_host._free_loopback_port()
            endpoint = f"http://127.0.0.1:{port}"
            # Cleanup responsibility begins before Compose is invoked.
            attempted = True
            sidecar.start(
                {
                    "C10_COORDINATOR_PUBLIC_KEY_PEM_B64": base64.b64encode(
                        keys.coordinator_public_pem.encode("utf-8")
                    ).decode("ascii"),
                    "C10_SIDECAR_EVIDENCE_PRIVATE_KEY_PEM_B64": base64.b64encode(
                        keys.sidecar_private_pem.encode("utf-8")
                    ).decode("ascii"),
                    "PLAYWRIGHT_HOST_TRANSPORT_V3_KEY": keys.transport_key,
                    "MAX_CONCURRENT_PAGES": "1",
                    "C10_PROFILE_SHA256": sha256(requested),
                    "C10_BROWSER_CPUS": "2",
                    "C10_BROWSER_PIDS": "384",
                    "C10_ADMISSION_LANE": JLL_SELECTION_RULE,
                },
                port,
                startup_deadline,
            )
            bridge_host._verify_health(
                endpoint,
                keys,
                {"requested": requested},
                startup_deadline,
                admission_lane=JLL_SELECTION_RULE,
            )
            collection_deadline = time.monotonic() + collection_seconds
            authority = object()
            action_context = _ACTIVE_PRODUCTION_ACTION.set(authority)
            try:
                result = _execute_authorized_jll_admission_action(
                    repo_root=repo_root,
                    receipt_root=receipt_root,
                    endpoint=endpoint,
                    binding=binding,
                    adapter_implementation_sha256=adapter_implementation_sha256,
                    deadline=collection_deadline,
                    authority=authority,
                    keys=keys,
                    profile_sha256=sha256(requested),
                )
            finally:
                _ACTIVE_PRODUCTION_ACTION.reset(action_context)
        except BaseException as exc:  # noqa: BLE001 - re-raised after teardown
            failure = exc
        if attempted:
            try:
                # Teardown uses its own budget even when the run deadline expired.
                sidecar.stop()
            except BaseException as cleanup_error:
                _quarantine_jll_admission(
                    repo_root=repo_root,
                    lock=lock,
                    run_id=run_id,
                    receipt_root=receipt_root,
                    compose_project=sidecar.compose_project,
                    cleanup_error=cleanup_error,
                )
                if failure is not None:
                    failure.add_note(
                        f"C10 JLL admission sidecar teardown also failed: {cleanup_error}"
                    )
                    # The cleanup error is not chained onto the propagated
                    # failure, so carry its quarantine-persistence notes too.
                    for note in getattr(cleanup_error, "__notes__", ()):
                        failure.add_note(note)
                    raise failure
                unproven = C10Error(
                    "C10 JLL admission sidecar teardown was not proven; the "
                    "canonical lock is retained for operator recovery"
                )
                for note in getattr(cleanup_error, "__notes__", ()):
                    unproven.add_note(note)
                raise unproven from cleanup_error
        # Teardown is proven (or never began): ordinary reclamation may resume.
        lock.disarm_benchmark()
        if failure is not None:
            raise failure
        if result is None:
            raise C10Error("JLL admission produced no controller result")
        return result
    finally:
        lock.release()


def _canonical_session_store(
    repo_root: Path, plan: Mapping[str, Any]
) -> C10SessionStore:
    """Derive a stable owner-only sibling ledger, never inside the lock tree."""
    validate_plan(plan)
    return C10SessionStore(_c10_ledger_root(repo_root) / f"{plan['plan_sha256']}.json")


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


def _execute_authorized_host_action(
    host: _C10HostTransport,
    plan: Mapping[str, Any],
    *,
    claim: Mapping[str, Any],
    lock: SharedLock,
    deadline: float,
    authority: object,
) -> Mapping[str, Any]:
    """Run browser transport only inside the production-controller lifecycle.

    This is deliberately the sole code path that starts the C10 sidecar.  The
    caller has already completed the canonical lock, durable claim, runtime
    preflight, and (for P1) reviewed transition.  The transport object has no
    full-lifecycle execution method and is never exported from host_session.
    """
    if _ACTIVE_PRODUCTION_ACTION.get() is not authority:
        raise C10Error(
            "C10 host action requires active production-controller authority"
        )
    validate_plan(plan)
    host.cards.assert_plan_identity(plan)
    _remaining(deadline)
    if lock.path.resolve() != canonical_shared_lock_dir(host.repo_root).resolve():
        raise C10Error("C10 host rejected a noncanonical SharedLock identity")
    try:
        descriptor = lock._owned_directory_fd()
    except Exception as exc:
        raise C10Error("C10 host requires the owned canonical SharedLock") from exc
    os.close(descriptor)
    durable = host.session_store.read_bound(plan, claim)
    if dict(durable) != dict(claim):
        raise C10Error("C10 host rejects an unbound durable claim")

    sidecar_attempted = False
    result: Mapping[str, Any] | None = None
    try:
        with PrivateReceiptStore.create(host.private_root) as private_store:
            keys = host._keys(deadline)
            port = host._free_loopback_port()
            arm = durable["arm"]
            if not isinstance(arm, Mapping):
                raise C10Error("C10 durable claim arm is invalid")
            profile = plan["profiles"].get(arm.get("variant"))
            if not isinstance(profile, Mapping):
                raise C10Error("C10 profile is invalid for durable claim")
            requested = profile.get("requested")
            if not isinstance(requested, Mapping):
                raise C10Error("C10 profile request is invalid")
            # Cleanup responsibility begins before Compose is invoked: an up
            # timeout can leave a container even when start raises.
            sidecar_attempted = True
            host.sidecar.start(
                {
                    "C10_COORDINATOR_PUBLIC_KEY_PEM_B64": base64.b64encode(
                        keys.coordinator_public_pem.encode("utf-8")
                    ).decode("ascii"),
                    "C10_SIDECAR_EVIDENCE_PRIVATE_KEY_PEM_B64": base64.b64encode(
                        keys.sidecar_private_pem.encode("utf-8")
                    ).decode("ascii"),
                    "PLAYWRIGHT_HOST_TRANSPORT_V3_KEY": keys.transport_key,
                    "MAX_CONCURRENT_PAGES": str(requested["global_pages"]),
                    "C10_PROFILE_SHA256": sha256(requested),
                    "C10_BROWSER_CPUS": str(requested["browser_cpus"]),
                    "C10_BROWSER_PIDS": str(requested["browser_pids"]),
                },
                port,
                deadline,
            )
            endpoint = f"http://127.0.0.1:{port}"
            host._verify_health(endpoint, keys, profile, deadline)
            evidence, artifacts = host._run_cohort(
                endpoint, durable, plan, profile, keys, private_store, deadline
            )
            result = {
                "claim": durable,
                "receipt_root": private_store.descriptor(),
                "evidence_manifest": artifacts,
                "evidence_manifest_sha256": sha256(evidence),
                "evidence_public_key": keys.sidecar_public_pem,
                "evidence_key_id": _key_id(keys.sidecar_public_pem),
                "binding": evidence[0]["binding"],
            }
    except BaseException as exc:
        lock.retain_on_exit = True
        host.session_store._controller_record_quarantine(durable, str(exc))
        host.quarantine(str(exc))
        raise
    finally:
        try:
            if sidecar_attempted:
                host.sidecar.stop(deadline)
        except BaseException as cleanup_error:
            lock.retain_on_exit = True
            host.session_store._controller_record_quarantine(
                durable, str(cleanup_error)
            )
            host.quarantine(f"C10 sidecar cleanup failed: {cleanup_error}")
            raise
    if result is None:
        raise C10Error("C10 host did not produce a terminal cohort result")
    return result


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
        with _controller_ledger_authorization(
            "claim", plan, pending_arm, attempt=None, deadline=deadline
        ):
            claim = store._controller_claim(plan, deadline=deadline)
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
        host = _C10HostTransport(
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
        action_authority = object()
        action_context = _ACTIVE_PRODUCTION_ACTION.set(action_authority)
        try:
            host_result = _execute_authorized_host_action(
                host,
                plan,
                claim=claim,
                lock=lock,
                deadline=deadline,
                authority=action_authority,
            )
        finally:
            _ACTIVE_PRODUCTION_ACTION.reset(action_context)
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
        with _controller_ledger_authorization(
            "terminal", plan, arm, attempt=claim["claim_id"], deadline=deadline
        ):
            terminal = store._controller_record_terminal(
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
        store._controller_record_quarantine(
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
