"""Experimental existing-parent-only partial fill; caller owns artifact custody.

This is not wired to the checkpoint runner. Private source observations remain
in the existing spool, which is not crash-durable or a historical archive.
"""

import hashlib
import json
import re
import uuid
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
        staged = {"external_id": identity, "source_url": url,
                  "inventory_observed_at": row["inventoryObservedAt"], "detail_observed_at": row["detailObservedAt"]}
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


def _receipt(staged, binding):
    required = {"artifact_sha256", "database_target_sha256", "producer_sha", "generation_id", "started_at", "finished_at", "attempt_id"}
    if not isinstance(binding, dict) or set(binding) != required:
        raise ValueError("invalid Matthews receipt binding")
    for key, pattern in (("artifact_sha256", r"[a-f0-9]{64}"), ("database_target_sha256", r"[a-f0-9]{64}"),
                         ("producer_sha", r"[a-f0-9]{40}"), ("attempt_id", r"[a-f0-9]{32}")):
        if not isinstance(binding.get(key), str) or not re.fullmatch(pattern, binding[key]):
            raise ValueError("invalid Matthews receipt identity")
    if not isinstance(binding["generation_id"], str) or not binding["generation_id"]:
        raise ValueError("invalid Matthews receipt generation")
    if parse_strict_freshness_timestamp(binding["started_at"], field="attempt start") > parse_strict_freshness_timestamp(binding["finished_at"], field="attempt finish"):
        raise ValueError("invalid Matthews receipt clocks")
    if not isinstance(staged, list) or not staged or any(not isinstance(row, dict) or set(row) != {"external_id", "source_url", "inventory_observed_at", "detail_observed_at", *FIELDS} for row in staged):
        raise ValueError("invalid Matthews staged rows")
    canonical = json.dumps({"binding": binding, "staged": staged}, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    key = "matthews-partial-v1:" + binding["artifact_sha256"]
    job = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
    return {"digest": digest, "key": key, "job": job}


def _postimage_query(payload):
    return f"""SELECT encode(sha256(convert_to(COALESCE(jsonb_agg(jsonb_build_object(
 'parent',to_jsonb(l),
 'source_index',COALESCE((SELECT jsonb_agg(to_jsonb(si) ORDER BY si.id) FROM credeals.cre_source_index si WHERE si.brokerage_id=l.brokerage_id AND (si.external_id=l.external_id OR si.url=l.source_url)),'[]'::jsonb),
 'contacts',COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.id) FROM credeals.cre_listing_contacts c WHERE c.listing_id=l.id),'[]'::jsonb),
 'documents',COALESCE((SELECT jsonb_agg(to_jsonb(d) ORDER BY d.id) FROM credeals.cre_listing_documents d WHERE d.listing_id=l.id),'[]'::jsonb),
 'images',COALESCE((SELECT jsonb_agg(to_jsonb(i) ORDER BY i.id) FROM credeals.cre_listing_images i WHERE i.listing_id=l.id),'[]'::jsonb)
 ) ORDER BY l.external_id)::text,'[]'),'UTF8')),'hex')
 FROM jsonb_array_elements({payload}::jsonb) c(row)
 JOIN credeals.cre_brokerages b ON b.slug='matthews'
 JOIN credeals.cre_listings l ON l.brokerage_id=b.id AND l.external_id=c.row->>'external_id'
 WHERE NOT (c.row->>'external_id' IN (SELECT value FROM jsonb_array_elements_text(held) value))"""


def fill_sql(staged, binding):
    """Produce one serializable transaction; never changes lifecycle or children.

    Only exact existing identity/URL matches without source-index contradictions
    qualify. Parent field and URL comparisons are made under row locks; the
    transaction refuses conflicting source-index or listing URL ownership.
    The scrape-job receipt binds the attempted artifact and final row/child
    digest. Caller must settle its writer and re-read the exact target before
    using the separate readback to reconcile an uncertain commit.
    """
    if not isinstance(staged, list) or not staged:
        raise ValueError("no staged Matthews observations")
    receipt = _receipt(staged, binding)
    payload = sql_lit(json.dumps(staged, ensure_ascii=True, separators=(",", ":"), allow_nan=False))
    definitions = ", ".join(f"{field} {pg_type}" for field, pg_type in FIELDS.items())
    assignments = ", ".join(f"{field}=COALESCE(l.{field}, c.{field})" for field in FIELDS)
    comparisons = " OR ".join(f"(l.{field} IS NULL AND c.{field} IS NOT NULL)" for field in FIELDS)
    excluded = ",".join(sql_lit(field) for field in (*FIELDS, "updated_at"))
    field_checks = " OR ".join(
        f"to_jsonb(l)->{sql_lit(field)} IS DISTINCT FROM CASE WHEN h.external_id IS NOT NULL "
        f"THEN p.full_row->{sql_lit(field)} ELSE COALESCE(NULLIF(p.full_row->{sql_lit(field)}, 'null'::jsonb), "
        f"to_jsonb(c)->{sql_lit(field)}) END" for field in FIELDS
    )
    return f"""
BEGIN ISOLATION LEVEL SERIALIZABLE;
SET LOCAL TIME ZONE 'UTC';
SET LOCAL extra_float_digits=3;
{lifecycle_transaction_lock_sql()}
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM credeals.cre_scrape_jobs WHERE id={sql_lit(receipt["job"])}::uuid OR artifact_run_key={sql_lit(receipt["key"])}) THEN
   RAISE EXCEPTION 'Matthews partial receipt already exists; reconcile before replay';
 END IF;
END $$;
CREATE TEMP TABLE _matthews_candidates ON COMMIT DROP AS
SELECT * FROM jsonb_to_recordset({payload}::jsonb)
 AS x(external_id text, source_url text, inventory_observed_at timestamptz, detail_observed_at timestamptz, {definitions});
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
 AND (l.scraped_at IS NULL OR (isfinite(l.scraped_at) AND l.scraped_at <= c.detail_observed_at))
 AND (l.canonical_url IS NULL OR l.canonical_url=c.source_url)
 AND NOT EXISTS (SELECT 1 FROM credeals.cre_listings other
   WHERE other.brokerage_id=b.id AND other.id<>l.id
   AND (other.source_url=c.source_url OR other.canonical_url=c.source_url))
 AND NOT EXISTS (SELECT 1 FROM credeals.cre_source_index si
   WHERE si.brokerage_id=b.id AND ((si.external_id=c.external_id
     AND (si.source_key IS DISTINCT FROM 'matthews' OR si.url IS DISTINCT FROM c.source_url
       OR si.soft_deleted IS TRUE OR si.observation_present IS FALSE
       OR (si.last_seen IS NOT NULL AND (NOT isfinite(si.last_seen) OR si.last_seen > c.inventory_observed_at))
       OR (si.last_enumerated_at IS NOT NULL AND (NOT isfinite(si.last_enumerated_at) OR si.last_enumerated_at > c.inventory_observed_at))))
     OR (si.external_id<>c.external_id AND si.url=c.source_url)));
CREATE TEMP TABLE _matthews_before ON COMMIT DROP AS
SELECT l.id, to_jsonb(l) AS full_row, to_jsonb(l) - ARRAY[{excluded}]::text[] AS parent,
 COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.id) FROM credeals.cre_listing_contacts c WHERE c.listing_id=l.id),'[]'::jsonb) AS contacts,
 COALESCE((SELECT jsonb_agg(to_jsonb(d) ORDER BY d.id) FROM credeals.cre_listing_documents d WHERE d.listing_id=l.id),'[]'::jsonb) AS documents,
 COALESCE((SELECT jsonb_agg(to_jsonb(i) ORDER BY i.id) FROM credeals.cre_listing_images i WHERE i.listing_id=l.id),'[]'::jsonb) AS images
FROM credeals.cre_listings l JOIN _matthews_eligible e ON e.id=l.id;
CREATE TEMP TABLE _matthews_held ON COMMIT DROP AS
SELECT c.external_id, 'identity_conflict_or_unknown'::text AS reason FROM _matthews_candidates c
LEFT JOIN _matthews_eligible e USING (external_id) WHERE e.id IS NULL;
CREATE TEMP TABLE _matthews_updated (id uuid PRIMARY KEY) ON COMMIT DROP;
DO $$ DECLARE candidate record; changed uuid;
BEGIN
 FOR candidate IN SELECT external_id FROM _matthews_eligible ORDER BY external_id LOOP
   BEGIN
     changed := NULL;
     UPDATE credeals.cre_listings l SET {assignments}
     FROM _matthews_candidates c JOIN _matthews_eligible e USING (external_id)
     WHERE l.id=e.id AND c.external_id=candidate.external_id AND ({comparisons})
     RETURNING l.id INTO changed;
     IF changed IS NOT NULL THEN INSERT INTO _matthews_updated VALUES (changed); END IF;
   EXCEPTION WHEN check_violation OR string_data_right_truncation OR numeric_value_out_of_range
       OR invalid_text_representation OR foreign_key_violation OR not_null_violation OR unique_violation THEN
     INSERT INTO _matthews_held VALUES (candidate.external_id,'record_write_rejected');
   END;
 END LOOP;
END $$;
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM _matthews_before p JOIN credeals.cre_listings l ON l.id=p.id
 JOIN _matthews_candidates c ON c.external_id=l.external_id
 LEFT JOIN _matthews_held h ON h.external_id=c.external_id
 WHERE ({field_checks}) OR to_jsonb(l) - ARRAY[{excluded}]::text[] IS DISTINCT FROM p.parent
 OR COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.id) FROM credeals.cre_listing_contacts c WHERE c.listing_id=l.id),'[]'::jsonb) IS DISTINCT FROM p.contacts
 OR COALESCE((SELECT jsonb_agg(to_jsonb(d) ORDER BY d.id) FROM credeals.cre_listing_documents d WHERE d.listing_id=l.id),'[]'::jsonb) IS DISTINCT FROM p.documents
 OR COALESCE((SELECT jsonb_agg(to_jsonb(i) ORDER BY i.id) FROM credeals.cre_listing_images i WHERE i.listing_id=l.id),'[]'::jsonb) IS DISTINCT FROM p.images)
 THEN RAISE EXCEPTION 'Matthews preserved parent or children changed'; END IF;
END $$;
INSERT INTO credeals.cre_scrape_jobs
 (id,brokerage_id,status,started_at,completed_at,listings_discovered,listings_scraped,listings_saved,errors_count,artifact_run_key,notes)
SELECT {sql_lit(receipt["job"])}::uuid,b.id,'partial',
 {sql_lit(binding["started_at"])}::timestamptz,{sql_lit(binding["finished_at"])}::timestamptz,
 (SELECT count(*) FROM _matthews_candidates),
 (SELECT count(*) FROM _matthews_candidates) - (SELECT count(*) FROM _matthews_held),
 (SELECT count(*) FROM _matthews_updated),1,{sql_lit(receipt["key"])},
 jsonb_build_object('schema','matthews-partial-receipt-v1','binding_sha256',{sql_lit(receipt["digest"])},
 'held',held,'held_reasons',held_reasons,'updated',updated,'postimage_sha256',({_postimage_query(payload)}))::text
FROM credeals.cre_brokerages b
CROSS JOIN LATERAL (SELECT COALESCE(jsonb_agg(external_id ORDER BY external_id),'[]'::jsonb) held,
 COALESCE(jsonb_object_agg(external_id,reason),'{{}}'::jsonb) held_reasons FROM _matthews_held) h
CROSS JOIN LATERAL (SELECT COALESCE(jsonb_agg(c.external_id ORDER BY c.external_id),'[]'::jsonb) updated
 FROM _matthews_candidates c JOIN _matthews_eligible e USING (external_id) JOIN _matthews_updated u ON u.id=e.id) u
WHERE b.slug='matthews';
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM credeals.cre_scrape_jobs WHERE id={sql_lit(receipt["job"])}::uuid) THEN
   RAISE EXCEPTION 'Matthews partial receipt missing'; END IF;
END $$;
COMMIT;
"""


def readback_sql(staged, binding):
    """Return a receipt match count and its held/updated partition, never rows."""
    receipt = _receipt(staged, binding)
    payload = sql_lit(json.dumps(staged, ensure_ascii=True, separators=(",", ":"), allow_nan=False))
    return f"""
SET TIME ZONE 'UTC';
SET extra_float_digits=3;
WITH matched AS (
 SELECT j.notes::jsonb AS receipt FROM credeals.cre_scrape_jobs j
 JOIN credeals.cre_brokerages b ON b.id=j.brokerage_id AND b.slug='matthews'
 WHERE j.id={sql_lit(receipt["job"])}::uuid AND j.artifact_run_key={sql_lit(receipt["key"])}
 AND j.status='partial' AND j.notes::jsonb->>'schema'='matthews-partial-receipt-v1'
 AND j.notes::jsonb->>'binding_sha256'={sql_lit(receipt["digest"])}
), verified AS (
 SELECT receipt FROM matched, LATERAL (SELECT receipt->'held' AS held) h
 WHERE jsonb_typeof(held)='array' AND jsonb_typeof(receipt->'updated')='array'
 AND receipt->>'postimage_sha256'=({_postimage_query(payload)})
)
SELECT jsonb_build_object('occupied_jobs',(SELECT count(*) FROM credeals.cre_scrape_jobs WHERE id={sql_lit(receipt["job"])}::uuid OR artifact_run_key={sql_lit(receipt["key"])}),
 'matching_jobs',(SELECT count(*) FROM matched),
 'verified_jobs',(SELECT count(*) FROM verified),
 'held',COALESCE((SELECT receipt->'held' FROM verified),'[]'::jsonb),
 'held_reasons',COALESCE((SELECT receipt->'held_reasons' FROM verified),'{{}}'::jsonb),
 'updated',COALESCE((SELECT receipt->'updated' FROM verified),'[]'::jsonb));
"""
