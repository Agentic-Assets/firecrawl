import hashlib
from pathlib import Path

import cre_checkpoint_refresh as refresh
from source_evidence import automatic, events, native_pages
from test_automatic import SHA, setup_policy
from test_pyramid_pages import entry


def test_pyramid_settle_unknown_replay(tmp_path, monkeypatch):
    config, policy, policy_path = setup_policy(
        tmp_path, sources=("pyramid-brokerage",), count=100
    )
    policy.update(
        schema_version=automatic.SCHEMA_V2,
        collector_projections={},
        native_source_formats={"pyramid-brokerage": "pyramid_inventory_decoded_v1"},
        max_native_pages=4,
    )
    raw = events.encoded(policy)
    policy_path.write_bytes(raw)
    config = automatic.configuration(
        str(policy_path),
        hashlib.sha256(raw).hexdigest(),
        config["env_file"],
        config["target_sha256"],
        SHA,
        ["pyramid-brokerage"],
    )
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    manifest = refresh.new_manifest(
        run,
        git_sha=SHA,
        git_dirty=False,
        sources=("pyramid-brokerage",),
        page_cap=4,
        concurrency=1,
        automatic_source_evidence=config,
    )
    attempt = {"number": 1, "source_evidence": {"state": "no_artifact"}}
    manifest["sources"]["pyramid-brokerage"]["attempts"].append(attempt)
    directory = native_pages.mark(run, manifest, "pyramid-brokerage", attempt)
    raw, name = entry(b'[{"id":1},{"id":2}]')
    path = directory / name
    path.write_bytes(raw)
    path.chmod(0o600)
    native_pages.settle(run, manifest, "pyramid-brokerage", attempt, lambda *_: None)
    native = attempt["native_source_evidence"]
    assert native["state"] == "settled" and native["pages"][0]["state"] == "pending"
    first = native["pages"][0]
    first["state"] = "outcome_unknown"
    automatic.prepare_resume(manifest, replay_unknown=True)
    calls = []
    monkeypatch.setattr(
        automatic.cli, "run_psql", lambda *args, **kwargs: calls.append(args)
    )
    automatic.pump(run, manifest, lambda *_: None)
    assert len(calls) == 1 and first["state"] == "appended"


def test_pyramid_failed_worker_collect_retains_settled_page(tmp_path, monkeypatch):
    config, policy, path = setup_policy(
        tmp_path, sources=("pyramid-brokerage",), count=100
    )
    policy.update(
        schema_version=automatic.SCHEMA_V2,
        collector_projections={},
        native_source_formats={"pyramid-brokerage": "pyramid_inventory_decoded_v1"},
        max_native_pages=4,
    )
    raw = events.encoded(policy)
    path.write_bytes(raw)
    config = automatic.configuration(
        str(path),
        hashlib.sha256(raw).hexdigest(),
        config["env_file"],
        config["target_sha256"],
        SHA,
        ["pyramid-brokerage"],
    )
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    manifest = refresh.new_manifest(
        run,
        git_sha=SHA,
        git_dirty=False,
        sources=("pyramid-brokerage",),
        page_cap=4,
        concurrency=1,
        automatic_source_evidence=config,
    )
    monkeypatch.setattr(refresh, "_manifest_checkpoint_artifact_valid", lambda *_: None)
    monkeypatch.setattr(refresh, "git_identity", lambda: (SHA, False))
    calls = []
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: calls.append(a))

    def worker(_argv, _log, *, env):
        directory = Path(env["CRE_PYRAMID_NATIVE_CAPTURE_DIR"])
        raw, name = entry(b'[{"id":1}]')
        path = directory / name
        path.write_bytes(raw)
        path.chmod(0o600)
        return 1

    monkeypatch.setattr(refresh, "run_command", worker)
    assert (
        refresh.collect_source(
            run,
            manifest,
            "pyramid-brokerage",
            transactions=("sale", "lease"),
            page_cap=4,
            concurrency=1,
            attempts_this_run=1,
        )
        is None
    )
    attempt = manifest["sources"]["pyramid-brokerage"]["attempts"][0]
    assert attempt["source_evidence"]["state"] == "no_artifact"
    assert (
        attempt["native_source_evidence"]["pages"][0]["state"] == "appended"
        and len(calls) == 1
    )


def test_pyramid_resume_refuses_cleanup_unknown_before_recovery(tmp_path, monkeypatch):
    import pytest

    path = tmp_path / "manifest.json"
    path.write_text('{"error":"owned process cleanup_unknown"}')
    called = []
    monkeypatch.setattr(
        automatic, "recover_unsealed", lambda *args, **kwargs: called.append(True)
    )
    with pytest.raises(
        refresh.ProcessCleanupUnknown, match="manual owned-process settlement"
    ):
        refresh.load_resume_manifest(
            path, git_sha=SHA, sources=("pyramid-brokerage",), page_cap=4, concurrency=1
        )
    assert not called
