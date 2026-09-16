"""Hermetic Python-to-Node C10 v3 loopback boundary integration."""

from __future__ import annotations

import base64
import os
import select
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from capacity_c10_test_support import controller_claim, sealed_jll_plan

from capacity_c10 import contracts
from capacity_c10.host_orchestration import _C10HostTransport
from capacity_c10.host_registry import C10SealedCardRegistry
from capacity_c10.host_store import C10SessionStore


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextmanager
def _loopback_listener(
    repo_root: Path, environment: dict[str, str]
) -> Iterator[subprocess.Popen[str]]:
    # `--import tsx` resolves the "tsx" package from cwd's node module
    # resolution chain. Only apps/playwright-service-ts (not the repository
    # root) owns that dependency, matching production's `_run_child`, which
    # runs its own child from the collector package for the same reason.
    process = subprocess.Popen(
        [
            "node",
            "--import",
            "tsx",
            "c10_browser_loopback_fixture.ts",
        ],
        cwd=repo_root / "apps/playwright-service-ts",
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], 0.1)
            if ready and process.stdout.readline().strip() == "c10-loopback-ready":
                break
            if process.poll() is not None:
                break
        else:
            pytest.fail("C10 loopback listener did not become ready")
        if process.poll() is not None:
            assert process.stderr is not None
            pytest.fail(
                "C10 loopback listener exited before readiness: "
                f"{process.stderr.read()}"
            )
        yield process
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_python_issued_capability_reaches_real_loopback_listener_and_quarantines_failure(
    tmp_path: Path,
) -> None:
    """No Docker, provider, or browser traffic: only the real local protocol."""
    repo_root = Path(__file__).parents[4]
    plan, cohort = sealed_jll_plan()
    cards = C10SealedCardRegistry(plan, cohort)
    store = C10SessionStore(tmp_path / "ledger" / "session.json")
    host = object.__new__(_C10HostTransport)
    host.repo_root = repo_root
    host.cards = cards
    host.session_store = store

    deadline = time.monotonic() + 60
    keys = host._keys(deadline)
    claim = controller_claim(store, plan)
    profile = plan["profiles"][claim["arm"]["variant"]]
    port = _free_loopback_port()
    environment = {
        **os.environ,
        "C10_COORDINATOR_PUBLIC_KEY_PEM_B64": base64.b64encode(
            keys.coordinator_public_pem.encode("utf-8")
        ).decode("ascii"),
        "C10_SIDECAR_EVIDENCE_PRIVATE_KEY_PEM_B64": base64.b64encode(
            keys.sidecar_private_pem.encode("utf-8")
        ).decode("ascii"),
        "PLAYWRIGHT_HOST_TRANSPORT_V3_KEY": keys.transport_key,
        "C10_BROWSER_INTERNAL_PORT": str(port),
        "C10_PROFILE_SHA256": contracts.sha256(profile["requested"]),
        "NODE_ENV": "test",
    }
    endpoint = f"http://127.0.0.1:{port}"
    issued = host._issue(
        endpoint,
        claim,
        plan,
        cards.resolve("jll-enumeration"),
        keys,
        deadline,
        0,
    )

    with _loopback_listener(repo_root, environment) as listener:
        host._verify_health(endpoint, keys, profile, deadline)
        evidence = host._run_child(issued, deadline)
        host._verify_evidence(evidence, issued, keys, deadline)

        denied: dict[str, Any] = dict(issued)
        denied["hostTransportKey"] = "wrong-test-transport-key"
        with pytest.raises(contracts.C10Error, match="issued browser child failed"):
            host._run_child(denied, deadline)
        store._controller_record_quarantine(
            claim, "loopback listener rejected tampered transport"
        )

    assert listener.returncode == 0
    assert evidence["binding"] == issued["capability"]["binding"]
    assert evidence["status"] == 200
    assert store._arm_path(0).with_suffix(".quarantine").exists(), (
        "a rejected child must retain durable quarantine evidence"
    )


def test_python_verifies_signed_health_reports_the_admission_lane(
    tmp_path: Path,
) -> None:
    """A sidecar started with C10_ADMISSION_LANE set must report that lane in
    its signed health, and `_verify_health` must accept it only when the
    caller names the same lane (the default strict check rejects it)."""
    repo_root = Path(__file__).parents[4]
    plan, cohort = sealed_jll_plan()
    cards = C10SealedCardRegistry(plan, cohort)
    store = C10SessionStore(tmp_path / "ledger" / "session.json")
    host = object.__new__(_C10HostTransport)
    host.repo_root = repo_root
    host.cards = cards
    host.session_store = store

    deadline = time.monotonic() + 60
    keys = host._keys(deadline)
    claim = controller_claim(store, plan)
    profile = plan["profiles"][claim["arm"]["variant"]]
    port = _free_loopback_port()
    lane = "jll-canonical-url-lexicographic-v1"
    environment = {
        **os.environ,
        "C10_COORDINATOR_PUBLIC_KEY_PEM_B64": base64.b64encode(
            keys.coordinator_public_pem.encode("utf-8")
        ).decode("ascii"),
        "C10_SIDECAR_EVIDENCE_PRIVATE_KEY_PEM_B64": base64.b64encode(
            keys.sidecar_private_pem.encode("utf-8")
        ).decode("ascii"),
        "PLAYWRIGHT_HOST_TRANSPORT_V3_KEY": keys.transport_key,
        "C10_BROWSER_INTERNAL_PORT": str(port),
        "C10_PROFILE_SHA256": contracts.sha256(profile["requested"]),
        "C10_ADMISSION_LANE": lane,
        "NODE_ENV": "test",
    }
    endpoint = f"http://127.0.0.1:{port}"

    with _loopback_listener(repo_root, environment) as listener:
        host._verify_health(endpoint, keys, profile, deadline, admission_lane=lane)
        with pytest.raises(
            contracts.C10Error, match="signed health binding is invalid"
        ):
            host._verify_health(endpoint, keys, profile, deadline)

    assert listener.returncode == 0
