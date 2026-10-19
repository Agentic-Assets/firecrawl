"""Real private-file receipt qualification; no provider or database access."""

import copy
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone

import cre_notification_summary as summary
import cre_validate
import pytest

SHA = "a" * 40
SOURCE = "svn"
INVOCATION = "b" * 32
CHILD = "c" * 32
SERIES = "2026-10-15T172000Z"
RUN = "2026-10-15T172001Z-123456abcdef"
REFUSED = (
    summary.SummaryUnavailable,
    OSError,
    ValueError,
    KeyError,
    TypeError,
    IndexError,
    summary.safe.StopRefused,
)


def iso(milliseconds):
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat()


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    collector = tmp_path / "collector"
    series = collector / "out/checkpoint-series" / SERIES
    child = series / "runs" / RUN
    for directory in (
        series / ".series-control",
        child / ".series-control",
        child / "sources",
    ):
        directory.mkdir(parents=True, mode=0o700)
        directory.chmod(0o700)
    monkeypatch.setattr(summary, "COLLECTOR", collector)
    now = int(time.time() * 1000)
    admission = now - 20000
    target = {"sha256": "d" * 64}
    files = {
        "parent": series / "manifest.json",
        "control": series / ".series-control" / (INVOCATION + ".json"),
        "child": child / "manifest.json",
        "settlement": child / ".series-control" / (CHILD + ".json"),
        "artifact": child / "sources" / (SOURCE + ".json"),
    }
    inventory = [
        {
            "sourceKey": SOURCE,
            "transactionMode": "sale",
            "transactionType": "Sale/Lease",
            "id": "sale-1",
            "url": "https://example.test/sale-1",
        },
        {
            "sourceKey": SOURCE,
            "transactionMode": "lease",
            "id": "lease-1",
            "url": "https://example.test/lease-1",
        },
        {
            "sourceKey": SOURCE,
            "transactionMode": "sale",
            "id": "both-1",
            "url": "https://example.test/both-1",
        },
        {
            "sourceKey": SOURCE,
            "transactionMode": "lease",
            "id": "both-1",
            "url": "https://example.test/both-1",
        },
    ]
    artifact_value = {
        "runMeta": {
            "mode": "full",
            "transactions": ["sale", "lease"],
            "finishedAt": iso(now - 3000),
            "freshness": {"generationId": RUN},
        },
        "listings": inventory,
        "brokers": [],
    }
    artifact_raw = json.dumps(artifact_value, separators=(",", ":")).encode()
    artifact_sha = hashlib.sha256(artifact_raw).hexdigest()
    values = {
        "parent": {
            "schema_version": 1,
            "series_id": SERIES,
            "status": "complete",
            "error": None,
            "collector_git_sha": SHA,
            "collector_git_dirty": False,
            "started_at": iso(admission),
            "finished_at": iso(now - 1000),
            "config": {"sources": [SOURCE], "transactions": ["sale", "lease"]},
            "database_target": target,
            "latest_invocation_admission": {
                "expected_sha": SHA,
                "windows_admission_unix_ms": admission,
            },
            "latest_invocation_control": {
                "invocation_id": INVOCATION,
                "receipt": f".series-control/{INVOCATION}.json",
            },
            "sources": {
                SOURCE: {
                    "state": "complete",
                    "checkpoint_run": "runs/" + RUN,
                    "checkpoint_status": "supported_scope_complete",
                    "attempts": [{"rc": 0, "series_control_child_id": CHILD}],
                }
            },
        },
        "control": {
            "stop_owner": {
                "schema_version": 1,
                "owner_role": "checkpoint_series_one_shot_cli",
                "invocation_id": INVOCATION,
                "uid": os.geteuid(),
                "series_directory_identity": summary.safe._identity(series.stat()),
                "control_directory_identity": summary.safe._identity(
                    (series / ".series-control").stat()
                ),
            },
            "status": "complete",
            "local_settlement": "confirmed",
            "started_unix_ms": admission + 100,
            "finished_unix_ms": now - 900,
            "last_checkpoint": {
                "run_id": RUN,
                "child_invocation_id": CHILD,
                "pid": 12345,
            },
        },
        "child": {
            "schema_version": 2,
            "run_id": RUN,
            "collector_git_sha": SHA,
            "collector_git_dirty": False,
            "status": "supported_scope_complete",
            "error": None,
            "started_at": iso(admission + 1000),
            "finished_at": iso(now - 2000),
            "config": {"sources": [SOURCE], "transactions": ["sale", "lease"]},
            "preflight": {"database_target": target},
            "validation": {
                "rc": 0,
                "query_execution_ok": True,
                "quality_no_regression": True,
                "readback_ok": True,
                "failed_readback_sources": [],
            },
            "sources": {
                SOURCE: {
                    "state": "ingested",
                    "ingest": {"rc": 0},
                    "artifact": {
                        "path": f"sources/{SOURCE}.json",
                        "sha256": artifact_sha,
                        "bytes": len(artifact_raw),
                        "staged_unique": 3,
                        "inventory_only": 0,
                        "rejected_by_ingest": 0,
                    },
                    "readback": {
                        "ok": True,
                        "expected_staged_unique": 3,
                        "latest_inventory_batch_active": 3,
                        "generation_id": RUN,
                    },
                }
            },
        },
        "settlement": {
            "schema_version": 1,
            "invocation_id": INVOCATION,
            "child_invocation_id": CHILD,
            "run_id": RUN,
            "collector_git_sha": SHA,
            "pid": 12345,
            "guards_stopped": True,
            "nested_cleanup_confirmed": True,
            "terminal_evidence_saved": True,
        },
    }

    def save():
        for name, path in files.items():
            if name == "artifact":
                path.write_bytes(
                    json.dumps(artifact_value, separators=(",", ":")).encode()
                )
            else:
                path.write_text(json.dumps(values[name]))
            path.chmod(0o600)

    save()
    return {
        "series": series,
        "admission": admission,
        "values": values,
        "files": files,
        "artifact": artifact_value,
        "artifact_raw": artifact_raw,
        "artifact_sha": artifact_sha,
        "save": save,
    }


def read(evidence, **changes):
    args = {
        "series_directory": evidence["series"],
        "source": SOURCE,
        "expected_sha": SHA,
        "windows_admission_ms": evidence["admission"],
    }
    args.update(changes)
    return summary.verified_summary(**args)


def bind_staged_breakdown(
    evidence,
    source=SOURCE,
    *,
    sale=2,
    lease=3,
    both=2,
    artifact_bytes=None,
    generation_id=RUN,
):
    artifact = evidence["values"]["child"]["sources"][source]["artifact"]
    artifact["freshness_generation_id"] = generation_id
    artifact["finished_at"] = evidence["artifact"]["runMeta"]["finishedAt"]
    stats = {
        "sale": sale,
        "lease": lease,
        "both": both,
        "measurement": "current_staged_inventory_transaction_types",
        "source_key": source,
        "artifact_sha256": artifact["sha256"],
        "artifact_bytes": artifact_bytes or artifact["bytes"],
        "generation_id": generation_id,
    }
    artifact["transaction_breakdown"] = stats
    if artifact_bytes is not None:
        artifact["bytes"] = artifact_bytes
    evidence["save"]()
    return stats


def pipeline_evidence(evidence, primary_source=SOURCE, second_source="cbre"):
    """Bind two source entries to one child and one verified validation unit."""
    source2 = second_source
    parent = evidence["values"]["parent"]
    child = evidence["values"]["child"]
    if primary_source != SOURCE:
        parent["sources"][primary_source] = parent["sources"].pop(SOURCE)
        child["sources"][primary_source] = child["sources"].pop(SOURCE)
        parent["config"]["sources"] = [primary_source, source2]
        child["config"]["sources"] = [primary_source, source2]
        artifact_path = evidence["files"]["artifact"]
        artifact_path.rename(artifact_path.with_name(f"{primary_source}.json"))
        evidence["files"]["artifact"] = artifact_path.with_name(
            f"{primary_source}.json"
        )
        for listing in evidence["artifact"]["listings"]:
            listing["sourceKey"] = primary_source
        evidence["artifact_raw"] = json.dumps(
            evidence["artifact"], separators=(",", ":")
        ).encode()
        evidence["artifact_sha"] = hashlib.sha256(evidence["artifact_raw"]).hexdigest()
        artifact = child["sources"][primary_source]["artifact"]
        artifact.update(
            path=f"sources/{primary_source}.json",
            sha256=evidence["artifact_sha"],
            bytes=len(evidence["artifact_raw"]),
        )
        (evidence["files"]["artifact"]).write_bytes(evidence["artifact_raw"])
        evidence["files"]["artifact"].chmod(0o600)
    parent["config"].update(
        pipeline=True,
        sources=[primary_source, source2],
        current_inventory_admissions={},
    )
    parent["status"] = "failed"
    if evidence["values"]["control"]["status"] == "complete":
        evidence["values"]["control"]["status"] = "failed"
    parent["sources"][primary_source]["state"] = "complete"
    parent["sources"][primary_source]["pipeline_result"] = {
        "state": "verified",
        "unit_id": "unit-0001",
        "validation_path": "pipeline/unit-0001/validation.json",
        "validation_sha256": "0" * 64,
        "readback_ok": True,
        "generation_id": RUN,
        "artifact_sha256": evidence["artifact_sha"],
    }
    parent["sources"][primary_source]["attempts"][-1]["checkpoint_run"] = "runs/" + RUN
    parent["sources"][source2] = {
        "state": "failed_source",
        "checkpoint_run": "runs/" + RUN,
        "checkpoint_status": child["status"],
        "attempts": [
            {
                "rc": 1,
                "checkpoint_run": "runs/" + RUN,
                "series_control_child_id": CHILD,
            }
        ],
    }
    child["status"] = "failed"
    child["config"].update(
        pipeline=True,
        sources=[primary_source, source2],
        current_inventory_admissions=parent["config"]["current_inventory_admissions"],
    )
    child["sources"][source2] = {"state": "collect_failed"}
    child["pipeline"] = {
        "status": "partial",
        "source_results": {
            primary_source: parent["sources"][primary_source]["pipeline_result"],
            source2: {"state": "failed", "reason": "bounded test fixture"},
        },
        "units": [
            {
                "unit_id": "unit-0001",
                "sources": [primary_source],
                "state": "verified",
                "validation_path": "pipeline/unit-0001/validation.json",
                "validation_sha256": "0" * 64,
            }
        ],
    }
    parent["sources"][primary_source]["checkpoint_status"] = child["status"]
    validation_dir = evidence["series"] / "runs" / RUN / "pipeline" / "unit-0001"
    validation_dir.mkdir(parents=True, mode=0o700)
    validation_dir.chmod(0o700)
    validation_value = {
        "ok": True,
        "validation_profile": cre_validate.FULL_VALIDATION_PROFILE,
        "audit_scope": cre_validate.VALIDATION_AUDIT_SCOPES[
            cre_validate.FULL_VALIDATION_PROFILE
        ],
        "queries": {name: [] for name in cre_validate.QUERIES},
        "historical_primary_child_conflicts": [],
    }
    validation_raw = json.dumps(validation_value, separators=(",", ":")).encode()
    digest = hashlib.sha256(validation_raw).hexdigest()
    child["pipeline"]["units"][0]["validation_sha256"] = digest
    parent["sources"][primary_source]["pipeline_result"]["validation_sha256"] = digest
    child["pipeline"]["source_results"][primary_source]["validation_sha256"] = digest
    (validation_dir / "validation.json").write_bytes(validation_raw)
    (validation_dir / "validation.json").chmod(0o600)
    evidence["save"]()
    return {"source": primary_source, "source2": source2, "parent": parent, "child": child}


def qualify_pipeline_second_source(evidence, setup):
    """Turn the second fixture source into a peer verified by the same unit."""
    source = setup["source2"]
    parent, child = setup["parent"], setup["child"]
    primary = setup["source"]
    second_artifact_value = copy.deepcopy(evidence["artifact"])
    for listing in second_artifact_value["listings"]:
        listing["sourceKey"] = source
    second_raw = json.dumps(second_artifact_value, separators=(",", ":")).encode()
    second_sha = hashlib.sha256(second_raw).hexdigest()
    second_path = evidence["series"] / "runs" / RUN / "sources" / f"{source}.json"
    second_path.write_bytes(second_raw)
    second_path.chmod(0o600)
    checkpoint = copy.deepcopy(child["sources"][primary])
    checkpoint["artifact"].update(
        path=f"sources/{source}.json",
        sha256=second_sha,
        bytes=len(second_raw),
        freshness_generation_id=RUN,
        finished_at=second_artifact_value["runMeta"]["finishedAt"],
        transaction_breakdown={
            "sale": 2,
            "lease": 3,
            "both": 2,
            "measurement": "current_staged_inventory_transaction_types",
            "source_key": source,
            "artifact_sha256": second_sha,
            "artifact_bytes": len(second_raw),
            "generation_id": RUN,
        },
    )
    checkpoint["readback"]["generation_id"] = RUN
    child["sources"][source] = checkpoint
    child["pipeline"]["units"][0]["sources"].append(source)
    result = copy.deepcopy(parent["sources"][primary]["pipeline_result"])
    result["artifact_sha256"] = second_sha
    child["pipeline"]["source_results"][source] = result
    entry = {
        "state": "complete",
        "checkpoint_run": "runs/" + RUN,
        "checkpoint_status": "supported_scope_complete",
        "pipeline_result": result,
        "attempts": [
            {
                "rc": 0,
                "checkpoint_run": "runs/" + RUN,
                "series_control_child_id": CHILD,
            }
        ],
    }
    parent["sources"][source] = entry
    parent["sources"][primary]["checkpoint_status"] = "supported_scope_complete"
    child["status"] = "supported_scope_complete"
    parent["status"] = "complete"
    if evidence["values"]["control"]["status"] == "failed":
        evidence["values"]["control"]["status"] = "complete"
    child["pipeline"]["status"] = "complete"
    evidence["save"]()
    return second_path


def rewrite_pipeline_validation(evidence, setup, mutate):
    path = evidence["series"] / "runs" / RUN / "pipeline" / "unit-0001" / "validation.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    mutate(report)
    raw = json.dumps(report, separators=(",", ":")).encode()
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    setup["child"]["pipeline"]["units"][0]["validation_sha256"] = digest
    setup["child"]["pipeline"]["source_results"][SOURCE][
        "validation_sha256"
    ] = digest
    setup["parent"]["sources"][SOURCE]["pipeline_result"][
        "validation_sha256"
    ] = digest
    evidence["save"]()


def read_pipeline(evidence, source_keys=None):
    args = {
        "series_directory": evidence["series"],
        "source_keys": source_keys or [SOURCE, "cbre"],
        "expected_sha": SHA,
        "windows_admission_ms": evidence["admission"],
    }
    return summary.verified_pipeline_summary(**args)


def test_real_private_receipts_prove_only_exact_current_inventory(
    evidence, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("summary must not execute a provider/database command")

    monkeypatch.setattr(subprocess, "run", forbidden)
    result = read(evidence)
    assert result["verified"] is True
    assert result["verified_listings"] == 3
    assert result["transaction_scope"] == ["sale", "lease"]
    assert result["measurement"] == "generation_exact_current_inventory"
    assert result["invocation_id"] == INVOCATION
    assert result["transaction_breakdown"] == {
        "sale": 2,
        "lease": 3,
        "both": 2,
        "measurement": "current_staged_inventory_transaction_types",
    }
    assert not (
        {"new", "updated", "sale_count", "lease_count", "whole_source_coverage"}
        & result.keys()
    )


def test_large_bound_staging_stats_avoid_reading_artifact_payload(evidence, monkeypatch):
    bind_staged_breakdown(evidence, artifact_bytes=20 * 1024 * 1024)
    original = summary.safe._read_at

    def forbid_artifact_read(descriptor, name, limit=16384):
        if name == f"{SOURCE}.json":
            raise AssertionError("bound staging statistics should not open the payload")
        return original(descriptor, name, limit)

    monkeypatch.setattr(summary.safe, "_read_at", forbid_artifact_read)
    result = read(evidence)
    assert result["verified_listings"] == 3
    assert result["transaction_breakdown"] == {
        "sale": 2,
        "lease": 3,
        "both": 2,
        "measurement": "current_staged_inventory_transaction_types",
    }
    assert result["transaction_breakdown_status"]["basis"] == (
        "artifact_staging_statistics"
    )


@pytest.mark.parametrize(
    ("field", "value", "outer_field"),
    [
        ("source_key", "cbre", None),
        ("artifact_sha256", "f" * 64, None),
        ("artifact_bytes", 999, None),
        ("generation_id", "stale-generation", None),
        ("measurement", "raw_cards", None),
        ("sale", True, None),
        ("both", 4, None),
        ("sale", 1, None),
        ("generation_id", RUN, "freshness_generation_id"),
    ],
)
def test_invalid_staging_stats_omit_breakdown_and_preserve_total(
    evidence, field, value, outer_field
):
    stats = bind_staged_breakdown(evidence)
    if outer_field:
        evidence["values"]["child"]["sources"][SOURCE]["artifact"][
            outer_field
        ] = "stale-generation"
    else:
        stats[field] = value
    evidence["save"]()
    result = read(evidence)
    assert result["verified_listings"] == 3
    assert "transaction_breakdown" not in result
    assert result["transaction_breakdown_status"]["status"] == "unavailable"
    assert result["transaction_breakdown_status"]["reason"] == (
        "staging_stats_invalid"
    )


def test_staging_stats_reject_unknown_fields(evidence):
    stats = bind_staged_breakdown(evidence)
    stats["raw_cards"] = 4
    evidence["save"]()
    result = read(evidence)
    assert result["verified_listings"] == 3
    assert "transaction_breakdown" not in result


def test_pipeline_reports_only_qualified_source_from_one_shared_child(evidence):
    setup = pipeline_evidence(evidence)
    result = read_pipeline(evidence)
    assert result["verified"] is True
    assert result["complete"] is False
    assert result["selected_source_keys"] == [SOURCE, setup["source2"]]
    assert result["failed_source_keys"] == [setup["source2"]]
    assert result["pending_source_keys"] == []
    assert len(result["per_source_summaries"]) == 1
    row = result["per_source_summaries"][0]
    assert row["source_key"] == SOURCE
    assert row["verified_listings"] == 3
    assert row["transaction_breakdown_status"]["status"] == "verified"
    assert type(row["transaction_breakdown_status"]["elapsed_ms"]) is int
    assert row["transaction_breakdown"] == {
        "sale": 2,
        "lease": 3,
        "both": 2,
        "measurement": "current_staged_inventory_transaction_types",
    }
    assert "verified_listings" not in result
    assert "total_verified_listings" not in result


def test_pipeline_uses_large_bound_staging_stats_without_reading_payload(
    evidence, monkeypatch
):
    setup = pipeline_evidence(evidence)
    bind_staged_breakdown(evidence, source=SOURCE, artifact_bytes=20 * 1024 * 1024)
    original = summary.safe._read_at

    def forbid_artifact_read(descriptor, name, limit=16384):
        if name == f"{SOURCE}.json":
            raise AssertionError("pipeline stats should not open the artifact payload")
        return original(descriptor, name, limit)

    monkeypatch.setattr(summary.safe, "_read_at", forbid_artifact_read)
    result = read_pipeline(evidence)
    row = result["per_source_summaries"][0]
    assert row["source_key"] == SOURCE
    assert row["verified_listings"] == 3
    assert row["transaction_breakdown"]["sale"] == 2
    assert row["transaction_breakdown"]["lease"] == 3
    assert row["transaction_breakdown"]["both"] == 2
    assert row["transaction_breakdown_status"]["basis"] == (
        "artifact_staging_statistics"
    )
    assert setup["child"]["sources"][SOURCE]["artifact"]["bytes"] > (
        summary.MAX_TRANSACTION_ARTIFACT_BYTES
    )


@pytest.mark.parametrize(
    ("field", "value", "outer_stale"),
    [
        ("source_key", "foreign", False),
        ("artifact_sha256", "f" * 64, False),
        ("artifact_bytes", 1, False),
        ("generation_id", "stale-generation", False),
        ("measurement", "raw_cards", False),
        ("sale", True, False),
        ("both", 4, False),
        ("sale", 1, False),
        ("freshness_generation_id", "stale-generation", True),
    ],
)
def test_pipeline_invalid_staging_stats_omit_breakdown_but_keep_source_total(
    evidence, field, value, outer_stale
):
    setup = pipeline_evidence(evidence)
    stats = bind_staged_breakdown(evidence, source=SOURCE)
    if outer_stale:
        setup["child"]["sources"][SOURCE]["artifact"][field] = value
    else:
        stats[field] = value
    evidence["save"]()
    result = read_pipeline(evidence)
    row = result["per_source_summaries"][0]
    assert row["verified_listings"] == 3
    assert "transaction_breakdown" not in row
    assert row["transaction_breakdown_status"]["status"] == "unavailable"
    assert row["transaction_breakdown_status"]["reason"] == (
        "staging_stats_invalid"
    )
    assert setup["child"]["pipeline"]["source_results"][SOURCE]["state"] == (
        "verified"
    )


def test_empty_admission_map_qualifies_normal_hanley_and_srs(evidence):
    setup = pipeline_evidence(evidence, "hanley", "srs")
    qualify_pipeline_second_source(evidence, setup)
    result = read_pipeline(evidence, ["hanley", "srs"])
    assert result["complete"] is True
    assert result["selected_source_keys"] == ["hanley", "srs"]
    assert [row["source_key"] for row in result["per_source_summaries"]] == [
        "hanley",
        "srs",
    ]
    assert [row["verified_listings"] for row in result["per_source_summaries"]] == [
        3,
        3,
    ]
    assert setup["parent"]["config"]["current_inventory_admissions"] == {}


def test_mixed_admitted_and_normal_sources_use_only_the_admitted_subset(evidence):
    from cre_current_inventory_admission import configuration

    setup = pipeline_evidence(evidence, "hanley", "interra-realty")
    qualify_pipeline_second_source(evidence, setup)
    admissions = {"interra-realty": configuration("interra-realty")}
    setup["parent"]["config"]["current_inventory_admissions"] = admissions
    setup["child"]["config"]["current_inventory_admissions"] = admissions
    evidence["save"]()
    result = read_pipeline(evidence, ["hanley", "interra-realty"])
    assert result["complete"] is True
    assert [row["source_key"] for row in result["per_source_summaries"]] == [
        "hanley",
        "interra-realty",
    ]
    assert setup["parent"]["config"]["current_inventory_admissions"] == admissions


@pytest.mark.parametrize("invalid_map", [None, {"foreign-source": {}}])
def test_pipeline_rejects_missing_or_foreign_admission_map(evidence, invalid_map):
    setup = pipeline_evidence(evidence)
    if invalid_map is None:
        setup["parent"]["config"].pop("current_inventory_admissions")
        setup["child"]["config"].pop("current_inventory_admissions")
    else:
        setup["parent"]["config"]["current_inventory_admissions"] = invalid_map
        setup["child"]["config"]["current_inventory_admissions"] = invalid_map
    evidence["save"]()
    with pytest.raises(REFUSED):
        read_pipeline(evidence)


@pytest.mark.parametrize(
    ("target", "value"),
    [
        ("validation_hash", "f" * 64),
        ("source_map", "foreign"),
        ("unit_state", "failed"),
        ("terminal_age", -600000),
    ],
)
def test_pipeline_tampered_or_unsettled_evidence_never_supplies_counts(
    evidence, target, value
):
    setup = pipeline_evidence(evidence)
    if target == "validation_hash":
        setup["child"]["pipeline"]["source_results"][SOURCE][
            "validation_sha256"
        ] = value
        evidence["save"]()
    elif target == "source_map":
        setup["child"]["pipeline"]["source_results"][value] = setup[
            "child"
        ]["pipeline"]["source_results"].pop(SOURCE)
        evidence["save"]()
    elif target == "unit_state":
        setup["child"]["pipeline"]["units"][0]["state"] = value
        evidence["save"]()
    else:
        evidence["values"]["control"]["finished_unix_ms"] += value
        evidence["save"]()
    with pytest.raises(REFUSED):
        read_pipeline(evidence)


@pytest.mark.parametrize(
    "field",
    [
        "validation_profile",
        "audit_scope",
        "queries",
        "historical_primary_child_conflicts",
    ],
)
def test_pipeline_requires_explicit_complete_full_audit_report(evidence, field):
    setup = pipeline_evidence(evidence)
    rewrite_pipeline_validation(evidence, setup, lambda report: report.pop(field))
    with pytest.raises(REFUSED):
        read_pipeline(evidence)


@pytest.mark.parametrize("missing_side", ["artifact", "source_result", "both"])
def test_pipeline_requires_independently_present_artifact_hashes(
    evidence, missing_side
):
    setup = pipeline_evidence(evidence)
    artifact = setup["child"]["sources"][SOURCE]["artifact"]
    source_result = setup["child"]["pipeline"]["source_results"][SOURCE]
    if missing_side in {"artifact", "both"}:
        artifact.pop("sha256")
    if missing_side in {"source_result", "both"}:
        source_result.pop("artifact_sha256")
    evidence["save"]()
    with pytest.raises(REFUSED):
        read_pipeline(evidence)


@pytest.mark.parametrize("bytes_value", [None, True, 0, -1, 1.5])
def test_pipeline_requires_positive_finite_artifact_byte_binding(evidence, bytes_value):
    setup = pipeline_evidence(evidence)
    artifact = setup["child"]["sources"][SOURCE]["artifact"]
    if bytes_value is None:
        artifact.pop("bytes")
    else:
        artifact["bytes"] = bytes_value
    evidence["save"]()
    with pytest.raises(REFUSED):
        read_pipeline(evidence)


def test_pipeline_rejects_child_outside_parent_temporal_chain(evidence):
    setup = pipeline_evidence(evidence)
    setup["child"]["started_at"] = "2000-01-01T00:00:00+00:00"
    setup["child"]["finished_at"] = "2000-01-01T00:01:00+00:00"
    evidence["save"]()
    with pytest.raises(REFUSED):
        read_pipeline(evidence)


def test_pipeline_rejects_traversal_unit_path(evidence):
    setup = pipeline_evidence(evidence)
    unit = setup["child"]["pipeline"]["units"][0]
    result = setup["child"]["pipeline"]["source_results"][SOURCE]
    unit["unit_id"] = ".."
    unit["validation_path"] = "pipeline/../validation.json"
    result["unit_id"] = ".."
    result["validation_path"] = "pipeline/../validation.json"
    setup["parent"]["sources"][SOURCE]["pipeline_result"] = result
    evidence["save"]()
    with pytest.raises(REFUSED):
        read_pipeline(evidence)


def test_pipeline_distinguishes_pending_from_failed(evidence):
    setup = pipeline_evidence(evidence)
    source2 = setup["source2"]
    setup["parent"]["sources"][source2]["state"] = "pending"
    setup["child"]["pipeline"]["status"] = "partial"
    setup["child"]["sources"][source2] = {"state": "pending"}
    setup["child"]["pipeline"]["source_results"].pop(source2)
    evidence["save"]()
    result = read_pipeline(evidence)
    assert result["verified"] is True
    assert result["complete"] is False
    assert result["failed_source_keys"] == []
    assert result["pending_source_keys"] == [source2]


def test_pipeline_marks_complete_only_when_every_selected_source_qualifies(evidence):
    setup = pipeline_evidence(evidence)
    source2 = setup["source2"]
    parent, child = setup["parent"], setup["child"]
    parent["config"]["sources"] = [SOURCE]
    parent["sources"].pop(source2)
    parent["status"] = "complete"
    evidence["values"]["control"]["status"] = "complete"
    child["config"]["sources"] = [SOURCE]
    child["sources"].pop(source2)
    child["pipeline"]["source_results"].pop(source2)
    child["pipeline"]["status"] = "complete"
    child["status"] = "supported_scope_complete"
    parent["sources"][SOURCE]["checkpoint_status"] = child["status"]
    evidence["save"]()
    result = read_pipeline(evidence, [SOURCE])
    assert result["verified"] is True
    assert result["complete"] is True
    assert result["failed_source_keys"] == []
    assert result["pending_source_keys"] == []
    assert [row["source_key"] for row in result["per_source_summaries"]] == [SOURCE]


def test_legacy_cli_contract_remains_single_source(evidence, capsys):
    status = summary.main(
        [
            "--series-directory",
            str(evidence["series"]),
            "--source",
            SOURCE,
            "--expected-sha",
            SHA,
            "--windows-admission-unix-ms",
            str(evidence["admission"]),
        ]
    )
    assert status == 0
    output = json.loads(capsys.readouterr().out)
    assert output["source"] == SOURCE
    assert output["verified_listings"] == 3
    assert "per_source_summaries" not in output


def test_breakdown_proof_failures_preserve_exact_verified_total(evidence):
    evidence["files"]["artifact"].write_text("tampered", encoding="utf-8")
    result = read(evidence)
    assert result["verified"] is True
    assert result["verified_listings"] == 3
    assert "transaction_breakdown" not in result


@pytest.mark.parametrize(
    "failure", ["missing", "oversized", "stale_generation", "identity_mismatch"]
)
def test_optional_breakdown_unavailable_does_not_discard_total(evidence, failure):
    artifact_meta = evidence["values"]["child"]["sources"][SOURCE]["artifact"]
    artifact_value = evidence["artifact"]
    if failure == "missing":
        evidence["files"]["artifact"].unlink()
    elif failure == "oversized":
        artifact_meta["bytes"] = summary.MAX_TRANSACTION_ARTIFACT_BYTES + 1
        evidence["save"]()
    elif failure == "stale_generation":
        artifact_value["runMeta"]["freshness"]["generationId"] = "stale-generation"
        evidence["artifact_raw"] = json.dumps(
            artifact_value, separators=(",", ":")
        ).encode()
        artifact_meta["sha256"] = hashlib.sha256(evidence["artifact_raw"]).hexdigest()
        artifact_meta["bytes"] = len(evidence["artifact_raw"])
        evidence["save"]()
    else:
        artifact_value["listings"].append(
            {
                "sourceKey": SOURCE,
                "transactionMode": "sale",
                "id": "extra",
                "url": "https://example.test/extra",
            }
        )
        evidence["artifact_raw"] = json.dumps(
            artifact_value, separators=(",", ":")
        ).encode()
        artifact_meta["sha256"] = hashlib.sha256(evidence["artifact_raw"]).hexdigest()
        artifact_meta["bytes"] = len(evidence["artifact_raw"])
        evidence["save"]()
    result = read(evidence)
    assert result["verified"] is True
    assert result["verified_listings"] == 3
    assert "transaction_breakdown" not in result


def test_zero_count_transaction_pass_is_a_verified_breakdown(evidence):
    lease = {
        "sourceKey": SOURCE,
        "transactionMode": "lease",
        "id": "lease-only",
        "url": "https://example.test/lease-only",
    }
    evidence["artifact"]["listings"] = [lease]
    evidence["artifact_raw"] = json.dumps(
        evidence["artifact"], separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(evidence["artifact_raw"]).hexdigest()
    meta = evidence["values"]["child"]["sources"][SOURCE]
    meta["artifact"].update(
        staged_unique=1, sha256=digest, bytes=len(evidence["artifact_raw"])
    )
    meta["readback"].update(expected_staged_unique=1, latest_inventory_batch_active=1)
    evidence["save"]()
    result = read(evidence)
    assert result["verified_listings"] == 1
    assert result["transaction_breakdown"] == {
        "sale": 0,
        "lease": 1,
        "both": 0,
        "measurement": "current_staged_inventory_transaction_types",
    }


def test_explicit_interra_additive_scope_is_qualified_without_whole_source_claim(
    evidence,
):
    import cre_checkpoint_series as series

    source = "interra-realty"
    parent, child = evidence["values"]["parent"], evidence["values"]["child"]
    config = series.series_config(
        sources=[source],
        page_cap=400,
        concurrency=1,
        attempts_per_source=3,
        max_resume_age_hours=24,
        max_host_cpu_percent=90,
        cpu_sustain_seconds=30,
        cpu_sample_seconds=2,
        nice=15,
        admit_current_inventory_source=source,
    )
    parent["config"] = config
    parent["sources"][source] = parent["sources"].pop(SOURCE)
    status = "additive_scope_complete_current_inventory"
    parent["sources"][source]["checkpoint_status"] = status
    child["status"] = status
    child["config"] = series._expected_child_config(source, config)
    child["sources"][source] = child["sources"].pop(SOURCE)
    child["scope"] = {
        "kind": "collector_registry_additive_current_inventory",
        "whole_source_coverage": False,
    }
    evidence["save"]()
    result = read(evidence, source=source)
    assert result["source"] == source and result["verified_listings"] == 3
    assert "whole_source_coverage" not in result


@pytest.mark.parametrize(
    ("document", "path", "value"),
    [
        ("parent", ("status",), "complete_with_source_failures"),
        ("parent", ("collector_git_dirty",), True),
        ("parent", ("collector_git_sha",), "e" * 40),
        ("parent", ("latest_invocation_admission", "windows_admission_unix_ms"), 1),
        ("parent", ("config", "sources"), ["interra-realty"]),
        ("parent", ("sources", SOURCE, "state"), "failed_source"),
        ("parent", ("sources", SOURCE, "attempts"), []),
        ("parent", ("sources", SOURCE, "checkpoint_run"), "runs/../escape"),
        ("control", ("status",), "cleanup_unknown"),
        ("control", ("local_settlement",), "unknown"),
        ("control", ("stop_owner", "invocation_id"), "e" * 32),
        ("control", ("stop_owner", "series_directory_identity"), [1, 2]),
        ("control", ("stop_owner", "control_directory_identity"), [1, 2]),
        ("control", ("last_checkpoint", "child_invocation_id"), "e" * 32),
        ("child", ("status",), "failed"),
        ("child", ("collector_git_sha",), "e" * 40),
        ("child", ("preflight", "database_target"), {"sha256": "e" * 64}),
        ("child", ("config", "transactions"), ["sale"]),
        ("child", ("validation", "query_execution_ok"), False),
        ("child", ("validation", "quality_no_regression"), False),
        ("child", ("validation", "readback_ok"), False),
        ("child", ("sources", SOURCE, "ingest", "rc"), 23),
        ("child", ("sources", SOURCE, "readback", "ok"), False),
        ("child", ("sources", SOURCE, "readback", "expected_staged_unique"), 0),
        ("child", ("sources", SOURCE, "readback", "expected_staged_unique"), True),
        ("child", ("sources", SOURCE, "readback", "latest_inventory_batch_active"), 40),
        ("child", ("sources", SOURCE, "artifact", "staged_unique"), 42),
        ("child", ("sources", SOURCE, "artifact", "inventory_only"), 1),
        ("settlement", ("invocation_id",), "e" * 32),
        ("settlement", ("child_invocation_id",), "e" * 32),
        ("settlement", ("guards_stopped",), False),
        ("settlement", ("nested_cleanup_confirmed",), False),
        ("settlement", ("terminal_evidence_saved",), False),
    ],
)
def test_bad_or_incomplete_proof_never_supplies_counts(evidence, document, path, value):
    current = evidence["values"][document]
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = copy.deepcopy(value)
    evidence["save"]()
    with pytest.raises(REFUSED):
        read(evidence)


@pytest.mark.parametrize("document", ["parent", "control", "child", "settlement"])
@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "public", "missing"])
def test_unsafe_evidence_refuses(evidence, document, unsafe):
    path = evidence["files"][document]
    if unsafe == "public":
        path.chmod(0o644)
    elif unsafe == "missing":
        path.unlink()
    else:
        original = path.with_suffix(".original")
        path.rename(original)
        if unsafe == "symlink":
            path.symlink_to(original)
        else:
            os.link(original, path)
    with pytest.raises(REFUSED):
        read(evidence)


def test_runtime_locator_cannot_escape_or_follow_directory_link(evidence):
    with pytest.raises(REFUSED):
        read(
            evidence,
            series_directory=evidence["series"].parent / "../elsewhere" / SERIES,
        )
    original = evidence["series"].with_name("retained")
    evidence["series"].rename(original)
    evidence["series"].symlink_to(original, target_is_directory=True)
    with pytest.raises(REFUSED):
        read(evidence)


def test_stale_and_future_control_receipts_refuse(evidence):
    evidence["values"]["control"]["finished_unix_ms"] -= 600000
    evidence["save"]()
    with pytest.raises(REFUSED):
        read(evidence)


def test_changed_receipt_during_read_refuses(evidence, monkeypatch):
    original = summary.safe._read_at
    count = 0

    def changing(descriptor, name, limit=16384):
        nonlocal count
        count += 1
        if count == 5:
            evidence["values"]["parent"]["error"] = "changed"
            evidence["save"]()
        return original(descriptor, name, limit)

    monkeypatch.setattr(summary.safe, "_read_at", changing)
    with pytest.raises(REFUSED):
        read(evidence)


def test_cli_outputs_only_fixed_unavailable_result(evidence, capsys):
    evidence["files"]["control"].write_text('{"credential":"SECRET_CANARY"')
    result = summary.main(
        [
            "--series-directory",
            str(evidence["series"]),
            "--source",
            SOURCE,
            "--expected-sha",
            SHA,
            "--windows-admission-unix-ms",
            str(evidence["admission"]),
        ]
    )
    assert result == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"schema_version": 1, "verified": False}
    assert not output.err and "SECRET_CANARY" not in output.out

def test_actual_pipeline_producer_attempt_and_report_feed_strict_summary(evidence, monkeypatch):
    from pathlib import Path
    import cre_checkpoint_series as series
    import cre_checkpoint_refresh as refresh
    from test_pipeline_configuration import config

    setup = pipeline_evidence(evidence)
    qualify_pipeline_second_source(evidence, setup)
    parent, child = setup["parent"], setup["child"]
    parent["config"] = config(sources=[SOURCE, "cbre"], pipeline=True)
    parent["database_target"] = {"algorithm": "sha256", "value": "d" * 64}
    child["preflight"]["database_target"] = parent["database_target"]
    parent_finish = parent["finished_at"]
    for row in parent["sources"].values():
        row.update(checkpoint_run=None, attempts=[])
    monkeypatch.setattr(series, "allocate_run_id", lambda: RUN)
    times = iter([child["started_at"]])
    monkeypatch.setattr(series, "utc_now", lambda: next(times, parent_finish))
    monkeypatch.setattr(series, "_load_child_manifest", lambda *a, **k: (Path("manifest.json"), child))
    # DB qualification has its own tests; retain the fixture's qualified child.
    monkeypatch.setattr(series, "pipeline_source_qualified", lambda *a, **k: True)

    class Process:
        pid = 12345
        def wait(self):
            return 0

    class Control:
        invocation_id = INVOCATION
        child_invocation_id = CHILD
        def prepare_checkpoint(self, *a):
            pass
        def spawn(self, *a, **k):
            return Process()
        def check(self):
            pass
        def observe_exit(self, *a):
            pass

    assert series.run_pipeline_series(evidence["series"], parent, env_file=None, control=Control()) == 0
    for row in parent["sources"].values():
        assert row["attempts"][-1]["checkpoint_run"] == "runs/" + RUN
    child_dir = evidence["series"] / "runs" / RUN
    output = child_dir / "pipeline/unit-0001/validation.json"
    validation_bytes = output.read_bytes()
    child["preflight"]["validation_path"] = "pipeline/unit-0001/validation.json"
    monkeypatch.setattr(refresh, "save_manifest", lambda *a: None)
    monkeypatch.setattr(refresh, "verify_validation_readback", lambda *a, **k: {"ok": True, "failed_sources": []})

    def validator(argv, log, **kwargs):
        output.write_bytes(validation_bytes)
        output.chmod(0o644)  # Actual standalone validator output under normal umask.
        return 0

    monkeypatch.setattr(refresh, "run_command", validator)
    refresh.run_final_validation(child_dir, child, None, sources=[SOURCE, "cbre"], evidence_dir=output.parent, before_path=output)
    assert output.stat().st_mode & 0o777 == 0o600
    evidence["save"]()
    assert read_pipeline(evidence)["complete"] is True


@pytest.mark.parametrize(
    "outcome", ["failed", "interrupted", "cleanup_unknown", "write_unknown"]
)
def test_real_main_terminal_metadata_keeps_verified_partial_summary(
    evidence, monkeypatch, outcome
):
    """Real terminal producer and private-file reader; commands/telemetry are fake."""
    from contextlib import nullcontext

    import cre_checkpoint_refresh as refresh

    setup = pipeline_evidence(evidence)
    child = setup["child"]
    expected_finish = child["finished_at"]
    child["pipeline"]["commit_outcome_unknown"] = outcome == "write_unknown"
    cleared = []
    commands = []
    template = refresh.new_manifest(
        evidence["files"]["child"].parent,
        git_sha=SHA,
        git_dirty=False,
        sources=[SOURCE, "cbre"],
        page_cap=400,
        concurrency=1,
        pipeline=True,
        database_target=child["preflight"]["database_target"],
    )
    child["config"] = template["config"]
    child["config"]["current_inventory_admissions"] = {}
    child["scope"] = template["scope"]
    child["schema_version"] = template["schema_version"]

    class OwnedLock:
        held = False

        def __init__(self, path, *, recovery_required):
            assert recovery_required

        def __enter__(self):
            self.held = True
            return self

        def clear_recovery_requirement(self):
            cleared.append(True)

        def __exit__(self, *args):
            self.held = False

    def fail_command(*args, **kwargs):
        commands.append(args)
        # main's actual resume path must clear the previous terminal timestamp
        # before any next command; the new failure must publish a new timestamp.
        saved = json.loads(evidence["files"]["child"].read_text())
        assert saved["finished_at"] is None
        if outcome == "interrupted":
            raise KeyboardInterrupt
        if outcome == "cleanup_unknown":
            raise refresh.ProcessCleanupUnknown("synthetic unsettled child")
        raise refresh.RefreshError("synthetic source acquisition failure")

    monkeypatch.setattr(refresh, "load_resume_manifest", lambda *a, **k: child)
    monkeypatch.setattr(refresh, "git_identity", lambda: (SHA, False))
    monkeypatch.setattr(
        refresh,
        "database_target_fingerprint",
        lambda *a: child["preflight"]["database_target"],
    )
    monkeypatch.setattr(
        refresh, "checkpoint_lock_dir", lambda *a: evidence["series"] / "private-lock"
    )
    monkeypatch.setattr(refresh, "SharedLock", OwnedLock)
    monkeypatch.setattr(
        refresh, "observe_resource_usage", lambda *a, **k: nullcontext()
    )
    monkeypatch.setattr(refresh, "utc_now", lambda: expected_finish)
    monkeypatch.setattr(refresh, "run_cpu_guard_preflight", lambda *a: 10.0)
    for guard in (refresh.HostCpuGuard, refresh.HostMemoryGuard):
        monkeypatch.setattr(guard, "start", lambda *a: None)
        monkeypatch.setattr(guard, "stop", lambda *a: None)
    monkeypatch.setattr(refresh.HostMemoryGuard, "preflight", lambda *a: 0)
    monkeypatch.setattr(refresh, "run_command", fail_command)
    monkeypatch.setattr(refresh, "save_report", lambda *a: None)
    argv = [
        "--resume",
        str(evidence["files"]["child"].parent),
        "--sources",
        SOURCE + ",cbre",
        "--pipeline",
    ]
    if outcome == "interrupted":
        assert refresh.main(argv) == 130
    else:
        with pytest.raises(refresh.RefreshError, match="synthetic"):
            refresh.main(argv)
    assert len(commands) == 1
    saved = json.loads(evidence["files"]["child"].read_text())
    assert saved["finished_at"] == expected_finish
    assert saved["status"] == ("interrupted" if outcome == "interrupted" else "failed")
    assert saved["pipeline"]["units"][0]["state"] == "verified"
    assert bool(cleared) is (outcome not in {"cleanup_unknown", "write_unknown"})
    evidence["values"]["child"] = saved
    for item in setup["parent"]["sources"].values():
        item["checkpoint_status"] = saved["status"]
    if outcome in {"cleanup_unknown", "write_unknown"}:
        # A terminal timestamp is not proof of settlement. Keep the independently
        # required cleanup confirmation false for uncertain child/write outcomes.
        evidence["values"]["settlement"]["nested_cleanup_confirmed"] = False
    evidence["save"]()
    if outcome in {"interrupted", "cleanup_unknown", "write_unknown"}:
        with pytest.raises(REFUSED):
            read_pipeline(evidence)
    else:
        result = read_pipeline(evidence)
        assert result["verified"] is True and result["complete"] is False
        assert result["per_source_summaries"][0]["verified_listings"] == 3
        assert result["failed_source_keys"] == ["cbre"]
        output = evidence["series"] / "produced-partial-summary.json"
        output.write_text(json.dumps(result))
        output.chmod(0o600)


def settled_global_failure(evidence):
    """Real terminal shapes from a verified unit followed by a pre-write stop."""
    setup = pipeline_evidence(evidence)
    evidence["values"]["control"]["status"] = "failed"
    setup["child"]["pipeline"].update(
        status="reconciliation_required", commit_outcome_unknown=False, active_unit=None
    )
    evidence["save"]()
    return setup


def test_settled_global_failure_retains_only_verified_brokerage(evidence):
    settled_global_failure(evidence)
    result = read_pipeline(evidence)
    assert result["verified"] is True and result["complete"] is False
    assert result["failed_source_keys"] == ["cbre"]
    assert [row["source_key"] for row in result["per_source_summaries"]] == [SOURCE]
    assert result["per_source_summaries"][0]["verified_listings"] == 3
    assert result["per_source_summaries"][0]["transaction_breakdown"]["sale"] == 2
    assert "verified_listings" not in result


@pytest.mark.parametrize("checkpoint_state", [
    "pending", "collecting", "candidate", "validated", "gated", "dry_run_passed", "mixed",
])
def test_actual_failed_pipeline_producer_preserves_settled_sibling_summary(
    evidence, monkeypatch, checkpoint_state
):
    import cre_checkpoint_series as series
    from test_pipeline_configuration import config

    setup = settled_global_failure(evidence)
    parent, child = setup["parent"], setup["child"]
    stopped = {"cbre": checkpoint_state}
    if checkpoint_state == "mixed":
        stopped = {"cbre": "dry_run_passed", "jll-investor": "collecting", "colliers": "pending"}
    selected = [SOURCE, *stopped]
    for source, state in stopped.items():
        child["sources"][source] = {"state": state, "attempts": []}
        child["pipeline"]["source_results"][source] = {
            "state": "failed", "reason": "global pipeline stop",
        }
        parent["sources"][source] = {"state": "pending"}
    parent["config"] = config(sources=selected, pipeline=True)
    child["config"]["sources"] = selected
    parent["database_target"] = {"algorithm": "sha256", "value": "d" * 64}
    child["preflight"]["database_target"] = parent["database_target"]
    for row in parent["sources"].values():
        row.update(checkpoint_run=None, attempts=[])
    monkeypatch.setattr(series, "allocate_run_id", lambda: RUN)
    times = iter([child["started_at"]])
    monkeypatch.setattr(series, "utc_now", lambda: next(times, parent["finished_at"]))
    monkeypatch.setattr(series, "_load_child_manifest", lambda *a, **k: (None, child))
    # The reader still verifies the sibling's real full-profile report and readback.
    monkeypatch.setattr(series, "pipeline_source_qualified", lambda _, key, **k: key == SOURCE)

    class Process:
        pid = 12345

        def wait(self):
            return 1

    class Control:
        invocation_id = INVOCATION
        child_invocation_id = CHILD

        def prepare_checkpoint(self, *a):
            pass

        def spawn(self, *a, **k):
            return Process()

        def check(self):
            pass

        def observe_exit(self, *a):
            pass

    assert series.run_pipeline_series(
        evidence["series"], parent, env_file=None, control=Control()
    ) == 1
    for source in stopped:
        assert parent["sources"][source]["state"] == "pending"
        assert parent["sources"][source]["attempts"][-1]["rc"] == 1
    evidence["save"]()
    result = read_pipeline(evidence, source_keys=selected)
    assert result["verified"] is True and result["complete"] is False
    assert result["failed_source_keys"] == [
        source for source, state in stopped.items() if state == "dry_run_passed"
    ]
    assert result["pending_source_keys"] == [
        source for source, state in stopped.items() if state != "dry_run_passed"
    ]
    assert [row["source_key"] for row in result["per_source_summaries"]] == [SOURCE]
    assert result["per_source_summaries"][0]["verified_listings"] == 3
    assert "verified_listings" not in result


@pytest.mark.parametrize("fault", [
    "reason", "parent_state", "checkpoint_state", "pipeline_status", "child_status",
    "parent_status", "write_unknown", "active_unit", "cleanup_unknown", "owner_unsettled",
    "artifact_hash", "report_hash", "generation", "stale", "active_ingest",
])
def test_pending_global_stop_requires_exact_settled_producer_shape(evidence, fault):
    setup = settled_global_failure(evidence)
    parent, child = setup["parent"], setup["child"]
    parent["sources"]["cbre"]["state"] = "pending"
    child["sources"]["cbre"]["state"] = "dry_run_passed"
    result = child["pipeline"]["source_results"]["cbre"]
    result["reason"] = "global pipeline stop"
    if fault == "reason":
        result["reason"] += "?"
    elif fault == "parent_state":
        parent["sources"]["cbre"]["state"] = "collecting"
    elif fault == "checkpoint_state":
        child["sources"]["cbre"]["state"] = "unknown"
    elif fault == "pipeline_status":
        child["pipeline"]["status"] = "partial"
    elif fault == "child_status":
        child["status"] = "supported_scope_complete"
    elif fault == "parent_status":
        parent["status"] = "complete"
    elif fault == "write_unknown":
        child["pipeline"]["commit_outcome_unknown"] = True
    elif fault == "active_unit":
        child["pipeline"]["active_unit"] = "unit-0002"
    elif fault == "cleanup_unknown":
        evidence["values"]["settlement"]["nested_cleanup_confirmed"] = False
    elif fault == "owner_unsettled":
        evidence["values"]["control"]["local_settlement"] = "unknown"
    elif fault == "artifact_hash":
        child["sources"][SOURCE]["artifact"]["sha256"] = "f" * 64
    elif fault == "report_hash":
        child["pipeline"]["source_results"][SOURCE]["validation_sha256"] = "f" * 64
    elif fault == "generation":
        child["pipeline"]["source_results"][SOURCE]["generation_id"] = "stale"
    elif fault == "stale":
        evidence["values"]["control"]["finished_unix_ms"] -= 600000
    elif fault == "active_ingest":
        child["sources"]["cbre"]["state"] = "ingest_recovery_required"
    evidence["save"]()
    with pytest.raises(REFUSED):
        read_pipeline(evidence)


@pytest.mark.parametrize("fault", [
    "write_unknown", "missing_write_outcome", "numeric_write_outcome", "active_unit",
    "cleanup_unknown", "owner_mismatch", "interrupted", "ingesting", "generation",
    "report_hash", "stale", "artifact_hash", "owner_unsettled", "active_ingest",
])
def test_global_failure_requires_known_settlement_and_unchanged_unit(evidence, fault):
    setup = settled_global_failure(evidence)
    pipeline = setup["child"]["pipeline"]
    control = evidence["values"]["control"]
    if fault == "write_unknown":
        pipeline["commit_outcome_unknown"] = True
    elif fault == "missing_write_outcome":
        pipeline.pop("commit_outcome_unknown")
    elif fault == "numeric_write_outcome":
        pipeline["commit_outcome_unknown"] = 0
    elif fault == "active_unit":
        pipeline["active_unit"] = "unit-0002"
    elif fault == "cleanup_unknown":
        evidence["values"]["settlement"]["nested_cleanup_confirmed"] = False
    elif fault == "owner_mismatch":
        control["status"] = "complete"
    elif fault == "interrupted":
        control["status"] = "interrupted"
    elif fault == "ingesting":
        setup["child"]["sources"]["cbre"]["state"] = "ingest_recovery_required"
    elif fault == "generation":
        pipeline["source_results"][SOURCE]["generation_id"] = "stale"
    elif fault == "report_hash":
        pipeline["source_results"][SOURCE]["validation_sha256"] = "f" * 64
    elif fault == "stale":
        control["finished_unix_ms"] -= 600000
    elif fault == "artifact_hash":
        setup["child"]["sources"][SOURCE]["artifact"]["sha256"] = "f" * 64
    elif fault == "owner_unsettled":
        control["local_settlement"] = "unknown"
    elif fault == "active_ingest":
        setup["child"]["sources"]["cbre"]["state"] = "ingesting"
    evidence["save"]()
    with pytest.raises(REFUSED):
        read_pipeline(evidence)


@pytest.mark.parametrize("pipeline_status", ["complete", "partial", "failed"])
def test_other_pipeline_terminal_states_cannot_hide_unknown_commit(evidence, pipeline_status):
    setup = pipeline_evidence(evidence)
    setup["child"]["pipeline"].update(status=pipeline_status, commit_outcome_unknown=True)
    evidence["save"]()
    with pytest.raises(REFUSED):
        read_pipeline(evidence)


def interrupted_pipeline(evidence, kind="deadline"):
    """Controlled producer shape: parent attempts stop before reconciliation."""
    setup = settled_global_failure(evidence)
    parent, child = setup["parent"], setup["child"]
    rc = 124 if kind == "deadline" else 130
    parent.update(status="interrupted", error=(
        "series work deadline expired" if rc == 124 else "operator interruption"
    ))
    child["status"] = "interrupted"
    evidence["values"]["control"]["status"] = (
        "deadline_interrupted" if rc == 124 else "interrupted"
    )
    for entry in parent["sources"].values():
        entry.update(state="interrupted", checkpoint_status=None)
        entry.pop("pipeline_result", None)
        entry["attempts"][-1]["rc"] = rc
    child["sources"]["cbre"]["state"] = "collecting"
    evidence["save"]()
    return setup


@pytest.mark.parametrize("kind", ["deadline", "operator"])
def test_settled_interruption_uses_verified_child_not_parent_success(evidence, kind):
    setup = interrupted_pipeline(evidence, kind)
    before = copy.deepcopy(setup["parent"])
    result = read_pipeline(evidence)
    assert result["verified"] is True and result["complete"] is False
    assert result["failed_source_keys"] == ["cbre"]
    assert result["pending_source_keys"] == []
    assert [r["source_key"] for r in result["per_source_summaries"]] == [SOURCE]
    assert result["per_source_summaries"][0]["verified_listings"] == 3
    assert "verified_listings" not in result
    assert setup["parent"] == before


@pytest.mark.parametrize("fault", [
    "exit", "bool_exit", "control_kind", "child_terminal", "parent_state",
    "locator", "child_id", "write_unknown", "write_absent", "write_numeric",
    "active_unit", "ingesting", "recovery", "cleanup", "guards", "receipt",
    "settlement", "generation", "artifact", "validation", "stale", "target",
])
def test_interrupted_pipeline_keeps_every_settlement_and_unit_guard(evidence, fault):
    setup = interrupted_pipeline(evidence)
    parent, child = setup["parent"], setup["child"]
    pipeline = child["pipeline"]
    entry = parent["sources"][SOURCE]
    receipt = evidence["values"]["settlement"]
    if fault == "exit":
        entry["attempts"][-1]["rc"] = 130
    elif fault == "bool_exit":
        entry["attempts"][-1]["rc"] = True
    elif fault == "control_kind":
        evidence["values"]["control"]["status"] = "interrupted"
    elif fault == "child_terminal":
        child["status"] = "failed"
    elif fault == "parent_state":
        entry["state"] = "complete"
    elif fault == "locator":
        entry["attempts"][-1]["checkpoint_run"] += "0"
    elif fault == "child_id":
        entry["attempts"][-1]["series_control_child_id"] = "f" * 32
    elif fault == "write_unknown":
        pipeline["commit_outcome_unknown"] = True
    elif fault == "write_absent":
        pipeline.pop("commit_outcome_unknown")
    elif fault == "write_numeric":
        pipeline["commit_outcome_unknown"] = 0
    elif fault == "active_unit":
        pipeline["active_unit"] = "unit-0002"
    elif fault == "ingesting":
        child["sources"]["cbre"]["state"] = "ingesting"
    elif fault == "recovery":
        child["sources"]["cbre"]["state"] = "ingest_recovery_required"
    elif fault in {"cleanup", "guards", "receipt"}:
        receipt[{"cleanup": "nested_cleanup_confirmed", "guards": "guards_stopped",
                 "receipt": "terminal_evidence_saved"}[fault]] = False
    elif fault == "settlement":
        evidence["values"]["control"]["local_settlement"] = "unknown"
    elif fault == "generation":
        pipeline["source_results"][SOURCE]["generation_id"] = "stale"
    elif fault == "artifact":
        child["sources"][SOURCE]["artifact"]["sha256"] = "f" * 64
    elif fault == "validation":
        pipeline["source_results"][SOURCE]["validation_sha256"] = "f" * 64
    elif fault == "stale":
        evidence["values"]["control"]["finished_unix_ms"] -= 600000
    elif fault == "target":
        child["preflight"]["database_target"] = {"sha256": "f" * 64}
    evidence["save"]()
    with pytest.raises(REFUSED):
        read_pipeline(evidence)
