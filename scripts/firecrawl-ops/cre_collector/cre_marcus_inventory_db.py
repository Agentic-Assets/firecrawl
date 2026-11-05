"""Read-only Marcus identity partition. This module grants no write authority."""
import base64
import json

from cre_marcus_inventory import stage_inventory


def validate_staged(staged, generation, started_at, finished_at):
    """Bind every derived column back to the complete retained observation."""
    if not isinstance(staged, list) or not staged:
        raise ValueError("empty Marcus inventory partition")
    identifiers, activities, urls = set(), set(), set()
    for row in staged:
        try:
            original = json.loads(base64.b64decode(row["observation_base64"], validate=True))
            derived = stage_inventory(original, generation, started_at, finished_at)
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise ValueError("invalid Marcus inventory observation") from exc
        if (row != derived or row["external_id"] in identifiers or row["activity_id"] in activities
                or row["source_url"].rstrip("/") in urls):
            raise ValueError("duplicate or inconsistent Marcus inventory")
        identifiers.add(row["external_id"])
        activities.add(row["activity_id"])
        urls.add(row["source_url"].rstrip("/"))


def identity_probe_sql(staged, generation, started_at, finished_at):
    """Build a read-only exact-ID/URL partition; caller binds target and owner.

    This is a preliminary probe, never an authorization to write. A writer must
    take the shared lifecycle lock, lock source-index then parent rows, repeat
    the partition and compare exact preimages in the transaction.
    """
    from cre_ingest import sql_lit

    validate_staged(staged, generation, started_at, finished_at)
    payload = sql_lit(json.dumps(staged, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False))
    return f"""
WITH candidate AS (SELECT value AS r FROM jsonb_array_elements({payload}::jsonb)),
scoped AS (SELECT b.id brokerage_id,c.r FROM candidate c
           JOIN credeals.cre_brokerages b ON b.slug='marcus-millichap'),
conflicts AS (
 SELECT s.r->>'external_id' external_id FROM scoped s
 LEFT JOIN credeals.cre_listings own ON own.brokerage_id=s.brokerage_id AND own.external_id=s.r->>'external_id'
 LEFT JOIN credeals.cre_source_index si ON si.brokerage_id=s.brokerage_id AND si.external_id=s.r->>'external_id'
 WHERE (own.id IS NOT NULL AND (rtrim(own.source_url,'/') IS DISTINCT FROM rtrim(s.r->>'source_url','/')
   OR jsonb_typeof(own.raw_data) IS DISTINCT FROM 'object'
   OR (own.raw_data ? 'marcusInventoryObservations' AND NOT COALESCE(
       jsonb_typeof(own.raw_data->'marcusInventoryObservations')='object'
       AND own.raw_data#>>'{{marcusInventoryObservations,sourceKey}}'='marcus-millichap'
       AND own.raw_data#>>'{{marcusInventoryObservations,externalId}}'=s.r->>'external_id'
       AND jsonb_typeof(own.raw_data#>'{{marcusInventoryObservations,entries}}')='array',false))))
 OR (si.id IS NOT NULL AND (si.source_key IS DISTINCT FROM 'marcus-millichap'
       OR rtrim(si.url,'/') IS DISTINCT FROM rtrim(s.r->>'source_url','/')))
 OR EXISTS (SELECT 1 FROM credeals.cre_listings other
   WHERE (own.id IS NULL OR other.id<>own.id)
   AND (rtrim(other.source_url,'/')=rtrim(s.r->>'source_url','/')
     OR rtrim(other.canonical_url,'/')=rtrim(s.r->>'source_url','/')))
 OR EXISTS (SELECT 1 FROM credeals.cre_source_index other
   WHERE (si.id IS NULL OR other.id<>si.id) AND rtrim(other.url,'/')=rtrim(s.r->>'source_url','/'))
)
SELECT (SELECT count(*) FROM candidate) AS staged,(SELECT count(*) FROM scoped) AS joined,
 COALESCE((SELECT json_agg(external_id ORDER BY external_id) FROM conflicts),'[]'::json)::text AS held,
 COALESCE((SELECT jsonb_agg(jsonb_build_object('external_id',s.r->>'external_id',
  'listing',to_jsonb(own),'source_index',to_jsonb(si)) ORDER BY s.r->>'external_id')
 FROM scoped s LEFT JOIN credeals.cre_listings own ON own.brokerage_id=s.brokerage_id
    AND own.external_id=s.r->>'external_id'
 LEFT JOIN credeals.cre_source_index si ON si.brokerage_id=s.brokerage_id
    AND si.external_id=s.r->>'external_id'),'[]'::jsonb)::text AS preimage
"""
