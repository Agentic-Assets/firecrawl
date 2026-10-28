"""Wrap a single-source collector artifact without interpreting its records."""

import json
import re

from source_evidence import events

SCHEMA = "cre-retained-observations/v1"


def convert(artifact, *, source_key, policy_binding=None, numeric_literals=None):
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
    digest_bytes = events.encoded(artifact)
    digest_kind = "canonical_json_utf8"
    if numeric_literals is not None:
        from source_evidence import acquisition
        reconstructed = acquisition.encode_json(artifact, numeric_literals).encode("utf8")
        if acquisition.decode_json(reconstructed) != (artifact, numeric_literals):
            raise ValueError("numeric literals disagree with reconstructed collector artifact")
        digest_bytes = acquisition.encode_json(json.loads(digest_bytes), numeric_literals).encode("utf8")
        digest_kind = "canonical_json_exact_numeric_literals_utf8"
    if policy_binding is not None and (not isinstance(policy_binding, dict) or set(policy_binding) !=
                {"policySha256", "collectorGitSha", "targetSha256", "transportSha256", "artifactFileSha256", "generation", "attempt"}
                or any(not isinstance(policy_binding[k], str) or not re.fullmatch(r"[a-f0-9]{64}", policy_binding[k])
                       for k in ("policySha256", "targetSha256", "transportSha256", "artifactFileSha256"))
                or not isinstance(policy_binding["collectorGitSha"], str)
                or not re.fullmatch(r"[a-f0-9]{40}", policy_binding["collectorGitSha"])
                or not isinstance(policy_binding["generation"], str) or not policy_binding["generation"]
                or type(policy_binding["attempt"]) is not int or policy_binding["attempt"] < 1):
        raise ValueError("invalid automatic policy binding")
    return {"schema": SCHEMA, "provenance": {"sourceKey": source_key, "observedAt": clock,
            "observedAtOrigin": "collector_run_finished_at", "recordKind": "collector_emitted_listing",
            "originalArtifactSha256": events.sha256(digest_bytes),
            "originalArtifactHashKind": digest_kind,
            **({"automaticPolicy": policy_binding} if policy_binding is not None else {}),
            **({"numericLiterals": numeric_literals,
                "numericEncoding": "json_pointer_to_literal_null_placeholder_v1"}
               if numeric_literals is not None else {})},
            "context": {k: v for k, v in artifact.items() if k != "listings"}, "records": records}
