"""Experimental existing-parent-only partial fill; caller owns artifact custody.

This is not wired to the checkpoint runner. Private source observations remain
in the existing spool, which is not crash-durable or a historical archive.
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

FIELDS = {
    "title": "text", "address": "text", "city": "text", "state": "text", "zip": "text",
    "description": "text", "property_type": "text", "size_sf": "numeric",
    "lot_size_sf": "numeric", "year_built": "integer", "cap_rate": "numeric", "highlights": "text[]",
}


def stage_observations(observations, generation, started_at, finished_at):
    """Validate producer identity/provenance; this does not authenticate custody."""
    if not isinstance(generation, str) or not generation or not isinstance(observations, list):
        raise ValueError("invalid Matthews generation or observations")
    started = parse_strict_freshness_timestamp(started_at, field="generation start")
    finished = parse_strict_freshness_timestamp(finished_at, field="generation finish")
    if started > finished:
        raise ValueError("invalid generation window")
    rows = []
    ids, urls = set(), set()
    for item in observations:
        if not isinstance(item, dict) or item.get("schema") != "matthews-completed-parse-observation/v1" or item.get("sourceKey") != "matthews":
            raise ValueError("invalid Matthews completed observation")
        row = item.get("observation")
        if not isinstance(row, dict):
            raise TypeError("missing Matthews observation")
        identity, url = row.get("id"), row.get("url")
        if not isinstance(identity, str) or not isinstance(url, str):
            raise TypeError("missing Matthews identity")
        parsed = urlsplit(url)
        normalized = re.sub(r"%[0-9A-Fa-f]{2}", lambda m: m[0].lower(), parsed.path[12:])
        provenance = row.get("freshnessProvenance")
        if (parsed.scheme != "https" or parsed.netloc != "www.matthews.com" or parsed.query or parsed.fragment
                or not re.fullmatch(r"/properties/[^/]+", parsed.path) or normalized != identity
                or any(ord(c) < 33 or ord(c) == 127 for c in url) or row.get("canonicalUrl") != url
                or not isinstance(provenance, dict) or provenance.get("generationId") != generation
                or provenance.get("method") != "matthews_canonical_detail"
                or provenance.get("identityMethod") != "canonical_property_url"
                or provenance.get("cacheDisposition") != "live" or provenance.get("detailScope") != "detail_page"
                or re.search(r"%(?![0-9a-fA-F]{2})", parsed.path) or identity in ids or url in urls):
            raise ValueError("invalid Matthews identity or freshness proof")
        inventory = parse_strict_freshness_timestamp(row.get("inventoryObservedAt"), field="inventory")
        detail = parse_strict_freshness_timestamp(row.get("detailObservedAt"), field="detail")
        if not started <= inventory <= detail <= finished:
            raise ValueError("Matthews observation is outside its generation window")
        mapped = to_row({**row, "sourceKey": "matthews"}, {}, row["detailObservedAt"])
        if not isinstance(mapped, dict) or mapped.get("slug") != "matthews" or mapped.get("external_id") != identity or mapped.get("source_url") != url:
            raise ValueError("Matthews staged identity differs")
        staged = {"external_id": identity, "source_url": url}
        for column, pg_type in FIELDS.items():
            value = mapped.get(column)
            if pg_type == "text" and value is not None and (not isinstance(value, str) or not value.strip() or "\x00" in value):
                raise ValueError("invalid Matthews text field")
            if column == "state" and value is not None and not re.fullmatch(r"[A-Z]{2}", value):
                raise ValueError("invalid Matthews state")
            if column == "highlights" and not value:
                value = None
            staged[column] = value
        ids.add(identity)
        urls.add(url)
        rows.append(staged)
    return rows


def fill_sql(staged):
    """Produce one serializable transaction; never changes lifecycle or children.

    Only exact existing identity/URL matches without source-index contradictions
    qualify. Parent field and URL comparisons are made under row locks; the
    transaction refuses conflicting source-index or listing URL ownership.
    A commit-unknown recovery protocol is not implemented by this function.
    """
    if not isinstance(staged, list) or not staged:
        raise ValueError("no staged Matthews observations")
    payload = sql_lit(json.dumps(staged, ensure_ascii=True, separators=(",", ":"), allow_nan=False))
    definitions = ", ".join(f"{field} {pg_type}" for field, pg_type in FIELDS.items())
    assignments = ", ".join(f"{field}=COALESCE(l.{field}, c.{field})" for field in FIELDS)
    comparisons = " OR ".join(f"(l.{field} IS NULL AND c.{field} IS NOT NULL)" for field in FIELDS)
    return f"""
BEGIN ISOLATION LEVEL SERIALIZABLE;
{lifecycle_transaction_lock_sql()}
CREATE TEMP TABLE _matthews_candidates ON COMMIT DROP AS
SELECT * FROM jsonb_to_recordset({payload}::jsonb)
 AS x(external_id text, source_url text, {definitions});
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM _matthews_candidates GROUP BY external_id HAVING count(*)<>1)
 OR EXISTS (SELECT 1 FROM _matthews_candidates GROUP BY source_url HAVING count(*)<>1)
 THEN RAISE EXCEPTION 'duplicate Matthews candidate'; END IF;
END $$;
CREATE TEMP TABLE _matthews_index_locked ON COMMIT DROP AS
SELECT si.id FROM credeals.cre_source_index si
JOIN credeals.cre_brokerages b ON b.id=si.brokerage_id AND b.slug='matthews'
JOIN _matthews_candidates c ON c.external_id=si.external_id OR c.source_url=si.url
ORDER BY si.id FOR UPDATE OF si;
CREATE TEMP TABLE _matthews_locked ON COMMIT DROP AS
SELECT l.id FROM credeals.cre_listings l
JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id AND b.slug='matthews'
JOIN _matthews_candidates c ON c.external_id=l.external_id OR c.source_url=l.source_url
ORDER BY l.id FOR UPDATE OF l;
CREATE TEMP TABLE _matthews_eligible ON COMMIT DROP AS
SELECT c.external_id, l.id
FROM _matthews_candidates c JOIN credeals.cre_brokerages b ON b.slug='matthews'
JOIN credeals.cre_listings l ON l.brokerage_id=b.id AND l.external_id=c.external_id
WHERE l.source_url=c.source_url AND l.deleted_at IS NULL AND l.status='active'
 AND (l.canonical_url IS NULL OR l.canonical_url=c.source_url)
 AND NOT EXISTS (SELECT 1 FROM credeals.cre_listings other
   WHERE other.brokerage_id=b.id AND other.id<>l.id
   AND (other.source_url=c.source_url OR other.canonical_url=c.source_url))
 AND NOT EXISTS (SELECT 1 FROM credeals.cre_source_index si
   WHERE si.brokerage_id=b.id AND ((si.external_id=c.external_id
     AND (si.source_key IS DISTINCT FROM 'matthews' OR si.url IS DISTINCT FROM c.source_url
       OR si.soft_deleted IS TRUE OR si.observation_present IS FALSE))
     OR (si.external_id<>c.external_id AND si.url=c.source_url)));
CREATE TEMP TABLE _matthews_updated ON COMMIT DROP AS
WITH updated AS (
 UPDATE credeals.cre_listings l SET {assignments}
 FROM _matthews_candidates c JOIN _matthews_eligible e USING (external_id)
 WHERE l.id=e.id AND ({comparisons}) RETURNING l.id
) SELECT id FROM updated;
SELECT c.external_id, CASE WHEN e.id IS NULL THEN 'held' WHEN u.id IS NULL THEN 'unchanged' ELSE 'updated' END,
       to_jsonb(l) FROM _matthews_candidates c
LEFT JOIN _matthews_eligible e USING (external_id)
LEFT JOIN _matthews_updated u ON u.id=e.id
LEFT JOIN credeals.cre_listings l ON l.id=e.id ORDER BY c.external_id;
COMMIT;
"""
