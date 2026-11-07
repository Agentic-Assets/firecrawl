"""Read-only target partition for provisional CBRE partial observations.

No result from this module grants write authority. Any future writer must bind
and recheck the same source, target and full preimage under the lifecycle lock.
"""
import json
import re
from copy import deepcopy

from cre_ingest import sql_lit


def _candidate_ids(projection):
    if (not isinstance(projection, dict) or projection.get("source_key") != "cbre"
            or projection.get("write_authorized") is not False
            or projection.get("whole_source_coverage") is not False
            or not isinstance(projection.get("generation"), str) or not projection["generation"]
            or type(projection.get("attempt")) is not int or projection["attempt"] < 1
            or not isinstance(projection.get("sidecar_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", projection["sidecar_sha256"])
            or not isinstance(projection.get("held"), list)
            or not isinstance(projection.get("candidates"), list)):
        raise ValueError("invalid provisional CBRE projection")
    ids = []
    for member in projection["candidates"]:
        identity = member.get("source_id") if isinstance(member, dict) else None
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
            raise ValueError("invalid provisional CBRE identity")
        ids.append(identity)
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate provisional CBRE identity")
    return ids


def identity_probe_sql(projection):
    """Build a read-only exact-native-ID probe; caller binds and verifies target."""
    ids = _candidate_ids(projection)
    payload = sql_lit(json.dumps(ids, separators=(",", ":")))
    return f"""BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout='45s';
WITH members AS (SELECT jsonb_array_elements_text({payload}::jsonb) AS external_id)
SELECT json_build_object('external_id', m.external_id, 'brokerage_id', b.id,
                         'listing', CASE WHEN l.id IS NULL THEN NULL ELSE to_jsonb(l) END) AS result
FROM members m
LEFT JOIN credeals.cre_brokerages b ON b.slug='cbre'
LEFT JOIN credeals.cre_listings l ON l.brokerage_id=b.id AND l.external_id=m.external_id
ORDER BY m.external_id;
ROLLBACK;
"""


def partition_existing(projection, probe_rows):
    """Retain only exact CBRE parent witnesses as provisional update candidates."""
    identities = _candidate_ids(projection)
    if not isinstance(probe_rows, list) or len(probe_rows) != len(identities):
        raise ValueError("CBRE target probe count differs")
    by_id = {}
    for result in probe_rows:
        if not isinstance(result, dict) or result.get("external_id") not in identities:
            raise ValueError("CBRE target probe identity differs")
        key = result["external_id"]
        if key in by_id:
            raise ValueError("duplicate CBRE target probe identity")
        by_id[key] = result
    if set(by_id) != set(identities):
        raise ValueError("CBRE target probe scope differs")
    eligible, held = [], []
    for source in projection["candidates"]:
        identity = source["source_id"]
        probe = by_id[identity]
        row = probe.get("listing")
        reason = None
        if not isinstance(probe.get("brokerage_id"), str) or not probe["brokerage_id"]:
            reason = "brokerage_missing"
        elif row is None:
            reason = "new_parent_without_source_url"
        elif (not isinstance(row, dict)
              or not {"id", "brokerage_id", "external_id", "deleted_at", "source_url", "raw_data"} <= row.keys()
              or not isinstance(row.get("id"), str) or not row["id"]
              or row.get("brokerage_id") != probe["brokerage_id"]
              or row.get("external_id") != identity):
            reason = "target_identity_invalid"
        elif row.get("deleted_at") is not None:
            reason = "target_retired"
        elif not isinstance(row.get("source_url"), str) or not row["source_url"]:
            reason = "target_source_url_missing"
        else:
            raw = row.get("raw_data")
            if not isinstance(raw, dict) or "cbrePartialObservations" in raw:
                reason = "target_history_unverified"
            else:
                contexts = [raw, raw.get("primary"), raw.get("secondary_pass")]
                contexts = [item for item in contexts if isinstance(item, dict)]
                witnesses = [(item.get("sourceKey"), item.get("id")) for item in contexts
                             if "sourceKey" in item or "id" in item]
                if not witnesses or any(source_key != "cbre" or key != identity for source_key, key in witnesses):
                    reason = "target_source_witness_invalid"
        if reason:
            held.append({"source_id": identity, "reason": reason, "source": deepcopy(source),
                         "target": deepcopy(probe)})
        else:
            eligible.append({"source_id": identity, "source": deepcopy(source), "target_preimage": deepcopy(row)})
    return {"source_key": "cbre", "generation": projection["generation"], "attempt": projection["attempt"],
            "sidecar_sha256": projection["sidecar_sha256"], "whole_source_coverage": False,
            "write_authorized": False, "provisional_existing": eligible, "held": held,
            "source_held": deepcopy(projection["held"])}


def partition_sidecar(payload, *, generation, attempt, custody, expected_page_size, probe_rows):
    """Derive a provisional target partition from the exact retained sidecar."""
    from cbre_partial_projection import project

    projection = project(payload, generation=generation, attempt=attempt, custody=custody,
                         expected_page_size=expected_page_size)
    return partition_existing(projection, probe_rows)
