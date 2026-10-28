"""Build a private, no-network evidence artifact from a saved Buildout page.

This tool is independent of collection, database writes, and scheduling. It does
not prove a source is complete, current, public, or safe to share. The original
input remains the recovery source for quarantined and byte-exact evidence.
"""
import argparse
import base64
import binascii
import hashlib
import json
import os
import re
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from buildout_observation_io import read_consistent
from cre_ingest import BUILDOUT_SOURCE_KEYS

MAX_INPUT_BYTES = 8_000_000
MAX_AUDIT_BYTES = 40 * 1024 * 1024
MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 20_000_000
MAX_RECORDS = 1000
MAX_ATTRIBUTES = 1000
MAX_VALUE_BYTES = 250_000
MAX_DEPTH = 32
SECRET_KEY = re.compile(r"(?:^|[^a-z0-9])(?:authorizations?|passwords?|passwds?|secrets?|credentials?|cookies?|api[ _-]?keys?|access[ _-]?tokens?|private[ _-]?keys?|session[ _-]?(?:tokens?|ids?|keys?))(?:$|[^a-z0-9])", re.IGNORECASE)
SECRET_ONLY = re.compile(r"(?:tokens?|sessions?)(?:[ _-]?(?:value|id|key))?", re.IGNORECASE)
CAMEL_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])|(?<=[a-z0-9])(?=[A-Z])")
SECRET_HEADER = re.compile(r"\b(?:authorization\s*:\s*(?:basic|bearer)\s+\S+|(?:set-)?cookie\s*:)", re.IGNORECASE)
SECRET_QUERY = re.compile(r"^(?:token|access[_-]?token|api[_-]?key|signature|sig|password|secret|session|access[_-]?key(?:[_-]?id)?|awsaccesskeyid|googleaccessid|x-(?:amz|goog)-(?:signature|credential|security[_-]?token))$", re.IGNORECASE)
URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class DuplicateJsonKey(ValueError):
    """Input cannot be represented without losing a duplicate key."""


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey("duplicate JSON object key")
        result[key] = value
    return result


def invalid_number(value):
    raise ValueError(f"non-finite JSON number: {value}")


def render(value, depth=0):
    """Write JSON while retaining decimal precision; input bytes remain authority."""
    if depth > MAX_DEPTH:
        raise ValueError("JSON nesting exceeds limit")
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite JSON number")
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(render(item, depth + 1) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(key, ensure_ascii=True) + ":" + render(item, depth + 1) for key, item in value.items()) + "}"
    raise TypeError("unsupported JSON value")


def credential_label(value):
    """Match credential words, including plural and camel-case field names."""
    normalized = CAMEL_BOUNDARY.sub(" ", value).strip()
    return bool(SECRET_KEY.search(normalized) or SECRET_ONLY.fullmatch(normalized))


def sensitive_string(value):
    if SECRET_HEADER.search(value):
        return True
    for match in URL.finditer(value):
        try:
            url = urlsplit(match.group(0))
            if url.username or url.password:
                return True
            query = url.query + "&" + url.fragment.replace("?", "&")
            if re.search(r"%(?![0-9a-fA-F]{2})", query):
                return True
            for key, _ in parse_qsl(query, keep_blank_values=True, encoding="utf-8", errors="strict"):
                for _ in range(5):
                    if SECRET_QUERY.fullmatch(key):
                        return True
                    decoded = unquote(key, encoding="utf-8", errors="strict")
                    if decoded == key:
                        break
                    key = decoded
                else:
                    return True
        except ValueError:
            return True
    return False


def pointer_component(value):
    return value.replace("~", "~0").replace("/", "~1")


def bounded_number(value, *, floating):
    if len(value) > 1000:
        raise ValueError("numeric token exceeds limit")
    return Decimal(value) if floating else int(value)


def input_limit(input_kind):
    if input_kind == "buildout-cache-audit":
        return MAX_AUDIT_BYTES
    if input_kind in ("buildout-page", "retained-observations"):
        return MAX_INPUT_BYTES
    raise ValueError("unsupported input kind")


def parse_json(raw):
    return json.loads(raw, object_pairs_hook=unique_object,
                      parse_int=lambda value: bounded_number(value, floating=False),
                      parse_float=lambda value: bounded_number(value, floating=True),
                      parse_constant=invalid_number)


AUDIT_REASON_CODES = frozenset({
    "plugin_mismatch", "invalid_page", "query_mismatch", "generation_missing_or_mismatch",
    "observation_timestamp_mismatch", "observation_outside_window", "source_observation_not_live",
    "transport_invalid", "endpoint_mismatch", "parsed_data_hash_mismatch", "unsafe_numeric_values",
    "page_shape_invalid",
})


def decode_audit(raw, source_key):
    audit = parse_json(raw)
    if (not isinstance(audit, dict) or audit.get("schema") != "buildout-page-audit-v1"
        or audit.get("sourceKey") != source_key or audit.get("scope") != "selected_page_only"
        or audit.get("authority") != "none" or audit.get("publisher_authenticated") is not False
        or audit.get("private_unreviewed") is not True or audit.get("coverage") != "unknown"
        or audit.get("input_byte_authority") != "originalCacheBytesBase64"):
        raise ValueError("invalid cache audit envelope")
    status, reasons = audit.get("status"), audit.get("provenance_reasons")
    if (status not in ("provenance_incomplete", "parsed_json_consistent_fidelity_unverified")
        or not isinstance(reasons, list) or len(reasons) > 64
        or any(not isinstance(reason, str) or reason not in AUDIT_REASON_CODES for reason in reasons)
        or (status == "provenance_incomplete") != bool(reasons)):
        raise ValueError("invalid audit provenance status")
    encoded, claimed_hash = audit.get("originalCacheBytesBase64"), audit.get("cacheFileSha256")
    if (not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_CACHE_BYTES + 2) // 3)
        or not isinstance(claimed_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", claimed_hash)):
        raise ValueError("invalid embedded cache bytes")
    try:
        cache_raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid embedded cache encoding") from exc
    if len(cache_raw) > MAX_CACHE_BYTES or hashlib.sha256(cache_raw).hexdigest() != claimed_hash:
        raise ValueError("embedded cache digest or size mismatch")
    try:
        cache = parse_json(cache_raw)
    except DuplicateJsonKey as exc:
        raise DuplicateJsonKey(
            "duplicate JSON object key: projection unsupported; retain the private original audit and cache; source availability is unverified"
        ) from exc
    data = cache.get("data") if isinstance(cache, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("inventory"), list):
        raise TypeError("embedded cache has no inventory array")
    if type(audit.get("observedRows")) is not int or audit["observedRows"] != len(data["inventory"]):
        raise ValueError("audit row count does not match embedded cache")
    fidelity = audit.get("data_fidelity")
    unsafe = fidelity.get("unsafeNumericValues") if isinstance(fidelity, dict) else None
    if type(unsafe) is not int or unsafe < 0:
        raise ValueError("invalid audit numeric context")
    return data, {"status": status, "reportedProvenanceIncomplete": status == "provenance_incomplete", "reasons": reasons,
                  "auditClaimsOrigin": "caller_reported", "auditClaimsVerification": "not_evaluated", "cacheBytesSha256": claimed_hash,
                  "cacheBytes": len(cache_raw), "auditReportedUnsafeNumericValues": unsafe,
                  "sourceBytesAuthority": "embedded_cache_bytes_not_provider_http",
                  "publisherAuthenticated": False, "sourceCompleteness": "unverified",
                  "cacheWrapperFields": "retained_in_original_audit_only"}


def build_artifact(raw, *, source_key, input_kind):
    if not isinstance(source_key, str) or source_key not in BUILDOUT_SOURCE_KEYS:
        raise ValueError("source key is not a registered Buildout source")
    if len(raw) > input_limit(input_kind):
        raise ValueError("input exceeds byte limit")
    audit_context = None
    if input_kind == "buildout-cache-audit":
        source, audit_context = decode_audit(raw, source_key)
    else:
        source = parse_json(raw)
    if input_kind in ("buildout-page", "buildout-cache-audit"):
        if not isinstance(source, dict) or not isinstance(source.get("inventory"), list):
            raise ValueError("Buildout page must contain an inventory array")
        rows = source["inventory"]
        prefix = "/data/inventory" if input_kind == "buildout-cache-audit" else "/inventory"
        page_fields = source
    elif input_kind == "retained-observations":
        if not isinstance(source, list):
            raise ValueError("retained observations must be an array")
        rows, prefix, page_fields = source, "", {}
    else:
        raise ValueError("unsupported input kind")
    if len(rows) > MAX_RECORDS:
        raise ValueError("record count exceeds limit")
    quarantine, output_rows = [], []

    def sanitize(value, pointer, depth=0, attribute_context=False):
        if depth > MAX_DEPTH:
            raise ValueError("JSON nesting exceeds limit")
        if isinstance(value, str) and sensitive_string(value):
            quarantine.append({"pointer": pointer, "reason": "credential-shaped or unverifiable content"})
            return {"$buildoutQuarantined": True}
        if isinstance(value, list):
            if (attribute_context and value and isinstance(value[0], str)
                and (credential_label(value[0]) or sensitive_string(value[0]))):
                quarantine.append({"pointer": pointer, "reason": "credential-shaped attribute label"})
                return {"$buildoutQuarantined": True}
            return [sanitize(item, f"{pointer}/{index}", depth + 1, attribute_context)
                    for index, item in enumerate(value)]
        if isinstance(value, dict):
            result = {}
            for ordinal, (key, item) in enumerate(value.items()):
                if credential_label(key) or sensitive_string(key):
                    quarantine.append({"parent": pointer, "fieldOrdinal": ordinal,
                                       "reason": "credential-shaped field name"})
                else:
                    child_pointer = pointer + "/" + pointer_component(key)
                    recognized_collection = key == "index_attributes" or (
                        key == "attributes" and pointer.endswith("/buildoutInventoryObservation")
                    )
                    result[key] = (sanitize_attribute_collection(item, child_pointer, depth + 1)
                                   if recognized_collection else
                                   sanitize(item, child_pointer, depth + 1, attribute_context))
            return result
        return value

    def sanitize_attribute_collection(value, pointer, depth=0):
        if value is None:
            return None
        if not isinstance(value, list):
            quarantine.append({"pointer": pointer, "reason": "attributes are not an array"})
            return {"$buildoutQuarantined": True}
        if len(value) > MAX_ATTRIBUTES:
            quarantine.append({"pointer": pointer, "reason": "attributes exceed count limit"})
            return {"$buildoutQuarantined": True}
        result = []
        for ordinal, item in enumerate(value):
            child = f"{pointer}/{ordinal}"
            if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str):
                quarantine.append({"pointer": child, "reason": "attribute is not a label/value pair"})
                result.append({"$buildoutQuarantined": True})
            else:
                result.append(sanitize(item, child, depth + 1, attribute_context=True))
        return result

    def capture_fields(fields, parent, target, excluded=()):
        for ordinal, (key, value) in enumerate(fields.items()):
            if key in excluded:
                continue
            if credential_label(key) or sensitive_string(key):
                quarantine.append({"parent": parent, "fieldOrdinal": ordinal,
                                   "reason": "credential-shaped field name"})
            elif len(render(value).encode("ascii")) > MAX_VALUE_BYTES:
                quarantine.append({"parent": parent, "fieldOrdinal": ordinal, "reason": "value exceeds byte limit"})
            else:
                child_pointer = parent + "/" + pointer_component(key)
                target[key] = (sanitize_attribute_collection(value, child_pointer)
                               if key == "index_attributes" else sanitize(value, child_pointer))

    safe_page = {}
    capture_fields(page_fields, "/data" if input_kind == "buildout-cache-audit" else "", safe_page, excluded=("inventory",))
    upstream_omissions = 0
    upstream_omissions_unknown = 0
    for index, row in enumerate(rows):
        parent = f"{prefix}/{index}"
        if not isinstance(row, dict):
            quarantine.append({"pointer": parent, "reason": "record is not an object"})
            continue
        attributes = row.get("index_attributes")
        has_attributes = "index_attributes" in row
        saved_fields = {}
        capture_fields(row, parent, saved_fields, excluded=("index_attributes",))
        if input_kind == "retained-observations":
            prior = row.get("buildoutInventoryObservation")
            omitted = prior.get("omittedCount") if isinstance(prior, dict) else None
            if type(omitted) is int and omitted >= 0:
                upstream_omissions += omitted
            else:
                upstream_omissions_unknown += 1
        entry = {"sourcePointer": parent, "identityResolution": "unresolved", "sourceFields": saved_fields,
                 "indexAttributesState": "missing" if not has_attributes else "null" if attributes is None else "present"}
        if has_attributes and attributes is not None:
            if not isinstance(attributes, list) or len(attributes) > MAX_ATTRIBUTES:
                entry["indexAttributesState"] = "quarantined"
                quarantine.append({"pointer": parent + "/index_attributes", "reason": "attributes are not a bounded array"})
            else:
                saved_attributes = []
                for attribute_index, pair in enumerate(attributes):
                    path = f"{parent}/index_attributes/{attribute_index}"
                    if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str):
                        quarantine.append({"pointer": path, "reason": "attribute is not a label/value pair"})
                    elif credential_label(pair[0]) or sensitive_string(pair[0]):
                        quarantine.append({"pointer": path, "reason": "credential-shaped attribute label"})
                    elif len(render(pair).encode("ascii")) > MAX_VALUE_BYTES:
                        quarantine.append({"pointer": path, "reason": "attribute exceeds byte limit"})
                    else:
                        saved_attributes.append({"sourceOrdinal": attribute_index,
                                                 "literal": [pair[0], sanitize(pair[1], path + "/1", attribute_context=True)]})
                entry["indexAttributes"] = saved_attributes
        output_rows.append(entry)
    document = {
        "schema": "buildout-private-observation-artifact-v1",
        "provenance": {"sourceKey": source_key, "sourceKeyOrigin": "operator_supplied", "inputKind": input_kind,
                       "inputSha256": hashlib.sha256(raw).hexdigest(), "inputBytes": len(raw),
                       "sourceCompleteness": "unverified", "sourceAvailability": "unverified",
                       "confidentiality": "unreviewed_private_data", "inputIsByteExactAuthority": True},
        **({"auditContext": audit_context, "reportedProvenanceIncomplete": audit_context["reportedProvenanceIncomplete"]} if audit_context is not None else {}),
        "pageFields": safe_page, "records": output_rows, "quarantine": quarantine,
        "summary": {"inputRecords": len(rows), "retainedRecords": len(output_rows), "quarantinedItems": len(quarantine), "upstreamReportedOmissions": upstream_omissions,
                    "upstreamOmissionsUnknownRecords": upstream_omissions_unknown,
                    **({"reportedProvenanceIncomplete": audit_context["reportedProvenanceIncomplete"]} if audit_context is not None else {}),
                    "status": "partial" if quarantine or len(output_rows) != len(rows) or upstream_omissions or upstream_omissions_unknown or not rows else "observed_only"},
    }
    encoded = (render(document) + "\n").encode("ascii")
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise ValueError("output exceeds byte limit")
    return encoded, document["summary"]


def read_bounded(path, limit=MAX_INPUT_BYTES):
    return read_consistent(path, limit)


def write_private(path, payload):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--input-kind", choices=("buildout-page", "retained-observations", "buildout-cache-audit"), required=True)
    parser.add_argument("--source-key", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    payload, summary = build_artifact(read_bounded(args.input, input_limit(args.input_kind)), source_key=args.source_key, input_kind=args.input_kind)
    write_private(args.out, payload)
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
