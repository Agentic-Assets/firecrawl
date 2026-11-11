"""Source-qualified Buildout observation SQL and transaction readback."""

import json
import re
from uuid import UUID, uuid5

from source_fields.buildout import (
    classify_prior_inventory_presence,
    qualify_prior_inventory_binding,
)


def _literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _uuid(value):
    parsed = UUID(str(value))
    return _literal(str(parsed)) + "::uuid"


def _current_raw_sql(value):
    """Select the explicitly current inventory, even if it is malformed."""
    return (f"(CASE WHEN jsonb_typeof({value})='object' "
            f"AND {value} ? 'latestInventoryObservation' "
            f"THEN {value}->'latestInventoryObservation' ELSE {value} END)")


def _canonical_applied(items):
    """Compare complete identities without depending on database collation."""
    if not isinstance(items, list):
        return None
    result = []
    seen = set()
    for item in items:
        if (not isinstance(item, dict) or set(item) != {"listing_id", "external_id", "native_ids"}
                or not isinstance(item["listing_id"], str)
                or not isinstance(item["external_id"], str) or not item["external_id"]
                or item["external_id"] in seen
                or not isinstance(item["native_ids"], list) or not item["native_ids"]
                or any(not isinstance(n, str) for n in item["native_ids"])
                or len(item["native_ids"]) != len(set(item["native_ids"]))):
            return None
        seen.add(item["external_id"])
        result.append((item["external_id"], item["listing_id"], tuple(sorted(item["native_ids"]))))
    return sorted(result)


def _identity_partition_queries(sources):
    """Return shared source-bound identity queries for writer and read-only probe."""
    from source_fields.buildout import observation_sources

    sources = frozenset(sources)
    if not sources or not sources <= observation_sources():
        raise ValueError("invalid Buildout observation sources")
    source = ", ".join(_literal(item) for item in sorted(sources))
    claims = f"""
-- A historical ID is collision evidence, not a current identity. Include
-- plausible claims even when the latest observation is missing or malformed.
SELECT l.brokerage_id, l.external_id, claim.value #>> '{{}}' AS native_id
FROM credeals.cre_listings l
JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id AND b.slug IN ({source})
CROSS JOIN LATERAL jsonb_path_query(l.raw_data, '$.**.id') AS claim(value)
UNION
SELECT s.brokerage_id, s.external_id, claim.value #>> '{{}}' AS native_id
FROM _src s CROSS JOIN LATERAL jsonb_path_query(s.raw_data, '$.**.id') AS claim(value)
WHERE s.slug IN ({source})
"""
    shapes = f"""
-- Qualify each whole source-native shape before comparing it. A canonical
-- property may have one flat observation or two merged sale/lease members.
SELECT s.brokerage_id, s.external_id, input.kind,
       array_agg(member.value->>'id' ORDER BY member.value->>'id') AS native_ids
FROM _src s JOIN credeals.cre_listings l USING (brokerage_id, external_id)
CROSS JOIN LATERAL (VALUES ('prior', {_current_raw_sql('l.raw_data')}, l.source_url),
                           ('incoming', {_current_raw_sql('s.raw_data')}, s.source_url)) AS input(kind, raw, url)
CROSS JOIN LATERAL jsonb_array_elements(CASE
  WHEN jsonb_typeof(input.raw) <> 'object' THEN '[]'::jsonb
  WHEN input.raw ? 'latestInventoryObservation' THEN '[]'::jsonb
  WHEN input.raw ? 'primary' AND input.raw ? 'secondary_pass'
       AND (input.raw - 'primary' - 'secondary_pass') = '{{}}'::jsonb
    THEN jsonb_build_array(input.raw->'primary', input.raw->'secondary_pass')
  WHEN NOT (input.raw ? 'primary') AND NOT (input.raw ? 'secondary_pass')
    THEN jsonb_build_array(input.raw)
  ELSE '[]'::jsonb END) AS member(value)
WHERE s.slug IN ({source})
GROUP BY s.brokerage_id, s.external_id, s.slug, input.kind, input.url
HAVING bool_and(COALESCE(jsonb_typeof(member.value)='object'
       AND member.value->>'sourceKey'=s.slug
       AND jsonb_typeof(member.value->'id') IN ('number','string')
       AND CASE WHEN member.value->>'id' ~ '^[1-9][0-9]{{0,15}}$'
                THEN (member.value->>'id')::numeric <= 9007199254740991 ELSE false END
       AND NULLIF(member.value->>'url','') IS NOT NULL, false))
   AND bool_or(member.value->>'url'=input.url)
   AND count(DISTINCT member.value->>'id')=count(*)
"""
    held = f"""
-- Include newly staged keys: they may conflict with a historical parent or
-- another newly staged parent even though they do not yet join cre_listings.
SELECT DISTINCT s.brokerage_id, s.external_id
FROM _src s
JOIN _buildout_identity_claims own
  ON own.brokerage_id=s.brokerage_id AND own.external_id=s.external_id
JOIN _buildout_identity_claims other
  ON other.brokerage_id=own.brokerage_id AND other.native_id=own.native_id
 AND other.external_id<>own.external_id
WHERE s.slug IN ({source}) AND own.native_id IS NOT NULL
UNION
SELECT s.brokerage_id, s.external_id
FROM _src s
JOIN credeals.cre_listings l USING (brokerage_id, external_id)
LEFT JOIN credeals.cre_source_index si USING (brokerage_id, external_id)
WHERE s.slug IN ({source}) AND NOT COALESCE(
  si.source_key = s.slug AND si.url = l.source_url
  AND s.source_url = l.source_url
  AND (si.last_enumerated_at IS NULL OR si.last_enumerated_at <= s.scraped_at)
  AND EXISTS (SELECT 1 FROM _buildout_identity_shapes prior
              JOIN _buildout_identity_shapes incoming USING (brokerage_id, external_id)
              WHERE prior.brokerage_id=s.brokerage_id AND prior.external_id=s.external_id
                AND prior.kind='prior' AND incoming.kind='incoming'
                AND (prior.native_ids @> incoming.native_ids
                     OR prior.native_ids <@ incoming.native_ids)),
  false)
"""
    return claims, shapes, held


def identity_partition_sql(sources):
    """Classify staged identities in transaction-local tables without deleting rows."""
    claims, shapes, held = _identity_partition_queries(sources)
    return f"""
CREATE TEMP TABLE _buildout_identity_claims ON COMMIT DROP AS {claims};
CREATE INDEX ON _buildout_identity_claims (brokerage_id, native_id);
CREATE TEMP TABLE _buildout_identity_shapes ON COMMIT DROP AS {shapes};
CREATE TEMP TABLE _buildout_identity_held ON COMMIT DROP AS {held};
"""


def identity_partition_ctes(sources):
    """Render the same identity predicates as read-only materialized CTEs."""
    claims, shapes, held = _identity_partition_queries(sources)
    return (f"_buildout_identity_claims AS MATERIALIZED ({claims}),\n"
            f"_buildout_identity_shapes AS MATERIALIZED ({shapes}),\n"
            f"_buildout_identity_held AS MATERIALIZED ({held})")


def identity_guard_sql(sources, *, reject_held=False):
    """Hold ambiguous existing identities before the ingest replaces them."""
    sources = frozenset(sources)
    partition = identity_partition_sql(sources)
    source = ", ".join(_literal(item) for item in sorted(sources))
    refusal = ("DO $$ BEGIN IF EXISTS (SELECT 1 FROM _buildout_identity_held) THEN "
               "RAISE EXCEPTION 'Buildout identity ambiguity requires source hold'; "
               "END IF; END $$;" if reject_held else "")
    return f"""
-- Lock in lifecycle order before reading prior native identity.
DO $$ BEGIN
PERFORM si.id FROM credeals.cre_source_index si JOIN _src s USING (brokerage_id, external_id)
WHERE s.slug IN ({source}) ORDER BY si.id FOR UPDATE OF si;
PERFORM l.id FROM credeals.cre_listings l JOIN _src s USING (brokerage_id, external_id)
WHERE s.slug IN ({source}) ORDER BY l.id FOR UPDATE OF l;
END $$;
{partition}
{refusal}
DELETE FROM _src s USING _buildout_identity_held h
WHERE s.brokerage_id = h.brokerage_id AND s.external_id = h.external_id;
SELECT 'buildout_identity_held=' || count(*)::text FROM _buildout_identity_held;
"""


def readback(scope, env_file):
    """Read the artifact-keyed assessment and its events on the bound target."""
    from cre_ingest import (
        _RUN_UUID_NAMESPACE,
        artifact_run_identity_from_digests,
        database_target_fingerprint_from_url,
        find_psql,
        load_db_url,
    )
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    target = scope["database_target_sha256"]
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != target:
        raise ValueError("observation readback target changed")
    key, _ = artifact_run_identity_from_digests([scope["artifact_sha256"]])
    job = str(uuid5(_RUN_UUID_NAMESPACE, f"{key}:{scope['source_key']}"))
    source_value = "not_observed_in_complete_enumeration:" + scope["source_key"] + ":" + scope["generation_id"]
    sql = f"""BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
{PIN_STRING_LITERALS}
SELECT jsonb_build_object(
 'notes', j.notes,
 'events', (SELECT count(*) FROM credeals.cre_listing_events e
            WHERE e.scrape_job_id=j.id AND e.event_type='disappeared'),
 'applied', COALESCE((SELECT jsonb_agg(jsonb_build_object(
                'listing_id', l.id::text, 'external_id', l.external_id,
                'native_ids', (SELECT jsonb_agg(member.value->>'id' ORDER BY member.value->>'id')
                  FROM jsonb_array_elements(CASE WHEN {_current_raw_sql('l.raw_data')} ? 'primary'
                    THEN jsonb_build_array({_current_raw_sql('l.raw_data')}->'primary', {_current_raw_sql('l.raw_data')}->'secondary_pass')
                    ELSE jsonb_build_array({_current_raw_sql('l.raw_data')}) END) member(value))) ORDER BY l.external_id)
              FROM credeals.cre_listing_events e
              JOIN credeals.cre_listings l ON l.id=e.listing_id AND l.brokerage_id=e.brokerage_id
              JOIN credeals.cre_source_index si
                ON si.brokerage_id=l.brokerage_id AND si.external_id=l.external_id
              WHERE e.scrape_job_id=j.id AND e.event_type='disappeared'
                AND e.source_value={_literal(source_value)}
                AND si.source_key={_literal(scope['source_key'])}
                AND si.url=e.source_url AND l.source_url=e.source_url
                AND si.observation_present IS FALSE
                AND si.presence_generation=e.presence_generation), '[]'::jsonb)) AS result
FROM credeals.cre_scrape_jobs j
JOIN credeals.cre_brokerages b ON b.id=j.brokerage_id
WHERE j.id={_uuid(job)} AND j.artifact_run_key={_literal(key)}
  AND b.slug={_literal(scope['source_key'])};
ROLLBACK;"""
    result = _run_psql_script(find_psql(), db_url, sql, timeout=45)
    if result.returncode:
        raise ValueError("observation readback query failed")
    rows = parse_tsv(result.stdout)
    if len(rows) != 1 or set(rows[0]) != {"result"}:
        raise ValueError("observation job is missing or ambiguous")
    value = json.loads(rows[0]["result"])
    marker = "\nCRE_BUILDOUT_OBSERVATION_V1:"
    notes = value.get("notes")
    if not isinstance(notes, str) or notes.count(marker) != 1:
        raise ValueError("observation receipt is missing or ambiguous")
    receipt = json.loads(notes.split(marker, 1)[1])
    ids = receipt.get("held_external_ids")
    applied = receipt.get("applied")
    # PostgreSQL aggregate ordering follows the database collation, which can
    # differ from Python's ordering or change between a receipt and readback.
    if (receipt.get("source") != scope["source_key"]
            or receipt.get("generation") != scope["generation_id"]
            or receipt.get("artifact_sha256") != scope["artifact_sha256"]
            or type(receipt.get("disappeared")) is not int
            or type(receipt.get("identity_held")) is not int
            or not isinstance(ids, list) or any(not isinstance(x, str) or not x for x in ids)
            or len(ids) != len(set(ids)) or len(ids) != receipt["identity_held"]
            or not isinstance(applied, list) or len(applied) != receipt["disappeared"]
            or any(not isinstance(item, dict) or set(item) != {"listing_id", "external_id", "native_ids"}
                   or not isinstance(item["listing_id"], str) or not isinstance(item["external_id"], str)
                   or not item["external_id"] or not isinstance(item["native_ids"], list)
                   or not item["native_ids"] or any(not isinstance(n, str) for n in item["native_ids"])
                   or len(item["native_ids"]) != len(set(item["native_ids"]))
                   or set(item["native_ids"]) & set(scope["native_ids"]) for item in applied)
            or len({item["external_id"] for item in applied}) != len(applied)
            or receipt["disappeared"] < 0
            or value.get("events") != receipt["disappeared"]
            or _canonical_applied(value.get("applied")) != _canonical_applied(applied)):
        raise ValueError("observation receipt and event readback differ")
    current, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(current).get("value") != target:
        raise ValueError("observation readback target changed")
    return receipt


def sql_fragment(scope, prior, job_id, artifact_run_key):
    """Generate a draft fragment; it neither begins nor commits a transaction.

    Prior entries must already be source-qualified preimages. The transaction
    locks and compares each preimage again, holding changed records locally.
    A caller must independently verify current owner, target and sealed proof.
    """
    from cre_ingest import (
        SOURCE_KEYS_BY_SLUG,
        SOURCE_TO_BROKERAGE,
        artifact_run_identity_from_digests,
    )

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
-- Include older or malformed observations as collision evidence without using
-- them to establish a current identity or a disappearance.
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
CROSS JOIN LATERAL (SELECT {_current_raw_sql('l.raw_data')} AS raw) AS current_observation
CROSS JOIN LATERAL jsonb_array_elements(CASE
 WHEN jsonb_typeof(current_observation.raw) <> 'object' THEN '[]'::jsonb
 WHEN current_observation.raw ? 'latestInventoryObservation' THEN '[]'::jsonb
 WHEN current_observation.raw ? 'primary' AND current_observation.raw ? 'secondary_pass'
      AND (current_observation.raw - 'primary' - 'secondary_pass') = '{{}}'::jsonb
   THEN jsonb_build_array(current_observation.raw->'primary', current_observation.raw->'secondary_pass')
 WHEN NOT (current_observation.raw ? 'primary') AND NOT (current_observation.raw ? 'secondary_pass')
   THEN jsonb_build_array(current_observation.raw)
 ELSE '[]'::jsonb END) AS member(value)
WHERE b.slug={slug_sql} AND si.source_key={source_sql} AND si.url=l.source_url
  AND si.observation_present IS TRUE AND si.last_enumerated_at IS NOT NULL
GROUP BY l.id, l.brokerage_id, l.external_id, l.source_url, l.raw_data,
         si.presence_generation, si.last_enumerated_at
HAVING bool_and(COALESCE(jsonb_typeof(member.value)='object'
       AND member.value->>'sourceKey'={source_sql}
       AND jsonb_typeof(member.value->'id') IN ('number','string')
       AND CASE WHEN member.value->>'id' ~ '^[1-9][0-9]{{0,15}}$'
                 THEN (member.value->>'id')::numeric <= 9007199254740991 ELSE false END
       AND NULLIF(member.value->>'url','') IS NOT NULL, false))
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
   -- An unresolved staged identity makes this source inventory ambiguous.
   -- No absence from this enumeration is authoritative until it is resolved.
   AND NOT EXISTS (SELECT 1 FROM _buildout_identity_held h
                   WHERE h.brokerage_id=c.brokerage_id)
   -- An artifact-keyed receipt freezes its absence outcome on replay.
   AND position(E'\\nCRE_BUILDOUT_OBSERVATION_V1:' in COALESCE(j.notes, '')) = 0
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
-- This source-qualified transaction receipt also records a zero-event result.
-- Preserve the first receipt on an exact artifact replay.
UPDATE credeals.cre_scrape_jobs j
SET notes = COALESCE(j.notes, '') || E'\\nCRE_BUILDOUT_OBSERVATION_V1:' ||
  jsonb_build_object('source', {source_sql}, 'generation', {generation_sql},
    'artifact_sha256', {digest_sql},
    'disappeared', (SELECT count(*) FROM _buildout_observation_applied),
    'identity_held', (SELECT count(*) FROM _buildout_identity_held),
    'held_external_ids', COALESCE((SELECT jsonb_agg(external_id ORDER BY external_id)
                                   FROM _buildout_identity_held), '[]'::jsonb),
    'applied', COALESCE((SELECT jsonb_agg(jsonb_build_object(
      'listing_id', a.listing_id::text, 'external_id', c.external_id,
      'native_ids', (SELECT jsonb_agg(member.value->>'id' ORDER BY member.value->>'id')
        FROM jsonb_array_elements(CASE WHEN {_current_raw_sql('c.raw_data')} ? 'primary'
          THEN jsonb_build_array({_current_raw_sql('c.raw_data')}->'primary', {_current_raw_sql('c.raw_data')}->'secondary_pass')
          ELSE jsonb_build_array({_current_raw_sql('c.raw_data')}) END) member(value))) ORDER BY c.external_id)
      FROM _buildout_observation_applied a
      JOIN _buildout_observation_candidate c ON c.listing_id=a.listing_id), '[]'::jsonb))::text
WHERE j.id={job_sql} AND j.artifact_run_key={job_key_sql}
  AND position(E'\\nCRE_BUILDOUT_OBSERVATION_V1:' in COALESCE(j.notes, '')) = 0;
"""
