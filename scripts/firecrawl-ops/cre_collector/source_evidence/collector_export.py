"""Verify and privately reconstruct a retained collector artifact."""
import copy
import json
from datetime import datetime, timezone

from source_evidence import acquisition, collector, events


def _instant(value):
    if not isinstance(value, str):
        raise ValueError("collector event time is invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("collector event time has no timezone")
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError("collector event time is invalid") from exc


def reconstruct(rows):
    """Verify complete event readback and return canonical artifact bytes and metadata."""
    wrapped = collector.from_events(rows)
    provenance = wrapped.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("collector capture provenance is invalid")
    source_clock = events.timestamp(provenance.get("observedAt"))
    instant = _instant(source_clock)
    normalized = copy.deepcopy(rows)
    for row in normalized:
        if _instant(row["observed_at"]) != instant:
            raise ValueError("collector event observation times disagree")
        row["observed_at"] = source_clock
    expected = events.plan(wrapped, source_key=normalized[0]["source_key"])
    events.validate_content_policy(expected)
    if normalized != expected["events"]:
        raise ValueError("collector events disagree with reconstructed artifact")
    original = collector.reconstruct(wrapped)
    canonical = events.encoded(original)
    if "numericLiterals" in provenance:
        literals = provenance["numericLiterals"]
        canonical = acquisition.encode_json(json.loads(canonical), literals).encode("utf-8")
        if acquisition.decode_json(canonical) != (original, literals):
            raise ValueError("collector numeric literal map disagrees with artifact")
    if len(canonical) > events.MAX_ARTIFACT_BYTES:
        raise ValueError("reconstructed collector artifact exceeds bound")
    digest = events.sha256(canonical)
    if digest != provenance.get("originalArtifactSha256"):
        raise ValueError("collector artifact digest mismatch")
    projection = provenance.get("projection")
    metadata = {"schema_version": "cre-source-evidence-collector-export/v1",
                "capture_id": normalized[0]["capture_id"],
                "source_key": normalized[0]["source_key"],
                "observation_schema": wrapped["schema"],
                "listing_count": len(wrapped["records"]),
                "observation_count": sum(len(source["retainedObservations"]) for source in original["sources"]
                                         if isinstance(source.get("retainedObservations"), list)),
                "separate_observation_count": collector.record_count(wrapped) - len(wrapped["records"]),
                "counts_basis": "collector_artifact_not_canonical_inventory",
                "source_provenance": copy.deepcopy(normalized[0]["provenance"]),
                "collector_provenance": copy.deepcopy(provenance),
                "representation": "source_projected_collector_artifact" if projection is not None else "collector_artifact",
                "projection_version": projection.get("version") if isinstance(projection, dict) else None,
                "artifact_hash_kind": provenance.get("originalArtifactHashKind"),
                "export_sha256": digest, "export_bytes": len(canonical),
                "encoding": "canonical_json_with_exceptional_numeric_literals_not_source_wire_bytes",
                "coverage": "unverified", "publisher_identity": "unverified"}
    return canonical, metadata


def export(rows, outdir):
    """Write verified collector JSON and metadata to a new private directory."""
    from source_evidence import private_export

    payload, metadata = reconstruct(rows)
    private_export.write(outdir, [("collector.json", payload),
                                  ("metadata.json", events.encoded(metadata) + b"\n")])
    return metadata
