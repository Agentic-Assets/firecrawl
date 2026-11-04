"""Interrupted acquisition evidence remains recoverable and fail-closed."""

import json
import subprocess
import sys
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series
import pytest
from source_evidence import automatic
from test_automatic import SHA, artifact, setup_policy


def make_run(tmp_path):
    config, _, _ = setup_policy(tmp_path)
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    manifest = refresh.new_manifest(run, git_sha=SHA, git_dirty=False, sources=("faris-lee",),
                                    page_cap=400, concurrency=1, automatic_source_evidence=config)
    return run, manifest


def test_serial_interrupted_temp_survives_next_attempt_and_can_be_sealed(tmp_path, monkeypatch):
    run, manifest = make_run(tmp_path)
    monkeypatch.setattr(refresh, "_manifest_checkpoint_artifact_valid", lambda *_: None)

    def interrupted(argv, *_args, **_kwargs):
        saved = json.loads((run / "manifest.json").read_bytes())
        attempt = saved["sources"]["faris-lee"]["attempts"][0]
        assert attempt["source_evidence"]["state"] == "awaiting_artifact"
        path = Path(next(v.removeprefix("--out=") for v in argv if v.startswith("--out=")))
        path.write_text(json.dumps(artifact()))
        raise KeyboardInterrupt()

    monkeypatch.setattr(refresh, "run_command", interrupted)
    with pytest.raises(KeyboardInterrupt):
        refresh.collect_source(run, manifest, "faris-lee", transactions=("sale", "lease"),
                               page_cap=400, concurrency=1, attempts_this_run=1)
    first = run / manifest["sources"]["faris-lee"]["attempts"][0]["temporary_artifact"]
    assert first.exists()

    class Stop(Exception):
        pass

    def next_attempt(argv, *_args, **_kwargs):
        new = Path(next(v.removeprefix("--out=") for v in argv if v.startswith("--out=")))
        assert new != first and first.exists()
        raise Stop()

    monkeypatch.setattr(refresh, "run_command", next_attempt)
    with pytest.raises(Stop):
        refresh.collect_source(run, manifest, "faris-lee", transactions=("sale", "lease"),
                               page_cap=400, concurrency=1, attempts_this_run=1)
    # The second worker did not publish anything; recovery reports that separately.
    automatic.recover_unsealed(run, manifest, refresh.save_manifest, make_private=refresh.make_private_artifact)
    attempts = manifest["sources"]["faris-lee"]["attempts"]
    assert attempts[0]["source_evidence"]["state"] == "pending"
    assert attempts[1]["source_evidence"]["state"] == "no_artifact"
    assert manifest["automatic_source_evidence_status"] == "incomplete"
    assert first.exists()


def test_rejected_rename_recovered_from_prelaunch_marker(tmp_path):
    run, manifest = make_run(tmp_path)
    attempt, temporary, _, _ = refresh._cohort_attempt(run, manifest, "faris-lee", collector_concurrency=1)
    temporary.write_text(json.dumps(artifact()))
    temporary.chmod(0o600)
    rejected, _ = refresh._archive_rejected(temporary, run, "faris-lee", attempt["number"])
    persisted = json.loads((run / "manifest.json").read_bytes())
    assert persisted["sources"]["faris-lee"]["attempts"][0]["rejected_artifact"] is None
    automatic.recover_unsealed(run, persisted, refresh.save_manifest, make_private=refresh.make_private_artifact)
    evidence = persisted["sources"]["faris-lee"]["attempts"][0]["source_evidence"]
    assert evidence["state"] == "pending" and evidence["events"] == 79
    assert (run / rejected).exists()


def test_process_exit_after_publish_recovers_from_durable_marker(tmp_path):
    run, manifest = make_run(tmp_path)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(manifest["config"]["automatic_source_evidence"]))
    script = r'''
import json, os, sys
from pathlib import Path
import cre_checkpoint_refresh as r
run, config_file = Path(sys.argv[1]), Path(sys.argv[2])
config = json.loads(config_file.read_text())
manifest = r.new_manifest(run, git_sha='a'*40, git_dirty=False, sources=('faris-lee',),
                          page_cap=400, concurrency=1, automatic_source_evidence=config)
r._manifest_checkpoint_artifact_valid = lambda *_: None
def worker(argv, *_args, **_kwargs):
    path = Path(next(v.removeprefix('--out=') for v in argv if v.startswith('--out=')))
    value = {'runMeta': {'finishedAt': '2026-10-28T10:00:00Z'},
             'sources': [{'sourceKey': 'faris-lee'}],
             'listings': [{'sourceKey': 'faris-lee', 'n': i} for i in range(78)]}
    path.write_text(json.dumps(value))
    os._exit(91)
r.run_command = worker
r.collect_source(run, manifest, 'faris-lee', transactions=('sale', 'lease'),
                 page_cap=400, concurrency=1, attempts_this_run=1)
'''
    child = subprocess.run([sys.executable, "-c", script, str(run), str(config_file)],
                           capture_output=True, check=False, timeout=20)
    assert child.returncode == 91
    persisted = json.loads((run / "manifest.json").read_bytes())
    attempt = persisted["sources"]["faris-lee"]["attempts"][0]
    assert attempt["source_evidence"]["state"] == "awaiting_artifact"
    assert (run / attempt["temporary_artifact"]).exists()
    automatic.recover_unsealed(run, persisted, refresh.save_manifest, make_private=refresh.make_private_artifact)
    assert attempt["source_evidence"]["state"] == "pending"
    assert attempt["source_evidence"]["events"] == 79


@pytest.mark.parametrize("case", ["missing", "both", "unsafe", "old_marker"])
def test_missing_conflicting_unsafe_and_legacy_attempts_are_explicit(tmp_path, case):
    run, manifest = make_run(tmp_path)
    attempt, temporary, _, _ = refresh._cohort_attempt(run, manifest, "faris-lee", collector_concurrency=1)
    if case == "both":
        temporary.write_text(json.dumps(artifact()))
        rejected = run / attempt["source_evidence"]["rejected_path"]
        rejected.parent.mkdir()
        rejected.write_text(json.dumps(artifact()))
    elif case == "unsafe":
        target = run / "target"
        target.write_text(json.dumps(artifact()))
        temporary.symlink_to(target)
    elif case == "old_marker":
        del attempt["source_evidence"]
    automatic.recover_unsealed(run, manifest, refresh.save_manifest, make_private=refresh.make_private_artifact)
    evidence = attempt["source_evidence"]
    assert evidence["state"] == ("no_artifact" if case == "missing" else "refused")
    assert manifest["automatic_source_evidence_status"] == "incomplete"
    if case != "missing":
        assert manifest["automatic_source_evidence_paused"]
    if case == "both":
        assert temporary.exists() and rejected.exists()
    if case == "unsafe":
        assert temporary.is_symlink() and target.exists()


def test_marker_distinct_from_unknown_commit_and_series_detects_missing():
    marker = {"state": "awaiting_artifact", "temporary_path": "sources/faris-lee.attempt-1.json.tmp",
              "rejected_path": "rejected/faris-lee-attempt-1.json"}
    attempt = {"number": 1, "source_evidence": marker}
    manifest = {"config": {"automatic_source_evidence": {"source_keys": ["faris-lee"]}},
                "sources": {"faris-lee": {"attempts": [attempt]}}}
    automatic.prepare_resume(manifest, replay_unknown=False)
    assert marker["state"] == "awaiting_artifact"
    with pytest.raises(ValueError, match="no uncertain"):
        automatic.prepare_resume(manifest, replay_unknown=True)
    assert series.evidence_reconciliation_required(manifest)
    attempt["source_evidence"] = {"state": "pending"}
    assert series.evidence_reconciliation_required(manifest)
    assert not series.source_local_failure({**manifest, "status": "failed",
                                            "error": "RefreshError: source checkpoints remain incomplete: faris-lee"})
    attempt["source_evidence"] = {"state": "no_artifact"}
    assert not series.evidence_reconciliation_required(manifest)
    assert series.source_local_failure({**manifest, "status": "failed",
                                        "error": "RefreshError: source checkpoints remain incomplete: faris-lee"})
    del attempt["source_evidence"]
    assert series.evidence_reconciliation_required(manifest)


@pytest.mark.parametrize("pipeline", [False, True])
def test_series_restart_stops_before_new_child_for_prior_sealed_pending(tmp_path, monkeypatch, pipeline):
    config, _, _ = setup_policy(tmp_path)
    settings = series.series_config(sources=("faris-lee",), page_cap=400, concurrency=1,
                                    attempts_per_source=1, max_resume_age_hours=24,
                                    max_host_cpu_percent=90, cpu_sustain_seconds=30,
                                    cpu_sample_seconds=2, nice=10, pipeline=pipeline,
                                    automatic_source_evidence=config)
    parent = series.new_manifest(tmp_path, git_sha=SHA, config=settings)
    parent["sources"]["faris-lee"].update(state="interrupted", checkpoint_run="runs/prior")
    child = {"status": "interrupted", "config": {"automatic_source_evidence": config},
             "sources": {"faris-lee": {"attempts": [{"source_evidence": {"state": "pending"}}]}}}
    monkeypatch.setattr(series, "_load_child_manifest", lambda *_a, **_k: (tmp_path / "prior.json", child))
    monkeypatch.setattr(series.subprocess, "Popen", lambda *_a, **_k: pytest.fail("started another child"))
    if pipeline:
        with pytest.raises(series.SeriesError, match="exact prior-run reconciliation"):
            series.run_series(tmp_path, parent, env_file=None, retry_failed=True)
    else:
        assert series.run_series(tmp_path, parent, env_file=None, retry_failed=True) == 1
        assert "exact prior-run reconciliation" in parent["error"]


def test_serial_series_rejects_mismatched_prior_child_before_dispatch(tmp_path, monkeypatch):
    config, _, _ = setup_policy(tmp_path)
    settings = series.series_config(sources=("faris-lee",), page_cap=400, concurrency=1,
                                    attempts_per_source=1, max_resume_age_hours=24,
                                    max_host_cpu_percent=90, cpu_sustain_seconds=30,
                                    cpu_sample_seconds=2, nice=10, automatic_source_evidence=config)
    parent = series.new_manifest(tmp_path, git_sha=SHA, config=settings)
    parent["sources"]["faris-lee"].update(state="interrupted", checkpoint_run="runs/prior")
    prior = tmp_path / "runs" / "prior"
    prior.mkdir(parents=True)
    (prior / "manifest.json").write_text(json.dumps({"run_id": "prior", "collector_git_sha": "b" * 40,
                                                       "config": {}, "sources": {}}))
    monkeypatch.setattr(series.subprocess, "Popen", lambda *_a, **_k: pytest.fail("started another child"))
    with pytest.raises(series.SeriesError, match="cannot verify prior automatic evidence outcome"):
        series.run_series(tmp_path, parent, env_file=None, retry_failed=True)


@pytest.mark.parametrize("replay_fails", [False, True])
def test_exact_unknown_replay_precedes_earlier_pending_write(tmp_path, monkeypatch, replay_fails):
    run, manifest = make_run(tmp_path)
    attempts = manifest["sources"]["faris-lee"]["attempts"]
    for number in (1, 2):
        attempt = {"number": number}
        attempts.append(attempt)
        path = run / f"input-{number}.json"
        path.write_text(json.dumps(artifact()))
        path.chmod(0o600)
        automatic.seal(run, manifest, "faris-lee", attempt, path)
        assert attempt["source_evidence"]["state"] == "pending"
    first, uncertain = (attempt["source_evidence"] for attempt in attempts)
    uncertain["state"] = "outcome_unknown"
    manifest["automatic_source_evidence_paused"] = True
    automatic.prepare_resume(manifest, replay_unknown=True)
    order = []

    def append(*_args, **_kwargs):
        current = "first" if first["state"] == "appending" else "uncertain"
        order.append(current)
        if replay_fails:
            raise RuntimeError("unknown outcome")

    monkeypatch.setattr(automatic.cli, "run_psql", append)
    automatic.pump(run, manifest, refresh.save_manifest)
    assert order == (["uncertain"] if replay_fails else ["uncertain", "first"])
    assert first["state"] == ("pending" if replay_fails else "appended")
    assert uncertain["state"] == ("outcome_unknown" if replay_fails else "appended")
