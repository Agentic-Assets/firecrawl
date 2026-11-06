"""Shared literal DAUM historical identity contradiction predicate."""

import json


def historical_conflict_sql(*, source_alias: str, target_alias: str, native_sql: str) -> str:
    """Return the exact comparison used by DAUM's transaction guard.

    Arguments are internal SQL identifiers/expressions, never external input.
    """
    s, t = source_alias, target_alias
    return f"""({t}.source_url = {s}.source_url AND {t}.external_id IS DISTINCT FROM {s}.external_id)
       OR ({t}.external_id = {s}.external_id AND {t}.source_url IS DISTINCT FROM {s}.source_url)
       OR ({native_sql} = ANY({t}.native_ids)
           AND ({t}.source_url IS DISTINCT FROM {s}.source_url OR {t}.external_id IS DISTINCT FROM {s}.external_id))
       OR (({t}.source_url = {s}.source_url OR {t}.external_id = {s}.external_id)
           AND cardinality({t}.native_ids) > 0
           AND (cardinality({t}.native_ids) <> 1 OR {t}.native_ids[1] IS DISTINCT FROM {native_sql}))"""


def cohort_members(data):
    """Extract exact source identities, including already-held observations."""
    from cre_daum_current_inventory import _path_key, _post, subset_source_scope

    scope = subset_source_scope(data)
    proofs = [entry["currentInventoryProof"] for entry in scope["entries"]]
    cohort = proofs[0]["cohort"]
    if any(proof.get("cohort") != cohort for proof in proofs):
        raise ValueError("DAUM identity cohorts differ")
    members, seen_native, seen_path = [], set(), set()
    for row in cohort:
        if not isinstance(row, list) or len(row) != 8:
            raise ValueError("DAUM identity cohort member invalid")
        native_id, source_url, external_id = row[:3]
        _post(native_id)
        if (external_id != _path_key(source_url) or native_id in seen_native
                or external_id in seen_path):
            raise ValueError("DAUM identity cohort collision")
        seen_native.add(native_id)
        seen_path.add(external_id)
        members.append({"native_id": native_id, "external_id": external_id, "source_url": source_url})
    if not members or [m["source_url"] for m in members] != sorted(m["source_url"] for m in members):
        raise ValueError("DAUM identity cohort ordering differs")
    return members


def identity_partition_query(members):
    """Classify historical parent/index conflicts; no identity is repaired."""
    from cre_ingest import sql_lit

    payload = sql_lit(json.dumps(members, sort_keys=True, separators=(",", ":")))
    conflict = historical_conflict_sql(source_alias="s", target_alias="t", native_sql="s.native_id")
    return f"""
WITH source AS (
  SELECT b.id AS brokerage_id, s.*
  FROM jsonb_to_recordset({payload}::jsonb) AS s(native_id text, external_id text, source_url text)
  JOIN credeals.cre_brokerages b ON b.slug='daum-commercial'
), history AS (
  SELECT t.brokerage_id, t.external_id, t.source_url,
         ARRAY(SELECT DISTINCT n.value #>> '{{}}' FROM jsonb_path_query(t.raw_data, '$.**.daumPostId') n(value)
               WHERE n.value <> 'null'::jsonb) AS native_ids
  FROM credeals.cre_listings t
  WHERE EXISTS (SELECT 1 FROM source s WHERE s.brokerage_id=t.brokerage_id)
), assessed AS (
  SELECT s.native_id, s.external_id, s.source_url,
    (EXISTS (SELECT 1 FROM history t WHERE t.brokerage_id=s.brokerage_id AND ({conflict}))
     OR EXISTS (SELECT 1 FROM credeals.cre_source_index si WHERE si.brokerage_id=s.brokerage_id
       AND ((si.url=s.source_url AND si.external_id IS DISTINCT FROM s.external_id)
         OR (si.external_id=s.external_id AND si.url IS DISTINCT FROM s.source_url)))) AS conflicted
  FROM source s
)
SELECT (SELECT count(*) FROM source) AS joined,
       COALESCE((SELECT json_agg(assessed ORDER BY source_url)::text FROM assessed), '[]') AS partition
"""


def validate_partition(members, partition):
    """Bind every classified row to one exact source-cohort identity."""
    if not isinstance(partition, list) or len(partition) != len(members):
        raise ValueError("DAUM identity partition cardinality differs")
    for member, observed in zip(members, partition, strict=True):
        if (not isinstance(observed, dict)
                or set(observed) != {"native_id", "external_id", "source_url", "conflicted"}
                or type(observed["conflicted"]) is not bool
                or any(observed[key] != member[key] for key in member)):
            raise ValueError("DAUM identity partition differs")
    return [item for item in partition if item["conflicted"]]


def transaction_partition_sql(members, partition):
    """Reclassify the complete cohort under the existing lifecycle lock.

    This publishes a temporary held set only. Callers must preserve fallback
    staging order and explicitly filter held identities before any upsert.
    """
    from cre_ingest import sql_lit

    validate_partition(members, partition)
    bound = sql_lit(json.dumps(partition, sort_keys=True, separators=(",", ":")))
    return f"""
CREATE TEMP TABLE _daum_identity_partition_recheck ON COMMIT DROP AS
{identity_partition_query(members)};
DO $$ BEGIN
 IF (SELECT joined FROM _daum_identity_partition_recheck) <> {len(members)}
    OR (SELECT partition::jsonb FROM _daum_identity_partition_recheck) IS DISTINCT FROM {bound}::jsonb
 THEN RAISE EXCEPTION 'DAUM identity partition changed'; END IF;
END $$;
CREATE TEMP TABLE _daum_identity_held ON COMMIT DROP AS
SELECT b.id AS brokerage_id, p.native_id, p.external_id, p.source_url
FROM jsonb_to_recordset({bound}::jsonb)
  AS p(native_id text, external_id text, source_url text, conflicted boolean)
JOIN credeals.cre_brokerages b ON b.slug='daum-commercial'
WHERE p.conflicted;
"""


def run_cohort_probe(artifact_path, artifact_sha256, generation, producer, target_sha256, env_file):
    """Read a source-bound partition, with no authority to mutate the target."""
    import hashlib
    import re

    from cre_checkpoint_refresh import git_identity
    from cre_code_handoff_git import read_file
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    if (not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256 or "")
            or not re.fullmatch(r"[0-9a-f]{64}", target_sha256 or "")
            or not re.fullmatch(r"[0-9a-f]{40}", producer or "") or not generation):
        raise ValueError("DAUM identity probe binding invalid")
    raw = read_file(artifact_path)
    db_url, _ = load_db_url(env_file)

    def recheck():
        current_url, _ = load_db_url(env_file)
        if (hashlib.sha256(raw).hexdigest() != artifact_sha256
                or read_file(artifact_path) != raw or git_identity() != (producer, False)
                or database_target_fingerprint_from_url(current_url).get("value") != target_sha256):
            raise ValueError("DAUM identity probe input or target changed")

    recheck()
    members = cohort_members(json.loads(raw))
    sql = ("\\set VERBOSITY verbose\nBEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
           + PIN_STRING_LITERALS + "\n" + identity_partition_query(members) + ";\nROLLBACK;\n")
    result = _run_psql_script(find_psql(), db_url, sql, timeout=45)
    if result.returncode:
        # Unclassified target/query failures are global; no output means no
        # trustworthy partition and does not establish source-local refusal.
        raise RuntimeError("DAUM identity probe target or query failed")
    recheck()
    try:
        rows = parse_tsv(result.stdout)
        if (len(rows) != 1 or set(rows[0]) != {"joined", "partition"}
                or rows[0]["joined"] != str(len(members))):
            raise ValueError("DAUM identity probe row count differs")
        partition = json.loads(rows[0]["partition"])
        validate_partition(members, partition)
    except (ValueError, TypeError, KeyError, SystemExit) as exc:
        raise RuntimeError("DAUM identity partition could not be qualified") from exc
    recheck()
    members_hash = hashlib.sha256(json.dumps(members, sort_keys=True, separators=(",", ":"),
                                               ensure_ascii=False).encode()).hexdigest()
    return {"artifact_sha256": artifact_sha256, "generation_id": generation, "producer_sha": producer,
            "database_target_sha256": target_sha256, "members_sha256": members_hash, "partition": partition}
