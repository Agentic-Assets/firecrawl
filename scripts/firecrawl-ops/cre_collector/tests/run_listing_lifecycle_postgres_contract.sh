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
if (( server_version_num < 140000 )); then
  echo "PostgreSQL 14+ is required; found $server_version_num." >&2
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
     (listing_id, event_type, COALESCE(field, ''), COALESCE(new_value, ''), scrape_job_id);
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
  ('10000000-0000-4000-8000-000000000001', 'svn'),
  ('10000000-0000-4000-8000-000000000002', 'race');
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
   'contract-generation-one'),
  ('30000000-0000-4000-8000-000000000002',
   '10000000-0000-4000-8000-000000000001', 'completed', now(), now(),
   'contract-generation-two'),
  ('30000000-0000-4000-8000-000000000003',
   '10000000-0000-4000-8000-000000000001', 'running', now(), NULL,
   'contract-race');

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

-- The same non-lifecycle event under a different job is distinct provenance.
-- Nullifying both job FKs must preserve both append-only rows without a unique
-- collision during the FK action.
INSERT INTO credeals.cre_listing_events
  (listing_id, brokerage_id, scrape_job_id, event_type, field, new_value)
VALUES
  ('20000000-0000-4000-8000-000000000001',
   '10000000-0000-4000-8000-000000000001',
   '30000000-0000-4000-8000-000000000002', 'status_change', 'status', 'sold');
DELETE FROM credeals.cre_scrape_jobs
WHERE id IN ('30000000-0000-4000-8000-000000000001',
             '30000000-0000-4000-8000-000000000002');

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
      WHERE event_type = 'status_change') <> 2 THEN
    RAISE EXCEPTION 'job-scoped non-lifecycle provenance or idempotence regressed';
  END IF;
  IF EXISTS (SELECT 1 FROM credeals.cre_listing_events
             WHERE event_type = 'status_change' AND scrape_job_id IS NOT NULL) THEN
    RAISE EXCEPTION 'two-job ON DELETE SET NULL compatibility regressed';
  END IF;
END $$;

INSERT INTO credeals.cre_listings
  (id, brokerage_id, external_id, source_url, status, updated_at)
VALUES
  ('20000000-0000-4000-8000-000000000002',
   '10000000-0000-4000-8000-000000000002', 'race-x',
   'https://example.test/race-x', 'active', '2026-08-31T12:00:00Z'),
  ('20000000-0000-4000-8000-000000000003',
   '10000000-0000-4000-8000-000000000002', 'race-y',
   'https://example.test/race-y', 'active', '2026-08-31T12:00:00Z');
INSERT INTO credeals.cre_source_index
  (brokerage_id, external_id, source_key, url, soft_deleted,
   observation_present, presence_generation, presence_changed_at,
   first_seen, last_seen, last_enumerated_at)
VALUES
  ('10000000-0000-4000-8000-000000000002', 'race-x', 'race',
   'https://example.test/race-x', false, true, 0,
   '2026-08-31T12:00:00Z', '2026-08-31T12:00:00Z',
   '2026-08-31T12:00:00Z', '2026-08-31T12:00:00Z'),
  ('10000000-0000-4000-8000-000000000002', 'race-y', 'race',
   'https://example.test/race-y', false, true, 0,
   '2026-08-31T12:00:00Z', '2026-08-31T12:00:00Z',
   '2026-08-31T12:00:00Z', '2026-08-31T12:00:00Z');
INSERT INTO credeals.cre_scrape_jobs
  (id, brokerage_id, status, started_at, completed_at, artifact_run_key)
VALUES
  ('30000000-0000-4000-8000-000000000004',
   '10000000-0000-4000-8000-000000000002', 'running', now(), NULL,
   'contract-opposing-race-one'),
  ('30000000-0000-4000-8000-000000000005',
   '10000000-0000-4000-8000-000000000002', 'running', now(), NULL,
   'contract-opposing-race-two');
SQL

# Run two opposing phase sets: transaction one observes X and retires Y, while
# transaction two observes Y and retires X. Without the shared transaction lock
# each can retain its present identity lock and wait forever on the other's
# retirement identity. Generated transaction, identity, source, listing, and
# retirement lock fragments are executed against both real tables.
run_opposing_lifecycle_transaction() {
  local present_external="$1"
  local present_listing_id="$2"
  local job_id="$3"
  local finished_at="$4"
  local pause_seconds="$5"
  python3 - "$COLLECTOR_DIR" "$present_external" "$present_listing_id" \
    "$job_id" "$finished_at" "$pause_seconds" <<'PY' \
    | PGAPPNAME="lifecycle-$present_external" \
      psql -X -v ON_ERROR_STOP=1 -d "$DB_NAME" >/dev/null
from pathlib import Path
import sys

collector = Path(sys.argv[1])
present_external, present_listing_id, job_id, finished_at, pause_seconds = sys.argv[2:]
sys.path.insert(0, str(collector))
import cre_ingest

sql = cre_ingest.build_sql(
    [], [{"slug": "race", "discovered": 1, "saved": 1, "errors": 0,
          "notes": None}],
    finished_at, {"race"}, history_guard=False, finished_at=finished_at,
)
transaction_start = sql.index(
    "-- Intentionally serialize all generated lifecycle mutation transactions."
)
transaction_end = sql.index("SET LOCAL standard_conforming_strings", transaction_start)
present_start = sql.index("-- Global lifecycle lock order")
present_end = sql.index("-- (H4a)", present_start)
retirement_start = sql.index("CREATE TEMP TABLE _retired_candidates")
event_start = sql.index("INSERT INTO credeals.cre_listing_events", retirement_start)
retirement_end = (
    sql.index("ON CONFLICT DO NOTHING;", event_start) + len("ON CONFLICT DO NOTHING;")
)
bid = "10000000-0000-4000-8000-000000000002"
external = cre_ingest.sql_lit(present_external)
listing_id = cre_ingest.sql_lit(present_listing_id)
job = cre_ingest.sql_lit(job_id)
finished = cre_ingest.sql_lit(finished_at)
print("""
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '20s';
""")
print(sql[transaction_start:transaction_end])
print(f"""
CREATE TEMP TABLE _src
  (brokerage_id uuid, external_id text) ON COMMIT DROP;
INSERT INTO _src VALUES
  ('{bid}', {external});
""")
print(sql[present_start:present_end])
print(f"""
CREATE TEMP TABLE _race_present_before ON COMMIT DROP AS
SELECT observation_present, presence_generation
FROM credeals.cre_source_index
WHERE brokerage_id = '{bid}' AND external_id = {external};
UPDATE credeals.cre_listings
SET deleted_at = NULL, status = 'active', updated_at = {finished}::timestamptz
WHERE brokerage_id = '{bid}' AND external_id = {external};
UPDATE credeals.cre_source_index
SET presence_generation = CASE WHEN observation_present THEN presence_generation
                               ELSE presence_generation + 1 END,
    presence_changed_at = CASE WHEN observation_present THEN presence_changed_at
                               ELSE {finished}::timestamptz END,
    last_enumerated_at = {finished}::timestamptz,
    last_seen = {finished}::timestamptz,
    soft_deleted = false,
    observation_present = true
WHERE brokerage_id = '{bid}' AND external_id = {external};
INSERT INTO credeals.cre_listing_events
  (listing_id, brokerage_id, scrape_job_id, event_type, source_value,
   presence_generation, detected_at)
SELECT {listing_id}::uuid, '{bid}'::uuid, {job}::uuid, 'reappeared',
       'contract_present', si.presence_generation, {finished}::timestamptz
FROM _race_present_before prior
JOIN credeals.cre_source_index si
  ON si.brokerage_id = '{bid}' AND si.external_id = {external}
WHERE NOT prior.observation_present
ON CONFLICT DO NOTHING;
SELECT pg_sleep({pause_seconds});
CREATE TEMP TABLE _up (id uuid PRIMARY KEY) ON COMMIT DROP;
INSERT INTO _up VALUES ({listing_id}::uuid);
CREATE TEMP TABLE _jobmeta
  (slug text, job_id uuid, finished_at timestamptz) ON COMMIT DROP;
INSERT INTO _jobmeta VALUES ('race', {job}::uuid, {finished}::timestamptz);
CREATE TEMP TABLE _prior_vals (
  id uuid, brokerage_id uuid, external_id text,
  sale_price_usd numeric, sale_price_per_sf numeric,
  lease_rate_min numeric, lease_rate_max numeric, status text,
  cap_rate numeric, deleted_at timestamptz
) ON COMMIT DROP;
""")
print(sql[retirement_start:retirement_end])
print("COMMIT;")
PY
}

run_opposing_lifecycle_transaction \
  race-x 20000000-0000-4000-8000-000000000002 \
  30000000-0000-4000-8000-000000000004 2026-08-31T12:05:00Z 1.0 &
first_pid=$!
first_holds_lock=0
for _attempt in $(seq 1 50); do
  if [[ "$(psql -X -Atqc \
    "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND application_name='lifecycle-race-x' AND wait_event='PgSleep'" \
    "$DB_NAME")" == "1" ]]; then
    first_holds_lock=1
    break
  fi
  sleep 0.05
done
if (( first_holds_lock != 1 )); then
  wait "$first_pid" || true
  echo "First opposing lifecycle transaction never reached its locked pause." >&2
  exit 1
fi
run_opposing_lifecycle_transaction \
  race-y 20000000-0000-4000-8000-000000000003 \
  30000000-0000-4000-8000-000000000005 2026-08-31T12:10:00Z 0.2 &
second_pid=$!
second_waits_on_shared_lock=0
for _attempt in $(seq 1 20); do
  if [[ "$(psql -X -Atqc \
    "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND application_name='lifecycle-race-y' AND wait_event_type='Lock' AND wait_event='advisory'" \
    "$DB_NAME")" == "1" ]]; then
    second_waits_on_shared_lock=1
    break
  fi
  sleep 0.025
done
if (( second_waits_on_shared_lock != 1 )); then
  wait "$first_pid" || true
  wait "$second_pid" || true
  echo "Opposing lifecycle transaction did not wait on the shared advisory lock." >&2
  exit 1
fi
wait "$first_pid"
wait "$second_pid"

psql -X -v ON_ERROR_STOP=1 -d "$DB_NAME" <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM credeals.cre_listings
    WHERE external_id = 'race-x' AND status = 'inactive'
      AND deleted_at IS NOT NULL
  ) THEN
    RAISE EXCEPTION 'opposing race did not retire X at the newer observation';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM credeals.cre_source_index
    WHERE external_id = 'race-x' AND soft_deleted AND NOT observation_present
      AND presence_generation = 1
      AND last_enumerated_at = '2026-08-31T12:10:00Z'
  ) THEN
    RAISE EXCEPTION 'X source-index state is stale or inconsistent';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM credeals.cre_listings
    WHERE external_id = 'race-y' AND status = 'active' AND deleted_at IS NULL
  ) OR NOT EXISTS (
    SELECT 1 FROM credeals.cre_source_index
    WHERE external_id = 'race-y' AND NOT soft_deleted AND observation_present
      AND presence_generation = 2
      AND last_enumerated_at = '2026-08-31T12:10:00Z'
  ) THEN
    RAISE EXCEPTION 'newer Y present transition was lost or internally inconsistent';
  END IF;
  IF (SELECT count(*) FROM credeals.cre_listing_events e
      JOIN credeals.cre_listings l ON l.id = e.listing_id
      WHERE l.external_id = 'race-x' AND e.event_type = 'disappeared'
        AND e.presence_generation = 1) <> 1 THEN
    RAISE EXCEPTION 'X disappearance transition is missing or duplicated';
  END IF;
  IF (SELECT count(*) FROM credeals.cre_listing_events e
      JOIN credeals.cre_listings l ON l.id = e.listing_id
      WHERE l.external_id = 'race-y'
        AND (e.event_type, e.presence_generation) IN
            (('disappeared', 1), ('reappeared', 2))) <> 2 THEN
    RAISE EXCEPTION 'Y opposing transition sequence is stale or incomplete';
  END IF;
END $$;
SELECT 'listing lifecycle PostgreSQL contract passed' AS result;
SQL
