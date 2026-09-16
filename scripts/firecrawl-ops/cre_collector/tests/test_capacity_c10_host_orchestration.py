"""C10 host-session orchestration contracts.  No external lifecycle runs."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import socket
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Self

import pytest
from capacity_c10_test_support import controller_claim, sealed_jll_plan

from capacity_c10 import contracts, production
from capacity_c10.host_orchestration import _C10HostTransport
from capacity_c10.host_session import (
    C10EphemeralKeys,
    C10SealedCardRegistry,
    C10SessionStore,
    DockerComposeSidecar,
    _OpenSsl,
)

_JLL_ADMISSION_LANE = "jll-canonical-url-lexicographic-v1"


def _signed_evidence(
    card: Mapping[str, object],
    body_payload: object,
    sidecar_private: str,
    deadline: float,
) -> tuple[dict[str, object], dict[str, object]]:
    """Build a validly-signed evidence/issued pair for one issued card.

    Mirrors the shape asserted by
    ``test_python_rejects_transport_success_without_sealed_enumeration_membership``.
    """
    binding = {"fixture": "binding"}
    issued = {"card": card, "capability": {"binding": binding}}
    unsigned = {
        "protocolVersion": 3,
        "binding": binding,
        "status": 200,
        "finalUrl": card["url"],
        "redirectCount": 0,
        "elapsedMs": 1,
        "challengeDetected": False,
        "contentType": "application/json",
        "bodyBase64": base64.b64encode(json.dumps(body_payload).encode("utf-8")).decode(
            "ascii"
        ),
        "jobId": "fixture",
        "pageLease": {"leaseId": "fixture", "slot": 0},
        "leaseStartMonotonicNs": "1",
        "leaseEndMonotonicNs": "2",
        "observedActivePages": 1,
        "configuredCapacity": 4,
        "queueMs": 0,
        "proxy": {"mode": "direct", "proxyId": None, "country": None},
        "engineAttempt": {
            "engine": "fixture",
            "ordinal": 1,
            "fallbackDisabled": True,
            "fallbackUsed": False,
        },
        "context": {"ephemeral": True, "storageState": "none", "cache": "disabled"},
        "cacheRead": False,
        "cacheWrite": False,
    }
    evidence = {
        **unsigned,
        "evidenceSignature": _OpenSsl.sign(
            sidecar_private, contracts.canonical_bytes(unsigned), deadline
        ),
    }
    return evidence, issued


@pytest.mark.parametrize(
    "payload",
    [
        {"errors": [{"message": "blocked"}]},
        {"data": {"properties": {"items": []}}},
        {
            "data": {
                "properties": {
                    "items": [{"pageUrl": "https://property.jll.com/not-sealed"}]
                }
            }
        },
        {"data": {"properties": {"items": "malformed"}}},
    ],
)
def test_python_rejects_transport_success_without_sealed_enumeration_membership(
    payload: object,
) -> None:
    plan, cohort = sealed_jll_plan()
    registry = C10SealedCardRegistry(plan, cohort)
    sidecar_private, sidecar_public = _OpenSsl.pair(time.monotonic() + 30)
    keys = C10EphemeralKeys("unused", "unused", sidecar_private, sidecar_public, "key")
    card = registry.resolve("jll-enumeration")
    issued = {"card": card, "capability": {"binding": {"fixture": "binding"}}}
    unsigned = {
        "protocolVersion": 3,
        "binding": issued["capability"]["binding"],
        "status": 200,
        "finalUrl": card["url"],
        "redirectCount": 0,
        "elapsedMs": 1,
        "challengeDetected": False,
        "contentType": "application/json",
        "bodyBase64": base64.b64encode(json.dumps(payload).encode("utf-8")).decode(
            "ascii"
        ),
        "jobId": "fixture",
        "pageLease": {"leaseId": "fixture", "slot": 0},
        "leaseStartMonotonicNs": "1",
        "leaseEndMonotonicNs": "2",
        "observedActivePages": 1,
        "configuredCapacity": 4,
        "queueMs": 0,
        "proxy": {"mode": "direct", "proxyId": None, "country": None},
        "engineAttempt": {
            "engine": "fixture",
            "ordinal": 1,
            "fallbackDisabled": True,
            "fallbackUsed": False,
        },
        "context": {"ephemeral": True, "storageState": "none", "cache": "disabled"},
        "cacheRead": False,
        "cacheWrite": False,
    }
    evidence = {
        **unsigned,
        "evidenceSignature": _OpenSsl.sign(
            sidecar_private, contracts.canonical_bytes(unsigned), time.monotonic() + 30
        ),
    }
    with pytest.raises(contracts.C10Error, match="enumeration evidence"):
        _C10HostTransport._verify_evidence(
            object.__new__(_C10HostTransport),
            evidence,
            issued,
            keys,
            time.monotonic() + 30,
        )


def test_registry_rejects_same_cohort_plan_b_before_any_lifecycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An alternate plan/source projection is rejected by the registry itself.

    ``production._execute_authorized_host_action`` calls
    ``host.cards.assert_plan_identity(plan)`` immediately after ``validate_plan``
    and before any lock-ownership proof, durable-claim lookup, or sidecar
    start. Plan B carries an altered source family (and a plan_sha256
    recomputed to match), so it is a genuinely different plan/source
    projection from the one the registry was sealed with. ``validate_plan``'s
    separate repository-authority pinning is orthogonal to the registry gate
    under test here, so it is bypassed for this plan-identity-only case.
    """
    plan_a, cohort = sealed_jll_plan()
    registry = C10SealedCardRegistry(plan_a, cohort)
    plan_b = copy.deepcopy(plan_a)
    plan_b["sources"][0]["family"] = "altered-plan-b"  # type: ignore[index]
    plan_b["plan_sha256"] = contracts.sha256(
        {key: value for key, value in plan_b.items() if key != "plan_sha256"}
    )

    class Sidecar:
        started = False

        def start(self, *_: object) -> None:
            self.started = True

        def stop(self, *_: object) -> None:
            raise AssertionError("Plan B must not reach lifecycle cleanup")

    class LockSpy:
        def __init__(self) -> None:
            self.touched = False

        @property
        def path(self) -> Path:
            self.touched = True
            raise AssertionError("Plan B must not reach lock ownership proof")

    class SessionStoreSpy:
        def read_bound(self, *_: object, **__: object) -> None:
            raise AssertionError("Plan B must not reach durable claim lookup")

    monkeypatch.setattr(
        "capacity_c10.host_orchestration.canonical_shared_lock_dir",
        lambda _root: tmp_path / ".cre.lock",
    )
    monkeypatch.setattr("capacity_c10.production.validate_plan", lambda _plan: None)
    sidecar = Sidecar()
    host = _C10HostTransport(
        repo_root=tmp_path,
        session_store=SessionStoreSpy(),  # type: ignore[arg-type]
        private_root=tmp_path / "private",
        cards=registry,
        sidecar=sidecar,
    )
    assert not hasattr(host, "execute")
    assert not hasattr(host, "_retired_direct_execution")
    authority = object()
    token = production._ACTIVE_PRODUCTION_ACTION.set(authority)
    try:
        with pytest.raises(
            contracts.C10Error,
            match="C10 registry rejects an alternate plan/source projection",
        ):
            production._execute_authorized_host_action(
                host,
                plan_b,
                claim={},
                lock=LockSpy(),  # type: ignore[arg-type]
                deadline=time.monotonic() + 1,
                authority=authority,
            )
    finally:
        production._ACTIVE_PRODUCTION_ACTION.reset(token)
    assert sidecar.started is False


def test_host_workflow_issues_signed_17_card_cohort_and_removes_sidecar_before_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = 4
    plan, cohort = sealed_jll_plan()
    registry = C10SealedCardRegistry(plan, cohort)
    lifecycle_events: list[str] = []
    issued: list[dict[str, object]] = []

    class FakeLock:
        def __init__(self, path: Path) -> None:
            self.path, self.retain_on_exit = path, False

        def acquire(self) -> None:
            lifecycle_events.append("lock")

        def release(self) -> None:
            lifecycle_events.append("release")

        def _owned_directory_fd(self) -> int:
            return os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)

    class FakeStore:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def seal_json(self, stem: str, value: object) -> dict[str, object]:
            return {
                "name": f"{stem}.sealed",
                "sha256": contracts.sha256(value),
                "bytes": 1,
            }

        def descriptor(self) -> dict[str, str]:
            return {"path": str(tmp_path / "private"), "id": "f" * 64}

    class FakeSidecar:
        private_key = ""
        sidecar_public = ""
        coordinator_public = ""
        capacity = 0
        profile_sha256 = ""
        stopped = False

        def start(
            self, environment: dict[str, str], _port: int, _deadline: float
        ) -> None:
            self.private_key = base64.b64decode(
                environment["C10_SIDECAR_EVIDENCE_PRIVATE_KEY_PEM_B64"]
            ).decode("utf-8")
            self.coordinator_public = base64.b64decode(
                environment["C10_COORDINATOR_PUBLIC_KEY_PEM_B64"]
            ).decode("utf-8")
            self.capacity = int(environment["MAX_CONCURRENT_PAGES"])
            self.profile_sha256 = environment["C10_PROFILE_SHA256"]
            lifecycle_events.append("start")

        def stop(self, _deadline: float) -> None:
            self.stopped = True
            lifecycle_events.append("removed")

    monkeypatch.setattr(
        "capacity_c10.host_orchestration.canonical_shared_lock_dir",
        lambda _root: tmp_path / ".cre.lock",
    )
    monkeypatch.setattr(
        "capacity_c10.production.canonical_shared_lock_dir",
        lambda _root: tmp_path / ".cre.lock",
    )
    monkeypatch.setattr(
        "capacity_c10.production.PrivateReceiptStore.create",
        lambda _root: FakeStore(),
    )
    sidecar = FakeSidecar()
    host = _C10HostTransport(
        repo_root=tmp_path,
        session_store=C10SessionStore(tmp_path / "session.json"),
        private_root=tmp_path / "private",
        cards=registry,
        sidecar=sidecar,
    )
    original_keys = host._keys

    def generated_keys(deadline: float):
        keys = original_keys(deadline)
        sidecar.sidecar_public = keys.sidecar_public_pem
        return keys

    class FakeHealthResponse:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, _: int) -> bytes:
            unsigned = {
                "protocolVersion": 3,
                "status": "healthy",
                "transport": "docker-loopback-tcp",
                "coordinatorKeyId": hashlib.sha256(
                    sidecar.coordinator_public.encode("utf-8")
                ).hexdigest(),
                "evidenceKeyId": hashlib.sha256(
                    sidecar.sidecar_public.encode("utf-8")
                ).hexdigest(),
                "activePages": 0,
                "configuredCapacity": sidecar.capacity,
                "profileSha256": sidecar.profile_sha256,
                "admissionLane": None,
                "replayEntries": 0,
            }
            return json.dumps(
                {
                    **unsigned,
                    "healthSignature": _OpenSsl.sign(
                        sidecar.private_key,
                        contracts.canonical_bytes(unsigned),
                        time.monotonic() + 60,
                    ),
                }
            ).encode("utf-8")

    monkeypatch.setattr(host, "_keys", generated_keys)
    monkeypatch.setattr(
        "capacity_c10.host_orchestration.urlopen",
        lambda *_args, **_kwargs: FakeHealthResponse(),
    )

    def signed_child(card: dict[str, object], deadline: float) -> dict[str, object]:
        issued.append(card)
        capability = card["capability"]  # type: ignore[assignment]
        request = card["card"]  # type: ignore[assignment]
        sequence = capability["cardSequence"]  # type: ignore[index]
        if sequence == 0:
            start, end, active = 1, 2, 1
        elif sequence <= target:
            start, end, active = 10, 20, target
        else:
            start, end, active = 20 + sequence * 10, 29 + sequence * 10, 1
        body = (
            json.dumps(
                {
                    "data": {
                        "properties": {
                            "items": [
                                {"pageUrl": route}
                                for route in request["expectedMemberRoutes"]  # type: ignore[index]
                            ]
                        }
                    }
                }
            ).encode("utf-8")
            if sequence == 0
            else b"{}"
        )
        unsigned = {
            "protocolVersion": 3,
            "binding": capability["binding"],
            "status": 200,
            "finalUrl": request["url"],
            "redirectCount": 0,
            "elapsedMs": 1,
            "challengeDetected": False,
            "contentType": "application/json",
            "bodyBase64": base64.b64encode(body).decode("ascii"),
            "jobId": f"job-{sequence}",
            "pageLease": {"leaseId": f"lease-{sequence}", "slot": 0},
            "leaseStartMonotonicNs": str(start),
            "leaseEndMonotonicNs": str(end),
            "observedActivePages": active,
            "configuredCapacity": target,
            "queueMs": 0,
            "proxy": {"mode": "direct", "proxyId": None, "country": None},
            "engineAttempt": {
                "engine": "fake",
                "ordinal": 1,
                "fallbackDisabled": True,
                "fallbackUsed": False,
            },
            "context": {"ephemeral": True, "storageState": "none", "cache": "disabled"},
            "cacheRead": False,
            "cacheWrite": False,
        }
        return {
            **unsigned,
            "evidenceSignature": _OpenSsl.sign(
                sidecar.private_key, contracts.canonical_bytes(unsigned), deadline
            ),
        }

    monkeypatch.setattr(host, "_run_child", signed_child)
    claim = controller_claim(host.session_store, plan)
    lock_path = tmp_path / ".cre.lock"
    lock_path.mkdir(mode=0o700)
    held_lock = FakeLock(lock_path)
    held_lock.acquire()
    authority = object()
    context = production._ACTIVE_PRODUCTION_ACTION.set(authority)
    try:
        result = production._execute_authorized_host_action(
            host,
            plan,
            claim=claim,
            lock=held_lock,
            deadline=time.monotonic() + 120,
            authority=authority,
        )
    finally:
        production._ACTIVE_PRODUCTION_ACTION.reset(context)
    held_lock.release()
    assert len(issued) == 17
    assert issued[0]["card"]["id"] == "jll-enumeration"  # type: ignore[index]
    assert {item["capability"]["cardSequence"] for item in issued} == set(range(17))  # type: ignore[index]
    assert all(
        item["capability"]["expiresAtMs"] <= item["capability"]["hostDeadlineAtMs"]  # type: ignore[index]
        for item in issued
    )
    assert len(result["evidence_manifest"]) == 17
    assert sidecar.stopped and lifecycle_events.index(
        "removed"
    ) < lifecycle_events.index("release")


def test_host_cleanup_failure_quarantines_and_never_returns_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan, cohort = sealed_jll_plan()
    registry = C10SealedCardRegistry(plan, cohort)

    class FailingSidecar:
        def start(self, *_: object) -> None:
            return None

        def stop(self, *_: object) -> None:
            raise contracts.C10Error("remove failed")

    class FakeStore:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(
        "capacity_c10.production.canonical_shared_lock_dir",
        lambda _root: tmp_path / ".cre.lock",
    )
    monkeypatch.setattr(
        "capacity_c10.host_orchestration.canonical_shared_lock_dir",
        lambda _root: tmp_path / ".cre.lock",
    )

    class Lock:
        def __init__(self, path: Path) -> None:
            self.path, self.retain_on_exit = path, False

        def acquire(self) -> None:
            return None

        def release(self) -> None:
            return None

        def _owned_directory_fd(self) -> int:
            return os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)

    monkeypatch.setattr(
        "capacity_c10.production.PrivateReceiptStore.create",
        lambda _root: FakeStore(),
    )
    store = C10SessionStore(tmp_path / "session.json")
    host = _C10HostTransport(
        repo_root=tmp_path,
        session_store=store,
        private_root=tmp_path / "private",
        cards=registry,
        sidecar=FailingSidecar(),
    )
    monkeypatch.setattr(host, "_verify_health", lambda *_: None)
    monkeypatch.setattr(host, "_run_cohort", lambda *_: ([{"binding": {}}], []))
    claim = controller_claim(store, plan)
    lock_path = tmp_path / ".cre.lock"
    lock_path.mkdir(mode=0o700)
    held_lock = Lock(lock_path)
    authority = object()
    context = production._ACTIVE_PRODUCTION_ACTION.set(authority)
    try:
        with pytest.raises(contracts.C10Error, match="remove failed"):
            production._execute_authorized_host_action(
                host,
                plan,
                claim=claim,
                lock=held_lock,
                deadline=time.monotonic() + 120,
                authority=authority,
            )
    finally:
        production._ACTIVE_PRODUCTION_ACTION.reset(context)
    assert (tmp_path / "session.arm-0.quarantine").exists()


def test_verify_evidence_strict_rejects_enumeration_card_without_sealed_membership() -> (
    None
):
    """A strict (non-admission) enumeration card must carry its sixteen sealed
    routes; a card claiming none is rejected before its body is even read,
    even though the evidence signature itself is valid."""
    plan, cohort = sealed_jll_plan()
    registry = C10SealedCardRegistry(plan, cohort)
    sidecar_private, sidecar_public = _OpenSsl.pair(time.monotonic() + 30)
    keys = C10EphemeralKeys("unused", "unused", sidecar_private, sidecar_public, "key")
    card = dict(registry.resolve("jll-enumeration"))
    card["expectedMemberRoutes"] = None
    deadline = time.monotonic() + 30
    evidence, issued = _signed_evidence(
        card, {"data": {"properties": {"items": []}}}, sidecar_private, deadline
    )
    with pytest.raises(contracts.C10Error, match="lacks sealed membership"):
        _C10HostTransport._verify_evidence(
            object.__new__(_C10HostTransport), evidence, issued, keys, deadline
        )


def test_verify_evidence_strict_rejects_a_body_missing_one_sealed_route() -> None:
    """Strict mode enforces the full subset, not just non-empty membership: a
    body observing every sealed route but one is still rejected."""
    plan, cohort = sealed_jll_plan()
    registry = C10SealedCardRegistry(plan, cohort)
    sidecar_private, sidecar_public = _OpenSsl.pair(time.monotonic() + 30)
    keys = C10EphemeralKeys("unused", "unused", sidecar_private, sidecar_public, "key")
    card = registry.resolve("jll-enumeration")
    routes = card["expectedMemberRoutes"]
    partial_items = [{"pageUrl": route} for route in routes[:-1]]
    deadline = time.monotonic() + 30
    evidence, issued = _signed_evidence(
        card,
        {"data": {"properties": {"items": partial_items}}},
        sidecar_private,
        deadline,
    )
    with pytest.raises(contracts.C10Error, match="does not bind sealed cohort"):
        _C10HostTransport._verify_evidence(
            object.__new__(_C10HostTransport), evidence, issued, keys, deadline
        )


def test_verify_evidence_admission_mode_accepts_null_routes_and_rejects_sealed_list() -> (
    None
):
    """The JLL admission controller's enumeration card must have no sealed
    membership yet (it recomputes membership from this very body); a card
    that still carries the sealed sixteen-route list is outside its lane."""
    plan, cohort = sealed_jll_plan()
    registry = C10SealedCardRegistry(plan, cohort)
    sidecar_private, sidecar_public = _OpenSsl.pair(time.monotonic() + 30)
    keys = C10EphemeralKeys("unused", "unused", sidecar_private, sidecar_public, "key")
    deadline = time.monotonic() + 30

    admission_card = dict(registry.resolve("jll-enumeration"))
    admission_card["expectedMemberRoutes"] = None
    evidence, issued = _signed_evidence(
        admission_card,
        {"data": {"properties": {"items": []}}},
        sidecar_private,
        deadline,
    )
    _C10HostTransport._verify_evidence(
        object.__new__(_C10HostTransport),
        evidence,
        issued,
        keys,
        deadline,
        admission_enumeration=True,
    )

    sealed_card = registry.resolve("jll-enumeration")
    evidence, issued = _signed_evidence(
        sealed_card, {"data": {"properties": {"items": []}}}, sidecar_private, deadline
    )
    with pytest.raises(
        contracts.C10Error, match="admission enumeration card is invalid"
    ):
        _C10HostTransport._verify_evidence(
            object.__new__(_C10HostTransport),
            evidence,
            issued,
            keys,
            deadline,
            admission_enumeration=True,
        )


def _signed_health(
    *,
    coordinator_public: str,
    sidecar_private: str,
    sidecar_public: str,
    configured_capacity: int,
    profile_sha256: str,
    admission_lane: str | None,
    deadline: float,
) -> dict[str, object]:
    unsigned = {
        "protocolVersion": 3,
        "status": "healthy",
        "transport": "docker-loopback-tcp",
        "coordinatorKeyId": hashlib.sha256(
            coordinator_public.encode("utf-8")
        ).hexdigest(),
        "evidenceKeyId": hashlib.sha256(sidecar_public.encode("utf-8")).hexdigest(),
        "activePages": 0,
        "configuredCapacity": configured_capacity,
        "profileSha256": profile_sha256,
        "admissionLane": admission_lane,
        "replayEntries": 0,
    }
    return {
        **unsigned,
        "healthSignature": _OpenSsl.sign(
            sidecar_private, contracts.canonical_bytes(unsigned), deadline
        ),
    }


def test_verify_health_rejects_admission_lane_reported_by_a_strict_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0/P1 calibration requires a strict sidecar. A sidecar that reports the
    JLL admission lane while the coordinator demands strict (admission_lane is
    the default None) must be rejected, even with a valid signature."""
    sidecar_private, sidecar_public = _OpenSsl.pair(time.monotonic() + 30)
    coordinator_public = "coordinator-public-fixture"
    keys = C10EphemeralKeys(
        "unused", coordinator_public, sidecar_private, sidecar_public, "transport-key"
    )
    profile = {"requested": {"global_pages": 4}}
    deadline = time.monotonic() + 30
    signed = _signed_health(
        coordinator_public=coordinator_public,
        sidecar_private=sidecar_private,
        sidecar_public=sidecar_public,
        configured_capacity=4,
        profile_sha256=contracts.sha256(profile["requested"]),
        admission_lane=_JLL_ADMISSION_LANE,
        deadline=deadline,
    )

    class FakeHealthResponse:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, _: int) -> bytes:
            return json.dumps(signed).encode("utf-8")

    monkeypatch.setattr(
        "capacity_c10.host_orchestration.urlopen",
        lambda *_args, **_kwargs: FakeHealthResponse(),
    )
    with pytest.raises(contracts.C10Error, match="signed health binding is invalid"):
        _C10HostTransport._verify_health(
            object.__new__(_C10HostTransport),
            "http://127.0.0.1:1",
            keys,
            profile,
            deadline,
        )


def test_verify_health_rejects_a_null_lane_when_the_admission_lane_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inverse direction: the JLL admission action requires its named
    lane, so a strict (admissionLane null) sidecar must be rejected too."""
    sidecar_private, sidecar_public = _OpenSsl.pair(time.monotonic() + 30)
    coordinator_public = "coordinator-public-fixture"
    keys = C10EphemeralKeys(
        "unused", coordinator_public, sidecar_private, sidecar_public, "transport-key"
    )
    profile = {"requested": {"global_pages": 4}}
    deadline = time.monotonic() + 30
    signed = _signed_health(
        coordinator_public=coordinator_public,
        sidecar_private=sidecar_private,
        sidecar_public=sidecar_public,
        configured_capacity=4,
        profile_sha256=contracts.sha256(profile["requested"]),
        admission_lane=None,
        deadline=deadline,
    )

    class FakeHealthResponse:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, _: int) -> bytes:
            return json.dumps(signed).encode("utf-8")

    monkeypatch.setattr(
        "capacity_c10.host_orchestration.urlopen",
        lambda *_args, **_kwargs: FakeHealthResponse(),
    )
    with pytest.raises(contracts.C10Error, match="signed health binding is invalid"):
        _C10HostTransport._verify_health(
            object.__new__(_C10HostTransport),
            "http://127.0.0.1:1",
            keys,
            profile,
            deadline,
            admission_lane=_JLL_ADMISSION_LANE,
        )


def test_rendered_identity_rejects_admission_lane_mismatch_in_either_direction() -> (
    None
):
    """`_rendered_identity` must bind the compose-rendered `C10_ADMISSION_LANE`
    to what the coordinator expects, in both directions."""
    expected_strict = {
        "C10_BROWSER_CPUS": "2",
        "MAX_CONCURRENT_PAGES": "4",
        "C10_PROFILE_SHA256": "a",
    }
    rendered_with_lane = json.dumps(
        {
            "services": {
                "playwright-service-c10": {
                    "cpus": "2",
                    "environment": {
                        "MAX_CONCURRENT_PAGES": "4",
                        "C10_PROFILE_SHA256": "a",
                        "C10_ADMISSION_LANE": _JLL_ADMISSION_LANE,
                    },
                    "ports": [
                        {"host_ip": "127.0.0.1", "published": "4444", "target": 3004}
                    ],
                }
            }
        }
    )
    assert (
        DockerComposeSidecar._rendered_identity(
            rendered_with_lane, 4444, expected_strict
        )
        is False
    )

    rendered_without_lane = json.dumps(
        {
            "services": {
                "playwright-service-c10": {
                    "cpus": "2",
                    "environment": {
                        "MAX_CONCURRENT_PAGES": "4",
                        "C10_PROFILE_SHA256": "a",
                    },
                    "ports": [
                        {"host_ip": "127.0.0.1", "published": "4444", "target": 3004}
                    ],
                }
            }
        }
    )
    expected_admission = {**expected_strict, "C10_ADMISSION_LANE": _JLL_ADMISSION_LANE}
    assert (
        DockerComposeSidecar._rendered_identity(
            rendered_without_lane, 4444, expected_admission
        )
        is False
    )


# ---------------------------------------------------------------------------
# Real-socket readiness-poll tests for `_poll_health` (via `_verify_health`).
# No Docker; a real loopback TCP listener stands in for the sidecar.
# ---------------------------------------------------------------------------


def _reserve_loopback_port() -> int:
    """Bind then close so the port is free but genuinely not listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        return int(reserved.getsockname()[1])


def _http_response_bytes(status: int, reason: str, body: bytes) -> bytes:
    headers = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    return headers + body


def _serve_one_response(
    listener: socket.socket, status: int, reason: str, body: bytes
) -> None:
    connection, _addr = listener.accept()
    try:
        connection.recv(65536)
        connection.sendall(_http_response_bytes(status, reason, body))
    finally:
        connection.close()


def _health_fixture() -> tuple[C10EphemeralKeys, dict[str, object], float, bytes]:
    sidecar_private, sidecar_public = _OpenSsl.pair(time.monotonic() + 30)
    coordinator_public = "coordinator-public-fixture"
    keys = C10EphemeralKeys(
        "unused", coordinator_public, sidecar_private, sidecar_public, "transport-key"
    )
    profile = {"requested": {"global_pages": 4}}
    deadline = time.monotonic() + 30
    signed = _signed_health(
        coordinator_public=coordinator_public,
        sidecar_private=sidecar_private,
        sidecar_public=sidecar_public,
        configured_capacity=4,
        profile_sha256=contracts.sha256(profile["requested"]),
        admission_lane=None,
        deadline=deadline,
    )
    return keys, profile, deadline, json.dumps(signed).encode("utf-8")


def test_verify_health_retries_through_an_initial_connection_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The port is genuinely closed (reserved then released) when
    ``_verify_health`` is first called, so the first poll attempt(s) must hit
    ECONNREFUSED. Only once the fixture server starts listening, ~0.5s later,
    does the poll succeed."""
    keys, profile, deadline, body = _health_fixture()
    port = _reserve_loopback_port()
    started = threading.Event()

    def serve() -> None:
        time.sleep(0.5)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
            listener.listen(1)
            started.set()
            _serve_one_response(listener, 200, "OK", body)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    start = time.monotonic()
    _C10HostTransport._verify_health(
        object.__new__(_C10HostTransport),
        f"http://127.0.0.1:{port}",
        keys,
        profile,
        deadline,
    )
    elapsed = time.monotonic() - start
    thread.join(timeout=5)
    assert not thread.is_alive()
    # The fixture server did not start listening until ~0.5s in, so success
    # proves at least one earlier attempt observed connection refused and the
    # poll loop retried rather than failing closed on it.
    assert elapsed >= 0.4


def test_verify_health_retries_through_reset_connections_then_succeeds() -> None:
    """The server accepts and immediately closes the first two connections
    (no bytes written), which the client observes as a reset/disconnect, not
    an HTTP response. `_verify_health` must retry through those and succeed
    on the third, real response."""
    keys, profile, deadline, body = _health_fixture()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen(5)

        def serve() -> None:
            for _ in range(2):
                connection, _addr = listener.accept()
                connection.close()
            _serve_one_response(listener, 200, "OK", body)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        _C10HostTransport._verify_health(
            object.__new__(_C10HostTransport),
            f"http://127.0.0.1:{port}",
            keys,
            profile,
            deadline,
        )
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_verify_health_fails_closed_immediately_on_an_http_error_status() -> None:
    """A real HTTP response, even an error one, is never a not-yet-ready
    signal: `_verify_health` must fail closed on the first request, with no
    retry."""
    keys, profile, deadline, _body = _health_fixture()
    request_count: list[int] = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen(1)

        def serve() -> None:
            connection, _addr = listener.accept()
            request_count.append(1)
            connection.recv(65536)
            connection.sendall(_http_response_bytes(503, "Service Unavailable", b"{}"))
            connection.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        start = time.monotonic()
        with pytest.raises(
            contracts.C10Error, match="signed sidecar health is unavailable"
        ):
            _C10HostTransport._verify_health(
                object.__new__(_C10HostTransport),
                f"http://127.0.0.1:{port}",
                keys,
                profile,
                deadline,
            )
        elapsed = time.monotonic() - start
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert request_count == [1]
    assert elapsed < 2.0


def test_verify_health_rejects_a_wrongly_signed_response_with_no_retry() -> None:
    """A validly-shaped HTTP 200 response whose signature does not verify is
    a strict verification failure, not a not-yet-ready signal: exactly one
    request, no retry."""
    keys, profile, deadline, _body = _health_fixture()
    other_sidecar_private, _other_sidecar_public = _OpenSsl.pair(deadline)
    wrongly_signed = _signed_health(
        coordinator_public="coordinator-public-fixture",
        sidecar_private=other_sidecar_private,  # signed with the WRONG key
        sidecar_public=keys.sidecar_public_pem,
        configured_capacity=4,
        profile_sha256=contracts.sha256(profile["requested"]),
        admission_lane=None,
        deadline=deadline,
    )
    body = json.dumps(wrongly_signed).encode("utf-8")
    request_count: list[int] = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen(1)

        def serve() -> None:
            connection, _addr = listener.accept()
            request_count.append(1)
            connection.recv(65536)
            connection.sendall(_http_response_bytes(200, "OK", body))
            connection.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        with pytest.raises(
            contracts.C10Error, match="signed health attestation is invalid"
        ):
            _C10HostTransport._verify_health(
                object.__new__(_C10HostTransport),
                f"http://127.0.0.1:{port}",
                keys,
                profile,
                deadline,
            )
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert request_count == [1]


def test_verify_health_gives_up_after_the_readiness_bound_on_a_never_listening_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A port that never starts listening must fail closed once the readiness
    bound elapses, rather than retrying until the outer run deadline."""
    monkeypatch.setattr(
        "capacity_c10.host_orchestration._HEALTH_READINESS_SECONDS", 1.0
    )
    keys, profile, deadline, _body = _health_fixture()
    port = _reserve_loopback_port()
    start = time.monotonic()
    with pytest.raises(
        contracts.C10Error, match="signed sidecar health is unavailable"
    ):
        _C10HostTransport._verify_health(
            object.__new__(_C10HostTransport),
            f"http://127.0.0.1:{port}",
            keys,
            profile,
            deadline,
        )
    elapsed = time.monotonic() - start
    # Bounded roughly by the 1.0s readiness bound (minus one poll interval of
    # slack) and well under the outer 30s deadline used by other tests here.
    assert 0.5 <= elapsed < 5.0
