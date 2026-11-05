"""Transaction-local DAUM subset preconditions; ordinary upserts stay unchanged."""

import json
import re


def guard_sql(claims):
    from cre_current_inventory_admission import require
    from cre_daum_current_inventory import (
        SUBSET_SCHEMA, SUBSET_SCOPE, SUBSET_SCHEMA_V2, SUBSET_SCOPE_V2,
        SUBSET_POLICY_V2, _path_key, _post, _sha,
    )
    from cre_ingest import sql_lit

    require(isinstance(claims, dict) and claims.get("schema") in {SUBSET_SCHEMA, SUBSET_SCHEMA_V2})
    v2 = claims["schema"] == SUBSET_SCHEMA_V2
    binding = claims.get("binding")
    require(
        isinstance(binding, dict)
        and binding.get("scope") == (SUBSET_SCOPE_V2 if v2 else SUBSET_SCOPE)
        and re.fullmatch(r"[a-f0-9]{32}", claims.get("attempt_id", ""))
    )
    members = claims.get("members")
    require(
        isinstance(members, list) and len(members) == binding.get("inventory_count")
    )
    ids, keys, urls = set(), set(), []
    for member in members:
        require(
            isinstance(member, dict)
            and set(member) == {"native_id", "external_id", "source_url", "emitted"}
        )
        _post(member["native_id"])
        require(
            member["external_id"] == _path_key(member["source_url"])
            and type(member["emitted"]) is bool
        )
        require(member["native_id"] not in ids and member["external_id"] not in keys)
        ids.add(member["native_id"])
        keys.add(member["external_id"])
        urls.append(member["source_url"])
    emitted = sum(member["emitted"] for member in members)
    if v2:
        require(claims.get("subset_policy") == binding.get("subset_policy") == SUBSET_POLICY_V2)
        observations = claims.get("titleObservations")
        require(isinstance(observations, list) and _sha(observations) == binding.get("title_observations_sha256"))
        by_id = {member["native_id"]: member for member in members}
        seen, observed_urls = set(), []
        for observation in observations:
            require(isinstance(observation, list) and len(observation) == 3)
            post, count, state = observation
            require(isinstance(post, str) and post in by_id and post not in seen)
            require(type(count) is int and count == 1 and state == "empty")
            require(by_id[post]["emitted"] is False)
            seen.add(post)
            observed_urls.append(by_id[post]["source_url"])
        require(observed_urls == sorted(observed_urls))
        fallback = claims.get("titleFallbackObservations", [])
        require(isinstance(fallback, list))
        if fallback:
            require(_sha(fallback) == binding.get("title_fallback_observations_sha256"))
        fallback_ids, fallback_urls = set(), []
        for observation in fallback:
            require(isinstance(observation, dict) and set(observation) == {"postId", "cardTitle", "documentTitle"})
            post, title = observation["postId"], observation["cardTitle"]
            require(isinstance(post, str) and post in by_id and post not in seen and post not in fallback_ids)
            require(by_id[post]["emitted"] is True and isinstance(title, str) and title and title == title.strip())
            require(observation["documentTitle"] == f"{title} – Daum Commercial")
            fallback_ids.add(post)
            fallback_urls.append(by_id[post]["source_url"])
        require(fallback_urls == sorted(fallback_urls))
    require(
        urls == sorted(set(urls))
        and emitted >= 100
        and emitted == binding.get("emitted_count")
        and len(members) - emitted == binding.get("held_count")
    )
    fallback_members = claims.get("fallbackMembers", [])
    fallback_partition = claims.get("fallbackPartition", [])
    if fallback_members or fallback_partition:
        from cre_daum_fallback_partition import transaction_guard_sql

        require(v2 and _sha(fallback_partition) == binding.get("fallback_partition_sha256"))
        require({row["native_id"] for row in fallback_members} == fallback_ids)
        for row in fallback_members:
            member = by_id[row["native_id"]]
            require(all(row[key] == member[key] for key in ("native_id", "external_id", "source_url")))
        transaction_guard_sql(fallback_members, fallback_partition)
    else:
        require(not v2 or not fallback_ids)
    held_filter = (
        " AND NOT EXISTS (SELECT 1 FROM _daum_fallback_held h "
        "WHERE h.brokerage_id=s.brokerage_id AND h.external_id=s.external_id)"
        if fallback_members else ""
    )
    payload = sql_lit(json.dumps(members, separators=(",", ":")))
    return f"""
-- DAUM verified subset: exhaustive incoming identities, including held members.
-- This runs under the existing lifecycle advisory lock, after emitted row locks.
CREATE TEMP TABLE _daum_subset_members ON COMMIT DROP AS
SELECT b.id AS brokerage_id, m.*
FROM jsonb_to_recordset({payload}::jsonb)
 AS m(native_id text, external_id text, source_url text, emitted boolean)
JOIN credeals.cre_brokerages b ON b.slug = 'daum-commercial';
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM credeals.cre_source_baseline WHERE source_key='daum-commercial') THEN
    RAISE EXCEPTION 'DAUM subset requires absent baseline';
  END IF;
  IF (SELECT count(*) FROM _daum_subset_members) <> {len(members)}
     OR EXISTS (
       (SELECT brokerage_id, external_id, source_url FROM _src
        EXCEPT SELECT brokerage_id, external_id, source_url FROM _daum_subset_members s WHERE emitted{held_filter})
       UNION ALL
       (SELECT brokerage_id, external_id, source_url FROM _daum_subset_members s WHERE emitted{held_filter}
        EXCEPT SELECT brokerage_id, external_id, source_url FROM _src)
     ) THEN
    RAISE EXCEPTION 'DAUM subset staging does not equal emitted identities';
  END IF;
  IF EXISTS (
    SELECT 1 FROM _daum_subset_members s
    JOIN _daum_identity_claims staged USING (brokerage_id, external_id)
    WHERE s.emitted AND staged.native_ids IS DISTINCT FROM ARRAY[s.native_id]
  ) THEN
    RAISE EXCEPTION 'DAUM subset native staging mismatch';
  END IF;
  IF EXISTS (
    SELECT 1 FROM _daum_subset_members s
    JOIN _daum_historical_identity_claims t ON t.brokerage_id = s.brokerage_id{held_filter}
    WHERE (t.source_url = s.source_url AND t.external_id IS DISTINCT FROM s.external_id)
       OR (t.external_id = s.external_id AND t.source_url IS DISTINCT FROM s.source_url)
       OR (s.native_id = ANY(t.native_ids)
           AND (t.source_url IS DISTINCT FROM s.source_url OR t.external_id IS DISTINCT FROM s.external_id))
       OR ((t.source_url = s.source_url OR t.external_id = s.external_id)
           AND cardinality(t.native_ids) > 0
           AND (cardinality(t.native_ids) <> 1 OR t.native_ids[1] IS DISTINCT FROM s.native_id))
  ) OR EXISTS (
    SELECT 1 FROM _daum_subset_members s
    JOIN credeals.cre_source_index si ON si.brokerage_id = s.brokerage_id{held_filter}
    WHERE (si.url = s.source_url AND si.external_id IS DISTINCT FROM s.external_id)
       OR (si.external_id = s.external_id AND si.url IS DISTINCT FROM s.source_url)
  ) THEN
    RAISE EXCEPTION 'DAUM subset historical identity contradiction including held members';
  END IF;
  IF EXISTS (
    SELECT 1 FROM _daum_subset_members s
    JOIN credeals.cre_listings t USING (brokerage_id, external_id)
    WHERE s.emitted{held_filter} AND (t.deleted_at IS NOT NULL OR t.status::text IS DISTINCT FROM 'active')
  ) OR EXISTS (
    SELECT 1 FROM _daum_subset_members s
    JOIN credeals.cre_source_index si USING (brokerage_id, external_id)
    WHERE s.emitted{held_filter} AND (si.soft_deleted IS DISTINCT FROM false
                         OR si.observation_present IS DISTINCT FROM true)
  ) THEN
    RAISE EXCEPTION 'DAUM subset would revive lifecycle or presence; refusing entire apply';
  END IF;
END $$;
"""
