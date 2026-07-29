#!/usr/bin/env python3
"""Generate a date-bounded, read-only CRE listing refresh report."""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from cre_ingest import SOURCE_TO_BROKERAGE, find_psql, load_db_url, sql_lit


SOURCE_KEY_SQL = """
CASE
  WHEN b.slug = 'cbre' AND l.external_id LIKE 'dealflow:%' THEN 'cbre-dealflow'
  WHEN b.slug = 'jll' AND l.external_id LIKE 'investor:%' THEN 'jll-investor'
  WHEN b.slug = 'colliers' AND l.external_id LIKE 'main:%' THEN 'colliers-main'
  WHEN NULLIF(l.raw_data->>'sourceKey', '') IS NOT NULL THEN l.raw_data->>'sourceKey'
  ELSE b.slug
END
"""


def validate_since(value):
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("--since must include a timezone")
    return parsed.isoformat()


def build_queries():
    registry_keys = ", ".join(sql_lit(key) for key in SOURCE_TO_BROKERAGE)
    return {
        "inventory": """
SELECT count(*) FILTER (WHERE deleted_at IS NULL) AS active_total,
       count(*) AS all_total,
       count(*) FILTER (WHERE deleted_at IS NULL AND created_at >= :since) AS created_since,
       count(*) FILTER (WHERE deleted_at IS NULL AND scraped_at >= :since) AS refreshed_since,
       round(100.0 * count(*) FILTER (WHERE deleted_at IS NULL AND scraped_at >= :since) /
             nullif(count(*) FILTER (WHERE deleted_at IS NULL), 0), 2) AS refreshed_pct,
       max(scraped_at) FILTER (WHERE deleted_at IS NULL) AS latest_scraped_at
FROM credeals.cre_listings;
""",
        "inventory_by_source": f"""
WITH active AS (
  SELECT {SOURCE_KEY_SQL} AS source_key, l.created_at, l.scraped_at,
         jsonb_path_exists(coalesce(l.raw_data, '{{}}'::jsonb), '$.**.detailError') AS detail_error
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE l.deleted_at IS NULL
)
SELECT source_key,
       count(*) AS active,
       count(*) FILTER (WHERE created_at >= :since) AS created_since,
       count(*) FILTER (WHERE scraped_at >= :since) AS refreshed_since,
       round(100.0 * count(*) FILTER (WHERE scraped_at >= :since) /
             nullif(count(*), 0), 2) AS refreshed_pct,
       count(*) FILTER (WHERE detail_error) AS detail_error_rows,
       max(scraped_at) AS latest_scraped_at
FROM active
GROUP BY source_key
ORDER BY source_key;
""",
        "registry_coverage": f"""
WITH active AS (
  SELECT {SOURCE_KEY_SQL} AS source_key
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE l.deleted_at IS NULL
)
SELECT count(*) FILTER (WHERE source_key IN ({registry_keys})) AS supported_active,
       count(*) FILTER (WHERE source_key NOT IN ({registry_keys})) AS unsupported_active,
       round(100.0 * count(*) FILTER (WHERE source_key IN ({registry_keys})) /
             nullif(count(*), 0), 2) AS supported_pct,
       round(100.0 * count(*) FILTER (WHERE source_key NOT IN ({registry_keys})) /
             nullif(count(*), 0), 2) AS unsupported_pct
FROM active;
""",
        "events_by_type": """
SELECT event_type, count(*) AS count
FROM credeals.cre_listing_events
WHERE detected_at >= :since
GROUP BY event_type ORDER BY event_type;
""",
        "source_index": """
SELECT count(*) AS total,
       count(*) FILTER (WHERE last_seen >= :since) AS seen_since,
       count(*) FILTER (WHERE last_enumerated_at >= :since) AS enumerated_since,
       count(*) FILTER (WHERE soft_deleted) AS soft_deleted
FROM credeals.cre_source_index;
""",
        "queue_by_source": """
SELECT source_key,
       count(*) FILTER (WHERE claimed_at IS NULL AND attempts < 5) AS pending,
       count(*) FILTER (WHERE claimed_at IS NOT NULL AND attempts < 5) AS claimed,
       count(*) FILTER (WHERE attempts >= 5) AS dead
FROM credeals.cre_enrichment_queue
GROUP BY source_key
ORDER BY pending DESC, source_key;
""",
        "details": """
WITH active AS (
  SELECT id FROM credeals.cre_listings WHERE deleted_at IS NULL
)
SELECT
  (SELECT count(*) FROM credeals.cre_listing_contacts c JOIN active a ON a.id = c.listing_id) AS contacts,
  (SELECT count(*) FROM credeals.cre_listing_documents d JOIN active a ON a.id = d.listing_id) AS documents,
  (SELECT count(*) FROM credeals.cre_listing_images i JOIN active a ON a.id = i.listing_id) AS images,
  (SELECT count(*) FROM credeals.cre_listing_media m JOIN active a ON a.id = m.listing_id) AS media,
  (SELECT count(*) FROM credeals.cre_listing_links k JOIN active a ON a.id = k.listing_id) AS links;
""",
        "ownership_invariants": """
SELECT
  (SELECT count(*) FROM credeals.cre_listing_om_facts) AS om_facts,
  (SELECT count(*) FROM credeals.cre_market_index) AS market_index;
""",
    }


def parse_tsv(output):
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) < 2:
        return []
    headers = lines[0].split("\t")
    return [dict(zip(headers, line.split("\t"))) for line in lines[1:]]


def run_query(psql, db_url, sql, since):
    rendered = sql.replace(":since", f"{sql_lit(since)}::timestamptz")
    proc = subprocess.run(
        [
            psql,
            db_url,
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
        ],
        input=f"BEGIN READ ONLY;\n{rendered}\nROLLBACK;\n",
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"psql exited {proc.returncode}")
    return parse_tsv(proc.stdout)


def markdown_table(rows):
    if not rows:
        return "_No rows._"
    headers = list(rows[0])
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend(
        "| " + " | ".join(str(row.get(header, "") or "") for header in headers) + " |"
        for row in rows
    )
    return "\n".join(lines)


def render_markdown(data):
    labels = {
        "inventory": "Inventory",
        "inventory_by_source": "Inventory by source",
        "registry_coverage": "Current collector registry coverage",
        "events_by_type": "Events since refresh start",
        "source_index": "Source index",
        "queue_by_source": "Enrichment queue",
        "details": "Active-listing child details",
        "ownership_invariants": "Cross-repository ownership invariants",
    }
    parts = [
        "# CRE refresh report",
        "",
        f"Refresh boundary: `{data['since']}`.",
        f"Generated: `{data['generated_at']}`.",
        "",
    ]
    for name in build_queries():
        parts.extend([f"## {labels[name]}", "", markdown_table(data[name]), ""])
    return "\n".join(parts).rstrip() + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, help="timezone-aware refresh start timestamp")
    parser.add_argument("--env-file", default=None, help="env file holding POSTGRES_URL*")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    try:
        since = validate_since(args.since)
    except ValueError as exc:
        parser.error(str(exc))
    db_url, _env_path = load_db_url(args.env_file)
    psql = find_psql()
    report = {
        "since": since,
        "generated_at": datetime.now().astimezone().isoformat(),
    }
    for name, sql in build_queries().items():
        report[name] = run_query(psql, db_url, sql, since)

    rendered = (
        json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.format == "json"
        else render_markdown(report)
    )
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
        print(f"wrote refresh report: {path}", file=sys.stderr)
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
