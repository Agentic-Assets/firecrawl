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
  'listing',to_jsonb(own),'source_index',to_jsonb(si),'children',jsonb_build_object(
    'contacts',COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.id) FROM credeals.cre_listing_contacts c WHERE c.listing_id=own.id),'[]'::jsonb),
    'documents',COALESCE((SELECT jsonb_agg(to_jsonb(d) ORDER BY d.id) FROM credeals.cre_listing_documents d WHERE d.listing_id=own.id),'[]'::jsonb),
    'images',COALESCE((SELECT jsonb_agg(to_jsonb(i) ORDER BY i.id) FROM credeals.cre_listing_images i WHERE i.listing_id=own.id),'[]'::jsonb),
    'media',COALESCE((SELECT jsonb_agg(to_jsonb(m) ORDER BY m.id) FROM credeals.cre_listing_media m WHERE m.listing_id=own.id),'[]'::jsonb),
    'links',COALESCE((SELECT jsonb_agg(to_jsonb(l) ORDER BY l.id) FROM credeals.cre_listing_links l WHERE l.listing_id=own.id),'[]'::jsonb))) ORDER BY s.r->>'external_id')
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
            or any(not isinstance(row, dict) or set(row) != {"external_id", "listing", "source_index", "children"} for row in preimage)
            or {row["external_id"] for row in preimage} != ids):
        raise ValueError("invalid Marcus target partition")


def _validate_write(staged, held, preimage, receipt, job_id):
    import re
    import uuid

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
    if not isinstance(receipt.get("partition"), dict) or receipt["partition"].get("staged") != staged:
        raise ValueError("Marcus staged partition differs from source receipt")
    return generation, start, finish, str(uuid.UUID(job_id))


def build_partial_sql(staged, held, preimage, receipt, job_id):
    """Build additive SQL; caller authenticates source, owner and exact target."""
    from cre_ingest import lifecycle_transaction_lock_sql, sql_lit

    generation, start, finish, job = _validate_write(staged, held, preimage, receipt, job_id)
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


def partial_readback_sql(staged, held, preimage, receipt, job_id):
    """Compare the owned parent/index and five child collections after commit."""
    import copy
    from cre_ingest import sql_lit

    _generation, _start, _finish, job = _validate_write(staged, held, preimage, receipt, job_id)
    prior = {item["external_id"]: item for item in preimage}
    expected = []
    for row in staged:
        before = prior[row["external_id"]]
        entry = {"schema": "marcus-inventory-entry/v1", "generation": receipt["generation"],
                 "attempt": receipt["attempt"], "sourceSha256": receipt["source_sha256"],
                 "detailQualified": False, "recordBase64": row["observation_base64"]}
        previous = before["listing"]
        raw = None
        if row["external_id"] not in held:
            if previous is None:
                raw = {"sourceKey": "marcus-millichap", "id": row["external_id"]}
            else:
                raw = copy.deepcopy(previous["raw_data"])
            history = raw.get("marcusInventoryObservations")
            if history is None:
                history = {"schema": "marcus-inventory-history/v1", "sourceKey": "marcus-millichap",
                           "externalId": row["external_id"], "entries": []}
                raw["marcusInventoryObservations"] = history
            if entry not in history["entries"]:
                history["entries"].append(entry)
        expected.append({"r": row, "p": before, "held": row["external_id"] in held, "raw": raw})
    payload = _payload(expected)
    return f"""
WITH expected AS (SELECT value->'r' r,value->'p' p,(value->>'held')::boolean held,value->'raw' raw
                  FROM jsonb_array_elements({payload}::jsonb)),
observed AS (
 SELECT e.*,to_jsonb(t) listing,to_jsonb(si) source_index,jsonb_build_object(
  'contacts',COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.id) FROM credeals.cre_listing_contacts c WHERE c.listing_id=t.id),'[]'::jsonb),
  'documents',COALESCE((SELECT jsonb_agg(to_jsonb(d) ORDER BY d.id) FROM credeals.cre_listing_documents d WHERE d.listing_id=t.id),'[]'::jsonb),
  'images',COALESCE((SELECT jsonb_agg(to_jsonb(i) ORDER BY i.id) FROM credeals.cre_listing_images i WHERE i.listing_id=t.id),'[]'::jsonb),
  'media',COALESCE((SELECT jsonb_agg(to_jsonb(m) ORDER BY m.id) FROM credeals.cre_listing_media m WHERE m.listing_id=t.id),'[]'::jsonb),
  'links',COALESCE((SELECT jsonb_agg(to_jsonb(l) ORDER BY l.id) FROM credeals.cre_listing_links l WHERE l.listing_id=t.id),'[]'::jsonb)) children
 FROM expected e JOIN credeals.cre_brokerages b ON b.slug='marcus-millichap'
 LEFT JOIN credeals.cre_listings t ON t.brokerage_id=b.id AND t.external_id=e.r->>'external_id'
 LEFT JOIN credeals.cre_source_index si ON si.brokerage_id=b.id AND si.external_id=e.r->>'external_id'
), mismatch AS (
 SELECT 1 FROM observed WHERE source_index IS DISTINCT FROM NULLIF(p->'source_index','null'::jsonb)
 OR children IS DISTINCT FROM p->'children'
 OR (held AND listing IS DISTINCT FROM NULLIF(p->'listing','null'::jsonb))
 OR (NOT held AND (listing IS NULL OR listing->'raw_data' IS DISTINCT FROM raw
   OR (p->'listing'='null'::jsonb AND (
       listing->>'source_url' IS DISTINCT FROM r->>'source_url' OR listing->>'canonical_url' IS NOT NULL
       OR listing->>'status' IS NOT NULL OR listing->>'deleted_at' IS NOT NULL
       OR listing->>'transaction_type' IS DISTINCT FROM 'sale' OR listing->>'title' IS DISTINCT FROM r->>'title'
       OR listing->'lat' IS DISTINCT FROM r->'lat' OR listing->'lng' IS DISTINCT FROM r->'lng'))
   OR (p->'listing'<>'null'::jsonb AND
       (listing - ARRAY['updated_at','raw_data']) IS DISTINCT FROM ((p->'listing') - ARRAY['updated_at','raw_data']))))
)
SELECT (SELECT count(*) FROM credeals.cre_scrape_jobs j JOIN credeals.cre_brokerages b ON b.id=j.brokerage_id
 WHERE b.slug='marcus-millichap' AND j.id={sql_lit(job)}::uuid AND j.status='partial'
 AND j.listings_discovered={len(staged)} AND j.listings_scraped={len(staged)} AND j.listings_saved={len(staged)-len(held)}
 AND j.started_at={sql_lit(receipt['started_at'])}::timestamptz AND j.completed_at={sql_lit(receipt['finished_at'])}::timestamptz) AS jobs,
 (SELECT count(*) FROM observed) AS observed,(SELECT count(*) FROM mismatch) AS mismatches
"""
