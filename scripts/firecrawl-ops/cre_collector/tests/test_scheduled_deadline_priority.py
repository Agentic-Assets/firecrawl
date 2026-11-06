"""A fresh scheduled generation retains all sources after a settled deadline."""
import json

import pytest

import cre_checkpoint_series as series
import cre_series_control
from scheduling.priority import after_deadline
from tests.test_colliers_partial_series import mixed_child
from tests.test_pipeline_configuration import PARENT_START, RUN_ID, config


def case(tmp_path, monkeypatch):
    sources = ["cbre", "colliers", "savills"]
    settings = config(pipeline=True, sources=sources)
    target = {"algorithm": "sha256", "value": "c" * 64}
    prior_dir = tmp_path / "2026-10-15T120000Z"
    prior_dir.mkdir()
    current_dir = tmp_path / "2026-10-16T120000Z"
    current_dir.mkdir()
    prior = series.new_manifest(prior_dir, git_sha="a" * 40, config=settings, database_target=target)
    prior.update(started_at=PARENT_START, status="interrupted",
                 latest_invocation_control={"invocation_id": "1" * 32})
    run = prior_dir / "runs" / RUN_ID
    child = mixed_child(run)
    child.update(status="interrupted", config=series._expected_child_config(",".join(sources), settings))
    child["pipeline"].update(status="reconciliation_required", commit_outcome_unknown=False)
    for row in child["sources"].values():
        row["attempts"] = [{"number": 1}]
    child["sources"]["savills"] = {"state": "pending", "attempts": []}
    child["pipeline"]["source_results"]["savills"] = {"state": "failed"}
    (run / "manifest.json").write_text(json.dumps(child))
    for source in sources:
        prior["sources"][source].update(state="interrupted", checkpoint_run=f"runs/{RUN_ID}",
            attempts=[{"started_at": child["started_at"], "series_control_child_id": "2" * 32}])
    (prior_dir / "manifest.json").write_text(json.dumps(prior))
    current = series.new_manifest(current_dir, git_sha="a" * 40, config=settings, database_target=target)
    receipt = {"status": "deadline_interrupted", "local_settlement": "confirmed",
               "last_checkpoint": {"run_id": RUN_ID, "child_invocation_id": "2" * 32}}
    monkeypatch.setattr(cre_series_control, "read_receipt", lambda *_: receipt)
    monkeypatch.setattr(series, "_load_child_manifest", lambda *args, **kwargs: (run / "manifest.json", child))
    return prior_dir, current_dir, current, prior, child, receipt


def test_unstarted_and_interrupted_precede_prior_verified_without_skipping():
    sources = ["cbre", "colliers", "savills", "jll"]
    parent = {"sources": {"cbre": {"state": "complete"}, "colliers": {"state": "qualified_partial_subset"},
                          "savills": {"state": "interrupted"}, "jll": {"state": "failed_source"}}}
    child = {"sources": {source: {"attempts": [] if source == "savills" else [{}]} for source in sources}}
    assert after_deadline(sources, parent, child) == ["savills", "jll", "colliers", "cbre"]
    assert set(after_deadline(sources, parent, child)) == set(sources)


def test_next_generation_derives_order_from_settled_bound_evidence(tmp_path, monkeypatch):
    prior_dir, current_dir, current, prior, _child, _receipt = case(tmp_path, monkeypatch)
    result = series.scheduled_deadline_priority(tmp_path, current_dir, current)
    assert result["order"] == ["savills", "colliers", "cbre"]
    assert result["previous_series"] == prior_dir.name
    assert len(result["previous_manifest_sha256"]) == 64
    assert prior["sources"]["cbre"]["state"] == "interrupted"


def test_preallocated_successor_uses_exact_predecessor_even_when_suffix_sorts_first(tmp_path, monkeypatch):
    prior_dir, current_dir, current, prior, child, _receipt = case(tmp_path, monkeypatch)
    prior_id = "2026-10-15T120000Z-ffffffffffff"
    current_id = "2026-10-15T120000Z-000000000000"
    prior_dir.rename(tmp_path / prior_id)
    current_dir.rename(tmp_path / current_id)
    prior_dir, current_dir = tmp_path / prior_id, tmp_path / current_id
    prior.update(series_id=prior_id, finished_at="2026-10-16T00:00:00Z")
    (prior_dir / "manifest.json").write_text(json.dumps(prior))
    current["series_id"] = current_id
    monkeypatch.setattr(series, "_load_child_manifest", lambda *a, **kw:
        (prior_dir / "runs" / RUN_ID / "manifest.json", child))
    result = series.scheduled_deadline_priority(tmp_path, current_dir, current, predecessor_id=prior_id)
    assert result["previous_series"] == prior_id
    assert result["order"] == ["savills", "colliers", "cbre"]


@pytest.mark.parametrize("tamper", ["unknown_write", "unsettled", "foreign_child", "foreign_target", "altered_report"])
def test_uncertain_or_tampered_prior_does_not_reorder(tmp_path, monkeypatch, tamper):
    prior_dir, current_dir, current, _prior, child, receipt = case(tmp_path, monkeypatch)
    if tamper == "unknown_write":
        child["pipeline"]["commit_outcome_unknown"] = True
    elif tamper == "unsettled":
        receipt["local_settlement"] = "unknown"
    elif tamper == "foreign_child":
        receipt["last_checkpoint"]["child_invocation_id"] = "3" * 32
    elif tamper == "foreign_target":
        child["preflight"]["database_target"]["value"] = "d" * 64
    else:
        report = prior_dir / "runs" / RUN_ID / child["pipeline"]["units"][0]["validation_path"]
        report.write_bytes(report.read_bytes() + b" ")
    result = series.scheduled_deadline_priority(tmp_path, current_dir, current)
    assert result is None


def test_changed_prior_custody_during_qualification_falls_back(tmp_path, monkeypatch):
    prior_dir, current_dir, current, _prior, _child, _receipt = case(tmp_path, monkeypatch)
    real_reconcile = series.reconcile_pipeline_series

    def change_after_qualification(*args, **kwargs):
        real_reconcile(*args, **kwargs)
        path = prior_dir / "runs" / RUN_ID / "manifest.json"
        path.write_bytes(path.read_bytes() + b" ")

    monkeypatch.setattr(series, "reconcile_pipeline_series", change_after_qualification)
    assert series.scheduled_deadline_priority(tmp_path, current_dir, current) is None


def test_exclusive_source_that_consumes_deadline_moves_behind_unstarted_peers():
    sources = ["colliers-main", "cbre", "savills"]
    parent = {"sources": {source: {"state": "interrupted"} for source in sources}}
    child = {"sources": {source: {"attempts": [{}] if source == "colliers-main" else []}
                         for source in sources}}
    assert after_deadline(sources, parent, child) == ["cbre", "savills", "colliers-main"]


def test_scope_or_missing_attempts_refuses():
    with pytest.raises(ValueError):
        after_deadline(["cbre", "cbre"], {"sources": {}}, {"sources": {}})
    with pytest.raises(TypeError):
        after_deadline(["cbre"], {"sources": {"cbre": {"state": "interrupted"}}},
                       {"sources": {"cbre": {}}})


def test_order_is_forwarded_to_one_fresh_child_with_full_scope(tmp_path, monkeypatch):
    settings = config(pipeline=True, sources=["cbre", "savills"])
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=settings,
        database_target={"algorithm": "sha256", "value": "c" * 64})
    manifest["pipeline_dispatch"] = {"order": ["savills", "cbre"]}
    captured = []

    def stop_after_capture(argv, **kwargs):
        captured.extend(argv)
        raise RuntimeError("synthetic stop before child start")

    monkeypatch.setattr(series.subprocess, "Popen", stop_after_capture)
    with pytest.raises(RuntimeError, match="synthetic stop"):
        series.run_pipeline_series(tmp_path, manifest, env_file=None)
    assert captured[captured.index("--sources") + 1] == "cbre,savills"
    assert captured[captured.index("--pipeline-dispatch-order") + 1] == "savills,cbre"


def test_preallocated_parent_forwards_exact_target_to_fresh_child(tmp_path, monkeypatch):
    settings = config(pipeline=True, sources=["cbre", "savills"])
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=settings,
        database_target={"algorithm": "sha256", "value": "c" * 64})
    manifest["preallocated_expected_target_sha256"] = "c" * 64
    captured = []
    def stop_after_capture(argv, **kwargs):
        captured.extend(argv)
        raise RuntimeError("synthetic stop before child start")
    monkeypatch.setattr(series.subprocess, "Popen", stop_after_capture)
    with pytest.raises(RuntimeError, match="synthetic stop"):
        series.run_pipeline_series(tmp_path, manifest, env_file=None)
    assert captured[captured.index("--expected-parent-db-target-sha256") + 1] == "c" * 64


@pytest.mark.parametrize("order", ["cbre", "cbre,cbre", "cbre,jll"])
def test_invalid_order_rejected_before_credentials_or_write(monkeypatch, order):
    import cre_checkpoint_refresh as refresh

    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda *_: pytest.fail("credentials accessed"))
    with pytest.raises(SystemExit):
        refresh.main(["--pipeline", "--sources", "cbre,savills", "--pipeline-dispatch-order", order])


def test_prior_superset_and_reviewed_ancestor_prioritize_current_subset(tmp_path, monkeypatch):
    _prior_dir, current_dir, current, _prior, _child, _receipt = case(tmp_path, monkeypatch)
    current["config"]["sources"] = ["cbre", "savills"]
    current["collector_git_sha"] = "b" * 40
    calls = []
    monkeypatch.setattr(series, "local_git_output", lambda argv: calls.append(argv) or "a" * 40)
    result = series.scheduled_deadline_priority(tmp_path, current_dir, current)
    assert result["order"] == ["savills", "cbre"]
    assert calls == [["merge-base", "a" * 40, "b" * 40]]


def test_unrelated_code_or_incomplete_prior_scope_cannot_prioritize(tmp_path, monkeypatch):
    _prior_dir, current_dir, current, _prior, _child, _receipt = case(tmp_path, monkeypatch)
    current["collector_git_sha"] = "b" * 40
    monkeypatch.setattr(series, "local_git_output", lambda _: "c" * 40)
    assert series.scheduled_deadline_priority(tmp_path, current_dir, current) is None
    current["collector_git_sha"] = "a" * 40
    current["config"]["sources"].append("jll")
    assert series.scheduled_deadline_priority(tmp_path, current_dir, current) is None


def test_git_cleanup_unknown_is_not_hidden(tmp_path, monkeypatch):
    _prior_dir, current_dir, current, _prior, _child, _receipt = case(tmp_path, monkeypatch)
    current["collector_git_sha"] = "b" * 40

    def unknown(_):
        raise series.ProcessCleanupUnknown("synthetic unknown")

    monkeypatch.setattr(series, "local_git_output", unknown)
    with pytest.raises(series.ProcessCleanupUnknown):
        series.scheduled_deadline_priority(tmp_path, current_dir, current)


def test_future_series_does_not_override_latest_earlier_deadline(tmp_path, monkeypatch):
    import shutil

    prior_dir, current_dir, current, _prior, _child, _receipt = case(tmp_path, monkeypatch)
    future_dir = tmp_path / "2099-10-16T120000Z"
    shutil.copytree(prior_dir, future_dir)
    manifest_path = future_dir / "manifest.json"
    future = json.loads(manifest_path.read_text())
    future["series_id"] = future_dir.name
    manifest_path.write_text(json.dumps(future))

    result = series.scheduled_deadline_priority(tmp_path, current_dir, current)
    assert result["previous_series"] == prior_dir.name
    assert result["order"] == ["savills", "colliers", "cbre"]
