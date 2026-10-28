"""Private parsed Buildout JSON observations; not original HTTP bytes."""
import copy
import re
from datetime import datetime

from source_evidence import acquisition, events
from source_evidence.buildout_scope import scope_hash

SCHEMA = "cre-buildout-parsed-observation/v1"
RECEIPT_KEYS = {"sourceKey", "page", "providerScopeSha256", "inventorySort", "capturedAt",
                "sourceObservedAt", "cacheDisposition", "transport", "bodySha256", "bodyBytes"}
PROVENANCE = {"representation": "collector_parsed_json_not_response_bytes",
              "numbers": "javascript_number_precision", "identity": "unverified",
              "visibility": "unverified", "coverage": "unverified",
              "originalBodyHashVerification": "caller_asserted_not_database_reverified",
              "httpStatus": "unobserved", "finalUrl": "unobserved"}


def receipt_checked(receipt):
    if (not isinstance(receipt, dict) or set(receipt) not in (RECEIPT_KEYS, RECEIPT_KEYS | {"feedKey"})
            or not isinstance(receipt.get("sourceKey"), str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", receipt["sourceKey"])
            or type(receipt.get("page")) is not int or not 0 <= receipt["page"] <= 9007199254740991
            or receipt.get("inventorySort") not in (None, "created_at asc, id asc")
            or receipt.get("cacheDisposition") not in ("live", "generation_cache")
            or receipt.get("transport") not in (None, "unknown", "direct_json", "firecrawl_json")
            or type(receipt.get("bodyBytes")) is not int or not 0 <= receipt["bodyBytes"] <= events.MAX_ARTIFACT_BYTES
            or not isinstance(receipt.get("bodySha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", receipt["bodySha256"])
            or not isinstance(receipt.get("providerScopeSha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", receipt["providerScopeSha256"])):
        raise ValueError("Buildout parsed receipt mismatch")
    if "feedKey" in receipt and not isinstance(receipt["feedKey"], str):
        raise ValueError("Buildout feed key mismatch")
    if receipt["providerScopeSha256"] != scope_hash(receipt["sourceKey"], receipt["inventorySort"], receipt.get("feedKey")):
        raise ValueError("Buildout provider scope mismatch")
    captured = events.timestamp(receipt.get("capturedAt"))
    observed = receipt.get("sourceObservedAt")
    if observed is not None:
        observed = events.timestamp(observed)
        if datetime.fromisoformat(observed.replace("Z", "+00:00")) > datetime.fromisoformat(captured.replace("Z", "+00:00")):
            raise ValueError("Buildout observation clock follows capture")


def _binding_checked(binding):
    allowed = {"policySha256", "targetSha256", "collectorGitSha", "transportSha256", "generation", "attempt"}
    if not isinstance(binding, dict) or binding.keys() - allowed:
        raise ValueError("Buildout capture binding mismatch")
    for key, value in binding.items():
        if key == "attempt":
            if type(value) is not int or not 1 <= value <= 1000000:
                raise ValueError("Buildout attempt binding mismatch")
        elif key == "generation":
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value):
                raise ValueError("Buildout generation binding mismatch")
        elif not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}" if key == "collectorGitSha" else r"[a-f0-9]{64}", value):
            raise ValueError("Buildout digest binding mismatch")


def convert(document, literals, *, receipt, capture_context):
    """Retain selected rows and surrounding context or the whole unmapped value."""
    receipt_checked(receipt)
    _binding_checked(capture_context)
    if not isinstance(literals, dict):
        raise ValueError("Buildout numeric literal map mismatch")  # noqa: TRY004
    reconstructed = acquisition.encode_json(document, literals).encode("utf8")
    decoded, decoded_literals = acquisition.decode_json(reconstructed)
    if events.encoded(decoded) != events.encoded(document) or events.encoded(decoded_literals) != events.encoded(literals):
        raise ValueError("Buildout numeric literals disagree with reconstructed JSON")
    selected = isinstance(document, dict) and isinstance(document.get("inventory"), list)
    if selected and len(document["inventory"]) > 10000:
        raise ValueError("Buildout inventory exceeds record bound")
    if selected:
        context = copy.deepcopy(document)
        records = context["inventory"]
        context["inventory"] = copy.deepcopy(acquisition.PLACEHOLDER)
    else:
        context = copy.deepcopy(document)
        records = []
    return {"schema": SCHEMA, "receipt": copy.deepcopy(receipt),
            "arrayPointer": "/inventory" if selected else None, "context": context,
            "records": records, "numericLiterals": copy.deepcopy(literals),
            "captureContext": copy.deepcopy(capture_context), "provenance": copy.deepcopy(PROVENANCE)}


def reconstruct(observation):
    if not isinstance(observation, dict) or observation.get("schema") != SCHEMA:
        raise ValueError("invalid Buildout observation")  # noqa: TRY004
    pointer = observation.get("arrayPointer")
    if pointer not in (None, "/inventory"):
        raise ValueError("invalid Buildout array selection")
    proxy = {**observation, "schema": acquisition.SCHEMA if pointer else acquisition.SCHEMA_CONTEXT}
    return acquisition._reconstruct_values(proxy)


def validate(observation):
    document = reconstruct(observation)
    expected = convert(document, observation.get("numericLiterals"), receipt=observation.get("receipt"),
                       capture_context=observation.get("captureContext"))
    if observation != expected:
        raise ValueError("Buildout observation differs from its representation")
