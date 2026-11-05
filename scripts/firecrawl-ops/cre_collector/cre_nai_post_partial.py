"""Source-native Infabode post staging for an explicitly partial NAI result.

A post is a publication identity, not proof of a unique property or offering.
The caller must independently authenticate the original artifact and owner.
"""
from __future__ import annotations

import base64
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

SOURCE = "nai-global"
SCOPE = "infabode_public_post"
_POST_ID = re.compile(r"infabode:([1-9][0-9]*)")


def _canonical_alias(value):
    """Recognize ordinary URLs for collision checks; preserve originals in raw."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError("invalid NAI source alias")
    try:
        p = urlsplit(value)
        if (p.scheme.lower() not in {"https", "http"} or not p.hostname or p.username is not None
                or p.password is not None or p.port not in (None, 80, 443) or p.fragment):
            raise ValueError("invalid NAI source alias")
        host = p.hostname.lower()
        if not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host:
            raise ValueError("invalid NAI source alias")
        # Keep path and query bytes intact. Aliases beyond this narrow rule
        # remain unresolved; no physical-property uniqueness follows.
        path = p.path.rstrip("/") or "/"
        return urlunsplit((p.scheme.lower(), host, path, p.query, ""))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("invalid NAI source alias") from exc


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("NAI observation clock missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("NAI observation clock invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("NAI observation clock invalid")
    return parsed


def stage_post(listing, generation):
    """Validate exact source-post identity; retain the complete input record."""
    if not isinstance(listing, dict) or listing.get("sourceKey") != SOURCE:
        raise ValueError("NAI source differs")
    native = listing.get("id")
    match = _POST_ID.fullmatch(native) if isinstance(native, str) else None
    if not match:
        raise ValueError("NAI native post ID invalid")
    post_id = match.group(1)
    expected_url = f"https://infabode.com/services/listings/{post_id}"
    post_url = f"https://infabode.com/post/{post_id}"
    if listing.get("url") != expected_url:
        raise ValueError("NAI source post URL differs")
    tx = listing.get("transactionMode")
    if tx not in {"sale", "lease"}:
        raise ValueError("NAI transaction category invalid")
    expected_type = 4 if tx == "sale" else 10
    for key in ("feedRow", "publicPost"):
        raw = listing.get(key)
        if (not isinstance(raw, dict) or type(raw.get("id")) not in {int, str}
                or str(raw["id"]) != post_id or raw.get("url") != post_url
                or not isinstance(raw.get("contentType"), dict)
                or raw["contentType"].get("id") != expected_type):
            raise ValueError("NAI source post evidence differs")
    provenance = listing.get("freshnessProvenance")
    if not isinstance(provenance, dict) or provenance.get("generationId") != generation:
        raise ValueError("NAI source generation differs")
    inventory = _timestamp(listing.get("inventoryObservedAt"))
    detail = _timestamp(listing.get("detailObservedAt"))
    if detail < inventory:
        raise ValueError("NAI observation ordering differs")
    alias = listing.get("canonicalUrl")
    public_alias = listing["publicPost"].get("urlOriginal") or listing["publicPost"].get("url")
    if alias != public_alias:
        raise ValueError("NAI source alias differs")
    alias_key = _canonical_alias(alias)

    # Reuse existing source mapper only after checking source identity. Values
    # absent or ambiguous there remain NULL; the unmodified source remains raw.
    from cre_ingest import to_row

    row = to_row(listing, {}, None)
    if not row or row.get("slug") != SOURCE or row.get("external_id") != native:
        raise ValueError("NAI mapped identity differs")
    fields = ("title", "city", "state", "description", "transaction_type",
              "property_type", "size_sf", "available_sf", "min_divisible_sf",
              "max_divisible_sf", "lot_size_sf", "year_built", "sale_price_usd",
              "sale_price_per_sf", "lease_rate_min", "lease_rate_max", "lease_rate_type")
    projected = {key: row.get(key) for key in fields}
    for key, value in projected.items():
        if isinstance(value, str) and ("\x00" in value or
                any(0xD800 <= ord(character) <= 0xDFFF for character in value)):
            projected[key] = None
        elif isinstance(value, float) and not math.isfinite(value):
            projected[key] = None
    year = projected["year_built"]
    if type(year) not in {int, float} or not math.isfinite(year) or int(year) != year or not -(2**31) <= year < 2**31:
        projected["year_built"] = None
    else:
        projected["year_built"] = int(year)
    lat, lng = row.get("lat"), row.get("lng")
    if (type(lat) in {int, float} and type(lng) in {int, float}
            and math.isfinite(lat) and math.isfinite(lng)
            and -90 <= lat <= 90 and -180 <= lng <= 180):
        projected.update(lat=lat, lng=lng)
    else:
        projected.update(lat=None, lng=None)
    try:
        payload = json.dumps(listing, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("NAI source observation cannot be retained") from exc
    return {"external_id": native, "source_url": expected_url, "alias_key": alias_key, "generation_id": generation,
            "observed_at": detail.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "observation_base64": base64.b64encode(payload).decode("ascii"),
            **projected}


def partition_posts(listings, generation):
    """Hold invalid or duplicate source identities; never discard source rows."""
    if not isinstance(listings, list) or not isinstance(generation, str) or not generation:
        raise ValueError("NAI source partition invalid")
    staged, held = [], []
    id_counts, alias_counts = {}, {}
    # An invalid peer can still witness a competing identity. Never count only
    # the successfully staged partition when testing source-local uniqueness.
    for index, listing in enumerate(listings):
        if isinstance(listing, dict):
            native = listing.get("id")
            if isinstance(native, str) and _POST_ID.fullmatch(native):
                id_counts[native] = id_counts.get(native, 0) + 1
            try:
                alias = _canonical_alias(listing.get("canonicalUrl"))
                if alias is not None:
                    alias_counts[alias] = alias_counts.get(alias, 0) + 1
            except ValueError:
                pass
        try:
            staged.append((index, stage_post(listing, generation)))
        except (TypeError, ValueError, KeyError, RecursionError):
            held.append({"index": index, "reason": "unqualified_source_post"})
    accepted = []
    for index, row in staged:
        if id_counts[row["external_id"]] > 1:
            held.append({"index": index, "reason": "duplicate_post_id"})
        elif row["alias_key"] is not None and alias_counts[row["alias_key"]] > 1:
            held.append({"index": index, "reason": "conflicting_source_alias"})
        else:
            accepted.append(row)
    return {"staged": accepted, "held": sorted(held, key=lambda item: item["index"])}

def _validated(staged):
    if not isinstance(staged, list) or not staged:
        raise ValueError("NAI staged partition empty")
    seen = set()
    for row in staged:
        try:
            raw = base64.b64decode(row["observation_base64"], validate=True)
            original = json.loads(raw)
            expected = stage_post(original, row["generation_id"])
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise ValueError("NAI staged record invalid") from exc
        if expected != row or row["external_id"] in seen:
            raise ValueError("NAI staged record differs or repeats")
        seen.add(row["external_id"])


def _payload(value):
    from cre_ingest import sql_lit

    return sql_lit(json.dumps(value, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=True, allow_nan=False))


def _url_key(expression):
    # Conservative case-folding intentionally holds possible conflicts. Query
    # and path stay part of the key; it makes no property-equivalence claim.
    return ("regexp_replace(regexp_replace(lower(" + expression
            + "), ':443(?=[/?#]|$)', '', 'g'), '/+(?=\\?|$)', '', 'g')")


def identity_probe_sql(staged):
    """Return source-bound parent/index preimages and collision holds.

    Intended to be rechecked inside the shared lifecycle transaction. It is not
    a standalone write authorization or a universal cross-writer URL constraint.
    """
    _validated(staged)
    payload = _payload(staged)
    alias = _url_key("s.r->>'alias_key'")
    source = _url_key("s.r->>'source_url'")
    other_source = _url_key("other.source_url")
    other_canonical = _url_key("other.canonical_url")
    index_url = _url_key("other.url")
    peer_alias = _url_key("peer.r->>'alias_key'")
    peer_source = _url_key("peer.r->>'source_url'")
    return f"""
WITH candidate AS (SELECT value AS r FROM jsonb_array_elements({payload}::jsonb)),
scoped AS (
 SELECT b.id brokerage_id, c.r FROM candidate c
 JOIN credeals.cre_brokerages b ON b.slug='nai-global'
),
conflicts AS (
 SELECT s.r->>'external_id' external_id FROM scoped s
 LEFT JOIN credeals.cre_listings own
   ON own.brokerage_id=s.brokerage_id AND own.external_id=s.r->>'external_id'
 LEFT JOIN credeals.cre_source_index si
   ON si.brokerage_id=s.brokerage_id AND si.external_id=s.r->>'external_id'
 WHERE (own.id IS NOT NULL AND (
   own.deleted_at IS NOT NULL OR (own.status IS NOT NULL AND own.status <> 'active')
   OR own.source_url IS DISTINCT FROM s.r->>'source_url'
   OR (own.scraped_at IS NOT NULL AND own.scraped_at > (s.r->>'observed_at')::timestamptz)
   OR jsonb_typeof(own.raw_data) IS DISTINCT FROM 'object'
   OR own.raw_data->>'sourceKey' IS DISTINCT FROM 'nai-global'
   OR own.raw_data->>'id' IS DISTINCT FROM s.r->>'external_id'
   OR (own.raw_data ? 'naiPublicPostHistory' AND NOT COALESCE(
      jsonb_typeof(own.raw_data->'naiPublicPostHistory')='object'
      AND own.raw_data#>>'{{naiPublicPostHistory,schema}}'='nai-public-post-history/v1'
      AND own.raw_data#>>'{{naiPublicPostHistory,identityScope}}'='infabode_public_post'
      AND own.raw_data#>>'{{naiPublicPostHistory,externalId}}'=s.r->>'external_id'
      AND jsonb_typeof(own.raw_data#>'{{naiPublicPostHistory,entries}}')='array'
      AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(
        CASE WHEN jsonb_typeof(own.raw_data#>'{{naiPublicPostHistory,entries}}')='array'
             THEN own.raw_data#>'{{naiPublicPostHistory,entries}}' ELSE '[]'::jsonb END) e(value)
        WHERE jsonb_typeof(e.value) IS DISTINCT FROM 'object'
          OR e.value->>'schema' IS DISTINCT FROM 'nai-public-post-entry/v1'
          OR COALESCE(e.value->>'observedAt','') !~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}T\\d{{2}}:\\d{{2}}:\\d{{2}}\\.\\d{{6}}Z$'
          OR (e.value->>'observedAt') >= (s.r->>'observed_at')
          OR jsonb_typeof(e.value->'recordBase64') IS DISTINCT FROM 'string'), false))))
 OR (si.id IS NOT NULL AND (
   own.id IS NULL OR si.source_key IS DISTINCT FROM 'nai-global'
   OR si.url IS DISTINCT FROM own.source_url OR si.soft_deleted IS TRUE
   OR si.observation_present IS FALSE
   OR (si.last_enumerated_at IS NOT NULL AND si.last_enumerated_at > (s.r->>'observed_at')::timestamptz)))
 OR EXISTS (SELECT 1 FROM scoped peer
   WHERE peer.r->>'external_id'<>s.r->>'external_id'
   AND (({peer_source}) IN ({source},{alias}) OR ({peer_alias}) IN ({source},{alias})))
 OR EXISTS (SELECT 1 FROM credeals.cre_listings other
   WHERE other.brokerage_id=s.brokerage_id AND (own.id IS NULL OR other.id<>own.id)
   AND (({other_source}) IN ({source},{alias}) OR ({other_canonical}) IN ({source},{alias})))
 OR EXISTS (SELECT 1 FROM credeals.cre_source_index other
   WHERE other.brokerage_id=s.brokerage_id AND other.external_id IS DISTINCT FROM s.r->>'external_id'
   AND ({index_url}) IN ({source},{alias}))
)
SELECT (SELECT count(*) FROM candidate) staged, (SELECT count(*) FROM scoped) joined,
 COALESCE((SELECT json_agg(external_id ORDER BY external_id) FROM conflicts),'[]'::json)::text held,
 COALESCE((SELECT jsonb_agg(jsonb_build_object('external_id',s.r->>'external_id',
   'listing',to_jsonb(own),'source_index',to_jsonb(si),'children',jsonb_build_object(
     'contacts',COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.id) FROM credeals.cre_listing_contacts c WHERE c.listing_id=own.id),'[]'::jsonb),
     'documents',COALESCE((SELECT jsonb_agg(to_jsonb(d) ORDER BY d.id) FROM credeals.cre_listing_documents d WHERE d.listing_id=own.id),'[]'::jsonb),
     'images',COALESCE((SELECT jsonb_agg(to_jsonb(i) ORDER BY i.id) FROM credeals.cre_listing_images i WHERE i.listing_id=own.id),'[]'::jsonb),
     'media',COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.id) FROM credeals.cre_listing_media x WHERE x.listing_id=own.id),'[]'::jsonb),
     'links',COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.id) FROM credeals.cre_listing_links x WHERE x.listing_id=own.id),'[]'::jsonb)))
   ORDER BY s.r->>'external_id')
 FROM scoped s LEFT JOIN credeals.cre_listings own
   ON own.brokerage_id=s.brokerage_id AND own.external_id=s.r->>'external_id'
 LEFT JOIN credeals.cre_source_index si
   ON si.brokerage_id=s.brokerage_id AND si.external_id=s.r->>'external_id'),'[]'::jsonb)::text preimage
"""

def _partition(staged, held, preimage):
    _validated(staged)
    ids = {row["external_id"] for row in staged}
    if (not isinstance(held, list) or held != sorted(set(held))
            or not set(held) <= ids or not isinstance(preimage, list)
            or len(preimage) != len(ids)
            or any(not isinstance(row, dict)
                   or set(row) != {"external_id", "listing", "source_index", "children"}
                   for row in preimage)
            or {row["external_id"] for row in preimage} != ids):
        raise ValueError("NAI target partition invalid")


def build_partial_sql(staged, held, preimage, job_id, started_at, finished_at):
    """Add post observations under the shared lifecycle lock.

    The caller authenticates source/target/owner and must resolve uncertain
    commit outcomes before another writer. Noncooperating writers are outside
    the shared advisory-lock contract.
    """
    import uuid

    from cre_ingest import lifecycle_transaction_lock_sql, sql_lit

    _partition(staged, held, preimage)
    job = str(uuid.UUID(job_id))
    payload, hold, before = _payload(staged), _payload(held), _payload(preimage)
    source = _url_key("c.r->>'source_url'")
    alias = _url_key("c.r->>'alias_key'")
    other_source = _url_key("l.source_url")
    other_canonical = _url_key("l.canonical_url")
    index_url = _url_key("si.url")
    return f"""
BEGIN;
{lifecycle_transaction_lock_sql()}
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM credeals.cre_scrape_jobs WHERE id={sql_lit(job)}::uuid)
 THEN RAISE EXCEPTION 'NAI partial job already exists'; END IF;
 PERFORM si.id FROM credeals.cre_source_index si
 WHERE si.brokerage_id=(SELECT id FROM credeals.cre_brokerages WHERE slug='nai-global')
 AND EXISTS (SELECT 1 FROM jsonb_array_elements({payload}::jsonb) c(r)
    WHERE si.external_id=c.r->>'external_id' OR ({index_url}) IN ({source},{alias}))
 ORDER BY si.id FOR UPDATE;
 PERFORM l.id FROM credeals.cre_listings l
 WHERE l.brokerage_id=(SELECT id FROM credeals.cre_brokerages WHERE slug='nai-global')
 AND EXISTS (SELECT 1 FROM jsonb_array_elements({payload}::jsonb) c(r)
    WHERE l.external_id=c.r->>'external_id'
       OR ({other_source}) IN ({source},{alias})
       OR ({other_canonical}) IN ({source},{alias}))
 ORDER BY l.id FOR UPDATE;
END $$;
CREATE TEMP TABLE _nai_partial_probe ON COMMIT DROP AS {identity_probe_sql(staged)};
DO $$ BEGIN
 IF (SELECT staged FROM _nai_partial_probe) <> {len(staged)}
 OR (SELECT joined FROM _nai_partial_probe) <> {len(staged)}
 OR (SELECT held::jsonb FROM _nai_partial_probe) <> {hold}::jsonb
 OR (SELECT preimage::jsonb FROM _nai_partial_probe) <> {before}::jsonb
 THEN RAISE EXCEPTION 'NAI post identity partition changed'; END IF;
END $$;
CREATE TEMP TABLE _nai_partial_rows ON COMMIT DROP AS
 SELECT b.id brokerage_id,value r FROM jsonb_array_elements({payload}::jsonb)
 JOIN credeals.cre_brokerages b ON b.slug='nai-global'
 WHERE NOT (value->>'external_id' IN (SELECT jsonb_array_elements_text({hold}::jsonb)));
INSERT INTO credeals.cre_scrape_jobs
 (id,brokerage_id,status,started_at,completed_at,listings_discovered,listings_scraped,listings_saved,errors_count,notes)
SELECT {sql_lit(job)}::uuid,id,'partial',{sql_lit(started_at)}::timestamptz,{sql_lit(finished_at)}::timestamptz,
 {len(staged)},{len(staged)},{len(staged)-len(held)},0,'NAI source posts; identity scope publication; coverage unknown'
FROM credeals.cre_brokerages WHERE slug='nai-global';
UPDATE credeals.cre_listings l SET raw_data=jsonb_set(l.raw_data,'{{naiPublicPostHistory}}',
 CASE WHEN l.raw_data ? 'naiPublicPostHistory'
   THEN jsonb_set(l.raw_data->'naiPublicPostHistory','{{entries}}',
      (l.raw_data#>'{{naiPublicPostHistory,entries}}') ||
      jsonb_build_array(jsonb_build_object('schema','nai-public-post-entry/v1',
          'observedAt',r->>'observed_at','recordBase64',r->>'observation_base64')))
   ELSE jsonb_build_object('schema','nai-public-post-history/v1','identityScope','infabode_public_post',
      'externalId',r->>'external_id','entries',
      jsonb_build_array(jsonb_build_object('schema','nai-public-post-entry/v1',
          'observedAt',r->>'observed_at','recordBase64',r->>'observation_base64'))) END)
FROM _nai_partial_rows s WHERE l.brokerage_id=s.brokerage_id AND l.external_id=r->>'external_id';
INSERT INTO credeals.cre_listings
 (brokerage_id,external_id,source_url,canonical_url,status,transaction_type,property_type,
  title,city,state,lat,lng,description,size_sf,available_sf,min_divisible_sf,max_divisible_sf,
  lot_size_sf,year_built,sale_price_usd,sale_price_per_sf,lease_rate_min,lease_rate_max,lease_rate_type,
  country,scraped_at,raw_data)
SELECT brokerage_id,r->>'external_id',r->>'source_url',NULL,NULL,r->>'transaction_type',r->>'property_type',
 r->>'title',r->>'city',r->>'state',(r->>'lat')::double precision,(r->>'lng')::double precision,
 r->>'description',(r->>'size_sf')::numeric,(r->>'available_sf')::numeric,
 (r->>'min_divisible_sf')::numeric,(r->>'max_divisible_sf')::numeric,(r->>'lot_size_sf')::numeric,
 (r->>'year_built')::integer,(r->>'sale_price_usd')::numeric,(r->>'sale_price_per_sf')::numeric,
 (r->>'lease_rate_min')::numeric,(r->>'lease_rate_max')::numeric,r->>'lease_rate_type',
 NULL,NULL,jsonb_build_object('sourceKey','nai-global','id',r->>'external_id',
 'identityScope','infabode_public_post','naiPublicPostHistory',
 jsonb_build_object('schema','nai-public-post-history/v1','identityScope','infabode_public_post',
   'externalId',r->>'external_id','entries',
   jsonb_build_array(jsonb_build_object('schema','nai-public-post-entry/v1',
     'observedAt',r->>'observed_at','recordBase64',r->>'observation_base64'))))
FROM _nai_partial_rows s WHERE NOT EXISTS (
 SELECT 1 FROM credeals.cre_listings l
 WHERE l.brokerage_id=s.brokerage_id AND l.external_id=s.r->>'external_id');
COMMIT;
"""

def partial_readback_sql(staged, held, preimage, job_id, started_at, finished_at):
    """Verify exact parent projection, raw history, lifecycle and five children."""
    import copy
    import uuid

    from cre_ingest import sql_lit

    _partition(staged, held, preimage)
    job = str(uuid.UUID(job_id))
    prior = {item["external_id"]: item for item in preimage}
    expected = []
    for row in staged:
        previous = prior[row["external_id"]]
        entry = {"schema": "nai-public-post-entry/v1", "observedAt": row["observed_at"],
                 "recordBase64": row["observation_base64"]}
        listing = previous["listing"]
        if row["external_id"] in held:
            raw = None
        elif listing is None:
            raw = {"sourceKey": "nai-global", "id": row["external_id"],
                   "identityScope": SCOPE, "naiPublicPostHistory": {
                       "schema": "nai-public-post-history/v1", "identityScope": SCOPE,
                       "externalId": row["external_id"], "entries": [entry]}}
        else:
            raw = copy.deepcopy(listing["raw_data"])
            history = raw.get("naiPublicPostHistory")
            if history is None:
                history = {"schema": "nai-public-post-history/v1", "identityScope": SCOPE,
                           "externalId": row["external_id"], "entries": []}
                raw["naiPublicPostHistory"] = history
            history["entries"].append(entry)
        expected.append({"r": row, "p": previous, "held": row["external_id"] in held, "raw": raw})
    payload = _payload(expected)
    fields = ("transaction_type", "property_type", "title", "city", "state", "lat", "lng",
              "description", "size_sf", "available_sf", "min_divisible_sf", "max_divisible_sf",
              "lot_size_sf", "year_built", "sale_price_usd", "sale_price_per_sf",
              "lease_rate_min", "lease_rate_max", "lease_rate_type")
    projected = " OR ".join(f"listing->'{key}' IS DISTINCT FROM r->'{key}'" for key in fields)
    return f"""
WITH expected AS (
 SELECT value->'r' r,value->'p' p,(value->>'held')::boolean held,value->'raw' raw
 FROM jsonb_array_elements({payload}::jsonb)
), observed AS (
 SELECT e.*,to_jsonb(l) listing,to_jsonb(si) source_index,
 jsonb_build_object(
  'contacts',COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.id) FROM credeals.cre_listing_contacts c WHERE c.listing_id=l.id),'[]'::jsonb),
  'documents',COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.id) FROM credeals.cre_listing_documents x WHERE x.listing_id=l.id),'[]'::jsonb),
  'images',COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.id) FROM credeals.cre_listing_images x WHERE x.listing_id=l.id),'[]'::jsonb),
  'media',COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.id) FROM credeals.cre_listing_media x WHERE x.listing_id=l.id),'[]'::jsonb),
  'links',COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.id) FROM credeals.cre_listing_links x WHERE x.listing_id=l.id),'[]'::jsonb)) children
 FROM expected e JOIN credeals.cre_brokerages b ON b.slug='nai-global'
 LEFT JOIN credeals.cre_listings l ON l.brokerage_id=b.id AND l.external_id=e.r->>'external_id'
 LEFT JOIN credeals.cre_source_index si ON si.brokerage_id=b.id AND si.external_id=e.r->>'external_id'
), mismatches AS (
 SELECT 1 FROM observed WHERE source_index IS DISTINCT FROM NULLIF(p->'source_index','null'::jsonb)
 OR children IS DISTINCT FROM p->'children'
 OR (held AND listing IS DISTINCT FROM NULLIF(p->'listing','null'::jsonb))
 OR (NOT held AND (listing IS NULL OR listing->'raw_data' IS DISTINCT FROM raw
   OR listing->>'source_url' IS DISTINCT FROM r->>'source_url'
   OR (p->'listing'='null'::jsonb AND (
       listing->>'status' IS NOT NULL OR listing->>'canonical_url' IS NOT NULL
       OR listing->>'country' IS NOT NULL OR listing->>'scraped_at' IS NOT NULL OR {projected}))
   OR (p->'listing'<>'null'::jsonb AND
      (listing - ARRAY['updated_at','raw_data']) IS DISTINCT FROM
      ((p->'listing') - ARRAY['updated_at','raw_data']))))
)
SELECT (SELECT count(*) FROM credeals.cre_scrape_jobs j JOIN credeals.cre_brokerages b ON b.id=j.brokerage_id
 WHERE b.slug='nai-global' AND j.id={sql_lit(job)}::uuid AND j.status='partial'
 AND j.listings_discovered={len(staged)} AND j.listings_saved={len(staged)-len(held)}
 AND j.started_at={sql_lit(started_at)}::timestamptz AND j.completed_at={sql_lit(finished_at)}::timestamptz) jobs,
 (SELECT count(*) FROM observed) observed,(SELECT count(*) FROM mismatches) mismatches
"""