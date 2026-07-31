#!/usr/bin/env python3
"""
Read-only Supabase validation for the CRE collector.

Uses the same credential-loading and psql discovery helpers as cre_ingest.py.
Credential values are never printed. All queries run inside READ ONLY
transactions and only inspect the `credeals` listing tables/views.
"""

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cre_ingest import (
    INVENTORY_ONLY_SOURCE_DEFINITIONS,
    assert_expected_database_target,
    find_psql,
    load_db_url,
    psql_connection_args,
    psql_connection_env,
    source_key_sql,
)
from cre_source_policy import load_source_policy


SOURCE_KEY_SQL = source_key_sql()

# Raw payloads may be stored flat, as a merged sale/lease pass, or beneath the
# inventory-only preservation wrapper.  Keep this ordered tuple shared by the
# read-only SQL report and its pure fixture coverage.  The first payload with
# persisted freshness evidence wins; no database column is a provenance
# fallback.
FRESHNESS_PAYLOAD_PATHS = (
    ("latestInventoryObservation",),
    ("latestInventoryObservation", "primary"),
    ("latestInventoryObservation", "secondary_pass"),
    (),
    ("primary",),
    ("secondary_pass",),
)
FRESHNESS_PROVENANCE_FIELDS = (
    "inventory_observed_at",
    "detail_observed_at",
    "generation_id",
    "detail_scope",
    "cache_disposition",
)


def _raw_payload_at(raw_data, path):
    payload = raw_data
    for key in path:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload if isinstance(payload, dict) else None


def _nonempty_string(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def persisted_freshness_provenance(raw_data):
    """Return only freshness values explicitly persisted in raw JSON data.

    ``cre_listings.scraped_at`` is deliberately absent from this interface:
    ingestion time is not evidence that the current collector observed a
    provider detail page.  Callers receive ``None`` when a persisted payload
    does not prove a field.
    """
    empty = dict.fromkeys(FRESHNESS_PROVENANCE_FIELDS)
    if not isinstance(raw_data, dict):
        return empty
    for path in FRESHNESS_PAYLOAD_PATHS:
        payload = _raw_payload_at(raw_data, path)
        if payload is None:
            continue
        provenance = payload.get("freshnessProvenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        result = {
            "inventory_observed_at": _nonempty_string(
                payload.get("inventoryObservedAt")
            ),
            "detail_observed_at": _nonempty_string(payload.get("detailObservedAt")),
            "generation_id": _nonempty_string(provenance.get("generationId")),
            "detail_scope": _nonempty_string(provenance.get("detailScope")),
            "cache_disposition": _nonempty_string(
                provenance.get("cacheDisposition")
            ),
        }
        if any(result.values()):
            return result
    return empty


def _sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _raw_json_object(alias, path):
    return f"{alias}.raw_data #> '{{{','.join(path)}}}'"


def _freshness_payload_sql(alias):
    branches = []
    for path in FRESHNESS_PAYLOAD_PATHS:
        payload = _raw_json_object(alias, path)
        branches.append(
            "WHEN jsonb_typeof({payload}) = 'object' AND (\n"
            "          {payload} ? 'inventoryObservedAt' OR\n"
            "          {payload} ? 'detailObservedAt' OR\n"
            "          jsonb_typeof({payload}->'freshnessProvenance') = 'object'\n"
            "        ) THEN {payload}".format(payload=payload)
        )
    return "CASE\n      " + "\n      ".join(branches) + "\n      ELSE NULL::jsonb\n    END"


FRESHNESS_PAYLOAD_SQL = _freshness_payload_sql("l")


SOURCE_POLICY = load_source_policy()
SOURCE_POLICY_SQL = ",\n".join(
    "    ({source_key}, {evidence_class}, {detail_claim})".format(
        source_key=_sql_literal(source_key),
        evidence_class=_sql_literal(entry["evidence_class"]),
        detail_claim=_sql_literal(entry["detail_claim"]),
    )
    for source_key, entry in SOURCE_POLICY.items()
)


INVENTORY_ONLY_DEFINITIONS_SQL = ",\n".join(
    "    ({source_key}, {slug}, {external_id_like}, {watermark_external_id})".format(
        source_key=_sql_literal(source_key),
        slug=_sql_literal(definition["slug"]),
        external_id_like=_sql_literal(definition["external_id_like"]),
        watermark_external_id=_sql_literal(
            definition["watermark_external_id"]
        ),
    )
    for source_key, definition in INVENTORY_ONLY_SOURCE_DEFINITIONS.items()
)


QUERIES = {
    "totals": """
SELECT 'cre_listings_active' AS metric, count(*)::text AS value
FROM credeals.cre_listings WHERE deleted_at IS NULL
UNION ALL
SELECT 'v_cre_listings_full', count(*)::text FROM credeals.v_cre_listings_full
UNION ALL
SELECT 'v_cre_active_for_sale', count(*)::text FROM credeals.v_cre_active_for_sale
UNION ALL
SELECT 'v_cre_active_for_lease', count(*)::text FROM credeals.v_cre_active_for_lease
UNION ALL
SELECT 'v_cre_market_summary', count(*)::text FROM credeals.v_cre_market_summary
ORDER BY metric;
""",
    "source_counts": f"""
WITH base AS (
  SELECT
    {SOURCE_KEY_SQL} AS source_key,
    b.slug AS brokerage_slug,
    l.transaction_type,
    l.scraped_at,
    NULLIF(l.raw_data->>'inventoryObservedAt', '')::timestamptz
      AS inventory_observed_at,
    jsonb_path_exists(l.raw_data, '$.**.detailUnavailable') AS detail_unavailable,
    l.deleted_at
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
),
active AS (
  SELECT * FROM base WHERE deleted_at IS NULL
),
latest AS (
  SELECT source_key,
         max(scraped_at) AS latest_scraped_at,
         max(inventory_observed_at) AS latest_inventory_observed_at
  FROM active
  GROUP BY source_key
),
soft_deleted AS (
  SELECT source_key, count(*) AS soft_deleted
  FROM base
  WHERE deleted_at IS NOT NULL
  GROUP BY source_key
)
SELECT
  a.source_key,
  min(a.brokerage_slug) AS brokerage_slug,
  count(*)::text AS active,
  count(*) FILTER (WHERE a.transaction_type = 'sale')::text AS sale,
  count(*) FILTER (WHERE a.transaction_type = 'lease')::text AS lease,
  count(*) FILTER (WHERE a.transaction_type = 'sale_or_lease')::text AS sale_or_lease,
  count(*) FILTER (WHERE a.scraped_at = latest.latest_scraped_at)::text AS latest_batch_active,
  to_char(latest.latest_scraped_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS"Z"') AS latest_scraped_at,
  count(*) FILTER (
    WHERE a.inventory_observed_at = latest.latest_inventory_observed_at
  )::text AS latest_inventory_batch_active,
  to_char(
    latest.latest_inventory_observed_at AT TIME ZONE 'UTC',
    'YYYY-MM-DD HH24:MI:SS"Z"'
  ) AS latest_inventory_observed_at,
  count(*) FILTER (WHERE a.detail_unavailable)::text AS detail_unavailable,
  coalesce(max(soft_deleted.soft_deleted), 0)::text AS soft_deleted
FROM active a
JOIN latest ON latest.source_key = a.source_key
LEFT JOIN soft_deleted ON soft_deleted.source_key = a.source_key
GROUP BY a.source_key, latest.latest_scraped_at, latest.latest_inventory_observed_at
ORDER BY a.source_key;
""",
    "freshness_generations": f"""
WITH source_policy (source_key, evidence_class, detail_claim) AS (
  VALUES
{SOURCE_POLICY_SQL}
),
-- Materialize the expensive JSON provenance projection once per active row.
-- Without this fence, PostgreSQL inlines it into every aggregate and the final
-- presentation sort, multiplying JSON extraction work enough to hit the
-- production statement timeout.
raw AS MATERIALIZED (
  SELECT
    {SOURCE_KEY_SQL} AS source_key,
    NULLIF(provenance."generationId", '') AS generation_id,
    NULLIF(provenance."detailScope", '') AS detail_scope,
    NULLIF(provenance."cacheDisposition", '') AS cache_disposition,
    NULLIF(observed."inventoryObservedAt", '')::timestamptz
      AS inventory_observed_at,
    NULLIF(observed."detailObservedAt", '')::timestamptz
      AS detail_observed_at
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  CROSS JOIN LATERAL (
    SELECT {FRESHNESS_PAYLOAD_SQL} AS payload
    OFFSET 0
  ) AS persisted
  CROSS JOIN LATERAL jsonb_to_record(
    coalesce(persisted.payload, '{{}}'::jsonb)
  ) AS observed(
    "inventoryObservedAt" text,
    "detailObservedAt" text,
    "freshnessProvenance" jsonb
  )
  CROSS JOIN LATERAL jsonb_to_record(
    coalesce(observed."freshnessProvenance", '{{}}'::jsonb)
  ) AS provenance(
    "generationId" text,
    "detailScope" text,
    "cacheDisposition" text
  )
  WHERE l.deleted_at IS NULL
),
active AS (
  SELECT * FROM raw
  WHERE source_key IS NOT NULL
    AND generation_id IS NOT NULL
)
SELECT
  active.source_key,
  source_policy.evidence_class,
  source_policy.detail_claim,
  active.generation_id,
  coalesce(
    jsonb_agg(DISTINCT active.detail_scope)
      FILTER (WHERE active.detail_scope IS NOT NULL),
    '[]'::jsonb
  )::text AS detail_scopes,
  coalesce(
    jsonb_agg(DISTINCT active.cache_disposition)
      FILTER (WHERE active.cache_disposition IS NOT NULL),
    '[]'::jsonb
  )::text AS cache_dispositions,
  count(*)::text AS active,
  count(*) FILTER (WHERE active.inventory_observed_at IS NOT NULL)::text
    AS persisted_inventory_observed,
  count(*) FILTER (WHERE active.detail_observed_at IS NOT NULL)::text
    AS persisted_detail_observed,
  count(*) FILTER (
    WHERE source_policy.detail_claim IN (
      'current_strict_detail', 'current_property_detail'
    ) AND active.detail_observed_at IS NULL
  )::text AS missing_persisted_detail_proof,
  to_char(
    min(active.inventory_observed_at) AT TIME ZONE 'UTC',
    'YYYY-MM-DD HH24:MI:SS"Z"'
  ) AS earliest_inventory_observed_at,
  to_char(
    max(active.inventory_observed_at) AT TIME ZONE 'UTC',
    'YYYY-MM-DD HH24:MI:SS"Z"'
  ) AS latest_inventory_observed_at,
  to_char(
    min(active.detail_observed_at) AT TIME ZONE 'UTC',
    'YYYY-MM-DD HH24:MI:SS"Z"'
  ) AS earliest_detail_observed_at,
  to_char(
    max(active.detail_observed_at) AT TIME ZONE 'UTC',
    'YYYY-MM-DD HH24:MI:SS"Z"'
  ) AS latest_detail_observed_at
FROM active
LEFT JOIN source_policy USING (source_key)
GROUP BY
  active.source_key,
  source_policy.evidence_class,
  source_policy.detail_claim,
  active.generation_id;
""",
    "inventory_only_index": f"""
WITH definitions (
  source_key, brokerage_slug, external_id_like, watermark_external_id
) AS (
  VALUES
{INVENTORY_ONLY_DEFINITIONS_SQL}
),
scoped AS (
  SELECT
    definitions.source_key,
    si.soft_deleted,
    si.last_enumerated_at
  FROM definitions
  JOIN credeals.cre_brokerages b
    ON b.slug = definitions.brokerage_slug
  JOIN credeals.cre_source_index si
    ON si.brokerage_id = b.id
   AND si.source_key = definitions.source_key
   AND si.external_id LIKE definitions.external_id_like
),
watermark AS (
  SELECT
    definitions.source_key,
    max(si.last_enumerated_at) AS scope_watermark_at
  FROM definitions
  JOIN credeals.cre_brokerages b
    ON b.slug = definitions.brokerage_slug
  LEFT JOIN credeals.cre_source_index si
    ON si.brokerage_id = b.id
   AND si.source_key = definitions.source_key
   AND si.external_id = definitions.watermark_external_id
  GROUP BY definitions.source_key
),
latest AS (
  SELECT source_key, max(last_enumerated_at) AS latest_enumerated_at
  FROM scoped
  GROUP BY source_key
),
summary AS (
  SELECT
    scoped.source_key,
    count(*) FILTER (WHERE NOT scoped.soft_deleted) AS active,
    count(*) FILTER (WHERE scoped.soft_deleted) AS soft_deleted,
    count(*) FILTER (
      WHERE NOT scoped.soft_deleted
        AND scoped.last_enumerated_at = latest.latest_enumerated_at
    ) AS latest_batch_active,
    max(latest.latest_enumerated_at) AS latest_enumerated_at
  FROM scoped
  JOIN latest ON latest.source_key = scoped.source_key
  GROUP BY scoped.source_key
)
SELECT
  definitions.source_key,
  coalesce(summary.active, 0)::text AS active,
  coalesce(summary.soft_deleted, 0)::text AS soft_deleted,
  coalesce(summary.latest_batch_active, 0)::text AS latest_batch_active,
  to_char(
    summary.latest_enumerated_at AT TIME ZONE 'UTC',
    'YYYY-MM-DD HH24:MI:SS"Z"'
  ) AS latest_enumerated_at,
  to_char(
    watermark.scope_watermark_at AT TIME ZONE 'UTC',
    'YYYY-MM-DD HH24:MI:SS"Z"'
  ) AS scope_watermark_at
FROM definitions
LEFT JOIN summary ON summary.source_key = definitions.source_key
LEFT JOIN watermark ON watermark.source_key = definitions.source_key
ORDER BY definitions.source_key;
""",
    "quality_by_source": f"""
WITH active AS (
  SELECT
    {SOURCE_KEY_SQL} AS source_key,
    l.*
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE l.deleted_at IS NULL
)
SELECT
  source_key,
  count(*) FILTER (WHERE source_url IS NULL OR source_url !~* '^https?://')::text AS bad_source_url,
  count(*) FILTER (
    WHERE canonical_url IS NULL OR btrim(canonical_url) = ''
  )::text AS missing_canonical_url,
  count(*) FILTER (
    WHERE canonical_url IS NOT NULL
      AND btrim(canonical_url) <> ''
      AND canonical_url !~* '^https?://'
  )::text AS bad_canonical_url,
  count(*) FILTER (WHERE title IS NULL OR btrim(title) = '')::text AS missing_title,
  count(*) FILTER (WHERE raw_data IS NULL OR raw_data = '{{}}'::jsonb)::text AS missing_raw_data,
  count(*) FILTER (WHERE state IS NULL OR btrim(state::text) = '')::text AS missing_state,
  count(*) FILTER (WHERE state IS NOT NULL AND state::text !~ '^[A-Z]{{2}}$')::text AS invalid_state,
  count(*) FILTER (WHERE lat IS NULL OR lng IS NULL)::text AS missing_coords,
  count(*) FILTER (WHERE lat IS NOT NULL AND (lat < -90 OR lat > 90))::text AS impossible_lat,
  count(*) FILTER (WHERE lng IS NOT NULL AND (lng < -180 OR lng > 180))::text AS impossible_lng,
  count(*) FILTER (WHERE sale_price_usd IS NOT NULL AND (sale_price_usd <= 0 OR sale_price_usd > 20000000000))::text AS sale_price_flags,
  count(*) FILTER (WHERE sale_price_per_sf IS NOT NULL AND (sale_price_per_sf <= 0 OR sale_price_per_sf > 10000))::text AS sale_psf_flags,
  count(*) FILTER (WHERE lease_rate_min IS NOT NULL AND (lease_rate_min <= 0 OR lease_rate_min > 500))::text AS lease_rate_min_flags,
  count(*) FILTER (WHERE lease_rate_max IS NOT NULL AND (lease_rate_max <= 0 OR lease_rate_max > 500))::text AS lease_rate_max_flags,
  count(*) FILTER (WHERE cap_rate IS NOT NULL AND (cap_rate <= 0 OR cap_rate >= 0.5))::text AS cap_rate_flags
FROM active
GROUP BY source_key
ORDER BY source_key;
""",
    "duplicates": f"""
WITH active AS (
  SELECT
    {SOURCE_KEY_SQL} AS source_key,
    l.*
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE l.deleted_at IS NULL
),
dup_external AS (
  SELECT count(*) AS groups, coalesce(sum(row_count), 0) AS rows
  FROM (
    SELECT brokerage_id, external_id, count(*) AS row_count
    FROM active
    WHERE external_id IS NOT NULL
    GROUP BY brokerage_id, external_id
    HAVING count(*) > 1
  ) s
),
dup_source AS (
  SELECT source_key, count(*) AS groups, coalesce(sum(row_count), 0) AS rows
  FROM (
    SELECT source_key, source_url, count(*) AS row_count
    FROM active
    GROUP BY source_key, source_url
    HAVING count(*) > 1
  ) s
  GROUP BY source_key
)
SELECT 'duplicate_external_id_groups' AS check_name, groups::text, rows::text, NULL::text AS source_key
FROM dup_external
UNION ALL
SELECT 'duplicate_source_url_groups', groups::text, rows::text, source_key
FROM dup_source
ORDER BY check_name, source_key NULLS FIRST;
""",
    "child_counts": f"""
WITH active AS (
  SELECT
    l.id,
    {SOURCE_KEY_SQL} AS source_key
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE l.deleted_at IS NULL
),
child_rows AS (
  SELECT a.source_key, 'contacts' AS child_type, count(c.id) AS count
  FROM active a LEFT JOIN credeals.cre_listing_contacts c ON c.listing_id = a.id
  GROUP BY a.source_key
  UNION ALL
  SELECT a.source_key, 'documents', count(d.id)
  FROM active a LEFT JOIN credeals.cre_listing_documents d ON d.listing_id = a.id
  GROUP BY a.source_key
  UNION ALL
  SELECT a.source_key, 'images', count(i.id)
  FROM active a LEFT JOIN credeals.cre_listing_images i ON i.listing_id = a.id
  GROUP BY a.source_key
  UNION ALL
  SELECT a.source_key, 'media', count(m.id)
  FROM active a LEFT JOIN credeals.cre_listing_media m ON m.listing_id = a.id
  GROUP BY a.source_key
  UNION ALL
  SELECT a.source_key, 'links', count(k.id)
  FROM active a LEFT JOIN credeals.cre_listing_links k ON k.listing_id = a.id
  GROUP BY a.source_key
)
SELECT source_key, child_type, count::text
FROM child_rows
ORDER BY source_key, child_type;
""",
    "bad_child_urls": """
SELECT 'document_bad_url' AS check_name, count(*)::text AS count
FROM credeals.cre_listing_documents d
JOIN credeals.cre_listings l ON l.id = d.listing_id
WHERE l.deleted_at IS NULL AND d.url !~* '^https?://'
UNION ALL
SELECT 'image_bad_url', count(*)::text
FROM credeals.cre_listing_images i
JOIN credeals.cre_listings l ON l.id = i.listing_id
WHERE l.deleted_at IS NULL AND i.url !~* '^https?://'
UNION ALL
SELECT 'media_bad_url', count(*)::text
FROM credeals.cre_listing_media m
JOIN credeals.cre_listings l ON l.id = m.listing_id
WHERE l.deleted_at IS NULL
  AND (m.url !~* '^https?://' OR (m.embed_url IS NOT NULL AND m.embed_url !~* '^https?://'))
UNION ALL
SELECT 'link_bad_url', count(*)::text
FROM credeals.cre_listing_links k
JOIN credeals.cre_listings l ON l.id = k.listing_id
WHERE l.deleted_at IS NULL AND k.url !~* '^https?://'
UNION ALL
SELECT 'contact_bad_profile_url', count(*)::text
FROM credeals.cre_listing_contacts c
JOIN credeals.cre_listings l ON l.id = c.listing_id
WHERE l.deleted_at IS NULL AND c.profile_url IS NOT NULL AND c.profile_url !~* '^https?://'
UNION ALL
SELECT 'contact_bad_avatar_url', count(*)::text
FROM credeals.cre_listing_contacts c
JOIN credeals.cre_listings l ON l.id = c.listing_id
WHERE l.deleted_at IS NULL AND c.avatar_url IS NOT NULL AND c.avatar_url !~* '^https?://'
UNION ALL
SELECT 'contact_bad_vcard_url', count(*)::text
FROM credeals.cre_listing_contacts c
JOIN credeals.cre_listings l ON l.id = c.listing_id
WHERE l.deleted_at IS NULL AND c.vcard_url IS NOT NULL AND c.vcard_url !~* '^https?://'
ORDER BY check_name;
""",
    "primary_child_conflicts": """
SELECT child_type, count(*)::text AS listings
FROM (
  SELECT 'contacts' AS child_type, listing_id
  FROM credeals.cre_listing_contacts
  WHERE is_primary
  GROUP BY listing_id
  HAVING count(*) > 1
  UNION ALL
  SELECT 'images' AS child_type, listing_id
  FROM credeals.cre_listing_images
  WHERE is_primary
  GROUP BY listing_id
  HAVING count(*) > 1
) conflicts
GROUP BY child_type
ORDER BY child_type;
""",
    "orphans": """
SELECT 'contacts' AS child_type, count(*)::text AS orphan_rows
FROM credeals.cre_listing_contacts c
LEFT JOIN credeals.cre_listings l ON l.id = c.listing_id
WHERE l.id IS NULL
UNION ALL
SELECT 'documents', count(*)::text
FROM credeals.cre_listing_documents d
LEFT JOIN credeals.cre_listings l ON l.id = d.listing_id
WHERE l.id IS NULL
UNION ALL
SELECT 'images', count(*)::text
FROM credeals.cre_listing_images i
LEFT JOIN credeals.cre_listings l ON l.id = i.listing_id
WHERE l.id IS NULL
UNION ALL
SELECT 'media', count(*)::text
FROM credeals.cre_listing_media m
LEFT JOIN credeals.cre_listings l ON l.id = m.listing_id
WHERE l.id IS NULL
UNION ALL
SELECT 'links', count(*)::text
FROM credeals.cre_listing_links k
LEFT JOIN credeals.cre_listings l ON l.id = k.listing_id
WHERE l.id IS NULL
ORDER BY child_type;
""",
    "search_smoke": """
SELECT 'industrial_tx_sale' AS smoke, count(*)::text AS rows
FROM credeals.search_cre_listings('industrial', NULL, 'TX', NULL, 'sale')
UNION ALL
SELECT 'office_sale', count(*)::text
FROM credeals.search_cre_listings('office', NULL, NULL, NULL, 'sale')
UNION ALL
SELECT 'lee', count(*)::text
FROM credeals.search_cre_listings('Lee', NULL, NULL, NULL, NULL)
UNION ALL
SELECT 'national_avenue', count(*)::text
FROM credeals.search_cre_listings('National Avenue', NULL, NULL, NULL, NULL)
ORDER BY smoke;
""",
}


def parse_tsv(output):
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return []
    headers = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        values = line.split("\t")
        rows.append(dict(zip(headers, values)))
    return rows


def _run_psql_script(psql, db_url, script):
    """Run a closed SQL script without a parent-to-psql stdin pipe."""
    script_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix="cre-validate-",
            suffix=".sql",
            delete=False,
        ) as script_file:
            script_file.write(script)
            script_path = Path(script_file.name)
        return subprocess.run(
            [
                psql,
                *psql_connection_args(db_url),
                "-q",
                "-v",
                "ON_ERROR_STOP=1",
                "-P",
                "pager=off",
                "-P",
                "footer=off",
                "-F",
                "\t",
                "-A",
                "-f",
                str(script_path),
            ],
            env=psql_connection_env(db_url),
            text=True,
            capture_output=True,
        )
    finally:
        if script_path is not None:
            script_path.unlink(missing_ok=True)


def run_query(psql, db_url, sql):
    wrapped = (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
        f"{sql}\n"
        "ROLLBACK;\n"
    )
    proc = _run_psql_script(psql, db_url, wrapped)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"psql exited {proc.returncode}")
    return parse_tsv(proc.stdout), proc.stderr.strip()


QUERY_MARKER_PREFIX = "__CRE_VALIDATION_QUERY__:"


def parse_query_batch(output, query_names):
    """Split one psql session's marked result sets without mixing snapshots."""
    expected = list(query_names)
    chunks = {name: [] for name in expected}
    current = None
    for line in output.splitlines():
        if line.startswith(QUERY_MARKER_PREFIX):
            name = line.removeprefix(QUERY_MARKER_PREFIX)
            if name not in chunks:
                raise SystemExit(f"unexpected validation query marker: {name!r}")
            if current == name or chunks[name]:
                raise SystemExit(f"duplicate validation query marker: {name!r}")
            current = name
            continue
        if current is None:
            if line.strip():
                raise SystemExit("unexpected psql output before validation query marker")
            continue
        chunks[current].append(line)
    missing = [name for name in expected if not chunks[name]]
    if missing:
        raise SystemExit(
            "validation query output is missing marked result(s): "
            + ", ".join(missing)
        )
    return {
        name: parse_tsv("\n".join(chunks[name]))
        for name in expected
    }


def run_queries(psql, db_url, queries):
    """Run all validation queries in one repeatable-read, read-only snapshot.

    The complete validation program is intentionally fed from a seekable file,
    rather than ``subprocess.run(input=...)``.  The latter can deadlock when a
    large multi-query script is still being written while psql fills its stdout
    pipe with an early result set.  A file lets psql read independently while
    Python drains stdout through ``communicate``.
    """
    statements = [
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;"
    ]
    for name, sql in queries.items():
        statements.extend((f"\\echo {QUERY_MARKER_PREFIX}{name}", sql))
    statements.append("ROLLBACK;")
    proc = _run_psql_script(psql, db_url, "\n".join(statements) + "\n")
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"psql exited {proc.returncode}")
    return parse_query_batch(proc.stdout, queries), proc.stderr.strip()


def normalize_warning(stderr):
    if not stderr:
        return None
    if "collation version mismatch" in stderr:
        return (
            "database collation version mismatch warning "
            "(known project-level warning; validation queries still completed)"
        )
    return " ".join(line.strip() for line in stderr.splitlines() if line.strip())


def markdown_table(rows):
    if not rows:
        return "_No rows._\n"
    headers = list(rows[0].keys())
    out = ["| " + " | ".join(headers) + " |"]
    out.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        out.append("| " + " | ".join(str(row.get(h, "") or "") for h in headers) + " |")
    return "\n".join(out) + "\n"


def render_markdown(report):
    generated = report["generated_at"]
    env_path = report["env_file"]
    parts = [
        "# CRE Collector Read-Only Validation",
        "",
        f"Generated: `{generated}`.",
        f"Credentials source file: `{env_path}`. Values were not printed.",
        "",
    ]
    if report.get("psql_warnings"):
        parts.extend(["## psql Warnings", ""])
        for warning in report["psql_warnings"]:
            parts.append(f"- {warning}")
        parts.append("")

    labels = {
        "totals": "Totals",
        "source_counts": "Source Counts",
        "freshness_generations": "Freshness Generations",
        "inventory_only_index": "Inventory-Only Source Index",
        "quality_by_source": "Quality By Source",
        "duplicates": "Duplicate Checks",
        "child_counts": "Child Counts",
        "bad_child_urls": "Bad Child URLs",
        "primary_child_conflicts": "Primary Child Conflicts",
        "orphans": "Child Orphans",
        "search_smoke": "Search Smoke",
    }
    for key in QUERIES:
        parts.extend([f"## {labels[key]}", "", markdown_table(report["queries"][key]), ""])
    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=None, help="env file holding POSTGRES_URL*")
    parser.add_argument(
        "--expected-db-target-sha256",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--out", default=None, help="optional output path")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    args = parser.parse_args()

    db_url, env_path = load_db_url(args.env_file)
    assert_expected_database_target(db_url, args.expected_db_target_sha256)
    psql = find_psql()
    report = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "env_file": env_path,
        "queries": {},
        "psql_warnings": [],
    }
    report["queries"], stderr = run_queries(psql, db_url, QUERIES)
    warning = normalize_warning(stderr)
    if warning:
        report["psql_warnings"].append(warning)

    if args.format == "json":
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    else:
        rendered = render_markdown(report)

    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
        print(f"wrote validation report: {path}", file=sys.stderr)
    else:
        print(rendered)


if __name__ == "__main__":
    main()
