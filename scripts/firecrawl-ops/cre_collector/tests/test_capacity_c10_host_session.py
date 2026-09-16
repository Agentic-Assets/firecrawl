"""Host-only C10 v3 authority and replay contracts.  No network or Docker."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from test_capacity_c10 import _plan

from capacity_c10 import contracts
from capacity_c10.host_session import (
    C10SealedCardRegistry,
    C10SessionStore,
    DockerComposeSidecar,
    _OpenSsl,
)


def test_durable_claim_is_one_use_and_rejects_an_alternate_ledger(
    tmp_path: Path,
) -> None:
    plan = _plan()
    session = contracts.new_session(plan)
    store = C10SessionStore(tmp_path / "private" / "session.json")
    claim = store.claim(plan, session)

    assert claim["arm"]["index"] == 0
    assert store.read_bound(plan, session)["claim_id"] == claim["claim_id"]
    with pytest.raises(contracts.C10Error, match="already has a claimed arm"):
        store.claim(plan, session)

    alternate = contracts.new_session(plan)
    alternate["consumed_arm_indexes"] = [0]
    with pytest.raises(contracts.C10Error, match="alternate plan or ledger"):
        store.read_bound(plan, alternate)


def test_ephemeral_ed25519_domains_are_separate_and_do_not_cross_verify() -> None:
    deadline = time.monotonic() + 20
    first_private, first_public = _OpenSsl.pair(deadline)
    second_private, second_public = _OpenSsl.pair(deadline)
    payload = b"c10 host-side session proof"
    signature = _OpenSsl.sign(first_private, payload, deadline)

    assert _OpenSsl.verify(first_public, payload, signature, deadline)
    assert not _OpenSsl.verify(second_public, payload, signature, deadline)
    assert first_private != second_private


def test_typescript_public_barrel_does_not_export_lifecycle_or_key_minting() -> None:
    root = Path(__file__).parents[1] / "capacity_c10" / "receipts"
    barrel = (root / "index.ts").read_text(encoding="utf-8")
    child = (root / "issued_browser_child.ts").read_text(encoding="utf-8")

    assert "local_browser_executor" not in barrel
    assert "local_operator_preflight" not in barrel
    assert not (root / "local_browser_executor.ts").exists()
    assert not (root / "local_operator_preflight.ts").exists()
    assert not (root / "jll_browser.ts").exists()
    assert "generateKeyPair" not in child
    assert "PRIVATE_KEY" not in child


def test_sealed_registry_rejects_arbitrary_non_jll_or_oversized_cards() -> None:
    plan = _plan()
    valid = {
        "id": "jll-1",
        "sourceKey": "jll",
        "cacheMode": "no-store",
        "maxBytes": 1024,
    }
    registry = C10SealedCardRegistry(plan, {"jll-1": valid})
    assert registry.resolve("jll-1")["id"] == "jll-1"
    with pytest.raises(contracts.C10Error, match="sealed registry"):
        registry.resolve("arbitrary")
    invalid = {**valid, "sourceKey": "cbre"}
    with pytest.raises(contracts.C10Error, match="JLL"):
        C10SealedCardRegistry(plan, {"jll-1": invalid})


def test_compose_overlay_is_rendered_before_start_and_owner_env_is_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    class Result:
        def __init__(self, code: int, stdout: str = "") -> None:
            self.returncode, self.stdout = code, stdout

    def fake_run(command: list[str], **_: object) -> Result:
        calls.append(command)
        if "config" in command:
            return Result(
                0,
                '{"services":{"playwright-service":{"ports":[{"host_ip":"127.0.0.1","published":"4444","target":3004}]}}}',
            )
        return Result(0)

    monkeypatch.setattr("capacity_c10.host_session.subprocess.run", fake_run)
    lifecycle = DockerComposeSidecar(tmp_path)
    lifecycle.start({"C10_BROWSER_CPUS": "2"}, 4444, time.monotonic() + 10)
    lifecycle.stop(time.monotonic() + 10)

    assert all("docker-compose.yaml" in call for call in calls)
    assert any("config" in call for call in calls)
    assert lifecycle._env_file is None
