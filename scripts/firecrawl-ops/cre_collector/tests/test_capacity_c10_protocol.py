"""C10 ledger, registry, and crypto contracts.  No network or Docker."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from capacity_c10_test_support import sealed_jll_plan
from test_capacity_c10 import _cohort, _plan

from capacity_c10 import contracts
from capacity_c10.host_session import C10SealedCardRegistry, C10SessionStore, _OpenSsl


def test_durable_claim_is_one_use_and_rejects_an_alternate_ledger(
    tmp_path: Path,
) -> None:
    plan = _plan()
    store = C10SessionStore(tmp_path / "private" / "session.json")
    claim = store.claim(plan)

    assert claim["arm"]["index"] == 0
    assert store.read_bound(plan, claim)["claim_id"] == claim["claim_id"]
    with pytest.raises(contracts.C10Error, match="terminal recovery"):
        store.claim(plan)
    store.record_terminal(claim, {})
    assert store.claim(plan)["arm"]["index"] == 1


def test_protocol_ledger_constructs_the_fixed_eight_arm_sequence_internally(
    tmp_path: Path,
) -> None:
    plan = _plan()
    store = C10SessionStore(tmp_path / "private" / f"{plan['plan_sha256']}.json")
    claimed: list[dict[str, object]] = []
    for index, variant in enumerate(contracts.ARM_SEQUENCE):
        claim = dict(store.claim(plan))
        assert claim["arm"] == {
            "index": index,
            "variant": variant,
            "pair_index": index // 2,
            "must_rollback_to_p0": variant == "p1",
        }
        store.record_terminal(claim, {})
        claimed.append(claim)
    with pytest.raises(contracts.C10Error, match="already consumed"):
        store.claim(plan)
    assert [claim["arm"]["variant"] for claim in claimed] == list(
        contracts.ARM_SEQUENCE
    )


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
        store.claim(plan)

    extra.unlink()
    claim = store.claim(plan)
    store.record_terminal(claim, {})
    terminal = store._arm_path(0)
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
