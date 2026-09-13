#!/usr/bin/env python3
"""Publish a generation-bound producer-freshness-v2 inventory receipt.

The checkpoint-series manifest remains authoritative. This sidecar advances
only after the complete required-source series and each child's database
readback prove the exact live inventory fingerprint. Failed, partial, running,
or interrupted series never replace the last-good canonical receipt.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import cre_source_health as legacy_health
from cre_source_policy import load_source_policy


CONTRACT_VERSION = "producer-freshness-v2"
PRODUCER_ID = "firecrawl-cre-listings"
SOURCE_VOCABULARY_VERSION = "firecrawl-source-registry-v1"
REQUIRED_SOURCE_IDS = frozenset(load_source_policy())
_FINGERPRINT_FIELDS = {
    "sourceId",
    "rowCount",
    "maxRowUpdatedAt",
    "maxObservationAt",
    "publicationStatus",
}


class InventoryGenerationNotReady(RuntimeError):
    """A series cannot advance the last-good inventory generation."""


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _canonical_digest(fingerprints: Mapping[str, Mapping[str, Any]]) -> str:
    payload = json.dumps(
        dict(sorted(fingerprints.items())),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _valid_fingerprint(value: object, source_id: str) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == _FINGERPRINT_FIELDS
        and value.get("sourceId") == source_id
        and value.get("publicationStatus") == "complete"
        and isinstance(value.get("rowCount"), int)
        and not isinstance(value.get("rowCount"), bool)
        and value["rowCount"] >= 0
        and _parse_timestamp(value.get("maxRowUpdatedAt")) is not None
        and _parse_timestamp(value.get("maxObservationAt")) is not None
    )


def _child_fingerprint(
    series_dir: Path, checkpoint: Mapping[str, Any], source_id: str
) -> dict[str, Any]:
    relative = checkpoint.get("checkpoint_run")
    if not isinstance(relative, str) or not relative:
        raise InventoryGenerationNotReady(f"{source_id} has no checkpoint run")
    child_path = (series_dir / relative / "manifest.json").resolve()
    try:
        child_path.relative_to(series_dir.resolve())
        child = json.loads(child_path.read_text(encoding="utf-8"))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        raise InventoryGenerationNotReady(
            f"{source_id} checkpoint manifest is unavailable"
        ) from exc
    source = (child.get("sources") or {}).get(source_id) or {}
    readback = source.get("readback") or {}
    fingerprint = readback.get("inventory_fingerprint")
    if readback.get("ok") is not True or not _valid_fingerprint(fingerprint, source_id):
        raise InventoryGenerationNotReady(
            f"{source_id} has no complete inventory fingerprint readback"
        )
    return dict(fingerprint)


def build_inventory_generation_receipt(
    series_dir: Path, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the v2 receipt only for one complete required-source series."""
    manifest_sources = manifest.get("sources")
    configured_sources = (manifest.get("config") or {}).get("sources")
    if (
        manifest.get("status") != "complete"
        or not isinstance(manifest_sources, Mapping)
        or set(manifest_sources) != REQUIRED_SOURCE_IDS
        or not isinstance(configured_sources, list)
        or set(configured_sources) != REQUIRED_SOURCE_IDS
        or len(configured_sources) != len(REQUIRED_SOURCE_IDS)
    ):
        raise InventoryGenerationNotReady(
            "series is not a complete required-source generation"
        )

    base = legacy_health.build_receipt(series_dir, manifest)
    fingerprints: dict[str, dict[str, Any]] = {}
    for source_id in sorted(REQUIRED_SOURCE_IDS):
        checkpoint = manifest_sources[source_id]
        if not isinstance(checkpoint, Mapping) or checkpoint.get("state") != "complete":
            raise InventoryGenerationNotReady(f"{source_id} is not complete")
        source_health = (base.get("sources") or {}).get(source_id)
        if (
            not isinstance(source_health, dict)
            or source_health.get("publicationStatus") != "complete"
        ):
            raise InventoryGenerationNotReady(
                f"{source_id} has no complete source-health evidence"
            )
        fingerprint = _child_fingerprint(series_dir, checkpoint, source_id)
        fingerprints[source_id] = fingerprint
        source_health["inventoryFingerprint"] = fingerprint

    digest = _canonical_digest(fingerprints)
    base["contractVersion"] = CONTRACT_VERSION
    base["producerId"] = PRODUCER_ID
    base["inventoryGeneration"] = {
        "generationId": f"inventory-generation:{digest}",
        "digest": digest,
        "sourceVocabularyVersion": SOURCE_VOCABULARY_VERSION,
        "complete": True,
    }
    return base


def _valid_prior_v2(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    generation = value.get("inventoryGeneration")
    sources = value.get("sources")
    if (
        value.get("contractVersion") != CONTRACT_VERSION
        or value.get("producerId") != PRODUCER_ID
        or _parse_timestamp(value.get("producerComputedAt")) is None
        or not isinstance(generation, Mapping)
        or generation.get("sourceVocabularyVersion") != SOURCE_VOCABULARY_VERSION
        or generation.get("complete") is not True
        or not isinstance(sources, Mapping)
        or set(sources) != REQUIRED_SOURCE_IDS
    ):
        return False
    try:
        fingerprints = {
            source_id: source["inventoryFingerprint"]
            for source_id, source in sources.items()
        }
    except (KeyError, TypeError):
        return False
    if any(
        not isinstance(sources[source_id], Mapping)
        or sources[source_id].get("sourceId") != source_id
        or sources[source_id].get("publicationStatus") != "complete"
        or not _valid_fingerprint(fingerprints[source_id], source_id)
        for source_id in REQUIRED_SOURCE_IDS
    ):
        return False
    digest = _canonical_digest(fingerprints)
    return (
        generation.get("digest") == digest
        and generation.get("generationId") == f"inventory-generation:{digest}"
    )


def publish_series_health(series_dir: Path, manifest: Mapping[str, Any]) -> Path:
    """Atomically advance the canonical v2 receipt, or retain last-good state."""
    canonical = series_dir.parent / "producer-source-health.json"
    if manifest.get("status") != "complete":
        legacy_health._write_publication_status(
            series_dir,
            manifest,
            status="not_advanced",
            reason="inventory_generation_incomplete",
        )
        return canonical

    try:
        receipt = build_inventory_generation_receipt(series_dir, manifest)
        with legacy_health._canonical_lock(canonical):
            previous = (
                legacy_health._read_json(canonical) if canonical.exists() else None
            )
            if previous is not None and _valid_prior_v2(previous):
                previous_at = _parse_timestamp(previous.get("producerComputedAt"))
                receipt_at = _parse_timestamp(receipt.get("producerComputedAt"))
                if (
                    previous_at is not None
                    and receipt_at is not None
                    and previous_at > receipt_at
                ):
                    receipt = previous
            elif (
                previous is not None
                and previous.get("contractVersion") == CONTRACT_VERSION
            ):
                legacy_health._write_publication_status(
                    series_dir,
                    manifest,
                    status="degraded",
                    reason="canonical_receipt_invalid",
                )
                return canonical
            legacy_health._atomic_write(series_dir / "source-health.json", receipt)
            legacy_health._atomic_write(canonical, receipt)
        legacy_health._write_publication_status(
            series_dir, manifest, status="ok", reason=None
        )
    except InventoryGenerationNotReady:
        legacy_health._write_publication_status(
            series_dir,
            manifest,
            status="not_advanced",
            reason="inventory_generation_incomplete",
        )
    except Exception:
        legacy_health._write_publication_status(
            series_dir,
            manifest,
            status="degraded",
            reason="receipt_output_failed",
        )
        raise RuntimeError("producer inventory-generation publication failed") from None
    return canonical
