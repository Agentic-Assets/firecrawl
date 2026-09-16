"""Hostile-root contracts for the C10 durable session ledger."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from capacity_c10_test_support import controller_claim, sealed_jll_plan

from capacity_c10 import host_store
from capacity_c10.contracts import C10Error


def _ledger(tmp_path: Path) -> tuple[host_store.C10SessionStore, Path]:
    root = tmp_path / ".cre-c10-ledger-v1"
    return host_store.C10SessionStore(root / "session.json"), root


def _replace_root(root: Path) -> Path:
    retired = root.with_name(f"{root.name}-retired")
    root.rename(retired)
    root.mkdir(mode=0o700)
    return retired


@pytest.mark.parametrize("kind", ["symlink", "not-directory", "wrong-mode"])
def test_session_store_rejects_hostile_root_before_claim(
    tmp_path: Path, kind: str
) -> None:
    plan, _cohort = sealed_jll_plan()
    store, root = _ledger(tmp_path)
    if kind == "symlink":
        target = tmp_path / "attacker-root"
        target.mkdir(mode=0o700)
        root.symlink_to(target, target_is_directory=True)
    elif kind == "not-directory":
        root.write_text("not a directory", encoding="utf-8")
    else:
        root.mkdir(mode=0o700)
        root.chmod(0o750)

    with pytest.raises(C10Error, match="ledger root"):
        controller_claim(store, plan)


def test_session_store_rejects_root_owned_by_another_effective_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan, _cohort = sealed_jll_plan()
    store, root = _ledger(tmp_path)
    root.mkdir(mode=0o700)
    actual_uid = os.geteuid()
    monkeypatch.setattr(host_store.os, "geteuid", lambda: actual_uid + 1)

    with pytest.raises(C10Error, match="ledger root"):
        controller_claim(store, plan)


@pytest.mark.parametrize(
    "operation",
    ["claim", "read", "quarantine", "terminal"],
)
def test_session_store_rejects_root_swaps_for_every_ledger_operation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: str
) -> None:
    plan, _cohort = sealed_jll_plan()
    store, root = _ledger(tmp_path)
    claim = dict(controller_claim(store, plan))
    _replace_root(root)

    actions: dict[str, Callable[[], object]] = {
        "claim": lambda: controller_claim(store, plan),
        "read": lambda: store.read_bound(plan, claim),
        "quarantine": lambda: store.record_quarantine(claim, "hostile-root"),
        "terminal": lambda: store.record_terminal(plan, claim, {}),
    }
    if operation == "terminal":
        monkeypatch.setattr(
            store, "_validate_authenticated_terminal", lambda *_a, **_k: None
        )

    with pytest.raises(C10Error, match="root identity changed"):
        actions[operation]()
    assert list(root.iterdir()) == []
