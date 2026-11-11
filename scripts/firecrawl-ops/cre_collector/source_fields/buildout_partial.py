"""Source-bound inputs for a future verified Buildout held-identity subset.

This module does not authorize ingestion or claim a successful partial write.
"""

import hashlib
import json
import re
from decimal import Decimal
from uuid import UUID

from cre_buildout_current_inventory import definitions
from cre_ingest import merge_rows
from source_fields.buildout import (
    prepare_inventory_observation_scope,
    validate_inventory,
)
from source_fields.buildout_observation_sql import _literal, identity_partition_ctes


def prepare_partition(raw, manifest, source, *, code_sha, dirty, target_sha, attempt_id):
    """Derive exact staged identities from the already-qualified source artifact."""
    if not isinstance(attempt_id, str) or re.fullmatch(r"[a-f0-9]{32}", attempt_id) is None:
        raise ValueError("invalid Buildout partial ingest attempt")
    scope = prepare_inventory_observation_scope(
        raw, manifest, source, current_code_sha=code_sha,
        current_git_dirty=dirty, current_target_sha256=target_sha,
    )
    artifact = json.loads(raw)
    validated = validate_inventory(
        artifact, source, definitions()[source], manifest["run_id"], manifest["started_at"]
    )
    bindings = {external: (transaction, native) for external, transaction, native in validated["staged_bindings"]}
    staged = []
    for external, members in sorted(validated["staged"].items()):
        row = members[0][1]
        for _, member in members[1:]:
            row = merge_rows(row, member)
        transaction, native = bindings[external]
        if row["external_id"] != external or row["transaction_type"] != transaction:
            raise ValueError("Buildout partial staged identity changed")
        staged.append({"external_id": external, "transaction_type": transaction,
                       "native_ids": native, "source_url": row["source_url"],
                       "scraped_at": row["scraped_at"], "raw_data": row["raw_data"]})
    if not staged or [item["external_id"] for item in staged] != scope["staged_external_ids"]:
        raise ValueError("Buildout partial staged partition differs")
    payload = json.dumps(staged, ensure_ascii=False, allow_nan=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return {"scope": scope, "attempt_id": attempt_id, "staged": staged,
            "staged_sha256": hashlib.sha256(payload).hexdigest()}


def _validated_partition_payload(partition):
    """Check staged bytes at every boundary; callers must separately bind the artifact."""
    if not isinstance(partition, dict) or not isinstance(partition.get("scope"), dict):
        raise TypeError("invalid Buildout partial partition")
    source = partition["scope"].get("source_key")
    staged = partition.get("staged")
    if (not isinstance(source, str) or not isinstance(staged, list) or not staged
            or any(not isinstance(item, dict) or set(item) != {
                "external_id", "transaction_type", "native_ids", "source_url", "scraped_at", "raw_data"
            } for item in staged)):
        raise ValueError("invalid Buildout partial staged rows")
    raw = json.dumps(staged, ensure_ascii=False, allow_nan=False, sort_keys=True,
                     separators=(",", ":"))
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != partition.get("staged_sha256"):
        raise ValueError("Buildout partial staged rows changed")
    return raw


def identity_preseal_sql(partition):
    """Read a whole-source hold decision; never authorize a partial write."""
    raw = _validated_partition_payload(partition)
    source = partition["scope"]["source_key"]
    predicate = identity_partition_ctes([source])
    return f"""
WITH _src AS MATERIALIZED (
SELECT b.id AS brokerage_id, b.slug AS slug, item.value->>'external_id' AS external_id,
       item.value->>'source_url' AS source_url, item.value->'raw_data' AS raw_data,
       (item.value->>'scraped_at')::timestamptz AS scraped_at
FROM jsonb_array_elements({_literal(raw)}::jsonb) AS item(value)
JOIN credeals.cre_brokerages b ON b.slug={_literal(source)}
),
{predicate}
SELECT (SELECT count(*) FROM credeals.cre_brokerages WHERE slug={_literal(source)})::text AS brokerages,
       (SELECT count(*) FROM _src)::text AS staged,
       COALESCE((SELECT jsonb_agg(external_id ORDER BY external_id) FROM _buildout_identity_held), '[]'::jsonb)::text AS held;
"""


def identity_probe_sql(partition):
    """Project the guard's actual partition and exact parent/index/child preimages.

    The caller must run this in a target-bound read-only transaction, retain the
    exact returned bytes, and recheck them under transaction locks before use.
    """
    raw = _validated_partition_payload(partition)
    source = partition["scope"]["source_key"]
    predicate = identity_partition_ctes([source])
    return f"""
WITH _src AS MATERIALIZED (
SELECT b.id AS brokerage_id, b.slug AS slug, item.value->>'external_id' AS external_id,
       item.value->>'source_url' AS source_url, item.value->'raw_data' AS raw_data,
       (item.value->>'scraped_at')::timestamptz AS scraped_at
FROM jsonb_array_elements({_literal(raw)}::jsonb) AS item(value)
JOIN credeals.cre_brokerages b ON b.slug={_literal(source)}
),
{predicate},
related AS (
 SELECT brokerage_id, external_id FROM _src
 UNION
 SELECT other.brokerage_id, other.external_id
 FROM _src s
 JOIN _buildout_identity_claims own USING (brokerage_id, external_id)
 JOIN _buildout_identity_claims other ON other.brokerage_id=own.brokerage_id
   AND other.native_id=own.native_id AND other.external_id<>own.external_id
), conflicts AS (
 SELECT DISTINCT h.external_id AS held_external_id, other.external_id AS related_external_id
 FROM _buildout_identity_held h
 JOIN _buildout_identity_claims own USING (brokerage_id, external_id)
 JOIN _buildout_identity_claims other ON other.brokerage_id=own.brokerage_id
   AND other.native_id=own.native_id AND other.external_id<>own.external_id
), preimage AS (
 SELECT r.brokerage_id::text AS brokerage_id, r.external_id, to_jsonb(l) AS listing, to_jsonb(si) AS source_index,
   jsonb_build_object(
     'contacts', COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.id) FROM credeals.cre_listing_contacts c WHERE c.listing_id=l.id), '[]'::jsonb),
     'documents', COALESCE((SELECT jsonb_agg(to_jsonb(d) ORDER BY d.id) FROM credeals.cre_listing_documents d WHERE d.listing_id=l.id), '[]'::jsonb),
     'images', COALESCE((SELECT jsonb_agg(to_jsonb(i) ORDER BY i.id) FROM credeals.cre_listing_images i WHERE i.listing_id=l.id), '[]'::jsonb),
     'media', COALESCE((SELECT jsonb_agg(to_jsonb(m) ORDER BY m.id) FROM credeals.cre_listing_media m WHERE m.listing_id=l.id), '[]'::jsonb),
     'links', COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.id) FROM credeals.cre_listing_links x WHERE x.listing_id=l.id), '[]'::jsonb)
   ) AS children
 FROM related r
 LEFT JOIN credeals.cre_listings l USING (brokerage_id, external_id)
 LEFT JOIN credeals.cre_source_index si USING (brokerage_id, external_id)
)
SELECT (SELECT id::text FROM credeals.cre_brokerages WHERE slug={_literal(source)}) AS brokerage_id,
 (SELECT count(*) FROM _src)::text AS staged,
 COALESCE((SELECT jsonb_agg(external_id ORDER BY external_id) FROM _buildout_identity_held), '[]'::jsonb)::text AS held,
 COALESCE((SELECT jsonb_agg(to_jsonb(conflicts) ORDER BY held_external_id, related_external_id) FROM conflicts), '[]'::jsonb)::text AS conflicts,
 COALESCE((SELECT jsonb_agg(to_jsonb(preimage) ORDER BY external_id) FROM preimage), '[]'::jsonb)::text AS preimage;
"""


def validate_probe(partition, row):
    """Validate one result while retaining the database's exact preimage text.

    The caller must independently rederive ``partition`` from the sealed
    artifact and recheck the target and owner before relying on this result.
    """
    _validated_partition_payload(partition)
    if not isinstance(row, dict) or set(row) != {"brokerage_id", "staged", "held", "conflicts", "preimage"}:
        raise ValueError("Buildout partial probe result is malformed")
    try:
        if str(UUID(row["brokerage_id"])) != row["brokerage_id"]:
            raise ValueError("noncanonical brokerage")
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Buildout partial brokerage is malformed") from exc
    staged = partition["staged"]
    expected = {item["external_id"] for item in staged}
    if (len(expected) != len(staged) or row["staged"] != str(len(staged))
            or not all(isinstance(row[key], str) for key in ("held", "conflicts", "preimage"))):
        raise ValueError("Buildout partial probe staged identities differ")
    try:
        held = json.loads(row["held"])
        conflicts = json.loads(row["conflicts"])
        preimage = json.loads(row["preimage"], parse_float=Decimal)
    except (TypeError, ValueError) as exc:
        raise ValueError("Buildout partial probe JSON is malformed") from exc
    if (not isinstance(held, list) or any(not isinstance(item, str) or not item for item in held)
            or len(held) != len(set(held)) or not set(held) <= expected
            or not isinstance(conflicts, list) or not isinstance(preimage, list)):
        raise ValueError("Buildout partial held identities differ")
    pairs = set()
    for item in conflicts:
        if (not isinstance(item, dict) or set(item) != {"held_external_id", "related_external_id"}
                or item["held_external_id"] not in held
                or not isinstance(item["related_external_id"], str) or not item["related_external_id"]
                or item["held_external_id"] == item["related_external_id"]):
            raise ValueError("Buildout partial conflict evidence differs")
        pairs.add((item["held_external_id"], item["related_external_id"]))
    if len(pairs) != len(conflicts):
        raise ValueError("Buildout partial conflict evidence is duplicated")
    required = expected | {other for _, other in pairs}
    found = set()
    brokerages = set()
    for item in preimage:
        if (not isinstance(item, dict) or set(item) != {"brokerage_id", "external_id", "listing", "source_index", "children"}
                or item["brokerage_id"] != row["brokerage_id"]
                or not isinstance(item["external_id"], str) or not item["external_id"]
                or item["external_id"] in found
                or (item["listing"] is not None and not isinstance(item["listing"], dict))
                or (item["source_index"] is not None and not isinstance(item["source_index"], dict))
                or not isinstance(item["children"], dict)
                or set(item["children"]) != {"contacts", "documents", "images", "media", "links"}
                or any(not isinstance(value, list) for value in item["children"].values())):
            raise ValueError("Buildout partial preimage is malformed")
        for record in (item["listing"], item["source_index"]):
            if record is not None and (record.get("external_id") != item["external_id"]
                                       or record.get("brokerage_id") != item["brokerage_id"]):
                raise ValueError("Buildout partial preimage source differs")
        listing_id = (item["listing"] or {}).get("id")
        if item["listing"] is not None:
            try:
                if str(UUID(listing_id)) != listing_id:
                    raise ValueError("noncanonical listing")
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValueError("Buildout partial listing ID is malformed") from exc
        elif item["external_id"] not in expected:
            raise ValueError("Buildout partial related listing is missing")
        for values in item["children"].values():
            child_ids = set()
            for child in values:
                if (listing_id is None or not isinstance(child, dict)
                        or child.get("listing_id") != listing_id or not isinstance(child.get("id"), str)):
                    raise ValueError("Buildout partial child preimage differs")
                try:
                    child_id = str(UUID(child["id"]))
                except (TypeError, ValueError, AttributeError) as exc:
                    raise ValueError("Buildout partial child ID is malformed") from exc
                if child_id != child["id"] or child_id in child_ids:
                    raise ValueError("Buildout partial child identity differs")
                child_ids.add(child_id)
        found.add(item["external_id"])
        brokerages.add(item["brokerage_id"])
    if found != required or len(brokerages) != 1:
        raise ValueError("Buildout partial preimage identities differ")
    binding = {"scope": partition["scope"], "attempt_id": partition["attempt_id"],
               "brokerage_id": row["brokerage_id"],
               "staged_sha256": partition["staged_sha256"]}
    raw_binding = json.dumps(binding, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    result = {**binding, "binding_sha256": hashlib.sha256(raw_binding).hexdigest(),
              "held": held, "conflicts": conflicts, "preimage": row["preimage"],
              "preimage_sha256": hashlib.sha256(row["preimage"].encode("utf-8")).hexdigest()}
    envelope = json.dumps(result, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    return {**result, "proof_sha256": hashlib.sha256(envelope).hexdigest()}


def verify_probe(partition, proof):
    """Reject edits to a probe's binding, held set, conflicts or preimage."""
    if not isinstance(proof, dict):
        raise TypeError("Buildout partial proof is malformed")
    try:
        row = {"brokerage_id": proof["brokerage_id"], "staged": str(len(partition["staged"])),
               "held": json.dumps(proof["held"]), "conflicts": json.dumps(proof["conflicts"]),
               "preimage": proof["preimage"]}
        expected = validate_probe(partition, row)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Buildout partial proof is malformed") from exc
    if expected != proof:
        raise ValueError("Buildout partial proof changed")
    return proof
