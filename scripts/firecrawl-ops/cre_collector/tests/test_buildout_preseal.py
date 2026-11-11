"""Whole-source pre-seal holds must be bound, no-write and fail closed."""

import hashlib
import json
from pathlib import Path

import pytest
from test_buildout_current_inventory import CODE, SOURCE, TARGET, setup

import cre_checkpoint_refresh as refresh
import cre_refresh_pipeline as pipeline
from source_fields.buildout_partial import prepare_partition
from source_fields.buildout_preseal import classify


@pytest.mark.parametrize("held, expected", [([], True), (["dual"], False)])
def test_bound_preseal_clear_or_whole_source_hold(tmp_path, monkeypatch, held, expected):
    run, artifact, _, manifest, _ = setup(tmp_path)
    coverage = run / "coverage.json"
    coverage.write_text("stable")
    digest = hashlib.sha256(coverage.read_bytes()).hexdigest()
    gate = {"verdict": "ok"}
    manifest["sources"][SOURCE]["gate"] = gate
    monkeypatch.setattr(refresh, "git_identity", lambda: (CODE, False))
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _: {"value": TARGET})
    monkeypatch.setattr(refresh, "save_manifest", lambda *_: None)
    monkeypatch.setattr(pipeline, "require_pipeline_owner", lambda *_: None)

    def command(argv, log, **_kwargs):
        values = {argv[i]: argv[i + 1] for i in range(3, len(argv), 2)}
        source_manifest = json.loads(Path(values["--manifest"]).read_bytes())
        part = prepare_partition(artifact.read_bytes(), source_manifest, SOURCE, code_sha=CODE, dirty=False,
                                 target_sha=TARGET, attempt_id=values["--attempt"])
        report = {"schema": "cre-buildout-preseal-identity-v1", "scope": part["scope"],
                  "manifest_sha256": values["--manifest-sha"], "attempt_id": values["--attempt"],
                  "staged_sha256": part["staged_sha256"], "staged_count": len(part["staged"]),
                  "held": held, "verdict": "hold" if held else "clear"}
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("synthetic probe\n")
        Path(values["--out"]).write_text(json.dumps(report))
        return 0

    monkeypatch.setattr(refresh, "run_command", command)
    result = pipeline.preseal_buildout_identity(run, manifest, SOURCE, artifact, None, coverage, digest, gate)
    assert result is expected
    assert manifest["sources"][SOURCE]["identity_preseal"]["held_count"] == len(held)
    if held:
        assert manifest["sources"][SOURCE]["state"] == "gate_blocked"
        assert manifest["sources"][SOURCE]["partial_probe_hold"] == "buildout_identity_ambiguous"
    else:
        first = manifest["sources"][SOURCE]["identity_preseal"]["path"]
        assert pipeline.preseal_buildout_identity(run, manifest, SOURCE, artifact, None, coverage, digest, gate)
        assert manifest["sources"][SOURCE]["identity_preseal"]["path"] != first
        assert [item["state"] for item in manifest["sources"][SOURCE]["identity_preseal_attempts"]] == ["complete", "complete"]
        saved = manifest["sources"][SOURCE]["identity_preseal"]
        snapshot = prepare_partition(artifact.read_bytes(), manifest, SOURCE, code_sha=CODE, dirty=False,
                                     target_sha=TARGET, attempt_id="a" * 32)
        observation = {"path": "synthetic.json", "sha256": "c" * 64, "scope": snapshot["scope"]}
        monkeypatch.setattr(refresh, "prepare_buildout_observation", lambda *_: observation)
        monkeypatch.setattr(refresh, "buildout_observation_readback", lambda *_: {"identity_held": 0})
        launched = []
        monkeypatch.setattr(refresh, "run_command", lambda argv, *_args, **_kwargs: launched.append(argv) or 0)
        for changed in (None, {**saved, "verdict": "hold"}, {**saved, "sha256": "0" * 64}):
            manifest["sources"][SOURCE]["identity_preseal"] = changed
            with pytest.raises(refresh.GlobalStageError):
                refresh.ingest_source(run, manifest, SOURCE, artifact, None, require_buildout_preseal=True)
            assert not launched
        manifest["sources"][SOURCE]["identity_preseal"] = saved
        refresh.ingest_source(run, manifest, SOURCE, artifact, None, require_buildout_preseal=True)
        assert "--checkpoint-reject-buildout-holds" in launched[0]


def test_probe_result_refuses_foreign_duplicate_or_incomplete_scope(tmp_path):
    _run, artifact, _, manifest, _ = setup(tmp_path)
    partition = prepare_partition(artifact.read_bytes(), manifest, SOURCE, code_sha=CODE, dirty=False,
                                  target_sha=TARGET, attempt_id="a" * 32)
    good = {"brokerages": "1", "staged": str(len(partition["staged"])), "held": "[]"}
    assert classify([good], partition) == []
    for row in ({**good, "brokerages": "0"}, {**good, "staged": "0"},
                {**good, "held": '["foreign"]'}, {**good, "held": '["dual", "dual"]'}):
        with pytest.raises(ValueError):
            classify([row], partition)


def test_malformed_full_inventory_is_never_a_clear_probe(tmp_path, monkeypatch):
    run, artifact, _payload, manifest, _ = setup(tmp_path)
    manifest["started_at"] = "2000-01-01T00:00:00Z"
    monkeypatch.setattr(refresh, "git_identity", lambda: (CODE, False))
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _: {"value": TARGET})
    monkeypatch.setattr(pipeline, "require_pipeline_owner", lambda *_: None)
    monkeypatch.setattr(refresh, "save_manifest", lambda *_: None)
    coverage = run / "coverage"
    coverage.write_text("stable")
    gate = {"verdict": "ok"}
    manifest["sources"][SOURCE]["gate"] = gate
    assert not pipeline.preseal_buildout_identity(run, manifest, SOURCE, artifact, None, coverage,
                                                  refresh.sha256_file(coverage), gate)
    assert manifest["sources"][SOURCE]["partial_probe_hold"] == "buildout_full_inventory_invalid"


def test_unsettled_prior_probe_cannot_be_silently_replaced(tmp_path, monkeypatch):
    run, artifact, _payload, manifest, _ = setup(tmp_path)
    manifest["sources"][SOURCE]["identity_preseal_attempts"] = [{"state": "running"}]
    monkeypatch.setattr(refresh, "git_identity", lambda: (CODE, False))
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _: {"value": TARGET})
    monkeypatch.setattr(pipeline, "require_pipeline_owner", lambda *_: None)
    with pytest.raises(refresh.GlobalStageError, match="settlement is unknown"):
        pipeline.preseal_buildout_identity(run, manifest, SOURCE, artifact, None, run / "coverage", "x", {})
