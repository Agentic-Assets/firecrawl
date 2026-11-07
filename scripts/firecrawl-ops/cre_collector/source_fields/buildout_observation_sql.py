"""WIP SQL fragment for source-qualified, observe-only Buildout absence.

Not connected to ingestion. The owner must bind the source scope and actual
PostgreSQL target, and call this only inside the normal ingest transaction.
"""

import json
import re
from uuid import UUID

from source_fields.buildout import classify_prior_inventory_presence, qualify_prior_inventory_binding


def _literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _uuid(value):
    parsed = UUID(str(value))
    return _literal(str(parsed)) + "::uuid"


def sql_fragment(scope, prior, job_id, artifact_run_key):
    """Generate a draft fragment; it neither begins nor commits a transaction.

    Prior entries must already be source-qualified preimages. The transaction
    locks and compares each preimage again, holding changed records locally.
    A caller must independently verify current owner, target and sealed proof.
    """
    from cre_ingest import SOURCE_KEYS_BY_SLUG, SOURCE_TO_BROKERAGE, artifact_run_identity_from_digests

    source = scope["source_key"]
    generation = scope["generation_id"]
    observed_at = scope["observed_at"]
    digest = scope["artifact_sha256"]
    native = scope["native_ids"]
    if (not isinstance(source, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", source)
            or not isinstance(generation, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", generation)
            or not isinstance(observed_at, str)
            or not re.fullmatch(r"[0-9TtZz:+.\-]{20,40}", observed_at)
            or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or not isinstance(native, list) or not native
            or not isinstance(artifact_run_key, str) or not artifact_run_key):
        raise ValueError("invalid observation scope")
    mapping = SOURCE_TO_BROKERAGE.get(source)
    if (mapping is None or SOURCE_KEYS_BY_SLUG.get(mapping[0]) != {source}
            or artifact_run_identity_from_digests([digest])[0] != artifact_run_key):
        raise ValueError("job is not bound to source artifact")
    slug_sql = _literal(mapping[0])
    validated = {"pages": [{"rows": [{"id": value} for value in native]}]}
    qualified = []
    for row in prior or []:
        if not isinstance(row, dict):
            continue
        listing = {"id": row.get("listing_id"), "brokerage_id": row.get("brokerage_id"),
                   "external_id": row.get("external_id"), "source_url": row.get("source_url"),
                   "raw_data": row.get("raw_data")}
        index = {"brokerage_id": row.get("brokerage_id"), "external_id": row.get("external_id"),
                 "source_key": row.get("source_key"), "url": row.get("source_url"),
                 "observation_present": row.get("observation_present"),
                 "presence_generation": row.get("presence_generation"),
                 "last_enumerated_at": row.get("last_enumerated_at")}
        actual = qualify_prior_inventory_binding(source, listing, index)
        if actual is not None and actual.get("native_ids") == row.get("native_ids"):
            qualified.append(actual)
    candidates = classify_prior_inventory_presence(validated, qualified, source)["not_observed"]
    rows = []
    for row in candidates:
        if (not isinstance(row.get("source_url"), str)
                or not isinstance(row.get("raw_data"), dict)
                or not isinstance(row.get("last_enumerated_at"), str)
                or not re.fullmatch(r"[0-9TtZz:+.\-]{20,40}", row["last_enumerated_at"])):
            continue
        try:
            listing_id, brokerage_id = _uuid(row["listing_id"]), _uuid(row["brokerage_id"])
            raw_data = json.dumps(row["raw_data"], ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError, OverflowError):
            continue
        rows.append("(" + ",".join((
            listing_id, brokerage_id, _literal(row["external_id"]), _literal(row["source_url"]),
            _literal(raw_data) + "::jsonb", str(row["presence_generation"]),
            _literal(row["last_enumerated_at"]) + "::timestamptz",
        )) + ")")
    values = ",\n".join(rows)
    insert = ("INSERT INTO _buildout_observation_candidate VALUES " + values + ";") if rows else ""
    source_sql, generation_sql = _literal(source), _literal(generation)
    if prior is None:
        current_values = ",".join("(" + _literal(value) + ")" for value in native)
        insert = f"""
CREATE TEMP TABLE _buildout_current_native (native_id text PRIMARY KEY) ON COMMIT DROP;
INSERT INTO _buildout_current_native VALUES {current_values};
-- Preserve any plausible historical native claim, including unqualified raw
-- shapes, so conflicting parents hold locally rather than being guessed away.
CREATE TEMP TABLE _buildout_possible_native ON COMMIT DROP AS
SELECT DISTINCT l.id AS listing_id, item.value #>> '{{}}' AS native_id
FROM credeals.cre_listings l
JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id
CROSS JOIN LATERAL jsonb_path_query(l.raw_data, '$.**.id') AS item(value)
WHERE b.slug={slug_sql}
  AND jsonb_typeof(item.value) IN ('number','string')
  AND (item.value #>> '{{}}') ~ '^[1-9][0-9]{{0,15}}$';
CREATE INDEX ON _buildout_possible_native (native_id);
CREATE TEMP TABLE _buildout_qualified_prior ON COMMIT DROP AS
SELECT l.id AS listing_id, l.brokerage_id, l.external_id, l.source_url, l.raw_data,
       si.presence_generation, si.last_enumerated_at,
       array_agg(member.value->>'id') AS native_ids
FROM credeals.cre_listings l
JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id
JOIN credeals.cre_source_index si ON si.brokerage_id=l.brokerage_id AND si.external_id=l.external_id
CROSS JOIN LATERAL jsonb_array_elements(CASE
 WHEN jsonb_typeof(l.raw_data) <> 'object' THEN '[]'::jsonb
 WHEN l.raw_data ? 'primary' AND l.raw_data ? 'secondary_pass'
      AND (l.raw_data - 'primary' - 'secondary_pass') = '{{}}'::jsonb
   THEN jsonb_build_array(l.raw_data->'primary', l.raw_data->'secondary_pass')
 WHEN NOT (l.raw_data ? 'primary') AND NOT (l.raw_data ? 'secondary_pass')
   THEN jsonb_build_array(l.raw_data)
 ELSE '[]'::jsonb END) AS member(value)
WHERE b.slug={slug_sql} AND si.source_key={source_sql} AND si.url=l.source_url
  AND si.observation_present IS TRUE AND si.last_enumerated_at IS NOT NULL
GROUP BY l.id, l.brokerage_id, l.external_id, l.source_url, l.raw_data,
         si.presence_generation, si.last_enumerated_at
HAVING bool_and(jsonb_typeof(member.value)='object'
       AND member.value->>'sourceKey'={source_sql}
       AND jsonb_typeof(member.value->'id') IN ('number','string')
       AND CASE WHEN member.value->>'id' ~ '^[1-9][0-9]{{0,15}}$'
                 THEN (member.value->>'id')::numeric <= 9007199254740991 ELSE false END
       AND NULLIF(member.value->>'url','') IS NOT NULL)
   AND bool_or(member.value->>'url'=l.source_url)
   AND count(DISTINCT member.value->>'id')=count(*);
INSERT INTO _buildout_observation_candidate
SELECT q.listing_id,q.brokerage_id,q.external_id,q.source_url,q.raw_data,
       q.presence_generation,q.last_enumerated_at
FROM _buildout_qualified_prior q
WHERE NOT EXISTS (SELECT 1 FROM unnest(q.native_ids) n(native_id)
                  JOIN _buildout_current_native current USING (native_id))
  AND NOT EXISTS (SELECT 1 FROM unnest(q.native_ids) n(native_id)
                  JOIN _buildout_possible_native other USING (native_id)
                  WHERE other.listing_id <> q.listing_id);
"""
    time_sql, digest_sql = _literal(observed_at) + "::timestamptz", _literal(digest)
    job_sql, job_key_sql = _uuid(job_id), _literal(artifact_run_key)
    event_value = _literal("not_observed_in_complete_enumeration:" + source + ":" + generation)
    return f"""
-- Caller must hold the lifecycle advisory lock and pin standard_conforming_strings.
CREATE TEMP TABLE _buildout_observation_candidate (
 listing_id uuid, brokerage_id uuid, external_id text, source_url text,
 raw_data jsonb, presence_generation bigint, last_enumerated_at timestamptz
) ON COMMIT DROP;
{insert}
-- Exact artifact-keyed job is the current source-scope receipt. Restrict this
-- path to a brokerage with exactly one source, so later brokerage jobs are a
-- conservative source watermark rather than another feed's claim.
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM credeals.cre_scrape_jobs j
   JOIN credeals.cre_brokerages b ON b.id=j.brokerage_id
   WHERE j.id={job_sql} AND j.artifact_run_key={job_key_sql}
     AND j.completed_at={time_sql} AND b.slug={slug_sql}) THEN
   RAISE EXCEPTION 'source observation job does not match artifact, time and brokerage';
 END IF;
 IF EXISTS (SELECT 1 FROM credeals.cre_scrape_jobs later
   JOIN credeals.cre_brokerages b ON b.id=later.brokerage_id
   WHERE b.slug={slug_sql} AND later.id <> {job_sql}
     AND later.completed_at >= {time_sql}) THEN
   RAISE EXCEPTION 'newer or concurrent brokerage job prevents source observation';
 END IF;
 IF EXISTS (SELECT 1 FROM credeals.cre_source_index si
   JOIN credeals.cre_brokerages b ON b.id=si.brokerage_id
   WHERE b.slug={slug_sql} AND si.source_key={source_sql}
     AND si.last_enumerated_at > {time_sql}) THEN
   RAISE EXCEPTION 'newer source-index observation prevents source observation';
 END IF;
END $$;-- Source-index then listing locks follow the existing lifecycle lock order.
SELECT si.id FROM credeals.cre_source_index si
 JOIN _buildout_observation_candidate c USING (brokerage_id, external_id)
 ORDER BY si.id FOR UPDATE OF si;
SELECT l.id FROM credeals.cre_listings l
 JOIN _buildout_observation_candidate c ON c.listing_id=l.id
 ORDER BY l.id FOR UPDATE OF l;
CREATE TEMP TABLE _buildout_observation_applied ON COMMIT DROP AS
WITH changed AS (
 UPDATE credeals.cre_source_index si
 SET observation_present=false, presence_generation=si.presence_generation+1,
     presence_changed_at={time_sql}, last_enumerated_at={time_sql}
 FROM _buildout_observation_candidate c
 JOIN credeals.cre_listings l ON l.id=c.listing_id
 JOIN credeals.cre_scrape_jobs j ON j.id={job_sql} AND j.brokerage_id=c.brokerage_id
  AND j.artifact_run_key={job_key_sql}
 WHERE si.brokerage_id=c.brokerage_id AND si.external_id=c.external_id
   AND si.source_key={source_sql} AND si.url=c.source_url
   AND si.observation_present IS TRUE AND si.presence_generation=c.presence_generation
   AND si.last_enumerated_at=c.last_enumerated_at AND si.last_enumerated_at < {time_sql}
   AND l.brokerage_id=c.brokerage_id AND l.external_id=c.external_id
   AND l.source_url=c.source_url AND l.raw_data=c.raw_data
 RETURNING c.listing_id, c.brokerage_id, c.source_url, si.presence_generation
) SELECT * FROM changed;
INSERT INTO credeals.cre_listing_events
 (listing_id, brokerage_id, scrape_job_id, event_type, source_value,
  source_url, presence_generation, detected_at)
SELECT listing_id, brokerage_id, {job_sql}, 'disappeared', {event_value},
 source_url, presence_generation, {time_sql}
FROM _buildout_observation_applied ON CONFLICT DO NOTHING;
"""
