"""Validate exact Buildout partition evidence; this does not authorize a write.

The caller must independently bind the artifact, target, owner and attempt and
obtain the receipt and generation rows in a target-bound database snapshot.
This module is not connected to the production runner yet.
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid5


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch(rf"[a-f0-9]{{{length}}}", value) is not None


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def _keys(values):
    if (not isinstance(values, list)
            or any(not isinstance(item, str) or not item for item in values)
            or len(values) != len(set(values))):
        raise ValueError("Buildout partition identities are invalid")
    return set(values)


def _timestamp(value):
    if (not isinstance(value, str) or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value) is None):
        raise ValueError("Buildout observation timestamp is invalid")
    return datetime.fromisoformat(value).astimezone(UTC)


def prepare_expected_bindings(raw, manifest, source, *, producer_sha, target_sha):
    """Rederive staged source URLs and native IDs from the sealed artifact.

    Scope validation binds artifact bytes, source, generation, target and code;
    the caller still authenticates the live owner, target and clean checkout.
    """
    import json

    from cre_buildout_current_inventory import definitions
    from source_fields.buildout import (
        prepare_inventory_observation_scope,
        validate_inventory,
    )

    scope = prepare_inventory_observation_scope(raw, manifest, source, current_code_sha=producer_sha,
                                                current_git_dirty=False, current_target_sha256=target_sha)
    validated = validate_inventory(json.loads(raw), source, definitions()[source],
                                   manifest["run_id"], manifest["started_at"])
    native = {external: (transaction, ids) for external, transaction, ids in validated["staged_bindings"]}
    result = {}
    for external, members in validated["staged"].items():
        # validate_inventory has already merged the transformed first member
        # in place. The original provider rows remain separate and unmodified.
        row = members[0][1]
        transaction, ids = native[external]
        if (row.get("external_id") != external or row.get("transaction_type") != transaction
                or not isinstance(row.get("source_url"), str) or not row["source_url"] or not _keys(ids)):
            raise ValueError("Buildout staged binding differs")
        native_urls = {}
        for payload, _ in members:
            if not isinstance(payload, Mapping) or payload.get("sourceKey") != source:
                raise ValueError("Buildout staged native source differs")
            native_id, url = str(payload.get("id")), payload.get("url")
            if native_id not in ids or native_id in native_urls or not isinstance(url, str) or not url:
                raise ValueError("Buildout staged native URL differs")
            native_urls[native_id] = url
        if set(native_urls) != set(ids):
            raise ValueError("Buildout staged native URL coverage differs")
        _timestamp(row.get("scraped_at"))
        result[external] = {"source_url": row["source_url"], "native_ids": ids,
                            "native_urls": native_urls, "scraped_at": row["scraped_at"]}
    if set(result) != set(scope["staged_external_ids"]):
        raise ValueError("Buildout staged binding coverage differs")
    return scope, result


def _binding(scope, attempt):
    """Validate inputs for the experimental in-transaction receipt builder."""
    from cre_ingest import _RUN_UUID_NAMESPACE, artifact_run_identity_from_digests
    from source_fields.buildout import observation_sources

    if not isinstance(scope, Mapping) or not _hex(attempt, 32):
        raise ValueError("Buildout partition binding is invalid")
    source, generation = scope.get("source_key"), scope.get("generation_id")
    digest, target, producer = (scope.get("artifact_sha256"), scope.get("database_target_sha256"),
                                scope.get("collector_git_sha"))
    if (source not in observation_sources() or not isinstance(generation, str) or not generation
            or not _hex(digest, 64) or not _hex(target, 64) or not _hex(producer, 40)):
        raise ValueError("Buildout partition scope is invalid")
    staged = _keys(scope.get("staged_external_ids"))
    if not staged:
        raise ValueError("Buildout partition has no staged identities")
    artifact_key, _ = artifact_run_identity_from_digests([digest])
    return source, generation, digest, target, producer, staged, artifact_key, str(
        uuid5(_RUN_UUID_NAMESPACE, f"{artifact_key}:{source}")
    )


def replay_fence_sql(sources):
    """Refuse legacy writes of an artifact with a partition receipt."""
    from source_fields.buildout import observation_sources
    from source_fields.buildout_observation_sql import _literal

    sources = frozenset(sources)
    if not sources or not sources <= observation_sources():
        raise ValueError("invalid Buildout partition sources")
    source_sql = ",".join(_literal(source) for source in sorted(sources))
    return f"""
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM _jobmeta jm
   WHERE jm.slug IN ({source_sql}) AND
    (position(E'\\nCRE_BUILDOUT_PARTITION_V1:' in COALESCE(jm.notes,''))>0 OR
     position(E'\\nCRE_BUILDOUT_OBSERVATION_V1:' in COALESCE(jm.notes,''))>0)) THEN
  RAISE EXCEPTION 'Buildout job input contains reserved receipt marker';
 END IF;
 IF EXISTS (SELECT 1 FROM _jobmeta jm JOIN credeals.cre_brokerages b ON b.slug=jm.slug
   JOIN credeals.cre_scrape_jobs j ON j.id=jm.job_id OR
     (j.brokerage_id=b.id AND j.artifact_run_key=jm.artifact_run_key)
   WHERE jm.slug IN ({source_sql})
     AND position(E'\\nCRE_BUILDOUT_PARTITION_V1:' in COALESCE(j.notes,''))>0) THEN
  RAISE EXCEPTION 'Buildout partition receipt requires reconciliation before replay';
 END IF;
END $$;
"""


def experimental_sql(scope, attempt):
    """Build transaction fragments for isolated fixture use, not live admission.

    The caller must independently authenticate scope, target and attempt. This
    experimental lock is table-wide and must be measured and reviewed before
    use. A preexisting artifact job is refused so a changed partition cannot
    silently replay. Transaction-local preimage comparison does not replace
    independent postcommit readback or authorize coordinator continuation.
    """
    from source_fields.buildout_observation_sql import _literal

    source, generation, digest, target, producer, staged, artifact_key, job = _binding(scope, attempt)
    source_sql, job_sql, key_sql = _literal(source), _literal(job), _literal(artifact_key)
    values = ",".join(f"({_literal(key)})" for key in sorted(staged))
    before_job = f"""
SET LOCAL TIME ZONE 'UTC';
SET LOCAL datestyle = 'ISO, YMD';
SET LOCAL extra_float_digits = 3;
SET LOCAL bytea_output = 'hex';
DO $$ DECLARE prior_timeout text := current_setting('lock_timeout'); BEGIN
 IF current_setting('transaction_isolation') <> 'read committed' THEN
   RAISE EXCEPTION 'Buildout partial requires read committed isolation';
 END IF;
 PERFORM set_config('lock_timeout','5s',true);
 LOCK TABLE credeals.cre_brokerages, credeals.cre_listings, credeals.cre_source_index,
   credeals.cre_listing_contacts, credeals.cre_listing_documents, credeals.cre_listing_images,
   credeals.cre_listing_media, credeals.cre_listing_links,
   credeals.cre_scrape_jobs, credeals.cre_listing_events IN SHARE ROW EXCLUSIVE MODE;
 PERFORM set_config('lock_timeout',prior_timeout,true);
 IF EXISTS (SELECT 1 FROM _jobmeta jm WHERE
   position(E'\\nCRE_BUILDOUT_OBSERVATION_V1:' in COALESCE(jm.notes,''))>0 OR
   position(E'\\nCRE_BUILDOUT_PARTITION_V1:' in COALESCE(jm.notes,''))>0) THEN
   RAISE EXCEPTION 'Buildout partial input contains reserved receipt marker';
 END IF;
 IF EXISTS (SELECT 1 FROM credeals.cre_scrape_jobs j
   JOIN credeals.cre_brokerages b ON b.id=j.brokerage_id
   WHERE j.id={job_sql}::uuid OR (b.slug={source_sql} AND j.artifact_run_key={key_sql})) THEN
   RAISE EXCEPTION 'Buildout partial artifact already has a job; reconcile before replay';
 END IF;
END $$;
"""
    before_guard = f"""
CREATE TEMP TABLE _buildout_partition_expected(external_id text PRIMARY KEY) ON COMMIT DROP;
INSERT INTO _buildout_partition_expected VALUES {values};
CREATE TEMP TABLE _buildout_partition_staged ON COMMIT DROP AS
 SELECT brokerage_id, external_id FROM _src WHERE slug={source_sql};
DO $$ BEGIN
 IF EXISTS (SELECT external_id FROM _buildout_partition_expected EXCEPT SELECT external_id FROM _buildout_partition_staged)
 OR EXISTS (SELECT external_id FROM _buildout_partition_staged EXCEPT SELECT external_id FROM _buildout_partition_expected)
 OR (SELECT count(*) FROM _buildout_partition_staged) <> (SELECT count(*) FROM _buildout_partition_expected) THEN
  RAISE EXCEPTION 'Buildout partial staged partition differs';
 END IF;
END $$;
"""
    held_snapshot = """
CREATE TEMP VIEW _buildout_held_snapshot_current AS
WITH related AS (
 SELECT h.brokerage_id,h.external_id FROM _buildout_identity_held h
 UNION
 SELECT other.brokerage_id,other.external_id
 FROM _buildout_identity_held h
 JOIN _buildout_identity_claims own USING (brokerage_id,external_id)
 JOIN _buildout_identity_claims other ON other.brokerage_id=own.brokerage_id
   AND other.native_id=own.native_id AND other.external_id<>own.external_id
)
SELECT r.brokerage_id,r.external_id,to_jsonb(l) AS listing,to_jsonb(si) AS source_index,
 jsonb_build_object(
  'contacts',COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.id) FROM credeals.cre_listing_contacts c WHERE c.listing_id=l.id),'[]'::jsonb),
  'documents',COALESCE((SELECT jsonb_agg(to_jsonb(d) ORDER BY d.id) FROM credeals.cre_listing_documents d WHERE d.listing_id=l.id),'[]'::jsonb),
  'images',COALESCE((SELECT jsonb_agg(to_jsonb(i) ORDER BY i.id) FROM credeals.cre_listing_images i WHERE i.listing_id=l.id),'[]'::jsonb),
  'media',COALESCE((SELECT jsonb_agg(to_jsonb(m) ORDER BY m.id) FROM credeals.cre_listing_media m WHERE m.listing_id=l.id),'[]'::jsonb),
  'links',COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.id) FROM credeals.cre_listing_links x WHERE x.listing_id=l.id),'[]'::jsonb)
 ) AS children
FROM related r LEFT JOIN credeals.cre_listings l USING (brokerage_id,external_id)
LEFT JOIN credeals.cre_source_index si USING (brokerage_id,external_id);
CREATE TEMP TABLE _buildout_held_preimage ON COMMIT DROP AS
 SELECT * FROM _buildout_held_snapshot_current;
"""
    receipt = f"""
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM _buildout_identity_held) THEN
  RAISE EXCEPTION 'experimental Buildout partial requires a held identity';
 END IF;
 IF EXISTS (SELECT brokerage_id,external_id FROM _buildout_partition_staged
            EXCEPT (SELECT brokerage_id,external_id FROM _src
                    UNION SELECT brokerage_id,external_id FROM _buildout_identity_held))
 OR EXISTS ((SELECT brokerage_id,external_id FROM _src
             UNION SELECT brokerage_id,external_id FROM _buildout_identity_held)
            EXCEPT SELECT brokerage_id,external_id FROM _buildout_partition_staged)
 OR EXISTS (SELECT brokerage_id,external_id FROM _src INTERSECT
            SELECT brokerage_id,external_id FROM _buildout_identity_held)
 OR EXISTS (SELECT brokerage_id,external_id FROM _src EXCEPT SELECT brokerage_id,external_id FROM _up)
 OR EXISTS (SELECT brokerage_id,external_id FROM _up EXCEPT SELECT brokerage_id,external_id FROM _src)
 OR (SELECT count(*) FROM _src) <> (SELECT count(*) FROM _up)
 OR EXISTS (SELECT 1 FROM _src s JOIN _up u USING (brokerage_id,external_id)
   LEFT JOIN credeals.cre_source_index si USING (brokerage_id,external_id)
   WHERE si.source_key IS DISTINCT FROM {source_sql} OR si.url IS DISTINCT FROM s.source_url
      OR si.last_enumerated_at IS DISTINCT FROM s.scraped_at OR si.observation_present IS NOT TRUE)
 OR EXISTS (SELECT * FROM _buildout_held_preimage EXCEPT SELECT * FROM _buildout_held_snapshot_current)
 OR EXISTS (SELECT * FROM _buildout_held_snapshot_current EXCEPT SELECT * FROM _buildout_held_preimage) THEN
  RAISE EXCEPTION 'Buildout partial emitted partition differs';
 END IF;
END $$;
DO $$ DECLARE updated_rows bigint; marker text := E'\\nCRE_BUILDOUT_OBSERVATION_V1:';
 evidence jsonb; BEGIN
 SELECT split_part(j.notes,marker,2)::jsonb INTO STRICT evidence
 FROM credeals.cre_scrape_jobs j JOIN credeals.cre_brokerages b ON b.id=j.brokerage_id
 WHERE j.id={job_sql}::uuid AND j.artifact_run_key={key_sql} AND b.slug={source_sql}
   AND (length(j.notes)-length(replace(j.notes,marker,'')))=length(marker);
 IF evidence->>'source' IS DISTINCT FROM {source_sql}
    OR evidence->>'generation' IS DISTINCT FROM {_literal(generation)}
    OR evidence->>'artifact_sha256' IS DISTINCT FROM {_literal(digest)}
    OR (evidence->>'identity_held')::integer IS DISTINCT FROM (SELECT count(*) FROM _buildout_identity_held)
    OR (EXISTS (SELECT 1 FROM _buildout_identity_held) AND (evidence->>'disappeared')::integer IS DISTINCT FROM 0)
    OR (EXISTS (SELECT 1 FROM _buildout_identity_held) AND EXISTS (
        SELECT 1 FROM credeals.cre_listing_events e WHERE e.scrape_job_id={job_sql}::uuid
          AND e.event_type='disappeared'))
    OR EXISTS (SELECT jsonb_array_elements_text(evidence->'held_external_ids')
               EXCEPT SELECT external_id FROM _buildout_identity_held)
    OR EXISTS (SELECT external_id FROM _buildout_identity_held
               EXCEPT SELECT jsonb_array_elements_text(evidence->'held_external_ids')) THEN
   RAISE EXCEPTION 'Buildout partial observation receipt differs';
 END IF;
 UPDATE credeals.cre_scrape_jobs j
 SET status=CASE WHEN EXISTS (SELECT 1 FROM _buildout_identity_held) THEN 'partial' ELSE status END,
     listings_saved=(SELECT count(*) FROM _src),
     notes=replace(COALESCE(j.notes,''), E'\\nCRE_BUILDOUT_OBSERVATION_V1:',
       E'\\nCRE_BUILDOUT_PARTITION_V1:' || jsonb_build_object(
        'schema','buildout-partition-v1','source_key',{source_sql},
        'generation_id',{_literal(generation)},'artifact_sha256',{_literal(digest)},
        'database_target_sha256',{_literal(target)},'collector_git_sha',{_literal(producer)},
        'attempt_id',{_literal(attempt)},'job_id',{job_sql},
        'held',COALESCE((SELECT jsonb_agg(external_id ORDER BY external_id) FROM _buildout_identity_held),'[]'::jsonb),
        'held_related_ids',COALESCE((SELECT jsonb_agg(external_id ORDER BY convert_to(external_id,'UTF8'))
                                   FROM _buildout_held_preimage),'[]'::jsonb),
        'held_preimage_sha256',(SELECT encode(sha256(convert_to(COALESCE(
            jsonb_agg(to_jsonb(p) ORDER BY convert_to(p.external_id,'UTF8')),'[]'::jsonb)::text,'UTF8')),'hex')
            FROM _buildout_held_preimage p),
        'emitted',COALESCE((SELECT jsonb_agg(jsonb_build_object('external_id',s.external_id,'listing_id',u.id::text)
                    ORDER BY s.external_id) FROM _src s JOIN _up u USING (brokerage_id,external_id)),'[]'::jsonb))::text
       || E'\\nCRE_BUILDOUT_OBSERVATION_V1:')
 WHERE j.id={job_sql}::uuid AND j.artifact_run_key={key_sql}
   AND position(E'\\nCRE_BUILDOUT_OBSERVATION_V1:' in COALESCE(j.notes,''))>0
   AND position(E'\\nCRE_BUILDOUT_PARTITION_V1:' in COALESCE(j.notes,''))=0;
 GET DIAGNOSTICS updated_rows = ROW_COUNT;
 IF updated_rows <> 1 THEN
  RAISE EXCEPTION 'Buildout partial receipt was not recorded';
 END IF;
END $$;
DROP VIEW _buildout_held_snapshot_current;
"""
    return before_job, before_guard, held_snapshot, receipt


def experimental_readback_sql(scope, attempt):
    """Build a single-snapshot readback; caller authenticates target.

    This aggregate query is not yet bounded by rows or bytes and is not ready
    for production use on large sources.
    """
    from cre_ingest import source_key_sql
    from cre_validate import FRESHNESS_PAYLOAD_SQL, PIN_STRING_LITERALS
    from source_fields.buildout_observation_sql import _current_raw_sql, _literal

    source, generation, _digest, _target, _producer, _staged, artifact_key, job = _binding(scope, attempt)
    source_sql, generation_sql = _literal(source), _literal(generation)
    raw = _current_raw_sql("l.raw_data")
    return f"""
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
{PIN_STRING_LITERALS}
SET LOCAL TIME ZONE 'UTC'; SET LOCAL datestyle='ISO, YMD';
SET LOCAL extra_float_digits=3; SET LOCAL bytea_output='hex'; SET LOCAL statement_timeout='30s';
WITH job AS MATERIALIZED (
 SELECT j.*, b.slug,
  split_part(split_part(j.notes,E'\\nCRE_BUILDOUT_PARTITION_V1:',2),E'\\nCRE_BUILDOUT_OBSERVATION_V1:',1)::jsonb AS receipt,
  (length(j.notes)-length(replace(j.notes,E'\\nCRE_BUILDOUT_PARTITION_V1:','')))
    /length(E'\\nCRE_BUILDOUT_PARTITION_V1:') AS partition_markers,
  (length(j.notes)-length(replace(j.notes,E'\\nCRE_BUILDOUT_OBSERVATION_V1:','')))
    /length(E'\\nCRE_BUILDOUT_OBSERVATION_V1:') AS observation_markers
 FROM credeals.cre_scrape_jobs j JOIN credeals.cre_brokerages b ON b.id=j.brokerage_id
 WHERE j.id={_literal(job)}::uuid AND j.artifact_run_key={_literal(artifact_key)} AND b.slug={source_sql}
), related AS (
 SELECT j.brokerage_id,item.value AS external_id FROM job j
 CROSS JOIN LATERAL jsonb_array_elements_text(j.receipt->'held_related_ids') item(value)
), held_preimage AS (
 SELECT r.brokerage_id,r.external_id,to_jsonb(l) AS listing,to_jsonb(si) AS source_index,
  jsonb_build_object(
   'contacts',COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.id) FROM credeals.cre_listing_contacts c WHERE c.listing_id=l.id),'[]'::jsonb),
   'documents',COALESCE((SELECT jsonb_agg(to_jsonb(d) ORDER BY d.id) FROM credeals.cre_listing_documents d WHERE d.listing_id=l.id),'[]'::jsonb),
   'images',COALESCE((SELECT jsonb_agg(to_jsonb(i) ORDER BY i.id) FROM credeals.cre_listing_images i WHERE i.listing_id=l.id),'[]'::jsonb),
   'media',COALESCE((SELECT jsonb_agg(to_jsonb(m) ORDER BY m.id) FROM credeals.cre_listing_media m WHERE m.listing_id=l.id),'[]'::jsonb),
   'links',COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.id) FROM credeals.cre_listing_links x WHERE x.listing_id=l.id),'[]'::jsonb)
  ) AS children
 FROM related r LEFT JOIN credeals.cre_listings l USING (brokerage_id,external_id)
 LEFT JOIN credeals.cre_source_index si ON si.brokerage_id=r.brokerage_id AND si.external_id=r.external_id
), generation_rows AS (
 SELECT l.external_id,l.id::text AS listing_id,l.source_url,
  COALESCE((SELECT jsonb_agg(member.value->>'id' ORDER BY member.value->>'id')
    FROM jsonb_array_elements(CASE WHEN {raw} ? 'primary' AND {raw} ? 'secondary_pass'
      THEN jsonb_build_array({raw}->'primary',{raw}->'secondary_pass')
      ELSE jsonb_build_array({raw}) END) member(value)),'[]'::jsonb) AS native_ids,
  COALESCE((SELECT jsonb_agg(jsonb_build_object('id',member.value->>'id','url',member.value->>'url',
      'source',member.value->>'sourceKey','generation',member.value->'freshnessProvenance'->>'generationId')
      ORDER BY member.value->>'id')
    FROM jsonb_array_elements(CASE WHEN {raw} ? 'primary' AND {raw} ? 'secondary_pass'
      THEN jsonb_build_array({raw}->'primary',{raw}->'secondary_pass')
      ELSE jsonb_build_array({raw}) END) member(value)),'[]'::jsonb) AS native_members,
  COALESCE(jsonb_typeof({raw})='object' AND (
    ({raw} ? 'primary' AND {raw} ? 'secondary_pass' AND ({raw}-'primary'-'secondary_pass')='{{}}'::jsonb)
    OR (NOT ({raw} ? 'primary') AND NOT ({raw} ? 'secondary_pass'))),false) AS raw_shape_valid,
  persisted.payload->'freshnessProvenance'->>'generationId' AS generation_id,
  {source_key_sql('decoded')} AS row_source,si.source_key AS index_source,si.url AS index_url,
  si.observation_present AS index_present,
  to_char(si.last_enumerated_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS index_at
 FROM credeals.cre_listings l JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id
 CROSS JOIN LATERAL (SELECT l.external_id,l.raw_data #> '{{}}'::text[] AS raw_data OFFSET 0) decoded
 CROSS JOIN LATERAL (SELECT {FRESHNESS_PAYLOAD_SQL} AS payload OFFSET 0) persisted
 LEFT JOIN credeals.cre_source_index si ON si.brokerage_id=l.brokerage_id AND si.external_id=l.external_id
 WHERE b.slug={source_sql} AND l.deleted_at IS NULL
   AND persisted.payload->'freshnessProvenance'->>'generationId'={generation_sql}
)
SELECT jsonb_build_object('receipt',j.receipt,'snapshot',jsonb_build_object(
 'job_status',j.status,'job_saved',j.listings_saved,
 'partition_markers',j.partition_markers,'observation_markers',j.observation_markers,
 'disappeared_events',(SELECT count(*) FROM credeals.cre_listing_events e
                       WHERE e.scrape_job_id=j.id AND e.event_type='disappeared'),
 'held_preimage_sha256',(SELECT encode(sha256(convert_to(COALESCE(
   jsonb_agg(to_jsonb(p) ORDER BY convert_to(p.external_id,'UTF8')),'[]'::jsonb)::text,'UTF8')),'hex')
   FROM held_preimage p),
 'rows',COALESCE((SELECT jsonb_agg(to_jsonb(g) ORDER BY g.external_id) FROM generation_rows g),'[]'::jsonb)
)) AS result FROM job j;
ROLLBACK;
"""


def validate_partition_consistency(scope, attempt, receipt, generation_rows):
    """Compare supplied expected, held and emitted identities, not only counts.

    ``generation_rows`` must contain every row for the source and generation,
    from the same independent snapshot as the stored receipt. This function
    checks consistency only: it does not authenticate the inputs or prove a
    commit, generation freshness, unchanged held data or safe write settlement.
    An empty emitted subset is reported as all-held, never as source coverage.
    """
    source, generation, digest, target, producer, staged, _artifact_key, expected_job = _binding(scope, attempt)
    fields = {"schema", "source_key", "generation_id", "artifact_sha256", "database_target_sha256",
              "collector_git_sha", "attempt_id", "job_id", "held", "held_related_ids",
              "held_preimage_sha256", "emitted"}
    if (not isinstance(receipt, Mapping) or set(receipt) != fields
            or receipt["schema"] != "buildout-partition-v1" or receipt["source_key"] != source
            or receipt["generation_id"] != generation or receipt["artifact_sha256"] != digest
            or receipt["database_target_sha256"] != target or receipt["collector_git_sha"] != producer
            or receipt["attempt_id"] != attempt or receipt["job_id"] != expected_job):
        raise ValueError("Buildout partition receipt binding differs")
    held = _keys(receipt["held"])
    if not held:
        raise ValueError("Buildout partition is not a held partial")
    related = _keys(receipt["held_related_ids"])
    if not held <= related or not _hex(receipt["held_preimage_sha256"], 64):
        raise ValueError("Buildout held preimage binding differs")
    emitted_rows = receipt["emitted"]
    if not isinstance(emitted_rows, list) or not isinstance(generation_rows, list):
        raise TypeError("Buildout partition readback is invalid")

    def index(rows):
        result, parent_ids = {}, set()
        for row in rows:
            if (not isinstance(row, Mapping) or set(row) != {"external_id", "listing_id"}
                    or not isinstance(row["external_id"], str) or not row["external_id"]
                    or not _uuid(row["listing_id"]) or row["external_id"] in result
                    or row["listing_id"] in parent_ids):
                raise ValueError("Buildout partition row identity is invalid")
            result[row["external_id"]] = row["listing_id"]
            parent_ids.add(row["listing_id"])
        return result

    emitted, observed = index(emitted_rows), index(generation_rows)
    if held & emitted.keys() or held | emitted.keys() != staged or emitted != observed:
        raise ValueError("Buildout partition or generation identities differ")
    return {"outcome": "all_held" if not emitted else "partition_consistent",
            "source_key": source, "generation_id": generation, "attempt_id": attempt,
            "staged_count": len(staged), "emitted_count": len(emitted), "held": sorted(held),
            "whole_source_coverage": False}


def validate_partial_readback(scope, attempt, expected_bindings, receipt, snapshot):
    """Validate a caller-authenticated, single-snapshot partial readback.

    Expected bindings must be independently rederived from the sealed artifact;
    the caller must authenticate the database target, owner and snapshot. A
    mismatch may be concurrent postcommit drift and is not proof of rollback.
    """
    if not isinstance(expected_bindings, Mapping) or not isinstance(snapshot, Mapping):
        raise TypeError("Buildout partial readback input is invalid")
    staged = _keys(scope.get("staged_external_ids"))
    if set(expected_bindings) != staged:
        raise ValueError("Buildout expected bindings differ from staging")
    for key, value in expected_bindings.items():
        if (not isinstance(value, Mapping) or set(value) != {"source_url", "native_ids", "native_urls", "scraped_at"}
                or not isinstance(value["source_url"], str) or not value["source_url"]):
            raise ValueError(f"Buildout expected binding invalid: {key}")
        _timestamp(value["scraped_at"])
        native_ids = _keys(value["native_ids"])
        if (not native_ids or not isinstance(value["native_urls"], Mapping)
                or set(value["native_urls"]) != native_ids
                or any(not isinstance(url, str) or not url for url in value["native_urls"].values())):
            raise ValueError("Buildout expected native IDs missing")
    fields = {"job_status", "job_saved", "disappeared_events", "held_preimage_sha256", "rows",
              "partition_markers", "observation_markers"}
    if set(snapshot) != fields or not isinstance(snapshot["rows"], list):
        raise ValueError("Buildout partial snapshot shape differs")
    simplified = []
    for row in snapshot["rows"]:
        expected_fields = {"external_id", "listing_id", "source_url", "native_ids", "native_members",
                           "raw_shape_valid", "generation_id", "row_source",
                           "index_source", "index_url", "index_present", "index_at"}
        if not isinstance(row, Mapping) or set(row) != expected_fields:
            raise ValueError("Buildout partial snapshot row is invalid")
        expected = expected_bindings.get(row["external_id"])
        members = row["native_members"]
        if (not isinstance(expected, Mapping) or row["source_url"] != expected["source_url"]
                or _keys(row["native_ids"]) != _keys(expected["native_ids"])
                or row["raw_shape_valid"] is not True or not isinstance(members, list)
                or len(members) != len(expected["native_ids"])
                or row["generation_id"] != scope["generation_id"]
                or row["row_source"] != scope["source_key"]
                or row["index_source"] != scope["source_key"] or row["index_url"] != expected["source_url"]
                or row["index_present"] is not True
                or _timestamp(row["index_at"]) != _timestamp(expected["scraped_at"])):
            raise ValueError("Buildout partial source, identity or index differs")
        actual_native = {}
        for member in members:
            if (not isinstance(member, Mapping) or set(member) != {"id", "url", "source", "generation"}
                    or member["source"] != scope["source_key"] or member["generation"] != scope["generation_id"]
                    or not isinstance(member["id"], str) or member["id"] in actual_native):
                raise ValueError("Buildout partial native member differs")
            actual_native[member["id"]] = member["url"]
        if actual_native != expected["native_urls"]:
            raise ValueError("Buildout partial native URL differs")
        simplified.append({"external_id": row["external_id"], "listing_id": row["listing_id"]})
    result = validate_partition_consistency(scope, attempt, receipt, simplified)
    if (snapshot["job_status"] != "partial" or type(snapshot["job_saved"]) is not int
            or snapshot["job_saved"] != result["emitted_count"]
            or type(snapshot["disappeared_events"]) is not int or snapshot["disappeared_events"] != 0
            or type(snapshot["partition_markers"]) is not int or snapshot["partition_markers"] != 1
            or type(snapshot["observation_markers"]) is not int or snapshot["observation_markers"] != 1
            or snapshot["held_preimage_sha256"] != receipt["held_preimage_sha256"]):
        raise ValueError("Buildout partial job, events or held preimage differs")
    return result


def experimental_readback(artifact_path, manifest_path, manifest_sha, source, producer, target, attempt, env_file):
    """Read one exact target-bound snapshot; caller must authenticate owner.

    This is not connected to the runner. Failure can indicate postcommit drift
    or an unknown outcome; it must never be interpreted as an exact rollback.
    """
    import hashlib
    import json

    from cre_checkpoint_refresh import git_identity
    from cre_code_handoff_git import read_file
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_validate import _run_psql_script, parse_tsv

    if not _hex(manifest_sha, 64) or not _hex(producer, 40) or not _hex(target, 64):
        raise ValueError("Buildout readback binding is invalid")
    manifest_raw, artifact_raw = read_file(manifest_path), read_file(artifact_path)
    if hashlib.sha256(manifest_raw).hexdigest() != manifest_sha or git_identity() != (producer, False):
        raise ValueError("Buildout readback inputs changed")

    def unique_object(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate Buildout readback input field")
            result[key] = value
        return result

    manifest = json.loads(manifest_raw, object_pairs_hook=unique_object)
    scope, expected = prepare_expected_bindings(artifact_raw, manifest, source,
                                                producer_sha=producer, target_sha=target)
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != target:
        raise ValueError("Buildout readback target changed")
    query = experimental_readback_sql(scope, attempt)
    process = _run_psql_script(find_psql(), db_url, query, timeout=45)
    if process.returncode:
        raise ValueError("Buildout partial readback query failed")
    rows = parse_tsv(process.stdout)
    if len(rows) != 1 or set(rows[0]) != {"result"}:
        raise ValueError("Buildout partial job is missing or ambiguous")
    data = json.loads(rows[0]["result"], object_pairs_hook=unique_object)
    if not isinstance(data, dict) or set(data) != {"receipt", "snapshot"}:
        raise ValueError("Buildout partial readback shape differs")
    result = validate_partial_readback(scope, attempt, expected, data["receipt"], data["snapshot"])
    current_url, _ = load_db_url(env_file)
    if (database_target_fingerprint_from_url(current_url).get("value") != target
            or git_identity() != (producer, False) or read_file(artifact_path) != artifact_raw
            or read_file(manifest_path) != manifest_raw):
        raise ValueError("Buildout readback inputs or target changed")
    return result
