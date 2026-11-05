"""DAUM fallback identity partition; this module grants no write authority."""

def staged_fallback_members(data):
    """Bind corroborated fallback witnesses to their source listing identities."""
    from cre_daum_current_inventory import SUBSET_SCHEMA_V2, _path_key, _post, _time, subset_source_scope
    from cre_ingest import is_daum_title_fallback

    scope = subset_source_scope(data)
    proofs = [entry["currentInventoryProof"] for entry in data["sources"]]
    if any(p.get("schema") != SUBSET_SCHEMA_V2 for p in proofs):
        raise ValueError("DAUM fallback requires V2 proof")
    witnesses = proofs[0].get("titleFallbackObservations", [])
    if not isinstance(witnesses, list) or any(p.get("titleFallbackObservations", []) != witnesses for p in proofs):
        raise ValueError("DAUM fallback witnesses differ")
    expected = {}
    for item in witnesses:
        if not isinstance(item, dict) or set(item) != {"postId", "cardTitle", "documentTitle"}:
            raise ValueError("DAUM fallback witness shape invalid")
        post, title = item["postId"], item["cardTitle"]
        _post(post)
        if (post in expected or not isinstance(title, str) or not title.strip()
                or title != title.strip() or item["documentTitle"] != f"{title} – Daum Commercial"):
            raise ValueError("DAUM fallback witness invalid")
        expected[post] = item
    rows = {}
    for listing in scope["listings"]:
        if not isinstance(listing, dict):
            raise ValueError("DAUM listing invalid")
        post = listing.get("daumPostId")
        if post not in expected:
            if "daumTitleFallback" in listing:
                raise ValueError("DAUM fallback witness missing")
            continue
        if not is_daum_title_fallback(listing):
            raise ValueError("DAUM fallback listing invalid")
        source_url = listing["url"]
        external_id = _path_key(source_url)
        if listing.get("id") != external_id or listing["daumTitleFallback"] != {
                "cardTitle": expected[post]["cardTitle"], "documentTitle": expected[post]["documentTitle"]}:
            raise ValueError("DAUM fallback source binding differs")
        inventory_at, detail_at = listing.get("inventoryObservedAt"), listing.get("detailObservedAt")
        if _time(inventory_at) > _time(detail_at):
            raise ValueError("DAUM fallback observation times differ")
        row = {"native_id": post, "external_id": external_id, "source_url": source_url,
               "inventory_at": inventory_at, "detail_at": detail_at}
        if post in rows and rows[post] != row:
            raise ValueError("DAUM fallback pass identity differs")
        rows[post] = row
    if set(rows) != set(expected):
        raise ValueError("DAUM fallback listing missing")
    return sorted(rows.values(), key=lambda row: row["external_id"])

def identity_probe_sql(members):
    """Produce source-local classifications and exact target preimage hashes.

    This is a read-only probe. Admission must bind its output to the artifact,
    generation and target, and mutation must repeat it under the lifecycle lock.
    """
    import json

    from cre_daum_current_inventory import _path_key, _post, _time
    from cre_ingest import sql_lit

    if not isinstance(members, list):
        raise ValueError("DAUM fallback members invalid")
    ids, keys, urls = set(), set(), set()
    for row in members:
        if not isinstance(row, dict) or set(row) != {"native_id", "external_id", "source_url", "inventory_at", "detail_at"}:
            raise ValueError("DAUM fallback member invalid")
        _post(row["native_id"])
        if _time(row["inventory_at"]) > _time(row["detail_at"]):
            raise ValueError("DAUM fallback observation times differ")
        if (row["external_id"] != _path_key(row["source_url"])
                or row["native_id"] in ids or row["external_id"] in keys
                or row["source_url"] in urls):
            raise ValueError("DAUM fallback member identity collision")
        ids.add(row["native_id"])
        keys.add(row["external_id"])
        urls.add(row["source_url"])
    payload = sql_lit(json.dumps(members, sort_keys=True, separators=(",", ":")))
    return f"""
WITH candidate AS (
  SELECT * FROM jsonb_to_recordset({payload}::jsonb)
    AS c(native_id text, external_id text, source_url text, inventory_at timestamptz, detail_at timestamptz)
), scoped AS (
  SELECT c.*, b.id AS brokerage_id FROM candidate c
  JOIN credeals.cre_brokerages b ON b.slug='daum-commercial'
), assessed AS (
  SELECT c.*,
    CASE
      WHEN EXISTS (
        SELECT 1 FROM credeals.cre_listings other
        WHERE other.brokerage_id=c.brokerage_id
          AND ((other.source_url=c.source_url AND other.external_id IS DISTINCT FROM c.external_id)
            OR (other.external_id=c.external_id AND other.source_url IS DISTINCT FROM c.source_url)
            OR (other.external_id IS DISTINCT FROM c.external_id AND EXISTS (
              SELECT 1 FROM jsonb_path_query(other.raw_data, '$.**.daumPostId') v
              WHERE v #>> '{{}}' = c.native_id)))
      ) OR EXISTS (
        SELECT 1 FROM credeals.cre_source_index si
        WHERE si.brokerage_id=c.brokerage_id
          AND ((si.url=c.source_url AND si.external_id IS DISTINCT FROM c.external_id)
            OR (si.external_id=c.external_id AND si.url IS DISTINCT FROM c.source_url))
      ) THEN 'identity_conflict'
      WHEN own.id IS NOT NULL AND (
        own.deleted_at IS NOT NULL OR own.status::text IS DISTINCT FROM 'active'
        OR (own.canonical_url IS NOT NULL AND own.canonical_url IS DISTINCT FROM c.source_url)
        OR jsonb_typeof(own.raw_data) IS DISTINCT FROM 'object'
        OR NOT jsonb_path_exists(own.raw_data, '$.**.sourceKey ? (@ == "daum-commercial")')
        OR ARRAY(SELECT DISTINCT v #>> '{{}}' FROM jsonb_path_query(own.raw_data, '$.**.daumPostId') v)
           IS DISTINCT FROM ARRAY[c.native_id]
      ) THEN 'existing_parent_unqualified'
      WHEN own.id IS NOT NULL AND (
        (own.raw_data ? 'daumTitleFallbackHistory'
         AND jsonb_typeof(own.raw_data->'daumTitleFallbackHistory') IS DISTINCT FROM 'array')
        OR (own.raw_data ? 'daumFreshnessOwner' AND (
              COALESCE(own.raw_data->>'daumFreshnessOwner' NOT IN ('title_fallback', 'unknown_tenure'), true)
              OR (own.raw_data->>'daumFreshnessOwner' = 'title_fallback'
                  AND NOT (own.raw_data ? 'latestDaumTitleFallbackObservation'))
              OR (own.raw_data->>'daumFreshnessOwner' = 'unknown_tenure'
                  AND (jsonb_typeof(own.raw_data->'latestDaumUnknownTenureObservation') IS DISTINCT FROM 'object'
                       OR own.raw_data#>>'{{latestDaumUnknownTenureObservation,sourceKey}}' IS DISTINCT FROM 'daum-commercial'
                       OR own.raw_data#>'{{latestDaumUnknownTenureObservation,daumUnknownTenure}}' IS DISTINCT FROM 'true'::jsonb))))
        OR (own.raw_data ? 'latestDaumTitleFallbackObservation' AND CASE
          WHEN jsonb_typeof(own.raw_data->'latestDaumTitleFallbackObservation') = 'object'
           AND jsonb_typeof(own.raw_data#>'{{latestDaumTitleFallbackObservation,freshnessProvenance}}') = 'object'
          THEN (
            ((own.raw_data->'latestDaumTitleFallbackObservation')
             - 'sourceKey' - 'inventoryObservedAt' - 'detailObservedAt' - 'freshnessProvenance') <> '{{}}'::jsonb
            OR own.raw_data#>>'{{latestDaumTitleFallbackObservation,sourceKey}}' IS DISTINCT FROM 'daum-commercial'
            OR ((own.raw_data#>'{{latestDaumTitleFallbackObservation,freshnessProvenance}}')
                - 'generationId' - 'detailScope' - 'cacheDisposition' - 'method' - 'identityMethod') <> '{{}}'::jsonb
            OR NULLIF(btrim(own.raw_data#>>'{{latestDaumTitleFallbackObservation,freshnessProvenance,generationId}}'), '') IS NULL
            OR own.raw_data#>>'{{latestDaumTitleFallbackObservation,freshnessProvenance,detailScope}}' IS DISTINCT FROM 'detail_page'
            OR own.raw_data#>>'{{latestDaumTitleFallbackObservation,freshnessProvenance,cacheDisposition}}' IS DISTINCT FROM 'live'
            OR own.raw_data#>>'{{latestDaumTitleFallbackObservation,freshnessProvenance,method}}' IS DISTINCT FROM 'daum_direct_detail_html'
            OR own.raw_data#>>'{{latestDaumTitleFallbackObservation,freshnessProvenance,identityMethod}}' IS DISTINCT FROM 'wordpress_shortlink_post_id'
            OR CASE WHEN jsonb_typeof(own.raw_data->'daumTitleFallbackHistory') = 'array'
                    THEN jsonb_array_length(own.raw_data->'daumTitleFallbackHistory') = 0
                    ELSE true END
            OR NOT COALESCE(pg_input_is_valid(
              own.raw_data#>>'{{latestDaumTitleFallbackObservation,inventoryObservedAt}}',
              'timestamp with time zone'), false)
            OR NOT COALESCE(pg_input_is_valid(
              own.raw_data#>>'{{latestDaumTitleFallbackObservation,detailObservedAt}}',
              'timestamp with time zone'), false)
          ) ELSE true END)
      ) THEN 'existing_evidence_unqualified'
      WHEN si.id IS NOT NULL AND (
        si.soft_deleted IS DISTINCT FROM false OR si.observation_present IS DISTINCT FROM true
        OR si.source_key IS DISTINCT FROM 'daum-commercial'
      ) THEN 'source_index_unqualified'
      WHEN own.id IS NULL AND si.id IS NOT NULL THEN 'orphan_source_index'
      WHEN own.scraped_at > c.detail_at OR si.last_enumerated_at > c.inventory_at
        OR CASE WHEN own.raw_data ? 'latestDaumTitleFallbackObservation'
          THEN (own.raw_data#>>'{{latestDaumTitleFallbackObservation,inventoryObservedAt}}')::timestamptz > c.inventory_at
            OR (own.raw_data#>>'{{latestDaumTitleFallbackObservation,detailObservedAt}}')::timestamptz >= c.detail_at
          ELSE false END
        THEN 'newer_target_observation'
      ELSE NULL
    END AS hold_reason,
    encode(sha256(convert_to(COALESCE(to_jsonb(own)::text,'null') || E'\\n' || COALESCE(to_jsonb(si)::text,'null'), 'UTF8')), 'hex') AS preimage
  FROM scoped c
  LEFT JOIN credeals.cre_listings own ON own.brokerage_id=c.brokerage_id AND own.external_id=c.external_id
  LEFT JOIN credeals.cre_source_index si ON si.brokerage_id=c.brokerage_id AND si.external_id=c.external_id
)
SELECT (SELECT count(*) FROM candidate) AS staged,
       (SELECT count(*) FROM scoped) AS joined,
       COALESCE((SELECT json_agg(json_build_object(
         'native_id', native_id, 'external_id', external_id, 'source_url', source_url,
         'hold_reason', hold_reason, 'preimage', preimage)
         ORDER BY external_id)::text FROM assessed), '[]') AS partition
"""

def transaction_guard_sql(members, partition, *, filter_src=False):
    """Lock owned targets and recheck the bound partition before filtering holds."""
    import json
    import re

    from cre_ingest import sql_lit

    # Reuse the probe's input validation before constructing any SQL.
    query = identity_probe_sql(members)
    if not isinstance(partition, list) or len(partition) != len(members):
        raise ValueError("DAUM fallback partition shape invalid")
    expected = {item["external_id"]: item for item in members}
    seen = set()
    for item in partition:
        if not isinstance(item, dict) or set(item) != {
                "native_id", "external_id", "source_url", "hold_reason", "preimage"}:
            raise ValueError("DAUM fallback partition entry invalid")
        key = item["external_id"]
        if (key in seen or key not in expected
                or any(item[k] != expected[key][k] for k in ("native_id", "external_id", "source_url"))
                or item["hold_reason"] not in (None, "identity_conflict", "existing_parent_unqualified",
                                               "source_index_unqualified", "orphan_source_index",
                                               "newer_target_observation", "existing_evidence_unqualified")
                or not isinstance(item["preimage"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", item["preimage"])):
            raise ValueError("DAUM fallback partition entry differs")
        seen.add(key)
    if [item["external_id"] for item in partition] != sorted(expected):
        raise ValueError("DAUM fallback partition ordering differs")
    payload = sql_lit(json.dumps(members, sort_keys=True, separators=(",", ":")))
    bound = sql_lit(json.dumps(partition, sort_keys=True, separators=(",", ":")))
    sql = f"""
CREATE TEMP TABLE _daum_fallback_members ON COMMIT DROP AS
SELECT b.id AS brokerage_id, m.*
FROM jsonb_to_recordset({payload}::jsonb)
  AS m(native_id text, external_id text, source_url text, inventory_at timestamptz, detail_at timestamptz)
JOIN credeals.cre_brokerages b ON b.slug='daum-commercial';
-- The transaction-wide lifecycle advisory lock is already held. Lock in the
-- same source-index-then-parent order used by the shared ingest.
CREATE TEMP TABLE _daum_fallback_source_locks ON COMMIT DROP AS
SELECT si.id FROM credeals.cre_source_index si JOIN _daum_fallback_members m
  ON si.brokerage_id=m.brokerage_id AND si.external_id=m.external_id
  ORDER BY si.id FOR UPDATE OF si;
CREATE TEMP TABLE _daum_fallback_parent_locks ON COMMIT DROP AS
SELECT t.id FROM credeals.cre_listings t JOIN _daum_fallback_members m
  ON t.brokerage_id=m.brokerage_id AND t.external_id=m.external_id
  ORDER BY t.id FOR UPDATE OF t;
CREATE TEMP TABLE _daum_fallback_recheck ON COMMIT DROP AS
{query};
DO $$
BEGIN
 IF (SELECT count(*) FROM _daum_fallback_members) <> {len(members)}
 OR (SELECT staged FROM _daum_fallback_recheck) <> {len(members)}
 OR (SELECT joined FROM _daum_fallback_recheck) <> {len(members)}
 OR (SELECT partition::jsonb FROM _daum_fallback_recheck) IS DISTINCT FROM {bound}::jsonb
 THEN RAISE EXCEPTION 'DAUM fallback partition changed'; END IF;
END $$;
"""
    if filter_src:
        sql += f"""
CREATE TEMP TABLE _daum_fallback_partition ON COMMIT DROP AS
SELECT * FROM jsonb_to_recordset({bound}::jsonb)
 AS p(native_id text, external_id text, source_url text, hold_reason text, preimage text);
DO $$
BEGIN
 IF (SELECT count(*) FROM _daum_fallback_partition) <> {len(members)}
 OR EXISTS (
   SELECT 1 FROM _src s WHERE jsonb_path_exists(s.raw_data, '$.**.daumTitleFallback')
     AND NOT EXISTS (SELECT 1 FROM _daum_fallback_members m
                     WHERE m.brokerage_id=s.brokerage_id AND m.external_id=s.external_id)
 )
 OR EXISTS (
   SELECT 1 FROM _daum_fallback_members m
   LEFT JOIN _src s ON s.brokerage_id=m.brokerage_id AND s.external_id=m.external_id
   WHERE s.external_id IS NULL OR s.source_url IS DISTINCT FROM m.source_url
      OR NOT jsonb_path_exists(s.raw_data, '$.**.sourceKey ? (@ == "daum-commercial")')
      OR ARRAY(SELECT DISTINCT v #>> '{{}}' FROM jsonb_path_query(s.raw_data, '$.**.daumPostId') v)
         IS DISTINCT FROM ARRAY[m.native_id]
 )
 THEN RAISE EXCEPTION 'DAUM fallback staging differs'; END IF;
END $$;
CREATE TEMP TABLE _daum_fallback_held ON COMMIT DROP AS
SELECT m.brokerage_id, m.external_id, m.native_id, p.hold_reason
FROM _daum_fallback_members m JOIN _daum_fallback_partition p
  ON p.external_id=m.external_id
WHERE p.hold_reason IS NOT NULL;
CREATE TEMP TABLE _daum_fallback_apply ON COMMIT DROP AS
SELECT s.*, m.inventory_at AS fallback_inventory_at, m.detail_at AS fallback_detail_at,
       t.id AS fallback_parent_id
FROM _daum_fallback_members m JOIN _daum_fallback_partition p
  ON p.external_id=m.external_id AND p.hold_reason IS NULL
JOIN _src s ON s.brokerage_id=m.brokerage_id AND s.external_id=m.external_id
LEFT JOIN credeals.cre_listings t ON t.brokerage_id=m.brokerage_id AND t.external_id=m.external_id;
CREATE TEMP TABLE _daum_fallback_skipped ON COMMIT DROP AS
SELECT brokerage_id, external_id FROM _daum_fallback_held
UNION ALL
SELECT brokerage_id, external_id FROM _daum_fallback_apply WHERE fallback_parent_id IS NOT NULL;
DELETE FROM _src s USING _daum_fallback_skipped h
 WHERE s.brokerage_id=h.brokerage_id AND s.external_id=h.external_id;
"""
    return sql


class DaumSourceProbeRefusal(Exception):
    """A settled source-local identity query cannot qualify this source."""


def run_identity_probe(artifact_path, artifact_sha256, generation, producer, target_sha256, env_file):
    """Capture a read-only partition bound to the source artifact and target."""
    import hashlib
    import json
    import re
    import subprocess

    from cre_checkpoint_refresh import git_identity
    from cre_code_handoff_git import read_file
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    if (not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256 or "")
            or not re.fullmatch(r"[0-9a-f]{64}", target_sha256 or "")
            or not re.fullmatch(r"[0-9a-f]{40}", producer or "") or not generation):
        raise ValueError("DAUM fallback probe binding invalid")
    raw = read_file(artifact_path)
    if hashlib.sha256(raw).hexdigest() != artifact_sha256 or git_identity() != (producer, False):
        raise ValueError("DAUM fallback probe input changed")
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != target_sha256:
        raise ValueError("DAUM fallback probe target differs")

    def recheck():
        current_url, _ = load_db_url(env_file)
        if (database_target_fingerprint_from_url(current_url).get("value") != target_sha256
                or read_file(artifact_path) != raw or git_identity() != (producer, False)):
            raise ValueError("DAUM fallback probe input or target changed")

    def source_refusal(cause):
        # The direct psql child is reaped by subprocess.run on timeout. A
        # separate read-only check must prove this exact target is healthy.
        recheck()
        try:
            health = _run_psql_script(find_psql(), db_url,
                                      "BEGIN TRANSACTION READ ONLY;\nSELECT 1 AS healthy;\nROLLBACK;\n",
                                      timeout=10)
            healthy = health.returncode == 0 and parse_tsv(health.stdout) == [{"healthy": "1"}]
        except (OSError, RuntimeError, ValueError, TypeError, SystemExit, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("DAUM fallback probe target health is unknown") from exc
        recheck()
        if not healthy:
            raise RuntimeError("DAUM fallback probe target health is unknown") from cause
        raise DaumSourceProbeRefusal("DAUM fallback identity could not be qualified") from cause

    try:
        members = staged_fallback_members(json.loads(raw))
        if not members:
            raise ValueError("DAUM fallback probe has no candidates")
        query = identity_probe_sql(members)
    except ValueError as exc:
        source_refusal(exc)
    sql = ("\\set VERBOSITY verbose\nBEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
           + PIN_STRING_LITERALS + "\n" + query + ";\nROLLBACK;\n")
    try:
        result = _run_psql_script(find_psql(), db_url, sql, timeout=45)
    except subprocess.TimeoutExpired as exc:
        source_refusal(exc)
    if result.returncode:
        state = re.search(r"ERROR:\s+([0-9A-Z]{5}):", getattr(result, "stderr", "") or "")
        if (result.returncode == 3 and state
                and (state.group(1).startswith("22") or state.group(1) in {"42601", "57014"})):
            source_refusal(RuntimeError("DAUM source identity query was refused"))
        raise RuntimeError("DAUM fallback identity probe target or query failed")
    recheck()
    try:
        rows = parse_tsv(result.stdout)
        if (len(rows) != 1 or set(rows[0]) != {"staged", "joined", "partition"}
                or rows[0]["staged"] != str(len(members)) or rows[0]["joined"] != str(len(members))):
            raise ValueError("DAUM fallback probe result differs")
        partition = json.loads(rows[0]["partition"])
        transaction_guard_sql(members, partition)
    except (ValueError, TypeError, KeyError, SystemExit) as exc:
        source_refusal(exc)
    recheck()
    member_hash = hashlib.sha256(json.dumps(members, sort_keys=True, separators=(",", ":"),
                                             ensure_ascii=False).encode()).hexdigest()
    return {"artifact_sha256": artifact_sha256, "generation_id": generation,
            "producer_sha": producer, "database_target_sha256": target_sha256,
            "members_sha256": member_hash, "partition": partition}

def bound_held_count(binding, checkpoint):
    """Recheck the published target-held partition against its saved probe."""
    import re

    from cre_daum_current_inventory import _path_key, _post, _sha

    probe = checkpoint.get("daum_fallback_probe")
    if not isinstance(binding, dict) or not isinstance(probe, dict):
        raise ValueError("DAUM fallback publication binding missing")
    partition = probe.get("partition")
    if (not isinstance(partition, list) or _sha(partition) != binding.get("fallback_partition_sha256")
            or probe.get("artifact_sha256") != binding.get("artifact_sha256")
            or probe.get("generation_id") != binding.get("generation_id")
            or probe.get("producer_sha") != binding.get("collector_git_sha")
            or probe.get("database_target_sha256") != binding.get("database_target_sha256")):
        raise ValueError("DAUM fallback publication binding differs")
    held = []
    seen, keys = set(), []
    for item in partition:
        if (not isinstance(item, dict) or set(item) != {"native_id", "external_id", "source_url", "hold_reason", "preimage"}
                or not isinstance(item.get("native_id"), str) or not isinstance(item.get("external_id"), str)
                or item["native_id"] in seen or item["external_id"] in keys
                or item["hold_reason"] not in (None, "identity_conflict", "existing_parent_unqualified",
                                               "source_index_unqualified", "orphan_source_index",
                                               "newer_target_observation", "existing_evidence_unqualified")
                or not isinstance(item.get("preimage"), str) or not re.fullmatch(r"[a-f0-9]{64}", item["preimage"])):
            raise ValueError("DAUM fallback publication partition invalid")
        _post(item["native_id"])
        if _path_key(item["source_url"]) != item["external_id"]:
            raise ValueError("DAUM fallback publication URL differs")
        seen.add(item["native_id"])
        keys.append(item["external_id"])
        if item["hold_reason"] is not None:
            held.append({"native_id": item["native_id"], "external_id": item["external_id"], "reason": item["hold_reason"]})
    if keys != sorted(keys) or held != binding.get("target_held"):
        raise ValueError("DAUM fallback held partition differs")
    source_emitted, emitted = binding.get("source_emitted_count"), binding.get("emitted_count")
    source_held, total_held = binding.get("source_held_count"), binding.get("held_count")
    if (any(type(value) is not int or value < 0 for value in (source_emitted, emitted, source_held, total_held))
            or emitted != source_emitted - len(held) or total_held != source_held + len(held)):
        raise ValueError("DAUM fallback publication counts differ")
    return len(held)
