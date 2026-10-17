"""Saved serial evidence qualifies metrics; no database or provider calls."""

import copy
import json
from datetime import datetime, timedelta

import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series
import cre_current_inventory_admission as admission
import cre_performance_report as performance
import cre_validate
import pytest
from test_current_inventory_admission import setup as interra_setup
from test_cre_checkpoint_refresh import strict_artifact, freshness_generation_row

SHA = "a" * 40


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def fixture(tmp_path, *, source="interra-realty", parent=False):
    if source == "interra-realty":
        run, artifact_path, payload, manifest, gate = interra_setup(tmp_path)
        policy = admission.configuration(source)
        admit = admission.admit(
            artifact_path,
            manifest,
            gate,
            current_code_sha=SHA,
            current_git_dirty=False,
            phase="source",
        )
        gate["per_source"][source].update(admit)
    else:
        run = tmp_path / "2026-07-29T120000Z-0123456789ab"
        run.mkdir()
        manifest = refresh.new_manifest(
            run,
            git_sha=SHA,
            git_dirty=False,
            sources=[source],
            page_cap=400,
            concurrency=1,
            database_target={"algorithm": "sha256", "value": "b" * 64},
        )
        manifest["started_at"] = "2026-07-29T12:00:00+00:00"
        payload = strict_artifact(source=source, generation=run.name)
        artifact_path = run / "artifact.json"
        write(artifact_path, payload)
        stats = refresh.validate_source_artifact(
            artifact_path,
            source,
            manifest["started_at"],
            expected_generation_id=run.name,
            expected_generation_started_at=manifest["started_at"],
        )
        manifest["sources"][source]["artifact"] = {
            **stats,
            "path": "artifact.json",
            "attempt_started_at": manifest["started_at"],
        }
        gate = {
            "generated_for_artifact": str(artifact_path),
            "per_source": {
                source: {
                    "verdict": "ok",
                    "current_active": 2,
                    "mark_missing_safe": False,
                }
            },
            "summary": {
                "mode": "live",
                "torow_errors": 0,
                "hold_sources": [],
                "mark_missing_safe_brokerages": [],
            },
        }
        policy = None
    started = datetime.fromisoformat(manifest["started_at"])
    artifact = manifest["sources"][source]["artifact"]
    observed = artifact["finished_at"]
    finished = datetime.fromisoformat(observed.replace("Z", "+00:00")) + timedelta(
        seconds=20
    )
    manifest.update(
        status="additive_scope_complete_current_inventory"
        if policy
        else "supported_scope_complete",
        error=None,
        resource_stop=None,
        finished_at=finished.isoformat(),
    )
    if policy:
        manifest["scope"] = {
            "kind": "collector_registry_additive_current_inventory",
            "source_keys": [source],
            "whole_source_coverage": False,
        }
    checkpoint = manifest["sources"][source]
    checkpoint.update(
        state="ingested",
        dry_run={"rc": 0},
        ingest={
            "rc": 0,
            "started_at": (finished - timedelta(seconds=15)).isoformat(),
            "finished_at": (finished - timedelta(seconds=10)).isoformat(),
            "mark_missing": False,
            "status_activation": False,
            "log": f"logs/{source}-ingest.log",
        },
        gate={"path": f"gates/{source}.json", "rc": 0, **gate["per_source"][source]},
    )
    (run / "gates").mkdir()
    (run / "logs").mkdir()
    (run / f"logs/{source}-ingest.log").write_text(
        f"[{checkpoint['ingest']['started_at']}] command: /usr/bin/python3 cre_ingest.py --in {artifact_path} --expected-db-target-sha256 {'b' * 64}\ningest complete\n[{checkpoint['ingest']['finished_at']}] rc=0\n"
    )
    write(run / f"gates/{source}.json", gate)
    aggregate = copy.deepcopy(gate)
    write(run / "aggregate-gate.json", aggregate)
    manifest["aggregate_gate"] = {
        "path": "aggregate-gate.json",
        "rc": 0,
        "hold_sources": [],
        "non_ok_sources": [],
        "baseline_advisory_holds": [],
        "mark_missing_safe_brokerages": [],
    }
    validation = {
        "ok": True,
        "validation_profile": "full",
        "audit_scope": cre_validate.VALIDATION_AUDIT_SCOPES["full"],
        "generated_at": (finished - timedelta(seconds=5)).isoformat(),
        "historical_primary_child_conflicts": [],
        "queries": {key: [] for key in cre_validate.QUERIES},
    }
    validation["queries"]["freshness_generations"] = [
        freshness_generation_row(
            source,
            generation_id=run.name,
            earliest_inventory_observed_at=payload["listings"][0][
                "inventoryObservedAt"
            ],
            latest_inventory_observed_at=payload["listings"][0]["inventoryObservedAt"],
        )
    ]
    validation["queries"]["source_counts"] = [
        {
            "source_key": source,
            "latest_inventory_observed_at": observed,
            "latest_inventory_batch_active": "2",
            "latest_scraped_at": observed,
            "latest_batch_active": "2",
        }
    ]
    validation["queries"]["inventory_generation_fingerprints"] = [
        {
            "source_key": key,
            "row_count": "2" if key == source else "0",
            "active_row_count": "2",
            "classified_row_count": "2",
            "unclassified_row_count": "0",
            "max_observation_at": observed,
            "max_row_updated_at": observed,
        }
        for key in refresh.SOURCE_KEYS
    ]
    assert refresh.verify_validation_readback(
        run, manifest, validation, now=finished, persist_manifest=False
    )["ok"], manifest["sources"][source].get("readback")
    write(run / "validation.json", validation)
    before = {
        **validation,
        "validation_profile": "checkpoint-preflight",
        "audit_scope": cre_validate.VALIDATION_AUDIT_SCOPES["checkpoint-preflight"],
        "generated_at": started.isoformat(),
        "queries": {
            key: validation["queries"][key]
            for key in cre_validate.CHECKPOINT_PREFLIGHT_QUERY_NAMES
        },
    }
    write(run / "pre-validation.json", before)
    manifest["preflight"].update(validation_path="pre-validation.json")
    manifest["validation"] = {
        "path": "validation.json",
        "rc": 0,
        "query_execution_ok": True,
        "quality_no_regression": True,
        "readback_ok": True,
        "failed_readback_sources": [],
    }
    write(run / "manifest.json", manifest)
    if not parent:
        return run, manifest, finished
    config = series.series_config(
        sources=[source],
        page_cap=400,
        concurrency=1,
        attempts_per_source=1,
        max_resume_age_hours=24,
        max_host_cpu_percent=90,
        cpu_sustain_seconds=30,
        cpu_sample_seconds=2,
        nice=15,
        admit_current_inventory_source=source if policy else None,
    )
    # Match an actual series-produced child configuration, not invented approval.
    manifest["config"] = series._expected_child_config(source, config)
    write(run / "manifest.json", manifest)
    root = tmp_path / "2026-07-29T115959Z"
    (root / "runs").mkdir(parents=True)
    import shutil

    destination = root / "runs" / run.name
    shutil.move(run, destination)
    log = destination / f"logs/{source}-ingest.log"
    log.write_text(
        log.read_text().replace(
            str(run / "artifact.json"), str(destination / "artifact.json")
        )
    )
    # Gate references in saved reports are their original generation artifact path.
    for name in [f"gates/{source}.json", "aggregate-gate.json"]:
        raw = json.loads((destination / name).read_text())
        raw["generated_for_artifact"] = str(destination / "artifact.json")
        write(destination / name, raw)
    write(
        root / "manifest.json",
        {
            "schema_version": 1,
            "series_id": root.name,
            "collector_git_sha": SHA,
            "status": "complete",
            "started_at": manifest["started_at"],
            "finished_at": finished.isoformat(),
            "database_target": manifest["preflight"]["database_target"],
            "config": config,
            "sources": {
                source: {
                    "state": "complete",
                    "checkpoint_status": manifest["status"],
                    "checkpoint_run": f"runs/{destination.name}",
                    "attempts": [
                        {
                            "started_at": manifest["started_at"],
                            "finished_at": finished.isoformat(),
                            "rc": 0,
                        }
                    ],
                }
            },
        },
    )
    return root, manifest, finished


@pytest.mark.parametrize("source", ["svn", "interra-realty"])
@pytest.mark.parametrize("parent", [False, True])
def test_serial_verified_inventory_uses_saved_canonical_proof(tmp_path, source, parent):
    run, manifest, finished = fixture(tmp_path, source=source, parent=parent)
    result = performance.build_report(run, now=finished)
    row = result["sources"][0]
    assert row["verified_current_inventory_unique"] == 2
    assert row["verified_current_inventory_basis"] == "serial_generation_full_readback"
    assert row["fresh_qualified_unique"] is None
    assert manifest["sources"][source]["readback"]["ok"]


@pytest.mark.parametrize(
    "fault",
    [
        "running",
        "error",
        "resource_stop",
        "dirty",
        "ingest_rc",
        "ingest_unknown",
        "dry_run",
        "no_regression",
        "missing_readback",
        "wrong_readback_count",
        "wrong_generation",
        "missing_family",
        "profile",
        "report_time",
        "missing_report",
        "artifact_hash",
        "artifact_bytes",
        "artifact_count_bool",
        "artifact_body",
        "source_gate",
        "aggregate_gate",
        "gate_target",
        "gate_policy",
        "target_missing",
        "target",
        "partial_zero_holds",
        "coverage_hold",
        "wrong_transactions",
        "before_quality",
        "ingest_log_rc",
        "ingest_log_target",
        "report_hash",
        "unsafe_report_path",
        "stale_detail",
    ],
)
def test_invalid_serial_evidence_never_gains_verified_count(tmp_path, fault):
    run, manifest, finished = fixture(
        tmp_path, source="svn" if fault == "target" else "interra-realty"
    )
    source = manifest["config"]["sources"][0]
    checkpoint = manifest["sources"][source]
    if fault == "running":
        manifest["status"] = "running"
    elif fault == "error":
        manifest["error"] = "failed"
    elif fault == "resource_stop":
        manifest["resource_stop"] = {"reason": "unknown"}
    elif fault == "dirty":
        manifest["collector_git_dirty"] = True
    elif fault == "ingest_rc":
        checkpoint["ingest"]["rc"] = 1
    elif fault == "ingest_unknown":
        checkpoint["ingest_recovery"] = {"required": True}
    elif fault == "dry_run":
        checkpoint["dry_run"]["rc"] = None
    elif fault == "no_regression":
        manifest["validation"]["quality_no_regression"] = False
    elif fault == "missing_readback":
        checkpoint.pop("readback")
    elif fault == "wrong_readback_count":
        checkpoint["readback"]["expected_staged_unique"] = 3
    elif fault == "target_missing":
        manifest["preflight"].pop("database_target")
    elif fault == "target":
        manifest["preflight"]["database_target"]["value"] = "c" * 64
    elif fault == "artifact_hash":
        checkpoint["artifact"]["sha256"] = "c" * 64
    elif fault == "artifact_bytes":
        checkpoint["artifact"]["bytes"] += 1
    elif fault == "artifact_count_bool":
        checkpoint["artifact"]["staged_unique"] = True
    elif fault == "artifact_body":
        (run / "artifact.json").write_text("{}")
    elif fault in {"source_gate", "aggregate_gate", "gate_target", "gate_policy"}:
        path = run / (
            f"gates/{source}.json"
            if fault != "aggregate_gate"
            else "aggregate-gate.json"
        )
        value = json.loads(path.read_text())
        if fault in {"source_gate", "aggregate_gate"}:
            value["per_source"][source]["verdict"] = "hold"
        elif fault == "gate_target":
            value["per_source"][source]["current_inventory_proof"][
                "database_target_sha256"
            ] = "c" * 64
        else:
            manifest["config"]["current_inventory_admission"][
                "source_policy_sha256"
            ] = "c" * 64
        write(path, value)
    elif fault in {
        "wrong_generation",
        "missing_family",
        "profile",
        "report_time",
        "stale_detail",
    }:
        path = run / "validation.json"
        value = json.loads(path.read_text())
        if fault == "wrong_generation":
            value["queries"]["freshness_generations"][0]["generation_id"] = "different"
        elif fault == "missing_family":
            value["queries"].pop("search_smoke")
        elif fault == "profile":
            value["validation_profile"] = "checkpoint-preflight"
        elif fault == "report_time":
            value["generated_at"] = (finished + timedelta(seconds=1)).isoformat()
        else:
            value["queries"]["freshness_generations"][0][
                "earliest_inventory_observed_at"
            ] = (
                datetime.fromisoformat(manifest["started_at"]) - timedelta(seconds=1)
            ).isoformat()
        write(path, value)
    elif fault == "missing_report":
        (run / "validation.json").unlink()
    elif fault == "partial_zero_holds":
        manifest["status"] = "qualified_partial_subset"
        manifest["scope"] = {
            "kind": "collector_registry_verified_subset",
            "held_count": 0,
            "whole_source_coverage": False,
        }
    elif fault == "coverage_hold":
        manifest["status"] = "additive_scope_complete_coverage_hold"
        manifest["config"]["admit_baseline_hold_additively"] = True
    elif fault == "wrong_transactions":
        manifest["config"]["transactions"] = ["sale"]
    elif fault == "before_quality":
        path = run / "validation.json"
        value = json.loads(path.read_text())
        value["queries"]["quality_by_source"] = [
            {"source_key": source, "bad_source_url": "1"}
        ]
        write(path, value)
    elif fault in {"ingest_log_rc", "ingest_log_target"}:
        path = run / checkpoint["ingest"]["log"]
        path.write_text(
            path.read_text().replace("rc=0", "rc=1")
            if fault == "ingest_log_rc"
            else path.read_text().replace("b" * 64, "c" * 64)
        )
    elif fault == "report_hash":
        manifest["validation"]["sha256"] = "c" * 64
    elif fault == "unsafe_report_path":
        manifest["validation"]["path"] = "../outside.json"
    write(run / "manifest.json", manifest)
    assert (
        performance.build_report(run, now=finished)["sources"][0].get(
            "verified_current_inventory_unique"
        )
        is None
    )


@pytest.mark.parametrize(
    "fault", ["target", "sha", "config", "attempt", "failed", "path"]
)
def test_serial_parent_cannot_borrow_child_qualification(tmp_path, fault):
    run, _, finished = fixture(tmp_path, parent=True)
    path = run / "manifest.json"
    parent = json.loads(path.read_text())
    checkpoint = parent["sources"]["interra-realty"]
    if fault == "target":
        parent["database_target"]["value"] = "c" * 64
    elif fault == "sha":
        parent["collector_git_sha"] = "c" * 40
    elif fault == "config":
        parent["config"]["current_inventory_admission"]["source_policy_sha256"] = (
            "c" * 64
        )
    elif fault == "attempt":
        checkpoint["attempts"][-1]["rc"] = 1
    elif fault == "failed":
        checkpoint["state"] = "failed_global"
    else:
        checkpoint["checkpoint_run"] = "../outside"
    write(path, parent)
    assert (
        performance.build_report(run, now=finished)["sources"][0].get(
            "verified_current_inventory_unique"
        )
        is None
    )


def test_report_qualification_never_rewrites_saved_manifest_or_validation(tmp_path):
    run, _, finished = fixture(tmp_path)
    before = {p: p.read_bytes() for p in run.rglob("*.json")}
    assert (
        performance.build_report(run, now=finished + timedelta(days=10))["sources"][0][
            "verified_current_inventory_unique"
        ]
        == 2
    )
    assert all(p.read_bytes() == raw for p, raw in before.items())


@pytest.mark.parametrize(
    "fault", ["wrong_artifact", "duplicate_target", "missing_target", "dry_run"]
)
def test_ingest_command_binding_is_exact_and_never_executed(tmp_path, fault):
    run, manifest, finished = fixture(tmp_path, source="svn")
    path = run / manifest["sources"]["svn"]["ingest"]["log"]
    raw = path.read_text()
    if fault == "wrong_artifact":
        raw = raw.replace(str(run / "artifact.json"), str(run / "other.json"))
    elif fault == "duplicate_target":
        raw = raw.replace(
            "\ningest complete",
            " --expected-db-target-sha256 " + "b" * 64 + "\ningest complete",
        )
    elif fault == "missing_target":
        raw = raw.replace("--expected-db-target-sha256 " + "b" * 64, "")
    else:
        raw = raw.replace("\ningest complete", " --dry-run\ningest complete")
    path.write_text(raw)
    assert (
        performance.build_report(run, now=finished)["sources"][0][
            "verified_current_inventory_unique"
        ]
        is None
    )


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "ancestor_link"])
def test_aliased_validation_bytes_refuse(tmp_path, kind):
    run, manifest, finished = fixture(tmp_path)
    validation = run / "validation.json"
    moved = run / "original.json"
    validation.rename(moved)
    if kind == "symlink":
        validation.symlink_to(moved)
    elif kind == "hardlink":
        import os

        os.link(moved, validation)
    else:
        nested = run / "nested"
        nested.mkdir()
        moved.rename(nested / "validation.json")
        (run / "alias").symlink_to(nested, target_is_directory=True)
        manifest["validation"]["path"] = "alias/validation.json"
        write(run / "manifest.json", manifest)
    assert (
        performance.build_report(run, now=finished)["sources"][0][
            "verified_current_inventory_unique"
        ]
        is None
    )


def test_changed_report_during_qualification_refuses(tmp_path, monkeypatch):
    import cre_serial_qualification as qualification

    run, _, finished = fixture(tmp_path)
    original = qualification._read_regular_file
    calls = []

    def changed(path, **kwargs):
        raw = original(path, **kwargs)
        if path.name == "validation.json":
            calls.append(path)
            if len(calls) == 1:
                path.write_bytes(raw + b" ")
        return raw

    monkeypatch.setattr(qualification, "_read_regular_file", changed)
    assert (
        performance.build_report(run, now=finished)["sources"][0][
            "verified_current_inventory_unique"
        ]
        is None
    )


def test_savills_coverage_hold_remains_explicitly_partial(tmp_path):
    run, _, finished = fixture(tmp_path, source="svn")
    path = run / "manifest.json"
    value = json.loads(path.read_text())
    value["status"] = "additive_scope_complete_coverage_hold"
    value["config"]["sources"] = ["savills"]
    value["config"]["admit_baseline_hold_additively"] = True
    value["sources"]["savills"] = value["sources"].pop("svn")
    value["scope"] = {
        "kind": "collector_registry_additive_coverage_hold",
        "whole_source_coverage": False,
    }
    write(path, value)
    result = performance.build_report(run, now=finished)
    assert result["sources"][0]["verified_current_inventory_unique"] is None
    assert result["run_status"] == "additive_scope_complete_coverage_hold"
    assert result["sources"][0]["status"] == "additive_scope_complete_coverage_hold"


@pytest.mark.parametrize(
    "field,value",
    [
        ("transaction_breakdown", None),
        ("strict_freshness", False),
        ("detail_errors", 1),
        ("freshness_generation_started_at", "2025-01-01T00:00:00+00:00"),
    ],
)
def test_compact_staging_binding_is_required(tmp_path, field, value):
    run, manifest, _ = fixture(tmp_path)
    manifest["sources"]["interra-realty"]["artifact"][field] = value
    write(run / "manifest.json", manifest)
    assert (
        performance.build_report(run)["sources"][0]["verified_current_inventory_unique"]
        is None
    )


def test_metric_streams_artifact_without_decoding_or_staging(tmp_path, monkeypatch):
    run, _, _ = fixture(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("historical metric must not decode or re-stage artifact")

    monkeypatch.setattr(refresh, "_manifest_checkpoint_artifact_valid", forbidden)
    monkeypatch.setattr(refresh, "validate_source_artifact", forbidden)
    assert (
        performance.build_report(run)["sources"][0]["verified_current_inventory_unique"]
        == 2
    )


def test_compact_extraction_preserves_live_notification_age_gate(tmp_path, monkeypatch):
    import cre_notification_summary as notification

    run, manifest, _ = fixture(tmp_path)
    artifact = manifest["sources"]["interra-realty"]["artifact"]
    finished = datetime.fromisoformat(artifact["finished_at"]).timestamp()
    args = (artifact, "interra-realty", 2, manifest["run_id"])
    monkeypatch.setattr(notification.time, "time", lambda: finished + 86400)
    assert notification._staged_transaction_breakdown(
        *args
    ) == notification.bound_staged_transaction_breakdown(*args)
    for now in (finished - 1, finished + 86401):
        monkeypatch.setattr(notification.time, "time", lambda: now)
        with pytest.raises(notification.SummaryUnavailable):
            notification._staged_transaction_breakdown(*args)
        assert notification.bound_staged_transaction_breakdown(*args) is not None
