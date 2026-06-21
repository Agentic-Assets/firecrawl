#!/usr/bin/env python3
"""
Read-only data-quality audit for the live CRE listings database.

The audit uses the same env-file and psql discovery helpers as cre_ingest.py,
prints only the credential file path, and writes a timestamped markdown report
by default under out/data_quality/. It is intentionally investigative rather
than a migration validator: findings are grouped by severity, with per-source
counts and samples to make recurring data drift easy to spot.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cre_ingest import find_psql, load_db_url


SOURCE_KEY_SQL = """
CASE
  WHEN b.slug = 'cbre' AND l.external_id LIKE 'dealflow:%' THEN 'cbre-dealflow'
  WHEN b.slug = 'jll' AND l.external_id LIKE 'investor:%' THEN 'jll-investor'
  WHEN b.slug = 'colliers' AND l.external_id LIKE 'main:%' THEN 'colliers-main'
  WHEN NULLIF(l.raw_data->>'sourceKey', '') IS NOT NULL THEN l.raw_data->>'sourceKey'
  ELSE b.slug
END
"""


@dataclass(frozen=True)
class AuditQuery:
    title: str
    sql: str
    note: str = ""


QUERIES: dict[str, AuditQuery] = {
    "object_health": AuditQuery(
        "Object Health",
        """
WITH expected_tables(name) AS (VALUES
  ('cre_brokerages'),('cre_listings'),('cre_listing_contacts'),
  ('cre_listing_documents'),('cre_listing_images'),('cre_listing_media'),
  ('cre_listing_links'),('cre_listing_om_facts'),('cre_listing_events'),
  ('cre_source_index'),('cre_source_baseline'),('cre_enrichment_queue'),
  ('cre_listing_price_history'),('cre_zip_cbsa_crosswalk')
), expected_views(name) AS (VALUES
  ('v_cre_listings_full'),('v_cre_active_for_sale'),('v_cre_active_for_lease'),
  ('v_cre_recent_changes'),('v_cre_market_summary'),
  ('v_cre_enrichment_queue_pending'),('v_cre_enrichment_dead')
), expected_columns(table_name, column_name) AS (VALUES
  ('cre_source_index','prior_sale_price'),('cre_source_index','prior_lease_rate'),
  ('cre_source_index','prior_status'),('cre_listings','building_class'),
  ('cre_listings','property_subtype'),('cre_listings','cbsa_name'),
  ('cre_listings','extra_facts'),('cre_listing_contacts','profile_url'),
  ('cre_listing_contacts','avatar_url'),('cre_listing_contacts','vcard_url'),
  ('cre_listing_contacts','license')
), table_missing AS (
  SELECT name FROM expected_tables WHERE to_regclass('credeals.' || name) IS NULL
), view_bad AS (
  SELECT e.name, coalesce(array_to_string(c.reloptions, ','), '') AS reloptions
  FROM expected_views e
  LEFT JOIN pg_class c ON c.oid = to_regclass('credeals.' || e.name)
  WHERE c.oid IS NULL
     OR coalesce(array_to_string(c.reloptions, ','), '') NOT LIKE '%security_invoker=true%'
), cols AS (
  SELECT e.table_name, e.column_name, c.column_name IS NOT NULL AS present
  FROM expected_columns e
  LEFT JOIN information_schema.columns c
    ON c.table_schema = 'credeals'
   AND c.table_name = e.table_name
   AND c.column_name = e.column_name
)
SELECT 'missing_tables' AS check_name, count(*)::bigint AS count,
       coalesce(jsonb_agg(name ORDER BY name), '[]'::jsonb) AS detail
FROM table_missing
UNION ALL
SELECT 'bad_or_missing_security_invoker_views', count(*)::bigint,
       coalesce(jsonb_agg(jsonb_build_object('view', name, 'reloptions', reloptions) ORDER BY name), '[]'::jsonb)
FROM view_bad
UNION ALL
SELECT 'required_columns_missing', count(*)::bigint,
       coalesce(jsonb_agg(jsonb_build_object('table', table_name, 'column', column_name)
                          ORDER BY table_name, column_name), '[]'::jsonb)
FROM cols
WHERE NOT present;
""",
    ),
    "source_summary": AuditQuery(
        "Source Summary",
        f"""
WITH base AS (
  SELECT {SOURCE_KEY_SQL} AS source_key, b.slug AS brokerage_slug, l.*
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
), active AS (
  SELECT * FROM base WHERE deleted_at IS NULL
), latest AS (
  SELECT source_key, max(scraped_at) AS latest_scraped_at FROM active GROUP BY source_key
), soft_deleted AS (
  SELECT source_key, count(*) AS soft_deleted
  FROM base
  WHERE deleted_at IS NOT NULL
  GROUP BY source_key
)
SELECT
  a.source_key,
  min(a.brokerage_slug) AS brokerage_slug,
  count(*)::bigint AS active_rows,
  count(*) FILTER (WHERE a.status IN ('active','under_contract','pending'))::bigint AS on_market_rows,
  count(*) FILTER (WHERE a.transaction_type = 'sale')::bigint AS sale_rows,
  count(*) FILTER (WHERE a.transaction_type = 'lease')::bigint AS lease_rows,
  count(*) FILTER (WHERE a.transaction_type = 'sale_or_lease')::bigint AS sale_or_lease_rows,
  count(*) FILTER (WHERE a.scraped_at = latest.latest_scraped_at)::bigint AS latest_batch_rows,
  latest.latest_scraped_at,
  coalesce(max(soft_deleted.soft_deleted), 0)::bigint AS soft_deleted_rows
FROM active a
JOIN latest USING (source_key)
LEFT JOIN soft_deleted USING (source_key)
GROUP BY a.source_key, latest.latest_scraped_at
ORDER BY a.source_key;
""",
    ),
    "listing_quality_by_source": AuditQuery(
        "Listing Quality By Source",
        f"""
WITH active AS (
  SELECT {SOURCE_KEY_SQL} AS source_key, l.*
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE l.deleted_at IS NULL
)
SELECT
  source_key,
  count(*)::bigint AS rows_checked,
  count(*) FILTER (WHERE source_url IS NULL OR source_url !~* '^https?://')::bigint AS bad_source_url,
  count(*) FILTER (WHERE canonical_url IS NOT NULL AND canonical_url !~* '^https?://')::bigint AS bad_canonical_url,
  count(*) FILTER (WHERE title IS NULL OR btrim(title) = '')::bigint AS missing_title,
  count(*) FILTER (WHERE raw_data IS NULL OR raw_data = '{{}}'::jsonb)::bigint AS missing_raw_data,
  count(*) FILTER (WHERE transaction_type IS NULL)::bigint AS missing_transaction_type,
  count(*) FILTER (WHERE property_type IS NULL)::bigint AS missing_property_type,
  count(*) FILTER (WHERE city IS NULL OR btrim(city) = '')::bigint AS missing_city,
  count(*) FILTER (WHERE state IS NULL OR btrim(state::text) = '')::bigint AS missing_state,
  count(*) FILTER (WHERE state IS NOT NULL AND state::text !~ '^[A-Z]{{2}}$')::bigint AS invalid_state,
  count(*) FILTER (WHERE lat IS NULL OR lng IS NULL)::bigint AS missing_coords,
  count(*) FILTER (WHERE lat IS NOT NULL AND (lat < -90 OR lat > 90))::bigint AS impossible_lat,
  count(*) FILTER (WHERE lng IS NOT NULL AND (lng < -180 OR lng > 180))::bigint AS impossible_lng,
  count(*) FILTER (WHERE cbsa_name IS NULL OR btrim(cbsa_name) = '')::bigint AS missing_cbsa_name,
  count(*) FILTER (WHERE country IS NULL OR btrim(country::text) = '')::bigint AS missing_country
FROM active
GROUP BY source_key
ORDER BY source_key;
""",
    ),
    "field_fill_by_source": AuditQuery(
        "High-Value Field Fill Rates",
        f"""
WITH active AS (
  SELECT {SOURCE_KEY_SQL} AS source_key, l.*
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE l.deleted_at IS NULL
)
SELECT
  source_key,
  count(*)::bigint AS rows_checked,
  round(100.0 * count(*) FILTER (WHERE canonical_url IS NOT NULL) / nullif(count(*), 0), 1) AS canonical_url_pct,
  round(100.0 * count(*) FILTER (WHERE county IS NOT NULL) / nullif(count(*), 0), 1) AS county_pct,
  round(100.0 * count(*) FILTER (WHERE cbsa_name IS NOT NULL) / nullif(count(*), 0), 1) AS cbsa_pct,
  round(100.0 * count(*) FILTER (WHERE property_subtype IS NOT NULL) / nullif(count(*), 0), 1) AS subtype_pct,
  round(100.0 * count(*) FILTER (WHERE building_class IS NOT NULL) / nullif(count(*), 0), 1) AS building_class_pct,
  round(100.0 * count(*) FILTER (WHERE size_sf IS NOT NULL OR lot_size_sf IS NOT NULL OR available_sf IS NOT NULL) / nullif(count(*), 0), 1) AS size_signal_pct,
  round(100.0 * count(*) FILTER (WHERE sale_price_usd IS NOT NULL OR sale_price_per_sf IS NOT NULL OR lease_rate_min IS NOT NULL OR lease_rate_max IS NOT NULL) / nullif(count(*), 0), 1) AS price_signal_pct,
  round(100.0 * count(*) FILTER (WHERE cap_rate IS NOT NULL OR noi IS NOT NULL OR occupancy_rate IS NOT NULL) / nullif(count(*), 0), 1) AS underwriting_signal_pct
FROM active
GROUP BY source_key
ORDER BY source_key;
""",
        "Percentages are fill rates over active rows. Low values can be expected for sources that do not expose those fields.",
    ),
    "numeric_anomaly_summary": AuditQuery(
        "Numeric Anomaly Summary",
        f"""
WITH active AS (
  SELECT {SOURCE_KEY_SQL} AS source_key, l.*
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE l.deleted_at IS NULL
), anomalies AS (
  SELECT source_key, 'sale_price_usd' AS field_name
  FROM active
  WHERE sale_price_usd IS NOT NULL AND (sale_price_usd <= 0 OR sale_price_usd > 20000000000)
  UNION ALL
  SELECT source_key, 'sale_price_per_sf'
  FROM active
  WHERE sale_price_per_sf IS NOT NULL AND (sale_price_per_sf <= 0 OR sale_price_per_sf > 10000)
  UNION ALL
  SELECT source_key, 'lease_rate_min'
  FROM active
  WHERE lease_rate_min IS NOT NULL AND (lease_rate_min <= 0 OR lease_rate_min > 500)
  UNION ALL
  SELECT source_key, 'lease_rate_max'
  FROM active
  WHERE lease_rate_max IS NOT NULL AND (lease_rate_max <= 0 OR lease_rate_max > 500)
  UNION ALL
  SELECT source_key, 'lease_rate_range'
  FROM active
  WHERE lease_rate_min IS NOT NULL AND lease_rate_max IS NOT NULL AND lease_rate_min > lease_rate_max
  UNION ALL
  SELECT source_key, 'cap_rate'
  FROM active
  WHERE cap_rate IS NOT NULL AND (cap_rate <= 0 OR cap_rate >= 0.5)
  UNION ALL
  SELECT source_key, 'occupancy_rate'
  FROM active
  WHERE occupancy_rate IS NOT NULL AND (occupancy_rate < 0 OR occupancy_rate > 1)
)
SELECT source_key, field_name, count(*)::bigint AS flagged_rows
FROM anomalies
GROUP BY source_key, field_name
ORDER BY source_key, field_name;
""",
    ),
    "numeric_anomalies": AuditQuery(
        "Numeric Anomaly Samples",
        f"""
WITH active AS (
  SELECT {SOURCE_KEY_SQL} AS source_key, l.*
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE l.deleted_at IS NULL
), anomalies AS (
  SELECT source_key, external_id, title, source_url, 'sale_price_usd' AS field_name,
         sale_price_usd::text AS value
  FROM active
  WHERE sale_price_usd IS NOT NULL AND (sale_price_usd <= 0 OR sale_price_usd > 20000000000)
  UNION ALL
  SELECT source_key, external_id, title, source_url, 'sale_price_per_sf', sale_price_per_sf::text
  FROM active
  WHERE sale_price_per_sf IS NOT NULL AND (sale_price_per_sf <= 0 OR sale_price_per_sf > 10000)
  UNION ALL
  SELECT source_key, external_id, title, source_url, 'lease_rate_min', lease_rate_min::text
  FROM active
  WHERE lease_rate_min IS NOT NULL AND (lease_rate_min <= 0 OR lease_rate_min > 500)
  UNION ALL
  SELECT source_key, external_id, title, source_url, 'lease_rate_max', lease_rate_max::text
  FROM active
  WHERE lease_rate_max IS NOT NULL AND (lease_rate_max <= 0 OR lease_rate_max > 500)
  UNION ALL
  SELECT source_key, external_id, title, source_url, 'lease_rate_range', lease_rate_min::text || ' > ' || lease_rate_max::text
  FROM active
  WHERE lease_rate_min IS NOT NULL AND lease_rate_max IS NOT NULL AND lease_rate_min > lease_rate_max
  UNION ALL
  SELECT source_key, external_id, title, source_url, 'cap_rate', cap_rate::text
  FROM active
  WHERE cap_rate IS NOT NULL AND (cap_rate <= 0 OR cap_rate >= 0.5)
  UNION ALL
  SELECT source_key, external_id, title, source_url, 'occupancy_rate', occupancy_rate::text
  FROM active
  WHERE occupancy_rate IS NOT NULL AND (occupancy_rate < 0 OR occupancy_rate > 1)
)
SELECT *
FROM anomalies
ORDER BY source_key, field_name, external_id
LIMIT 100;
""",
    ),
    "missing_field_samples": AuditQuery(
        "Missing Or Invalid Field Samples",
        f"""
WITH active AS (
  SELECT {SOURCE_KEY_SQL} AS source_key, l.*
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE l.deleted_at IS NULL
), misses AS (
  SELECT source_key, external_id, title, city, state::text AS state, source_url,
         'missing_title' AS issue
  FROM active
  WHERE title IS NULL OR btrim(title) = ''
  UNION ALL
  SELECT source_key, external_id, title, city, state::text, source_url,
         'missing_state'
  FROM active
  WHERE state IS NULL OR btrim(state::text) = ''
  UNION ALL
  SELECT source_key, external_id, title, city, state::text, source_url,
         'missing_property_type'
  FROM active
  WHERE property_type IS NULL
  UNION ALL
  SELECT source_key, external_id, title, city, state::text, source_url,
         'missing_transaction_type'
  FROM active
  WHERE transaction_type IS NULL
  UNION ALL
  SELECT source_key, external_id, title, city, state::text, source_url,
         'bad_source_url'
  FROM active
  WHERE source_url IS NULL OR source_url !~* '^https?://'
)
SELECT *
FROM misses
ORDER BY issue, source_key, external_id
LIMIT 120;
""",
    ),
    "duplicate_external_ids": AuditQuery(
        "Duplicate External ID Groups",
        f"""
WITH active AS (
  SELECT {SOURCE_KEY_SQL} AS source_key, l.*
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE l.deleted_at IS NULL
), dupes AS (
  SELECT source_key, brokerage_id, external_id, count(*) AS rows,
         jsonb_agg(jsonb_build_object('id', id, 'title', title, 'source_url', source_url)
                   ORDER BY scraped_at DESC) AS sample_rows
  FROM active
  WHERE external_id IS NOT NULL
  GROUP BY source_key, brokerage_id, external_id
  HAVING count(*) > 1
)
SELECT source_key, external_id, rows, sample_rows
FROM dupes
ORDER BY rows DESC, source_key, external_id
LIMIT 50;
""",
    ),
    "duplicate_source_url_summary": AuditQuery(
        "Duplicate Source URL Summary",
        f"""
WITH active AS (
  SELECT {SOURCE_KEY_SQL} AS source_key, l.*
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE l.deleted_at IS NULL
), dupes AS (
  SELECT source_key, source_url, count(*) AS rows
  FROM active
  WHERE source_url IS NOT NULL
  GROUP BY source_key, source_url
  HAVING count(*) > 1
)
SELECT source_key, count(*)::bigint AS duplicate_url_groups, sum(rows)::bigint AS rows_in_duplicate_groups
FROM dupes
GROUP BY source_key
ORDER BY source_key;
""",
    ),
    "duplicate_source_urls": AuditQuery(
        "Duplicate Source URL Groups",
        f"""
WITH active AS (
  SELECT {SOURCE_KEY_SQL} AS source_key, l.*
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE l.deleted_at IS NULL
), dupes AS (
  SELECT source_key, source_url, count(*) AS rows,
         jsonb_agg(jsonb_build_object('external_id', external_id, 'title', title)
                   ORDER BY scraped_at DESC) AS sample_rows
  FROM active
  WHERE source_url IS NOT NULL
  GROUP BY source_key, source_url
  HAVING count(*) > 1
)
SELECT source_key, source_url, rows, sample_rows
FROM dupes
ORDER BY rows DESC, source_key, source_url
LIMIT 80;
""",
    ),
    "child_coverage_by_source": AuditQuery(
        "Child Row Coverage By Source",
        f"""
WITH active AS (
  SELECT l.id, {SOURCE_KEY_SQL} AS source_key
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE l.deleted_at IS NULL
), per_listing AS (
  SELECT
    a.source_key,
    a.id,
    (SELECT count(*) FROM credeals.cre_listing_contacts c WHERE c.listing_id = a.id) AS contacts,
    (SELECT count(*) FROM credeals.cre_listing_documents d WHERE d.listing_id = a.id) AS documents,
    (SELECT count(*) FROM credeals.cre_listing_images i WHERE i.listing_id = a.id) AS images,
    (SELECT count(*) FROM credeals.cre_listing_media m WHERE m.listing_id = a.id) AS media,
    (SELECT count(*) FROM credeals.cre_listing_links lnk WHERE lnk.listing_id = a.id) AS links,
    (SELECT count(*) FROM credeals.cre_listing_om_facts f WHERE f.listing_id = a.id) AS om_facts
  FROM active a
)
SELECT
  source_key,
  count(*)::bigint AS active_rows,
  sum(contacts)::bigint AS contact_rows,
  sum(documents)::bigint AS document_rows,
  sum(images)::bigint AS image_rows,
  sum(media)::bigint AS media_rows,
  sum(links)::bigint AS link_rows,
  sum(om_facts)::bigint AS om_fact_rows,
  count(*) FILTER (WHERE contacts = 0)::bigint AS listings_without_contacts,
  count(*) FILTER (WHERE documents = 0)::bigint AS listings_without_documents,
  count(*) FILTER (WHERE images = 0)::bigint AS listings_without_images
FROM per_listing
GROUP BY source_key
ORDER BY source_key;
""",
    ),
    "bad_child_urls": AuditQuery(
        "Bad Child URL Samples",
        """
WITH bad AS (
  SELECT 'document_url' AS issue, l.external_id, l.title, d.url
  FROM credeals.cre_listing_documents d
  JOIN credeals.cre_listings l ON l.id = d.listing_id
  WHERE l.deleted_at IS NULL AND d.url !~* '^https?://'
  UNION ALL
  SELECT 'image_url', l.external_id, l.title, i.url
  FROM credeals.cre_listing_images i
  JOIN credeals.cre_listings l ON l.id = i.listing_id
  WHERE l.deleted_at IS NULL AND i.url !~* '^https?://'
  UNION ALL
  SELECT 'profile_url', l.external_id, l.title, c.profile_url
  FROM credeals.cre_listing_contacts c
  JOIN credeals.cre_listings l ON l.id = c.listing_id
  WHERE l.deleted_at IS NULL AND c.profile_url IS NOT NULL AND c.profile_url !~* '^https?://'
  UNION ALL
  SELECT 'avatar_url', l.external_id, l.title, c.avatar_url
  FROM credeals.cre_listing_contacts c
  JOIN credeals.cre_listings l ON l.id = c.listing_id
  WHERE l.deleted_at IS NULL AND c.avatar_url IS NOT NULL AND c.avatar_url !~* '^https?://'
  UNION ALL
  SELECT 'vcard_url', l.external_id, l.title, c.vcard_url
  FROM credeals.cre_listing_contacts c
  JOIN credeals.cre_listings l ON l.id = c.listing_id
  WHERE l.deleted_at IS NULL AND c.vcard_url IS NOT NULL AND c.vcard_url !~* '^https?://'
)
SELECT *
FROM bad
ORDER BY issue, external_id
LIMIT 120;
""",
    ),
    "orphans": AuditQuery(
        "Orphan Checks",
        """
SELECT 'contacts_without_parent' AS check_name, count(*)::bigint AS count
FROM credeals.cre_listing_contacts c
LEFT JOIN credeals.cre_listings l ON l.id = c.listing_id
WHERE l.id IS NULL
UNION ALL
SELECT 'documents_without_parent', count(*)::bigint
FROM credeals.cre_listing_documents d
LEFT JOIN credeals.cre_listings l ON l.id = d.listing_id
WHERE l.id IS NULL
UNION ALL
SELECT 'images_without_parent', count(*)::bigint
FROM credeals.cre_listing_images i
LEFT JOIN credeals.cre_listings l ON l.id = i.listing_id
WHERE l.id IS NULL
UNION ALL
SELECT 'media_without_parent', count(*)::bigint
FROM credeals.cre_listing_media m
LEFT JOIN credeals.cre_listings l ON l.id = m.listing_id
WHERE l.id IS NULL
UNION ALL
SELECT 'links_without_parent', count(*)::bigint
FROM credeals.cre_listing_links lk
LEFT JOIN credeals.cre_listings l ON l.id = lk.listing_id
WHERE l.id IS NULL
UNION ALL
SELECT 'om_facts_without_parent', count(*)::bigint
FROM credeals.cre_listing_om_facts f
LEFT JOIN credeals.cre_listings l ON l.id = f.listing_id
WHERE l.id IS NULL
ORDER BY check_name;
""",
    ),
    "queue_health": AuditQuery(
        "Enrichment Queue Health",
        """
SELECT
  (SELECT count(*) FROM credeals.v_cre_enrichment_queue_pending)::bigint AS pending_rows,
  (SELECT count(*) FROM credeals.v_cre_enrichment_dead)::bigint AS dead_rows,
  (SELECT count(*) FROM credeals.cre_enrichment_queue
    WHERE done_at IS NULL AND claimed_at IS NOT NULL AND attempts < 5)::bigint AS claimed_live_rows,
  (SELECT count(*) FROM credeals.cre_enrichment_queue
    WHERE done_at IS NULL AND claimed_at IS NOT NULL AND attempts < 5
      AND claimed_at < now() - interval '2 hours')::bigint AS stale_claimed_rows,
  (SELECT min(enqueued_at) FROM credeals.v_cre_enrichment_queue_pending) AS oldest_pending_at,
  (SELECT max(enqueued_at) FROM credeals.v_cre_enrichment_queue_pending) AS newest_pending_at;
""",
    ),
    "source_index_health": AuditQuery(
        "Source Index Health",
        """
SELECT
  source_key,
  count(*)::bigint AS index_rows,
  count(*) FILTER (WHERE soft_deleted IS TRUE)::bigint AS soft_deleted_index_rows,
  count(*) FILTER (WHERE last_enumerated_at IS NULL)::bigint AS missing_last_enumerated,
  count(*) FILTER (WHERE last_enumerated_at < now() - interval '8 days')::bigint AS stale_last_enumerated,
  min(last_enumerated_at) AS oldest_last_enumerated,
  max(last_enumerated_at) AS newest_last_enumerated
FROM credeals.cre_source_index
GROUP BY source_key
ORDER BY source_key;
""",
    ),
    "recent_jobs": AuditQuery(
        "Recent Scrape Jobs",
        """
WITH ranked AS (
  SELECT
    b.slug AS brokerage_slug,
    j.status,
    j.listings_discovered,
    j.listings_scraped,
    j.listings_saved,
    j.errors_count,
    j.started_at,
    j.completed_at,
    row_number() OVER (PARTITION BY b.slug ORDER BY j.started_at DESC NULLS LAST) AS rn
  FROM credeals.cre_scrape_jobs j
  JOIN credeals.cre_brokerages b ON b.id = j.brokerage_id
)
SELECT brokerage_slug, status, listings_discovered, listings_scraped, listings_saved,
       errors_count, started_at, completed_at
FROM ranked
WHERE rn = 1
ORDER BY brokerage_slug;
""",
    ),
    "search_smoke": AuditQuery(
        "Search Function Smoke",
        """
SELECT 'industrial_tx_sale' AS smoke_name, count(*)::bigint AS result_rows
FROM credeals.search_cre_listings('industrial', NULL, 'TX', NULL, 'sale')
UNION ALL
SELECT 'office_sale', count(*)::bigint
FROM credeals.search_cre_listings('office', NULL, NULL, NULL, 'sale')
UNION ALL
SELECT 'retail_lease', count(*)::bigint
FROM credeals.search_cre_listings('retail', NULL, NULL, NULL, 'lease')
UNION ALL
SELECT 'national_avenue', count(*)::bigint
FROM credeals.search_cre_listings('National Avenue', NULL, NULL, NULL, NULL)
ORDER BY smoke_name;
""",
    ),
}


def slug_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def default_report_path() -> Path:
    root = Path(__file__).resolve().parent
    return root / "out" / "data_quality" / f"cre_data_quality_audit_{slug_timestamp()}.md"


def json_dumps(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


def run_json_query(psql: str, db_url: str, sql: str, timeout_ms: int) -> tuple[list[dict[str, Any]], str]:
    wrapped = f"""
BEGIN READ ONLY;
SET LOCAL statement_timeout = {int(timeout_ms)};
SELECT coalesce(jsonb_agg(to_jsonb(q)), '[]'::jsonb)
FROM (
{sql.rstrip().rstrip(';')}
) q;
ROLLBACK;
"""
    env = os.environ.copy()
    env["PGOPTIONS"] = (
        "-c default_transaction_read_only=on "
        f"-c statement_timeout={int(timeout_ms)}"
    )
    proc = subprocess.run(
        [psql, db_url, "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1"],
        input=wrapped,
        text=True,
        capture_output=True,
        env=env,
        timeout=max(5, timeout_ms // 1000 + 10),
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"psql exited {proc.returncode}")
    payload = proc.stdout.strip()
    return json.loads(payload or "[]"), normalize_warning(proc.stderr)


def normalize_warning(stderr: str) -> str:
    if not stderr.strip():
        return ""
    if "collation version mismatch" in stderr:
        return (
            "database collation version mismatch warning "
            "(known project-level warning; read-only audit still completed)"
        )
    return " ".join(line.strip() for line in stderr.splitlines() if line.strip())


def as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    return int(value)


def as_float(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    return float(value)


def is_positive(row: dict[str, Any], *keys: str) -> bool:
    return any(as_int(row.get(key)) > 0 for key in keys)


def finding(severity: str, title: str, detail: str) -> dict[str, str]:
    return {"severity": severity, "title": title, "detail": detail}


def build_findings(results: dict[str, list[dict[str, Any]]]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []

    for row in results.get("object_health", []):
        count = as_int(row.get("count"))
        if count:
            findings.append(finding("FAIL", row["check_name"], json_dumps(row.get("detail", []))))

    for row in results.get("orphans", []):
        if as_int(row.get("count")):
            findings.append(finding("FAIL", row["check_name"], f"{row['count']} orphan child row(s)."))

    queue = first_row(results, "queue_health")
    if queue:
        if as_int(queue.get("dead_rows")):
            findings.append(finding("FAIL", "dead enrichment rows", f"{queue['dead_rows']} queue rows are dead-lettered."))
        if as_int(queue.get("stale_claimed_rows")):
            findings.append(finding("FAIL", "stale claimed enrichment rows", f"{queue['stale_claimed_rows']} claimed rows are older than 2 hours."))

    if results.get("duplicate_external_ids"):
        findings.append(finding("FAIL", "duplicate active external IDs", f"{len(results['duplicate_external_ids'])} duplicate groups sampled."))

    quality_warn_keys = [
        "bad_source_url",
        "bad_canonical_url",
        "missing_title",
        "missing_raw_data",
        "missing_transaction_type",
        "missing_property_type",
        "invalid_state",
        "impossible_lat",
        "impossible_lng",
    ]
    for row in results.get("listing_quality_by_source", []):
        active = as_int(row.get("rows_checked"))
        if active and is_positive(row, *quality_warn_keys):
            bits = [f"{key}={row[key]}" for key in quality_warn_keys if as_int(row.get(key))]
            findings.append(finding("WARN", f"{row['source_key']} listing quality flags", ", ".join(bits)))

    for row in results.get("listing_quality_by_source", []):
        missing_state = as_int(row.get("missing_state"))
        missing_coords = as_int(row.get("missing_coords"))
        rows = max(as_int(row.get("rows_checked")), 1)
        if missing_state / rows >= 0.05:
            findings.append(finding("INFO", f"{row['source_key']} state fill gap", f"{missing_state}/{rows} active rows have no state."))
        if missing_coords / rows >= 0.15:
            findings.append(finding("INFO", f"{row['source_key']} coordinate fill gap", f"{missing_coords}/{rows} active rows have no coordinates."))

    numeric_total = sum(as_int(row.get("flagged_rows")) for row in results.get("numeric_anomaly_summary", []))
    if numeric_total:
        findings.append(finding("WARN", "numeric anomaly rows", f"{numeric_total} rows exceed audit thresholds."))

    if results.get("bad_child_urls"):
        findings.append(finding("WARN", "bad child URLs", f"{len(results['bad_child_urls'])} sampled child URLs are not absolute http(s)."))

    duplicate_source_url_groups = sum(
        as_int(row.get("duplicate_url_groups"))
        for row in results.get("duplicate_source_url_summary", [])
    )
    if duplicate_source_url_groups:
        duplicate_rows = sum(
            as_int(row.get("rows_in_duplicate_groups"))
            for row in results.get("duplicate_source_url_summary", [])
        )
        findings.append(
            finding(
                "WARN",
                "duplicate active source URLs",
                f"{duplicate_source_url_groups} duplicate URL groups covering {duplicate_rows} rows.",
            )
        )

    for row in results.get("source_index_health", []):
        if as_int(row.get("stale_last_enumerated")):
            findings.append(finding("WARN", f"{row['source_key']} stale source index rows", f"{row['stale_last_enumerated']} rows older than 8 days."))

    for row in results.get("recent_jobs", []):
        if row.get("status") != "completed" or as_int(row.get("errors_count")):
            findings.append(finding("WARN", f"{row['brokerage_slug']} latest scrape job", f"status={row.get('status')} errors={row.get('errors_count')}"))

    for row in results.get("search_smoke", []):
        if as_int(row.get("result_rows")) == 0:
            findings.append(finding("WARN", f"search smoke returned no rows: {row['smoke_name']}", "Search function may still be valid, but this canned probe found no result."))

    if not findings:
        findings.append(finding("OK", "no blocking findings", "No failures or warning-level audit findings were detected."))
    return findings


def first_row(results: dict[str, list[dict[str, Any]]], key: str) -> dict[str, Any]:
    rows = results.get(key, [])
    return rows[0] if rows else {}


def md_escape(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, sort_keys=True)
    else:
        text = str(value)
    text = text.replace("\n", " ").replace("\r", " ")
    return text.replace("|", "\\|")


def markdown_table(rows: list[dict[str, Any]], max_rows: int = 60) -> str:
    if not rows:
        return "_No rows._\n"
    shown = rows[:max_rows]
    headers = list(shown[0].keys())
    out = ["| " + " | ".join(headers) + " |"]
    out.append("|" + "|".join("---" for _ in headers) + "|")
    for row in shown:
        out.append("| " + " | ".join(md_escape(row.get(h)) for h in headers) + " |")
    if len(rows) > max_rows:
        out.append(f"\n_Showing {max_rows} of {len(rows)} rows._")
    return "\n".join(out) + "\n"


def render_markdown(report: dict[str, Any]) -> str:
    results = report["results"]
    parts = [
        "# CRE Listings Data Quality Audit",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Credentials source file: `{report['env_file']}`. Credential values were not printed.",
        "Scope: read-only checks over `credeals` listing, child, monitor, queue, and agent-facing view/function surfaces.",
        "",
        "## Executive Summary",
        "",
        markdown_table(report["findings"], max_rows=200),
        "",
    ]
    warnings = report.get("psql_warnings") or []
    if warnings:
        parts.extend(["## psql Warnings", ""])
        parts.extend(f"- {warning}" for warning in warnings)
        parts.append("")

    key_counts = first_row(results, "queue_health")
    source_rows = results.get("source_summary", [])
    active_total = sum(as_int(row.get("active_rows")) for row in source_rows)
    parts.extend(
        [
            "## Snapshot",
            "",
            f"- Active listing rows by source summary: `{active_total}`",
            f"- Pending enrichment rows: `{key_counts.get('pending_rows', '')}`",
            f"- Dead enrichment rows: `{key_counts.get('dead_rows', '')}`",
            f"- Stale claimed enrichment rows: `{key_counts.get('stale_claimed_rows', '')}`",
            "",
        ]
    )

    for key, query in QUERIES.items():
        parts.extend([f"## {query.title}", ""])
        if query.note:
            parts.extend([query.note, ""])
        parts.extend([markdown_table(results.get(key, [])), ""])
    return "\n".join(parts)


def run_audit(args: argparse.Namespace) -> dict[str, Any]:
    db_url, env_path = load_db_url(args.env_file)
    psql = find_psql()
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "env_file": env_path,
        "results": {},
        "psql_warnings": [],
    }
    for key, query in QUERIES.items():
        rows, warning = run_json_query(psql, db_url, query.sql, args.statement_timeout_ms)
        report["results"][key] = rows
        if warning and warning not in report["psql_warnings"]:
            report["psql_warnings"].append(warning)
    report["findings"] = build_findings(report["results"])
    return report


def write_report(report: dict[str, Any], out_path: Path, write_json: bool) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(report), encoding="utf-8")
    if write_json:
        json_path = out_path.with_suffix(".json")
        json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_path


def severity_counts(findings: list[dict[str, str]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in findings:
        out[item["severity"]] = out.get(item["severity"], 0) + 1
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=None, help="env file holding POSTGRES_URL*")
    parser.add_argument("--out", default=None, help="markdown report path; defaults to out/data_quality/<timestamp>.md")
    parser.add_argument("--json", action="store_true", help="also write a sibling JSON report")
    parser.add_argument("--statement-timeout-ms", type=int, default=120000)
    parser.add_argument(
        "--fail-on",
        choices=("never", "fail", "warn"),
        default="never",
        help="exit nonzero on FAIL findings, or on WARN/FAIL findings",
    )
    args = parser.parse_args()

    report = run_audit(args)
    out_path = Path(args.out) if args.out else default_report_path()
    write_report(report, out_path, args.json)

    counts = severity_counts(report["findings"])
    print(f"wrote data-quality audit: {out_path}")
    if args.json:
        print(f"wrote data-quality audit JSON: {out_path.with_suffix('.json')}")
    print("findings:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none")

    if args.fail_on == "fail" and counts.get("FAIL", 0):
        return 1
    if args.fail_on == "warn" and (counts.get("FAIL", 0) or counts.get("WARN", 0)):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
