"""Resource diagnostics stay inside coordinator ownership and preserve outcomes."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series
import pytest
from cre_validate import CHECKPOINT_PREFLIGHT_QUERY_NAMES


@pytest.mark.parametrize(
    "outcome", ["complete", "health_failure", "cpu_stop", "interrupt"]
)
def test_resource_observer_and_report_follow_checkpoint_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    events: list[str] = []

    class OwnedLock:
        def __init__(self, _path, *, recovery_required):
            assert recovery_required is True
            self.held = False

        def __enter__(self):
            events.append("lock_enter")
            self.held = True
            return self

        def clear_recovery_requirement(self):
            assert events[-1] == "report_saved"
            events.append("fence_clear")

        def __exit__(self, *_exc):
            events.append("lock_exit")
            self.held = False

    @contextmanager
    def observe(run_dir, **kwargs):
        assert events == ["lock_enter"]
        assert callable(kwargs["cpu_sampler"])
        assert kwargs["context_provider"]()["phase"] == "preflight"
        assert "CRE_ACTIVATE_STATUS" not in kwargs["env"]
        events.append("observer_enter")
        try:
            yield
        finally:
            events.append("observer_exit")

    def save_report(run_dir):
        assert events[-1] == "guard_stop"
        assert "observer_exit" in events
        manifest = json.loads((run_dir / "manifest.json").read_text())
        expected = {
            "complete": "supported_scope_complete",
            "health_failure": "failed",
            "cpu_stop": "resource_guard_interrupted",
            "interrupt": "interrupted",
        }
        assert manifest["status"] == expected[outcome]
        events.append("report_saved")

    def run_command(argv, _log, **_kwargs):
        if Path(argv[1]).name == "firecrawl_healthcheck.sh":
            if outcome == "health_failure":
                return 7
            if outcome == "interrupt":
                raise KeyboardInterrupt
        if Path(argv[1]).name == "cre_validate.py":
            target = Path(argv[argv.index("--out") + 1])
            target.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "validation_profile": refresh.CHECKPOINT_PREFLIGHT_PROFILE,
                        "audit_scope": refresh.VALIDATION_AUDIT_SCOPES[
                            refresh.CHECKPOINT_PREFLIGHT_PROFILE
                        ],
                        "queries": {
                            **{name: [] for name in CHECKPOINT_PREFLIGHT_QUERY_NAMES},
                            "lifecycle_schema_contract": [
                                {"contract_item": name, "status": "ok"}
                                for name in refresh.LIFECYCLE_SCHEMA_CONTRACT_ITEMS
                            ],
                        },
                    }
                )
            )
        return 0

    monkeypatch.setenv("CRE_ACTIVATE_STATUS", "1")
    monkeypatch.setattr(refresh, "git_identity", lambda: ("b" * 40, False))
    monkeypatch.setattr(
        refresh, "checkpoint_lock_dir", lambda _value: tmp_path / "lock"
    )
    monkeypatch.setattr(
        refresh,
        "database_target_fingerprint",
        lambda _value: {
            "algorithm": "sha256",
            "value": "a" * 64,
        },
    )
    monkeypatch.setattr(refresh, "SharedLock", OwnedLock)
    monkeypatch.setattr(refresh, "observe_resource_usage", observe)
    monkeypatch.setattr(refresh, "save_report", save_report)
    monkeypatch.setattr(
        refresh,
        "run_cpu_guard_preflight",
        lambda _guard: 95 if outcome == "cpu_stop" else 20,
    )
    monkeypatch.setattr(
        refresh.HostCpuGuard, "start", lambda _guard: events.append("guard_start")
    )
    monkeypatch.setattr(
        refresh.HostCpuGuard, "stop", lambda _guard: events.append("guard_stop")
    )
    monkeypatch.setattr(refresh, "run_command", run_command)
    monkeypatch.setattr(refresh, "record_scope_from_validation", lambda *_args: None)
    monkeypatch.setattr(refresh, "prepare_sources_cohort", lambda *_a, **_kw: [])
    monkeypatch.setattr(refresh, "run_aggregate_gate", lambda *_args: None)
    monkeypatch.setattr(refresh, "ingest_admitted_sources", lambda *_args: None)
    monkeypatch.setattr(refresh, "run_final_validation", lambda *_args: None)
    argv = ["--out-root", str(tmp_path / "runs"), "--sources", "cbre"]
    if outcome == "health_failure":
        with pytest.raises(refresh.GlobalStageError, match="healthcheck failed"):
            refresh.main(argv)
    else:
        assert (
            refresh.main(argv)
            == {"complete": 0, "cpu_stop": 75, "interrupt": 130}[outcome]
        )
    assert events[-4:] == ["guard_stop", "report_saved", "fence_clear", "lock_exit"]
    assert events.index("observer_exit") < events.index("lock_exit")
    # Guard decisions and terminal evidence both settle before the exact owner
    # clears its durable crash fence and releases ownership.
    assert events.index("guard_stop") < events.index("lock_exit")


@pytest.mark.parametrize("outcome", ["complete", "failure", "interrupt"])
def test_series_saves_report_before_releasing_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    events: list[str] = []

    class Ownership:
        def __init__(self, _path):
            pass

        def __enter__(self):
            events.append("owned")

        def __exit__(self, *_exc):
            events.append("released")

    def run_series(run_dir, manifest, **_kwargs):
        events.append("run")
        if outcome == "interrupt":
            raise KeyboardInterrupt
        manifest["status"] = "complete" if outcome == "complete" else "failed"
        series.save_manifest(run_dir, manifest)
        return 0 if outcome == "complete" else 1

    def save_report(run_dir):
        assert events == ["owned", "run"]
        assert (run_dir / "manifest.json").is_file()
        events.append("report")

    monkeypatch.setattr(series, "SeriesOwnershipLock", Ownership)
    monkeypatch.setattr(series, "git_identity", lambda: ("b" * 40, False))
    monkeypatch.setattr(
        series,
        "database_target_fingerprint",
        lambda _value: {
            "algorithm": "sha256",
            "value": "a" * 64,
        },
    )
    monkeypatch.setattr(series, "run_series", run_series)
    monkeypatch.setattr(series, "save_report", save_report)
    argv = ["--out-root", str(tmp_path / "series"), "--sources", "cbre"]
    if outcome == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            series.main(argv)
    else:
        assert series.main(argv) == (0 if outcome == "complete" else 1)
    assert events == ["owned", "run", "report", "released"]
