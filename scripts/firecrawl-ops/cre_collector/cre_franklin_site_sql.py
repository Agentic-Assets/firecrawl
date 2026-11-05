"""SQL for the admitted Franklin source-qualified partial-ingest path.

The checkpoint owner rederives the projection from the retained source artifact,
binds code, generation and target, and fences uncertain commits before calling
this module. New rows use source-explicit ACTIVE status only; unknown status
is stored as NULL and excluded from active views.

Each distinct card observation and its provenance is retained in raw_data. Source page and artifact
limits still bound each attempt; accumulating history grows the listing row.
Durable long-term evidence custody and growth monitoring remain follow-up work.
"""
import hashlib
import json
import re
import uuid
from datetime import timedelta
from urllib.parse import urlsplit

from cre_ingest import (
    _RUN_UUID_NAMESPACE,
    artifact_run_identity_from_digests,
    lifecycle_transaction_lock_sql,
    parse_strict_freshness_timestamp,
    sql_lit,
    to_row,
)

_ID = re.compile(r"franklin-site:(sale|lease):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\Z")
_PATH = re.compile(r"/properties/for-(sale|lease)/[a-z0-9]+(?:[-a-z0-9]*[a-z0-9])?/?\Z")


def stage(listings):
    """Stage verified projections; do not treat this check as source admission."""
    if not isinstance(listings, list) or not listings:
        raise ValueError("Franklin site rows missing")
    staged = []
    ids = set()
    urls = set()
    generations = set()
    for item in listings:
        if not isinstance(item, dict) or item.get("sourceKey") != "franklin-street":
            raise ValueError("invalid Franklin site source")
        identity = item.get("id")
        match = _ID.fullmatch(identity) if isinstance(identity, str) else None
        url = item.get("url")
        parts = urlsplit(url) if isinstance(url, str) else None
        path_match = _PATH.fullmatch(parts.path) if parts else None
        observed = item.get("franklinSiteObservation")
        card = observed.get("card") if isinstance(observed, dict) else None
        attrs = card.get("attributes") if isinstance(card, dict) else None
        if (match is None or parts is None or parts.scheme != "https" or parts.netloc != "franklinst.com"
                or parts.query or parts.fragment or path_match is None or match[1] != path_match[1]
                or item.get("transactionMode") != match[1] or item.get("canonicalUrl") != url
                or item.get("preserveChildCollections") is not True
                or not isinstance(attrs, dict) or str(attrs.get("data-listing-id", "")).lower() != match[2]
                or attrs.get("data-offer-type") != ("FOR_SALE" if match[1] == "sale" else "FOR_LEASE")
                or observed.get("coverage") != "partial_unknown"
                or observed.get("sourceUrl") != f"https://franklinst.com/properties/for-{match[1]}/"
                or not isinstance(observed.get("observedAt"), str)
                or not isinstance(item.get("franklinSiteGenerationId"), str)
                or not 1 <= len(item["franklinSiteGenerationId"]) <= 128
                or not re.fullmatch(r"[0-9a-f]{64}", str(observed.get("decodedHtmlSha256", "")))
                or not isinstance(card.get("html"), str)
                or identity in ids or url in urls):
            raise ValueError("invalid Franklin site identity or observation")
        parse_strict_freshness_timestamp(observed["observedAt"], field="observedAt")
        row = to_row(item, {}, None)
        if not row or row["slug"] != "franklin-street" or row["external_id"] != identity:
            raise ValueError("Franklin staging identity differs")
        if row["lat"] is None or row["lng"] is None:
            row["lat"] = row["lng"] = None
        ids.add(identity)
        urls.add(url)
        generations.add(item["franklinSiteGenerationId"])
        address = attrs.get("data-listing-address")
        if not isinstance(address, str) or not address.strip():
            address = None
        status = "active" if attrs.get("data-status") == "ACTIVE" else None
        staged.append({key: row.get(key) for key in (
            "external_id", "source_url", "canonical_url", "transaction_type",
            "title", "state", "lat", "lng",
        )} | {"address": address, "status": status,
              "observation": {**observed, "generationId": item["franklinSiteGenerationId"]}})
    if len(generations) != 1:
        raise ValueError("mixed Franklin site generations")
    return staged


def _partition(staged, held):
    identities = {row["external_id"] for row in staged}
    if not isinstance(held, list) or held != sorted(set(held)) or not set(held) <= identities:
        raise ValueError("invalid Franklin held partition")


def _binding(staged, held, binding):
    required = {"artifact_sha256", "attempt_id", "generation_id", "producer_sha",
                "database_target_sha256", "generation_started_at", "started_at", "finished_at"}
    if not isinstance(binding, dict) or set(binding) != required:
        raise ValueError("invalid Franklin transaction binding")
    if (any(not isinstance(binding.get(key), str) for key in required)
            or not re.fullmatch(r"[0-9a-f]{64}", binding["artifact_sha256"])
            or not re.fullmatch(r"[0-9a-f]{64}", binding.get("database_target_sha256", ""))
            or not re.fullmatch(r"[0-9a-f]{40}", binding.get("producer_sha", ""))
            or not re.fullmatch(r"[0-9a-f]{32}", binding.get("attempt_id", ""))
            or not isinstance(binding.get("generation_id"), str)
            or any(row["observation"]["generationId"] != binding["generation_id"] for row in staged)):
        raise ValueError("invalid Franklin identity binding")
    generation_started = parse_strict_freshness_timestamp(binding["generation_started_at"], field="generation_started_at")
    started = parse_strict_freshness_timestamp(binding["started_at"], field="started_at")
    finished = parse_strict_freshness_timestamp(binding["finished_at"], field="finished_at")
    if generation_started > started or started > finished:
        raise ValueError("invalid Franklin attempt clocks")
    for row in staged:
        observed = parse_strict_freshness_timestamp(row["observation"]["observedAt"], field="observedAt")
        if observed < started or observed > finished + timedelta(minutes=5):
            raise ValueError("Franklin observation outside bound attempt")
    staged_digest = hashlib.sha256(json.dumps(staged, sort_keys=True, separators=(",", ":"),
                                             ensure_ascii=False).encode()).hexdigest()
    claims = {**binding, "held": held, "staged_sha256": staged_digest}
    claims_digest = hashlib.sha256(json.dumps(claims, sort_keys=True, separators=(",", ":"),
                                             ensure_ascii=False).encode()).hexdigest()
    key, _ = artifact_run_identity_from_digests([binding["artifact_sha256"]])
    job_id = str(uuid.uuid5(_RUN_UUID_NAMESPACE, f"{key}:franklin-street"))
    return {"key": key, "job_id": job_id, "notes": "franklin-site-partial-v1:" + claims_digest}


def _site_query(payload):
    return f"""SELECT b.id AS brokerage_id, x.*
FROM jsonb_to_recordset({payload}::jsonb) AS x(
 external_id text, source_url text, canonical_url text, transaction_type text,
 title text, state text, lat double precision, lng double precision, address text, status text, observation jsonb)
JOIN credeals.cre_brokerages b ON b.slug='franklin-street'"""


def _held_query():
    return """SELECT s.external_id FROM _franklin_site s
LEFT JOIN credeals.cre_listings own ON own.brokerage_id=s.brokerage_id AND own.external_id=s.external_id
WHERE (own.id IS NOT NULL AND (own.deleted_at IS NOT NULL OR own.source_url IS DISTINCT FROM s.source_url
 OR own.canonical_url IS DISTINCT FROM s.canonical_url
 OR own.raw_data->>'franklinSitePartialVersion' IS DISTINCT FROM '1'
 OR jsonb_typeof(own.raw_data->'franklinSiteObservations') IS DISTINCT FROM 'array'
 OR own.raw_data->>'franklinSiteLastObservedAt' IS NULL
 OR CASE WHEN jsonb_typeof(own.raw_data->'franklinSiteObservations')='array'
         THEN (jsonb_array_length(own.raw_data->'franklinSiteObservations') = 0
           OR (own.raw_data->>'franklinSiteLastObservedAt')::timestamptz
                    > (s.observation->>'observedAt')::timestamptz
           OR ((own.raw_data->>'franklinSiteLastObservedAt')::timestamptz
                    = (s.observation->>'observedAt')::timestamptz
               AND own.raw_data->'franklinSiteObservations'->-1->'card' IS DISTINCT FROM s.observation->'card')
           )
         ELSE false END))
 OR EXISTS (SELECT 1 FROM credeals.cre_listings other
            WHERE other.brokerage_id=s.brokerage_id
              AND (other.source_url IN (s.source_url, s.canonical_url)
                   OR other.canonical_url IN (s.source_url, s.canonical_url))
              AND (own.id IS NULL OR other.id<>own.id))"""


def identity_probe_sql(listings):
    """Compute a source-local candidate partition without making writes."""
    staged = stage(listings)
    payload = sql_lit(json.dumps(staged, ensure_ascii=False, separators=(",", ":")))
    return f"""
WITH _franklin_site AS ({_site_query(payload)}),
held AS ({_held_query()})
SELECT json_build_object('staged', {len(staged)}, 'joined', (SELECT count(*) FROM _franklin_site),
 'held', COALESCE((SELECT json_agg(external_id ORDER BY external_id) FROM held), '[]'::json)) AS result;
"""


def _location_compatible(left, right):
    """Do not combine nonmatching evidence into one normalized location."""
    return " AND ".join(
        f"({left}.{field} IS NULL OR {right}.{field} IS NULL OR {left}.{field} = {right}.{field})"
        for field in ("address", "state", "lat", "lng")
    )


def build_sql(listings, held, binding):
    """Create a transaction rechecking its prebound identity partition."""
    staged = stage(listings)
    _partition(staged, held)
    job = _binding(staged, held, binding)
    payload = sql_lit(json.dumps(staged, ensure_ascii=False, separators=(",", ":")))
    expected_held = sql_lit(json.dumps(held, separators=(",", ":")))
    return f"""
BEGIN;
SET LOCAL statement_timeout='600s';
SET LOCAL TIME ZONE 'UTC';
-- Stable, round-trip float JSON is required for the postimage digest.
SET LOCAL extra_float_digits=3;
{lifecycle_transaction_lock_sql()}
CREATE TEMP TABLE _franklin_site ON COMMIT DROP AS
{_site_query(payload)};
DO $$ BEGIN
 IF (SELECT count(*) FROM _franklin_site) <> {len(staged)} THEN
   RAISE EXCEPTION 'Franklin source brokerage missing';
 END IF;
END $$;
-- Serialize against both same-ID and same-URL parents under the global lock.
CREATE TEMP TABLE _franklin_site_locks ON COMMIT DROP AS
SELECT l.id FROM credeals.cre_listings l
JOIN _franklin_site s ON s.brokerage_id=l.brokerage_id
 AND (s.external_id=l.external_id
      OR l.source_url IN (s.source_url, s.canonical_url)
      OR l.canonical_url IN (s.source_url, s.canonical_url))
ORDER BY l.id FOR UPDATE OF l;
CREATE TEMP TABLE _franklin_site_held ON COMMIT DROP AS
{_held_query()};
CREATE TEMP TABLE _franklin_site_before ON COMMIT DROP AS
SELECT s.external_id, l AS prior_row, to_jsonb(l) AS prior FROM _franklin_site s
JOIN credeals.cre_listings l ON l.brokerage_id=s.brokerage_id AND l.external_id=s.external_id
WHERE NOT EXISTS (SELECT 1 FROM _franklin_site_held h WHERE h.external_id=s.external_id);
DO $$ BEGIN
 IF COALESCE((SELECT jsonb_agg(external_id ORDER BY external_id) FROM _franklin_site_held), '[]'::jsonb)
      IS DISTINCT FROM {expected_held}::jsonb THEN
   RAISE EXCEPTION 'Franklin identity partition changed';
 END IF;
END $$;
INSERT INTO credeals.cre_scrape_jobs
 (id, brokerage_id, status, started_at, completed_at, listings_discovered,
  listings_scraped, listings_saved, errors_count, notes, artifact_run_key)
SELECT {sql_lit(job["job_id"])}::uuid, b.id, 'partial',
       {sql_lit(binding["started_at"])}::timestamptz, {sql_lit(binding["finished_at"])}::timestamptz,
       {len(staged)}, 0, {len(staged) - len(held)}, 1, {sql_lit(job["notes"])}, {sql_lit(job["key"])}
FROM credeals.cre_brokerages b WHERE b.slug='franklin-street'
ON CONFLICT DO NOTHING;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM credeals.cre_scrape_jobs j
  JOIN credeals.cre_brokerages b ON b.id=j.brokerage_id AND b.slug='franklin-street'
  WHERE j.id={sql_lit(job["job_id"])}::uuid AND j.artifact_run_key={sql_lit(job["key"])}
    AND j.status='partial' AND (j.notes={sql_lit(job["notes"])}
        OR j.notes LIKE {sql_lit(job["notes"] + ":%")})) THEN
  RAISE EXCEPTION 'Franklin partial job binding differs';
 END IF;
END $$;
INSERT INTO credeals.cre_listings AS target
 (brokerage_id, external_id, source_url, canonical_url, transaction_type, title, state, lat, lng, address, status, scraped_at, raw_data)
SELECT s.brokerage_id, s.external_id, s.source_url, s.canonical_url, s.transaction_type,
       s.title, s.state, s.lat, s.lng, s.address, s.status, NULL,
       jsonb_build_object('sourceKey', 'franklin-street', 'franklinSitePartialVersion', '1',
                          'franklinSiteObservations', jsonb_build_array(s.observation),
                          'franklinSiteLastObservedAt', s.observation->>'observedAt',
                          'franklinSiteLastGenerationId', s.observation->>'generationId')
FROM _franklin_site s
WHERE NOT EXISTS (SELECT 1 FROM _franklin_site_held h WHERE h.external_id=s.external_id)
ON CONFLICT (brokerage_id, external_id) WHERE external_id IS NOT NULL DO UPDATE SET
 title=COALESCE(target.title, EXCLUDED.title),
 status=COALESCE(target.status, EXCLUDED.status),
 address=CASE WHEN {_location_compatible("target", "EXCLUDED")} THEN COALESCE(target.address, EXCLUDED.address) ELSE target.address END,
 state=CASE WHEN {_location_compatible("target", "EXCLUDED")} THEN COALESCE(target.state, EXCLUDED.state) ELSE target.state END,
 lat=CASE WHEN {_location_compatible("target", "EXCLUDED")} AND target.lat IS NULL AND target.lng IS NULL THEN EXCLUDED.lat ELSE target.lat END,
 lng=CASE WHEN {_location_compatible("target", "EXCLUDED")} AND target.lat IS NULL AND target.lng IS NULL THEN EXCLUDED.lng ELSE target.lng END,
 raw_data=jsonb_set(jsonb_set(CASE WHEN
   target.raw_data->'franklinSiteObservations'->-1 = EXCLUDED.raw_data->'franklinSiteObservations'->0
 THEN target.raw_data ELSE jsonb_set(target.raw_data, '{{franklinSiteObservations}}',
   (target.raw_data->'franklinSiteObservations') || (EXCLUDED.raw_data->'franklinSiteObservations')) END,
   '{{franklinSiteLastObservedAt}}', EXCLUDED.raw_data->'franklinSiteLastObservedAt'),
   '{{franklinSiteLastGenerationId}}', EXCLUDED.raw_data->'franklinSiteLastGenerationId')
WHERE target.deleted_at IS NULL AND target.source_url=EXCLUDED.source_url
  AND target.raw_data->>'franklinSitePartialVersion'='1';
-- Check preservation against the transaction's locked preimage before sealing
-- a receipt of the exact resulting rows, including every historical observation.
DO $$ BEGIN
 IF EXISTS (
   SELECT 1 FROM _franklin_site_before b
   JOIN _franklin_site s ON s.external_id=b.external_id
   JOIN credeals.cre_listings l ON l.brokerage_id=s.brokerage_id AND l.external_id=s.external_id
   WHERE (to_jsonb(l) - ARRAY['title','address','status','state','lat','lng','raw_data','updated_at'])
         IS DISTINCT FROM (b.prior - ARRAY['title','address','status','state','lat','lng','raw_data','updated_at'])
      OR (l.raw_data->'franklinSiteObservations') IS DISTINCT FROM
         CASE WHEN b.prior->'raw_data'->'franklinSiteObservations'->-1 = s.observation
              THEN b.prior->'raw_data'->'franklinSiteObservations'
              ELSE (b.prior->'raw_data'->'franklinSiteObservations') || jsonb_build_array(s.observation) END
      OR (l.raw_data - ARRAY['franklinSiteObservations','franklinSiteLastObservedAt','franklinSiteLastGenerationId'])
         IS DISTINCT FROM ((b.prior->'raw_data') - ARRAY['franklinSiteObservations','franklinSiteLastObservedAt','franklinSiteLastGenerationId'])
 ) THEN RAISE EXCEPTION 'Franklin preimage preservation differs'; END IF;
END $$;
-- Compute expected scalar results from the locked preimage, including
-- incompatible-location preservation, and refuse any different postimage.
DO $$ BEGIN
 IF EXISTS (
   SELECT 1 FROM _franklin_site s
   LEFT JOIN _franklin_site_before b ON b.external_id=s.external_id
   -- Keep typed floats: JSON serialization can round coordinates and make
   -- unchanged stored values fail this exact comparison.
   LEFT JOIN LATERAL (SELECT (b.prior_row).*) p ON true
   LEFT JOIN credeals.cre_listings l ON l.brokerage_id=s.brokerage_id AND l.external_id=s.external_id
   WHERE NOT EXISTS (SELECT 1 FROM _franklin_site_held h WHERE h.external_id=s.external_id)
   AND (l.id IS NULL
     OR l.source_url IS DISTINCT FROM s.source_url OR l.canonical_url IS DISTINCT FROM s.canonical_url
     OR l.title IS DISTINCT FROM COALESCE(p.title, s.title)
     OR l.status IS DISTINCT FROM COALESCE(p.status, s.status)
     OR l.address IS DISTINCT FROM CASE WHEN {_location_compatible("p", "s")} THEN COALESCE(p.address, s.address) ELSE p.address END
     OR l.state IS DISTINCT FROM CASE WHEN {_location_compatible("p", "s")} THEN COALESCE(p.state, s.state) ELSE p.state END
     OR l.lat IS DISTINCT FROM CASE WHEN {_location_compatible("p", "s")} AND p.lat IS NULL AND p.lng IS NULL THEN s.lat ELSE p.lat END
     OR l.lng IS DISTINCT FROM CASE WHEN {_location_compatible("p", "s")} AND p.lat IS NULL AND p.lng IS NULL THEN s.lng ELSE p.lng END
     OR l.raw_data->'franklinSiteObservations'->-1 IS DISTINCT FROM s.observation)
 ) THEN RAISE EXCEPTION 'Franklin expected scalar postimage differs'; END IF;
END $$;
UPDATE credeals.cre_scrape_jobs j
SET notes={sql_lit(job["notes"])} || ':' || (
 SELECT encode(sha256(convert_to(COALESCE(jsonb_agg(to_jsonb(l) ORDER BY s.external_id)::text, '[]'), 'UTF8')), 'hex')
 FROM _franklin_site s
 JOIN credeals.cre_listings l ON l.brokerage_id=s.brokerage_id AND l.external_id=s.external_id
 WHERE NOT EXISTS (SELECT 1 FROM _franklin_site_held h WHERE h.external_id=s.external_id))
WHERE j.id={sql_lit(job["job_id"])}::uuid AND j.notes={sql_lit(job["notes"])};
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM credeals.cre_scrape_jobs j
   WHERE j.id={sql_lit(job["job_id"])}::uuid AND j.notes={sql_lit(job["notes"])} || ':' || (
    SELECT encode(sha256(convert_to(COALESCE(jsonb_agg(to_jsonb(l) ORDER BY s.external_id)::text, '[]'), 'UTF8')), 'hex')
    FROM _franklin_site s
    JOIN credeals.cre_listings l ON l.brokerage_id=s.brokerage_id AND l.external_id=s.external_id
    WHERE NOT EXISTS (SELECT 1 FROM _franklin_site_held h WHERE h.external_id=s.external_id)))
 THEN RAISE EXCEPTION 'Franklin postimage receipt differs'; END IF;
END $$;
SELECT json_build_object('held', COALESCE((SELECT json_agg(external_id ORDER BY external_id)
  FROM _franklin_site_held), '[]'::json), 'total', (SELECT count(*) FROM _franklin_site));
COMMIT;
"""

def readback_sql(listings, held, binding):
    """Build exact generation/card readback for the transaction's unheld partition."""
    staged = stage(listings)
    _partition(staged, held)
    job = _binding(staged, held, binding)
    accepted = [row for row in staged if row["external_id"] not in set(held)]
    payload = sql_lit(json.dumps(accepted, ensure_ascii=False, separators=(",", ":")))
    return f"""
SET TIME ZONE 'UTC';
SET extra_float_digits=3;
WITH expected AS (
 SELECT * FROM jsonb_to_recordset({payload}::jsonb) AS x(
  external_id text, source_url text, canonical_url text, title text, address text, state text,
  lat double precision, lng double precision, status text, observation jsonb)
), invalid AS (
 SELECT e.external_id FROM expected e
 LEFT JOIN credeals.cre_brokerages b ON b.slug='franklin-street'
 LEFT JOIN credeals.cre_listings l ON l.brokerage_id=b.id AND l.external_id=e.external_id
 WHERE l.id IS NULL OR l.deleted_at IS NOT NULL OR l.source_url IS DISTINCT FROM e.source_url
    OR l.canonical_url IS DISTINCT FROM e.canonical_url
    OR (e.status IS NOT NULL AND l.status IS NULL)
    OR (e.title IS NOT NULL AND l.title IS NULL)
    OR (({_location_compatible("l", "e")}) AND
        ((e.address IS NOT NULL AND l.address IS NULL)
         OR (e.state IS NOT NULL AND l.state IS NULL)
         OR (e.lat IS NOT NULL AND e.lng IS NOT NULL AND l.lat IS NULL AND l.lng IS NULL)))
    OR l.raw_data->>'franklinSitePartialVersion' IS DISTINCT FROM '1'
    OR l.raw_data->>'franklinSiteLastObservedAt' IS DISTINCT FROM e.observation->>'observedAt'
    OR l.raw_data->>'franklinSiteLastGenerationId' IS DISTINCT FROM e.observation->>'generationId'
    OR l.raw_data->'franklinSiteObservations'->-1 IS DISTINCT FROM e.observation
)
SELECT json_build_object('expected', (SELECT count(*) FROM expected),
 'invalid', COALESCE((SELECT json_agg(external_id ORDER BY external_id) FROM invalid), '[]'::json),
 'matching_jobs', (SELECT count(*) FROM credeals.cre_scrape_jobs j
   JOIN credeals.cre_brokerages b ON b.id=j.brokerage_id AND b.slug='franklin-street'
   WHERE j.id={sql_lit(job["job_id"])}::uuid AND j.artifact_run_key={sql_lit(job["key"])}
    AND j.status='partial' AND j.notes={sql_lit(job["notes"])} || ':' || (
      SELECT encode(sha256(convert_to(COALESCE(jsonb_agg(to_jsonb(l) ORDER BY e.external_id)::text, '[]'), 'UTF8')), 'hex')
      FROM expected e
      JOIN credeals.cre_brokerages b ON b.slug='franklin-street'
      JOIN credeals.cre_listings l ON l.brokerage_id=b.id AND l.external_id=e.external_id))) AS result;
"""
