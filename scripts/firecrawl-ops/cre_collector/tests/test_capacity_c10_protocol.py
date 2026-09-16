"""C10 ledger, registry, and crypto contracts.  No network or Docker."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from capacity_c10_test_support import controller_claim, sealed_jll_plan
from test_capacity_c10 import _cohort, _plan

from capacity_c10 import contracts
from capacity_c10.host_session import C10SealedCardRegistry, C10SessionStore, _OpenSsl


def test_public_store_calls_cannot_fabricate_a_controller_accepted_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _plan()
    store = C10SessionStore(tmp_path / "private" / "session.json")
    with pytest.raises(contracts.C10Error, match="controller provenance"):
        store.claim(plan)

    claim = controller_claim(store, plan)
    monkeypatch.setattr(
        store, "_validate_authenticated_terminal", lambda *_a, **_k: None
    )
    fabricated = {
        "host_result": {
            "evidence_public_key": "attacker-key",
            "receipt_root": {"path": str(tmp_path), "id": "a" * 64},
        }
    }
    with pytest.raises(contracts.C10Error, match="controller provenance"):
        store.record_terminal(plan, claim, fabricated, deadline=time.monotonic() + 30)


def test_durable_claim_is_one_use_and_rejects_an_alternate_ledger(
    tmp_path: Path,
) -> None:
    plan = _plan()
    store = C10SessionStore(tmp_path / "private" / "session.json")
    claim = controller_claim(store, plan)

    assert claim["arm"]["index"] == 0
    assert store.read_bound(plan, claim)["claim_id"] == claim["claim_id"]
    with pytest.raises(contracts.C10Error, match="terminal recovery"):
        controller_claim(store, plan)
    with pytest.raises(contracts.C10Error, match="authenticated host arm schema"):
        store.record_terminal(plan, claim, {})
    assert store.read_bound(plan, claim)["claim_id"] == claim["claim_id"]


def test_protocol_ledger_constructs_the_fixed_eight_arm_sequence_internally(
    tmp_path: Path,
) -> None:
    plan = _plan()
    store = C10SessionStore(tmp_path / "private" / f"{plan['plan_sha256']}.json")
    claim = dict(controller_claim(store, plan))
    assert claim["arm"] == {
        "index": 0,
        "variant": contracts.ARM_SEQUENCE[0],
        "pair_index": 0,
        "must_rollback_to_p0": False,
    }
    assert len(contracts.ARM_SEQUENCE) == 8
    assert set(contracts.ARM_SEQUENCE) == {"p0", "p1"}


def test_protocol_ledger_rejects_extra_or_tampered_terminal_records(
    tmp_path: Path,
) -> None:
    plan = _plan()
    store = C10SessionStore(tmp_path / "private" / f"{plan['plan_sha256']}.json")
    store.path.parent.mkdir(parents=True, mode=0o700)
    extra = store.path.with_name(f"{store.path.stem}.arm-8.json")
    extra.write_text("{}", encoding="utf-8")
    extra.chmod(0o600)
    with pytest.raises(contracts.C10Error, match="extra arm"):
        controller_claim(store, plan)

    extra.unlink()
    controller_claim(store, plan)
    record = {
        "kind": "cre_capacity_c10_v3_host_terminal",
        "plan_sha256": plan["plan_sha256"],
        "authenticated_arm": {},
        "authenticated_arm_sha256": contracts.sha256({}),
    }
    terminal = store._arm_path(0)
    terminal.write_text(json.dumps(record), encoding="utf-8")
    terminal.chmod(0o600)
    payload = json.loads(terminal.read_text(encoding="utf-8"))
    payload["authenticated_arm_sha256"] = "0" * 64
    terminal.write_text(json.dumps(payload), encoding="utf-8")
    terminal.chmod(0o600)
    with pytest.raises(contracts.C10Error, match="cannot be reverified"):
        store.load_terminal(plan, 0)


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
    plan, cohort = sealed_jll_plan()
    registry = C10SealedCardRegistry(plan, cohort)
    assert registry.resolve("jll-member-0")["id"] == "jll-member-0"
    projection = registry.resolve("jll-member-0")
    projection["url"] = "https://attacker.invalid/"
    assert registry.resolve("jll-member-0")["allowedHost"] == "property.jll.com"
    with pytest.raises(contracts.C10Error, match="sealed registry"):
        registry.resolve("arbitrary")
    alternate = _cohort()
    with pytest.raises(contracts.C10Error, match="different plan or cohort"):
        C10SealedCardRegistry(plan, alternate)
