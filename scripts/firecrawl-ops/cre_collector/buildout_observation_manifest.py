"""Offline integrity seals for private Buildout artifacts; never authorizes writes.

This has no network, database, scheduler, or ingestion integration. Source and
scope are caller assertions, not proof of collection rights, identity or coverage.
"""
import argparse
import hashlib
import json
import re
import threading
from pathlib import Path

import buildout_observation_artifact as artifact_builder
from cre_ingest import BUILDOUT_SOURCE_KEYS

SCHEMA = "buildout-private-observation-manifest-v1"
ARTIFACT_SCHEMA = "buildout-private-observation-artifact-v1"
MAX_MANIFEST_BYTES = 64_000
SCOPE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SCOPE_KEYS = ("sourceKey", "generationId", "captureId", "page", "transaction")
# A capture ID is immutable metadata, never a way to open a second page slot.
SLOT_KEYS = ("sourceKey", "generationId", "page", "transaction")
PAGE_KEYS = ("sourceKey", "generationId", "page")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def read_bounded(path, limit):
    with path.open("rb") as stream:
        return stream.read(limit + 1)


def validate_scope(scope):
    if not isinstance(scope, dict) or set(scope) != set(SCOPE_KEYS):
        raise ValueError("invalid asserted scope")
    source = scope["sourceKey"]
    if not isinstance(source, str) or source not in BUILDOUT_SOURCE_KEYS:
        raise ValueError("unregistered asserted source")
    for name in ("generationId", "captureId"):
        value = scope[name]
        if (not isinstance(value, str) or not SCOPE_ID.fullmatch(value)
            or artifact_builder.credential_label(value)):
            raise ValueError("invalid asserted scope identifier")
    if type(scope["page"]) is not int or not 0 <= scope["page"] <= 1_000_000:
        raise ValueError("invalid asserted page")
    if scope["transaction"] not in ("sale", "lease", "unknown"):
        raise ValueError("invalid asserted transaction")
    return dict(scope)


def parse_object(raw, limit):
    if len(raw) > limit:
        raise ValueError("input exceeds byte limit")
    value = json.loads(raw, object_pairs_hook=artifact_builder.unique_object,
                       parse_int=lambda value: artifact_builder.bounded_number(value, floating=False),
                       parse_float=lambda value: artifact_builder.bounded_number(value, floating=True),
                       parse_constant=artifact_builder.invalid_number)
    if not isinstance(value, dict):
        raise TypeError("expected JSON object")
    return value


def builder_fingerprint():
    return {"schema": ARTIFACT_SCHEMA, "moduleSha256": digest(Path(artifact_builder.__file__).read_bytes()),
            "sourceRegistrySha256": digest(canonical(sorted(BUILDOUT_SOURCE_KEYS)))}


def seal(input_bytes, artifact_bytes, *, scope, input_kind):
    scope = validate_scope(scope)
    if input_kind not in ("buildout-page", "retained-observations"):
        raise ValueError("invalid input kind")
    if len(input_bytes) > artifact_builder.MAX_INPUT_BYTES or len(artifact_bytes) > artifact_builder.MAX_OUTPUT_BYTES:
        raise ValueError("input exceeds byte limit")
    # Independently reconstruct the artifact from exact input bytes and compare
    # bytes, rather than trusting the digest or counters embedded in the file.
    expected, summary = artifact_builder.build_artifact(input_bytes, source_key=scope["sourceKey"], input_kind=input_kind)
    if expected != artifact_bytes:
        raise ValueError("artifact does not match rebuilt input")
    result = {
        "schema": SCHEMA,
        "scope": scope,
        "scopeOrigin": "operator_asserted_unverified",
        "inputKind": input_kind,
        "input": {"sha256": digest(input_bytes), "bytes": len(input_bytes)},
        "artifact": {"sha256": digest(artifact_bytes), "bytes": len(artifact_bytes)},
        "builder": builder_fingerprint(),
        "summary": summary,
        "sourceAuthorization": "not_evaluated",
        "sourceCompleteness": "unverified",
        "confidentiality": "unreviewed_private_data",
        "signature": "none",
    }
    result["manifestSha256"] = digest(canonical(result))
    return result


def verify(manifest_bytes, input_bytes, artifact_bytes, *, expected_scope):
    manifest = parse_object(manifest_bytes, MAX_MANIFEST_BYTES)
    scope = validate_scope(expected_scope)
    if manifest.get("scope") != scope:
        raise ValueError("asserted scope does not match expected scope")
    expected = seal(input_bytes, artifact_bytes, scope=scope, input_kind=manifest.get("inputKind"))
    if manifest != expected:
        raise ValueError("manifest or binding mismatch")
    return expected


class FixtureReplayModel:
    """Process-local test model; it cannot persist or authorize production data."""
    def __init__(self):
        self._slots = {}
        self._page_bindings = {}
        self._lock = threading.Lock()

    def apply(self, manifest_bytes, input_bytes, artifact_bytes, *, expected_scope):
        # This model intentionally accepts only an explicitly labelled local
        # test page; the label is an assertion, not a security boundary.
        source = parse_object(input_bytes, artifact_builder.MAX_INPUT_BYTES)
        if source.get("syntheticFixture") is not True:
            raise ValueError("fixture model requires synthetic input")
        manifest = verify(manifest_bytes, input_bytes, artifact_bytes, expected_scope=expected_scope)
        data = parse_object(artifact_bytes, artifact_builder.MAX_OUTPUT_BYTES)
        if data.get("schema") != ARTIFACT_SCHEMA or not isinstance(data.get("records"), list):
            raise ValueError("invalid artifact records")
        evidence = []
        pointers = set()
        for record in data["records"]:
            if not isinstance(record, dict) or not isinstance(record.get("sourcePointer"), str):
                raise TypeError("invalid record pointer")
            pointer = record["sourcePointer"]
            if pointer in pointers:
                raise ValueError("duplicate record pointer")
            pointers.add(pointer)
            evidence.append((pointer, artifact_builder.render(record)))
        slot = tuple(manifest["scope"][key] for key in SLOT_KEYS)
        page = tuple(manifest["scope"][key] for key in PAGE_KEYS)
        page_binding = (manifest["scope"]["captureId"], manifest["inputKind"],
                        canonical(manifest["input"]), canonical(manifest["artifact"]),
                        canonical(manifest["builder"]), canonical(manifest["summary"]))
        binding = (manifest["manifestSha256"], tuple(evidence))
        with self._lock:
            previous_page = self._page_bindings.get(page)
            if previous_page is not None and previous_page != page_binding:
                raise ValueError("divergent fixture page binding")
            previous = self._slots.get(slot)
            if previous is not None:
                if previous != binding:
                    raise ValueError("divergent fixture replay")
                return "unchanged"
            self._page_bindings[page] = page_binding
            self._slots[slot] = binding
            return "inserted"

    def snapshot(self):
        with self._lock:
            return dict(self._slots)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seal", "verify"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-key", required=True)
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--capture-id", required=True)
    parser.add_argument("--page", type=int, required=True)
    parser.add_argument("--transaction", choices=("sale", "lease", "unknown"), required=True)
    parser.add_argument("--input-kind", choices=("buildout-page", "retained-observations"), required=True)
    args = parser.parse_args()
    scope = {"sourceKey": args.source_key, "generationId": args.generation_id,
             "captureId": args.capture_id, "page": args.page, "transaction": args.transaction}
    original = read_bounded(args.input, artifact_builder.MAX_INPUT_BYTES)
    artifact = read_bounded(args.artifact, artifact_builder.MAX_OUTPUT_BYTES)
    if args.action == "seal":
        manifest = seal(original, artifact, scope=scope, input_kind=args.input_kind)
        artifact_builder.write_private(args.manifest, canonical(manifest) + b"\n")
    else:
        manifest = verify(read_bounded(args.manifest, MAX_MANIFEST_BYTES), original, artifact, expected_scope=scope)
        if manifest["inputKind"] != args.input_kind:
            raise ValueError("input kind mismatch")
    print(json.dumps({"localIntegrity": "verified", "sourceAuthorization": "not_evaluated",
                      "sourceCompleteness": "unverified"}, sort_keys=True))


if __name__ == "__main__":
    main()
