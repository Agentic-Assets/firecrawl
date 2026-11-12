"""Synthetic atomic enrichment partition: research fixture only, never runtime.

The boolean detail witness is deliberately a stand-in. It is not an authenticated
source response and this module MUST NOT be used for production admission.
"""
import hashlib
import json
import uuid


def build_sql(claims, attempt_id):
    if not isinstance(claims, list) or not claims or len(claims) > 20:
        raise ValueError("fixture requires one to twenty claims")
    attempt_id = str(uuid.UUID(attempt_id))
    raw = json.dumps(claims, sort_keys=True, separators=(",", ":"))
    request_hash = hashlib.sha256(raw.encode()).hexdigest()
    escaped = "'" + raw.replace("'", "''") + "'"
    return r"""
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL standard_conforming_strings=on;
SET LOCAL lock_timeout='2s';
SET LOCAL statement_timeout='10s';
SELECT pg_advisory_xact_lock(1687068469);
SELECT pg_advisory_xact_lock(hashtextextended('credeals:listing-lifecycle:v1',0));
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM credeals.fixture_atomic_receipts WHERE attempt_id='ATTEMPT'::uuid)
 THEN RAISE EXCEPTION 'fixture attempt already committed; read receipt'; END IF;
END $$;
CREATE TEMP TABLE requested ON COMMIT DROP AS
SELECT * FROM jsonb_to_recordset(ARTIFACT::jsonb) AS x(
 claim_id uuid,brokerage_id uuid,source_key text,external_id text,url text,reason text,
 claimed_at timestamptz,enqueued_at timestamptz,attempts integer,
 detail_verified boolean,observed_native_id text,title text);
DO $$ BEGIN
 IF (SELECT count(*) FROM requested) != (SELECT count(DISTINCT claim_id) FROM requested)
    OR EXISTS (SELECT 1 FROM requested WHERE claim_id IS NULL OR brokerage_id IS NULL
      OR source_key IS NULL OR external_id IS NULL OR url IS NULL OR reason IS NULL OR claimed_at IS NULL
      OR enqueued_at IS NULL OR attempts IS NULL OR detail_verified IS NULL
      OR title IS NULL OR NOT isfinite(claimed_at) OR NOT isfinite(enqueued_at))
 THEN RAISE EXCEPTION 'invalid synthetic claim artifact'; END IF;
END $$;
CREATE TEMP TABLE queue_locks ON COMMIT DROP AS
 SELECT q.id FROM credeals.cre_enrichment_queue q JOIN requested r ON r.claim_id=q.id
 ORDER BY q.id FOR UPDATE OF q;
CREATE TEMP TABLE index_locks ON COMMIT DROP AS
 SELECT s.brokerage_id,s.external_id FROM credeals.cre_source_index s
 JOIN requested r ON r.brokerage_id=s.brokerage_id AND r.external_id=s.external_id
 ORDER BY s.brokerage_id,s.external_id FOR UPDATE OF s;
CREATE TEMP TABLE listing_locks ON COMMIT DROP AS
 SELECT l.brokerage_id,l.external_id FROM credeals.cre_listings l
 JOIN requested r ON r.brokerage_id=l.brokerage_id AND r.external_id=l.external_id
 ORDER BY l.brokerage_id,l.external_id FOR UPDATE OF l;
CREATE TEMP TABLE decision_clock ON COMMIT DROP AS SELECT clock_timestamp() AS checked_at;
CREATE TEMP TABLE decision ON COMMIT DROP AS
 SELECT r.claim_id,CASE
  WHEN q.id IS NULL OR q.brokerage_id IS DISTINCT FROM r.brokerage_id
   OR q.source_key IS DISTINCT FROM r.source_key OR q.external_id IS DISTINCT FROM r.external_id
   OR q.url IS DISTINCT FROM r.url OR q.reason IS DISTINCT FROM r.reason OR q.claimed_at IS DISTINCT FROM r.claimed_at
   OR q.enqueued_at IS DISTINCT FROM r.enqueued_at OR q.attempts IS DISTINCT FROM r.attempts
   OR q.done_at IS NOT NULL THEN 'superseded'
  WHEN q.claimed_at > (SELECT checked_at FROM decision_clock)
   OR q.claimed_at <= (SELECT checked_at FROM decision_clock)-interval '1 hour'
   THEN 'unverified'
  WHEN q.attempts < 0 OR q.attempts >= 5
   OR (SELECT count(*) FROM requested dups WHERE dups.brokerage_id=r.brokerage_id
          AND dups.external_id=r.external_id)>1
   OR (SELECT count(*) FROM credeals.cre_enrichment_queue others
       WHERE others.brokerage_id=r.brokerage_id AND others.external_id=r.external_id
         AND others.done_at IS NULL)>1
   OR NOT EXISTS (SELECT 1 FROM credeals.fixture_source_map m
                  WHERE m.brokerage_id=r.brokerage_id AND m.source_key=r.source_key)
   OR NOT r.detail_verified OR r.observed_native_id IS DISTINCT FROM r.external_id
   OR l.external_id IS NULL OR l.source_url IS DISTINCT FROM r.url OR l.status IS DISTINCT FROM 'active'
   OR l.deleted_at IS NOT NULL OR s.external_id IS NULL OR s.source_key IS DISTINCT FROM r.source_key
   OR s.url IS DISTINCT FROM r.url OR s.observation_present IS DISTINCT FROM true
   OR s.soft_deleted IS DISTINCT FROM false
   OR EXISTS (SELECT 1 FROM credeals.cre_listings other
              WHERE other.brokerage_id=r.brokerage_id AND other.source_url=r.url
                AND other.external_id IS DISTINCT FROM r.external_id)
   OR EXISTS (SELECT 1 FROM credeals.cre_source_index other
              WHERE other.brokerage_id=r.brokerage_id AND other.url=r.url
                AND other.external_id IS DISTINCT FROM r.external_id)
   THEN 'held'
  ELSE 'accepted' END AS outcome
 FROM requested r LEFT JOIN credeals.cre_enrichment_queue q ON q.id=r.claim_id
 LEFT JOIN credeals.cre_listings l ON l.brokerage_id=r.brokerage_id AND l.external_id=r.external_id
 LEFT JOIN credeals.cre_source_index s ON s.brokerage_id=r.brokerage_id AND s.external_id=r.external_id;
CREATE TEMP TABLE applied ON COMMIT DROP AS WITH writes AS (
 UPDATE credeals.cre_listings l SET title=r.title,write_count=write_count+1
 FROM requested r JOIN decision d USING (claim_id)
 WHERE d.outcome='accepted' AND l.brokerage_id=r.brokerage_id AND l.external_id=r.external_id
 RETURNING r.claim_id
) SELECT claim_id FROM writes;
CREATE TEMP TABLE completed ON COMMIT DROP AS WITH removals AS (
 DELETE FROM credeals.cre_enrichment_queue q USING decision d
 WHERE d.outcome='accepted' AND q.id=d.claim_id RETURNING q.id AS claim_id
) SELECT claim_id FROM removals;
CREATE TEMP TABLE deferred ON COMMIT DROP AS WITH cooldown AS (
 UPDATE credeals.cre_enrichment_queue q
 SET claimed_at=GREATEST(clock_timestamp(),q.claimed_at+interval '1 microsecond')
 FROM decision d WHERE d.outcome='held' AND q.id=d.claim_id RETURNING q.id AS claim_id
) SELECT claim_id FROM cooldown;
DO $$ BEGIN
 IF EXISTS ((SELECT claim_id FROM decision WHERE outcome='accepted' EXCEPT SELECT claim_id FROM applied)
     UNION ALL (SELECT claim_id FROM applied EXCEPT SELECT claim_id FROM decision WHERE outcome='accepted')
     UNION ALL (SELECT claim_id FROM decision WHERE outcome='accepted' EXCEPT SELECT claim_id FROM completed)
     UNION ALL (SELECT claim_id FROM completed EXCEPT SELECT claim_id FROM decision WHERE outcome='accepted')
     UNION ALL (SELECT claim_id FROM decision WHERE outcome='held' EXCEPT SELECT claim_id FROM deferred)
     UNION ALL (SELECT claim_id FROM deferred EXCEPT SELECT claim_id FROM decision WHERE outcome='held'))
 THEN RAISE EXCEPTION 'fixture actual write/disposition differs from decision'; END IF;
END $$;
INSERT INTO credeals.fixture_atomic_receipts(attempt_id,request_hash,decisions)
 SELECT 'ATTEMPT'::uuid,'HASH',jsonb_object_agg(claim_id::text,outcome) FROM decision;
COMMIT;
""".replace("ATTEMPT", attempt_id).replace("HASH", request_hash).replace("ARTIFACT", escaped)
