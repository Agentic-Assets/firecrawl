"""Read-only, generation-bound Pyramid identity classification before sealing."""

import hashlib
import json
import re


def stage(artifact):
    from cre_ingest import to_row

    if not isinstance(artifact, dict) or not isinstance(artifact.get("listings"), list):
        raise TypeError("Pyramid artifact missing listings")
    brokers = {i: value for i, value in enumerate(artifact.get("brokers") or [])}
    finished = (artifact.get("runMeta") or {}).get("finishedAt")
    rows, seen = [], set()
    for listing in artifact["listings"]:
        if not isinstance(listing, dict) or listing.get("sourceKey") != "pyramid-brokerage":
            raise ValueError("Pyramid source differs")
        row = to_row(listing, brokers, finished)
        if not row or row.get("slug") != "pyramid-brokerage" or row.get("external_id") in seen:
            raise ValueError("Pyramid staging identity invalid")
        seen.add(row["external_id"])
        native_id = (row.get("raw_data") or {}).get("wordpressRecord", {}).get("id")
        if type(native_id) is not int or not 0 < native_id <= 9007199254740991:
            raise ValueError("Pyramid native identity invalid")
        rows.append({**{k: row[k] for k in ("slug", "external_id", "source_url", "title", "address", "city", "state", "transaction_type", "description")},
                     "native_id": native_id})
    if not rows:
        raise ValueError("Pyramid staging empty")
    return rows


def projection_digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def probe_sql(rows):
    from cre_ingest import sql_lit
    from cre_pyramid_subset_sql import (
        current_index_sql,
        strong_facts_sql,
        valid_native_id_sql,
    )

    payload = sql_lit(json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    return f"""WITH _stage AS (
 SELECT * FROM jsonb_to_recordset({payload}::jsonb) AS s(
   slug text, external_id text, source_url text, title text, address text, city text,
   state text, transaction_type text, description text, native_id bigint)
), _src AS (
 SELECT b.id brokerage_id, s.* FROM _stage s JOIN credeals.cre_brokerages b ON b.slug=s.slug
), _history AS (
 SELECT t.*, ARRAY(SELECT DISTINCT n.value #>> '{{}}' FROM
   jsonb_path_query(t.raw_data, '$.**.wordpressRecord.id') n(value)) native_ids
 FROM credeals.cre_listings t WHERE t.brokerage_id IN (SELECT brokerage_id FROM _src)
), _transitions AS (
 SELECT DISTINCT s.external_id FROM _src s JOIN _history t USING (brokerage_id,external_id)
 WHERE t.source_url=s.source_url AND EXISTS (
   SELECT 1 FROM unnest(t.native_ids) n(value)
   WHERE n.value IS DISTINCT FROM s.native_id::text)
), _conflicts AS (
 SELECT s.external_id FROM _src s JOIN _history t ON t.brokerage_id=s.brokerage_id
 WHERE (t.source_url=s.source_url AND t.external_id IS DISTINCT FROM s.external_id)
    OR (t.external_id=s.external_id AND t.source_url IS DISTINCT FROM s.source_url)
    OR (s.native_id::text=ANY(t.native_ids)
        AND (t.source_url IS DISTINCT FROM s.source_url OR t.external_id IS DISTINCT FROM s.external_id))
    OR (t.external_id=s.external_id AND t.source_url=s.source_url AND
        (t.deleted_at IS NOT NULL OR t.status::text IS DISTINCT FROM 'active'
         OR (cardinality(t.native_ids)>0 AND (cardinality(t.native_ids)<>1
            OR NOT ({valid_native_id_sql("t.native_ids[1]")})
            OR (t.native_ids[1] IS DISTINCT FROM s.native_id::text AND NOT (
                {strong_facts_sql("s", "t")}
                AND {current_index_sql("s.brokerage_id", "s.external_id", "s.source_url")}
                AND NOT EXISTS (SELECT 1 FROM _history h WHERE h.brokerage_id=t.brokerage_id
                    AND h.external_id IS DISTINCT FROM t.external_id AND t.native_ids[1]=ANY(h.native_ids))
            ) IS TRUE)))))
 UNION
 SELECT s.external_id FROM _src s JOIN credeals.cre_source_index i ON i.brokerage_id=s.brokerage_id
 WHERE (i.url=s.source_url AND i.external_id IS DISTINCT FROM s.external_id)
    OR (i.external_id=s.external_id AND (i.url IS DISTINCT FROM s.source_url
        OR i.source_key IS DISTINCT FROM 'pyramid-brokerage'
        OR i.soft_deleted IS DISTINCT FROM false OR i.observation_present IS DISTINCT FROM true))
 UNION
 SELECT s.external_id FROM _src s JOIN _history t USING (brokerage_id,external_id)
 GROUP BY s.external_id HAVING count(*)>1
 UNION
 SELECT s.external_id FROM _src s JOIN credeals.cre_source_index i USING (brokerage_id,external_id)
 GROUP BY s.external_id HAVING count(*)>1
)
SELECT (SELECT count(*) FROM _stage) staged, (SELECT count(*) FROM _src) joined,
       COALESCE((SELECT json_agg(external_id ORDER BY external_id)::text FROM _conflicts),'[]') held,
       COALESCE((SELECT json_agg(external_id ORDER BY external_id)::text FROM _transitions),'[]') transitions"""


def run_probe(artifact_path, artifact_sha256, generation, producer, target_sha256, env_file):
    from cre_checkpoint_refresh import git_identity
    from cre_code_handoff_git import read_file
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    if not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256 or "") or not re.fullmatch(r"[0-9a-f]{64}", target_sha256 or "") or not re.fullmatch(r"[0-9a-f]{40}", producer or "") or not generation:
        raise ValueError("Pyramid probe binding malformed")
    raw = read_file(artifact_path)
    if hashlib.sha256(raw).hexdigest() != artifact_sha256 or git_identity() != (producer, False):
        raise ValueError("Pyramid probe input identity changed")
    artifact = json.loads(raw)
    if (artifact.get("runMeta") or {}).get("freshness", {}).get("generationId") != generation:
        raise ValueError("Pyramid probe generation differs")
    rows = stage(artifact)
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != target_sha256:
        raise ValueError("Pyramid probe target differs")
    sql = "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n" + PIN_STRING_LITERALS + "\n" + probe_sql(rows) + ";\nROLLBACK;\n"
    result = _run_psql_script(find_psql(), db_url, sql, timeout=45)
    if result.returncode:
        raise RuntimeError("Pyramid identity probe failed")
    data = parse_tsv(result.stdout)
    if len(data) != 1 or set(data[0]) != {"staged", "joined", "held", "transitions"} or data[0]["staged"] != str(len(rows)) or data[0]["joined"] != str(len(rows)):
        raise ValueError("Pyramid probe shape differs")
    held = json.loads(data[0]["held"])
    transitions = json.loads(data[0]["transitions"])
    for identities in (held, transitions):
        if not isinstance(identities, list) or identities != sorted(set(identities)) or not set(identities) <= {r["external_id"] for r in rows}:
            raise ValueError("Pyramid probe identities invalid")
    current_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(current_url).get("value") != target_sha256 or read_file(artifact_path) != raw or git_identity() != (producer, False):
        raise ValueError("Pyramid probe input or target changed")
    return {"schema": "pyramid-identity-probe-v1", "artifact_sha256": artifact_sha256,
            "generation_id": generation, "producer_sha": producer, "database_target_sha256": target_sha256,
            "staged_sha256": projection_digest(rows), "held": held, "transitions": transitions}
