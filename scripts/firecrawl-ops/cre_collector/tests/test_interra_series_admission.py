"""Interra opt-in propagation and terminal child reconciliation, offline only."""

import json

import cre_checkpoint_series as series
import pytest


def config(enabled=True):
    return series.series_config(
        sources=["interra-realty"],
        page_cap=400,
        concurrency=1,
        attempts_per_source=1,
        max_resume_age_hours=24,
        max_host_cpu_percent=90,
        cpu_sustain_seconds=30,
        cpu_sample_seconds=2,
        nice=15,
        admit_current_inventory_source="interra-realty" if enabled else None,
    )


def child(parent_config, run_id="child"):
    return {
        "run_id": run_id,
        "collector_git_sha": "a" * 40,
        "status": "additive_scope_complete_current_inventory",
        "error": None,
        "config": series._expected_child_config("interra-realty", parent_config),
        "scope": {
            "kind": "collector_registry_additive_current_inventory",
            "whole_source_coverage": False,
        },
        "started_at": "2026-10-15T12:00:01Z",
        "finished_at": "2026-10-15T12:00:02Z",
    }


def test_opt_in_only_configuration_and_real_child_argv(tmp_path):
    ordinary, enabled = config(False), config()
    assert "current_inventory_admission" not in ordinary
    assert {
        k: v for k, v in enabled.items() if k != "current_inventory_admission"
    } == ordinary
    for cfg, expected in [(ordinary, False), (enabled, True)]:
        argv = series.build_checkpoint_argv(
            "interra-realty",
            child_out_root=tmp_path / "runs",
            env_file=None,
            config=cfg,
        )
        assert ("--admit-current-inventory-source" in argv) is expected
        if expected:
            assert argv[-2:] == ["--admit-current-inventory-source", "interra-realty"]
    parent = series.new_manifest(tmp_path, git_sha="a" * 40, config=enabled)
    assert parent["scope"] == child(enabled)["scope"]
    assert "scope" not in series.new_manifest(
        tmp_path, git_sha="a" * 40, config=ordinary
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "legacy_status",
        "whole_coverage",
        "wrong_scope",
        "missing_admission",
        "policy_drift",
        "wrong_source",
        "wrong_transactions",
        "malformed_scope",
    ],
)
def test_terminal_admission_rejects_mismatched_child(mutation):
    cfg = config()
    value = child(cfg)
    if mutation == "legacy_status":
        value["status"] = series.SUCCESS_STATUS
    elif mutation == "whole_coverage":
        value["scope"]["whole_source_coverage"] = True
    elif mutation == "wrong_scope":
        value["scope"]["kind"] = "collector_registry"
    elif mutation == "missing_admission":
        value["config"].pop("current_inventory_admission")
    elif mutation == "policy_drift":
        value["config"]["current_inventory_admission"] = {
            "source_key": "interra-realty",
            "source_policy_sha256": "0" * 64,
        }
    elif mutation == "wrong_source":
        value["config"]["sources"] = ["hanley"]
    elif mutation == "wrong_transactions":
        value["config"]["transactions"] = ["sale"]
    elif mutation == "malformed_scope":
        value["scope"] = []
    assert not series._child_succeeded(value, cfg)
    assert series._reconciled_state(value, cfg) == "failed_global"


def test_additive_success_cannot_be_imported_into_ordinary_parent():
    assert series._child_succeeded(child(config()), config())
    assert not series._child_succeeded(child(config()), config(False))
    assert series._child_succeeded({"status": series.SUCCESS_STATUS}, config(False))


@pytest.mark.parametrize("enabled", [True, False])
def test_stale_reconciliation_preserves_explicit_admission(tmp_path, enabled):
    cfg = config(enabled)
    parent = series.new_manifest(tmp_path, git_sha="a" * 40, config=cfg)
    checkpoint = parent["sources"]["interra-realty"]
    checkpoint.update(
        state="running",
        checkpoint_run="runs/child",
        attempts=[{"started_at": "2026-10-15T12:00:00Z"}],
    )
    value = child(cfg)
    if not enabled:
        value["status"] = series.SUCCESS_STATUS
    path = tmp_path / "runs/child/manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(value))
    series.reconcile_stale_running_sources(tmp_path, parent)
    assert checkpoint["state"] == "complete"
    assert checkpoint["checkpoint_status"] == value["status"]


def test_run_series_accepts_only_exact_opt_in_child(tmp_path, monkeypatch):
    cfg = config()
    parent = series.new_manifest(tmp_path, git_sha="a" * 40, config=cfg)

    class Process:
        pid = 999999

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(series.subprocess, "Popen", lambda *_a, **_k: Process())
    monkeypatch.setattr(
        series,
        "_load_child_manifest",
        lambda *_a, **_k: (tmp_path / "runs/child/manifest.json", child(cfg)),
    )
    assert series.run_series(tmp_path, parent, env_file=None, retry_failed=False) == 0
    assert parent["status"] == "complete"
    assert parent["scope"]["whole_source_coverage"] is False
