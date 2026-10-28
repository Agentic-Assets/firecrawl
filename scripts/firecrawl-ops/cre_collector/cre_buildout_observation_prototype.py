"""Offline-only Buildout observation SQL planner for synthetic fixture artifacts.

No database client, connection options, production schema, or live writer exists.
The paired migration is intentionally executable only in an isolated fixture DB.
Its content screening is defense in depth, not proof that a real artifact is free
of secrets. Do not use this prototype for real/provider evidence or production.
"""
import argparse
import hashlib
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from buildout_observation_io import read_consistent
from cre_ingest import BUILDOUT_SOURCE_KEYS

MAX_ARTIFACT_BYTES = 8_000_000
MAX_LISTINGS = 1000
MAX_OBSERVATION_BYTES = 250_000
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
SENSITIVE = re.compile(r"(?:authorization|password|secret|credential|cookie|bearer|api[ _-]?key|access[ _-]?token|https?://|%)", re.IGNORECASE)
VALUE_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
LABEL_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def lit(value):
    return "'" + value.replace("'", "''") + "'"


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def validate_observation(value):
    if not isinstance(value, dict) or set(value) - {
        "schema", "attributes", "omittedCount", "omissions", "provenance", "observedAt", "generationId"
    }:
        raise ValueError("invalid observation object")
    pairs, omissions, status = value.get("attributes"), value.get("omissions"), value.get("provenance")
    if (value.get("schema") != "buildout-inventory-attributes-v1"
        or not isinstance(pairs, list) or len(pairs) > 100
        or not isinstance(omissions, dict) or set(omissions) != {"malformed", "sensitive", "limit"}
        or any(type(n) is not int or n < 0 for n in omissions.values())
        or type(value.get("omittedCount")) is not int or value["omittedCount"] != sum(omissions.values())
        or not isinstance(status, dict) or set(status) != {"observedAt", "generationId"}
        or any(v not in ("valid", "missing", "invalid") for v in status.values())):
        raise ValueError("invalid observation structure")
    if len(canonical(value).encode("utf-8")) > MAX_OBSERVATION_BYTES:
        raise ValueError("observation exceeds byte limit")
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("invalid attribute pair")
        label, item = pair
        if (not isinstance(label, str) or not label.strip() or len(label) > 256
            or LABEL_CONTROL.search(label) or SENSITIVE.search(label)):
            raise ValueError("unsafe attribute label")
        if isinstance(item, str):
            if len(item) > 2048 or VALUE_CONTROL.search(item) or SENSITIVE.search(item):
                raise ValueError("unsafe attribute value")
        elif item is not None and type(item) not in (bool, int, float):
            raise ValueError("invalid attribute value")
        elif type(item) is float and not math.isfinite(item):
            raise ValueError("invalid number")
    # Metadata is never trusted as an attestation of freshness.
    for field in ("observedAt", "generationId"):
        present = field in value
        if present != (status[field] == "valid"):
            raise ValueError("observation provenance mismatch")
        if present and (not isinstance(value[field], str) or len(value[field]) > 256
                        or LABEL_CONTROL.search(value[field]) or SENSITIVE.search(value[field])):
            raise ValueError("invalid observation provenance")
    if "generationId" in value and not SAFE_ID.fullmatch(value["generationId"]):
        raise ValueError("invalid observation generation")
    if "observedAt" in value:
        timestamp = value["observedAt"]
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})", timestamp):
            raise ValueError("invalid observation time")
        if timestamp[-1] != "Z":
            hour, minute = map(int, timestamp[-5:].split(":"))
            if hour > 14 or minute > 59 or (hour == 14 and minute):
                raise ValueError("invalid observation offset")
        try:
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid observation time") from exc
    return value


def fixture_source_url(value):
    """Only fixture-owned URLs; never resolve a network address."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parts = urlsplit(value)
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        if (parts.scheme != "https" or parts.hostname != "example.test"
            or parts.username is not None or parts.password is not None
            or parts.port is not None or parts.fragment or "%" in value
            or any(key != "propertyId" or not SAFE_ID.fullmatch(item) for key, item in pairs)
            or len(pairs) > 1 or LABEL_CONTROL.search(value)):
            return None
        return value
    except ValueError:
        return None


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def read_fixture(path):
    # Limit actual I/O, not just the byte string after loading it.
    return read_consistent(path, MAX_ARTIFACT_BYTES)


def build_plan(raw):
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError("fixture artifact exceeds byte limit")
    data = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(data, dict) or data.get("syntheticFixture") is not True:
        raise ValueError("only explicitly synthetic fixtures are supported")
    meta = data.get("runMeta")
    freshness = meta.get("freshness") if isinstance(meta, dict) else None
    generation = freshness.get("generationId") if isinstance(freshness, dict) else None
    if not isinstance(generation, str) or not SAFE_ID.fullmatch(generation) or SENSITIVE.search(generation):
        raise ValueError("fixture generation must be an opaque safe identifier")
    listings = data.get("listings")
    if not isinstance(listings, list) or len(listings) > MAX_LISTINGS:
        raise ValueError("fixture listing count exceeds limit")
    artifact_hash = hashlib.sha256(raw).hexdigest()
    records, omitted, flagged = [], 0, 0
    for ordinal, item in enumerate(listings):
        if not isinstance(item, dict):
            raise TypeError("invalid listing object")
        obs = item.get("buildoutInventoryObservation")
        if obs is None:
            omitted += 1
            continue
        source, mode = item.get("sourceKey"), item.get("transactionMode")
        if not isinstance(source, str) or source not in BUILDOUT_SOURCE_KEYS or mode not in ("sale", "lease"):
            raise ValueError("unknown source or transaction")
        obs = validate_observation(obs)
        if obs.get("generationId") is not None and obs["generationId"] != generation:
            raise ValueError("observation generation does not match artifact")
        raw_id = item.get("id")
        # Never attach or equate native IDs to a canonical listing in this fixture.
        native_id = str(raw_id) if type(raw_id) in (str, int) else None
        if native_id is not None and (not SAFE_ID.fullmatch(native_id) or SENSITIVE.search(native_id)):
            native_id = None
        raw_url = item.get("url")
        source_url = fixture_source_url(raw_url)
        url_state = "present" if source_url is not None else "missing" if raw_url is None else "unsupported"
        if (obs["omittedCount"] or any(v != "valid" for v in obs["provenance"].values())
            or native_id is None or url_state != "present"):
            flagged += 1
        envelope = {"sourceKey": source, "generationId": generation, "transactionMode": mode,
                    "ordinal": ordinal, "identityState": "unresolved", "sourceNativeId": native_id,
                    "sourceUrl": source_url, "sourceUrlState": url_state, "observation": obs}
        digest = hashlib.sha256(canonical(envelope).encode("utf-8")).hexdigest()
        records.append((source, generation, mode, ordinal, artifact_hash, digest, native_id, source_url, url_state, canonical({"sourceUrlState": url_state, "payload": obs})))
    return records, {"artifactSha256": artifact_hash, "observations": len(records),
                     "withoutObservation": omitted, "flaggedObservations": flagged,
                     "status": "partial" if omitted or flagged or not records else "observed_only",
                     "sourceCoverage": "not_evaluated", "sourceAvailability": "not_evaluated"}


def do_block(body):
    """Choose a delimiter that cannot occur in untrusted SQL literals."""
    index = 0
    while True:
        delimiter = f"$cre_observation_{index}$"
        if delimiter not in body:
            return f"DO {delimiter} {body} {delimiter};"
        index += 1


def sql_plan(records):
    lines = ["BEGIN;", "SET LOCAL standard_conforming_strings = on;", "DO $$ BEGIN IF current_database() !~ '^cre_observation_fixture_[0-9a-f]{12}$' OR current_setting('cre.observation_fixture', true) IS DISTINCT FROM 'isolated-synthetic-only' THEN RAISE EXCEPTION 'fixture only'; END IF; END $$;"]
    for source, generation, mode, ordinal, artifact_hash, digest, native_id, source_url, url_state, observation in records:
        values = ", ".join((lit(source), lit(generation), lit(mode), str(ordinal), lit(artifact_hash),
                            lit(digest), "'unresolved'", lit(native_id) if native_id is not None else "NULL",
                            lit(source_url) if source_url is not None else "NULL", lit(observation) + "::jsonb"))
        where = (f"source_key={lit(source)} AND generation_id={lit(generation)} AND "
                 f"transaction_mode={lit(mode)} AND ordinal={ordinal}")
        lines.append("INSERT INTO cre_observation_fixture.buildout_observations "
                     "(source_key,generation_id,transaction_mode,ordinal,artifact_sha256,evidence_sha256,identity_state,source_native_id,source_url,observation) "
                     f"VALUES ({values}) ON CONFLICT DO NOTHING;")
        # Recheck after ON CONFLICT waits, so a concurrent divergent insert cannot be silently accepted.
        guard = (f"BEGIN IF NOT EXISTS (SELECT 1 FROM cre_observation_fixture.buildout_observations WHERE {where} "
                 f"AND artifact_sha256={lit(artifact_hash)} AND evidence_sha256={lit(digest)} "
                 f"AND source_native_id IS NOT DISTINCT FROM {lit(native_id) if native_id is not None else 'NULL'} "
                 f"AND source_url IS NOT DISTINCT FROM {lit(source_url) if source_url is not None else 'NULL'} "
                 f"AND observation={lit(observation)}::jsonb) THEN "
                 "RAISE EXCEPTION 'divergent observation replay'; END IF; END")
        lines.append(do_block(guard))
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def write_plan(path, plan):
    """Create a new private file without following an existing destination."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(plan)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-artifact", type=Path, required=True)
    parser.add_argument("--out-sql", type=Path, required=True)
    args = parser.parse_args()
    records, summary = build_plan(read_fixture(args.fixture_artifact))
    # Caller chooses a private, ignored output path. This output may contain evidence.
    write_plan(args.out_sql, sql_plan(records))
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
