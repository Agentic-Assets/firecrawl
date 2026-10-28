"""Partial outcomes preserve settlement and diagnostic consumer semantics."""

import copy
import sys
from datetime import datetime, timezone
import pytest
import cre_checkpoint_series as series
import cre_series_control as control
import cre_source_health as health
import cre_performance_report as performance
import cre_performance_compare as compare
import cre_current_inventory_admission as admission
from test_cre_series_control import cli_role, fake_stop  # noqa: F401
import test_cre_series_control as support

SOURCE = "daum-commercial"


@pytest.mark.skipif(sys.platform != "linux", reason="controlled series requires Linux pidfd")
def test_actual_control_finish_preserves_partial_terminal(
    tmp_path, monkeypatch, cli_role  # noqa: F811
):  # noqa: F811
    manifest = support.manifest_at(tmp_path)

    def work(_path, current, **kwargs):
        current["status"] = "qualified_partial_subset"
        return 0

    monkeypatch.setattr(series, "run_series", work)
    monkeypatch.setattr(series, "save_report", lambda *a: None)
    with support.SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        assert (
            series.run_controlled_series(
                tmp_path,
                manifest,
                lock,
                support.args(),
                _cli_control_capability=cli_role,
            )
            == 0
        )
    receipt = control.read_receipt(
        tmp_path, manifest["latest_invocation_control"]["invocation_id"]
    )
    assert receipt["status"] == "qualified_partial_subset"
    assert receipt["local_settlement"] == "not_started"


@pytest.mark.skipif(sys.platform != "linux", reason="series stop requires Linux pidfd")
def test_stop_observer_recognizes_settled_partial_without_full_credit(fake_stop):  # noqa: F811
    _, terminal, _ = fake_stop
    terminal["status"] = "qualified_partial_subset"
    rc, result = control.stop_series("/private/series", "a" * 32, 1)
    assert rc == 0 and result["outcome"] == "qualified_partial_subset"
    assert result["local_settlement"] == "confirmed"


def subset_health():
    observation = "2026-10-16T12:00:01Z"
    checkpoint = {"state": "qualified_partial_subset"}
    child = {
        "run_id": "same",
        "status": "qualified_partial_subset",
        "started_at": "2026-10-16T12:00:00Z",
        "finished_at": "2026-10-16T12:00:02Z",
        "config": {
            "sources": [SOURCE],
            "transactions": ["sale", "lease"],
            "current_inventory_admission": admission.subset_configuration(),
        },
        "scope": {
            "kind": "collector_registry_verified_subset",
            "whole_source_coverage": False,
        },
        "sources": {
            SOURCE: {
                "state": "ingested",
                "readback": {"ok": True, "generation_id": "same"},
                "artifact": {"sha256": "a" * 64},
                "gate": {
                    "current_inventory_proof": {
                        "generation_id": "same",
                        "artifact_sha256": "a" * 64,
                        "scope": admission.DAUM_SUBSET_SCOPE,
                        "whole_source_coverage": False,
                    }
                },
            }
        },
    }
    return checkpoint, child, observation


def test_health_partial_attempt_never_advances_full_success_values():
    cp, child, observed = subset_health()
    kwargs = {"computed_at": "2026-10-16T12:00:03Z", "source_key": SOURCE}
    qualified = health._qualified_subset_attempt(cp, child, observed, **kwargs)
    assert (
        qualified
        and health._attempt_status(cp, qualified_subset=qualified) == "succeeded"
    )
    assert health._publication_status(cp, child, observed, **kwargs) == "partial"
    assert (
        health._successful_values(
            SOURCE, cp, child, observed, computed_at=kwargs["computed_at"]
        )
        is None
    )
    assert not health._qualified_subset_attempt(
        cp, child, observed, computed_at=kwargs["computed_at"], source_key="jll"
    )
    for fault in ["generation", "hash", "scope", "clock"]:
        bad = copy.deepcopy(child)
        if fault == "generation":
            bad["sources"][SOURCE]["readback"]["generation_id"] = "other"
        elif fault == "hash":
            bad["sources"][SOURCE]["artifact"]["sha256"] = "b" * 64
        elif fault == "scope":
            bad["scope"]["whole_source_coverage"] = True
        else:
            bad["started_at"] = "2026-10-16T12:00:02Z"
        assert health._publication_status(cp, bad, observed, **kwargs) == "unknown"


def test_performance_partial_is_known_and_excluded_from_full_matched_success(tmp_path):
    cp, child, _ = subset_health()
    child["sources"][SOURCE]["artifact"]["staged_unique"] = 100
    result = performance._source_report(
        tmp_path,
        child,
        SOURCE,
        now=datetime.now(timezone.utc),
        command_evidence=([], []),
    )
    assert result["status"] == "qualified_partial_subset"
    assert "qualified_partial_subset" in performance.STATUSES
    assert "qualified_partial_subset" not in compare._SUCCESS
