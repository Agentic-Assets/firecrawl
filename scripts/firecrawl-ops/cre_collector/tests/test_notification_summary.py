"""Real private-file receipt qualification; no provider or database access."""

import copy
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone

import cre_notification_summary as summary
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
