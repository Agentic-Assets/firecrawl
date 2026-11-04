#!/usr/bin/env bash
# Isolated PostgreSQL 17 contract test for the OM-facts migrations and the
# retire-time archive snapshot.
#
# This is intentionally opt-in: the regular collector pytest suite stays pure
# and does not require Docker. The runner starts an unexposed disposable
# container and proves, against real PostgreSQL:
#   - source migration 013 gives a fresh schema the five-column key and is
#     idempotent on rerun;
#   - the generated mark-missing archive snapshot works before the GetCREdata
#     run-lineage DDL (no extraction_run_id) and after it (the column is carried
#     into the archive, and a facts-only column does not break the snapshot);
#   - migration 015 refuses without explicit psql approval and is idempotent
#     after approval on a legacy four-column key;
#   - once extraction_run_id exists (GetCREdata schema), a rerun of 013 neither
#     recreates the retired five-column index nor rewrites the table comment, and
#     015 is an executed no-op.
# The ingest no longer writes cre_listing_om_facts, so there is no upsert to
# execute. The container is removed even when a command fails.

set -euo pipefail

COLLECTOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIGRATION="$COLLECTOR_DIR/../sql/013_cre_listing_om_facts.sql"
ALIGN_MIGRATION="$COLLECTOR_DIR/../sql/015_align_om_facts_conflict_key.sql"
PG_IMAGE="${PG_IMAGE:-postgres:17-alpine}"
CONTAINER="firecrawl-om-facts-contract-$$"

WORK_DIR="$(mktemp -d)"

cleanup() {
  docker rm --force "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$WORK_DIR"
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

psql_contract() {
  docker exec --interactive "$CONTAINER" psql -X -v ON_ERROR_STOP=1 -U postgres -d contract "$@"
}

# Generates the SQL for one archive-snapshot stage. The mark-missing archive block
# is taken verbatim from the production generator (build_sql), never re-typed.
cat > "$WORK_DIR/archive_stage.py" <<'PY'
from datetime import datetime, timezone
from pathlib import Path
import sys

collector_dir, stage = Path(sys.argv[1]), sys.argv[2]
sys.path.insert(0, str(collector_dir))
import cre_ingest as ci

generated = ci.build_sql(
    [], [], datetime(2026, 7, 11, tzinfo=timezone.utc).isoformat(), {"svn"},
    history_guard=True,
)
marker = "-- (013) Archive final OM facts of retired listings."
start = generated.index(marker)
end = generated.index("END $$;", start) + len("END $$;")
archive_block = generated[start:end]

LISTING = "00000000-0000-0000-0000-0000000000a1"
RUN = "00000000-0000-0000-0000-0000000000b1"


def seed(with_run):
    cols = ", extraction_run_id" if with_run else ""
    vals = f", '{RUN}'" if with_run else ""
    return f"""
TRUNCATE credeals.cre_listing_om_facts, credeals.cre_listing_om_facts_archive;
DELETE FROM _retired;
INSERT INTO credeals.cre_listings (id) VALUES ('{LISTING}') ON CONFLICT DO NOTHING;
INSERT INTO credeals.cre_listing_om_facts
  (listing_id, fact_group, fact_key, fact_value_num, source_doc_url, parser_version{cols})
VALUES ('{LISTING}', 'scalar', 'noi', 1, 'https://example.test/om.pdf', 'om-contract/1'{vals});
INSERT INTO _retired (id) VALUES ('{LISTING}');
"""


def expect_archive(expected_run):
    check = (
        "(SELECT count(*) FROM credeals.cre_listing_om_facts_archive) = 1"
        " AND (SELECT fact_value_num FROM credeals.cre_listing_om_facts_archive) = 1"
    )
    if expected_run is not None:
        check += (
            " AND (SELECT extraction_run_id FROM credeals.cre_listing_om_facts_archive)"
            f" = '{expected_run}'::uuid"
        )
    return check


def assert_state(label, condition):
    return f"""
DO $$ BEGIN
  IF NOT ({condition}) THEN
    RAISE EXCEPTION 'archive snapshot contract failed: {label}';
  END IF;
END $$;
"""


print("CREATE TEMP TABLE _retired (id uuid NOT NULL);")
if stage == "legacy":
    print(seed(False))
    print(archive_block)
    print(assert_state("pre-lineage snapshot", expect_archive(None)))
    print(assert_state(
        "pre-lineage archive has no extraction_run_id column",
        "NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = "
        "'credeals.cre_listing_om_facts_archive'::regclass "
        "AND attname = 'extraction_run_id' AND NOT attisdropped)",
    ))
    print("SELECT 'OM facts archive pre-lineage contract passed' AS result;")
elif stage == "lineage":
    print("""
ALTER TABLE credeals.cre_listing_om_facts ADD COLUMN extraction_run_id uuid;
ALTER TABLE credeals.cre_listing_om_facts_archive ADD COLUMN extraction_run_id uuid;
""")
    print(seed(True))
    print(archive_block)
    print(assert_state("lineage carried into the archive", expect_archive(RUN)))
    # Column on the facts table only: the snapshot must still work, legacy columns.
    print("ALTER TABLE credeals.cre_listing_om_facts_archive DROP COLUMN extraction_run_id;")
    print(seed(True))
    print(archive_block)
    print(assert_state("facts-only lineage column", expect_archive(None)))
    print("ALTER TABLE credeals.cre_listing_om_facts_archive ADD COLUMN extraction_run_id uuid;")
    print("SELECT 'OM facts archive lineage contract passed' AS result;")
else:
    raise SystemExit("unknown stage " + stage)
PY

# Fresh schema: 013 once, then rerun it (it is included by 000_run_all.sql).
{
  cat <<'SQL'
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA credeals;
CREATE TABLE credeals.cre_listings (id uuid PRIMARY KEY);
SQL
  cat "$MIGRATION"
} | psql_contract
psql_contract <<'SQL'
DO $$ BEGIN
  IF obj_description('credeals.cre_listing_om_facts'::regclass, 'pg_class') NOT LIKE 'OM/PDF-parsed facts%' THEN
    RAISE EXCEPTION '013 did not set the table comment on a fresh schema';
  END IF;
END $$;
SQL
psql_contract < "$MIGRATION"
psql_contract <<'SQL'
DO $$ BEGIN
  IF obj_description('credeals.cre_listing_om_facts'::regclass, 'pg_class') NOT LIKE 'OM/PDF-parsed facts%'
     OR (SELECT count(*) FROM pg_indexes
          WHERE schemaname = 'credeals' AND indexname = 'cre_listing_om_facts_uq') <> 1 THEN
    RAISE EXCEPTION '013 rerun on a fresh schema was not idempotent';
  END IF;
END $$;
SELECT 'OM facts 013 fresh rerun contract passed' AS result;
SQL

python3 "$WORK_DIR/archive_stage.py" "$COLLECTOR_DIR" legacy | psql_contract

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
    RAISE EXCEPTION 'expected fresh schema to use the canonical five-column key: %', index_definition;
  END IF;
END $$;
SELECT 'OM facts fresh-schema contract passed' AS result;
SQL

docker exec --interactive "$CONTAINER" psql -X -v ON_ERROR_STOP=1 -U postgres -d contract <<'SQL'
TRUNCATE credeals.cre_listing_om_facts;
DROP INDEX credeals.cre_listing_om_facts_uq;
CREATE UNIQUE INDEX cre_listing_om_facts_uq
  ON credeals.cre_listing_om_facts
  (listing_id, fact_group, fact_key, source_doc_url) NULLS NOT DISTINCT;
SQL

if docker exec --interactive "$CONTAINER" psql -X -v ON_ERROR_STOP=1 -U postgres -d contract \
  < "$ALIGN_MIGRATION"; then
  echo "legacy alignment unexpectedly ran without approval" >&2
  exit 1
fi

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
  IF index_definition IS NULL OR index_definition LIKE
      '%(listing_id, fact_group, fact_key, source_doc_url, parser_version) NULLS NOT DISTINCT%' THEN
    RAISE EXCEPTION 'unapproved migration 015 changed the legacy index: %', index_definition;
  END IF;
END $$;
SELECT 'OM facts unapproved-alignment refusal contract passed' AS result;
SQL

docker exec --interactive "$CONTAINER" psql -X -v ON_ERROR_STOP=1 -U postgres -d contract \
  -v CRE_APPROVE_OM_FACTS_KEY_ALIGNMENT=1 < "$ALIGN_MIGRATION"
docker exec --interactive "$CONTAINER" psql -X -v ON_ERROR_STOP=1 -U postgres -d contract \
  -v CRE_APPROVE_OM_FACTS_KEY_ALIGNMENT=1 \
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

# Legacy installs are restored to the canonical five-column key, then the
# GetCREdata run-lineage schema is applied (extraction_run_id on both tables).
python3 "$WORK_DIR/archive_stage.py" "$COLLECTOR_DIR" lineage | psql_contract

# GetCREdata replaced the five-column unique index (different name) and owns the
# table comment. Rerunning 013 and 015 must change neither, and must not
# recreate the retired index.
psql_contract <<'SQL'
DROP INDEX credeals.cre_listing_om_facts_uq;
CREATE UNIQUE INDEX cre_listing_om_facts_run_uq
  ON credeals.cre_listing_om_facts
  (listing_id, fact_group, fact_key, source_doc_url, parser_version, extraction_run_id)
  NULLS NOT DISTINCT;
COMMENT ON TABLE credeals.cre_listing_om_facts IS 'GetCREdata-owned comment';
SQL

assert_lineage_schema_untouched() {
  psql_contract <<'SQL'
DO $$ BEGIN
  IF to_regclass('credeals.cre_listing_om_facts_uq') IS NOT NULL THEN
    RAISE EXCEPTION 'retired five-column index was recreated';
  END IF;
  IF to_regclass('credeals.cre_listing_om_facts_run_uq') IS NULL THEN
    RAISE EXCEPTION 'run-lineage index disappeared';
  END IF;
  IF obj_description('credeals.cre_listing_om_facts'::regclass, 'pg_class')
       IS DISTINCT FROM 'GetCREdata-owned comment' THEN
    RAISE EXCEPTION 'table comment was rewritten';
  END IF;
END $$;
SQL
}

psql_contract < "$MIGRATION"
assert_lineage_schema_untouched
psql_contract -v CRE_APPROVE_OM_FACTS_KEY_ALIGNMENT=1 < "$ALIGN_MIGRATION"
assert_lineage_schema_untouched
psql_contract -v CRE_APPROVE_OM_FACTS_KEY_ALIGNMENT=1 < "$ALIGN_MIGRATION"
assert_lineage_schema_untouched
psql_contract -c "SELECT 'OM facts run-lineage no-op contract passed' AS result"
