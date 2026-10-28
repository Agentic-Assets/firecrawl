"""Project private Buildout observations offline, without admission or writes.

Input and output remain unreviewed private data. This tool does not verify
source identity, row transaction, source visibility, freshness, or listing
completeness. The manifest transaction is an operator assertion, not proof of
any row's tenure. Results are never canonical listings or ingest inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import available_space_count
import buildout_observation_manifest as manifest_tools
from available_space_count import parse_buildout_available_space_count
from buildout_observation_artifact import (
    BUILDOUT_SOURCE_KEYS,
    MAX_OUTPUT_BYTES,
    sensitive_string,
    unique_object,
    write_private,
)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_BEARER = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)
_MAX_RECORDS = 1000
_MAX_ATTRIBUTES = 1000


def _literal(value: object) -> str | None:
    """Limit retained evidence; this does not certify it as secret-free."""
    if not isinstance(value, str) or len(value) > 128:
        return None
    if _CONTROL.search(value) or _BEARER.search(value) or sensitive_string(value):
        return None
    return value


def _field(pairs: list[list[object]], label: str, *, spaces: bool) -> dict:
    values = [pair[1] for pair in pairs if pair[0] == label]
    if not values:
        return {"state": "missing"}
    retained = [_literal(value) for value in values]
    if any(value is None for value in retained):
        return {"state": "omitted", "reason": "unsafe_or_nonstring_literal"}
    if len(values) != 1:
        return {"state": "ambiguous", "reason": "duplicate_label", "literals": retained}
    literal = retained[0]
    if spaces:
        count = parse_buildout_available_space_count(literal)
        if count is None:
            return {"state": "invalid", "reason": "unsupported_spaces_literal", "literal": literal}
        return {"state": "candidate_unverified", "literal": literal, "value": count}
    # Cap-rate interpretation belongs exclusively to the TS parser, not a
    # second percent grammar in this offline projection.
    return {"state": "literal_only", "literal": literal}


def _attributes(record: dict, kind: str) -> tuple[list[list[object]] | None, str | None]:
    if kind in {"buildout-page", "buildout-cache-audit"}:
        state = record.get("indexAttributesState")
        if state != "present":
            return None, "attributes_" + (state if state in {"missing", "null", "quarantined"} else "invalid")
        attrs = record.get("indexAttributes")
        if not isinstance(attrs, list) or len(attrs) > _MAX_ATTRIBUTES:
            return None, "malformed_attributes"
        pairs = []
        for expected_ordinal, item in enumerate(attrs):
            if not isinstance(item, dict) or set(item) != {"sourceOrdinal", "literal"}:
                return None, "malformed_attributes"
            ordinal, pair = item["sourceOrdinal"], item["literal"]
            if type(ordinal) is not int or ordinal != expected_ordinal:
                return None, "malformed_attributes"
            pairs.append(pair)
    else:
        fields = record.get("sourceFields")
        prior = fields.get("buildoutInventoryObservation") if isinstance(fields, dict) else None
        if not isinstance(prior, dict) or prior.get("schema") != "buildout-inventory-attributes-v1":
            return None, "missing_retained_observation"
        if type(prior.get("omittedCount")) is not int or prior["omittedCount"] != 0:
            return None, "upstream_omissions_or_unknown"
        pairs = prior.get("attributes")
        if not isinstance(pairs, list) or len(pairs) > _MAX_ATTRIBUTES:
            return None, "malformed_attributes"
    if any(not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str)
           or not pair[0].strip() for pair in pairs):
        return None, "malformed_attributes"
    return pairs, None


def project(
    raw: bytes, *, original: bytes, manifest: bytes, expected_scope: dict,
) -> tuple[bytes, dict]:
    """Require byte-consistency verification; never assert publisher identity."""
    verified = manifest_tools.verify(manifest, original, raw, expected_scope=expected_scope)
    if len(raw) > MAX_OUTPUT_BYTES:
        raise ValueError("artifact exceeds byte limit")
    data = json.loads(raw, object_pairs_hook=unique_object,
                      parse_int=lambda value: manifest_tools.artifact_builder.bounded_number(value, floating=False),
                      parse_float=lambda value: manifest_tools.artifact_builder.bounded_number(value, floating=True),
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid number")))
    if not isinstance(data, dict) or data.get("schema") != "buildout-private-observation-artifact-v1":
        raise ValueError("unsupported artifact schema")
    provenance, records, quarantine, summary = (data.get("provenance"), data.get("records"),
                                                 data.get("quarantine"), data.get("summary"))
    if (not isinstance(provenance, dict) or provenance.get("sourceKey") not in BUILDOUT_SOURCE_KEYS
        or provenance.get("sourceKeyOrigin") != "operator_supplied"
        or provenance.get("inputKind") not in {"buildout-page", "retained-observations", "buildout-cache-audit"}
        or not isinstance(records, list) or len(records) > _MAX_RECORDS
        or not isinstance(quarantine, list) or not isinstance(summary, dict)):
        raise ValueError("invalid artifact structure")
    kind = provenance["inputKind"]
    # Any upstream omission could hide a conflicting attribute. Treat the
    # whole artifact as incomplete rather than infer which row was affected.
    incomplete = (bool(quarantine) or summary.get("status") != "observed_only"
                  or type(summary.get("inputRecords")) is not int
                  or summary.get("inputRecords") != len(records)
                  or type(summary.get("retainedRecords")) is not int
                  or summary.get("retainedRecords") != len(records)
                  or type(summary.get("quarantinedItems")) is not int
                  or summary.get("quarantinedItems") != len(quarantine)) or any(
        type(summary.get(key)) is not int or summary[key] != 0
        for key in ("upstreamReportedOmissions", "upstreamOmissionsUnknownRecords")
    )
    output = []
    previous_row = -1
    for index, record in enumerate(records):
        if not isinstance(record, dict) or record.get("identityResolution") != "unresolved":
            raise ValueError("invalid record identity")
        pointer = record.get("sourcePointer")
        pointer_pattern = (r"/inventory/(?:0|[1-9][0-9]*)" if kind == "buildout-page" else
                           r"/data/inventory/(?:0|[1-9][0-9]*)" if kind == "buildout-cache-audit" else
                           r"/(?:0|[1-9][0-9]*)")
        if not isinstance(pointer, str) or not re.fullmatch(pointer_pattern, pointer, re.ASCII):
            raise ValueError("invalid source pointer")
        ordinal = int(pointer.rsplit("/", 1)[-1])
        if ordinal <= previous_row or (not incomplete and ordinal != index):
            raise ValueError("invalid source pointer ordering")
        previous_row = ordinal
        pairs, reason = _attributes(record, kind)
        if incomplete or reason:
            field = {"state": "omitted", "reason": "upstream_quarantine_or_omissions" if incomplete else reason}
            space, cap = field, field.copy()
        else:
            space = _field(pairs, "# of Spaces", spaces=True)
            cap = _field(pairs, "Cap Rate", spaces=False)
        output.append({"sourcePointer": pointer, "identityResolution": "unresolved",
                       "rowTransaction": "unverified",
                       "availableSpaceCount": space, "capRate": cap})
    audit = data.get("auditContext") if kind == "buildout-cache-audit" else None
    if kind == "buildout-cache-audit" and (
        not isinstance(audit, dict) or type(audit.get("reportedProvenanceIncomplete")) is not bool
        or data.get("reportedProvenanceIncomplete") is not audit["reportedProvenanceIncomplete"]
    ):
        raise ValueError("invalid cache audit context")
    result = {"schema": "buildout-offline-projection-v1", "artifactSha256": hashlib.sha256(raw).hexdigest(),
              "integrity": "verified_byte_consistency_only", "scope": verified["scope"],
              "projectionCode": {"projectorSha256": manifest_tools.fingerprint_file(Path(__file__)),
                                 "availableSpaceParserSha256": manifest_tools.fingerprint_file(Path(available_space_count.__file__)),
                                 "fingerprintOrigin": "local_files_at_projection_time_not_authentication"},
              "sourceKey": provenance["sourceKey"], "sourceKeyOrigin": "operator_supplied",
              "sourceCompleteness": "unverified", "sourceAvailability": "unverified",
              "visibility": "unverified", "freshness": "unverified", "promotion": "not_authorized",
              "auditProvenance": audit if audit is not None else "not_evaluated",
              "reportedProvenanceIncomplete": audit["reportedProvenanceIncomplete"] if audit is not None else None,
              "records": output}
    encoded = (json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("ascii")
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise ValueError("projection exceeds byte limit")
    counts = {"records": len(output), "upstreamIncomplete": incomplete}
    if audit is not None:
        counts["reportedProvenanceIncomplete"] = audit["reportedProvenanceIncomplete"]
    return encoded, counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--original", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--source-key", required=True)
    parser.add_argument("--generation-id")
    parser.add_argument("--capture-id", required=True)
    parser.add_argument("--page", type=int)
    parser.add_argument("--transaction", required=True, choices=("sale", "lease", "unknown"))
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    raw = manifest_tools.read_bounded(args.input, MAX_OUTPUT_BYTES)
    manifest = manifest_tools.read_bounded(args.manifest, manifest_tools.MAX_MANIFEST_BYTES)
    hint = manifest_tools.parse_object(manifest, manifest_tools.MAX_MANIFEST_BYTES)
    original = manifest_tools.read_bounded(args.original, manifest_tools.artifact_builder.input_limit(hint.get("inputKind")))
    scope = {"sourceKey": args.source_key, "generationId": args.generation_id,
             "captureId": args.capture_id, "page": args.page, "transaction": args.transaction}
    encoded, counts = project(raw, original=original, manifest=manifest, expected_scope=scope)
    write_private(args.out, encoded)
    print(json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    main()
