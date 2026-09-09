"""Pure, no-network contracts for the producer freshness receipt."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import cre_checkpoint_series as series
import cre_source_health as health


def _manifest(
    *,
    state: str,
    updated_at: str,
    checkpoint_run: str | None = None,
    source: str = "cbre",
):
    return {
        "series_id": "series-1",
        "status": "complete"
        if state == "complete"
        else "complete_with_source_failures",
        "updated_at": updated_at,
        "sources": {
            source: {
                "state": state,
                "checkpoint_run": checkpoint_run,
                "attempts": [
                    {
                        "started_at": "2026-08-11T12:00:00+00:00",
                        "finished_at": updated_at,
                    }
                ],
            }
        },
    }


def _write_child(
    series_dir: Path,
    observation_at: str,
    *,
    partial: bool = False,
    source: str = "cbre",
    latest_inventory_at: str | None = None,
    earliest_detail_at: str | None = None,
    latest_detail_at: str | None = None,
    enumerated_at: str | None = None,
    scope_watermark_at: str | None = None,
    queue_health: dict | None = None,
) -> str:
    child_dir = series_dir / "runs" / "child-1"
    child_dir.mkdir(parents=True)
    child = {
        "run_id": "child-1",
        "status": health.TERMINAL_SUCCESS,
        "scope": {
            "kind": "collector_registry_additive_coverage_hold"
            if partial
            else "collector_registry",
            "whole_source_coverage": not partial,
        },
        "sources": {
            source: {
                "artifact": {
                    "sha256": "a" * 64,
                    "staged_unique": 12,
                    "inventory_only": 3,
                },
                "readback": {
                    "earliest_inventory_observed_at": observation_at,
                    "latest_inventory_observed_at": (
                        latest_inventory_at or observation_at
                    ),
                    "earliest_detail_observed_at": (
                        earliest_detail_at or observation_at
                    ),
                    "latest_detail_observed_at": (latest_detail_at or observation_at),
                    "inventory_only": {
                        "latest_enumerated_at": enumerated_at or observation_at,
                        **(
                            {"scope_watermark_at": scope_watermark_at}
                            if scope_watermark_at is not None
                            else {}
                        ),
                    },
                    "queue_health": queue_health,
                },
            }
        },
    }
    (child_dir / "manifest.json").write_text(json.dumps(child), encoding="utf-8")
    return "runs/child-1"


def _hold_canonical_lock(canonical: str, acquired, release) -> None:
    with health._canonical_lock(Path(canonical)):
        acquired.set()
        release.wait(timeout=5)


def _publish_after_lock(series_dir: str, manifest: dict, started, finished) -> None:
    started.set()
    health.publish_series_health(Path(series_dir), manifest)
    finished.set()


def test_current_cache_cannot_make_august_observation_fresh(tmp_path):
    run = _write_child(tmp_path, "2026-08-11T12:30:00+00:00")
    receipt = health.build_receipt(
        tmp_path,
        _manifest(
            state="complete", updated_at="2026-09-08T12:30:00+00:00", checkpoint_run=run
        ),
    )

    source = receipt["sources"]["cbre"]
    assert source["state"] == "stale"
    assert source["lastSuccessfulObservationAt"] == "2026-08-11T12:30:00+00:00"
    assert source["sourceVintage"] == "2026-08-11"
    assert source["producerComputedAt"] == "2026-09-08T12:30:00+00:00"
    assert source["publishedAt"] is None


def test_missing_observation_is_unknown(tmp_path):
    receipt = health.build_receipt(
        tmp_path,
        _manifest(state="complete", updated_at="2026-09-08T12:30:00+00:00"),
    )

    assert receipt["sources"]["cbre"]["state"] == "unknown"


def test_failed_attempt_preserves_last_good_inventory_and_marks_failure(tmp_path):
    previous = {
        "sources": {
            "cbre": {
                "lastSuccessfulObservationAt": "2026-09-07T12:30:00+00:00",
                "collectedAt": "2026-09-07T12:30:00+00:00",
                "sourceVintage": None,
                "completeness": {"runId": "good", "observedCount": 15},
            }
        }
    }
    receipt = health.build_receipt(
        tmp_path,
        _manifest(state="failed_source", updated_at="2026-09-08T12:30:00+00:00"),
        previous=previous,
    )

    source = receipt["sources"]["cbre"]
    assert source["state"] == "fresh"
    assert source["attemptStatus"] == "failed"
    assert source["publicationStatus"] == "failed"
    assert source["completeness"]["runId"] == "good"
    assert source["completeness"]["observedCount"] == 15


def test_partial_success_is_explicit_and_replay_is_idempotent(tmp_path):
    run = _write_child(tmp_path, "2026-09-08T12:00:00+00:00", partial=True)
    manifest = _manifest(
        state="complete",
        updated_at="2026-09-08T12:30:00+00:00",
        checkpoint_run=run,
    )
    first = health.build_receipt(tmp_path, manifest)
    second = health.build_receipt(tmp_path, manifest, previous=first)

    assert first == second
    assert first["sources"]["cbre"]["publicationStatus"] == "partial"
    assert first["sources"]["cbre"]["lastAttemptObservationAt"] == (
        "2026-09-08T12:00:00+00:00"
    )
    assert first["sources"]["cbre"]["lastSuccessfulObservationAt"] is None
    assert first["sources"]["cbre"]["completeness"] is None


def test_queue_snapshot_separates_retryable_and_dead_letter_work():
    result = health.summarize_queue_rows(
        [
            {"source_key": "cbre", "attempts": 0},
            {"source_key": "cbre", "attempts": 2, "failure_class": "transient"},
            {"source_key": "cbre", "attempts": 5, "failure_class": "deterministic"},
        ]
    )

    assert result["cbre"] == {
        "backlogCount": 3,
        "retryCount": 2,
        "deadLetterCount": 1,
        "deterministicFailureCount": 1,
        "transientFailureCount": 1,
        "unclassifiedFailureCount": 0,
    }


def test_validated_child_queue_health_is_published(tmp_path):
    run = _write_child(
        tmp_path,
        "2026-09-08T12:00:00+00:00",
        queue_health={
            "source_key": "cbre",
            "backlog_count": "4",
            "retry_count": "3",
            "dead_letter_count": "1",
            "deterministic_failure_count": "0",
            "transient_failure_count": "0",
            "unclassified_failure_count": "2",
        },
    )
    receipt = health.build_receipt(
        tmp_path,
        _manifest(
            state="complete",
            updated_at="2026-09-08T12:30:00+00:00",
            checkpoint_run=run,
        ),
    )

    source = receipt["sources"]["cbre"]
    assert source["backlogCount"] == 4
    assert source["retryCount"] == 3
    assert source["deadLetterCount"] == 1
    assert source["unclassifiedFailureCount"] == 2


def test_success_without_observation_does_not_erase_prior_success(tmp_path):
    run = _write_child(tmp_path, "not-a-timestamp")
    previous = {
        "sources": {
            "cbre": {
                "lastSuccessfulObservationAt": "2026-09-07T12:30:00+00:00",
                "collectedAt": "2026-09-07T12:30:00+00:00",
            }
        }
    }
    receipt = health.build_receipt(
        tmp_path,
        _manifest(
            state="complete",
            updated_at="2026-09-08T12:30:00+00:00",
            checkpoint_run=run,
        ),
        previous=previous,
    )

    source = receipt["sources"]["cbre"]
    assert source["lastSuccessfulObservationAt"] == "2026-09-07T12:30:00+00:00"
    assert source["publicationStatus"] == "unknown"


def test_unreadable_canonical_receipt_is_not_overwritten(tmp_path):
    series_dir = tmp_path / "series-1"
    series_dir.mkdir()
    canonical = tmp_path / "producer-source-health.json"
    canonical.write_text("not json", encoding="utf-8")

    health.publish_series_health(
        series_dir,
        _manifest(
            state="failed_source",
            updated_at="2026-09-08T12:30:00+00:00",
        ),
    )

    assert canonical.read_text(encoding="utf-8") == "not json"
    assert (series_dir / "source-health.json").is_file()
    publication = json.loads(
        (series_dir / "source-health-publication.json").read_text(encoding="utf-8")
    )
    assert publication["status"] == "degraded"
    assert publication["reason"] == "canonical_receipt_invalid"


def test_pending_source_does_not_erase_prior_attempt_or_publication(tmp_path):
    previous = {
        "sources": {
            "cbre": {
                "sourceId": "cbre",
                "lastAttemptAt": "2026-09-07T12:30:00+00:00",
                "lastSuccessfulObservationAt": "2026-09-07T12:30:00+00:00",
                "collectedAt": "2026-09-07T12:30:00+00:00",
                "attemptStatus": "succeeded",
                "publicationStatus": "complete",
            }
        }
    }
    receipt = health.build_receipt(
        tmp_path,
        _manifest(state="pending", updated_at="2026-09-08T12:30:00+00:00"),
        previous=previous,
    )

    source = receipt["sources"]["cbre"]
    assert source["lastAttemptAt"] == "2026-09-07T12:30:00+00:00"
    assert source["attemptStatus"] == "succeeded"
    assert source["publicationStatus"] == "complete"


def test_partial_success_preserves_prior_complete_observation_and_completeness(
    tmp_path,
):
    run = _write_child(tmp_path, "2026-09-08T12:00:00+00:00", partial=True)
    previous = {
        "producerComputedAt": "2026-09-07T12:30:00+00:00",
        "sources": {
            "cbre": {
                "lastAttemptAt": "2026-09-07T12:30:00+00:00",
                "lastSuccessfulObservationAt": "2026-09-07T12:00:00+00:00",
                "collectedAt": "2026-09-07T12:00:00+00:00",
                "attemptStatus": "succeeded",
                "publicationStatus": "complete",
                "completeness": {"runId": "complete-run", "observedCount": 20},
            }
        },
    }

    receipt = health.build_receipt(
        tmp_path,
        _manifest(
            state="complete",
            updated_at="2026-09-08T12:30:00+00:00",
            checkpoint_run=run,
        ),
        previous=previous,
    )

    source = receipt["sources"]["cbre"]
    assert source["lastSuccessfulObservationAt"] == "2026-09-07T12:00:00+00:00"
    assert source["completeness"]["runId"] == "complete-run"
    assert source["completeness"]["observedCount"] == 20
    assert source["lastAttemptObservationAt"] == "2026-09-08T12:00:00+00:00"
    assert source["publicationStatus"] == "partial"


def test_whole_source_observation_uses_earliest_required_watermark(tmp_path):
    run = _write_child(
        tmp_path,
        "2026-09-08T12:04:00+00:00",
        latest_inventory_at="2026-09-08T12:09:00+00:00",
        earliest_detail_at="2026-09-08T12:02:00+00:00",
        latest_detail_at="2026-09-08T12:10:00+00:00",
        enumerated_at="2026-09-08T12:08:00+00:00",
        scope_watermark_at="2026-09-08T12:01:00+00:00",
    )

    receipt = health.build_receipt(
        tmp_path,
        _manifest(
            state="complete",
            updated_at="2026-09-08T12:30:00+00:00",
            checkpoint_run=run,
        ),
    )

    source = receipt["sources"]["cbre"]
    assert source["lastSuccessfulObservationAt"] == "2026-09-08T12:01:00+00:00"
    assert source["lastAttemptObservationAt"] == "2026-09-08T12:01:00+00:00"


def test_future_observation_beyond_five_minute_skew_is_unknown(tmp_path):
    run = _write_child(tmp_path, "2026-09-08T12:05:01+00:00")

    receipt = health.build_receipt(
        tmp_path,
        _manifest(
            state="complete",
            updated_at="2026-09-08T12:00:00+00:00",
            checkpoint_run=run,
        ),
    )

    source = receipt["sources"]["cbre"]
    assert source["state"] == "unknown"
    assert source["publicationStatus"] == "unknown"
    assert source["lastSuccessfulObservationAt"] is None
    assert source["lastAttemptObservationAt"] is None


def test_out_of_order_success_cannot_move_canonical_source_backward(tmp_path):
    newer_dir = tmp_path / "series-newer"
    older_dir = tmp_path / "series-older"
    newer_dir.mkdir()
    older_dir.mkdir()
    newer_run = _write_child(newer_dir, "2026-09-08T12:00:00+00:00")
    older_run = _write_child(older_dir, "2026-09-01T12:00:00+00:00")
    health.publish_series_health(
        newer_dir,
        _manifest(
            state="complete",
            updated_at="2026-09-08T12:30:00+00:00",
            checkpoint_run=newer_run,
        ),
    )

    health.publish_series_health(
        older_dir,
        _manifest(
            state="complete",
            updated_at="2026-09-01T12:30:00+00:00",
            checkpoint_run=older_run,
        ),
    )

    canonical = json.loads(
        (tmp_path / "producer-source-health.json").read_text(encoding="utf-8")
    )
    source = canonical["sources"]["cbre"]
    assert source["lastSuccessfulObservationAt"] == "2026-09-08T12:00:00+00:00"
    assert source["lastAttemptAt"] == "2026-09-08T12:30:00+00:00"
    assert source["publicationStatus"] == "complete"


def test_schema_invalid_prior_is_preserved_and_never_copied_to_series(tmp_path):
    series_dir = tmp_path / "series-1"
    series_dir.mkdir()
    canonical = tmp_path / "producer-source-health.json"
    invalid = {
        "contractVersion": health.CONTRACT_VERSION,
        "producerId": health.PRODUCER_ID,
        "producerComputedAt": "2026-09-07T12:30:00+00:00",
        "seriesId": "old",
        "seriesStatus": "complete",
        "sources": {
            "cbre": {
                "sourceId": "cbre",
                "secretToken": "super-secret-value",
            }
        },
    }
    canonical.write_text(json.dumps(invalid), encoding="utf-8")

    health.publish_series_health(
        series_dir,
        _manifest(
            state="failed_source",
            updated_at="2026-09-08T12:30:00+00:00",
        ),
    )

    assert json.loads(canonical.read_text(encoding="utf-8")) == invalid
    series_output = (series_dir / "source-health.json").read_text(encoding="utf-8")
    publication_output = (series_dir / "source-health-publication.json").read_text(
        encoding="utf-8"
    )
    assert "super-secret-value" not in series_output
    assert "super-secret-value" not in publication_output
    assert json.loads(publication_output)["reason"] == "canonical_receipt_invalid"


def test_atomic_write_refuses_a_preexisting_temp_symlink(tmp_path, monkeypatch):
    output = tmp_path / "health.json"
    target = tmp_path / "do-not-touch.json"
    target.write_text("protected", encoding="utf-8")
    monkeypatch.setattr(health.time, "time_ns", lambda: 123)
    temporary = output.with_name(
        f".{output.name}.{health.os.getpid()}.123.tmp"
    )
    temporary.symlink_to(target)

    try:
        health._atomic_write(output, {"status": "ok"})
    except FileExistsError:
        pass
    else:  # pragma: no cover - the exclusive create must reject this path
        raise AssertionError("preexisting temp symlink was accepted")

    assert target.read_text(encoding="utf-8") == "protected"
    assert not output.exists()


def test_receipt_output_failure_does_not_abort_manifest_save(
    tmp_path, monkeypatch, capsys
):
    secret = "postgres://user:secret@example.invalid/db"

    def fail_publication(*_args, **_kwargs):
        raise OSError(secret)

    monkeypatch.setattr(series, "publish_series_health", fail_publication)
    manifest = _manifest(state="failed_source", updated_at="2026-09-08T12:30:00+00:00")

    series.save_manifest(tmp_path, manifest)

    saved = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert saved["series_id"] == "series-1"
    captured = capsys.readouterr()
    assert "producer source-health publication failed (OSError)" in captured.err
    assert secret not in captured.err


def test_canonical_publication_lock_serializes_two_processes(tmp_path):
    context = multiprocessing.get_context("fork")
    acquired = context.Event()
    release = context.Event()
    started = context.Event()
    finished = context.Event()
    canonical = tmp_path / "producer-source-health.json"
    series_dir = tmp_path / "series-1"
    series_dir.mkdir()
    manifest = _manifest(state="failed_source", updated_at="2026-09-08T12:30:00+00:00")
    holder = context.Process(
        target=_hold_canonical_lock,
        args=(str(canonical), acquired, release),
    )
    publisher = context.Process(
        target=_publish_after_lock,
        args=(str(series_dir), manifest, started, finished),
    )

    holder.start()
    assert acquired.wait(timeout=2)
    publisher.start()
    assert started.wait(timeout=2)
    assert not finished.wait(timeout=0.2)
    release.set()
    holder.join(timeout=3)
    publisher.join(timeout=3)

    assert holder.exitcode == 0
    assert publisher.exitcode == 0
    assert finished.is_set()
    assert canonical.is_file()
