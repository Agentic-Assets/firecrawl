"""No-network contracts for Transwestern image-transition outcome receipts."""

import hashlib
import json
import os

import pytest

import cre_transwestern_image_outcome as outcome
import cre_colliers_image_transition as colliers_outcome


def valid_outcome():
    return {
        "schema": "transwestern-image-transition-v1",
        "source_key": "transwestern",
        "binding": {
            "artifact_sha256": "a" * 64,
            "artifact_bytes": 123,
            "artifact_run_key": "fixture-run-key",
            "generation_id": "fixture-generation",
            "generation_started_at": "2026-10-19T00:00:00Z",
            "database_target_sha256": "b" * 64,
            "attempt_id": "c" * 32,
            "collector_git_sha": "d" * 40,
        },
        "raw_before": 12,
        "raw_retained_after": 10,
        "raw_new_after": 0,
        "raw_after": 10,
        "raw_retired_before": 0,
        "protected_before": 10,
        "protected_retained_after": 10,
        "excluded_before": 2,
        "staged_current": 2,
        "current_source_primary": 1,
        "historical_primary": 1,
        "unassigned_primary": 0,
        "image_state_sha256": "e" * 64,
    }


def report_row(image_count=10, digest="e" * 64):
    return {"image_count": image_count, "image_state_sha256": digest}


def test_reports_and_readback_require_exact_bound_conservation():
    receipt = valid_outcome()
    assert outcome.matches_reports(receipt, 12, 10)
    assert outcome.matches_readback(receipt, [report_row()])
    assert outcome.matches_readback(receipt, [report_row("10")])
    assert not outcome.matches_readback(receipt, [])
    assert not outcome.matches_readback(receipt, [report_row(), report_row()])
    assert not outcome.matches_readback(receipt, [report_row(11)])
    assert not outcome.matches_readback(receipt, [report_row(10, "f" * 64)])


@pytest.mark.parametrize("count", [10, "10"])
def test_report_image_count_requires_one_exact_source_row_and_count(count):
    report = {"queries": {"child_counts": [
        {"source_key": "transwestern", "child_type": "images", "count": count}
    ]}}
    assert outcome.report_image_count(report) == 10


@pytest.mark.parametrize("rows", [
    [],
    [
        {"source_key": "transwestern", "child_type": "images", "count": "10"},
        {"source_key": "transwestern", "child_type": "images", "count": "1"},
    ],
    [{"source_key": "transwestern", "child_type": "images", "count": True}],
    [{"source_key": "transwestern", "child_type": "images", "count": "010"}],
    [{"source_key": "transwestern", "child_type": "images", "count": " 10 "}],
])
def test_report_image_count_refuses_missing_duplicate_or_malformed_count(rows):
    assert outcome.report_image_count({"queries": {"child_counts": rows}}) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("raw_before", True),
        ("raw_retained_after", -1),
        ("raw_after", 11),
        ("raw_new_after", 1),
        ("raw_retired_before", 1),
        ("protected_before", 9),
        ("protected_retained_after", 9),
        ("excluded_before", 3),
        ("staged_current", 0),
        ("current_source_primary", 2),
        ("historical_primary", 2),
        ("unassigned_primary", True),
        ("image_state_sha256", "not-a-digest"),
        ("source_key", "colliers-main"),
        ("schema", "unknown"),
    ],
)
def test_malformed_or_nonconserving_outcome_is_refused(field, value):
    receipt = valid_outcome()
    receipt[field] = value
    assert not outcome.matches_reports(receipt, 12, 10)


@pytest.mark.parametrize(
    "field,value",
    [
        ("collector_git_sha", "not-a-sha"),
        ("artifact_bytes", True),
        ("attempt_id", "x" * 32),
        ("extra", "unknown"),
    ],
)
def test_tw_binding_requires_exact_eight_field_code_and_artifact_identity(field, value):
    binding = valid_outcome()["binding"]
    if field == "extra":
        binding[field] = value
    else:
        binding[field] = value
    assert not outcome.valid_binding(binding)


def test_colliers_keeps_its_original_seven_field_binding_contract():
    binding = valid_outcome()["binding"]
    assert colliers_outcome.valid_binding(
        {key: value for key, value in binding.items() if key != "collector_git_sha"}
    )
    assert not colliers_outcome.valid_binding(binding)


def test_shared_log_reader_accepts_exactly_one_source_specific_bounded_line(tmp_path):
    log = tmp_path / "ingest.log"
    log.write_text(
        "ordinary output\n"
        + outcome.PREFIX
        + json.dumps(valid_outcome())
        + "\ningest complete\n"
    )
    observed = outcome.read_outcome(log)
    assert observed["outcome"] == valid_outcome()
    assert observed["log_sha256"] == hashlib.sha256(log.read_bytes()).hexdigest()
    assert observed["log_bytes"] == log.stat().st_size

    log.write_text(log.read_text() + outcome.PREFIX + "{}\n")
    with pytest.raises(ValueError, match="ambiguous"):
        outcome.read_outcome(log)


def checkpoint_fixture(tmp_path):
    artifact_path = tmp_path / "artifacts" / "transwestern.json"
    artifact_path.parent.mkdir()
    artifact_path.write_text('{"synthetic":true}')
    log_path = tmp_path / "logs" / "transwestern-ingest.log"
    log_path.parent.mkdir()
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    manifest = {
        "collector_git_sha": "d" * 40,
        "run_id": "fixture-generation",
        "started_at": "2026-10-19T00:00:00Z",
        "preflight": {"database_target": {"value": "b" * 64}},
        "sources": {
            "transwestern": {
                "state": "ingested",
                "artifact": {
                    "path": "artifacts/transwestern.json",
                    "sha256": digest,
                    "bytes": artifact_path.stat().st_size,
                    "staged_unique": 2,
                },
                "ingest": {
                    "attempt_id": "c" * 32,
                    "rc": 0,
                    "started_at": "2026-10-19T00:00:01Z",
                    "finished_at": "2026-10-19T00:00:02Z",
                    "log": "logs/transwestern-ingest.log",
                    "mark_missing": False,
                    "status_activation": False,
                },
            }
        },
    }
    receipt = valid_outcome()
    receipt["binding"] = outcome.expected_binding(tmp_path, manifest, "c" * 32)
    log_path.write_text(outcome.PREFIX + json.dumps(receipt) + "\ningest complete\n")
    manifest["sources"]["transwestern"]["ingest"]["image_transition"] = (
        outcome.read_outcome(log_path)
    )
    return manifest, receipt, artifact_path, log_path


def test_verified_checkpoint_rechecks_tw_artifact_code_log_and_pipeline_state(tmp_path):
    manifest, receipt, artifact_path, log_path = checkpoint_fixture(tmp_path)
    assert outcome.verified_checkpoint(tmp_path, manifest) == receipt

    manifest["collector_git_sha"] = "f" * 40
    assert outcome.verified_checkpoint(tmp_path, manifest) is None
    manifest["collector_git_sha"] = "d" * 40

    manifest["sources"]["transwestern"]["ingest_recovery"] = {"required": True}
    assert outcome.verified_checkpoint(tmp_path, manifest) is None
    manifest["sources"]["transwestern"].pop("ingest_recovery")

    log_path.write_text(log_path.read_text() + "changed\n")
    assert outcome.verified_checkpoint(tmp_path, manifest) is None


@pytest.mark.parametrize(
    "mutation",
    [
        "nonzero", "recovered", "unfinished", "status_activation", "wrong_log",
        "staged", "artifact_bytes", "artifact_path", "artifact_content",
    ],
)
def test_uncertain_tw_checkpoint_never_yields_receipt(tmp_path, mutation):
    manifest, receipt, artifact_path, log_path = checkpoint_fixture(tmp_path)
    checkpoint = manifest["sources"]["transwestern"]
    ingest = checkpoint["ingest"]
    if mutation == "nonzero":
        ingest["rc"] = 1
    elif mutation == "recovered":
        checkpoint["ingest_recovery"] = {"reason": "unknown"}
    elif mutation == "unfinished":
        ingest["finished_at"] = None
    elif mutation == "status_activation":
        ingest["status_activation"] = True
    elif mutation == "wrong_log":
        ingest["log"] = "logs/other.log"
    elif mutation == "staged":
        checkpoint["artifact"]["staged_unique"] += 1
    elif mutation == "artifact_bytes":
        checkpoint["artifact"]["bytes"] += 1
    elif mutation == "artifact_path":
        checkpoint["artifact"]["path"] = "artifacts/other.json"
    elif mutation == "artifact_content":
        artifact_path.write_bytes(artifact_path.read_bytes() + b" ")
    assert outcome.verified_checkpoint(tmp_path, manifest) is None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX named pipe contract")
def test_shared_artifact_reader_refuses_fifo_without_blocking(tmp_path):
    import cre_image_transition_log as shared

    os.mkfifo(tmp_path / "fifo", 0o600)
    with pytest.raises((OSError, ValueError)):
        shared.artifact_binding(
            tmp_path,
            {
                "sources": {
                    "transwestern": {"artifact": {"path": "fifo"}}
                }
            },
            "transwestern",
            "c" * 32,
            include_collector_git_sha=True,
        )
