"""C10 production receipt revalidation, sequence, and recovery contracts."""

from __future__ import annotations

import copy
import hashlib
import time
from pathlib import Path
from typing import Self

import pytest
from capacity_c10_test_support import sealed_jll_plan

from capacity_c10 import contracts, host_store
from capacity_c10.host_session import C10SessionStore, _OpenSsl
from capacity_c10.production import (
    execute_counterbalanced_sequence,
    execute_production_arm,
    main,
)


def _secure_roots(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)


def _seed_valid_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plan: dict[str, object],
    store: C10SessionStore,
    claim: dict[str, object],
) -> dict[str, bytes]:
    """Persist a signed, re-openable predecessor terminal without Linux I/O."""
    private_key, public_key = _OpenSsl.pair(time.monotonic() + 30)
    arm = claim["arm"]
    assert isinstance(arm, dict)
    profile = plan["profiles"][arm["variant"]]  # type: ignore[index]
    assert isinstance(profile, dict)
    binding = {
        "planSha256": plan["plan_sha256"],
        "cohortSha256": plan["cohort_sha256"],
        "sessionSha256": claim["session_sha256"],
        "armSha256": contracts.sha256(arm),
        "profileSha256": contracts.sha256(profile["requested"]),
        "cardSha256": "c" * 64,
        "manifestSha256": "d" * 64,
    }
    artifacts: dict[str, bytes] = {}
    manifest: list[dict[str, object]] = []
    evidence: list[dict[str, object]] = []
    for index in range(17):
        unsigned = {"binding": binding, "fixtureSequence": index}
        signed = {
            **unsigned,
            "evidenceSignature": _OpenSsl.sign(
                private_key, contracts.canonical_bytes(unsigned), time.monotonic() + 30
            ),
        }
        body = contracts.canonical_bytes(signed)
        digest = hashlib.sha256(body).hexdigest()
        name = f"browser-evidence-{index}-{digest}.sealed"
        artifacts[name] = body
        manifest.append({"name": name, "sha256": digest, "bytes": len(body)})
        evidence.append(signed)
    root = {"path": str((tmp_path / "private-receipts").resolve()), "id": "r" * 64}

    class FakeReceipts:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def descriptor(self) -> dict[str, str]:
            return root

        def read_sealed(self, artifact: dict[str, object]) -> bytes:
            try:
                return artifacts[str(artifact["name"])]
            except KeyError as exc:
                raise contracts.C10Error("fixture receipt artifact is missing") from exc

    monkeypatch.setattr(
        host_store.PrivateReceiptStore,
        "create",
        classmethod(lambda _cls, _root: FakeReceipts()),
    )
    authenticated = {
        "kind": "cre_capacity_c10_authenticated_host_arm_v1",
        "plan_sha256": plan["plan_sha256"],
        "index": arm["index"],
        "variant": arm["variant"],
        "no_write": plan["no_write"],
        "runtime": {
            "profile_config_sha256": plan["profiles"]["config_sha256"],  # type: ignore[index]
            "profile_requested_sha256": contracts.sha256(profile["requested"]),
            "runtime_receipt_sha256": "a" * 64,
            "container_snapshot_sha256": "b" * 64,
            "transition_sha256": "c" * 64,
        },
        "host_result": {
            "claim": claim,
            "receipt_root": root,
            "evidence_manifest": manifest,
            "evidence_manifest_sha256": contracts.sha256(evidence),
            "evidence_public_key": public_key,
            "evidence_key_id": hashlib.sha256(public_key.encode()).hexdigest(),
            "binding": binding,
        },
    }
    store.record_terminal(plan, claim, authenticated)
    return artifacts


@pytest.mark.parametrize("corruption", ["missing", "tampered"])
def test_prior_terminal_receipts_block_later_arm_before_runtime_or_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, corruption: str
) -> None:
    plan, cohort = sealed_jll_plan()
    store = C10SessionStore(
        tmp_path / ".cre-c10-ledger-v1" / f"{plan['plan_sha256']}.json"
    )
    claim = dict(store.claim(plan))
    artifacts = _seed_valid_terminal(monkeypatch, tmp_path, plan, store, claim)
    name = next(iter(artifacts))
    if corruption == "missing":
        del artifacts[name]
    else:
        artifacts[name] = b'{"tampered":true}'

    lock_events: list[str] = []

    class Lock:
        def __init__(self) -> None:
            self.path, self.retain_on_exit = tmp_path / ".cre.lock", False

        def acquire(self) -> None:
            lock_events.append("lock")

        def arm_benchmark(self, _evidence: object) -> None:
            lock_events.append("arm")

        def release(self) -> None:
            lock_events.append("release")

    monkeypatch.setattr(
        "capacity_c10.production.canonical_shared_lock_dir",
        lambda _root: tmp_path / ".cre.lock",
    )
    monkeypatch.setattr(
        "capacity_c10.production._canonical_lock",
        lambda _root: Lock(),
    )
    monkeypatch.setattr(
        "capacity_c10.production._C10HostTransport",
        lambda **_kwargs: pytest.fail("tampered predecessor must fail before host"),
    )
    monkeypatch.setattr(
        "capacity_c10.production._validate_claim_inputs",
        lambda _plan, arm, **kwargs: (
            kwargs["private_root"] / f"arm-{arm['index']}",
            kwargs["runtime_receipt_root"] / f"arm-{arm['index']}.json",
            kwargs["approval_root"] / f"arm-{arm['index']}.json",
            kwargs["admission_root"] / f"arm-{arm['index']}.json",
        ),
    )
    with pytest.raises(
        contracts.C10Error, match="artifact is missing|signature is invalid"
    ):
        execute_production_arm(
            repo_root=tmp_path,
            plan=plan,
            cohort=cohort,
            private_root=tmp_path / "private",
            runtime_receipt_root=tmp_path / "receipts",
            approval_root=tmp_path / "approvals",
            admission_root=tmp_path / "admissions",
        )
    assert lock_events == ["lock", "arm", "release"]


def test_terminal_revalidation_uses_one_expiring_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan, _cohort = sealed_jll_plan()
    store = C10SessionStore(tmp_path / "private" / f"{plan['plan_sha256']}.json")
    claim = dict(store.claim(plan))
    _seed_valid_terminal(monkeypatch, tmp_path, plan, store, claim)
    seen_deadlines: list[float] = []
    monkeypatch.setattr(
        host_store._OpenSsl,
        "verify",
        lambda _key, _body, _signature, deadline: (
            seen_deadlines.append(deadline) or True
        ),
    )
    deadline = time.monotonic() + 10
    store.load_terminal(plan, 0, deadline=deadline)
    assert seen_deadlines == [deadline] * 17
    with pytest.raises(contracts.C10Error, match="revalidation deadline expired"):
        store.load_terminal(plan, 0, deadline=time.monotonic())


def test_counterbalance_passes_only_canonical_roots_until_protocol_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan, cohort = sealed_jll_plan()
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "capacity_c10.production.canonical_shared_lock_dir",
        lambda root: root / ".cre.lock",
    )
    monkeypatch.setattr(
        "capacity_c10.production.execute_production_arm",
        lambda **kwargs: calls.append(kwargs) or {"next_arm_index": len(calls)},
    )

    fresh = tmp_path / "fresh"
    execute_counterbalanced_sequence(
        repo_root=fresh,
        plan=plan,
        cohort=cohort,
        private_root=fresh / "private",
        runtime_receipt_root=fresh / "receipts",
        approval_root=fresh / "approvals",
        admission_root=fresh / "admissions",
    )
    assert len(calls) == 8
    for call in calls:
        assert call["private_root"] == fresh / "private"
        assert call["runtime_receipt_root"] == fresh / "receipts"
        assert call["approval_root"] == fresh / "approvals"
        assert call["admission_root"] == fresh / "admissions"


@pytest.mark.parametrize("arm_sequence", [[], ["p0"]])
def test_counterbalance_rejects_noncanonical_arm_sequences_before_execution(
    tmp_path: Path, arm_sequence: list[str]
) -> None:
    plan, cohort = sealed_jll_plan()
    invalid = copy.deepcopy(plan)
    invalid["arm_sequence"] = arm_sequence
    invalid["plan_sha256"] = contracts.sha256(
        {key: value for key, value in invalid.items() if key != "plan_sha256"}
    )
    with pytest.raises(contracts.C10Error):
        execute_counterbalanced_sequence(
            repo_root=tmp_path,
            plan=invalid,
            cohort=cohort,
            private_root=tmp_path / "private",
            runtime_receipt_root=tmp_path / "receipts",
            approval_root=tmp_path / "approvals",
            admission_root=tmp_path / "admissions",
        )


@pytest.mark.parametrize("timeout_seconds", [0, -1, 121, float("inf")])
def test_production_rejects_invalid_timeout_before_lock_or_ledger_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, timeout_seconds: float
) -> None:
    plan, cohort = sealed_jll_plan()
    monkeypatch.setattr(
        "capacity_c10.production._canonical_lock",
        lambda _root: pytest.fail("invalid timeout must fail before lock"),
    )
    with pytest.raises(contracts.C10Error, match="timeout_seconds"):
        execute_production_arm(
            repo_root=tmp_path,
            plan=plan,
            cohort=cohort,
            private_root=tmp_path / "private",
            runtime_receipt_root=tmp_path / "receipts",
            timeout_seconds=timeout_seconds,
        )
    assert not (tmp_path / ".cre-c10-ledger-v1").exists()


def test_lock_arm_failure_quarantines_and_releases_before_a_replay_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan, cohort = sealed_jll_plan()
    events: list[str] = []

    class Lock:
        def __init__(self) -> None:
            self.path, self.retain_on_exit = tmp_path / ".cre.lock", False

        def acquire(self) -> None:
            events.append("lock")

        def arm_benchmark(self, _evidence: object) -> None:
            events.append("arm")
            raise contracts.C10Error("arm marker failed")

        def release(self) -> None:
            events.append("release")

    monkeypatch.setattr("capacity_c10.production._canonical_lock", lambda _: Lock())
    monkeypatch.setattr(
        "capacity_c10.production.canonical_shared_lock_dir",
        lambda _root: tmp_path / ".cre.lock",
    )
    monkeypatch.setattr(
        "capacity_c10.production._C10HostTransport",
        lambda **_kwargs: type("Host", (), {"lock_path": tmp_path / ".cre.lock"})(),
    )
    _secure_roots(tmp_path / "private", tmp_path / "receipts")
    with pytest.raises(contracts.C10Error, match="arm marker failed"):
        execute_production_arm(
            repo_root=tmp_path,
            plan=plan,
            cohort=cohort,
            private_root=tmp_path / "private",
            runtime_receipt_root=tmp_path / "receipts",
        )
    assert events == ["lock", "arm", "release"]
    assert (
        tmp_path / ".cre-c10-ledger-v1" / f"{plan['plan_sha256']}.quarantine"
    ).exists()
    with pytest.raises(contracts.C10Error, match="terminal recovery"):
        execute_production_arm(
            repo_root=tmp_path,
            plan=plan,
            cohort=cohort,
            private_root=tmp_path / "private",
            runtime_receipt_root=tmp_path / "receipts",
        )


def test_cli_execute_rejects_invalid_timeout_before_reading_or_mutating_paths() -> None:
    with pytest.raises(contracts.C10Error, match="lifecycle timeout"):
        main(
            [
                "--execute",
                "--timeout-seconds",
                "0",
                "--plan",
                "/definitely/missing/plan.json",
                "--cohort",
                "/definitely/missing/cohort.json",
                "--private-root",
                "/definitely/missing/private",
                "--runtime-receipt-root",
                "/definitely/missing/receipts",
                "--repo-root",
                "/definitely/missing/repo",
            ]
        )
