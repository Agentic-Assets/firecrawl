"""Bounded Lyon search records; caller authenticates sealed source snapshots."""

import base64
import json
import math
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit


def stage_record(record):
    """Validate identity and retain complete source occurrences without inference.

    The caller must reparse and hash-bind each occurrence to its sealed snapshot;
    this function cannot authenticate the snapshot by itself.
    """
    if not isinstance(record, dict) or set(record) != {"url", "sourceIdentity", "occurrences"}:
        raise ValueError("invalid Lyon record")
    url = record["url"]
    if not isinstance(url, str) or any(ord(c) < 32 for c in url):
        raise ValueError("invalid Lyon URL")
    try:
        url.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("invalid Lyon URL encoding") from exc
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "lyonstahl.com" or parsed.fragment):
        raise ValueError("invalid Lyon URL")
    identity = record["sourceIdentity"]
    if not isinstance(identity, dict) or set(identity) != {"kind", "value"}:
        raise ValueError("invalid Lyon source identity")
    kind, value = identity["kind"], identity["value"]
    if not isinstance(value, str) or not value:
        raise ValueError("invalid Lyon source identity")
    if kind == "property_path":
        match = re.fullmatch(r"/properties/([^/]+)/?", parsed.path, re.IGNORECASE)
        if not match or parsed.query or match.group(1) != value:
            raise ValueError("Lyon path identity differs")
        external_id = value
    elif kind == "wordpress_post":
        try:
            params = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        except ValueError as exc:
            raise ValueError("invalid Lyon query") from exc
        if (parsed.path != "/" or not re.fullmatch(r"[1-9][0-9]*", value)
                or len(params) != 2 or set(params) != {("post_type", "properties"), ("p", value)}):
            raise ValueError("Lyon post identity differs")
        external_id = "wordpress:" + value
    else:
        raise ValueError("unknown Lyon identity kind")
    occurrences = record["occurrences"]
    if not isinstance(occurrences, list) or not occurrences:
        raise ValueError("Lyon occurrences missing")
    seen = set()
    for occurrence in occurrences:
        if not isinstance(occurrence, dict) or set(occurrence) != {"filter", "index", "observedAt", "responseTextSha256", "row"}:
            raise ValueError("invalid Lyon occurrence")
        filter_name, index = occurrence["filter"], occurrence["index"]
        if filter_name not in {"all", "sale", "lease"} or type(index) is not int or index < 0 or (filter_name, index) in seen:
            raise ValueError("duplicate or invalid Lyon occurrence")
        seen.add((filter_name, index))
        try:
            clock = datetime.fromisoformat(occurrence["observedAt"])
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("invalid Lyon observation clock") from exc
        if clock.tzinfo is None or not isinstance(occurrence["responseTextSha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", occurrence["responseTextSha256"]):
            raise ValueError("invalid Lyon occurrence proof")
        if not isinstance(occurrence["row"], dict) or occurrence["row"].get("permalink") != url:
            raise ValueError("Lyon occurrence URL differs")
    try:
        payload = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid Lyon occurrence content") from exc
    def unique_text(key):
        values = set()
        for occurrence in occurrences:
            value = occurrence["row"].get(key)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                continue
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                continue
            values.add(value.strip())
        return next(iter(values)) if len(values) == 1 else None

    def coordinate_pair():
        latitudes, longitudes, pairs = set(), set(), set()
        for occurrence in occurrences:
            row = occurrence["row"]
            values = []
            for key, bound in (("latitude", 90), ("longitude", 180)):
                value = row.get(key)
                if value is None or value == "":
                    values.append(None)
                    continue
                if isinstance(value, bool):
                    return None, None
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    return None, None
                if not math.isfinite(number) or abs(number) > bound:
                    return None, None
                values.append(number)
            lat, lng = values
            if lat is not None:
                latitudes.add(lat)
            if lng is not None:
                longitudes.add(lng)
            if lat is not None and lng is not None:
                pairs.add((lat, lng))
        if len(latitudes) != 1 or len(longitudes) != 1 or len(pairs) != 1:
            return None, None
        return next(iter(pairs))

    latitude, longitude = coordinate_pair()
    return {"slug": "lyon-stahl", "external_id": external_id, "source_url": url,
            "native_id": value if kind == "wordpress_post" else None,
            "source_kind": kind, "observation_base64": base64.b64encode(payload).decode("ascii"),
            "title": unique_text("title"), "address": unique_text("address"), "zip": unique_text("zip"),
            "lat": latitude, "lng": longitude}


def _validate_staged(staged):
    if not isinstance(staged, list) or not staged:
        raise ValueError("empty Lyon staging")
    ids, urls = set(), set()
    for item in staged:
        try:
            raw = base64.b64decode(item["observation_base64"], validate=True)
            record = json.loads(raw)
            valid = stage_record(record)
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise ValueError("invalid Lyon staged observation") from exc
        if item != valid or item["external_id"] in ids or item["source_url"] in urls:
            raise ValueError("duplicate or inconsistent Lyon staging")
        ids.add(item["external_id"])
        urls.add(item["source_url"])


def _payload(value):
    from cre_ingest import sql_lit
    return sql_lit(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False))


def identity_probe_sql(staged):
    """Classify exact identities, recording preimages without address matching."""
    _validate_staged(staged)
    payload = _payload(staged)
    return f"""
WITH candidate AS (SELECT value AS r FROM jsonb_array_elements({payload}::jsonb)),
scoped AS (SELECT b.id brokerage_id,c.r FROM candidate c JOIN credeals.cre_brokerages b ON b.slug='lyon-stahl'),
conflicts AS (
 SELECT s.r->>'external_id' external_id FROM scoped s
 LEFT JOIN credeals.cre_listings own ON own.brokerage_id=s.brokerage_id AND own.external_id=s.r->>'external_id'
 LEFT JOIN credeals.cre_source_index si ON si.brokerage_id=s.brokerage_id AND si.external_id=s.r->>'external_id'
 WHERE EXISTS (SELECT 1 FROM scoped peer WHERE peer.r->>'external_id'<>s.r->>'external_id'
               AND peer.r->>'source_url'=s.r->>'source_url')
 OR (own.id IS NOT NULL AND (own.source_url IS DISTINCT FROM s.r->>'source_url' OR own.deleted_at IS NOT NULL
   OR jsonb_typeof(own.raw_data) IS DISTINCT FROM 'object'
   OR (s.r->>'native_id' IS NOT NULL AND EXISTS (
      SELECT 1 FROM jsonb_path_query(own.raw_data, '$.**.lyonStahlPostId') n(value)
      WHERE n.value #>> '{{}}' IS DISTINCT FROM s.r->>'native_id'))
   OR (own.raw_data ? 'lyonSearchObservations' AND NOT COALESCE(
      jsonb_typeof(own.raw_data->'lyonSearchObservations')='object'
      AND own.raw_data#>>'{{lyonSearchObservations,sourceKey}}'='lyon-stahl'
      AND own.raw_data#>>'{{lyonSearchObservations,externalId}}'=s.r->>'external_id'
      AND jsonb_typeof(own.raw_data#>'{{lyonSearchObservations,entries}}')='array', false))))
 OR (si.id IS NOT NULL AND (si.source_key IS DISTINCT FROM 'lyon-stahl' OR si.url IS DISTINCT FROM s.r->>'source_url'
      OR si.soft_deleted IS TRUE OR si.observation_present IS FALSE))
 OR EXISTS (SELECT 1 FROM credeals.cre_listings other WHERE other.brokerage_id=s.brokerage_id
   AND (own.id IS NULL OR other.id<>own.id)
   AND (other.source_url=s.r->>'source_url' OR other.canonical_url=s.r->>'source_url'
     OR (s.r->>'native_id' IS NOT NULL AND EXISTS (
       SELECT 1 FROM jsonb_path_query(other.raw_data, '$.**.lyonStahlPostId') n(value)
       WHERE n.value #>> '{{}}' = s.r->>'native_id'))))
 OR EXISTS (SELECT 1 FROM credeals.cre_source_index other WHERE other.brokerage_id=s.brokerage_id
   AND other.external_id IS DISTINCT FROM s.r->>'external_id' AND other.url=s.r->>'source_url')
)
SELECT (SELECT count(*) FROM candidate) AS staged, (SELECT count(*) FROM scoped) AS joined,
 COALESCE((SELECT json_agg(external_id ORDER BY external_id) FROM conflicts),'[]'::json)::text AS held,
 COALESCE((SELECT jsonb_agg(jsonb_build_object('external_id',s.r->>'external_id',
   'listing',to_jsonb(own),'source_index',to_jsonb(si), 'children', jsonb_build_object(
     'contacts',COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.id) FROM credeals.cre_listing_contacts c WHERE c.listing_id=own.id),'[]'::jsonb),
     'documents',COALESCE((SELECT jsonb_agg(to_jsonb(d) ORDER BY d.id) FROM credeals.cre_listing_documents d WHERE d.listing_id=own.id),'[]'::jsonb),
     'images',COALESCE((SELECT jsonb_agg(to_jsonb(i) ORDER BY i.id) FROM credeals.cre_listing_images i WHERE i.listing_id=own.id),'[]'::jsonb))) ORDER BY s.r->>'external_id')
 FROM scoped s LEFT JOIN credeals.cre_listings own ON own.brokerage_id=s.brokerage_id AND own.external_id=s.r->>'external_id'
 LEFT JOIN credeals.cre_source_index si ON si.brokerage_id=s.brokerage_id AND si.external_id=s.r->>'external_id'),'[]'::jsonb)::text AS preimage
"""


def _validate_partition(staged, held, preimage):
    _validate_staged(staged)
    ids = {item["external_id"] for item in staged}
    if not isinstance(held, list) or held != sorted(set(held)) or not set(held) <= ids:
        raise ValueError("invalid Lyon held partition")
    if (not isinstance(preimage, list) or len(preimage) != len(ids)
            or any(not isinstance(item, dict) or set(item) != {"external_id", "listing", "source_index", "children"} for item in preimage)
            or {item["external_id"] for item in preimage} != ids):
        raise ValueError("invalid Lyon preimage")


def build_partial_sql(staged, held, preimage, job_id, started_at, finished_at):
    """Generate guarded additive SQL. Caller owns custody, gates and target binding."""
    import uuid

    from cre_ingest import lifecycle_transaction_lock_sql, sql_lit

    _validate_partition(staged, held, preimage)
    job = str(uuid.UUID(job_id))
    payload, held_payload, prior = _payload(staged), _payload(held), _payload(preimage)
    probe = identity_probe_sql(staged)
    return f"""
BEGIN;
{lifecycle_transaction_lock_sql()}
DO $$ BEGIN
 -- Match the shared source-index-before-listing lifecycle lock order. Scope
 -- row locks to offered identities and existing URL/native-ID collisions.
 PERFORM si.id FROM credeals.cre_source_index si
 WHERE si.brokerage_id=(SELECT id FROM credeals.cre_brokerages WHERE slug='lyon-stahl')
   AND EXISTS (SELECT 1 FROM jsonb_array_elements({payload}::jsonb) c(r)
     WHERE si.external_id=c.r->>'external_id' OR si.url=c.r->>'source_url')
 ORDER BY si.id FOR UPDATE;
 PERFORM t.id FROM credeals.cre_listings t
 WHERE t.brokerage_id=(SELECT id FROM credeals.cre_brokerages WHERE slug='lyon-stahl')
   AND EXISTS (SELECT 1 FROM jsonb_array_elements({payload}::jsonb) c(r)
     WHERE t.external_id=c.r->>'external_id' OR t.source_url=c.r->>'source_url'
       OR t.canonical_url=c.r->>'source_url'
       OR (c.r->>'native_id' IS NOT NULL AND EXISTS (
         SELECT 1 FROM jsonb_path_query(t.raw_data, '$.**.lyonStahlPostId') n(value)
         WHERE n.value #>> '{{}}' = c.r->>'native_id')))
 ORDER BY t.id FOR UPDATE;
END $$;
CREATE TEMP TABLE _lyon_probe ON COMMIT DROP AS {probe};
DO $$ BEGIN
 IF (SELECT staged FROM _lyon_probe) <> {len(staged)} OR (SELECT joined FROM _lyon_probe) <> {len(staged)}
 OR (SELECT held::jsonb FROM _lyon_probe) <> {held_payload}::jsonb
 OR (SELECT preimage::jsonb FROM _lyon_probe) <> {prior}::jsonb
 THEN RAISE EXCEPTION 'Lyon identity partition changed'; END IF;
END $$;
CREATE TEMP TABLE _lyon_rows ON COMMIT DROP AS
 SELECT b.id brokerage_id, value r FROM jsonb_array_elements({payload}::jsonb)
 JOIN credeals.cre_brokerages b ON b.slug='lyon-stahl'
 WHERE NOT (value->>'external_id' IN (SELECT jsonb_array_elements_text({held_payload}::jsonb)));
INSERT INTO credeals.cre_scrape_jobs (id,brokerage_id,status,started_at,completed_at,listings_discovered,listings_scraped,listings_saved,errors_count,notes)
SELECT {sql_lit(job)}::uuid,id,'partial',{sql_lit(started_at)}::timestamptz,{sql_lit(finished_at)}::timestamptz,
 {len(staged)},{len(staged)},{len(staged)-len(held)},1,'Lyon bounded search; coverage unknown'
FROM credeals.cre_brokerages WHERE slug='lyon-stahl';
UPDATE credeals.cre_listings t SET
 title=COALESCE(t.title,r->>'title'),
 address=CASE WHEN (t.address IS NULL OR r->>'address' IS NULL OR lower(btrim(t.address))=lower(btrim(r->>'address')))
                    AND (t.zip IS NULL OR r->>'zip' IS NULL OR btrim(t.zip)=btrim(r->>'zip'))
              THEN COALESCE(t.address,r->>'address') ELSE t.address END,
 zip=CASE WHEN (t.address IS NULL OR r->>'address' IS NULL OR lower(btrim(t.address))=lower(btrim(r->>'address')))
                AND (t.zip IS NULL OR r->>'zip' IS NULL OR btrim(t.zip)=btrim(r->>'zip'))
          THEN COALESCE(t.zip,r->>'zip') ELSE t.zip END,
 lat=CASE WHEN t.lat IS NULL AND t.lng IS NULL AND r->>'lat' IS NOT NULL AND r->>'lng' IS NOT NULL
     AND (t.address IS NULL OR r->>'address' IS NULL OR lower(btrim(t.address))=lower(btrim(r->>'address')))
     AND (t.zip IS NULL OR r->>'zip' IS NULL OR btrim(t.zip)=btrim(r->>'zip'))
     THEN (r->>'lat')::double precision ELSE t.lat END,
 lng=CASE WHEN t.lat IS NULL AND t.lng IS NULL AND r->>'lat' IS NOT NULL AND r->>'lng' IS NOT NULL
     AND (t.address IS NULL OR r->>'address' IS NULL OR lower(btrim(t.address))=lower(btrim(r->>'address')))
     AND (t.zip IS NULL OR r->>'zip' IS NULL OR btrim(t.zip)=btrim(r->>'zip'))
     THEN (r->>'lng')::double precision ELSE t.lng END,
 raw_data=jsonb_set(t.raw_data,'{{lyonSearchObservations}}',
   CASE WHEN t.raw_data ? 'lyonSearchObservations' THEN
     CASE WHEN EXISTS (SELECT 1 FROM jsonb_array_elements(t.raw_data#>'{{lyonSearchObservations,entries}}') old(value)
                        WHERE jsonb_typeof(old.value)='object' AND old.value->>'recordBase64'=r->>'observation_base64')
       THEN t.raw_data->'lyonSearchObservations'
       ELSE jsonb_set(t.raw_data->'lyonSearchObservations','{{entries}}', (t.raw_data#>'{{lyonSearchObservations,entries}}')
         || jsonb_build_array(jsonb_build_object('schema','lyon-search-entry/v1','recordBase64',r->>'observation_base64'))) END
   ELSE jsonb_build_object('schema','lyon-search-history/v1','sourceKey','lyon-stahl','externalId',r->>'external_id',
        'entries',jsonb_build_array(jsonb_build_object('schema','lyon-search-entry/v1','recordBase64',r->>'observation_base64'))) END)
FROM _lyon_rows s WHERE t.brokerage_id=s.brokerage_id AND t.external_id=r->>'external_id';
INSERT INTO credeals.cre_listings (brokerage_id,external_id,source_url,canonical_url,status,transaction_type,title,address,zip,lat,lng,raw_data)
SELECT brokerage_id,r->>'external_id',r->>'source_url',NULL,NULL,NULL,r->>'title',r->>'address',r->>'zip',
 (r->>'lat')::double precision,(r->>'lng')::double precision,
 jsonb_build_object('sourceKey','lyon-stahl','id',r->>'external_id','lyonSearchObservations',
 jsonb_build_object('schema','lyon-search-history/v1','sourceKey','lyon-stahl','externalId',r->>'external_id',
 'entries',jsonb_build_array(jsonb_build_object('schema','lyon-search-entry/v1','recordBase64',r->>'observation_base64'))))
FROM _lyon_rows s WHERE NOT EXISTS (SELECT 1 FROM credeals.cre_listings t
 WHERE t.brokerage_id=s.brokerage_id AND t.external_id=s.r->>'external_id');
COMMIT;
"""


def partial_readback_sql(staged, held, preimage, job_id, started_at, finished_at):
    """Verify the exact parent, source-index and child postconditions of the commit."""
    import copy
    import uuid

    from cre_ingest import sql_lit

    _validate_partition(staged, held, preimage)
    job = str(uuid.UUID(job_id))
    prior = {item["external_id"]: item for item in preimage}
    expected = []
    for row in staged:
        before = prior[row["external_id"]]
        entry = {"schema": "lyon-search-entry/v1", "recordBase64": row["observation_base64"]}
        listing = before["listing"]
        if row["external_id"] in held:
            raw = None
        elif listing is None:
            raw = {"sourceKey": "lyon-stahl", "id": row["external_id"], "lyonSearchObservations": {
                "schema": "lyon-search-history/v1", "sourceKey": "lyon-stahl",
                "externalId": row["external_id"], "entries": [entry]}}
        else:
            raw = copy.deepcopy(listing["raw_data"])
            history = raw.get("lyonSearchObservations")
            if history is None:
                history = {"schema": "lyon-search-history/v1", "sourceKey": "lyon-stahl",
                           "externalId": row["external_id"], "entries": []}
                raw["lyonSearchObservations"] = history
            if not any(isinstance(old, dict) and old.get("recordBase64") == row["observation_base64"]
                       for old in history["entries"]):
                history["entries"].append(entry)
        expected.append({"r": row, "p": before, "held": row["external_id"] in held, "raw": raw})
    payload = _payload(expected)
    return f"""
WITH expected AS (SELECT value->'r' r,value->'p' p,(value->>'held')::boolean held,value->'raw' raw FROM jsonb_array_elements({payload}::jsonb)),
observed AS (
 SELECT e.*,to_jsonb(t) listing,to_jsonb(si) source_index,
 jsonb_build_object(
  'contacts',COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.id) FROM credeals.cre_listing_contacts c WHERE c.listing_id=t.id),'[]'::jsonb),
  'documents',COALESCE((SELECT jsonb_agg(to_jsonb(d) ORDER BY d.id) FROM credeals.cre_listing_documents d WHERE d.listing_id=t.id),'[]'::jsonb),
  'images',COALESCE((SELECT jsonb_agg(to_jsonb(i) ORDER BY i.id) FROM credeals.cre_listing_images i WHERE i.listing_id=t.id),'[]'::jsonb)) children
 FROM expected e JOIN credeals.cre_brokerages b ON b.slug='lyon-stahl'
 LEFT JOIN credeals.cre_listings t ON t.brokerage_id=b.id AND t.external_id=e.r->>'external_id'
 LEFT JOIN credeals.cre_source_index si ON si.brokerage_id=b.id AND si.external_id=e.r->>'external_id'
), mismatches AS (
 SELECT 1 FROM observed WHERE source_index IS DISTINCT FROM NULLIF(p->'source_index','null'::jsonb)
 OR children IS DISTINCT FROM p->'children'
 OR (held AND listing IS DISTINCT FROM NULLIF(p->'listing','null'::jsonb))
 OR (NOT held AND (listing IS NULL OR listing->'raw_data' IS DISTINCT FROM raw
   OR listing->>'source_url' IS DISTINCT FROM r->>'source_url'
   OR (p->'listing'='null'::jsonb AND (
      listing->>'status' IS NOT NULL OR listing->>'transaction_type' IS NOT NULL OR listing->>'deleted_at' IS NOT NULL
      OR listing->>'canonical_url' IS NOT NULL OR listing->>'title' IS DISTINCT FROM r->>'title'
      OR listing->>'address' IS DISTINCT FROM r->>'address' OR listing->>'zip' IS DISTINCT FROM r->>'zip'
      OR listing->'lat' IS DISTINCT FROM r->'lat' OR listing->'lng' IS DISTINCT FROM r->'lng'))
   OR (p->'listing'<>'null'::jsonb AND (
      (listing - ARRAY['updated_at','raw_data','title','address','zip','lat','lng']) IS DISTINCT FROM ((p->'listing') - ARRAY['updated_at','raw_data','title','address','zip','lat','lng'])
      OR listing->>'title' IS DISTINCT FROM COALESCE(p#>>'{{listing,title}}',r->>'title')
      OR listing->>'address' IS DISTINCT FROM CASE WHEN
        (p#>>'{{listing,address}}' IS NULL OR r->>'address' IS NULL OR lower(btrim(p#>>'{{listing,address}}'))=lower(btrim(r->>'address')))
        AND (p#>>'{{listing,zip}}' IS NULL OR r->>'zip' IS NULL OR btrim(p#>>'{{listing,zip}}')=btrim(r->>'zip'))
        THEN COALESCE(p#>>'{{listing,address}}',r->>'address') ELSE p#>>'{{listing,address}}' END
      OR listing->>'zip' IS DISTINCT FROM CASE WHEN
        (p#>>'{{listing,address}}' IS NULL OR r->>'address' IS NULL OR lower(btrim(p#>>'{{listing,address}}'))=lower(btrim(r->>'address')))
        AND (p#>>'{{listing,zip}}' IS NULL OR r->>'zip' IS NULL OR btrim(p#>>'{{listing,zip}}')=btrim(r->>'zip'))
        THEN COALESCE(p#>>'{{listing,zip}}',r->>'zip') ELSE p#>>'{{listing,zip}}' END
      OR listing->'lat' IS DISTINCT FROM CASE WHEN p#>>'{{listing,lat}}' IS NULL AND p#>>'{{listing,lng}}' IS NULL
        AND r->>'lat' IS NOT NULL AND r->>'lng' IS NOT NULL
        AND (p#>>'{{listing,address}}' IS NULL OR r->>'address' IS NULL OR lower(btrim(p#>>'{{listing,address}}'))=lower(btrim(r->>'address')))
        AND (p#>>'{{listing,zip}}' IS NULL OR r->>'zip' IS NULL OR btrim(p#>>'{{listing,zip}}')=btrim(r->>'zip'))
        THEN r->'lat' ELSE p#>'{{listing,lat}}' END
      OR listing->'lng' IS DISTINCT FROM CASE WHEN p#>>'{{listing,lat}}' IS NULL AND p#>>'{{listing,lng}}' IS NULL
        AND r->>'lat' IS NOT NULL AND r->>'lng' IS NOT NULL
        AND (p#>>'{{listing,address}}' IS NULL OR r->>'address' IS NULL OR lower(btrim(p#>>'{{listing,address}}'))=lower(btrim(r->>'address')))
        AND (p#>>'{{listing,zip}}' IS NULL OR r->>'zip' IS NULL OR btrim(p#>>'{{listing,zip}}')=btrim(r->>'zip'))
        THEN r->'lng' ELSE p#>'{{listing,lng}}' END))))
)
SELECT (SELECT count(*) FROM credeals.cre_scrape_jobs j JOIN credeals.cre_brokerages b ON b.id=j.brokerage_id
 WHERE b.slug='lyon-stahl' AND j.id={sql_lit(job)}::uuid AND j.status='partial'
 AND j.listings_discovered={len(staged)} AND j.listings_scraped={len(staged)} AND j.listings_saved={len(staged)-len(held)}
 AND j.started_at={sql_lit(started_at)}::timestamptz AND j.completed_at={sql_lit(finished_at)}::timestamptz) AS jobs,
 (SELECT count(*) FROM observed) AS observed,(SELECT count(*) FROM mismatches) AS mismatches
"""
