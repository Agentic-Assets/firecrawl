"""Offline, lossless JSON response observation; receipt claims are unverified."""

import copy
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from source_evidence import events

SCHEMA = "cre-json-response-observation/v1"
SCHEMA_CONTEXT = "cre-json-response-context-observation/v1"
PLACEHOLDER = {"$cre_json_response_array": "records"}
RECEIPT_KEYS = {"sourceKey", "requestUrl", "finalUrl", "method", "status",
                "startedAt", "finishedAt", "bodySha256", "bodyBytes"}
NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")


class _Literal:
    def __init__(self, value):
        self.value = value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _nonfinite(_value):
    raise ValueError("nonfinite JSON number")


def _float(value):
    # Keep exceptional numeric lexemes out of Python's lossy float conversion.
    try:
        number = float(value)
        exact = Decimal(value)
        if (exact != Decimal(repr(number)) or (value.startswith("-") and exact.is_zero())):
            return _Literal(value)
    except (OverflowError, InvalidOperation, ValueError):
        return _Literal(value)
    return number


def _int(value):
    if value == "-0" or len(value) > 1000:
        return _Literal(value)
    return int(value)


def _path(parent, key):
    return parent + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _extract_literals(value, pointer, literals):
    if isinstance(value, _Literal):
        literals[pointer] = value.value
        return None
    if isinstance(value, list):
        return [_extract_literals(v, _path(pointer, i), literals) for i, v in enumerate(value)]
    if isinstance(value, dict):
        return {k: _extract_literals(v, _path(pointer, k), literals) for k, v in value.items()}
    return value


def _pointer_parts(pointer, *, max_bytes=4096, max_depth=128):
    if not isinstance(pointer, str) or len(pointer.encode("utf8")) > max_bytes:
        raise ValueError("invalid JSON pointer")
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError("invalid JSON pointer")
    parts = pointer[1:].split("/")
    if len(parts) > max_depth or any(re.search(r"~(?![01])", part) for part in parts):
        raise ValueError("invalid JSON pointer")
    return [part.replace("~1", "/").replace("~0", "~") for part in parts]


def _child(container, part):
    if isinstance(container, dict) and part in container:
        return part
    if isinstance(container, list) and re.fullmatch(r"0|[1-9][0-9]*", part):
        index = int(part)
        if index < len(container):
            return index
    raise ValueError("JSON pointer does not resolve")


def _replace(document, parts, value):
    if not parts:
        return value
    node = document
    for part in parts[:-1]:
        node = node[_child(node, part)]
    node[_child(node, parts[-1])] = value
    return document


def decode_json(raw, *, max_bytes=events.MAX_ARTIFACT_BYTES):
    """Decode bounded JSON with exceptional numbers retained by absolute pointer."""
    if (type(max_bytes) is not int or not 0 < max_bytes <= 32 * 1024 * 1024
            or not isinstance(raw, bytes) or len(raw) > max_bytes):
        raise ValueError("JSON exceeds limit or is not bytes")
    try:
        document = json.loads(raw.decode("utf8"), object_pairs_hook=_unique,
                              parse_constant=_nonfinite, parse_float=_float, parse_int=_int)
        literals = {}
        document = _extract_literals(document, "", literals)
        if len(events.encoded(literals)) > events.MAX_ARTIFACT_BYTES:
            raise ValueError("numeric literal map exceeds limit")
        for pointer in literals:
            _pointer_parts(pointer, max_bytes=events.MAX_ARTIFACT_BYTES, max_depth=512)
        events.encoded(document)
        return document, literals
    except (UnicodeError, json.JSONDecodeError, RecursionError, OverflowError) as exc:
        raise ValueError("invalid response JSON") from exc


def convert(raw, *, receipt, array_pointer, capture_context=None):
    """Retain every selected value and the surrounding parsed JSON context.

    Receipt and source assertions are caller supplied, not publisher or TLS proof.
    """
    if not isinstance(raw, bytes) or len(raw) > events.MAX_ARTIFACT_BYTES:
        raise ValueError("response body exceeds limit or is not bytes")
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
        raise ValueError("invalid HTTP receipt shape")
    source = receipt["sourceKey"]
    if not isinstance(source, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", source):
        raise ValueError("invalid asserted source key")
    for key in ("requestUrl", "finalUrl"):
        if events.http_url(receipt[key]) is None:
            raise ValueError("HTTP receipt URL required")
    start, finish = events.timestamp(receipt["startedAt"]), events.timestamp(receipt["finishedAt"])
    if datetime.fromisoformat(start.replace("Z", "+00:00")) > datetime.fromisoformat(finish.replace("Z", "+00:00")):
        raise ValueError("HTTP receipt clock reversed")
    valid_status = (type(receipt["status"]) is int and
                    (100 <= receipt["status"] <= 599 if array_pointer is None else receipt["status"] == 200))
    if (receipt["method"] not in ("GET", "POST") or not valid_status or type(receipt["bodyBytes"]) is not int
            or receipt["bodyBytes"] != len(raw) or receipt["bodySha256"] != events.sha256(raw)):
        raise ValueError("HTTP receipt does not bind the response body")
    parts = None if array_pointer is None else _pointer_parts(array_pointer)
    document, literals = decode_json(raw)
    if parts is None:
        records, context = [], copy.deepcopy(document)
    else:
        node = document
        for part in parts:
            node = node[_child(node, part)]
        if not isinstance(node, list) or len(node) > 10000:
            raise ValueError("selected response array is invalid or exceeds limit")
        records = copy.deepcopy(node)
        context = _replace(document, parts, copy.deepcopy(PLACEHOLDER))
    if capture_context is not None and len(events.encoded(capture_context)) > 65536:
        raise ValueError("capture context exceeds limit")
    return {"schema": SCHEMA_CONTEXT if parts is None else SCHEMA,
            "receipt": copy.deepcopy(receipt), "arrayPointer": array_pointer,
            "context": context, "records": records, "numericLiterals": literals,
            **({"captureContext": copy.deepcopy(capture_context)} if capture_context is not None else {}),
            "provenance": {"bodyHashKind": "caller_supplied_response_body_bytes_not_wire",
                           "receipt": "caller_asserted_not_tls_or_publisher_authentication",
                           "identity": "unverified", "visibility": "unverified",
                           "completeness": "unverified", "contextEncoding": "canonical_json_utf8",
                           "numericEncoding": "json_pointer_to_literal_null_placeholder_v1",
                           **({"recordSelection": "whole_response_no_listing_array_assertion"} if parts is None else {})}}


def reconstruct(observation):
    """Reconstruct ordinary JSON; exceptional numbers require reconstruct_json."""
    if isinstance(observation, dict) and observation.get("numericLiterals"):
        raise ValueError("exceptional numeric literals require reconstruct_json")
    return _reconstruct_values(observation)


def _reconstruct_values(observation):
    if not isinstance(observation, dict) or observation.get("schema") not in (SCHEMA, SCHEMA_CONTEXT):
        raise ValueError("invalid response observation")
    if observation["schema"] == SCHEMA_CONTEXT:
        if observation.get("arrayPointer") is not None or observation.get("records") != [] or "context" not in observation:
            raise ValueError("whole-response observation cannot claim a selected array")
        return copy.deepcopy(observation["context"])
    parts = _pointer_parts(observation.get("arrayPointer"))
    context = copy.deepcopy(observation.get("context"))
    node = context
    for part in parts:
        node = node[_child(node, part)]
    if node != PLACEHOLDER or not isinstance(observation.get("records"), list):
        raise ValueError("response observation placeholder mismatch")
    return _replace(context, parts, copy.deepcopy(observation["records"]))


def reconstruct_json(observation):
    """Emit valid JSON with exceptional numeric literals, not original bytes."""
    document = _reconstruct_values(observation)
    return encode_json(document, observation.get("numericLiterals"))


def encode_json(document, literals):
    """Reconstruct numeric values without changing source objects or genuine nulls."""
    if not isinstance(literals, dict):
        raise ValueError("missing numeric literal map")  # noqa: TRY004
    if len(events.encoded(literals)) > 16 * 1024 * 1024:
        raise ValueError("numeric literal map exceeds limit")
    for pointer, literal in literals.items():
        if not isinstance(literal, str) or not NUMBER.fullmatch(literal):
            raise ValueError("invalid numeric literal")
        node = document
        for part in _pointer_parts(pointer, max_bytes=events.MAX_ARTIFACT_BYTES, max_depth=512):
            node = node[_child(node, part)]
        if node is not None:
            raise ValueError("numeric literal placeholder mismatch")

    def emit(value, pointer):
        if pointer in literals:
            return literals[pointer]
        if isinstance(value, dict):
            return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + emit(v, _path(pointer, k))
                                  for k, v in value.items()) + "}"
        if isinstance(value, list):
            return "[" + ",".join(emit(v, _path(pointer, i)) for i, v in enumerate(value)) + "]"
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    return emit(document, "")


def validate(observation):
    """Check the whole reconstructable envelope; original byte hash remains a claim."""
    if not isinstance(observation, dict) or not isinstance(observation.get("receipt"), dict):
        raise ValueError("invalid response observation")  # noqa: TRY004
    receipt = observation["receipt"]
    if (set(receipt) != RECEIPT_KEYS or not isinstance(receipt.get("bodySha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", receipt["bodySha256"])
            or type(receipt.get("bodyBytes")) is not int
            or not 0 <= receipt["bodyBytes"] <= events.MAX_ARTIFACT_BYTES):
        raise ValueError("invalid original response binding")
    raw = reconstruct_json(observation).encode("utf8")
    expected = convert(raw, receipt={**receipt, "bodySha256": events.sha256(raw), "bodyBytes": len(raw)},
                       array_pointer=observation.get("arrayPointer"),
                       capture_context=observation.get("captureContext"))
    expected["receipt"] = copy.deepcopy(receipt)
    if expected != observation:
        raise ValueError("response observation disagrees with reconstructed values")
