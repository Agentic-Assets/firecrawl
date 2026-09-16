"""C10 production entrypoint, CLI, claim, and root-invariant contracts."""

from __future__ import annotations

import hashlib
import inspect
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Self

import pytest
from capacity_c10_test_support import sealed_jll_plan

import cre_capacity_runtime as runtime
from capacity_c10 import contracts, host_store, production
from capacity_c10.host_session import C10HostExecutionSession, C10SessionStore, _OpenSsl
from capacity_c10.production import execute_production_arm, main


def _secure_roots(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)


def _valid_p1_approval(path: Path, plan: dict[str, object]) -> None:
    approval = {
        "schema_version": runtime.SCHEMA_VERSION,
        "kind": runtime.APPROVAL_KIND,
        "profile": plan["profiles"]["p1"]["name"],  # type: ignore[index]
        "config_sha256": plan["profiles"]["config_sha256"],  # type: ignore[index]
        "transition_receipt_sha256": "a" * 64,
        "source_git_sha": "b" * 40,
        "approved_by": "coordinating-review",
        "approved": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_after_seconds": runtime.RECEIPT_MAX_AGE_SECONDS,
        "nonce": "c" * 64,
    }
    path.write_text(json.dumps(approval), encoding="utf-8")
    path.chmod(0o600)


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
    store.record_terminal(claim, authenticated)
    return artifacts


def test_production_entrypoint_constructs_host_without_browser_callback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    production_parameters = set(inspect.signature(execute_production_arm).parameters)
    host_parameters = set(inspect.signature(C10HostExecutionSession.execute).parameters)
    assert {"child", "cards", "evidence", "run_browser_arm"}.isdisjoint(
        production_parameters
    )
    assert "session" not in production_parameters
    assert "child" not in host_parameters


def test_production_approval_gate_uses_only_runtime_public_validator() -> None:
    source = inspect.getsource(production._validate_claim_inputs)
    assert "runtime.validate_review_approval(" in source
    for forbidden in (
        "runtime._validate_review_authority",
        "runtime.REVIEW_APPROVAL_MAX_BYTES",
        "runtime.NONCE_PATTERN",
        "runtime.SHA_PATTERN",
        "runtime._parse_time",
    ):
        assert forbidden not in source


def test_production_cli_is_dry_run_by_default_and_never_calls_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan, cohort = sealed_jll_plan()
    paths = {
        "plan": tmp_path / "plan.json",
        "cohort": tmp_path / "cohort.json",
    }
    for key, value in (("plan", plan), ("cohort", cohort)):
        paths[key].write_text(json.dumps(value), encoding="utf-8")
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir()
    monkeypatch.setattr(
        "capacity_c10.production.runtime.preflight",
        lambda *_args, **_kwargs: pytest.fail("dry-run must not call runtime"),
    )
    monkeypatch.setattr(
        "capacity_c10.production.canonical_shared_lock_dir",
        lambda _root: tmp_path / ".cre.lock",
    )
    assert (
        main(
            [
                "--plan",
                str(paths["plan"]),
                "--cohort",
                str(paths["cohort"]),
                "--private-root",
                str(tmp_path / "private"),
                "--runtime-receipt-root",
                str(receipt_root),
                "--repo-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "arm_sequence": None,
        "external_calls": False,
        "mode": "smoke",
        "next_arm_index": 0,
        "state": "dry_run",
    }


def test_production_dry_run_rejects_counterbalance_without_all_p1_admissions(
    tmp_path: Path,
) -> None:
    plan, cohort = sealed_jll_plan()
    plan_path, cohort_path = tmp_path / "plan.json", tmp_path / "cohort.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    cohort_path.write_text(json.dumps(cohort), encoding="utf-8")
    approvals, admissions = tmp_path / "approvals", tmp_path / "admissions"
    approvals.mkdir()
    admissions.mkdir()
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir()
    with pytest.raises(contracts.C10Error, match="missing a P1 approval"):
        main(
            [
                "--counterbalanced",
                "--plan",
                str(plan_path),
                "--cohort",
                str(cohort_path),
                "--private-root",
                str(tmp_path / "private"),
                "--runtime-receipt-root",
                str(receipt_root),
                "--approval-root",
                str(approvals),
                "--admission-root",
                str(admissions),
                "--repo-root",
                str(Path(__file__).parents[4]),
            ]
        )


def test_smoke_dry_run_and_execute_bind_the_same_durable_p1_arm_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan, cohort = sealed_jll_plan()
    plan_path, cohort_path = tmp_path / "plan.json", tmp_path / "cohort.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    cohort_path.write_text(json.dumps(cohort), encoding="utf-8")
    monkeypatch.setattr(
        "capacity_c10.production.canonical_shared_lock_dir",
        lambda _root: tmp_path / ".cre.lock",
    )
    store = C10SessionStore(
        tmp_path / ".cre-c10-ledger-v1" / f"{plan['plan_sha256']}.json"
    )
    first = store.claim(plan)
    store.record_terminal(first, {})
    roots = {
        "private": tmp_path / "private",
        "receipts": tmp_path / "receipts",
        "approvals": tmp_path / "approvals",
        "admissions": tmp_path / "admissions",
    }
    for root in roots.values():
        root.mkdir()
    approved = roots["approvals"] / "arm-1.json"
    approved.write_text("{}", encoding="utf-8")
    command = [
        "--smoke",
        "--plan",
        str(plan_path),
        "--cohort",
        str(cohort_path),
        "--private-root",
        str(roots["private"]),
        "--runtime-receipt-root",
        str(roots["receipts"]),
        "--approval-root",
        str(roots["approvals"]),
        "--admission-root",
        str(roots["admissions"]),
        "--repo-root",
        str(tmp_path),
    ]
    assert main(command) == 0

    def execute(**kwargs: object) -> dict[str, object]:
        assert kwargs["approval_root"] / "arm-1.json" == approved  # type: ignore[operator]
        assert not (kwargs["admission_root"] / "arm-1.json").exists()  # type: ignore[operator]
        return {"state": "fake-executed"}

    monkeypatch.setattr("capacity_c10.production.execute_production_arm", execute)
    assert main(["--execute", *command]) == 0


def test_cli_rejects_ambiguous_singular_root_flags() -> None:
    with pytest.raises(contracts.C10Error, match="obsolete; use canonical"):
        main(["--runtime-receipt", "/tmp/receipt.json"])


def test_production_claims_before_runtime_or_host_and_terminalizes_authenticated_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan, cohort = sealed_jll_plan()
    events: list[str] = []

    class Lock:
        def __init__(self) -> None:
            self.path, self.retain_on_exit = tmp_path / ".cre.lock", False

        def acquire(self) -> None:
            events.append("lock")

        def release(self) -> None:
            events.append("release")

        def arm_benchmark(self, _evidence: object) -> None:
            events.append("arm")

        def disarm_benchmark(self) -> None:
            events.append("disarm")

    receipt = {
        "profile": "c10-p0",
        "config_sha256": plan["profiles"]["config_sha256"],
        "receipt_sha256": "a" * 64,
        "baseline": {"snapshot_sha256": "b" * 64, "transition_sha256": "c" * 64},
    }

    class Host:
        def __init__(self, **_: object) -> None:
            self.lock_path = tmp_path / ".cre.lock"

        def execute(self, plan: object, **kwargs: object) -> dict[str, object]:
            assert (tmp_path / ".cre-c10-ledger-v1").exists()
            events.append("host")
            claim = kwargs["_claim"]
            assert isinstance(claim, dict)
            return {
                "claim": claim,
                "receipt_root": {"path": str(tmp_path / "private"), "id": "f" * 64},
                "evidence_manifest": [
                    {
                        "name": f"browser-evidence-{index}-{'d' * 64}.sealed",
                        "sha256": "d" * 64,
                        "bytes": 1,
                    }
                    for index in range(17)
                ],
                "evidence_manifest_sha256": "e" * 64,
                "evidence_public_key": "public-test-key",
                "evidence_key_id": hashlib.sha256(b"public-test-key").hexdigest(),
                "binding": {
                    "planSha256": plan["plan_sha256"],  # type: ignore[index]
                    "cohortSha256": plan["cohort_sha256"],  # type: ignore[index]
                },
            }

    monkeypatch.setattr("capacity_c10.production._canonical_lock", lambda _: Lock())
    monkeypatch.setattr(
        "capacity_c10.production.canonical_shared_lock_dir",
        lambda _root: tmp_path / ".cre.lock",
    )
    monkeypatch.setattr("capacity_c10.production.C10HostExecutionSession", Host)
    monkeypatch.setattr(
        "capacity_c10.production.runtime.experiment.load_profile",
        lambda _path, name: (
            {"requested": plan["profiles"]["p0"]["requested"]},
            plan["profiles"]["config_sha256"],
        ),
    )

    def preflight(*_: object, **__: object) -> dict[str, object]:
        assert (tmp_path / ".cre-c10-ledger-v1").exists()
        events.append("preflight")
        return receipt

    monkeypatch.setattr("capacity_c10.production.runtime.preflight", preflight)
    monkeypatch.setattr(
        "capacity_c10.production.runtime.capture_runtime",
        lambda **_: type(
            "Capture", (), {"public": {"settlement": {"state": "idle"}}}
        )(),
    )
    monkeypatch.setattr(
        "capacity_c10.production.runtime.evaluate_state", lambda *_: {"idle": True}
    )
    _secure_roots(tmp_path / "private", tmp_path / "receipts")
    result = execute_production_arm(
        repo_root=tmp_path,
        plan=plan,
        cohort=cohort,
        private_root=tmp_path / "private",
        runtime_receipt_root=tmp_path / "receipts",
    )
    assert events == ["lock", "arm", "preflight", "host", "disarm", "release"]
    assert result["comparison_state"].startswith("not_comparable")
    assert result["terminal"]["state"] == "terminal"
    assert result["next_arm_index"] == 1


def test_production_rolls_back_and_quarantines_p1_failure_before_lock_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan, cohort = sealed_jll_plan()
    seed = C10SessionStore(
        tmp_path / ".cre-c10-ledger-v1" / f"{plan['plan_sha256']}.json"
    )
    seed_claim = dict(seed.claim(plan))
    _seed_valid_terminal(monkeypatch, tmp_path, plan, seed, seed_claim)
    events: list[str] = []

    class Lock:
        def __init__(self) -> None:
            self.path, self.retain_on_exit = tmp_path / ".cre.lock", False

        def acquire(self) -> None:
            events.append("lock")

        def release(self) -> None:
            events.append("release")

        def arm_benchmark(self, _evidence: object) -> None:
            events.append("arm")

        def disarm_benchmark(self) -> None:
            events.append("disarm")

    receipt = {
        "profile": "c10-p1",
        "config_sha256": plan["profiles"]["config_sha256"],
        "receipt_sha256": "a" * 64,
        "baseline": {"snapshot_sha256": "b" * 64, "transition_sha256": "c" * 64},
    }

    class Host:
        def __init__(self, **_: object) -> None:
            self.lock_path = tmp_path / ".cre.lock"

        def execute(self, *_: object, **__: object) -> dict[str, object]:
            events.append("host")
            raise contracts.C10Error("host failure")

    monkeypatch.setattr("capacity_c10.production._canonical_lock", lambda _: Lock())
    monkeypatch.setattr(
        "capacity_c10.production.canonical_shared_lock_dir",
        lambda _root: tmp_path / ".cre.lock",
    )
    monkeypatch.setattr("capacity_c10.production.C10HostExecutionSession", Host)
    monkeypatch.setattr(
        "capacity_c10.production.runtime.experiment.load_profile",
        lambda _path, name: (
            {"requested": plan["profiles"]["p1"]["requested"]},
            plan["profiles"]["config_sha256"],
        ),
    )
    monkeypatch.setattr(
        "capacity_c10.production.runtime.preflight", lambda *_args, **_kwargs: receipt
    )

    def transition(*args: object, **_: object) -> dict[str, object]:
        state = args[2]
        events.append(str(state))
        return {
            "profile": "c10-p1",
            "state": state,
            "verified": True,
            "container_snapshot_sha256": "b" * 64,
            "transition_sha256": "c" * 64,
        }

    monkeypatch.setattr("capacity_c10.production.runtime.transition", transition)
    monkeypatch.setattr(
        "capacity_c10.production._validate_claim_inputs",
        lambda _plan, arm, **kwargs: (
            kwargs["private_root"] / f"arm-{arm['index']}",
            kwargs["runtime_receipt_root"] / f"arm-{arm['index']}.json",
            kwargs["approval_root"] / f"arm-{arm['index']}.json",
            kwargs["admission_root"] / f"arm-{arm['index']}.json",
        ),
    )
    with pytest.raises(contracts.C10Error, match="host failure"):
        execute_production_arm(
            repo_root=tmp_path,
            plan=plan,
            cohort=cohort,
            private_root=tmp_path / "private",
            runtime_receipt_root=tmp_path / "receipts",
            approval_root=tmp_path / "approvals",
            admission_root=tmp_path / "admissions",
        )
    assert events == ["lock", "arm", "candidate", "host", "baseline", "release"]
    assert list((tmp_path / ".cre-c10-ledger-v1").glob("*.quarantine"))


@pytest.mark.parametrize(
    "case",
    [
        "missing_approval",
        "stale_approval",
        "invalid_approval",
        "existing_receipt",
        "private_root_file",
        "receipt_root_file",
        "approval_root_file",
        "admission_root_file",
    ],
)
def test_direct_execute_rejects_unsafe_claim_inputs_without_quarantine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    plan, cohort = sealed_jll_plan()
    store = C10SessionStore(
        tmp_path / ".cre-c10-ledger-v1" / f"{plan['plan_sha256']}.json"
    )
    first = store.claim(plan)
    store.record_terminal(first, {})
    private, receipts = tmp_path / "private", tmp_path / "receipts"
    approvals, admissions = tmp_path / "approvals", tmp_path / "admissions"
    roots = {
        "private_root_file": private,
        "receipt_root_file": receipts,
        "approval_root_file": approvals,
        "admission_root_file": admissions,
    }
    for name, root in roots.items():
        if name == case:
            root.write_text("not-a-root", encoding="utf-8")
        else:
            _secure_roots(root)
    if case in {
        "stale_approval",
        "invalid_approval",
        "existing_receipt",
        "admission_root_file",
    }:
        _valid_p1_approval(approvals / "arm-1.json", plan)
    if case in {"stale_approval", "invalid_approval"}:
        approval_path = approvals / "arm-1.json"
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
        approval["created_at" if case == "stale_approval" else "profile"] = (
            "2000-01-01T00:00:00+00:00" if case == "stale_approval" else "other"
        )
        approval_path.write_text(json.dumps(approval), encoding="utf-8")
    if case == "existing_receipt":
        (receipts / "arm-1.json").write_text("blocked", encoding="utf-8")

    events: list[str] = []

    class Lock:
        def __init__(self) -> None:
            self.path, self.retain_on_exit = tmp_path / ".cre.lock", False

        def acquire(self) -> None:
            events.append("lock")

        def arm_benchmark(self, _evidence: object) -> None:
            events.append("arm")

        def release(self) -> None:
            events.append("release")

    monkeypatch.setattr("capacity_c10.production._canonical_lock", lambda _: Lock())
    monkeypatch.setattr(
        "capacity_c10.production.canonical_shared_lock_dir",
        lambda _root: tmp_path / ".cre.lock",
    )
    monkeypatch.setattr(
        "capacity_c10.production.runtime.preflight",
        lambda *_args, **_kwargs: pytest.fail("unsafe input reached runtime preflight"),
    )
    with pytest.raises(contracts.C10Error):
        execute_production_arm(
            repo_root=tmp_path,
            plan=plan,
            cohort=cohort,
            private_root=private,
            runtime_receipt_root=receipts,
            approval_root=approvals,
            admission_root=admissions,
        )
    assert events == ["lock", "release"]
    assert not list((tmp_path / ".cre-c10-ledger-v1").glob("*.quarantine"))
    assert store.next_arm_index(plan) == 1


@pytest.mark.parametrize("artifact", ["approval", "admission"])
def test_direct_p0_rejects_candidate_artifact_without_claim_or_quarantine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, artifact: str
) -> None:
    plan, cohort = sealed_jll_plan()
    private, receipts = tmp_path / "private", tmp_path / "receipts"
    approvals, admissions = tmp_path / "approvals", tmp_path / "admissions"
    _secure_roots(private, receipts, approvals, admissions)
    target = approvals if artifact == "approval" else admissions
    (target / "arm-0.json").write_text("unexpected", encoding="utf-8")

    events: list[str] = []

    class Lock:
        def __init__(self) -> None:
            self.path, self.retain_on_exit = tmp_path / ".cre.lock", False

        def acquire(self) -> None:
            events.append("lock")

        def arm_benchmark(self, _evidence: object) -> None:
            events.append("arm")

        def release(self) -> None:
            events.append("release")

    monkeypatch.setattr("capacity_c10.production._canonical_lock", lambda _: Lock())
    monkeypatch.setattr(
        "capacity_c10.production.canonical_shared_lock_dir",
        lambda _root: tmp_path / ".cre.lock",
    )
    monkeypatch.setattr(
        "capacity_c10.production.runtime.preflight",
        lambda *_args, **_kwargs: pytest.fail("P0 artifact rejection reached runtime"),
    )
    with pytest.raises(contracts.C10Error, match=f"P0 {artifact}"):
        execute_production_arm(
            repo_root=tmp_path,
            plan=plan,
            cohort=cohort,
            private_root=private,
            runtime_receipt_root=receipts,
            approval_root=approvals,
            admission_root=admissions,
        )
    assert events == ["lock", "release"]
    assert not (tmp_path / ".cre-c10-ledger-v1").exists()


def test_cli_execute_cannot_bypass_lock_held_claim_input_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan, cohort = sealed_jll_plan()
    plan_path, cohort_path = tmp_path / "plan.json", tmp_path / "cohort.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    cohort_path.write_text(json.dumps(cohort), encoding="utf-8")
    store = C10SessionStore(
        tmp_path / ".cre-c10-ledger-v1" / f"{plan['plan_sha256']}.json"
    )
    first = store.claim(plan)
    store.record_terminal(first, {})
    private, receipts = tmp_path / "private", tmp_path / "receipts"
    approvals, admissions = tmp_path / "approvals", tmp_path / "admissions"
    _secure_roots(private, receipts, approvals, admissions)

    events: list[str] = []

    class Lock:
        def __init__(self) -> None:
            self.path, self.retain_on_exit = tmp_path / ".cre.lock", False

        def acquire(self) -> None:
            events.append("lock")

        def arm_benchmark(self, _evidence: object) -> None:
            events.append("arm")

        def release(self) -> None:
            events.append("release")

    monkeypatch.setattr("capacity_c10.production._canonical_lock", lambda _: Lock())
    monkeypatch.setattr(
        "capacity_c10.production.canonical_shared_lock_dir",
        lambda _root: tmp_path / ".cre.lock",
    )
    monkeypatch.setattr(
        "capacity_c10.production.runtime.preflight",
        lambda *_args, **_kwargs: pytest.fail("CLI execute reached runtime preflight"),
    )
    with pytest.raises(contracts.C10Error, match="P1 approval"):
        main(
            [
                "--execute",
                "--smoke",
                "--plan",
                str(plan_path),
                "--cohort",
                str(cohort_path),
                "--private-root",
                str(private),
                "--runtime-receipt-root",
                str(receipts),
                "--approval-root",
                str(approvals),
                "--admission-root",
                str(admissions),
                "--repo-root",
                str(tmp_path),
            ]
        )
    assert events == ["lock", "release"]
    assert not list((tmp_path / ".cre-c10-ledger-v1").glob("*.quarantine"))
    assert store.next_arm_index(plan) == 1


def test_racing_runner_claims_the_actual_next_arm_and_derived_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan, cohort = sealed_jll_plan()
    ledger = C10SessionStore(
        tmp_path / ".cre-c10-ledger-v1" / f"{plan['plan_sha256']}.json"
    )
    events: list[str] = []
    advanced = False

    class Lock:
        def __init__(self) -> None:
            self.path, self.retain_on_exit = tmp_path / ".cre.lock", False

        def acquire(self) -> None:
            nonlocal advanced
            events.append("lock")
            if not advanced:
                advanced = True
                rival = ledger.claim(plan)
                ledger.record_terminal(rival, {})
                events.append("rival-p0-terminal")

        def arm_benchmark(self, _evidence: object) -> None:
            events.append("arm")

        def disarm_benchmark(self) -> None:
            events.append("disarm")

        def release(self) -> None:
            events.append("release")

    captured: dict[str, Path] = {}
    receipt = {
        "profile": "c10-p1",
        "config_sha256": plan["profiles"]["config_sha256"],
        "receipt_sha256": "a" * 64,
        "baseline": {"snapshot_sha256": "b" * 64, "transition_sha256": "c" * 64},
    }

    class Host:
        def __init__(self, *, private_root: Path, **_: object) -> None:
            self.lock_path = tmp_path / ".cre.lock"
            captured["private_root"] = private_root

        def execute(self, plan: object, **kwargs: object) -> dict[str, object]:
            events.append("host")
            claim = kwargs["_claim"]
            assert isinstance(claim, dict)
            return {
                "claim": claim,
                "receipt_root": {"path": str(tmp_path / "private"), "id": "f" * 64},
                "evidence_manifest": [
                    {
                        "name": f"browser-evidence-{index}-{'d' * 64}.sealed",
                        "sha256": "d" * 64,
                        "bytes": 1,
                    }
                    for index in range(17)
                ],
                "evidence_manifest_sha256": "e" * 64,
                "evidence_public_key": "public-test-key",
                "evidence_key_id": hashlib.sha256(b"public-test-key").hexdigest(),
                "binding": {
                    "planSha256": plan["plan_sha256"],  # type: ignore[index]
                    "cohortSha256": plan["cohort_sha256"],  # type: ignore[index]
                },
            }

    monkeypatch.setattr("capacity_c10.production._canonical_lock", lambda _: Lock())
    monkeypatch.setattr(
        "capacity_c10.production.canonical_shared_lock_dir",
        lambda _root: tmp_path / ".cre.lock",
    )
    monkeypatch.setattr("capacity_c10.production.C10HostExecutionSession", Host)
    monkeypatch.setattr(
        C10SessionStore,
        "load_terminal",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "capacity_c10.production.runtime.experiment.load_profile",
        lambda _path, _name: (
            {"requested": plan["profiles"]["p1"]["requested"]},
            plan["profiles"]["config_sha256"],
        ),
    )

    def preflight(_profile: str, receipt_path: Path, **_: object) -> dict[str, object]:
        captured["receipt"] = receipt_path
        events.append("preflight")
        return receipt

    def transition(*args: object, **kwargs: object) -> dict[str, object]:
        state = args[2]
        if state == "candidate":
            captured["approval"] = kwargs["approval_path"]  # type: ignore[assignment]
            captured["admission"] = kwargs["admission_out"]  # type: ignore[assignment]
        events.append(str(state))
        return {
            "profile": "c10-p1",
            "state": state,
            "verified": True,
            "container_snapshot_sha256": "b" * 64,
            "transition_sha256": "c" * 64,
        }

    monkeypatch.setattr("capacity_c10.production.runtime.preflight", preflight)
    monkeypatch.setattr("capacity_c10.production.runtime.transition", transition)
    monkeypatch.setattr(
        "capacity_c10.production.runtime.capture_runtime",
        lambda **_: type(
            "Capture", (), {"public": {"settlement": {"state": "idle"}}}
        )(),
    )
    monkeypatch.setattr(
        "capacity_c10.production.runtime.evaluate_state", lambda *_: {"idle": True}
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
    result = execute_production_arm(
        repo_root=tmp_path,
        plan=plan,
        cohort=cohort,
        private_root=tmp_path / "private-base",
        runtime_receipt_root=tmp_path / "receipts",
        approval_root=tmp_path / "approvals",
        admission_root=tmp_path / "admissions",
    )
    assert result["claim"]["arm"]["index"] == 1
    assert captured == {
        "private_root": tmp_path / "private-base" / "arm-1",
        "receipt": tmp_path / "receipts" / "arm-1.json",
        "approval": tmp_path / "approvals" / "arm-1.json",
        "admission": tmp_path / "admissions" / "arm-1.json",
    }
    assert not list((tmp_path / ".cre-c10-ledger-v1").glob("*.quarantine"))
    assert events[:4] == ["lock", "rival-p0-terminal", "arm", "preflight"]
