from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from capacity_c10 import (
    adapters,
    admission,
    compare,
    contracts,
    policy,
    runner,
    session_store,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class VerifiedAdapter:
    fully_verified = True

    def __init__(self, key: str) -> None:
        self.key = key
        self.implementation_sha256 = _digest(key)

    def verify_enumeration(self, evidence: Mapping[str, object]) -> None:
        return None

    def verify_member(self, evidence: Mapping[str, object]) -> None:
        return None

    def classify_not_found(self, evidence: Mapping[str, object]) -> bool:
        return False


def _registry() -> dict[str, VerifiedAdapter]:
    return {
        source["key"]: VerifiedAdapter(source["key"])
        for source in policy.load_policy()["sources"]
    }


def _cohort() -> dict[str, object]:
    sources = []
    for source in policy.load_policy()["sources"]:
        members = [{"provider_id": f"{source['key']}-{index}"} for index in range(16)]
        sources.append(
            {
                "source_key": source["key"],
                "plane": source["plane"],
                "core_state": "ready",
                "core_target_rows": 16,
                "core_selected_rows": 16,
                "core": members,
                "fresh_enumeration": {
                    "population_state": "verified",
                    "total_population": 16,
                    "receipt_sha256": _digest(source["key"] + "enumeration"),
                },
            }
        )
    cohort: dict[str, object] = {
        "schema_version": 1,
        "kind": "cre_capacity_multisource_v1_cohort",
        "config_sha256": _digest("multisource-config"),
        "sampling": {"core_per_source": 16},
        "planes": {},
        "aggregate": {"state": "ready_for_review"},
        "sources": sources,
    }
    return _seal_cohort(cohort)


def _seal_cohort(cohort: dict[str, object]) -> dict[str, object]:
    cohort["cohort_sha256"] = contracts.sha256(
        {key: cohort[key] for key in admission.COHORT_HASH_FIELDS}
    )
    return cohort


def _plan() -> dict[str, object]:
    return admission.admit_plan(_cohort(), registry=_registry())


def _reseal_plan(plan: dict[str, object]) -> dict[str, object]:
    plan["plan_sha256"] = contracts.sha256(
        {key: value for key, value in plan.items() if key != "plan_sha256"}
    )
    return plan


def _no_write() -> dict[str, object]:
    return {"no_write": dict(admission.NO_WRITE)}


def _arm(
    plan: Mapping[str, object], index: int, p0_rate: float = 10.0
) -> dict[str, object]:
    variant = contracts.ARM_SEQUENCE[index]
    rate = p0_rate if variant == "p0" else p0_rate * 1.2
    evidence: dict[str, object] = {
        "kind": compare.EVIDENCE_KIND,
        "plan_sha256": plan["plan_sha256"],
        "index": index,
        "variant": variant,
        "runtime": {
            "profile_config_sha256": plan["profiles"]["config_sha256"],  # type: ignore[index]
            "profile_requested_sha256": contracts.sha256(  # type: ignore[index]
                plan["profiles"][variant]["requested"]  # type: ignore[index]
            ),
            "runtime_receipt_sha256": _digest(f"receipt-{index}"),
            "container_snapshot_sha256": _digest(f"container-{index}"),
            "transition_sha256": _digest(f"transition-{index}"),
        },
        "started_monotonic_ns": 1_000_000_000,
        "finished_monotonic_ns": 61_000_000_000,
        "request": {"maxAge": 0, "storeInCache": False},
        "scheduler": {
            "configured_concurrency": plan["profiles"][variant]["requested"][  # type: ignore[index]
                "jll_detail_concurrency"
            ],
            "observed_max_active": plan["profiles"][variant]["requested"][  # type: ignore[index]
                "jll_detail_concurrency"
            ],
            "scheduled_member_count": 16,
        },
        "sources": [
            {
                "key": source["key"],
                "plane": source["plane"],
                "cohort_member_count": source["cohort_member_count"],
                "cohort_member_sha256": source["cohort_member_sha256"],
                "execution_mode": "browser_rendered",
                "engine": "c10-browser-only",
                "client_attempts": 1,
                "engine_attempts": 1,
                "cache_read": False,
                "cache_write": False,
                "started_monotonic_ns": 1_000_000_000 + index * 2_000_000_000,
                "finished_monotonic_ns": 2_000_000_000 + index * 2_000_000_000,
                "qualified_rows": int(rate),
            }
            for index, source in enumerate(plan["sources"])  # type: ignore[index]
        ],
    }
    evidence["evidence_sha256"] = contracts.sha256(evidence)
    return {
        "plan_sha256": plan["plan_sha256"],
        "index": index,
        "variant": variant,
        "terminal": True,
        **_no_write(),
        "sealed_browser_evidence": evidence,
    }


def test_policy_is_hashed_fixed_and_preserves_twenty_source_twelve_eight_floor() -> (
    None
):
    loaded = policy.load_policy()
    assert loaded["policy_sha256"] == contracts.sha256(
        {key: value for key, value in loaded.items() if key != "policy_sha256"}
    )
    assert len(loaded["sources"]) == 20
    assert {source["plane"] for source in loaded["sources"]} == {
        "strict_detail",
        "authoritative_inventory",
    }
    assert sum(source["plane"] == "strict_detail" for source in loaded["sources"]) == 12
    assert (
        sum(
            source["plane"] == "authoritative_inventory" for source in loaded["sources"]
        )
        == 8
    )
    assert loaded["source_workers"] == 1


def test_default_registry_has_no_generic_or_admitting_adapter() -> None:
    loaded = policy.load_policy()
    registry = adapters.default_registry()
    assert set(registry) == {source["key"] for source in loaded["sources"]}
    assert not any(adapter.fully_verified for adapter in registry.values())
    with pytest.raises(contracts.C10Error, match="not fully verified"):
        adapters.verified_registry(loaded, registry)


def test_candidate_registry_exposes_exact_twenty_named_unverified_adapters() -> None:
    loaded = policy.load_policy()
    registry = adapters.candidate_registry()
    assert set(registry) == {source["key"] for source in loaded["sources"]}
    assert all(adapter.fully_verified is False for adapter in registry.values())
    assert registry["jll"].__class__.__name__ == "JllCapacityC10Adapter"
    assert registry["cbre"].__class__.__name__ == "CbreAdapter"
    assert registry["savills"].__class__.__name__ == "SavillsAdapter"
    with pytest.raises(contracts.C10Error, match="not fully verified"):
        adapters.verified_registry(loaded, registry)


def test_admission_binds_exact_cohort_profiles_and_implementation_manifest() -> None:
    plan = _plan()
    contracts.validate_plan(plan)
    assert plan["profiles"]["p0"]["name"] == "c10-p0"
    assert plan["profiles"]["p1"]["name"] == "c10-p1"
    assert plan["arm_sequence"] == list(contracts.ARM_SEQUENCE)
    assert len(plan["sources"]) == 20
    assert plan["no_write"] == admission.NO_WRITE


def test_direct_plan_validation_rejects_self_hashed_policy_bypasses() -> None:
    wrong_policy = json.loads(json.dumps(_plan()))
    wrong_policy["policy_sha256"] = _digest("substituted-policy")
    _reseal_plan(wrong_policy)
    with pytest.raises(contracts.C10Error, match="canonical fixed policy"):
        contracts.validate_plan(wrong_policy)

    wrong_sources = json.loads(json.dumps(_plan()))
    for source in wrong_sources["sources"]:
        source["plane"] = "strict_detail"
    _reseal_plan(wrong_sources)
    with pytest.raises(contracts.C10Error, match="fixed policy"):
        compare.compare(
            wrong_sources,
            [_arm(wrong_sources, index) for index in range(8)],
        )

    wrong_schema = json.loads(json.dumps(_plan()))
    wrong_schema["sources"][0]["unreviewed"] = True
    _reseal_plan(wrong_schema)
    with pytest.raises(contracts.C10Error, match="source schema"):
        contracts.validate_plan(wrong_schema)

    wrong_profile = json.loads(json.dumps(_plan()))
    wrong_profile["profiles"]["p1"]["requested"]["browser_cpus"] = 40
    _reseal_plan(wrong_profile)
    with pytest.raises(contracts.C10Error, match="fixed P0/P1 policy"):
        contracts.validate_plan(wrong_profile)


def test_admission_rejects_partial_cohort_even_with_verified_adapters() -> None:
    cohort = _cohort()
    cohort["sources"] = cohort["sources"][:-1]
    _seal_cohort(cohort)
    with pytest.raises(contracts.C10Error, match="exactly 20"):
        admission.admit_plan(cohort, registry=_registry())


def test_admission_rejects_underfilled_or_wrong_plane_cohort() -> None:
    cohort = _cohort()
    source = cohort["sources"][0]
    source["core"] = source["core"][:-1]
    source["core_selected_rows"] = 15
    source["core_target_rows"] = 15
    _seal_cohort(cohort)
    with pytest.raises(contracts.C10Error, match="incomplete immutable membership"):
        admission.admit_plan(cohort, registry=_registry())


def test_admission_rejects_a_cohort_whose_declared_hash_does_not_bind_membership() -> (
    None
):
    cohort = _cohort()
    cohort["sources"][0]["core"][0]["provider_id"] = "mutated-after-review"
    with pytest.raises(contracts.C10Error, match="digest"):
        admission.admit_plan(cohort, registry=_registry())


def test_c10_profiles_have_exact_p0_p1_whitelist() -> None:
    plan = _plan()
    p0 = plan["profiles"]["p0"]["requested"]
    p1 = plan["profiles"]["p1"]["requested"]
    changed = {key for key in p0 if p0[key] != p1[key]}
    assert changed == admission.P0_P1_MUTABLE_FIELDS
    assert {key: p1[key] for key in changed} == {
        "browser_cpus": 6,
        "global_pages": 10,
        "jll_detail_concurrency": 10,
        "browser_pids": 768,
        "api_cpus": 2,
    }


def test_one_use_arms_are_separate_from_the_immutable_plan() -> None:
    plan = _plan()
    session = contracts.new_session(plan)
    claimed = []
    for _ in contracts.ARM_SEQUENCE:
        next_arm = contracts.claim_next_arm(plan, session)
        session = next_arm["session"]
        claimed.append(next_arm["arm"]["variant"])
    assert tuple(claimed) == contracts.ARM_SEQUENCE
    with pytest.raises(contracts.C10Error, match="already been consumed"):
        contracts.claim_next_arm(plan, session)
    contracts.validate_plan(plan)


def test_durable_claim_is_atomic_terminal_and_never_replays_after_recovery(
    tmp_path: Path,
) -> None:
    plan = _plan()
    initial = runner.initial_session(plan)
    path = tmp_path / "private" / "session.json"
    path.parent.mkdir(mode=0o700)
    with session_store.DurableArmSessionStore(path) as store:
        claimed = store.claim(plan, initial)
        assert claimed["arm"]["index"] == 0
        with pytest.raises(contracts.C10Error, match="unresolved claimed"):
            store.claim(plan, claimed["session"])
        result = {"terminal": True, "arm": claimed["arm"]}
        store.mark_terminal(plan, claimed["arm"], result)
        assert store.session(plan) == claimed["session"]
        persisted = json.loads(path.read_text())
        assert persisted["arms"] == [
            {
                "index": 0,
                "state": "terminal",
                "result": result,
                "result_sha256": contracts.sha256(result),
            }
        ]
        assert store.terminal_results(plan) == [result]
        next_claim = store.claim(plan, claimed["session"])
        assert next_claim["arm"]["index"] == 1

    with session_store.DurableArmSessionStore(path) as recovered:
        durable_session = recovered.session(plan)
        assert durable_session["consumed_arm_indexes"] == [0, 1]
        with pytest.raises(contracts.C10Error, match="unresolved claimed"):
            recovered.claim(plan, durable_session)
        with pytest.raises(contracts.C10Error, match="disagrees"):
            recovered.claim(plan, initial)


def test_new_durable_ledger_rejects_advanced_session_before_claim(
    tmp_path: Path,
) -> None:
    plan = _plan()
    initial = runner.initial_session(plan)
    advanced = contracts.claim_next_arm(plan, initial)["session"]
    path = tmp_path / "private" / "session.json"
    path.parent.mkdir(mode=0o700)

    with session_store.DurableArmSessionStore(path) as store:
        with pytest.raises(contracts.C10Error, match="must start from the empty"):
            store.claim(plan, advanced)

    assert not path.exists()


def test_durable_terminal_result_rejects_tamper_and_oversize(
    tmp_path: Path,
) -> None:
    plan = _plan()
    initial = runner.initial_session(plan)
    path = tmp_path / "private" / "session.json"
    path.parent.mkdir(mode=0o700)
    with session_store.DurableArmSessionStore(path) as store:
        claimed = store.claim(plan, initial)
        with pytest.raises(contracts.C10Error, match="size limit"):
            store.mark_terminal(
                plan,
                claimed["arm"],
                {"terminal": True, "evidence": "x" * (1024 * 1024)},
            )

    persisted = json.loads(path.read_text())
    assert persisted["arms"] == [{"index": 0, "state": "claimed"}]
    persisted["arms"][0] = {
        "index": 0,
        "state": "terminal",
        "result": {"terminal": True},
        "result_sha256": _digest("not-the-result"),
    }
    path.write_text(json.dumps(persisted))
    with session_store.DurableArmSessionStore(path) as recovered:
        with pytest.raises(contracts.C10Error, match="result evidence"):
            recovered.terminal_results(plan)


def test_durable_claim_serializes_concurrent_stale_sessions(tmp_path: Path) -> None:
    plan = _plan()
    initial = runner.initial_session(plan)
    path = tmp_path / "private" / "session.json"
    path.parent.mkdir(mode=0o700)

    def claim() -> int:
        with session_store.DurableArmSessionStore(path) as store:
            return store.claim(plan, initial)["arm"]["index"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim) for _ in range(2)]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except contracts.C10Error as exc:
            outcomes.append(str(exc))
    assert outcomes.count(0) == 1
    assert sum("disagrees" in str(outcome) for outcome in outcomes) == 1, outcomes


def test_durable_claim_rejects_root_replacement(tmp_path: Path) -> None:
    plan = _plan()
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    path = root / "session.json"
    store = session_store.DurableArmSessionStore(path)
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o700)
    moved = tmp_path / "moved"
    os.rename(root, moved)
    os.rename(replacement, root)
    try:
        with pytest.raises(contracts.C10Error, match="root was replaced"):
            store.claim(plan, runner.initial_session(plan))
    finally:
        store.close()


def _raw_browser_arm(
    plan: Mapping[str, object], arm: Mapping[str, object], concurrency: int
) -> dict[str, object]:
    return {
        "started_monotonic_ns": 1_000_000_000,
        "finished_monotonic_ns": 61_000_000_000,
        "request": {"maxAge": 0, "storeInCache": False},
        "scheduler": {
            "configured_concurrency": concurrency,
            "observed_max_active": concurrency,
            "scheduled_member_count": 16,
        },
        "sources": [
            {
                "key": source["key"],
                "plane": source["plane"],
                "cohort_member_count": source["cohort_member_count"],
                "cohort_member_sha256": source["cohort_member_sha256"],
                "execution_mode": "browser_rendered",
                "engine": "c10-browser-only",
                "client_attempts": 1,
                "engine_attempts": 1,
                "cache_read": False,
                "cache_write": False,
                "started_monotonic_ns": 1_000_000_000 + index * 2_000_000_000,
                "finished_monotonic_ns": 2_000_000_000 + index * 2_000_000_000,
                "qualified_rows": source["cohort_member_count"],
            }
            for index, source in enumerate(plan["sources"])  # type: ignore[index]
        ],
    }


def _runtime_receipt(plan: Mapping[str, object], variant: str) -> dict[str, object]:
    return {
        "profile": plan["profiles"][variant]["name"],  # type: ignore[index]
        "config_sha256": plan["profiles"]["config_sha256"],  # type: ignore[index]
        "receipt_sha256": _digest(f"runtime-{variant}"),
        "baseline": {
            "snapshot_sha256": _digest(f"baseline-snapshot-{variant}"),
            "transition_sha256": _digest(f"baseline-transition-{variant}"),
        },
    }


def test_coordinator_holds_one_lock_and_binds_p0_p1_scheduler_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    events: list[str] = []
    monkeypatch.setattr(
        runner, "canonical_shared_lock_dir", lambda: tmp_path / ".cre.lock"
    )

    class FakeLock:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.retain_on_exit = False

        def acquire(self) -> None:
            events.append("acquire")

        def release(self) -> None:
            events.append("release")

    def preflight(profile_name: str, _out: Path, **kwargs: object) -> dict[str, object]:
        assert kwargs == {
            "profile_config": admission.PROFILE_CONFIG,
            "experiment_kind": "C10",
        }
        variant = "p0" if profile_name == "c10-p0" else "p1"
        events.append(f"preflight-{variant}")
        return _runtime_receipt(plan, variant)

    def transition(*args: object, **kwargs: object) -> dict[str, object]:
        state = args[2]
        assert kwargs["_held_shared_lock"].path == tmp_path / ".cre.lock"  # type: ignore[index,union-attr]
        if state == "candidate":
            events.append("candidate")
            return {
                "profile": "c10-p1",
                "state": "candidate",
                "verified": True,
                "container_snapshot_sha256": _digest("candidate-snapshot"),
                "transition_sha256": _digest("candidate-transition"),
            }
        events.append("rollback")
        receipt = _runtime_receipt(plan, "p1")
        return {
            "profile": "c10-p1",
            "state": "baseline",
            "verified": True,
            # Runtime usage/settlement fields are intentionally volatile after
            # a browser arm; only transition_sha256 is the stable state proof.
            "container_snapshot_sha256": _digest("post-workload-snapshot"),
            "transition_sha256": receipt["baseline"]["transition_sha256"],  # type: ignore[index]
        }

    hooks = runner.C10CoordinatorHooks(
        preflight=preflight,
        transition=transition,
        run_browser_arm=lambda arm, concurrency: (
            events.append(f"run-{concurrency}")
            or _raw_browser_arm(plan, arm, concurrency)
        ),
        settle=lambda arm: (
            events.append(f"settle-{arm['variant']}")
            or {"state": "idle", "complete": True}
        ),
        quarantine=lambda reason: events.append(f"quarantine:{reason}"),
        lock_factory=FakeLock,
        canonical_lock_path=lambda: tmp_path / ".cre.lock",
    )
    first = runner.run_one_coordinated_arm(
        plan,
        runner.initial_session(plan),
        paths=runner.C10CoordinatorPaths(receipt_path=tmp_path / "p0.json"),
        hooks=hooks,
    )
    second = runner.run_one_coordinated_arm(
        plan,
        first["session"],
        paths=runner.C10CoordinatorPaths(
            receipt_path=tmp_path / "p1.json",
            approval_path=tmp_path / "approval.json",
            admission_out=tmp_path / "admission.json",
        ),
        hooks=hooks,
    )
    assert first["result"]["sealed_browser_evidence"]["scheduler"] == {
        "configured_concurrency": 4,
        "observed_max_active": 4,
        "scheduled_member_count": 16,
    }
    assert second["result"]["sealed_browser_evidence"]["scheduler"] == {
        "configured_concurrency": 10,
        "observed_max_active": 10,
        "scheduled_member_count": 16,
    }
    ledger_path = runner._canonical_ledger_path(
        tmp_path / ".cre.lock", plan, second["session"]
    )
    with session_store.DurableArmSessionStore(ledger_path) as recovered:
        assert recovered.terminal_results(plan) == [first["result"], second["result"]]
    assert events == [
        "acquire",
        "preflight-p0",
        "run-4",
        "settle-p0",
        "release",
        "acquire",
        "preflight-p1",
        "candidate",
        "run-10",
        "settle-p1",
        "rollback",
        "settle-p1",
        "release",
    ]


def test_coordinator_derives_one_ledger_and_rejects_stale_replay_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    events: list[str] = []
    monkeypatch.setattr(
        runner, "canonical_shared_lock_dir", lambda: tmp_path / ".cre.lock"
    )

    class FakeLock:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.retain_on_exit = False

        def acquire(self) -> None:
            events.append("acquire")

        def release(self) -> None:
            events.append("release")

    lock_path = tmp_path / ".cre.lock"
    hooks = runner.C10CoordinatorHooks(
        preflight=lambda profile, out, **kwargs: (
            events.append("preflight") or _runtime_receipt(plan, "p0")
        ),
        transition=lambda *args, **kwargs: pytest.fail("P0 must not transition"),
        run_browser_arm=lambda arm, concurrency: (
            events.append("browser") or _raw_browser_arm(plan, arm, concurrency)
        ),
        settle=lambda arm: (
            events.append("settle") or {"state": "idle", "complete": True}
        ),
        quarantine=lambda reason: events.append("quarantine"),
        lock_factory=FakeLock,
        canonical_lock_path=lambda: lock_path,
    )
    initial = runner.initial_session(plan)
    paths = runner.C10CoordinatorPaths(receipt_path=tmp_path / "p0.json")
    assert "session_path" not in runner.C10CoordinatorPaths.__dataclass_fields__
    ledger_path = runner._canonical_ledger_path(lock_path, plan, initial)
    assert ledger_path == runner._canonical_ledger_path(lock_path, plan, initial)
    first = runner.run_one_coordinated_arm(plan, initial, paths=paths, hooks=hooks)
    assert ledger_path.is_file()
    assert (
        runner._canonical_ledger_path(lock_path, plan, first["session"]) == ledger_path
    )

    alternate = tmp_path / "alternate" / "session.json"
    alternate.parent.mkdir(mode=0o700)
    with session_store.DurableArmSessionStore(alternate) as store:
        assert store.claim(plan, initial)["arm"]["index"] == 0
    assert alternate != ledger_path
    with pytest.raises(contracts.C10Error, match="disagrees"):
        runner.run_one_coordinated_arm(plan, initial, paths=paths, hooks=hooks)
    assert events == [
        "acquire",
        "preflight",
        "browser",
        "settle",
        "release",
        "acquire",
        "quarantine",
        "release",
    ]


def test_coordinator_rejects_caller_lock_path_before_lock_or_p0_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan()
    canonical = tmp_path / "out" / "daily" / ".cre.lock"
    monkeypatch.setattr(runner, "canonical_shared_lock_dir", lambda: canonical)
    hooks = runner.C10CoordinatorHooks(
        preflight=lambda *args, **kwargs: pytest.fail("must fail before preflight"),
        transition=lambda *args, **kwargs: pytest.fail("must fail before transition"),
        run_browser_arm=lambda *args, **kwargs: pytest.fail("must fail before browser"),
        settle=lambda *args, **kwargs: pytest.fail("must fail before settlement"),
        quarantine=lambda *args, **kwargs: pytest.fail("no lock means no quarantine"),
        lock_factory=lambda path: pytest.fail("must fail before lock construction"),
        canonical_lock_path=lambda: tmp_path / "split" / ".cre.lock",
    )

    with pytest.raises(contracts.C10Error, match="canonical shared CRE lock"):
        runner.run_one_coordinated_arm(
            plan,
            runner.initial_session(plan),
            paths=runner.C10CoordinatorPaths(receipt_path=tmp_path / "p0.json"),
            hooks=hooks,
        )


def test_coordinator_rejects_factory_lock_path_before_acquire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan()
    canonical = tmp_path / "out" / "daily" / ".cre.lock"
    monkeypatch.setattr(runner, "canonical_shared_lock_dir", lambda: canonical)

    class SplitLock:
        path = tmp_path / "split" / ".cre.lock"

        def acquire(self) -> None:
            pytest.fail("mismatched lock must not be acquired")

    hooks = runner.C10CoordinatorHooks(
        preflight=lambda *args, **kwargs: pytest.fail("must fail before preflight"),
        transition=lambda *args, **kwargs: pytest.fail("must fail before transition"),
        run_browser_arm=lambda *args, **kwargs: pytest.fail("must fail before browser"),
        settle=lambda *args, **kwargs: pytest.fail("must fail before settlement"),
        quarantine=lambda *args, **kwargs: pytest.fail("must fail before quarantine"),
        lock_factory=lambda path: SplitLock(),  # type: ignore[arg-type,return-value]
        canonical_lock_path=lambda: canonical,
    )

    with pytest.raises(contracts.C10Error, match="canonical SharedLock"):
        runner.run_one_coordinated_arm(
            plan,
            runner.initial_session(plan),
            paths=runner.C10CoordinatorPaths(receipt_path=tmp_path / "p0.json"),
            hooks=hooks,
        )


def test_coordinator_quarantines_before_releasing_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan()
    events: list[str] = []
    monkeypatch.setattr(
        runner, "canonical_shared_lock_dir", lambda: tmp_path / ".cre.lock"
    )

    class FakeLock:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.retain_on_exit = False

        def acquire(self) -> None:
            events.append("acquire")

        def release(self) -> None:
            events.append("release")

    hooks = runner.C10CoordinatorHooks(
        preflight=lambda profile, out, **kwargs: _runtime_receipt(plan, "p0"),
        transition=lambda *args, **kwargs: pytest.fail("P0 must not transition"),
        run_browser_arm=lambda arm, concurrency: {
            **_raw_browser_arm(plan, arm, concurrency),
            "scheduler": {
                "configured_concurrency": concurrency,
                "observed_max_active": concurrency - 1,
                "scheduled_member_count": 16,
            },
        },
        settle=lambda arm: pytest.fail("invalid evidence must not settle"),
        quarantine=lambda reason: events.append("quarantine"),
        lock_factory=FakeLock,
        canonical_lock_path=lambda: tmp_path / ".cre.lock",
    )
    with pytest.raises(contracts.C10Error, match="planned saturation"):
        runner.run_one_coordinated_arm(
            plan,
            runner.initial_session(plan),
            paths=runner.C10CoordinatorPaths(
                receipt_path=tmp_path / "p0.json",
            ),
            hooks=hooks,
        )
    assert events == ["acquire", "quarantine", "release"]


def test_coordinator_failure_persists_claim_and_blocks_recovery_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    events: list[str] = []
    monkeypatch.setattr(
        runner, "canonical_shared_lock_dir", lambda: tmp_path / ".cre.lock"
    )

    class FakeLock:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.retain_on_exit = False

        def acquire(self) -> None:
            events.append("acquire")

        def release(self) -> None:
            events.append("release")

    hooks = runner.C10CoordinatorHooks(
        preflight=lambda profile, out, **kwargs: (
            events.append("preflight") or _runtime_receipt(plan, "p0")
        ),
        transition=lambda *args, **kwargs: pytest.fail("P0 must not transition"),
        run_browser_arm=lambda arm, concurrency: (
            events.append("browser") or (_ for _ in ()).throw(RuntimeError("crash"))
        ),
        settle=lambda arm: pytest.fail("crashed arm must not settle"),
        quarantine=lambda reason: events.append("quarantine"),
        lock_factory=FakeLock,
        canonical_lock_path=lambda: tmp_path / ".cre.lock",
    )
    paths = runner.C10CoordinatorPaths(receipt_path=tmp_path / "p0.json")
    initial = runner.initial_session(plan)
    with pytest.raises(RuntimeError, match="crash"):
        runner.run_one_coordinated_arm(plan, initial, paths=paths, hooks=hooks)
    ledger_path = runner._canonical_ledger_path(tmp_path / ".cre.lock", plan, initial)
    with session_store.DurableArmSessionStore(ledger_path) as recovered:
        durable = recovered.session(plan)
        assert durable["consumed_arm_indexes"] == [0]
    with pytest.raises(contracts.C10Error, match="unresolved claimed"):
        runner.run_one_coordinated_arm(plan, durable, paths=paths, hooks=hooks)
    assert events == [
        "acquire",
        "preflight",
        "browser",
        "quarantine",
        "release",
        "acquire",
        "quarantine",
        "release",
    ]


def _raw_browser_arm(
    plan: Mapping[str, object], arm: Mapping[str, object], concurrency: int
) -> dict[str, object]:
    return {
        "started_monotonic_ns": 1_000_000_000,
        "finished_monotonic_ns": 61_000_000_000,
        "request": {"maxAge": 0, "storeInCache": False},
        "scheduler": {
            "configured_concurrency": concurrency,
            "observed_max_active": concurrency,
            "scheduled_member_count": 16,
        },
        "sources": [
            {
                "key": source["key"],
                "plane": source["plane"],
                "execution_mode": "browser_rendered",
                "engine": "c10-browser-only",
                "client_attempts": 1,
                "engine_attempts": 1,
                "cache_read": False,
                "cache_write": False,
                "qualified_rows": 100,
            }
            for source in plan["sources"]  # type: ignore[index]
        ],
    }


def _runtime_receipt(plan: Mapping[str, object], variant: str) -> dict[str, object]:
    return {
        "profile": plan["profiles"][variant]["name"],  # type: ignore[index]
        "config_sha256": plan["profiles"]["config_sha256"],  # type: ignore[index]
        "receipt_sha256": _digest(f"runtime-{variant}"),
        "baseline": {
            "snapshot_sha256": _digest(f"baseline-snapshot-{variant}"),
            "transition_sha256": _digest(f"baseline-transition-{variant}"),
        },
    }


def test_coordinator_holds_one_lock_and_binds_p0_p1_scheduler_evidence(
    tmp_path: Path,
) -> None:
    plan = _plan()
    events: list[str] = []

    class FakeLock:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.retain_on_exit = False

        def acquire(self) -> None:
            events.append("acquire")

        def release(self) -> None:
            events.append("release")

    def preflight(profile_name: str, _out: Path, **kwargs: object) -> dict[str, object]:
        assert kwargs == {
            "profile_config": admission.PROFILE_CONFIG,
            "experiment_kind": "C10",
        }
        variant = "p0" if profile_name == "c10-p0" else "p1"
        events.append(f"preflight-{variant}")
        return _runtime_receipt(plan, variant)

    def transition(*args: object, **kwargs: object) -> dict[str, object]:
        state = args[2]
        assert kwargs["_held_shared_lock"].path == tmp_path / ".cre.lock"  # type: ignore[index,union-attr]
        if state == "candidate":
            events.append("candidate")
            return {
                "profile": "c10-p1",
                "state": "candidate",
                "verified": True,
                "container_snapshot_sha256": _digest("candidate-snapshot"),
                "transition_sha256": _digest("candidate-transition"),
            }
        events.append("rollback")
        receipt = _runtime_receipt(plan, "p1")
        return {
            "profile": "c10-p1",
            "state": "baseline",
            "verified": True,
            "container_snapshot_sha256": receipt["baseline"]["snapshot_sha256"],  # type: ignore[index]
            "transition_sha256": receipt["baseline"]["transition_sha256"],  # type: ignore[index]
        }

    hooks = runner.C10CoordinatorHooks(
        preflight=preflight,
        transition=transition,
        run_browser_arm=lambda arm, concurrency: (
            events.append(f"run-{concurrency}")
            or _raw_browser_arm(plan, arm, concurrency)
        ),
        settle=lambda arm: (
            events.append(f"settle-{arm['variant']}")
            or {"state": "idle", "complete": True}
        ),
        quarantine=lambda reason: events.append(f"quarantine:{reason}"),
        lock_factory=FakeLock,
        canonical_lock_path=lambda: tmp_path / ".cre.lock",
    )
    first = runner.run_one_coordinated_arm(
        plan,
        runner.initial_session(plan),
        paths=runner.C10CoordinatorPaths(receipt_path=tmp_path / "p0.json"),
        hooks=hooks,
    )
    second = runner.run_one_coordinated_arm(
        plan,
        first["session"],
        paths=runner.C10CoordinatorPaths(
            receipt_path=tmp_path / "p1.json",
            approval_path=tmp_path / "approval.json",
            admission_out=tmp_path / "admission.json",
        ),
        hooks=hooks,
    )
    assert first["result"]["sealed_browser_evidence"]["scheduler"] == {
        "configured_concurrency": 4,
        "observed_max_active": 4,
        "scheduled_member_count": 16,
    }
    assert second["result"]["sealed_browser_evidence"]["scheduler"] == {
        "configured_concurrency": 10,
        "observed_max_active": 10,
        "scheduled_member_count": 16,
    }
    assert events == [
        "acquire",
        "preflight-p0",
        "run-4",
        "settle-p0",
        "release",
        "acquire",
        "preflight-p1",
        "candidate",
        "run-10",
        "settle-p1",
        "rollback",
        "settle-p1",
        "release",
    ]


def test_coordinator_quarantines_before_releasing_lock(tmp_path: Path) -> None:
    plan = _plan()
    events: list[str] = []

    class FakeLock:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.retain_on_exit = False

        def acquire(self) -> None:
            events.append("acquire")

        def release(self) -> None:
            events.append("release")

    hooks = runner.C10CoordinatorHooks(
        preflight=lambda profile, out, **kwargs: _runtime_receipt(plan, "p0"),
        transition=lambda *args, **kwargs: pytest.fail("P0 must not transition"),
        run_browser_arm=lambda arm, concurrency: {
            **_raw_browser_arm(plan, arm, concurrency),
            "scheduler": {
                "configured_concurrency": concurrency,
                "observed_max_active": concurrency - 1,
                "scheduled_member_count": 16,
            },
        },
        settle=lambda arm: pytest.fail("invalid evidence must not settle"),
        quarantine=lambda reason: events.append("quarantine"),
        lock_factory=FakeLock,
        canonical_lock_path=lambda: tmp_path / ".cre.lock",
    )
    with pytest.raises(contracts.C10Error, match="planned saturation"):
        runner.run_one_coordinated_arm(
            plan,
            runner.initial_session(plan),
            paths=runner.C10CoordinatorPaths(receipt_path=tmp_path / "p0.json"),
            hooks=hooks,
        )
    assert events == ["acquire", "quarantine", "release"]


def test_comparator_is_plane_separated_no_write_and_never_executable_adoption() -> None:
    plan = _plan()
    result = compare.compare(plan, [_arm(plan, index) for index in range(8)])
    assert result["state"] == "candidate_for_operator_review"
    assert result["adoptable"] is False
    assert result["cross_plane_aggregation"] == "not_computed_distinct_plane_estimands"
    assert set(result["planes"]) == {"strict_detail", "authoritative_inventory"}
    assert all(
        item["median_equal_source_gain_percent"] == 20.0
        for item in result["planes"].values()
    )


def test_comparator_rejects_missing_source_or_write_claim() -> None:
    plan = _plan()
    arms = [_arm(plan, index) for index in range(8)]
    evidence = arms[0]["sealed_browser_evidence"]
    evidence["sources"] = evidence["sources"][:-1]
    evidence["evidence_sha256"] = contracts.sha256(
        {key: value for key, value in evidence.items() if key != "evidence_sha256"}
    )
    with pytest.raises(contracts.C10Error, match="exactly match"):
        compare.compare(plan, arms)
    arms = [_arm(plan, index) for index in range(8)]
    arms[0]["no_write"]["cache_writes"] = 1
    with pytest.raises(contracts.C10Error, match="no-write"):
        compare.compare(plan, arms)
    arms = [_arm(plan, index) for index in range(8)]
    evidence = arms[0]["sealed_browser_evidence"]
    evidence["sources"][-1]["key"] = evidence["sources"][0]["key"]
    evidence["evidence_sha256"] = contracts.sha256(
        {key: value for key, value in evidence.items() if key != "evidence_sha256"}
    )
    with pytest.raises(contracts.C10Error, match="exactly match"):
        compare.compare(plan, arms)


def test_comparator_binds_source_cohort_and_serial_source_timing() -> None:
    plan = _plan()
    arms = [_arm(plan, index) for index in range(8)]
    evidence = arms[0]["sealed_browser_evidence"]
    evidence["sources"][0]["cohort_member_sha256"] = _digest("other-cohort")
    evidence["evidence_sha256"] = contracts.sha256(
        {key: value for key, value in evidence.items() if key != "evidence_sha256"}
    )
    with pytest.raises(contracts.C10Error, match="browser-rendered"):
        compare.compare(plan, arms)

    arms = [_arm(plan, index) for index in range(8)]
    evidence = arms[0]["sealed_browser_evidence"]
    evidence["sources"][1]["started_monotonic_ns"] = evidence["sources"][0][
        "started_monotonic_ns"
    ]
    evidence["evidence_sha256"] = contracts.sha256(
        {key: value for key, value in evidence.items() if key != "evidence_sha256"}
    )
    with pytest.raises(contracts.C10Error, match="serial browser arm"):
        compare.compare(plan, arms)


def test_comparator_bounds_qualified_rows_and_treats_zero_as_not_adoptable() -> None:
    plan = _plan()
    arms = [_arm(plan, index) for index in range(8)]
    evidence = arms[0]["sealed_browser_evidence"]
    evidence["sources"][0]["qualified_rows"] = (
        evidence["sources"][0]["cohort_member_count"] + 1
    )
    evidence["evidence_sha256"] = contracts.sha256(
        {key: value for key, value in evidence.items() if key != "evidence_sha256"}
    )
    with pytest.raises(contracts.C10Error, match="within the immutable cohort"):
        compare.compare(plan, arms)

    arms = [_arm(plan, index) for index in range(8)]
    evidence = arms[1]["sealed_browser_evidence"]
    evidence["sources"][0]["qualified_rows"] = 0
    evidence["evidence_sha256"] = contracts.sha256(
        {key: value for key, value in evidence.items() if key != "evidence_sha256"}
    )
    result = compare.compare(plan, arms)
    assert result["state"] == "measured_not_adoptable"
    assert result["adoptable"] is False


def test_comparator_derives_each_source_rate_from_its_own_interval() -> None:
    plan = _plan()
    p0 = _arm(plan, 0)
    p1 = _arm(plan, 1)
    p1_evidence = p1["sealed_browser_evidence"]
    p1_evidence["sources"][0]["finished_monotonic_ns"] = 3_000_000_000
    p1_evidence["sources"][1]["started_monotonic_ns"] = 3_000_000_000
    p1_evidence["sources"][1]["finished_monotonic_ns"] = 4_000_000_000
    p1_evidence["evidence_sha256"] = contracts.sha256(
        {key: value for key, value in p1_evidence.items() if key != "evidence_sha256"}
    )
    p0_rates = compare._browser_evidence_rates(plan, p0, 0)
    p1_rates = compare._browser_evidence_rates(plan, p1, 1)
    key = plan["sources"][0]["key"]
    assert round((p1_rates[key] / p0_rates[key] - 1.0) * 100, 3) == -40.0


def test_comparator_rejects_direct_or_unsaturated_browser_evidence() -> None:
    plan = _plan()
    arms = [_arm(plan, index) for index in range(8)]
    evidence = arms[0]["sealed_browser_evidence"]
    evidence["sources"][0]["execution_mode"] = "direct_native"
    evidence["evidence_sha256"] = contracts.sha256(
        {key: value for key, value in evidence.items() if key != "evidence_sha256"}
    )
    with pytest.raises(contracts.C10Error, match="browser-rendered"):
        compare.compare(plan, arms)
    arms = [_arm(plan, index) for index in range(8)]
    evidence = arms[0]["sealed_browser_evidence"]
    evidence["scheduler"]["observed_max_active"] = 3
    evidence["evidence_sha256"] = contracts.sha256(
        {key: value for key, value in evidence.items() if key != "evidence_sha256"}
    )
    with pytest.raises(contracts.C10Error, match="planned saturation"):
        compare.compare(plan, arms)


def test_comparator_rejects_direct_or_unsaturated_browser_evidence() -> None:
    plan = _plan()
    arms = [_arm(plan, index) for index in range(8)]
    evidence = arms[0]["sealed_browser_evidence"]
    evidence["sources"][0]["execution_mode"] = "direct_native"
    evidence["evidence_sha256"] = contracts.sha256(
        {key: value for key, value in evidence.items() if key != "evidence_sha256"}
    )
    with pytest.raises(contracts.C10Error, match="browser-rendered"):
        compare.compare(plan, arms)
    arms = [_arm(plan, index) for index in range(8)]
    evidence = arms[0]["sealed_browser_evidence"]
    evidence["scheduler"]["observed_max_active"] = 3
    evidence["evidence_sha256"] = contracts.sha256(
        {key: value for key, value in evidence.items() if key != "evidence_sha256"}
    )
    with pytest.raises(contracts.C10Error, match="planned saturation"):
        compare.compare(plan, arms)


def test_policy_loader_rejects_duplicate_source_or_nonserial_worker(
    tmp_path: Path,
) -> None:
    document = json.loads(policy.DEFAULT_POLICY.read_text())
    document["sources"][1]["key"] = document["sources"][0]["key"]
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(document))
    with pytest.raises(contracts.C10Error, match="unique"):
        policy.load_policy(path)
    document = json.loads(policy.DEFAULT_POLICY.read_text())
    document["source_workers"] = 2
    path.write_text(json.dumps(document))
    with pytest.raises(contracts.C10Error, match="serial"):
        policy.load_policy(path)
