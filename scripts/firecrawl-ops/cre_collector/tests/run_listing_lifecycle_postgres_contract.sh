#!/usr/bin/env bash
# Disposable local-PostgreSQL contract for migration 016 and retirement locking.

set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COLLECTOR_DIR="$(cd "$TEST_DIR/.." && pwd)"
MIGRATION="$COLLECTOR_DIR/../sql/016_cre_listing_lifecycle.sql"
DB_NAME="firecrawl_lifecycle_contract_$$"

cleanup() {
  dropdb --if-exists "$DB_NAME" >/dev/null 2>&1 || true
}

for command in psql createdb dropdb; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "$command is required for the lifecycle PostgreSQL contract test." >&2
    exit 78
  fi
done
if ! pg_isready >/dev/null 2>&1; then
  echo "A disposable-capable local PostgreSQL server is required." >&2
  exit 78
fi
server_version_num="$(psql -X -Atqc 'SHOW server_version_num' postgres)"
if (( server_version_num < 150000 )); then
  echo "PostgreSQL 15+ is required (NULLS NOT DISTINCT); found $server_version_num." >&2
  exit 78
fi

trap cleanup EXIT
createdb "$DB_NAME"

psql -X -v ON_ERROR_STOP=1 -d "$DB_NAME" <<'SQL'
CREATE EXTENSION pgcrypto;
CREATE SCHEMA credeals;
CREATE TABLE credeals.cre_brokerages (
  id uuid PRIMARY KEY,
  slug text NOT NULL UNIQUE
);
CREATE TABLE credeals.cre_listings (
  id uuid PRIMARY KEY,
  brokerage_id uuid NOT NULL REFERENCES credeals.cre_brokerages(id),
  external_id text NOT NULL,
  source_url text,
  status text,
  deleted_at timestamptz,
  updated_at timestamptz,
  sale_price_usd numeric,
  sale_price_per_sf numeric,
  lease_rate_min numeric,
  lease_rate_max numeric,
  cap_rate numeric,
  UNIQUE (brokerage_id, external_id)
);
CREATE TABLE credeals.cre_scrape_jobs (
  id uuid PRIMARY KEY,
  brokerage_id uuid NOT NULL REFERENCES credeals.cre_brokerages(id),
  status text NOT NULL,
  started_at timestamptz NOT NULL,
  completed_at timestamptz,
  notes text
);
CREATE TABLE credeals.cre_source_index (
  id bigserial PRIMARY KEY,
  brokerage_id uuid NOT NULL REFERENCES credeals.cre_brokerages(id),
  external_id text NOT NULL,
  source_key text,
  url text,
  soft_deleted boolean NOT NULL DEFAULT false,
  observed_status text,
  first_seen timestamptz,
  last_seen timestamptz,
  last_enumerated_at timestamptz,
  UNIQUE (brokerage_id, external_id)
);
CREATE TABLE credeals.cre_listing_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  listing_id uuid NOT NULL REFERENCES credeals.cre_listings(id),
  brokerage_id uuid NOT NULL REFERENCES credeals.cre_brokerages(id),
  scrape_job_id uuid REFERENCES credeals.cre_scrape_jobs(id) ON DELETE SET NULL,
  event_type text NOT NULL,
  field text,
  old_value text,
  new_value text,
  source_value text,
  source_url text,
  detected_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX cre_listing_events_idem_uq
  ON credeals.cre_listing_events
     (listing_id, event_type, COALESCE(field, ''), COALESCE(new_value, ''), scrape_job_id)
     NULLS NOT DISTINCT;
CREATE TABLE credeals.cre_listing_price_history (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  listing_id uuid NOT NULL REFERENCES credeals.cre_listings(id),
  observed_at timestamptz NOT NULL
);
SQL

if psql -X -v ON_ERROR_STOP=1 -d "$DB_NAME" -f "$MIGRATION" >/dev/null 2>&1; then
  echo "Migration 016 unexpectedly accepted a missing approval contract." >&2
  exit 1
fi

apply_migration() {
  psql -X -v ON_ERROR_STOP=1 -d "$DB_NAME" \
    -v CRE_APPROVE_LISTING_LIFECYCLE=1 \
    -v CRE_LISTING_LIFECYCLE_OPERATOR=cayman \
    -v CRE_LISTING_LIFECYCLE_APPROVAL_REF=AGENTIC-999999 \
    -v CRE_LISTING_LIFECYCLE_CONFIRM='APPLY 016_cre_listing_lifecycle' \
    -f "$MIGRATION" >/dev/null
}
apply_migration
apply_migration

psql -X -v ON_ERROR_STOP=1 -d "$DB_NAME" <<'SQL'
INSERT INTO credeals.cre_brokerages VALUES
  ('10000000-0000-4000-8000-000000000001', 'svn');
INSERT INTO credeals.cre_listings
  (id, brokerage_id, external_id, source_url, status, updated_at)
VALUES
  ('20000000-0000-4000-8000-000000000001',
   '10000000-0000-4000-8000-000000000001', 'generation-contract',
   'https://example.test/generation-contract', 'active', now());
INSERT INTO credeals.cre_scrape_jobs
  (id, brokerage_id, status, started_at, completed_at, artifact_run_key)
VALUES
  ('30000000-0000-4000-8000-000000000001',
   '10000000-0000-4000-8000-000000000001', 'completed', now(), now(),
   'contract-generation');

INSERT INTO credeals.cre_listing_events
  (listing_id, brokerage_id, scrape_job_id, event_type, field, new_value,
   presence_generation)
VALUES
  ('20000000-0000-4000-8000-000000000001',
   '10000000-0000-4000-8000-000000000001',
   '30000000-0000-4000-8000-000000000001', 'disappeared', 'status', 'inactive', 1),
  ('20000000-0000-4000-8000-000000000001',
   '10000000-0000-4000-8000-000000000001',
   '30000000-0000-4000-8000-000000000001', 'disappeared', 'status', 'inactive', 3)
ON CONFLICT DO NOTHING;
INSERT INTO credeals.cre_listing_events
  (listing_id, brokerage_id, scrape_job_id, event_type, field, new_value,
   presence_generation)
VALUES
  ('20000000-0000-4000-8000-000000000001',
   '10000000-0000-4000-8000-000000000001',
   '30000000-0000-4000-8000-000000000001', 'disappeared', 'status', 'inactive', 3)
ON CONFLICT DO NOTHING;
INSERT INTO credeals.cre_listing_events
  (listing_id, brokerage_id, scrape_job_id, event_type, field, new_value)
VALUES
  ('20000000-0000-4000-8000-000000000001',
   '10000000-0000-4000-8000-000000000001',
   '30000000-0000-4000-8000-000000000001', 'status_change', 'status', 'sold'),
  ('20000000-0000-4000-8000-000000000001',
   '10000000-0000-4000-8000-000000000001',
   '30000000-0000-4000-8000-000000000001', 'status_change', 'status', 'sold')
ON CONFLICT DO NOTHING;
DELETE FROM credeals.cre_scrape_jobs
WHERE id = '30000000-0000-4000-8000-000000000001';

DO $$
BEGIN
  IF (SELECT count(*) FROM credeals.cre_listing_events
      WHERE event_type = 'disappeared') <> 2 THEN
    RAISE EXCEPTION 'later lifecycle generation was suppressed';
  END IF;
  IF EXISTS (SELECT 1 FROM credeals.cre_listing_events
             WHERE event_type = 'disappeared' AND scrape_job_id IS NOT NULL) THEN
    RAISE EXCEPTION 'ON DELETE SET NULL lifecycle compatibility regressed';
  END IF;
  IF (SELECT count(*) FROM credeals.cre_listing_events
      WHERE event_type = 'status_change') <> 1 THEN
    RAISE EXCEPTION 'non-lifecycle idempotence regressed';
  END IF;
END $$;

INSERT INTO credeals.cre_listings
  (id, brokerage_id, external_id, source_url, status, updated_at)
VALUES
  ('20000000-0000-4000-8000-000000000002',
   '10000000-0000-4000-8000-000000000001', 'race-contract',
   'https://example.test/race-contract', 'active', '2026-08-31T12:00:00Z');
INSERT INTO credeals.cre_source_index
  (brokerage_id, external_id, source_key, url, soft_deleted,
   observation_present, presence_generation, presence_changed_at,
   first_seen, last_seen, last_enumerated_at)
VALUES
  ('10000000-0000-4000-8000-000000000001', 'race-contract', 'svn',
   'https://example.test/race-contract', false, true, 0,
   '2026-08-31T12:00:00Z', '2026-08-31T12:00:00Z',
   '2026-08-31T12:00:00Z', '2026-08-31T12:00:00Z');
SQL

(
  psql -X -v ON_ERROR_STOP=1 -d "$DB_NAME" <<'SQL'
BEGIN;
UPDATE credeals.cre_source_index
SET last_enumerated_at = '2026-08-31T12:10:00Z',
    last_seen = '2026-08-31T12:10:00Z',
    soft_deleted = false,
    observation_present = true
WHERE external_id = 'race-contract';
SELECT pg_sleep(1);
COMMIT;
SQL
) >/dev/null &
present_pid=$!
sleep 0.2

python3 - "$COLLECTOR_DIR" <<'PY' | psql -X -v ON_ERROR_STOP=1 -d "$DB_NAME" >/dev/null
from pathlib import Path
import sys

collector = Path(sys.argv[1])
sys.path.insert(0, str(collector))
import cre_ingest

sql = cre_ingest.build_sql(
    [], [{"slug": "svn", "discovered": 0, "saved": 0, "errors": 0,
          "notes": None}],
    "2026-08-31T11:55:00Z", {"svn"}, history_guard=False,
    finished_at="2026-08-31T12:05:00Z",
)
start = sql.index("CREATE TEMP TABLE _retired_candidates")
event_start = sql.index("INSERT INTO credeals.cre_listing_events", start)
end = sql.index("ON CONFLICT DO NOTHING;", event_start) + len("ON CONFLICT DO NOTHING;")
block = sql[start:end]
print("""
BEGIN;
CREATE TEMP TABLE _up (id uuid PRIMARY KEY) ON COMMIT DROP;
CREATE TEMP TABLE _jobmeta
  (slug text, job_id uuid, finished_at timestamptz) ON COMMIT DROP;
INSERT INTO _jobmeta VALUES
  ('svn', '30000000-0000-4000-8000-000000000002', '2026-08-31T12:05:00Z');
CREATE TEMP TABLE _prior_vals (
  id uuid, brokerage_id uuid, external_id text,
  sale_price_usd numeric, sale_price_per_sf numeric,
  lease_rate_min numeric, lease_rate_max numeric, status text,
  cap_rate numeric, deleted_at timestamptz
) ON COMMIT DROP;
""")
print(block)
print("COMMIT;")
PY
wait "$present_pid"

psql -X -v ON_ERROR_STOP=1 -d "$DB_NAME" <<'SQL'
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM credeals.cre_listings
             WHERE external_id = 'race-contract' AND deleted_at IS NOT NULL) THEN
    RAISE EXCEPTION 'concurrent newer present observation lost to retirement';
  END IF;
  IF EXISTS (SELECT 1 FROM credeals.cre_source_index
             WHERE external_id = 'race-contract'
               AND (soft_deleted OR NOT observation_present OR
                    last_enumerated_at <> '2026-08-31T12:10:00Z')) THEN
    RAISE EXCEPTION 'source-index lifecycle became internally inconsistent';
  END IF;
  IF EXISTS (SELECT 1 FROM credeals.cre_listing_events e
             JOIN credeals.cre_listings l ON l.id = e.listing_id
             WHERE l.external_id = 'race-contract' AND e.event_type = 'disappeared') THEN
    RAISE EXCEPTION 'race emitted a false disappeared event';
  END IF;
END $$;
SELECT 'listing lifecycle PostgreSQL contract passed' AS result;
SQL
