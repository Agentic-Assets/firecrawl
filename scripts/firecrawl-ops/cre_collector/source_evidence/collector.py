"""Wrap a single-source collector artifact without interpreting its records."""

import copy
import json
import re

from source_evidence import events

SCHEMA = "cre-retained-observations/v1"
SCHEMA_V2 = "cre-retained-observations/v2"
SCHEMAS = (SCHEMA, SCHEMA_V2)
AUTO_INTEGRAL_FLOATS = object()


def convert(artifact, *, source_key, policy_binding=None, numeric_literals=None, projection=None, schema=None,
            integral_floats=AUTO_INTEGRAL_FLOATS):
    if (not isinstance(artifact, dict) or not isinstance(source_key, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", source_key)):
        raise ValueError("invalid collector artifact")
    sources, records, meta = artifact.get("sources"), artifact.get("listings"), artifact.get("runMeta")
    if not isinstance(sources, list) or not sources or not isinstance(records, list) or not isinstance(meta, dict):
        raise ValueError("invalid collector artifact shape")
    if (any(not isinstance(row, dict) or row.get("sourceKey") != source_key for row in sources)
            or any(not isinstance(row, dict) or row.get("sourceKey") != source_key for row in records)):
        raise ValueError("mixed or mismatched collector source")
    if schema is not None and schema not in SCHEMAS:
        raise ValueError("unsupported collector observation schema")
    from source_evidence import number_witness
    if integral_floats is AUTO_INTEGRAL_FLOATS:
        integral_floats = number_witness.collect(artifact, numeric_literals) or None
    elif integral_floats is not None:
        restored = number_witness.restore(artifact, integral_floats, numeric_literals)
        if events.encoded(restored) != events.encoded(artifact):
            raise ValueError("collector input requires numeric witness restoration")
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
    if projection is not None:
        from source_evidence import collector_projection
        collector_projection.validate(artifact, projection, source_key=source_key, numeric_literals=numeric_literals)
        digest_kind = "projected_" + digest_kind
    binding_fields = {"policySha256", "collectorGitSha", "targetSha256", "transportSha256", "artifactFileSha256", "generation", "attempt"}
    if projection is not None:
        binding_fields.add("artifactFileHashKind")
    if policy_binding is not None and (not isinstance(policy_binding, dict) or set(policy_binding) != binding_fields
                or any(not isinstance(policy_binding[k], str) or not re.fullmatch(r"[a-f0-9]{64}", policy_binding[k])
                       for k in ("policySha256", "targetSha256", "transportSha256", "artifactFileSha256"))
                or not isinstance(policy_binding["collectorGitSha"], str)
                or not re.fullmatch(r"[a-f0-9]{40}", policy_binding["collectorGitSha"])
                or not isinstance(policy_binding["generation"], str) or not policy_binding["generation"]
                or type(policy_binding["attempt"]) is not int or policy_binding["attempt"] < 1
                or (projection is not None and (policy_binding["artifactFileHashKind"] != digest_kind
                    or policy_binding["artifactFileSha256"] != events.sha256(digest_bytes)))):
        raise ValueError("invalid automatic policy binding")
    has_observations = any(isinstance(row.get("retainedObservations"), list)
                           and row["retainedObservations"] for row in sources)
    selected = schema if schema is not None else (SCHEMA_V2 if has_observations else SCHEMA)
    if selected == SCHEMA_V2:
        from source_evidence import collector_observations
        context, groups, observations = collector_observations.split(artifact)
        if not groups:
            raise ValueError("v2 collector observation requires nonempty held observations")
    else:
        context = {k: v for k, v in artifact.items() if k != "listings"}
    result = {"schema": selected, "provenance": {"sourceKey": source_key, "observedAt": clock,
            "observedAtOrigin": "collector_run_finished_at", "recordKind": "collector_emitted_listing",
            "originalArtifactSha256": events.sha256(digest_bytes),
            "originalArtifactHashKind": digest_kind,
            **({"projection": projection, "originalArtifactRepresentation": "projected_collector_artifact",
                "originalSourceHashesVerification": "omitted_due_to_projection"} if projection is not None else {}),
            **({"automaticPolicy": policy_binding} if policy_binding is not None else {}),
            **({"integralFloats": integral_floats, "integralFloatEncoding": number_witness.ENCODING}
               if integral_floats is not None else {}),
            **({"numericLiterals": numeric_literals,
                "numericEncoding": "json_pointer_to_literal_null_placeholder_v1"}
               if numeric_literals is not None else {})},
            "context": context, "records": records}
    if selected == SCHEMA_V2:
        result.update(observationGroups=groups, observations=observations)
    return copy.deepcopy(result)


def reconstruct(wrapped):
    """Restore the source artifact representation from either collector envelope."""
    if not isinstance(wrapped, dict) or wrapped.get("schema") not in SCHEMAS:
        raise ValueError("invalid collector observation envelope")
    context, records = wrapped.get("context"), wrapped.get("records")
    if not isinstance(context, dict) or "listings" in context or not isinstance(records, list):
        raise ValueError("invalid collector observation context")
    if wrapped["schema"] == SCHEMA_V2:
        from source_evidence import collector_observations
        original = collector_observations.restore(context, records, wrapped.get("observationGroups"),
                                                   wrapped.get("observations"))
    else:
        original = {**copy.deepcopy(context), "listings": copy.deepcopy(records)}
    provenance = wrapped.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("invalid collector observation provenance")
    from source_evidence import number_witness
    has_map = "integralFloats" in provenance
    has_encoding = "integralFloatEncoding" in provenance
    if has_map != has_encoding:
        raise ValueError("numeric witness encoding mismatch")
    if has_map:
        if provenance["integralFloatEncoding"] != number_witness.ENCODING:
            raise ValueError("numeric witness encoding mismatch")
        original = number_witness.restore(original, provenance["integralFloats"], provenance.get("numericLiterals"))
    return original


def record_count(wrapped):
    """Count listing and held observation events together for admission bounds."""
    if not isinstance(wrapped, dict) or wrapped.get("schema") not in SCHEMAS:
        raise ValueError("invalid collector observation envelope")
    records = wrapped.get("records")
    if not isinstance(records, list):
        raise ValueError("invalid collector listing records")
    if wrapped["schema"] == SCHEMA:
        return len(records)
    observations = wrapped.get("observations")
    if not isinstance(observations, list):
        raise ValueError("invalid collector held observations")
    return len(records) + len(observations)


def from_events(rows):
    """Reassemble the ordered collector envelope; callers must replan and verify."""
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10001:
        raise ValueError("invalid collector event count")
    if any(not isinstance(row, dict) or set(row) != set(events.EVENT_FIELDS)
           or type(row.get("item_no")) is not int or row["item_no"] != index
           for index, row in enumerate(rows)):
        raise ValueError("invalid collector event fields or ordering")
    def fact(row):
        facts = row.get("facts")
        if (not isinstance(facts, list) or len(facts) != 1 or not isinstance(facts[0], dict)
                or set(facts[0]) != {"kind", "value"}):
            raise ValueError("invalid collector event fact")
        return facts[0]
    header = fact(rows[0])
    if header["kind"] not in ("retained_capture_context", "retained_capture_context_v2"):
        raise ValueError("collector capture header is required")
    metadata = header["value"]
    expected_schema = SCHEMA_V2 if header["kind"] == "retained_capture_context_v2" else SCHEMA
    if (not isinstance(metadata, dict) or metadata.get("schema") != expected_schema
            or "records" in metadata or "observations" in metadata):
        raise ValueError("invalid collector capture header")
    records, observations = [], []
    saw_observations = False
    for row in rows[1:]:
        item = fact(row)
        if item["kind"] == "source_record" and not saw_observations:
            records.append(copy.deepcopy(item["value"]))
        elif expected_schema == SCHEMA_V2 and item["kind"] == "source_observation":
            saw_observations = True
            observations.append(copy.deepcopy(item["value"]))
        else:
            raise ValueError("invalid collector event fact order")
    wrapped = {**copy.deepcopy(metadata), "records": records}
    if expected_schema == SCHEMA_V2:
        wrapped["observations"] = observations
    # Validate positions and completeness before returning the envelope.
    reconstruct(wrapped)
    return wrapped


def restore_event_numbers(rows):
    """Restore witnessed source numbers only at their exact event fact locations."""
    from source_evidence import acquisition, number_witness

    wrapped = from_events(rows)
    provenance = wrapped["provenance"]
    if "integralFloats" not in provenance:
        return wrapped, copy.deepcopy(rows)
    witnesses = provenance["integralFloats"]
    normalized = copy.deepcopy(rows)
    observation_positions = {}
    observation_sources = set()
    if wrapped["schema"] == SCHEMA_V2:
        observation_positions = {(item["sourceIndex"], item["observationIndex"]): index
                                 for index, item in enumerate(wrapped["observations"])}
        observation_sources = {group["sourceIndex"] for group in wrapped["observationGroups"]}
    for pointer, literal in witnesses.items():
        parts = acquisition._pointer_parts(pointer)
        if not parts:
            raise ValueError("collector witness cannot target root")
        if parts[0] == "listings":
            if len(parts) < 2:
                raise ValueError("invalid listing numeric witness")
            index = acquisition._child(wrapped["records"], parts[1])
            row_index = 1 + index
            target = ["facts", 0, "value", *parts[2:]]
        elif (wrapped["schema"] == SCHEMA_V2 and len(parts) >= 4
              and parts[0] == "sources" and parts[2] == "retainedObservations"
              and int(parts[1]) in observation_sources):
            sources = wrapped["context"]["sources"]
            source_index = acquisition._child(sources, parts[1])
            if not re.fullmatch(r"0|[1-9][0-9]*", parts[3]):
                raise ValueError("invalid held observation numeric witness")
            identity = (source_index, int(parts[3]))
            if identity not in observation_positions:
                raise ValueError("held observation numeric witness is outside its group")
            row_index = 1 + len(wrapped["records"]) + observation_positions[identity]
            target = ["facts", 0, "value", "value", *parts[4:]]
        else:
            row_index = 0
            target = ["facts", 0, "value", "context", *parts]
        node = normalized[row_index]
        for part in target[:-1]:
            node = node[part] if type(part) is int else node[acquisition._child(node, part)]
        key = target[-1]
        key = key if type(key) is int else acquisition._child(node, key)
        node[key] = number_witness.restore_value(node[key], literal)
    corrected = from_events(normalized)
    return corrected, normalized
