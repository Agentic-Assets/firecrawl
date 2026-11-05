"""Offline SQL planner for source-qualified Franklin site-card observations.

This is not a writer or an admission gate. A future refresh owner must rederive
and seal the projection from the retained artifact, bind code/generation/target,
and reconcile uncertain commits before invoking this plan.
"""
import json
import re
from urllib.parse import urlsplit

from cre_ingest import (
    lifecycle_transaction_lock_sql,
    parse_strict_freshness_timestamp,
    sql_lit,
    to_row,
)

_ID = re.compile(r"franklin-site:(sale|lease):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\Z")
_PATH = re.compile(r"/properties/for-(sale|lease)/[a-z0-9]+(?:[-a-z0-9]*[a-z0-9])?/?\Z")
MAX_CARD_BYTES = 65536
MAX_OBSERVATIONS = 128


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
                or len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_CARD_BYTES
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
        staged.append({key: row.get(key) for key in (
            "external_id", "source_url", "canonical_url", "transaction_type",
            "title", "state", "lat", "lng",
        )} | {"observation": {**observed, "generationId": item["franklinSiteGenerationId"]}})
    if len(generations) != 1:
        raise ValueError("mixed Franklin site generations")
    return staged


def build_sql(listings):
    """Create an additive, source-local transaction; callers own its recovery."""
    staged = stage(listings)
    payload = sql_lit(json.dumps(staged, ensure_ascii=False, separators=(",", ":")))
    return f"""
BEGIN;
SET LOCAL statement_timeout='600s';
{lifecycle_transaction_lock_sql()}
CREATE TEMP TABLE _franklin_site ON COMMIT DROP AS
SELECT b.id AS brokerage_id, x.*
FROM jsonb_to_recordset({payload}::jsonb) AS x(
 external_id text, source_url text, canonical_url text, transaction_type text,
 title text, state text, lat double precision, lng double precision, observation jsonb)
JOIN credeals.cre_brokerages b ON b.slug='franklin-street';
DO $$ BEGIN
 IF (SELECT count(*) FROM _franklin_site) <> {len(staged)} THEN
   RAISE EXCEPTION 'Franklin source brokerage missing';
 END IF;
END $$;
-- Serialize against both same-ID and same-URL parents under the global lock.
CREATE TEMP TABLE _franklin_site_locks ON COMMIT DROP AS
SELECT l.id FROM credeals.cre_listings l
JOIN _franklin_site s ON s.brokerage_id=l.brokerage_id
 AND (s.external_id=l.external_id OR s.source_url=l.source_url)
ORDER BY l.id FOR UPDATE OF l;
CREATE TEMP TABLE _franklin_site_held ON COMMIT DROP AS
SELECT s.external_id FROM _franklin_site s
LEFT JOIN credeals.cre_listings own ON own.brokerage_id=s.brokerage_id AND own.external_id=s.external_id
WHERE (own.id IS NOT NULL AND (own.deleted_at IS NOT NULL OR own.source_url IS DISTINCT FROM s.source_url
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
           OR (jsonb_array_length(own.raw_data->'franklinSiteObservations') >= {MAX_OBSERVATIONS}
               AND own.raw_data->'franklinSiteObservations'->-1->'card' IS DISTINCT FROM s.observation->'card'))
         ELSE false END))
 OR EXISTS (SELECT 1 FROM credeals.cre_listings other
            WHERE other.brokerage_id=s.brokerage_id AND other.source_url=s.source_url
              AND other.deleted_at IS NULL AND (own.id IS NULL OR other.id<>own.id));
INSERT INTO credeals.cre_listings AS target
 (brokerage_id, external_id, source_url, canonical_url, transaction_type, title, state, lat, lng, scraped_at, raw_data)
SELECT s.brokerage_id, s.external_id, s.source_url, s.canonical_url, s.transaction_type,
       s.title, s.state, s.lat, s.lng, NULL,
       jsonb_build_object('sourceKey', 'franklin-street', 'franklinSitePartialVersion', '1',
                          'franklinSiteObservations', jsonb_build_array(s.observation),
                          'franklinSiteLastObservedAt', s.observation->>'observedAt',
                          'franklinSiteLastGenerationId', s.observation->>'generationId')
FROM _franklin_site s
WHERE NOT EXISTS (SELECT 1 FROM _franklin_site_held h WHERE h.external_id=s.external_id)
ON CONFLICT (brokerage_id, external_id) WHERE external_id IS NOT NULL DO UPDATE SET
 title=COALESCE(target.title, EXCLUDED.title),
 state=COALESCE(target.state, EXCLUDED.state),
 lat=CASE WHEN target.lat IS NULL AND target.lng IS NULL THEN EXCLUDED.lat ELSE target.lat END,
 lng=CASE WHEN target.lat IS NULL AND target.lng IS NULL THEN EXCLUDED.lng ELSE target.lng END,
 raw_data=jsonb_set(jsonb_set(CASE WHEN
   target.raw_data->'franklinSiteObservations'->-1->'card' = EXCLUDED.raw_data->'franklinSiteObservations'->0->'card'
 THEN target.raw_data ELSE jsonb_set(target.raw_data, '{{franklinSiteObservations}}',
   (target.raw_data->'franklinSiteObservations') || (EXCLUDED.raw_data->'franklinSiteObservations')) END,
   '{{franklinSiteLastObservedAt}}', EXCLUDED.raw_data->'franklinSiteLastObservedAt'),
   '{{franklinSiteLastGenerationId}}', EXCLUDED.raw_data->'franklinSiteLastGenerationId')
WHERE target.deleted_at IS NULL AND target.source_url=EXCLUDED.source_url
  AND target.raw_data->>'franklinSitePartialVersion'='1';
SELECT json_build_object('held', COALESCE((SELECT json_agg(external_id ORDER BY external_id)
  FROM _franklin_site_held), '[]'::json), 'total', (SELECT count(*) FROM _franklin_site));
COMMIT;
"""

def readback_sql(listings, held):
    """Build exact generation/card readback for the transaction's unheld partition."""
    staged = stage(listings)
    identities = {row["external_id"] for row in staged}
    if (not isinstance(held, list) or held != sorted(set(held))
            or not set(held) <= identities):
        raise ValueError("invalid Franklin held partition")
    accepted = [row for row in staged if row["external_id"] not in set(held)]
    payload = sql_lit(json.dumps(accepted, ensure_ascii=False, separators=(",", ":")))
    return f"""
WITH expected AS (
 SELECT * FROM jsonb_to_recordset({payload}::jsonb) AS x(
  external_id text, source_url text, observation jsonb)
), invalid AS (
 SELECT e.external_id FROM expected e
 LEFT JOIN credeals.cre_brokerages b ON b.slug='franklin-street'
 LEFT JOIN credeals.cre_listings l ON l.brokerage_id=b.id AND l.external_id=e.external_id
 WHERE l.id IS NULL OR l.deleted_at IS NOT NULL OR l.source_url IS DISTINCT FROM e.source_url
    OR l.raw_data->>'franklinSitePartialVersion' IS DISTINCT FROM '1'
    OR l.raw_data->>'franklinSiteLastObservedAt' IS DISTINCT FROM e.observation->>'observedAt'
    OR l.raw_data->>'franklinSiteLastGenerationId' IS DISTINCT FROM e.observation->>'generationId'
    OR l.raw_data->'franklinSiteObservations'->-1->'card' IS DISTINCT FROM e.observation->'card'
)
SELECT json_build_object('expected', (SELECT count(*) FROM expected),
 'invalid', COALESCE((SELECT json_agg(external_id ORDER BY external_id) FROM invalid), '[]'::json));
"""
