"""Host-only C10 v3 authority and replay contracts.  No network or Docker."""

from __future__ import annotations

import base64
import copy
import hashlib
import inspect
import json
import time
from pathlib import Path
from typing import Self

import pytest
from capacity_c10 import admission, contracts
from capacity_c10.host_session import (
    C10HostExecutionSession,
    C10SealedCardRegistry,
    C10SessionStore,
    DockerComposeSidecar,
    _OpenSsl,
)
from capacity_c10.production import (
    _canonical_session_store,
    execute_production_arm,
    main,
)
from test_capacity_c10 import _cohort, _plan, _registry, _seal_cohort


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
    cohort = _cohort()
    jll = next(source for source in cohort["sources"] if source["source_key"] == "jll")
    for index, member in enumerate(jll["core"]):
        member["provider_id"] = str(index + 1)
        member["canonical_url"] = (
            f"https://property.jll.com/listings/member-{index + 1}"
        )
    _seal_cohort(cohort)
    plan = admission.admit_plan(cohort, registry=_registry())
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


def test_compose_overlay_is_rendered_before_start_and_owner_env_is_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    environments: list[object] = []

    class Result:
        def __init__(self, code: int, stdout: str = "") -> None:
            self.returncode, self.stdout = code, stdout

    def fake_run(command: list[str], **kwargs: object) -> Result:
        calls.append(command)
        environments.append(kwargs.get("env"))
        if "config" in command:
            return Result(
                0,
                '{"services":{"playwright-service-c10":{"cpus":"2","environment":{"MAX_CONCURRENT_PAGES":"4","C10_PROFILE_SHA256":"a"},"ports":[{"host_ip":"127.0.0.1","published":"4444","target":3004}]}}}',
            )
        return Result(0)

    monkeypatch.setattr("capacity_c10.host_sidecar.subprocess.run", fake_run)
    lifecycle = DockerComposeSidecar(tmp_path)
    lifecycle.start(
        {
            "C10_BROWSER_CPUS": "2",
            "MAX_CONCURRENT_PAGES": "4",
            "C10_PROFILE_SHA256": "a",
        },
        4444,
        time.monotonic() + 10,
    )
    lifecycle.stop(time.monotonic() + 10)

    assert all("docker-compose.yaml" in call for call in calls)
    assert any("config" in call for call in calls)
    assert any("rm" in call for call in calls)
    assert any("ps" in call for call in calls)
    assert environments[0] is environments[1] is environments[2] is environments[3]
    assert lifecycle._env_file is None


def test_signed_lease_intervals_reject_false_capacity_saturation() -> None:
    def evidence(
        start: int, end: int, active: int, capacity: int = 4
    ) -> dict[str, object]:
        return {
            "leaseStartMonotonicNs": str(start),
            "leaseEndMonotonicNs": str(end),
            "observedActivePages": active,
            "configuredCapacity": capacity,
        }

    # Sixteen completed requests are not P0 capacity proof when only three
    # leases overlap, even if a caller claims the configured capacity is four.
    serial = [evidence(index * 10, index * 10 + 9, 3) for index in range(16)]
    with pytest.raises(contracts.C10Error, match="exact P0/P1 target"):
        C10HostExecutionSession._verify_saturation(serial, 4)

    saturated = [evidence(0, 10, 4) for _ in range(4)] + [
        evidence(20 + index * 10, 29 + index * 10, 1) for index in range(12)
    ]
    C10HostExecutionSession._verify_saturation(saturated, 4)


def test_compose_partial_start_uses_same_environment_for_stop_and_quiescence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], object]] = []

    class Result:
        def __init__(self, code: int, stdout: str = "") -> None:
            self.returncode, self.stdout = code, stdout

    def fake_run(command: list[str], **kwargs: object) -> Result:
        calls.append((command, kwargs.get("env")))
        if "config" in command:
            return Result(
                0,
                '{"services":{"playwright-service-c10":{"cpus":"2","environment":{"MAX_CONCURRENT_PAGES":"4","C10_PROFILE_SHA256":"a"},"ports":[{"host_ip":"127.0.0.1","published":"4444","target":3004}]}}}',
            )
        if "up" in command:
            return Result(1)
        return Result(0)

    monkeypatch.setattr("capacity_c10.host_sidecar.subprocess.run", fake_run)
    lifecycle = DockerComposeSidecar(tmp_path)
    with pytest.raises(contracts.C10Error, match="startup failed"):
        lifecycle.start(
            {
                "C10_BROWSER_CPUS": "2",
                "MAX_CONCURRENT_PAGES": "4",
                "C10_PROFILE_SHA256": "a",
            },
            4444,
            time.monotonic() + 10,
        )
    assert any("rm" in command for command, _ in calls)
    assert any("ps" in command for command, _ in calls)
    assert len({id(environment) for _, environment in calls}) == 1
    assert lifecycle._env_file is None


def test_hung_child_is_process_group_killed_at_the_host_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class HungChild:
        pid = 7654
        returncode = None
        stdin = None
        stdout = None
        stderr = None

        def kill(self) -> None:
            raise AssertionError("process-group kill must be preferred")

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "capacity_c10.host_orchestration.subprocess.Popen",
        lambda *_args, **_kwargs: HungChild(),
    )
    monkeypatch.setattr(
        "capacity_c10.host_orchestration.os.killpg",
        lambda pid, signal: killed.append((pid, signal)),
    )
    session = object.__new__(C10HostExecutionSession)
    session.repo_root = tmp_path
    with pytest.raises(contracts.C10Error, match="pipes are unavailable"):
        session._run_child({}, time.monotonic() + 1)
    assert killed == [(7654, 9)]


def test_compose_stop_failure_removes_private_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(
        "capacity_c10.host_sidecar.subprocess.run", lambda *_args, **_kwargs: Result()
    )
    lifecycle = DockerComposeSidecar(tmp_path)
    env_file = tmp_path / "private.env"
    env_file.write_text("x=y\n", encoding="utf-8")
    lifecycle._env_file = env_file
    lifecycle._compose_env = {"C10_BROWSER_PRIVATE_ENV_FILE": str(env_file)}
    with pytest.raises(contracts.C10Error, match="removal was not confirmed"):
        lifecycle.stop(time.monotonic() + 10)
    assert lifecycle._env_file is None
    assert lifecycle._compose_env is None
    assert not env_file.exists()


def test_compose_rejects_a_stopped_container_with_secret_config_residue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Result:
        def __init__(self, stdout: str = "") -> None:
            self.returncode, self.stdout = 0, stdout

    def fake_run(command: list[str], **_: object) -> Result:
        return (
            Result('{"State":"exited","Config":{"Env":["C10_SECRET=x"]}}')
            if "ps" in command
            else Result()
        )

    monkeypatch.setattr("capacity_c10.host_sidecar.subprocess.run", fake_run)
    lifecycle = DockerComposeSidecar(tmp_path)
    env_file = tmp_path / "private.env"
    env_file.write_text("x=y\n", encoding="utf-8")
    lifecycle._env_file = env_file
    lifecycle._compose_env = {"C10_BROWSER_PRIVATE_ENV_FILE": str(env_file)}
    with pytest.raises(contracts.C10Error, match="Config.Env absence"):
        lifecycle.stop(time.monotonic() + 10)
    assert lifecycle._env_file is None
    assert not env_file.exists()


def _sealed_jll_plan() -> tuple[dict[str, object], dict[str, object]]:
    cohort = _cohort()
    jll = next(source for source in cohort["sources"] if source["source_key"] == "jll")
    for index, member in enumerate(jll["core"]):
        member["provider_id"] = str(index + 1)
        member["canonical_url"] = (
            f"https://property.jll.com/listings/member-{index + 1}"
        )
    _seal_cohort(cohort)
    return admission.admit_plan(cohort, registry=_registry()), cohort


def test_registry_rejects_same_cohort_plan_b_before_any_lifecycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan_a, cohort = _sealed_jll_plan()
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
    host = C10HostExecutionSession(
        repo_root=tmp_path,
        session_store=C10SessionStore(tmp_path / "session.json"),
        private_root=tmp_path / "private",
        cards=registry,
        sidecar=sidecar,
    )
    with pytest.raises(contracts.C10Error, match="alternate plan/source projection"):
        host.execute(plan_b, contracts.new_session(plan_b))
    assert sidecar.started is False


@pytest.mark.parametrize(("target", "consumed"), [(4, []), (10, [0])])
def test_host_workflow_issues_signed_17_card_cohort_and_removes_sidecar_before_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: int, consumed: list[int]
) -> None:
    plan, cohort = _sealed_jll_plan()
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
    monkeypatch.setattr("capacity_c10.host_orchestration.SharedLock", FakeLock)
    monkeypatch.setattr(
        "capacity_c10.host_orchestration.PrivateReceiptStore.create",
        lambda _root: FakeStore(),
    )
    sidecar = FakeSidecar()
    host = C10HostExecutionSession(
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
        # The exact P0/P1 target intervals overlap; remaining members complete
        # after that peak. The host must derive, not trust, this proof.
        if sequence == 0:
            start, end, active = 1, 2, 1
        elif sequence <= target:
            start, end, active = 10, 20, target
        else:
            start, end, active = 20 + sequence * 10, 29 + sequence * 10, 1
        unsigned = {
            "protocolVersion": 3,
            "binding": capability["binding"],
            "status": 200,
            "finalUrl": request["url"],
            "redirectCount": 0,
            "elapsedMs": 1,
            "challengeDetected": False,
            "contentType": "application/json",
            "bodyBase64": base64.b64encode(b"{}").decode("ascii"),
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

    session = contracts.new_session(plan)
    session["consumed_arm_indexes"] = consumed
    # Test substitution reaches the host's private child seam only. The public
    # host API has no callback parameter that a production caller could inject.
    monkeypatch.setattr(host, "_run_child", signed_child)
    result = host.execute(plan, session)
    assert len(issued) == 17
    assert issued[0]["card"]["id"] == "jll-enumeration"  # type: ignore[index]
    assert {item["capability"]["cardSequence"] for item in issued} == set(range(17))  # type: ignore[index]
    assert all(
        item["capability"]["expiresAtMs"] <= item["capability"]["hostDeadlineAtMs"]  # type: ignore[index]
        for item in issued
    )
    assert len(result["private_artifacts"]) == 17
    assert sidecar.stopped and lifecycle_events.index(
        "removed"
    ) < lifecycle_events.index("release")


def test_host_cleanup_failure_quarantines_and_never_returns_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan, cohort = _sealed_jll_plan()
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

    monkeypatch.setattr("capacity_c10.host_orchestration.SharedLock", Lock)
    monkeypatch.setattr(
        "capacity_c10.host_orchestration.PrivateReceiptStore.create",
        lambda _root: FakeStore(),
    )
    store = C10SessionStore(tmp_path / "session.json")
    host = C10HostExecutionSession(
        repo_root=tmp_path,
        session_store=store,
        private_root=tmp_path / "private",
        cards=registry,
        sidecar=FailingSidecar(),
    )
    monkeypatch.setattr(host, "_verify_health", lambda *_: None)
    monkeypatch.setattr(host, "_run_cohort", lambda *_: ([{"binding": {}}], []))
    with pytest.raises(contracts.C10Error, match="remove failed"):
        host.execute(plan, contracts.new_session(plan))
    assert (tmp_path / "session.json.quarantine").exists()


def test_production_entrypoint_constructs_host_without_browser_callback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    production_parameters = set(inspect.signature(execute_production_arm).parameters)
    host_parameters = set(inspect.signature(C10HostExecutionSession.execute).parameters)
    assert {"child", "cards", "evidence", "run_browser_arm"}.isdisjoint(
        production_parameters
    )
    assert "child" not in host_parameters


def test_production_cli_is_dry_run_by_default_and_never_calls_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan, cohort = _sealed_jll_plan()
    session = contracts.new_session(plan)
    paths = {
        "plan": tmp_path / "plan.json",
        "cohort": tmp_path / "cohort.json",
        "session": tmp_path / "session.json",
    }
    for key, value in (("plan", plan), ("cohort", cohort), ("session", session)):
        paths[key].write_text(json.dumps(value), encoding="utf-8")
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
                "--session",
                str(paths["session"]),
                "--private-root",
                str(tmp_path / "private"),
                "--runtime-receipt",
                str(tmp_path / "receipt.json"),
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
        "state": "dry_run",
    }


def test_production_claims_before_runtime_or_host_and_terminalizes_authenticated_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan, cohort = _sealed_jll_plan()
    events: list[str] = []

    class Lock:
        def __init__(self) -> None:
            self.path, self.retain_on_exit = tmp_path / ".cre.lock", False

        def acquire(self) -> None:
            events.append("lock")

        def release(self) -> None:
            events.append("release")

    receipt = {
        "profile": "c10-p0",
        "config_sha256": plan["profiles"]["config_sha256"],
        "receipt_sha256": "a" * 64,
        "baseline": {"snapshot_sha256": "b" * 64, "transition_sha256": "c" * 64},
    }

    class Host:
        def __init__(self, **_: object) -> None:
            self.lock_path = tmp_path / ".cre.lock"

        def execute(
            self, plan: object, session: object, **kwargs: object
        ) -> dict[str, object]:
            assert (tmp_path / ".cre-c10-ledger-v1").exists()
            events.append("host")
            claim = kwargs["_claim"]
            assert isinstance(claim, dict)
            return {
                "claim": claim,
                "private_artifacts": [
                    {"name": f"evidence-{index}", "sha256": "d" * 64, "bytes": 1}
                    for index in range(17)
                ],
                "evidence_manifest_sha256": "e" * 64,
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
    result = execute_production_arm(
        repo_root=tmp_path,
        plan=plan,
        cohort=cohort,
        session=contracts.new_session(plan),
        private_root=tmp_path / "private",
        runtime_receipt_path=tmp_path / "receipt.json",
    )
    assert events == ["lock", "preflight", "host", "release"]
    assert result["comparison_state"].startswith("not_comparable")
    assert result["terminal"]["state"] == "terminal"
    terminal = _canonical_session_store(tmp_path, plan, contracts.new_session(plan))
    assert (
        terminal.load_terminal(plan, contracts.new_session(plan))
        == result["authenticated_arm"]
    )
    with pytest.raises(contracts.C10Error, match="already claimed"):
        execute_production_arm(
            repo_root=tmp_path,
            plan=plan,
            cohort=cohort,
            session=contracts.new_session(plan),
            private_root=tmp_path / "private-replay",
            runtime_receipt_path=tmp_path / "receipt-replay.json",
        )


def test_production_rolls_back_and_quarantines_p1_failure_before_lock_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan, cohort = _sealed_jll_plan()
    session = contracts.new_session(plan)
    session["consumed_arm_indexes"] = [0]
    events: list[str] = []

    class Lock:
        def __init__(self) -> None:
            self.path, self.retain_on_exit = tmp_path / ".cre.lock", False

        def acquire(self) -> None:
            events.append("lock")

        def release(self) -> None:
            events.append("release")

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
    with pytest.raises(contracts.C10Error, match="host failure"):
        execute_production_arm(
            repo_root=tmp_path,
            plan=plan,
            cohort=cohort,
            session=session,
            private_root=tmp_path / "private",
            runtime_receipt_path=tmp_path / "receipt.json",
            approval_path=tmp_path / "approval.json",
            admission_out=tmp_path / "admission.json",
        )
    assert events == ["lock", "candidate", "host", "baseline", "release"]
    assert list((tmp_path / ".cre-c10-ledger-v1").glob("*.quarantine"))
