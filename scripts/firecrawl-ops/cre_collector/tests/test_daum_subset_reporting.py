"""Partial DAUM receipts remain qualified without complete-source credit."""

import copy
import pytest
import cre_checkpoint_series as series
import cre_current_inventory_admission as admission
import test_notification_summary as support

SOURCE = "daum-commercial"


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(support, "SOURCE", SOURCE)
    e = support.evidence.__wrapped__(tmp_path, monkeypatch)
    parent, child = e["values"]["parent"], e["values"]["child"]
    config = series.series_config(
        sources=[SOURCE],
        page_cap=400,
        concurrency=1,
        attempts_per_source=3,
        max_resume_age_hours=24,
        max_host_cpu_percent=90,
        cpu_sustain_seconds=5,
        cpu_sample_seconds=1,
        nice=10,
        admit_daum_verified_subset=True,
    )
    parent["config"] = config
    child["config"] = series._expected_child_config(SOURCE, config)
    parent["status"] = child["status"] = "qualified_partial_subset"
    e["values"]["control"]["status"] = "qualified_partial_subset"
    parent["sources"][SOURCE].update(
        state="qualified_partial_subset", checkpoint_status=child["status"]
    )
    child["scope"] = {
        "kind": "collector_registry_verified_subset",
        "whole_source_coverage": False,
    }
    target = {"algorithm": "sha256", "value": "d" * 64}
    parent["database_target"] = child["preflight"]["database_target"] = target
    checkpoint = child["sources"][SOURCE]
    artifact = checkpoint["artifact"]
    artifact.update(
        staged_unique=100,
        freshness_generation_id=child["run_id"],
        finished_at=child["finished_at"],
    )
    artifact["transaction_breakdown"] = {
        "sale": 100,
        "lease": 0,
        "both": 0,
        "measurement": "current_staged_inventory_transaction_types",
        "source_key": SOURCE,
        "artifact_sha256": artifact["sha256"],
        "artifact_bytes": artifact["bytes"],
        "generation_id": child["run_id"],
    }
    checkpoint["readback"].update(
        expected_staged_unique=100, latest_inventory_batch_active=100
    )
    binding = {
        "schema": "daum-verified-subset-v1",
        "scope": admission.DAUM_SUBSET_SCOPE,
        "whole_source_coverage": False,
        "generation_id": child["run_id"],
        "collector_git_sha": child["collector_git_sha"],
        "artifact_sha256": artifact["sha256"],
        "database_target_sha256": target["value"],
        "source_policy_sha256": admission.subset_configuration()[
            "source_policy_sha256"
        ],
        "inventory_count": 102,
        "emitted_count": 100,
        "held_count": 2,
        "staged_count": 100,
        "whole_lease_coverage": "unknown",
    }
    checkpoint["gate"] = {
        "verdict": admission.DAUM_SUBSET_VERDICT,
        "raw_verdict": "first_seen",
        "admission_scope": admission.DAUM_SUBSET_SCOPE,
        "current_inventory_proof": binding,
    }
    e["save"]()
    return e


@pytest.mark.parametrize("holds", [0, 2])
def test_serial_real_private_reader_always_partial(evidence, holds):
    child = evidence["values"]["child"]
    binding = child["sources"][SOURCE]["gate"]["current_inventory_proof"]
    binding.update(
        held_count=holds,
        inventory_count=100 + holds,
        whole_lease_coverage="unknown" if holds else "proved",
    )
    evidence["save"]()
    result = support.read(evidence)
    assert result["measurement"] == "generation_exact_verified_subset"
    assert result["qualification"] == "qualified_partial_subset"
    assert result["partial"] is True and result["whole_source_coverage"] is False
    assert result["held_count"] == holds and result["emitted_count"] == 100
    assert result["transaction_breakdown"]["lease"] == 0
    assert result["whole_lease_coverage"] == ("unknown" if holds else "proved")


@pytest.mark.parametrize(
    "fault",
    [
        "scope",
        "bool",
        "count",
        "hash",
        "generation",
        "target",
        "policy",
        "full_label",
        "readback",
        "settlement",
        "breakdown",
        "under_floor",
        "control_full_label",
    ],
)
def test_subset_summary_refuses_unqualified_or_borrowed_evidence(evidence, fault):
    child = evidence["values"]["child"]
    cp = child["sources"][SOURCE]
    b = cp["gate"]["current_inventory_proof"]
    if fault == "scope":
        b["scope"] = "additive_current_inventory"
    elif fault == "bool":
        b["held_count"] = True
    elif fault == "count":
        b["inventory_count"] += 1
    elif fault == "hash":
        b["artifact_sha256"] = "e" * 64
    elif fault == "generation":
        b["generation_id"] = "other"
    elif fault == "target":
        b["database_target_sha256"] = "e" * 64
    elif fault == "policy":
        b["source_policy_sha256"] = "e" * 64
    elif fault == "full_label":
        child["status"] = "supported_scope_complete"
    elif fault == "readback":
        cp["readback"]["ok"] = False
    elif fault == "settlement":
        evidence["values"]["settlement"]["nested_cleanup_confirmed"] = False
    elif fault == "breakdown":
        cp["artifact"]["transaction_breakdown"]["generation_id"] = "other"
    elif fault == "control_full_label":
        evidence["values"]["control"]["status"] = "complete"
    elif fault == "under_floor":
        b.update(emitted_count=99, inventory_count=101, staged_count=99)
    evidence["save"]()
    with pytest.raises(support.REFUSED):
        support.read(evidence)


def test_mixed_pipeline_partitions_subset_without_full_complete_credit(evidence):
    setup = support.pipeline_evidence(evidence, primary_source=SOURCE)
    support.qualify_pipeline_second_source(evidence, setup)
    parent, child = setup["parent"], setup["child"]
    for config in [parent["config"], child["config"]]:
        config.pop("current_inventory_admission", None)
        config["current_inventory_admissions"] = {
            SOURCE: admission.subset_configuration()
        }
    parent["status"] = child["status"] = "qualified_partial_subset"
    parent["sources"][SOURCE]["state"] = "qualified_partial_subset"
    result = child["pipeline"]["source_results"][SOURCE]
    result.update(
        qualification="qualified_partial_subset",
        partial=True,
        whole_source_coverage=False,
    )
    parent["sources"][SOURCE]["pipeline_result"] = result
    for source in [SOURCE, "cbre"]:
        parent["sources"][source]["checkpoint_status"] = child["status"]
    other = child["sources"]["cbre"]
    other["artifact"]["staged_unique"] = 3
    other["readback"].update(expected_staged_unique=3, latest_inventory_batch_active=3)
    evidence["save"]()
    result = support.read_pipeline(evidence, [SOURCE, "cbre"])
    assert result["complete"] is False
    assert result["qualified_subset_source_keys"] == [SOURCE]
    rows = {row["source_key"]: row for row in result["per_source_summaries"]}
    assert rows[SOURCE]["partial"] is True and rows[SOURCE]["held_count"] == 2
    assert rows["cbre"]["verified_listings"] == 3 and "partial" not in rows["cbre"]


def test_handled_partial_serial_attempt_never_replays_on_resume(tmp_path, monkeypatch):
    config = series.series_config(
        sources=[SOURCE],
        page_cap=400,
        concurrency=1,
        attempts_per_source=3,
        max_resume_age_hours=24,
        max_host_cpu_percent=90,
        cpu_sustain_seconds=5,
        cpu_sample_seconds=1,
        nice=15,
        admit_daum_verified_subset=True,
    )
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=config)
    child = {
        "status": "qualified_partial_subset",
        "error": None,
        "config": series._expected_child_config(SOURCE, config),
        "scope": {
            "kind": "collector_registry_verified_subset",
            "whole_source_coverage": False,
        },
    }
    calls = []

    class Process:
        pid = 4321

        def __init__(self, *a, **kw):
            calls.append(a)

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(series.subprocess, "Popen", Process)
    monkeypatch.setattr(
        series,
        "_load_child_manifest",
        lambda *a, **kw: (tmp_path / "runs/child/manifest.json", child),
    )
    assert series.run_series(tmp_path, manifest, env_file=None, retry_failed=False) == 0
    assert len(calls) == 1 and manifest["status"] == "qualified_partial_subset"
    assert manifest["sources"][SOURCE]["state"] == "qualified_partial_subset"
    resumed = series.load_resume_manifest(
        tmp_path / "manifest.json", git_sha="a" * 40, config=config
    )
    assert series.run_series(tmp_path, resumed, env_file=None, retry_failed=False) == 0
    assert len(calls) == 1


def test_partial_cannot_publish_last_good_even_if_terminal_label_is_forged(tmp_path):
    import cre_inventory_generation as generation

    config = {
        "sources": [SOURCE],
        "transactions": ["sale", "lease"],
        "current_inventory_admission": admission.subset_configuration(),
    }
    with pytest.raises(generation.InventoryGenerationNotReady, match="verified subset"):
        generation.build_inventory_generation_receipt(
            tmp_path, {"status": "complete", "config": config}
        )


def test_status_consumer_counts_partial_handled_but_not_complete(tmp_path):
    import json
    import cre_series_status as status
    import test_cre_series_status as helper

    path = helper._manifest(tmp_path / "2026-09-13T110000Z")
    manifest = json.loads(path.read_text())
    manifest["status"] = "qualified_partial_subset"
    manifest["sources"]["cbre"]["state"] = "complete"
    manifest["sources"]["jll"]["state"] = "qualified_partial_subset"
    manifest["sources"]["svn"]["state"] = "complete"
    path.write_text(json.dumps(manifest))
    value = status.load_manifest(path)
    result = status.build_snapshot(value, path)
    assert result["complete"] == 2
    assert result["qualified_partial_subsets"] == 1


def test_certificate_explicit_scope_refuses_even_forged_complete_labels(tmp_path):
    import json
    import cre_freshness_certificate as certificate
    import test_cre_freshness_certificate as helper

    path = helper._run(tmp_path, SOURCE)
    manifest_path = path / "manifest.json" if path.is_dir() else path
    manifest = json.loads(manifest_path.read_text())
    manifest["config"]["current_inventory_admission"] = admission.subset_configuration()
    manifest["status"] = "supported_scope_complete"
    manifest["scope"]["kind"] = "collector_registry"
    manifest_path.write_text(json.dumps(manifest))
    result = certificate.build_freshness_certificate(
        [manifest_path], now=helper.NOW, max_source_age_hours=24
    )
    assert result["status"] != "valid"
    assert any(item["code"] == "verified_subset" for item in result["failures"])


def test_handled_pipeline_partial_requalifies_saved_unit_without_dispatch(
    tmp_path, monkeypatch
):
    import json
    import hashlib
    import test_pipeline_configuration as helper

    config = series.series_config(
        sources=[SOURCE],
        page_cap=400,
        concurrency=1,
        attempts_per_source=3,
        max_resume_age_hours=24,
        max_host_cpu_percent=90,
        cpu_sustain_seconds=5,
        cpu_sample_seconds=1,
        nice=15,
        pipeline=True,
        admit_daum_verified_subset=True,
    )
    run = tmp_path / "runs" / helper.RUN_ID
    run.mkdir(parents=True)
    child = json.loads(
        json.dumps(helper.qualified_child(run)).replace('"cbre"', '"daum-commercial"')
    )
    child.update(
        status="qualified_partial_subset",
        collector_git_sha="a" * 40,
        config=series._expected_child_config(SOURCE, config),
        scope={
            "kind": "collector_registry_verified_subset",
            "whole_source_coverage": False,
        },
    )
    result = child["pipeline"]["source_results"][SOURCE]
    result.update(
        qualification="qualified_partial_subset",
        partial=True,
        whole_source_coverage=False,
    )
    report_path = run / result["validation_path"]
    report = json.loads(report_path.read_text().replace('"cbre"', '"daum-commercial"'))
    raw = json.dumps(report).encode()
    report_path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    result["validation_sha256"] = digest
    child["pipeline"]["units"][0]["validation_sha256"] = digest
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=config)
    manifest.update(
        status="qualified_partial_subset",
        started_at=helper.PARENT_START,
        finished_at=helper.PARENT_END,
    )
    manifest["sources"][SOURCE].update(
        state="qualified_partial_subset",
        checkpoint_run="runs/" + helper.RUN_ID,
        attempts=copy.deepcopy(helper.PARENT_ATTEMPTS),
    )
    monkeypatch.setattr(
        series, "_load_child_manifest", lambda *a, **kw: (run / "manifest.json", child)
    )
    monkeypatch.setattr(
        series.subprocess,
        "Popen",
        lambda *a, **kw: pytest.fail("handled partial dispatched another source/apply"),
    )
    assert series.run_pipeline_series(tmp_path, manifest, env_file=None) == 0
    result["readback_ok"] = False
    with pytest.raises(series.SeriesError, match="no longer qualifies"):
        series.run_pipeline_series(tmp_path, manifest, env_file=None)
