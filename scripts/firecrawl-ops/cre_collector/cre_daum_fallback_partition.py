"""DAUM fallback identity partition; this module grants no write authority."""

def staged_fallback_members(data):
    """Bind corroborated fallback witnesses to their source listing identities."""
    from cre_daum_current_inventory import SUBSET_SCHEMA_V2, _path_key, _post, subset_source_scope
    from cre_ingest import is_daum_title_fallback

    scope = subset_source_scope(data)
    proofs = [entry["currentInventoryProof"] for entry in data["sources"]]
    if any(p.get("schema") != SUBSET_SCHEMA_V2 for p in proofs):
        raise ValueError("DAUM fallback requires V2 proof")
    witnesses = proofs[0].get("titleFallbackObservations", [])
    if not isinstance(witnesses, list) or any(p.get("titleFallbackObservations", []) != witnesses for p in proofs):
        raise ValueError("DAUM fallback witnesses differ")
    expected = {}
    for item in witnesses:
        if not isinstance(item, dict) or set(item) != {"postId", "cardTitle", "documentTitle"}:
            raise ValueError("DAUM fallback witness shape invalid")
        post, title = item["postId"], item["cardTitle"]
        _post(post)
        if (post in expected or not isinstance(title, str) or not title.strip()
                or title != title.strip() or item["documentTitle"] != f"{title} – Daum Commercial"):
            raise ValueError("DAUM fallback witness invalid")
        expected[post] = item
    rows = {}
    for listing in scope["listings"]:
        if not isinstance(listing, dict):
            raise ValueError("DAUM listing invalid")
        post = listing.get("daumPostId")
        if post not in expected:
            if "daumTitleFallback" in listing:
                raise ValueError("DAUM fallback witness missing")
            continue
        if not is_daum_title_fallback(listing):
            raise ValueError("DAUM fallback listing invalid")
        source_url = listing["url"]
        external_id = _path_key(source_url)
        if listing.get("id") != external_id or listing["daumTitleFallback"] != {
                "cardTitle": expected[post]["cardTitle"], "documentTitle": expected[post]["documentTitle"]}:
            raise ValueError("DAUM fallback source binding differs")
        row = {"native_id": post, "external_id": external_id, "source_url": source_url}
        if post in rows and rows[post] != row:
            raise ValueError("DAUM fallback pass identity differs")
        rows[post] = row
    if set(rows) != set(expected):
        raise ValueError("DAUM fallback listing missing")
    return sorted(rows.values(), key=lambda row: row["external_id"])

def identity_probe_sql(members):
    """Produce source-local classifications and exact target preimage hashes.

    This is a read-only probe. Admission must bind its output to the artifact,
    generation and target, and mutation must repeat it under the lifecycle lock.
    """
    import json

    from cre_daum_current_inventory import _path_key, _post
    from cre_ingest import sql_lit

    if not isinstance(members, list):
        raise ValueError("DAUM fallback members invalid")
    ids, keys, urls = set(), set(), set()
    for row in members:
        if not isinstance(row, dict) or set(row) != {"native_id", "external_id", "source_url"}:
            raise ValueError("DAUM fallback member invalid")
        _post(row["native_id"])
        if (row["external_id"] != _path_key(row["source_url"])
                or row["native_id"] in ids or row["external_id"] in keys
                or row["source_url"] in urls):
            raise ValueError("DAUM fallback member identity collision")
        ids.add(row["native_id"])
        keys.add(row["external_id"])
        urls.add(row["source_url"])
    payload = sql_lit(json.dumps(members, sort_keys=True, separators=(",", ":")))
    return f"""
WITH candidate AS (
  SELECT * FROM jsonb_to_recordset({payload}::jsonb)
    AS c(native_id text, external_id text, source_url text)
), scoped AS (
  SELECT c.*, b.id AS brokerage_id FROM candidate c
  JOIN credeals.cre_brokerages b ON b.slug='daum-commercial'
), assessed AS (
  SELECT c.*,
    CASE
      WHEN EXISTS (
        SELECT 1 FROM credeals.cre_listings other
        WHERE other.brokerage_id=c.brokerage_id
          AND ((other.source_url=c.source_url AND other.external_id IS DISTINCT FROM c.external_id)
            OR (other.external_id=c.external_id AND other.source_url IS DISTINCT FROM c.source_url)
            OR (other.external_id IS DISTINCT FROM c.external_id AND EXISTS (
              SELECT 1 FROM jsonb_path_query(other.raw_data, '$.**.daumPostId') v
              WHERE v #>> '{{}}' = c.native_id)))
      ) OR EXISTS (
        SELECT 1 FROM credeals.cre_source_index si
        WHERE si.brokerage_id=c.brokerage_id
          AND ((si.url=c.source_url AND si.external_id IS DISTINCT FROM c.external_id)
            OR (si.external_id=c.external_id AND si.url IS DISTINCT FROM c.source_url))
      ) THEN 'identity_conflict'
      WHEN own.id IS NOT NULL AND (
        own.deleted_at IS NOT NULL OR own.status::text IS DISTINCT FROM 'active'
        OR own.canonical_url IS DISTINCT FROM c.source_url
        OR jsonb_typeof(own.raw_data) IS DISTINCT FROM 'object'
        OR NOT jsonb_path_exists(own.raw_data, '$.**.sourceKey ? (@ == "daum-commercial")')
        OR ARRAY(SELECT DISTINCT v #>> '{{}}' FROM jsonb_path_query(own.raw_data, '$.**.daumPostId') v)
           IS DISTINCT FROM ARRAY[c.native_id]
      ) THEN 'existing_parent_unqualified'
      WHEN si.id IS NOT NULL AND (
        si.soft_deleted IS DISTINCT FROM false OR si.observation_present IS DISTINCT FROM true
        OR si.source_key IS DISTINCT FROM 'daum-commercial'
      ) THEN 'source_index_unqualified'
      WHEN own.id IS NULL AND si.id IS NOT NULL THEN 'orphan_source_index'
      ELSE NULL
    END AS hold_reason,
    encode(sha256(convert_to(COALESCE(to_jsonb(own)::text,'null') || E'\\n' || COALESCE(to_jsonb(si)::text,'null'), 'UTF8')), 'hex') AS preimage
  FROM scoped c
  LEFT JOIN credeals.cre_listings own ON own.brokerage_id=c.brokerage_id AND own.external_id=c.external_id
  LEFT JOIN credeals.cre_source_index si ON si.brokerage_id=c.brokerage_id AND si.external_id=c.external_id
)
SELECT (SELECT count(*) FROM candidate) AS staged,
       (SELECT count(*) FROM scoped) AS joined,
       COALESCE((SELECT json_agg(json_build_object(
         'native_id', native_id, 'external_id', external_id, 'source_url', source_url,
         'hold_reason', hold_reason, 'preimage', preimage)
         ORDER BY external_id)::text FROM assessed), '[]') AS partition
"""

def transaction_guard_sql(members, partition):
    """Lock owned targets and recheck the bound partition before filtering holds."""
    import json
    import re

    from cre_ingest import sql_lit

    # Reuse the probe's input validation before constructing any SQL.
    query = identity_probe_sql(members)
    if not isinstance(partition, list) or len(partition) != len(members):
        raise ValueError("DAUM fallback partition shape invalid")
    expected = {item["external_id"]: item for item in members}
    seen = set()
    for item in partition:
        if not isinstance(item, dict) or set(item) != {
                "native_id", "external_id", "source_url", "hold_reason", "preimage"}:
            raise ValueError("DAUM fallback partition entry invalid")
        key = item["external_id"]
        if (key in seen or key not in expected
                or any(item[k] != expected[key][k] for k in ("native_id", "external_id", "source_url"))
                or item["hold_reason"] not in (None, "identity_conflict", "existing_parent_unqualified",
                                               "source_index_unqualified", "orphan_source_index")
                or not isinstance(item["preimage"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", item["preimage"])):
            raise ValueError("DAUM fallback partition entry differs")
        seen.add(key)
    if [item["external_id"] for item in partition] != sorted(expected):
        raise ValueError("DAUM fallback partition ordering differs")
    payload = sql_lit(json.dumps(members, sort_keys=True, separators=(",", ":")))
    bound = sql_lit(json.dumps(partition, sort_keys=True, separators=(",", ":")))
    return f"""
CREATE TEMP TABLE _daum_fallback_members ON COMMIT DROP AS
SELECT b.id AS brokerage_id, m.*
FROM jsonb_to_recordset({payload}::jsonb)
  AS m(native_id text, external_id text, source_url text)
JOIN credeals.cre_brokerages b ON b.slug='daum-commercial';
-- The transaction-wide lifecycle advisory lock is already held. Lock in the
-- same source-index-then-parent order used by the shared ingest.
CREATE TEMP TABLE _daum_fallback_source_locks ON COMMIT DROP AS
SELECT si.id FROM credeals.cre_source_index si JOIN _daum_fallback_members m
  ON si.brokerage_id=m.brokerage_id AND si.external_id=m.external_id
  ORDER BY si.id FOR UPDATE OF si;
CREATE TEMP TABLE _daum_fallback_parent_locks ON COMMIT DROP AS
SELECT t.id FROM credeals.cre_listings t JOIN _daum_fallback_members m
  ON t.brokerage_id=m.brokerage_id AND t.external_id=m.external_id
  ORDER BY t.id FOR UPDATE OF t;
CREATE TEMP TABLE _daum_fallback_recheck ON COMMIT DROP AS
{query};
DO $$
BEGIN
 IF (SELECT count(*) FROM _daum_fallback_members) <> {len(members)}
 OR (SELECT staged FROM _daum_fallback_recheck) <> {len(members)}
 OR (SELECT joined FROM _daum_fallback_recheck) <> {len(members)}
 OR (SELECT partition::jsonb FROM _daum_fallback_recheck) IS DISTINCT FROM {bound}::jsonb
 THEN RAISE EXCEPTION 'DAUM fallback partition changed'; END IF;
END $$;
"""
