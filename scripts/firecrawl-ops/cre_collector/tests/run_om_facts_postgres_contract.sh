#!/usr/bin/env bash
# Isolated PostgreSQL 17 contract test for the generated OM-facts upsert.
#
# This is intentionally opt-in: the regular collector pytest suite stays pure
# and does not require Docker. The runner starts an unexposed disposable
# container, applies source migration 013, executes the generated production
# upsert three times, asserts same-version updates plus cross-version coexistence,
# then recreates the legacy four-column index, applies migration 015 twice, and
# proves the exact five-column idempotent upgrade. The container is removed even
# when a command fails.

set -euo pipefail

COLLECTOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIGRATION="$COLLECTOR_DIR/../sql/013_cre_listing_om_facts.sql"
ALIGN_MIGRATION="$COLLECTOR_DIR/../sql/015_align_om_facts_conflict_key.sql"
PG_IMAGE="${PG_IMAGE:-postgres:17-alpine}"
CONTAINER="firecrawl-om-facts-contract-$$"

cleanup() {
  docker rm --force "$CONTAINER" >/dev/null 2>&1 || true
}

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required for the OM-facts PostgreSQL contract test." >&2
  exit 78
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker must be running for the OM-facts PostgreSQL contract test." >&2
  exit 78
fi

if [[ ! -f "$MIGRATION" ]]; then
  echo "Missing source migration: $MIGRATION" >&2
  exit 78
fi
if [[ ! -f "$ALIGN_MIGRATION" ]]; then
  echo "Missing alignment migration: $ALIGN_MIGRATION" >&2
  exit 78
fi

trap cleanup EXIT
docker run --detach --rm --name "$CONTAINER" \
  --env POSTGRES_PASSWORD=contract \
  --env POSTGRES_DB=contract \
  "$PG_IMAGE" >/dev/null

ready=0
for _ in {1..30}; do
  if docker exec "$CONTAINER" pg_isready -U postgres -d contract >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done

if [[ "$ready" -ne 1 ]]; then
  echo "Disposable PostgreSQL did not become ready within 30 seconds." >&2
  exit 1
fi

# postgres images expose a short-lived bootstrap server before they restart as
# the final server. Wait through that handoff, then prove a real SQL connection.
sleep 3
if ! docker exec "$CONTAINER" psql -X -v ON_ERROR_STOP=1 -U postgres -d contract \
  -c 'SELECT 1' >/dev/null; then
  echo "Disposable PostgreSQL did not accept a stable SQL connection." >&2
  exit 1
fi

python3 - "$COLLECTOR_DIR" "$MIGRATION" <<'PY' | \
  docker exec --interactive "$CONTAINER" psql -X -v ON_ERROR_STOP=1 -U postgres -d contract
from datetime import datetime, timezone
from pathlib import Path
import sys

collector_dir = Path(sys.argv[1])
sys.path.insert(0, str(collector_dir))
import cre_ingest as ci

generated = ci.build_sql(
    [], [], datetime(2026, 7, 11, tzinfo=timezone.utc).isoformat(), set(), history_guard=True
)
start = generated.index("DO $$ BEGIN\n  IF to_regclass('credeals.cre_listing_om_facts')")
end = generated.index("END $$;", start) + len("END $$;")
om_upsert = generated[start:end]

print("""
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA credeals;
CREATE TABLE credeals.cre_listings (id uuid PRIMARY KEY);
""")
print(Path(sys.argv[2]).read_text())
print("""
CREATE TEMP TABLE _up (
  brokerage_id uuid NOT NULL,
  external_id text NOT NULL,
  id uuid NOT NULL
);
CREATE TEMP TABLE _src (
  brokerage_id uuid NOT NULL,
  external_id text NOT NULL,
  om_facts jsonb NOT NULL
);
CREATE TEMP TABLE _child_refresh (id uuid NOT NULL);

INSERT INTO credeals.cre_listings (id)
VALUES ('00000000-0000-0000-0000-000000000001');
INSERT INTO _up (brokerage_id, external_id, id)
VALUES ('00000000-0000-0000-0000-000000000002', 'contract-listing',
        '00000000-0000-0000-0000-000000000001');
INSERT INTO _child_refresh (id)
VALUES ('00000000-0000-0000-0000-000000000001');
INSERT INTO _src (brokerage_id, external_id, om_facts)
VALUES (
  '00000000-0000-0000-0000-000000000002',
  'contract-listing',
  '[{"factGroup":"scalar","factKey":"noi","factValueNum":"1","sourceDocUrl":"https://example.test/om.pdf","parserVersion":"om-contract/1","confidence":"0.9"}]'::jsonb
);
""")
print(om_upsert)
print("""
UPDATE _src
SET om_facts =
  '[{"factGroup":"scalar","factKey":"noi","factValueNum":"2","sourceDocUrl":"https://example.test/om.pdf","parserVersion":"om-contract/1","confidence":"0.7"}]'::jsonb;
""")
print(om_upsert)
print("""
UPDATE _src
SET om_facts =
  '[{"factGroup":"scalar","factKey":"noi","factValueNum":"3","sourceDocUrl":"https://example.test/om.pdf","parserVersion":"om-contract/2","confidence":"0.8"}]'::jsonb;
""")
print(om_upsert)
print("""
DO $$
BEGIN
  IF (SELECT count(*) FROM credeals.cre_listing_om_facts) <> 2 THEN
    RAISE EXCEPTION 'expected two parser-version rows';
  END IF;
  IF (SELECT fact_value_num FROM credeals.cre_listing_om_facts
      WHERE parser_version = 'om-contract/1') <> 2 THEN
    RAISE EXCEPTION 'same-parser reparse did not update in place';
  END IF;
  IF (SELECT fact_value_num FROM credeals.cre_listing_om_facts
      WHERE parser_version = 'om-contract/2') <> 3 THEN
    RAISE EXCEPTION 'new parser version did not coexist';
  END IF;
END $$;
SELECT 'OM facts PostgreSQL contract passed' AS result;
""")
PY

docker exec --interactive "$CONTAINER" psql -X -v ON_ERROR_STOP=1 -U postgres -d contract <<'SQL'
TRUNCATE credeals.cre_listing_om_facts;
DROP INDEX credeals.cre_listing_om_facts_uq;
CREATE UNIQUE INDEX cre_listing_om_facts_uq
  ON credeals.cre_listing_om_facts
  (listing_id, fact_group, fact_key, source_doc_url) NULLS NOT DISTINCT;
SQL

docker exec --interactive "$CONTAINER" psql -X -v ON_ERROR_STOP=1 -U postgres -d contract \
  < "$ALIGN_MIGRATION"
docker exec --interactive "$CONTAINER" psql -X -v ON_ERROR_STOP=1 -U postgres -d contract \
  < "$ALIGN_MIGRATION"

docker exec --interactive "$CONTAINER" psql -X -v ON_ERROR_STOP=1 -U postgres -d contract <<'SQL'
DO $$
DECLARE
  index_definition text;
BEGIN
  SELECT pg_get_indexdef(i.indexrelid)
    INTO index_definition
    FROM pg_index i
    JOIN pg_class idx ON idx.oid = i.indexrelid
    JOIN pg_class tbl ON tbl.oid = i.indrelid
    JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
   WHERE ns.nspname = 'credeals'
     AND tbl.relname = 'cre_listing_om_facts'
     AND idx.relname = 'cre_listing_om_facts_uq'
     AND i.indisunique
     AND i.indnullsnotdistinct;
  IF index_definition IS NULL OR index_definition NOT LIKE
      '%(listing_id, fact_group, fact_key, source_doc_url, parser_version) NULLS NOT DISTINCT%' THEN
    RAISE EXCEPTION 'migration 015 did not produce the canonical index: %', index_definition;
  END IF;
END $$;
SELECT 'OM facts legacy alignment contract passed' AS result;
SQL
