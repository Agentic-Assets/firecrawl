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


def _payload(value):
    from cre_ingest import sql_lit
    return sql_lit(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False))


def _validate_partition(staged, held, preimage, generation, started_at, finished_at):
    validate_staged(staged, generation, started_at, finished_at)
    ids = {row["external_id"] for row in staged}
    if (not isinstance(held, list) or held != sorted(set(held)) or not set(held) <= ids
            or not isinstance(preimage, list) or len(preimage) != len(ids)
            or any(not isinstance(row, dict) or set(row) != {"external_id", "listing", "source_index"} for row in preimage)
            or {row["external_id"] for row in preimage} != ids):
        raise ValueError("invalid Marcus target partition")


def build_partial_sql(staged, held, preimage, receipt, job_id):
    """Build an additive transaction; caller authenticates receipt and exact target.

    This is not a DB connection or a recovery decision. Unknown commit outcomes
    remain fenced by the owning checkpoint until exact readback is available.
    """
    import re
    import uuid
    from cre_ingest import lifecycle_transaction_lock_sql, sql_lit

    if (not isinstance(receipt, dict) or receipt.get("schema") != "marcus-inventory-custody/v1"
            or receipt.get("source") != "marcus-millichap" or receipt.get("coverage") != "partial"
            or type(receipt.get("detail_qualified")) is not int or receipt["detail_qualified"] != 0
            or type(receipt.get("detail_errors")) is not int or receipt["detail_errors"] < 1
            or type(receipt.get("attempt")) is not int
            or receipt["attempt"] < 1 or not isinstance(receipt.get("source_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", receipt["source_sha256"])):
        raise ValueError("invalid Marcus source receipt")
    generation, start, finish = receipt.get("generation"), receipt.get("started_at"), receipt.get("finished_at")
    _validate_partition(staged, held, preimage, generation, start, finish)
    if receipt.get("partition", {}).get("staged") != staged:
        raise ValueError("Marcus staged partition differs from source receipt")
    job = str(uuid.UUID(job_id))
    payload, held_payload, prior = _payload(staged), _payload(held), _payload(preimage)
    probe = identity_probe_sql(staged, generation, start, finish)
    entry = "jsonb_build_object('schema','marcus-inventory-entry/v1','generation'," + sql_lit(generation) + \
            ",'attempt'," + str(receipt["attempt"]) + ",'sourceSha256'," + sql_lit(receipt["source_sha256"]) + \
            ",'detailQualified',false,'recordBase64',r->>'observation_base64')"
    return f"""
BEGIN;
{lifecycle_transaction_lock_sql()}
DO $$ BEGIN
 PERFORM si.id FROM credeals.cre_source_index si WHERE EXISTS
  (SELECT 1 FROM jsonb_array_elements({payload}::jsonb) c(r)
   WHERE (si.brokerage_id=(SELECT id FROM credeals.cre_brokerages WHERE slug='marcus-millichap')
          AND si.external_id=c.r->>'external_id') OR rtrim(si.url,'/')=rtrim(c.r->>'source_url','/'))
 ORDER BY si.id FOR UPDATE;
 PERFORM t.id FROM credeals.cre_listings t WHERE EXISTS
  (SELECT 1 FROM jsonb_array_elements({payload}::jsonb) c(r)
   WHERE (t.brokerage_id=(SELECT id FROM credeals.cre_brokerages WHERE slug='marcus-millichap')
          AND t.external_id=c.r->>'external_id') OR rtrim(t.source_url,'/')=rtrim(c.r->>'source_url','/')
          OR rtrim(t.canonical_url,'/')=rtrim(c.r->>'source_url','/'))
 ORDER BY t.id FOR UPDATE;
END $$;
CREATE TEMP TABLE _marcus_probe ON COMMIT DROP AS {probe};
DO $$ BEGIN
 IF (SELECT staged FROM _marcus_probe)<>{len(staged)} OR (SELECT joined FROM _marcus_probe)<>{len(staged)}
 OR (SELECT held::jsonb FROM _marcus_probe)<>{held_payload}::jsonb
 OR (SELECT preimage::jsonb FROM _marcus_probe)<>{prior}::jsonb
 THEN RAISE EXCEPTION 'Marcus identity partition changed'; END IF;
END $$;
CREATE TEMP TABLE _marcus_rows ON COMMIT DROP AS
 SELECT b.id brokerage_id,value r FROM jsonb_array_elements({payload}::jsonb)
 JOIN credeals.cre_brokerages b ON b.slug='marcus-millichap'
 WHERE NOT (value->>'external_id' IN (SELECT jsonb_array_elements_text({held_payload}::jsonb)));
INSERT INTO credeals.cre_scrape_jobs
 (id,brokerage_id,status,started_at,completed_at,listings_discovered,listings_scraped,listings_saved,errors_count,notes)
SELECT {sql_lit(job)}::uuid,id,'partial',{sql_lit(start)}::timestamptz,{sql_lit(finish)}::timestamptz,
 {len(staged)},{len(staged)},{len(staged)-len(held)},{receipt['detail_errors']},
 'Marcus inventory observations; details unverified; coverage partial'
FROM credeals.cre_brokerages WHERE slug='marcus-millichap';
UPDATE credeals.cre_listings t SET raw_data=jsonb_set(t.raw_data,'{{marcusInventoryObservations}}',
 CASE WHEN t.raw_data ? 'marcusInventoryObservations' THEN
   CASE WHEN EXISTS (SELECT 1 FROM jsonb_array_elements(t.raw_data#>'{{marcusInventoryObservations,entries}}') old(value)
       WHERE old.value = {entry}) THEN t.raw_data->'marcusInventoryObservations'
   ELSE jsonb_set(t.raw_data->'marcusInventoryObservations','{{entries}}',
       (t.raw_data#>'{{marcusInventoryObservations,entries}}') || jsonb_build_array({entry})) END
 ELSE jsonb_build_object('schema','marcus-inventory-history/v1','sourceKey','marcus-millichap',
       'externalId',r->>'external_id','entries',jsonb_build_array({entry})) END)
FROM _marcus_rows s WHERE t.brokerage_id=s.brokerage_id AND t.external_id=s.r->>'external_id';
INSERT INTO credeals.cre_listings
 (brokerage_id,external_id,source_url,canonical_url,status,transaction_type,title,lat,lng,raw_data)
SELECT brokerage_id,r->>'external_id',r->>'source_url',NULL,NULL,'sale',r->>'title',
 (r->>'lat')::double precision,(r->>'lng')::double precision,
 jsonb_build_object('sourceKey','marcus-millichap','id',r->>'external_id','marcusInventoryObservations',
  jsonb_build_object('schema','marcus-inventory-history/v1','sourceKey','marcus-millichap',
   'externalId',r->>'external_id','entries',jsonb_build_array({entry})))
FROM _marcus_rows s WHERE NOT EXISTS (SELECT 1 FROM credeals.cre_listings t
 WHERE t.brokerage_id=s.brokerage_id AND t.external_id=s.r->>'external_id');
COMMIT;
"""
