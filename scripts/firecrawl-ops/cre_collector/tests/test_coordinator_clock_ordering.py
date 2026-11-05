"""Real source dispatch and settlement retain observed wall timestamps."""
import io
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import cre_checkpoint_refresh as refresh
import pytest
from tests.test_colliers_partial_failed_collector_pipeline import (
    test_evidence_only_collector_failure_reaches_partial_pipeline as run_source,
)


def test_real_colliers_source_recovers_after_dispatch_clock_reversal(tmp_path, monkeypatch):
    original = refresh._cohort_attempt
    calls = []

    def inject_once(root, manifest, source, **kwargs):
        calls.append(source)
        if len(calls) == 1:
            prior = refresh.parse_iso8601(manifest["started_at"], field="generation") - timedelta(milliseconds=60)
            with monkeypatch.context() as patcher:
                patcher.setattr(refresh, "utc_now", lambda: prior.isoformat())
                return original(root, manifest, source, **kwargs)
        return original(root, manifest, source, **kwargs)

    monkeypatch.setattr(refresh, "_cohort_attempt", inject_once)
    run_source(tmp_path, monkeypatch, False)
    assert len(calls) >= 2


def test_real_colliers_finished_evidence_is_sealed_when_clock_expires(tmp_path, monkeypatch):
    original = refresh._finalize_cohort_collection

    def expire(root, manifest, item, **kwargs):
        prior = refresh.parse_iso8601(item.attempt_started_at, field="start") - timedelta(milliseconds=60)
        with monkeypatch.context() as patcher:
            patcher.setattr(refresh, "utc_now", lambda: prior.isoformat())
            return original(root, manifest, item, candidate_only=True, clock_expired=True)

    monkeypatch.setattr(refresh, "_finalize_cohort_collection", expire)
    with pytest.raises(AssertionError):
        run_source(tmp_path, monkeypatch, False)
    root = tmp_path / "generation"
    child = json.loads((root / "manifest.json").read_text())
    checkpoint = child["sources"]["colliers"]
    attempt = checkpoint["attempts"][-1]
    rejected = root / attempt["rejected_artifact"]
    assert rejected.stat().st_mode & 0o077 == 0
    assert hashlib.sha256(rejected.read_bytes()).hexdigest() == attempt["rejected_artifact_sha256"]
    assert attempt["colliers_partial"]["state"] == "sealed_local"
    assert attempt["clock_order_refused"] is True and checkpoint["clock_hold"]["phase"] == "finish"
    assert checkpoint["state"] == "collect_infrastructure_failed" and not (tmp_path / "detail-called").exists()


@pytest.mark.parametrize("expires", [False, True])
def test_settled_group_is_never_signalled_or_reproved_during_clock_deferral(tmp_path, monkeypatch, expires):
    run = tmp_path / "generation"
    run.mkdir()
    manifest = refresh.new_manifest(run, git_sha="a" * 40, git_dirty=False,
        sources=("savills",), page_cap=1, concurrency=1)
    attempt, path, _, started = refresh._cohort_attempt(run, manifest, "savills", collector_concurrency=1)
    group = SimpleNamespace(gone=False)
    item = refresh.CohortCollectionProcess("savills", SimpleNamespace(poll=lambda: 80),
        io.StringIO(), path, attempt, started, group)
    proved = []
    def prove(owned):
        proved.append(owned)
        owned.gone = True
    monkeypatch.setattr(refresh, "_require_completed_group_settlement", prove)
    monkeypatch.setattr(refresh.os, "killpg", lambda *args: pytest.fail("reaped group must not be signalled"))
    earlier = refresh.parse_iso8601(started, field="start") - timedelta(milliseconds=60)
    monkeypatch.setattr(refresh, "utc_now", lambda: earlier.isoformat())
    with pytest.raises(refresh.ClockOrderingPending):
        refresh._finalize_cohort_collection(run, manifest, item, candidate_only=True)
    assert attempt["rc"] is None and attempt["finished_at"] is None
    if expires:
        assert not refresh._finalize_cohort_collection(run, manifest, item, candidate_only=True, clock_expired=True)
        assert attempt["finished_at"] == earlier.isoformat()
        assert manifest["sources"]["savills"]["state"] == "collect_infrastructure_failed"
    else:
        monkeypatch.setattr(refresh, "utc_now", lambda: started)
        monkeypatch.setattr(refresh, "archive_rejected_with_evidence", lambda *args: (None, None))
        monkeypatch.setattr(refresh, "seal_source_evidence", lambda *args: None)
        assert not refresh._finalize_cohort_collection(run, manifest, item, candidate_only=True)
        assert attempt["finished_at"] == started
    assert proved == [group]
