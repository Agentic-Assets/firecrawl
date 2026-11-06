"""Source-local DAUM all-held result crosses the real private compute worker."""
import copy

import pytest

import cre_checkpoint_refresh as refresh
import cre_daum_current_inventory as daum
import cre_refresh_pipeline as pipeline
from test_daum_verified_subset import admit, complete, subset  # noqa: F401


@pytest.mark.parametrize("malformed", [False, True])
def test_all_held_compute_result_reaches_preseal_without_write(subset, monkeypatch, malformed):
    run_dir, artifact, _data, manifest, report = subset
    import test_essex_current_inventory as helpers
    for entry in _data["sources"]:
        for key in ("listings", "inventoryObserved", "detailObserved"):
            entry["freshness"][key] = entry["listingsCollected"]
    helpers.refresh(artifact, _data, manifest)
    original_report = copy.deepcopy(report)
    admit(subset)
    report.clear()
    report.update(original_report)
    probe = manifest["sources"][daum.SOURCE]["daum_identity_probe"]
    for item in probe["partition"]:
        item["conflicted"] = True
    if malformed:
        probe["partition"][0]["conflicted"] = "true"
    monkeypatch.setattr(pipeline, "require_pipeline_owner", lambda *_args: None)

    def gate(run, owner, source, path, _env):
        assert source == daum.SOURCE
        refresh._admit_current_inventory(run, owner, report, path, "source", source=source)
    monkeypatch.setattr(refresh, "gate_source", gate)
    with pipeline.artifact_compute_scope(run_dir):
        if malformed:
            with pytest.raises(refresh.GlobalStageError, match="artifact computation failed"):
                pipeline.preseal_source_gate(run_dir, manifest, daum.SOURCE, None)
            assert manifest["sources"][daum.SOURCE].get("state") != "gate_blocked"
        else:
            assert pipeline.preseal_source_gate(run_dir, manifest, daum.SOURCE, None) is False
            checkpoint = manifest["sources"][daum.SOURCE]
            assert checkpoint["state"] == "gate_blocked"
            assert checkpoint["partial_probe_hold"] == "source_identity_partition_invalid"
            assert checkpoint.get("ingest") is None
    responses = list((run_dir / "pipeline" / "compute").glob("*.response.json"))
    assert len(responses) == 1
    import json
    response = json.loads(responses[0].read_text())
    assert response.get("daum_all_identities_held") is (None if malformed else True)


@pytest.mark.parametrize("action,source,phase", [
    ("hash", "daum-commercial", "source"),
    ("admit", "cbre", "source"),
    ("admit", "daum-commercial", "aggregate"),
])
def test_all_held_marker_cannot_cross_foreign_compute_scope(tmp_path, monkeypatch, action, source, phase):
    import json
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    def command(argv, _log, **_kwargs):
        request = json.loads(Path(argv[argv.index("--request") + 1]).read_text())
        response = Path(argv[argv.index("--response") + 1])
        response.write_text(json.dumps({"request_id": request["request_id"], "ok": False,
                                        "daum_all_identities_held": True}))
        return 0
    from pathlib import Path
    monkeypatch.setattr(refresh, "run_command", command)
    with pytest.raises(refresh.GlobalStageError, match="artifact computation failed"):
        pipeline.run_artifact_compute(run, action, {"source": source, "phase": phase})
