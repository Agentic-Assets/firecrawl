"""Validate exact Buildout partition evidence; this does not authorize a write.

The caller must independently bind the artifact, target, owner and attempt and
obtain the receipt and generation rows in a target-bound database snapshot.
This module is not connected to the production runner yet.
"""

import re
from collections.abc import Mapping
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
              "collector_git_sha", "attempt_id", "job_id", "held", "emitted"}
    if (not isinstance(receipt, Mapping) or set(receipt) != fields
            or receipt["schema"] != "buildout-partition-v1" or receipt["source_key"] != source
            or receipt["generation_id"] != generation or receipt["artifact_sha256"] != digest
            or receipt["database_target_sha256"] != target or receipt["collector_git_sha"] != producer
            or receipt["attempt_id"] != attempt or receipt["job_id"] != expected_job):
        raise ValueError("Buildout partition receipt binding differs")
    held = _keys(receipt["held"])
    if not held:
        raise ValueError("Buildout partition is not a held partial")
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
