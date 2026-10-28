"""Decode bounded private Buildout parsed-page captures without HTTP claims."""
import base64
import binascii
import hashlib
import json

from buildout_observation_artifact import invalid_number, unique_object
from source_evidence import acquisition, buildout_response, events
from source_evidence.buildout_scope import scope_hash
from source_evidence.buildout_scope import supported_sources as supported_sources

FORMAT = "buildout_inventory_parsed_v1"
MAX_ENTRY_BYTES = ((16 * 1024 * 1024 * 4 + 2) // 3) + 16384
PROVENANCE = {"representation": "collector_parsed_json_not_response_bytes",
              "numbers": "javascript_number_precision", "identity": "unverified",
              "visibility": "unverified", "coverage": "unverified"}


def decode_entry(raw, filename, binding, *, source_key):
    """Verify a private entry then retain all parsed JSON or an invalid diagnostic."""
    if not isinstance(raw, bytes) or len(raw) > MAX_ENTRY_BYTES:
        raise ValueError("Buildout parsed entry exceeds bounds")
    entry = json.loads(raw.decode("utf8"), object_pairs_hook=unique_object, parse_constant=invalid_number)
    if (not isinstance(entry, dict) or set(entry) != {"schema", "receipt", "responseBodyBase64", "provenance"}
            or entry["schema"] != "cre-buildout-parsed-page/v1" or entry["provenance"] != PROVENANCE):
        raise ValueError("Buildout parsed entry contract mismatch")
    receipt = entry["receipt"]
    buildout_response.receipt_checked(receipt)
    if not isinstance(source_key, str) or receipt["sourceKey"] != source_key:
        raise ValueError("Buildout source is not registered")
    if receipt["providerScopeSha256"] != scope_hash(source_key, receipt["inventorySort"]):
        raise ValueError("Buildout provider scope mismatch")
    if filename != f"page-{receipt['page']}-{hashlib.sha256(raw).hexdigest()}.json":
        raise ValueError("Buildout parsed page name mismatch")
    try:
        body = base64.b64decode(entry["responseBodyBase64"], validate=True)
    except (TypeError, ValueError, binascii.Error) as exc:
        raise ValueError("Buildout body encoding invalid") from exc
    if (base64.b64encode(body).decode() != entry["responseBodyBase64"] or len(body) > events.MAX_ARTIFACT_BYTES
            or len(body) != receipt["bodyBytes"] or hashlib.sha256(body).hexdigest() != receipt["bodySha256"]):
        raise ValueError("Buildout parsed body hash mismatch")
    try:
        document, literals = acquisition.decode_json(body)
    except (ValueError, TypeError, KeyError, RecursionError):
        return None, "invalid_json"
    return buildout_response.convert(document, literals, receipt=receipt, capture_context=binding), None
