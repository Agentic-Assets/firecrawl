import json
from types import SimpleNamespace

import pytest

import cre_checkpoint_refresh as refresh
import cre_ingest
import cre_refresh_pipeline
import cre_validate
from source_fields.buildout_observation_sql import readback
from source_fields.buildout import observation_sources
from test_buildout_current_inventory import CODE, SOURCE, TARGET, setup


def test_owner_binds_complete_artifact_and_partial_does_not_block_ingest(tmp_path, monkeypatch):
    run, path, artifact, manifest, _ = setup(tmp_path)
    monkeypatch.setattr(cre_refresh_pipeline, "require_pipeline_owner", lambda *_: None)
    monkeypatch.setattr(refresh, "git_identity", lambda: (CODE, False))
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _: {"value": TARGET})
    binding = refresh.prepare_buildout_observation(run, manifest, SOURCE, path, None)
    assert binding["scope"]["artifact_sha256"] == manifest["sources"][SOURCE]["artifact"]["sha256"]
    assert json.loads((run / binding["path"]).read_text())["source"] == SOURCE
    artifact["sources"][0]["currentInventoryProof"]["pages"].pop()
    path.write_text(json.dumps(artifact))
    assert refresh.prepare_buildout_observation(run, manifest, SOURCE, path, None) is None


@pytest.mark.parametrize("source", sorted(observation_sources()))
def test_each_singleton_source_binds_its_own_feed_and_rejects_cross_source(tmp_path, monkeypatch, source):
    run, path, _, manifest, _ = setup(tmp_path, source=source)
    monkeypatch.setattr(cre_refresh_pipeline, "require_pipeline_owner", lambda *_: None)
    monkeypatch.setattr(refresh, "git_identity", lambda: (CODE, False))
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _: {"value": TARGET})
    binding = refresh.prepare_buildout_observation(run, manifest, source, path, None)
    assert binding["scope"]["source_key"] == source
    other = next(item for item in sorted(observation_sources()) if item != source)
    manifest["sources"][other] = manifest["sources"][source]
    assert refresh.prepare_buildout_observation(run, manifest, other, path, None) is None


@pytest.mark.parametrize("case", ["zero", "events_mismatch", "missing", "wrong_generation", "held_mismatch", "wrong_identity", "observed_native"])
def test_readback_requires_bound_receipt_even_when_zero(case, monkeypatch):
    scope = {"database_target_sha256": TARGET, "artifact_sha256": "c" * 64,
             "source_key": SOURCE, "generation_id": "run_1", "native_ids": ["10"]}
    receipt = {"source": SOURCE, "generation": "run_1", "artifact_sha256": "c" * 64,
               "disappeared": 0, "identity_held": 0, "held_external_ids": [], "applied": []}
    if case == "wrong_generation":
        receipt["generation"] = "other"
    if case == "held_mismatch":
        receipt["identity_held"] = 1
    if case in {"wrong_identity", "observed_native"}:
        receipt["disappeared"] = 1
        receipt["applied"] = [{"listing_id": "id", "external_id": "key", "native_ids": ["10" if case == "observed_native" else "11"]}]
    notes = "job\nCRE_BUILDOUT_OBSERVATION_V1:" + json.dumps(receipt)
    value = {"notes": notes if case != "missing" else "job",
             "events": 1 if case in {"events_mismatch", "wrong_identity", "observed_native"} else 0,
             "applied": receipt["applied"] if case != "wrong_identity" else [{"listing_id": "other", "external_id": "key", "native_ids": ["11"]}]}
    monkeypatch.setattr(cre_ingest, "load_db_url", lambda _: ("db", "env"))
    monkeypatch.setattr(cre_ingest, "database_target_fingerprint_from_url", lambda _: {"value": TARGET})
    monkeypatch.setattr(cre_ingest, "find_psql", lambda: "psql")
    monkeypatch.setattr(cre_validate, "_run_psql_script", lambda *a, **k: SimpleNamespace(returncode=0, stdout=""))
    monkeypatch.setattr(cre_validate, "parse_tsv", lambda _: [{"result": json.dumps(value)}])
    if case == "zero":
        assert readback(scope, None) == receipt
    else:
        with pytest.raises(ValueError):
            readback(scope, None)


@pytest.mark.parametrize("receipt_ok", [True, False])
@pytest.mark.parametrize("source", [SOURCE, "fortis-net-lease", "33-realty"])
def test_interrupted_commit_requires_observation_receipt(tmp_path, monkeypatch, receipt_ok, source):
    run, _, _, manifest, _ = setup(tmp_path, source=source)
    checkpoint = manifest["sources"][source]
    checkpoint.update(state="ingesting", ingest={"buildout_observation": {"scope": {}}})
    def run_validation(argv, _log, **_kwargs):
        from pathlib import Path
        refresh.atomic_write_json(Path(argv[argv.index("--out") + 1]),
                                  {"queries": {"artifact_run_jobs": [{"matching_jobs": "1"}]}})
        return 0
    monkeypatch.setattr(refresh, "run_command", run_validation)
    monkeypatch.setattr(refresh, "require_full_validation_report", lambda _: None)
    monkeypatch.setattr(refresh, "verify_validation_readback", lambda *a, **k: {"ok": True})
    def observation(*_):
        if not receipt_ok:
            raise ValueError("receipt missing")
        return {"disappeared": 0}
    monkeypatch.setattr(refresh, "buildout_observation_readback", observation)
    if receipt_ok:
        refresh.recover_interrupted_ingest(run, manifest, source, None)
        assert checkpoint["state"] == "ingested"
        assert checkpoint["ingest"]["buildout_observation_readback"] == {"disappeared": 0}
    else:
        with pytest.raises(refresh.GlobalStageError, match="exact readback"):
            refresh.recover_interrupted_ingest(run, manifest, source, None)
        assert checkpoint["state"] == "ingest_recovery_required"
        assert checkpoint["ingest_recovery"]["readback_ok"] is False
