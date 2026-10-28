"""Synthetic fixture-only full-artifact planner; not approved for real inputs."""

import hashlib
import re

import buildout_observation_manifest as manifest

MAX_BYTES = 16777216


def digest(value):
    return hashlib.sha256(value).hexdigest()


def plan(*, original, artifact, seal, scope, slot=0):
    if scope.get("sourceKey") != "fortis-net-lease":
        raise ValueError("fixture source not allowed")
    capture = scope.get("captureId")
    if not isinstance(capture, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", capture
    ):
        raise ValueError("invalid fixture capture")
    if type(slot) is not int or not 0 <= slot <= 1000000:
        raise ValueError("invalid receipt slot")
    if len(original) > MAX_BYTES or len(artifact) > MAX_BYTES:
        raise ValueError("fixture byte limit")
    source = manifest.parse_object(original, MAX_BYTES)
    # A caller-controlled marker is a tripwire, not real authorization.
    if source.get("syntheticFixture") is not True:
        raise ValueError("synthetic fixture marker required")
    verified = manifest.verify(seal, original, artifact, expected_scope=scope)
    data = manifest.parse_object(artifact, manifest.artifact_builder.MAX_OUTPUT_BYTES)
    records = data.get("records")
    if not isinstance(records, list) or len(records) > 1000:
        raise ValueError("invalid fixture records")
    assertions = {
        "source": scope["sourceKey"],
        "generation": scope["generationId"],
        "page": scope["page"],
        "transaction": scope["transaction"],
        "origin": "operator_asserted_unverified",
    }
    provenance = {
        "sourceAuthorization": "not_evaluated",
        "sourceCompleteness": "unverified",
        "integrity": "local_byte_consistency_only",
        "promotion": "not_authorized",
        "privacy": "fixture_only_heuristics_not_guaranteed",
        "quarantinedItems": data["summary"]["quarantinedItems"],
        "upstreamReportedOmissions": data["summary"]["upstreamReportedOmissions"],
        "upstreamOmissionsUnknownRecords": data["summary"][
            "upstreamOmissionsUnknownRecords"
        ],
        "manifestDigest": digest(seal),
        "manifestSchema": verified["schema"],
    }
    return {
        "p_capture_id": capture,
        "p_capture_page": slot,
        "p_assertions": assertions,
        "p_provenance": provenance,
        "p_original_sha256": digest(original),
        "p_original_bytes_length": len(original),
        "p_artifact_bytes": artifact,
        "p_artifact_sha256": digest(artifact),
        "p_observations": records,
    }
