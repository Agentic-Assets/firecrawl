"""Contracts for the JLL admission bridge in ``capacity_c10.production``.

Covers ``execute_jll_admission_collection``: timeout/adapter/binding
validation ordering before the canonical lock, lock/arm/start/health/action/
stop/disarm/release sequencing, deadline handoff from health to the
controller action, teardown-failure quarantine, and one end-to-end pass
through the real ``DockerComposeSidecar`` with ``subprocess.run`` faked out.

Nothing here touches Docker, a provider, or the real repository canonical
lock; every lock is a monkeypatched fake and every sidecar except the P1-a
end-to-end test is a fake too.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from capacity_c10 import contracts, production
from capacity_c10.contracts import C10Error, sha256
from capacity_c10.host_sidecar import C10EphemeralKeys


def _receipt_root(tmp_path: Path, name: str = "receipts") -> Path:
    root = tmp_path / name
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def _binding() -> dict[str, str]:
    return {
        name: letter * 64
        for name, letter in zip(
            (
                "planSha256",
                "cohortSha256",
                "policySha256",
                "sourceSha256",
                "armSha256",
                "implementationSha256",
            ),
            "abcdef",
            strict=True,
        )
    }


def _keys() -> C10EphemeralKeys:
    return C10EphemeralKeys(
        "coordinator-private",
        "coordinator-public",
        "sidecar-private",
        "sidecar-public",
        "transport-key",
    )


class _FakeLock:
    """Records lifecycle calls; raises on demand from ``acquire``/``arm_benchmark``."""

    def __init__(
        self,
        events: list[str],
        *,
        acquire_error: BaseException | None = None,
        arm_error: BaseException | None = None,
    ) -> None:
        self.events = events
        self.retain_on_exit = False
        self.path = Path("/tmp/fake-c10-lock")
        self._acquire_error = acquire_error
        self._arm_error = arm_error

    def acquire(self) -> None:
        if self._acquire_error is not None:
            raise self._acquire_error
        self.events.append("acquire")

    def release(self) -> None:
        self.events.append("release")

    def arm_benchmark(self, evidence: dict[str, Any]) -> None:
        if self._arm_error is not None:
            raise self._arm_error
        self.events.append("arm")

    def disarm_benchmark(self) -> None:
        self.events.append("disarm")


def _make_sidecar_cls(
    events: list[str], *, stop_error: BaseException | None = None
) -> type:
    class _FakeSidecar:
        def __init__(self, repo_root: Path) -> None:
            self.repo_root = repo_root
            self.compose_project = "c10-fake-project"
            events.append("sidecar_init")

        def start(
            self, environment: dict[str, str], port: int, deadline: float
        ) -> None:
            events.append("start")

        def stop(self) -> None:
            events.append("stop")
            if stop_error is not None:
                raise stop_error

    return _FakeSidecar


def _wire_common(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    events: list[str],
    *,
    lock: Any,
    sidecar_cls: type,
    action: Any,
    adapter_digest: str = "d" * 64,
    health_deadlines: list[float] | None = None,
) -> None:
    monkeypatch.setattr(production, "_canonical_lock", lambda _repo_root: lock)
    monkeypatch.setattr(production, "DockerComposeSidecar", sidecar_cls)
    monkeypatch.setattr(
        production, "repository_implementation_sha256", lambda _key: adapter_digest
    )
    monkeypatch.setattr(
        production, "_c10_ledger_root", lambda _repo_root: tmp_path / "ledger"
    )
    monkeypatch.setattr(
        production._C10HostTransport, "_keys", lambda self, deadline: _keys()
    )
    monkeypatch.setattr(
        production._C10HostTransport,
        "_free_loopback_port",
        staticmethod(lambda: 38111),
    )

    def fake_verify_health(
        self: Any,
        endpoint: str,
        keys: C10EphemeralKeys,
        profile: dict[str, Any],
        deadline: float,
        *,
        admission_lane: str | None = None,
    ) -> None:
        events.append("health")
        if health_deadlines is not None:
            health_deadlines.append(deadline)

    monkeypatch.setattr(
        production._C10HostTransport, "_verify_health", fake_verify_health
    )
    monkeypatch.setattr(production, "_execute_authorized_jll_admission_action", action)


def _valid_kwargs(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "repo_root": tmp_path,
        "receipt_root": overrides.pop("receipt_root", None) or _receipt_root(tmp_path),
        "binding": _binding(),
        "adapter_implementation_sha256": "d" * 64,
    }
    kwargs.update(overrides)
    return kwargs


# --- 1. stale adapter digest -------------------------------------------------


def test_stale_adapter_digest_rejected_before_lock_or_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []

    def action(**_: Any) -> dict[str, Any]:
        raise AssertionError("action must not run")

    lock = _FakeLock(events)
    sidecar_cls = _make_sidecar_cls(events)
    _wire_common(
        monkeypatch,
        tmp_path,
        events,
        lock=lock,
        sidecar_cls=sidecar_cls,
        action=action,
        adapter_digest="c" * 64,  # differs from the supplied "d" * 64
    )
    with pytest.raises(C10Error, match="does not match the repository implementation"):
        production.execute_jll_admission_collection(**_valid_kwargs(tmp_path))
    assert events == []


# --- 2. timeout validation ---------------------------------------------------

_STARTUP_MAX = production.JLL_ADMISSION_STARTUP_MAX_SECONDS
_COLLECTION_MAX = production.JLL_ADMISSION_COLLECTION_MAX_SECONDS


@pytest.mark.parametrize(
    "startup,collection",
    [
        (_STARTUP_MAX + 1, _COLLECTION_MAX),
        (_STARTUP_MAX, _COLLECTION_MAX + 1),
        (0, _COLLECTION_MAX),
        (True, _COLLECTION_MAX),
        ("10", _COLLECTION_MAX),
        (_STARTUP_MAX, 0),
        (_STARTUP_MAX, True),
        (_STARTUP_MAX, "10"),
    ],
)
def test_invalid_timeouts_rejected_before_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    startup: object,
    collection: object,
) -> None:
    events: list[str] = []

    def action(**_: Any) -> dict[str, Any]:
        raise AssertionError("action must not run")

    lock = _FakeLock(events)
    sidecar_cls = _make_sidecar_cls(events)
    _wire_common(
        monkeypatch, tmp_path, events, lock=lock, sidecar_cls=sidecar_cls, action=action
    )
    with pytest.raises(C10Error, match="timeout is outside its reviewed bound"):
        production.execute_jll_admission_collection(
            **_valid_kwargs(
                tmp_path,
                startup_timeout_seconds=startup,
                collection_timeout_seconds=collection,
            )
        )
    assert events == []


def test_boundary_timeouts_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []

    def action(**_: Any) -> dict[str, Any]:
        events.append("action")
        return {"ok": True}

    lock = _FakeLock(events)
    sidecar_cls = _make_sidecar_cls(events)
    _wire_common(
        monkeypatch, tmp_path, events, lock=lock, sidecar_cls=sidecar_cls, action=action
    )
    result = production.execute_jll_admission_collection(
        **_valid_kwargs(
            tmp_path,
            startup_timeout_seconds=_STARTUP_MAX,
            collection_timeout_seconds=_COLLECTION_MAX,
        )
    )
    assert result == {"ok": True}
    assert "acquire" in events and "disarm" in events and "release" in events


# --- 3. lock-held exception --------------------------------------------------


def test_lock_held_exception_prevents_any_sidecar_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []

    class _LockHeldError(Exception):
        pass

    def action(**_: Any) -> dict[str, Any]:
        raise AssertionError("action must not run")

    lock = _FakeLock(events, acquire_error=_LockHeldError("lock is held elsewhere"))
    sidecar_cls = _make_sidecar_cls(events)
    _wire_common(
        monkeypatch, tmp_path, events, lock=lock, sidecar_cls=sidecar_cls, action=action
    )
    with pytest.raises(_LockHeldError):
        production.execute_jll_admission_collection(**_valid_kwargs(tmp_path))
    # acquire() raised before appending "acquire"; nothing after it ran, and
    # release() (in the finally *inside* the try that acquire() precedes) was
    # never reached either.
    assert events == []


# --- 4. full ordering + deadline handoff ------------------------------------


def test_ordering_and_health_to_action_deadline_handoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    health_deadlines: list[float] = []
    action_deadlines: list[float] = []

    def action(**kwargs: Any) -> dict[str, Any]:
        events.append("action")
        action_deadlines.append(kwargs["deadline"])
        return {"manifest": "fake"}

    lock = _FakeLock(events)
    sidecar_cls = _make_sidecar_cls(events)
    _wire_common(
        monkeypatch,
        tmp_path,
        events,
        lock=lock,
        sidecar_cls=sidecar_cls,
        action=action,
        health_deadlines=health_deadlines,
    )
    result = production.execute_jll_admission_collection(
        **_valid_kwargs(
            tmp_path, startup_timeout_seconds=1, collection_timeout_seconds=500
        )
    )
    assert result == {"manifest": "fake"}
    assert events == [
        "acquire",
        "arm",
        "sidecar_init",
        "start",
        "health",
        "action",
        "stop",
        "disarm",
        "release",
    ]
    assert len(health_deadlines) == 1 and len(action_deadlines) == 1
    # Collection deadline is computed strictly after health succeeds, using a
    # much larger budget (500s) than startup (1s), so it must exceed it.
    assert action_deadlines[0] > health_deadlines[0]


# --- 5. controller timeout: teardown still proven ---------------------------


def test_controller_timeout_still_tears_down_and_disarms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []

    def action(**_: Any) -> dict[str, Any]:
        events.append("action")
        raise C10Error("C10 production deadline expired")

    lock = _FakeLock(events)
    sidecar_cls = _make_sidecar_cls(events)
    _wire_common(
        monkeypatch, tmp_path, events, lock=lock, sidecar_cls=sidecar_cls, action=action
    )
    with pytest.raises(C10Error, match="deadline expired"):
        production.execute_jll_admission_collection(**_valid_kwargs(tmp_path))
    assert events == [
        "acquire",
        "arm",
        "sidecar_init",
        "start",
        "health",
        "action",
        "stop",
        "disarm",
        "release",
    ]
    assert lock.retain_on_exit is False


# --- 6. P1-a: real DockerComposeSidecar, subprocess.run faked ---------------


def test_p1a_real_sidecar_teardown_runs_after_controller_deadline_expires(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    calls: list[dict[str, Any]] = []
    action_started_at: list[float] = []

    requested = {"global_pages": 1, "browser_cpus": 2, "browser_pids": 384}
    expected_profile_sha256 = sha256(requested)

    def fake_run(
        cmd: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        capture_output: bool,
        timeout: float,
        text: bool = False,
        check: bool = False,
    ) -> subprocess.CompletedProcess[Any]:
        calls.append({"cmd": list(cmd), "time": time.monotonic()})
        if "config" in cmd:
            port = env["C10_BROWSER_HOST_PORT"]
            payload = {
                "services": {
                    "playwright-service-c10": {
                        "image": "firecrawl-playwright-service-c10:local",
                        "cpus": env["C10_BROWSER_CPUS"],
                        "ports": [
                            {
                                "host_ip": "127.0.0.1",
                                "published": port,
                                "target": 3004,
                            }
                        ],
                        "environment": {
                            "MAX_CONCURRENT_PAGES": env["MAX_CONCURRENT_PAGES"],
                            "C10_PROFILE_SHA256": env["C10_PROFILE_SHA256"],
                            "C10_ADMISSION_LANE": env.get("C10_ADMISSION_LANE", ""),
                        },
                    }
                }
            }
            assert env["MAX_CONCURRENT_PAGES"] == "1"
            assert env["C10_PROFILE_SHA256"] == expected_profile_sha256
            assert env["C10_ADMISSION_LANE"] == "jll-canonical-url-lexicographic-v1"
            assert env["C10_BROWSER_CPUS"] == "2"
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        if "up" in cmd:
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        if "rm" in cmd:
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        if "ps" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected compose command: {cmd}")

    monkeypatch.setattr("capacity_c10.host_sidecar.subprocess.run", fake_run)

    def action(**_: Any) -> dict[str, Any]:
        events.append("action")
        action_started_at.append(time.monotonic())
        time.sleep(0.2)
        raise C10Error("C10 production deadline expired")

    lock = _FakeLock(events)
    monkeypatch.setattr(production, "_canonical_lock", lambda _repo_root: lock)
    monkeypatch.setattr(
        production, "repository_implementation_sha256", lambda _key: "d" * 64
    )
    monkeypatch.setattr(
        production, "_c10_ledger_root", lambda _repo_root: tmp_path / "ledger"
    )
    monkeypatch.setattr(
        production._C10HostTransport, "_keys", lambda self, deadline: _keys()
    )

    def fake_verify_health(self: Any, *args: Any, **kwargs: Any) -> None:
        events.append("health")

    monkeypatch.setattr(
        production._C10HostTransport, "_verify_health", fake_verify_health
    )
    monkeypatch.setattr(production, "_execute_authorized_jll_admission_action", action)

    with pytest.raises(C10Error, match="deadline expired"):
        production.execute_jll_admission_collection(
            **_valid_kwargs(
                tmp_path, startup_timeout_seconds=30, collection_timeout_seconds=0.05
            )
        )

    # The real DockerComposeSidecar does not push into `events`; only the
    # fake lock does, and it has no separate "stop" step from the caller's
    # perspective. Compose call order (rm/ps after the deadline) is asserted
    # separately via `calls`.
    assert events == ["acquire", "arm", "health", "action", "disarm", "release"]
    up_calls = [call for call in calls if "up" in call["cmd"]]
    config_calls = [call for call in calls if "config" in call["cmd"]]
    assert config_calls and up_calls
    rm_calls = [call for call in calls if "rm" in call["cmd"]]
    ps_calls = [call for call in calls if "ps" in call["cmd"]]
    assert rm_calls and ps_calls
    action_start = action_started_at[0]
    # The action slept 0.2s before raising; teardown's rm/ps must land after
    # that sleep completed (i.e. strictly after the collection deadline the
    # action was racing against), never in parallel with or before it.
    assert rm_calls[0]["time"] - action_start >= 0.15
    assert ps_calls[0]["time"] - action_start >= 0.15
    assert lock.retain_on_exit is False


# --- 7. teardown failure after controller failure ---------------------------


def test_teardown_failure_after_controller_failure_quarantines_and_reraises_original(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    receipt_root = _receipt_root(tmp_path)

    def action(**_: Any) -> dict[str, Any]:
        events.append("action")
        raise C10Error("controller collection failed")

    lock = _FakeLock(events)
    sidecar_cls = _make_sidecar_cls(
        events, stop_error=C10Error("sidecar removal was not confirmed")
    )
    _wire_common(
        monkeypatch, tmp_path, events, lock=lock, sidecar_cls=sidecar_cls, action=action
    )
    with pytest.raises(C10Error, match="controller collection failed") as excinfo:
        production.execute_jll_admission_collection(
            **_valid_kwargs(tmp_path, receipt_root=receipt_root)
        )
    assert events == [
        "acquire",
        "arm",
        "sidecar_init",
        "start",
        "health",
        "action",
        "stop",
        "release",
    ]
    assert "disarm" not in events
    assert lock.retain_on_exit is True
    notes = getattr(excinfo.value, "__notes__", [])
    assert any("teardown also failed" in note for note in notes)

    ledger_dir = tmp_path / "ledger"
    quarantine_files = list(ledger_dir.glob("jll-admission-*.quarantine"))
    assert len(quarantine_files) == 1
    quarantine_file = quarantine_files[0]
    mode = quarantine_file.stat().st_mode & 0o777
    assert mode == 0o600
    record = json.loads(quarantine_file.read_text())
    assert record["state"] == "quarantined"
    assert record["compose_project"] == "c10-fake-project"

    receipt_quarantine = receipt_root / "jll-admission-quarantine.json"
    assert receipt_quarantine.is_file()
    receipt_record = json.loads(receipt_quarantine.read_text())
    assert receipt_record["state"] == "quarantined"
    assert receipt_record["compose_project"] == "c10-fake-project"


# --- 8. teardown failure after otherwise-successful action -----------------


def test_teardown_failure_after_success_raises_teardown_not_proven(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    receipt_root = _receipt_root(tmp_path)

    def action(**_: Any) -> dict[str, Any]:
        events.append("action")
        return {"manifest": "fake"}

    lock = _FakeLock(events)
    sidecar_cls = _make_sidecar_cls(
        events, stop_error=C10Error("sidecar removal was not confirmed")
    )
    _wire_common(
        monkeypatch, tmp_path, events, lock=lock, sidecar_cls=sidecar_cls, action=action
    )
    with pytest.raises(C10Error, match="teardown was not proven"):
        production.execute_jll_admission_collection(
            **_valid_kwargs(tmp_path, receipt_root=receipt_root)
        )
    assert "disarm" not in events
    assert lock.retain_on_exit is True

    ledger_dir = tmp_path / "ledger"
    quarantine_files = list(ledger_dir.glob("jll-admission-*.quarantine"))
    assert len(quarantine_files) == 1
    receipt_quarantine = receipt_root / "jll-admission-quarantine.json"
    assert receipt_quarantine.is_file()


# --- 9. quarantine record persistence failure -------------------------------


def test_quarantine_persistence_failure_still_retains_lock_and_notes_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Receipt-root persistence fails while the ledger half still succeeds.

    Uses the no-prior-failure path (like test 8) so the raised C10Error's
    ``__cause__`` is exactly ``cleanup_error``: that is the object
    ``_quarantine_jll_admission`` actually annotates with
    ``add_note(...)`` for a persistence failure. When an action failure is
    also present (as in test 7), that note lands on ``cleanup_error`` too,
    but ``cleanup_error`` itself is never re-raised or chained in that
    branch (only its message is folded into one summary note on the
    original failure) -- so this is the reachable way to observe it.
    """
    events: list[str] = []
    # A receipt root that was never provisioned as an owner-0700 directory:
    # _open_private_root must fail, so the receipt-root half of the
    # quarantine write fails while the ledger half still succeeds.
    missing_receipt_root = tmp_path / "never-created-receipts"

    def action(**_: Any) -> dict[str, Any]:
        events.append("action")
        return {"manifest": "fake"}

    lock = _FakeLock(events)
    sidecar_cls = _make_sidecar_cls(
        events, stop_error=C10Error("sidecar removal was not confirmed")
    )
    _wire_common(
        monkeypatch, tmp_path, events, lock=lock, sidecar_cls=sidecar_cls, action=action
    )
    with pytest.raises(C10Error, match="teardown was not proven") as excinfo:
        production.execute_jll_admission_collection(
            **_valid_kwargs(tmp_path, receipt_root=missing_receipt_root)
        )
    assert lock.retain_on_exit is True
    cleanup_error = excinfo.value.__cause__
    assert cleanup_error is not None
    notes = getattr(cleanup_error, "__notes__", [])
    assert any("receipt-root quarantine failed" in note for note in notes)
    # The ledger half still succeeded despite the receipt-root failure.
    ledger_dir = tmp_path / "ledger"
    quarantine_files = list(ledger_dir.glob("jll-admission-*.quarantine"))
    assert len(quarantine_files) == 1


def test_three_way_failure_propagates_teardown_and_persistence_notes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Action fails, teardown fails, and a quarantine record cannot persist.

    The cleanup error is never raised or chained in this branch, so its
    persistence-failure note must be folded onto the propagated failure.
    """
    events: list[str] = []
    missing_receipt_root = tmp_path / "never-created-receipts"

    def action(**_: Any) -> dict[str, Any]:
        events.append("action")
        raise C10Error("JLL admission controller deadline expired")

    lock = _FakeLock(events)
    sidecar_cls = _make_sidecar_cls(
        events, stop_error=C10Error("sidecar removal was not confirmed")
    )
    _wire_common(
        monkeypatch, tmp_path, events, lock=lock, sidecar_cls=sidecar_cls, action=action
    )
    with pytest.raises(C10Error, match="deadline expired") as excinfo:
        production.execute_jll_admission_collection(
            **_valid_kwargs(tmp_path, receipt_root=missing_receipt_root)
        )
    assert lock.retain_on_exit is True
    notes = getattr(excinfo.value, "__notes__", [])
    assert any("teardown also failed" in note for note in notes)
    assert any("receipt-root quarantine failed" in note for note in notes)


# --- 10. arm_benchmark failure -----------------------------------------------


def test_arm_benchmark_failure_retains_lock_before_any_sidecar_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []

    def action(**_: Any) -> dict[str, Any]:
        raise AssertionError("action must not run")

    lock = _FakeLock(events, arm_error=C10Error("arm marker write failed"))
    sidecar_cls = _make_sidecar_cls(events)
    _wire_common(
        monkeypatch, tmp_path, events, lock=lock, sidecar_cls=sidecar_cls, action=action
    )
    with pytest.raises(C10Error, match="arm marker write failed"):
        production.execute_jll_admission_collection(**_valid_kwargs(tmp_path))
    assert events == ["acquire", "release"]
    assert "sidecar_init" not in events
    assert lock.retain_on_exit is True


def test_module_functions_still_present_for_regression_signature_checks() -> None:
    """Guard the private helpers this file relies on so a rename is caught here."""
    for name in (
        "_execute_authorized_jll_admission_action",
        "_c10_ledger_root",
        "_quarantine_jll_admission",
        "_require_timeout",
        "execute_jll_admission_collection",
    ):
        assert hasattr(production, name), name


def test_contracts_module_reexports_c10error() -> None:
    assert contracts.C10Error is C10Error
