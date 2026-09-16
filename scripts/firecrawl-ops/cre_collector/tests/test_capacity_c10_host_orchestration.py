"""C10 host-session orchestration contracts.  No external lifecycle runs."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import time
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
    _OpenSsl,
)


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

    monkeypatch.setattr(
        "capacity_c10.host_orchestration.canonical_shared_lock_dir",
        lambda _root: tmp_path / ".cre.lock",
    )
    sidecar = Sidecar()
    host = _C10HostTransport(
        repo_root=tmp_path,
        session_store=C10SessionStore(tmp_path / "session.json"),
        private_root=tmp_path / "private",
        cards=registry,
        sidecar=sidecar,
    )
    assert not hasattr(host, "execute")
    with pytest.raises(contracts.C10Error, match="direct host execution is retired"):
        host._retired_direct_execution(
            plan_a,
            timeout_seconds=1,
            _claim={},
            _held_shared_lock=None,  # type: ignore[arg-type]
            _deadline=time.monotonic() + 1,
        )
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
    monkeypatch.setattr("capacity_c10.host_orchestration.SharedLock", FakeLock)
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

    monkeypatch.setattr("capacity_c10.host_orchestration.SharedLock", Lock)
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
