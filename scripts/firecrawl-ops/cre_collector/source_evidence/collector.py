"""Wrap a single-source collector artifact without interpreting its records."""

import re

from source_evidence import events

SCHEMA = "cre-retained-observations/v1"


def convert(artifact, *, source_key):
    if (not isinstance(artifact, dict) or not isinstance(source_key, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", source_key)):
        raise ValueError("invalid collector artifact")
    sources, records, meta = artifact.get("sources"), artifact.get("listings"), artifact.get("runMeta")
    if not isinstance(sources, list) or not sources or not isinstance(records, list) or not isinstance(meta, dict):
        raise ValueError("invalid collector artifact shape")
    if (any(not isinstance(row, dict) or row.get("sourceKey") != source_key for row in sources)
            or any(not isinstance(row, dict) or row.get("sourceKey") != source_key for row in records)):
        raise ValueError("mixed or mismatched collector source")
    clock = events.timestamp(meta.get("finishedAt"))
    if len(records) > 10000 or len(events.encoded(artifact)) > events.MAX_ARTIFACT_BYTES:
        raise ValueError("collector artifact exceeds limit")
    return {"schema": SCHEMA, "provenance": {"sourceKey": source_key, "observedAt": clock,
            "observedAtOrigin": "collector_run_finished_at", "recordKind": "collector_emitted_listing",
            "originalArtifactSha256": events.sha256(events.encoded(artifact)),
            "originalArtifactHashKind": "canonical_json_utf8"},
            "context": {k: v for k, v in artifact.items() if k != "listings"}, "records": records}
