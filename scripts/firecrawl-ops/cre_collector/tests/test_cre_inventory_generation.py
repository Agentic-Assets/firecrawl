"""Pure tests for the producer-freshness-v2 generation receipt."""

from __future__ import annotations

import json
from pathlib import Path

import cre_inventory_generation as generation


OBSERVED_AT = "2026-09-09T11:45:00+00:00"
UPDATED_AT = "2026-09-09T11:50:00+00:00"
COMPUTED_AT = "2026-09-09T12:00:00+00:00"


def _complete_series(series_dir: Path) -> dict:
    sources = {}
    for source_id in sorted(generation.REQUIRED_SOURCE_IDS):
        child_dir = series_dir / "runs" / source_id
        child_dir.mkdir(parents=True)
        child = {
            "run_id": f"run-{source_id}",
            "status": generation.legacy_health.TERMINAL_SUCCESS,
            "scope": {
                "kind": "collector_registry",
                "whole_source_coverage": True,
            },
            "sources": {
                source_id: {
                    "artifact": {
                        "sha256": "a" * 64,
                        "staged_unique": 1,
                        "inventory_only": 0,
                    },
                    "readback": {
                        "ok": True,
                        "earliest_inventory_observed_at": OBSERVED_AT,
                        "latest_inventory_observed_at": OBSERVED_AT,
                        "inventory_only": {
                            "expected_active": 0,
                            "scope_watermark_at": OBSERVED_AT,
                        },
                        "inventory_fingerprint": {
                            "sourceId": source_id,
                            "rowCount": 1,
                            "maxRowUpdatedAt": UPDATED_AT,
                            "maxObservationAt": OBSERVED_AT,
                            "publicationStatus": "complete",
                        },
                    },
                }
            },
        }
        (child_dir / "manifest.json").write_text(json.dumps(child), encoding="utf-8")
        sources[source_id] = {
            "state": "complete",
            "checkpoint_run": f"runs/{source_id}",
            "attempts": [
                {
                    "started_at": OBSERVED_AT,
                    "finished_at": COMPUTED_AT,
                }
            ],
        }
    return {
        "series_id": "series-2026-09-09",
        "status": "complete",
        "updated_at": COMPUTED_AT,
        "config": {"sources": sorted(generation.REQUIRED_SOURCE_IDS)},
        "sources": sources,
    }


def test_receipt_matches_frozen_v2_shape_and_digest(tmp_path):
    manifest = _complete_series(tmp_path)

    receipt = generation.build_inventory_generation_receipt(tmp_path, manifest)

    assert receipt["contractVersion"] == "producer-freshness-v2"
    assert receipt["producerId"] == "firecrawl-cre-listings"
    assert receipt["inventoryGeneration"]["sourceVocabularyVersion"] == (
        "firecrawl-source-registry-v1"
    )
    assert receipt["inventoryGeneration"]["complete"] is True
    fingerprints = {
        source_id: source["inventoryFingerprint"]
        for source_id, source in receipt["sources"].items()
    }
    digest = generation._canonical_digest(fingerprints)
    assert receipt["inventoryGeneration"]["digest"] == digest
    assert receipt["inventoryGeneration"]["generationId"] == (
        f"inventory-generation:{digest}"
    )
    assert set(next(iter(fingerprints.values()))) == {
        "sourceId",
        "rowCount",
        "maxRowUpdatedAt",
        "maxObservationAt",
        "publicationStatus",
    }


def test_idempotent_retry_reuses_generation_and_mutation_changes_it(tmp_path):
    manifest = _complete_series(tmp_path)
    first = generation.build_inventory_generation_receipt(tmp_path, manifest)
    replay = generation.build_inventory_generation_receipt(tmp_path, manifest)

    assert replay["inventoryGeneration"] == first["inventoryGeneration"]

    child_path = tmp_path / "runs" / "cbre" / "manifest.json"
    child = json.loads(child_path.read_text(encoding="utf-8"))
    child["sources"]["cbre"]["readback"]["inventory_fingerprint"]["rowCount"] = 2
    child_path.write_text(json.dumps(child), encoding="utf-8")
    changed = generation.build_inventory_generation_receipt(tmp_path, manifest)

    assert (
        changed["inventoryGeneration"]["generationId"]
        != first["inventoryGeneration"]["generationId"]
    )


def test_failed_or_partial_series_preserves_last_good_canonical(tmp_path):
    series_dir = tmp_path / "series"
    series_dir.mkdir()
    manifest = _complete_series(series_dir)
    canonical = generation.publish_series_health(series_dir, manifest)
    before = canonical.read_bytes()

    manifest["status"] = "complete_with_source_failures"
    manifest["sources"]["cbre"]["state"] = "failed_source"
    generation.publish_series_health(series_dir, manifest)

    assert canonical.read_bytes() == before
    status = json.loads(
        (series_dir / "source-health-publication.json").read_text(encoding="utf-8")
    )
    assert status["status"] == "not_advanced"
    assert status["reason"] == "inventory_generation_incomplete"


def test_missing_readback_fingerprint_cannot_publish(tmp_path):
    manifest = _complete_series(tmp_path)
    child_path = tmp_path / "runs" / "cbre" / "manifest.json"
    child = json.loads(child_path.read_text(encoding="utf-8"))
    del child["sources"]["cbre"]["readback"]["inventory_fingerprint"]
    child_path.write_text(json.dumps(child), encoding="utf-8")

    try:
        generation.build_inventory_generation_receipt(tmp_path, manifest)
    except generation.InventoryGenerationNotReady as exc:
        assert "cbre has no complete inventory fingerprint readback" in str(exc)
    else:  # pragma: no cover - safety assertion
        raise AssertionError("receipt published without a fingerprint readback")
