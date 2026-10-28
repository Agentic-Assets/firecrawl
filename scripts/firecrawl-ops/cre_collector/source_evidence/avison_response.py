"""Avison projected native observations with honestly limited HTTP metadata."""
import copy
import json
import re
from datetime import datetime

from source_evidence import acquisition, avison_native, events

SCHEMA = "cre-avison-native-observation/v1"
REQUEST_URL = "https://pse-api.sharplaunch.com/data?entity=website&status=active"
RECEIPT_KEYS = {"sourceKey", "requestUrl", "method", "httpOutcome", "startedAt", "finishedAt"}
PROVENANCE = {"bodyKind": "source_projected_canonical_json_not_original_response",
              "transportBodyKind": "collector_decoded_text_reencoded_utf8_not_fetch_or_wire_bytes",
              "httpMetadata": "exact_status_and_final_url_unobserved",
              "identity": "unverified", "visibility": "unverified", "completeness": "unverified",
              "numericEncoding": "json_pointer_to_literal_null_placeholder_v1"}


def _projection_checked(document, projection):
    if (not isinstance(projection, dict) or set(projection) != {"version", "changes", "unresolvedProse"}
            or projection["version"] != avison_native.VERSION or not isinstance(projection["changes"], list)
            or len(projection["changes"]) > avison_native.MAX_CHANGES
            or type(projection["unresolvedProse"]) is not int
            or len(events.encoded(projection)) > avison_native.MAX_RECEIPT_BYTES):
        raise ValueError("Avison projection receipt mismatch")
    seen = set()
    unresolved = 0
    for change in projection["changes"]:
        if (not isinstance(change, dict) or set(change) != {"pointer", "action", "reason"}
                or change["action"] not in ("omitted", "projected")
                or change["reason"] not in ("withheld_asking", "withheld_asking_span", "unresolved_monetary_span",
                                            "withheld_asking_alias", "withheld_url_parameter")):
            raise ValueError("Avison projection change mismatch")
        parts = acquisition._pointer_parts(change["pointer"])
        identity = tuple(change[key] for key in ("pointer", "action", "reason"))
        if not parts or identity in seen:
            raise ValueError("Avison projection change is duplicate or unscoped")
        seen.add(identity)
        unresolved += change["reason"] == "unresolved_monetary_span"
        node = document
        missing = False
        try:
            for part in parts:
                node = node[acquisition._child(node, part)]
        except ValueError:
            missing = True
        if change["action"] == "omitted":
            if not missing or change["reason"] not in ("withheld_asking", "withheld_asking_alias"):
                raise ValueError("Avison omitted path remains present")
        elif missing or not isinstance(node, str):
            raise ValueError("Avison projected path is not a retained string")
    if unresolved != projection["unresolvedProse"]:
        raise ValueError("Avison unresolved projection count mismatch")


def _binding_checked(binding):
    allowed = {"policySha256", "targetSha256", "collectorGitSha", "transportSha256", "generation", "attempt"}
    if not isinstance(binding, dict) or binding.keys() - allowed:
        raise ValueError("Avison capture binding mismatch")
    for key, value in binding.items():
        if key == "attempt":
            if type(value) is not int or not 1 <= value <= 1000000:
                raise ValueError("Avison attempt binding mismatch")
        elif key == "generation":
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value):
                raise ValueError("Avison generation binding mismatch")
        elif not isinstance(value, str) or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})" if key == "collectorGitSha" else r"[a-f0-9]{64}", value):
            raise ValueError("Avison digest binding mismatch")


def receipt_checked(receipt):
    if (not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS
            or receipt.get("sourceKey") != "avison-young" or receipt.get("requestUrl") != REQUEST_URL
            or receipt.get("method") != "GET" or receipt.get("httpOutcome") != "success_status_unobserved"):
        raise ValueError("Avison receipt contract mismatch")
    start, finish = (events.timestamp(receipt.get(key)) for key in ("startedAt", "finishedAt"))
    if datetime.fromisoformat(start.replace("Z", "+00:00")) > datetime.fromisoformat(finish.replace("Z", "+00:00")):
        raise ValueError("Avison receipt clock reversed")


def convert(document, literals, projection, *, receipt, capture_context=None, response_encoding="items_array"):
    """Store projected JSON only; no original body/file hash enters this object."""
    receipt_checked(receipt)
    if response_encoding not in ("items_array", "wrapped_unmapped_json"):
        raise ValueError("Avison response encoding mismatch")
    if response_encoding == "wrapped_unmapped_json" and (set(document) != {"items", "unmappedResponse"}
                                                        or document["items"] != []):
        raise ValueError("Avison unmapped response cannot assert listing records")
    avison_native.validate_projected(document, literals)
    _projection_checked(document, projection)
    if capture_context is not None:
        _binding_checked(capture_context)
    # Reconstruction preserves unknown envelope fields as well as every row.
    context = copy.deepcopy(document)
    records = context["items"]
    context["items"] = copy.deepcopy(acquisition.PLACEHOLDER)
    # Plans and JSONB readback reorder object keys. Canonicalize key order before
    # restoring exceptional numeric literals, so sealing cannot change this hash.
    projected_bytes = acquisition.encode_json(json.loads(events.encoded(document)), literals).encode("utf8")
    if len(projected_bytes) > events.MAX_ARTIFACT_BYTES:
        raise ValueError("Avison projected response exceeds bounds")
    result = {"schema": SCHEMA, "receipt": copy.deepcopy(receipt), "arrayPointer": "/items",
            "responseEncoding": response_encoding,
            "context": context, "records": records, "numericLiterals": copy.deepcopy(literals),
            "projection": copy.deepcopy(projection),
            **({"captureContext": copy.deepcopy(capture_context)} if capture_context is not None else {}),
            "provenance": {**PROVENANCE, "projectedCanonicalSha256": events.sha256(projected_bytes),
                           "projectedCanonicalBytes": len(projected_bytes)}}
    from source_evidence.automatic import _refuse_credential_fields
    _refuse_credential_fields(result)
    return result


def reconstruct(observation):
    if (not isinstance(observation, dict) or observation.get("schema") != SCHEMA
            or observation.get("arrayPointer") != "/items" or not isinstance(observation.get("context"), dict)
            or observation["context"].get("items") != acquisition.PLACEHOLDER
            or not isinstance(observation.get("records"), list)):
        raise ValueError("Avison projected observation shape mismatch")
    return {**copy.deepcopy(observation["context"]), "items": copy.deepcopy(observation["records"])}


def validate(observation):
    document = reconstruct(observation)
    expected = convert(document, observation.get("numericLiterals"), observation.get("projection"),
                       receipt=observation.get("receipt"), capture_context=observation.get("captureContext"),
                       response_encoding=observation.get("responseEncoding"))
    if observation != expected:
        raise ValueError("Avison projected observation differs from its representation")
