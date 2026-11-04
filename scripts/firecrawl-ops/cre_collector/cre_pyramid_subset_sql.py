"""Transaction-local Pyramid partial-subset preconditions; no lifecycle authority."""

import hashlib
import json
import re


def valid_native_id_sql(value):
    return f"""CASE WHEN {value} ~ '^[1-9][0-9]{{0,15}}$'
                 THEN {value}::numeric <= 9007199254740991 ELSE false END"""


def strong_facts_sql(incoming, historical):
    fields = ("title", "address", "city", "state", "transaction_type", "description")
    return " AND ".join(
        f"{incoming}.{field} IS NOT NULL AND btrim({incoming}.{field}) <> '' "
        f"AND {incoming}.{field} = {historical}.{field}" for field in fields
    )


def current_index_sql(brokerage, external_id, source_url):
    return f"""(SELECT count(*) = 1 AND bool_and(
         (i.source_key = 'pyramid-brokerage' AND i.url = {source_url}
          AND i.observation_present IS TRUE AND i.soft_deleted IS FALSE) IS TRUE)
       FROM credeals.cre_source_index i
       WHERE i.brokerage_id = {brokerage} AND i.external_id = {external_id})"""


def unclassified_complete_guard_sql():
    """Refuse new native-ID contradictions when no bound subset was sealed."""
    valid = valid_native_id_sql("s.native_ids[1]")
    return f"""
-- A previously clear complete-source classification may become stale. Without
-- sealed transition claims, do not replace children after an ID change.
DO $$
BEGIN
  IF EXISTS (
    WITH incoming AS MATERIALIZED (
      SELECT s.brokerage_id, s.external_id, s.source_url,
             ARRAY(SELECT DISTINCT n.value #>> '{{}}' FROM
               jsonb_path_query(s.raw_data, '$.**.wordpressRecord.id') n(value)) native_ids
      FROM _src s WHERE jsonb_path_exists(s.raw_data, '$.**.sourceKey ? (@ == "pyramid-brokerage")')
    )
    SELECT 1 FROM incoming s
    WHERE cardinality(s.native_ids) > 0 AND (
      cardinality(s.native_ids) <> 1 OR NOT ({valid}) OR EXISTS (
        SELECT 1 FROM credeals.cre_source_index i WHERE i.brokerage_id=s.brokerage_id AND (
          (i.url=s.source_url AND i.external_id IS DISTINCT FROM s.external_id)
          OR (i.external_id=s.external_id AND (i.url IS DISTINCT FROM s.source_url
              OR i.source_key IS DISTINCT FROM 'pyramid-brokerage'
              OR i.soft_deleted IS DISTINCT FROM false OR i.observation_present IS DISTINCT FROM true))
        )
      ) OR EXISTS (
        SELECT 1 FROM credeals.cre_listings t
        WHERE t.brokerage_id=s.brokerage_id AND (
          (t.external_id=s.external_id AND (t.deleted_at IS NOT NULL OR t.status::text IS DISTINCT FROM 'active'))
          OR (t.source_url=s.source_url AND t.external_id=s.external_id AND EXISTS (
             SELECT 1 FROM jsonb_path_query(t.raw_data, '$.**.wordpressRecord.id') n(value)
             WHERE n.value #>> '{{}}' IS DISTINCT FROM s.native_ids[1]))
          OR ((t.source_url IS DISTINCT FROM s.source_url OR t.external_id IS DISTINCT FROM s.external_id)
              AND EXISTS (SELECT 1 FROM jsonb_path_query(t.raw_data, '$.**.wordpressRecord.id') n(value)
                          WHERE n.value #>> '{{}}' = s.native_ids[1]))
        )
      )
    )
  ) THEN
    RAISE EXCEPTION 'Pyramid unclassified identity or lifecycle changed; refusing mutation';
  END IF;
END $$;
"""


def historical_conflict_rows_sql():
    """One predicate shared by the ordinary guard and the typed rollback path."""
    return """
WITH incoming_pyramid AS MATERIALIZED (
  SELECT s.brokerage_id, s.external_id, s.source_url FROM _src s
  WHERE jsonb_path_exists(s.raw_data, '$.**.sourceKey ? (@ == "pyramid-brokerage")')
)
SELECT 1 FROM incoming_pyramid s
JOIN credeals.cre_listings t ON t.brokerage_id = s.brokerage_id
 AND ((t.source_url = s.source_url AND t.external_id IS DISTINCT FROM s.external_id)
   OR (t.external_id = s.external_id AND t.source_url IS DISTINCT FROM s.source_url))

"""


def guard_sql(claims, *, mark_missing, status_activation, inventory_only):
    """Bind the validated partition to locked, actually staged source rows.

    The caller must run this after the source-index/listing locks and before
    the listing upsert. The existing transaction lifecycle lock serializes
    conforming collector writers; it is not a lock on unrelated writers.
    """
    from cre_current_inventory_admission import require
    from cre_ingest import sql_lit
    from cre_pyramid_current_inventory import _path_key

    require(mark_missing is False and status_activation is False and inventory_only is False)
    require(isinstance(claims, dict) and claims.get("schema") in ("pyramid-verified-subset-v1", "pyramid-incomplete-v2"))
    incomplete_mode = claims["schema"] == "pyramid-incomplete-v2"
    binding = claims.get("binding")
    require(isinstance(binding, dict) and binding.get("scope") ==
            ("pyramid_incomplete_v2" if incomplete_mode else "pyramid_verified_subset_v1"))
    require(re.fullmatch(r"[a-f0-9]{32}", claims.get("attempt_id", "")))
    require(re.fullmatch(r"[a-f0-9]{64}", binding.get("artifact_sha256", "")))
    members = claims.get("members")
    require(all(type(binding.get(key)) is int for key in ("inventory_count", "emitted_count", "held_count")))
    require(isinstance(members, list) and len(members) == binding.get("inventory_count") and len(members) >= 100)
    ids, paths, urls = set(), set(), set()
    for member in members:
        require(isinstance(member, dict) and set(member) == {
            "native_id", "external_id", "source_url", "transaction", "emitted",
            *({"incomplete", "transition", "missing_fields"} if incomplete_mode else set())
        })
        if incomplete_mode:
            require(type(member["incomplete"]) is bool and (not member["incomplete"] or member["emitted"])
                    and type(member["transition"]) is bool and (not member["transition"] or member["emitted"])
                    and isinstance(member["missing_fields"], list)
                    and member["missing_fields"] == [field for field in ("state", "city", "assetType") if field in member["missing_fields"]]
                    and bool(member["missing_fields"]) is member["incomplete"])
        native = member["native_id"]
        require(type(native) is int and 0 < native <= 9007199254740991)
        require(member["external_id"] == _path_key(member["source_url"]))
        require(member["transaction"] in {"sale", "lease"} and type(member["emitted"]) is bool)
        require(native not in ids and member["external_id"] not in paths and member["source_url"] not in urls)
        ids.add(native)
        paths.add(member["external_id"])
        urls.add(member["source_url"])
    require([member["native_id"] for member in members] == sorted(ids))
    emitted = sum(member["emitted"] for member in members)
    require(emitted > 0 and emitted == binding.get("emitted_count"))
    require(len(members) - emitted == binding.get("held_count"))
    if incomplete_mode:
        require(type(binding.get("incomplete_count")) is int and type(binding.get("transition_count")) is int)
        counts = binding.get("missing_field_counts")
        require(isinstance(counts, dict) and set(counts) == {"state", "city", "assetType"}
                and all(type(value) is int for value in counts.values()))
        require(sum(m["incomplete"] for m in members) == binding.get("incomplete_count"))
        require(sum(m["transition"] for m in members) == binding.get("transition_count"))
        require(binding.get("missing_field_counts") == {
            field: sum(field in m["missing_fields"] for m in members)
            for field in ("state", "city", "assetType")})
        require(binding["held_count"] > 0 or binding["incomplete_count"] > 0 or binding["transition_count"] > 0)
    else:
        require(binding["held_count"] > 0)
    digest = hashlib.sha256(json.dumps(members, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    require(digest == binding.get("members_sha256"))
    payload = sql_lit(json.dumps(members, separators=(",", ":")))
    unexpected_transition = "OR EXISTS (SELECT 1 FROM _pyramid_native_transitions x JOIN _pyramid_subset_members m USING (brokerage_id,external_id) WHERE m.transition IS NOT TRUE)" if incomplete_mode else ""
    staged_count = len(members) if incomplete_mode else emitted
    staged_members = "TRUE" if incomplete_mode else "emitted"
    checked_members = "s.emitted" if incomplete_mode else "TRUE"
    omit_held = """
DELETE FROM _src s USING _pyramid_subset_members m
WHERE NOT m.emitted AND s.brokerage_id = m.brokerage_id AND s.external_id = m.external_id;
UPDATE credeals.cre_scrape_jobs j
SET status='partial', listings_saved=(SELECT count(*) FROM _src s WHERE s.brokerage_id=j.brokerage_id)
FROM _jobmeta jm WHERE jm.slug='pyramid-brokerage' AND j.id=jm.job_id;
""" if incomplete_mode else ""
    return f"""
-- Pyramid verified subset: complete identity partition including held members.
-- Run under the lifecycle lock after emitted source-index and listing row locks.
CREATE TEMP TABLE _pyramid_subset_members ON COMMIT DROP AS
SELECT b.id AS brokerage_id, m.*
FROM jsonb_to_recordset({payload}::jsonb)
 AS m(native_id bigint, external_id text, source_url text, transaction text, emitted boolean, incomplete boolean, transition boolean, missing_fields text[])
JOIN credeals.cre_brokerages b ON b.slug = 'pyramid-brokerage';
CREATE TEMP TABLE _pyramid_subset_history ON COMMIT DROP AS
SELECT t.brokerage_id, t.external_id, t.source_url, t.status, t.deleted_at,
       t.title, t.address, t.city, t.state, t.transaction_type, t.description,
       ARRAY(SELECT DISTINCT n.value #>> '{{}}'
             FROM jsonb_path_query(t.raw_data, '$.**.wordpressRecord.id') n(value)) AS native_ids
FROM credeals.cre_listings t
WHERE t.brokerage_id IN (SELECT brokerage_id FROM _pyramid_subset_members);
-- Only emitted, exactly matched active parents and current source-index rows
-- can transition. This table also selects additive child preservation.
INSERT INTO _pyramid_native_transitions (brokerage_id, external_id)
SELECT m.brokerage_id, m.external_id
FROM _pyramid_subset_members m
JOIN _src s USING (brokerage_id, external_id)
JOIN _pyramid_subset_history t USING (brokerage_id, external_id)
WHERE m.emitted AND t.source_url = m.source_url
  AND cardinality(t.native_ids) = 1
  AND ({valid_native_id_sql("t.native_ids[1]")})
  AND (t.native_ids[1] <> m.native_id::text OR m.transition IS TRUE)
  AND NOT EXISTS (SELECT 1 FROM _pyramid_subset_history h
                  WHERE h.brokerage_id = m.brokerage_id
                    AND h.external_id IS DISTINCT FROM m.external_id
                    AND t.native_ids[1] = ANY(h.native_ids))
  AND t.status::text = 'active' AND t.deleted_at IS NULL
  AND (SELECT count(*) FROM _pyramid_subset_history h
       WHERE h.brokerage_id = m.brokerage_id AND h.external_id = m.external_id) = 1
  AND {current_index_sql("m.brokerage_id", "m.external_id", "m.source_url")}
  AND {strong_facts_sql("s", "t")};
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM _pyramid_subset_members m WHERE m.transition IS TRUE
             AND NOT EXISTS (SELECT 1 FROM _pyramid_native_transitions x
                             WHERE x.brokerage_id=m.brokerage_id AND x.external_id=m.external_id))
     {unexpected_transition} THEN
    RAISE EXCEPTION 'Pyramid transition evidence changed after classification';
  END IF;
  IF EXISTS (SELECT 1 FROM credeals.cre_source_baseline WHERE source_key='pyramid-brokerage') THEN
    RAISE EXCEPTION 'Pyramid subset requires absent baseline';
  END IF;
  IF (SELECT count(*) FROM _pyramid_subset_members) <> {len(members)}
     OR (SELECT count(*) FROM _src WHERE brokerage_id IN (SELECT brokerage_id FROM _pyramid_subset_members)) <> {staged_count}
     OR EXISTS (
       (SELECT brokerage_id, external_id, source_url, transaction_type FROM _src
        WHERE brokerage_id IN (SELECT brokerage_id FROM _pyramid_subset_members)
        EXCEPT SELECT brokerage_id, external_id, source_url, transaction FROM _pyramid_subset_members WHERE {staged_members})
       UNION ALL
       (SELECT brokerage_id, external_id, source_url, transaction FROM _pyramid_subset_members WHERE emitted
        EXCEPT SELECT brokerage_id, external_id, source_url, transaction_type FROM _src
        WHERE brokerage_id IN (SELECT brokerage_id FROM _pyramid_subset_members))
     ) THEN
    RAISE EXCEPTION 'Pyramid subset staging does not equal emitted identities';
  END IF;
  IF EXISTS (
    SELECT 1 FROM _pyramid_subset_members m JOIN _src s USING (brokerage_id, external_id)
    WHERE m.emitted AND (
      s.canonical_url IS DISTINCT FROM m.source_url
      OR s.raw_data->>'sourceKey' IS DISTINCT FROM 'pyramid-brokerage'
      OR s.raw_data->>'transactionMode' IS DISTINCT FROM m.transaction
      OR s.raw_data#>>'{{wordpressRecord,id}}' IS DISTINCT FROM m.native_id::text
      OR s.raw_data#>>'{{wordpressRecord,link}}' IS DISTINCT FROM m.source_url
      OR s.raw_data#>>'{{wordpressRecord,status}}' IS DISTINCT FROM 'publish'
      OR s.raw_data#>>'{{wordpressRecord,type}}' IS DISTINCT FROM 'pbc-listings'
      OR (m.incomplete IS TRUE AND (
          ('city' = ANY(m.missing_fields) AND s.city IS NOT NULL)
          OR ('state' = ANY(m.missing_fields) AND s.state IS NOT NULL)
          OR ('assetType' = ANY(m.missing_fields) AND s.property_type IS NOT NULL)
          OR s.raw_data#>'{{missingFieldEvidence,fields}}' IS DISTINCT FROM to_jsonb(m.missing_fields)
          OR s.raw_data#>>'{{missingFieldEvidence,method}}' IS DISTINCT FROM 'source_detail_and_taxonomy'
          OR s.raw_data->'preserveChildCollections' IS DISTINCT FROM 'true'::jsonb
          OR s.raw_data->'detailObservedWithChildPreservation' IS DISTINCT FROM 'true'::jsonb))
    )
  ) THEN
    RAISE EXCEPTION 'Pyramid subset staged native or transaction identity differs';
  END IF;
  IF EXISTS (
    SELECT 1 FROM _pyramid_subset_members s
    JOIN _pyramid_subset_history t ON t.brokerage_id = s.brokerage_id
    WHERE {checked_members} AND ((t.source_url = s.source_url AND t.external_id IS DISTINCT FROM s.external_id)
       OR (t.external_id = s.external_id AND t.source_url IS DISTINCT FROM s.source_url)
       OR (s.native_id::text = ANY(t.native_ids)
           AND (t.source_url IS DISTINCT FROM s.source_url OR t.external_id IS DISTINCT FROM s.external_id))
       OR (t.source_url = s.source_url AND t.external_id = s.external_id
           AND EXISTS (SELECT 1 FROM unnest(t.native_ids) n(value)
                       WHERE n.value IS DISTINCT FROM s.native_id::text)
           AND NOT EXISTS (SELECT 1 FROM _pyramid_native_transitions x
                           WHERE x.brokerage_id = s.brokerage_id AND x.external_id = s.external_id)))
  ) OR EXISTS (
    SELECT 1 FROM _pyramid_subset_members s
    JOIN credeals.cre_source_index si ON si.brokerage_id = s.brokerage_id
    WHERE {checked_members} AND ((si.url = s.source_url AND si.external_id IS DISTINCT FROM s.external_id)
       OR (si.external_id = s.external_id AND (si.url IS DISTINCT FROM s.source_url
           OR si.source_key IS DISTINCT FROM 'pyramid-brokerage')))
  ) THEN
    RAISE EXCEPTION 'Pyramid subset historical identity contradiction including held members';
  END IF;
  IF EXISTS (
    SELECT s.brokerage_id, s.external_id FROM _pyramid_subset_members s
    JOIN credeals.cre_listings t USING (brokerage_id, external_id)
    WHERE {checked_members}
    GROUP BY s.brokerage_id, s.external_id HAVING count(*) > 1
  ) OR EXISTS (
    SELECT s.brokerage_id, s.external_id FROM _pyramid_subset_members s
    JOIN credeals.cre_source_index si USING (brokerage_id, external_id)
    WHERE {checked_members}
    GROUP BY s.brokerage_id, s.external_id HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION 'Pyramid subset has duplicate historical identities';
  END IF;
  IF EXISTS (
    SELECT 1 FROM _pyramid_subset_members s
    JOIN credeals.cre_listings t USING (brokerage_id, external_id)
    WHERE s.emitted AND (t.deleted_at IS NOT NULL OR t.status::text IS DISTINCT FROM 'active')
  ) OR EXISTS (
    SELECT 1 FROM _pyramid_subset_members s
    JOIN credeals.cre_source_index si USING (brokerage_id, external_id)
    WHERE s.emitted AND (si.soft_deleted IS DISTINCT FROM false
                         OR si.observation_present IS DISTINCT FROM true)
  ) THEN
    RAISE EXCEPTION 'Pyramid subset would revive lifecycle or presence; refusing entire apply';
  END IF;
END $$;
{omit_held}
"""
