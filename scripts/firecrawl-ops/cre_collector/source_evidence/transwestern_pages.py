"""Source-bound parsed Transwestern feed observations, never HTTP receipts."""
import base64
import binascii
import copy
import hashlib
import json

from buildout_observation_artifact import invalid_number, unique_object

from source_evidence import acquisition, buildout_response, events

FORMAT = "transwestern_inventory_parsed_v1"
SCHEMA = "cre-transwestern-parsed-observation/v1"
PROVENANCE = {"representation": "collector_parsed_json_not_response_bytes",
              "parser": "unwrap_extract_or_repair_possible", "numbers": "javascript_number_precision_and_serialization",
              "httpStatus": "unobserved", "finalUrl": "unobserved", "sourceFreshness": "unverified",
              "identity": "unverified", "visibility": "unverified", "coverage": "unverified",
              "originalBodyHashVerification": "caller_asserted_not_database_reverified"}
BUCKETS = {"sale": ["Sale", "Sale or Lease"], "lease": ["Lease", "Sublease", "Sale or Lease"]}
MAX_ENTRY = ((16 * 1024 * 1024 * 4 + 2) // 3) + 64 * 1024 * 6 + 16384


def receipt_checked(receipt):
    keys = {"sourceKey", "transaction", "bucket", "occurrence", "capturedAt", "freshRequested", "bodySha256", "bodyBytes"}
    if not isinstance(receipt, dict) or set(receipt) != keys or receipt.get("sourceKey") != "transwestern":
        raise ValueError("Transwestern receipt scope mismatch")
    tx, n = receipt["transaction"], receipt["occurrence"]
    if (not isinstance(tx, str) or tx not in BUCKETS or type(n) is not int or not 0 <= n < len(BUCKETS[tx])
            or receipt["bucket"] != BUCKETS[tx][n] or type(receipt["freshRequested"]) is not bool
            or type(receipt["bodyBytes"]) is not int or not 0 <= receipt["bodyBytes"] <= events.MAX_ARTIFACT_BYTES
            or not isinstance(receipt["bodySha256"], str) or len(receipt["bodySha256"]) != 64
            or any(c not in "0123456789abcdef" for c in receipt["bodySha256"])):
        raise ValueError("Transwestern receipt mismatch")
    events.timestamp(receipt["capturedAt"])


def convert(document, literals, *, receipt, capture_context):
    receipt_checked(receipt)
    buildout_response._binding_checked(capture_context)
    if not isinstance(literals, dict):
        raise TypeError("invalid numeric witnesses")
    encoded = acquisition.encode_json(document, literals).encode("utf8")
    decoded, witnesses = acquisition.decode_json(encoded)
    if events.encoded(decoded) != events.encoded(document) or events.encoded(witnesses) != events.encoded(literals):
        raise ValueError("numeric witnesses disagree")
    if isinstance(document, list):
        if len(document) > 10000:
            raise ValueError("feed exceeds record bound")
        records, context, pointer = copy.deepcopy(document), copy.deepcopy(acquisition.PLACEHOLDER), ""
    else:
        records, context, pointer = [], copy.deepcopy(document), None
    return {"schema": SCHEMA, "receipt": copy.deepcopy(receipt), "arrayPointer": pointer,
            "context": context, "records": records, "numericLiterals": copy.deepcopy(literals),
            "captureContext": copy.deepcopy(capture_context), "provenance": copy.deepcopy(PROVENANCE)}


def reconstruct(observation):
    pointer = observation.get("arrayPointer")
    if pointer not in (None, ""):
        raise ValueError("invalid Transwestern array pointer")
    return acquisition._reconstruct_values({**observation, "schema": acquisition.SCHEMA_CONTEXT if pointer is None else acquisition.SCHEMA})


def validate(observation):
    if not isinstance(observation, dict) or observation.get("schema") != SCHEMA:
        raise ValueError("invalid Transwestern observation")
    document = reconstruct(observation)
    if convert(document, observation.get("numericLiterals"), receipt=observation.get("receipt"),
               capture_context=observation.get("captureContext")) != observation:
        raise ValueError("Transwestern observation mismatch")


def decode_entry(raw, filename, binding):
    if not isinstance(raw, bytes) or len(raw) > MAX_ENTRY:
        raise ValueError("Transwestern entry exceeds bound")
    entry = json.loads(raw.decode("utf8"), object_pairs_hook=unique_object, parse_constant=invalid_number)
    if (not isinstance(entry, dict) or set(entry) != {"schema", "receipt", "responseBodyBase64", "provenance"}
            or entry["schema"] != "cre-transwestern-parsed-page/v1" or entry["provenance"] != PROVENANCE):
        raise ValueError("Transwestern entry mismatch")
    receipt = entry["receipt"]
    receipt_checked(receipt)
    if filename != f"page-{receipt['transaction']}-{receipt['occurrence']}-{hashlib.sha256(raw).hexdigest()}.json":
        raise ValueError("Transwestern filename mismatch")
    try:
        body = base64.b64decode(entry["responseBodyBase64"], validate=True)
    except (TypeError, ValueError, binascii.Error) as exc:
        raise ValueError("invalid body encoding") from exc
    if (base64.b64encode(body).decode() != entry["responseBodyBase64"] or len(body) != receipt["bodyBytes"]
            or hashlib.sha256(body).hexdigest() != receipt["bodySha256"]):
        raise ValueError("Transwestern body mismatch")
    document, literals = acquisition.decode_json(body)
    return convert(document, literals, receipt=receipt, capture_context=binding), None
