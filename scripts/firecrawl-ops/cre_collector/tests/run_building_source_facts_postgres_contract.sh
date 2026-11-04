#!/bin/bash
# Synthetic, network-isolated database only. Never accepts a live database URL.
set -euo pipefail
test_dir="$(cd "$(dirname "$0")" && pwd)"
migration="$test_dir/../../sql/018_cre_building_source_facts.sql"
include_pattern='^[[:space:]]*\\(i|ir|include|include_relative)[[:space:]]+.*018_cre_building_source_facts[.]sql'
if grep -Eq "$include_pattern" "$test_dir/../../sql/000_run_all.sql"; then echo 'FAIL generic runner includes 018'; exit 1; fi
result_dir="$(mktemp -d "${TMPDIR:-/tmp}/cre-building-facts.XXXXXX")"
# Bound every Docker call. Name and independently generated ownership token are
# retained even when start is uncertain; never delete an unverified container.
# If start times out, inspection is point-in-time and cannot rule out a later
# daemon-side creation. Report uncertain cleanup for independent reconciliation.
bounded() {
  python3 -c 'import subprocess,sys
try:
 p=subprocess.run(sys.argv[1:],timeout=25)
 sys.exit(p.returncode)
except subprocess.TimeoutExpired:
 print("fixture command timed out",file=sys.stderr)
 sys.exit(124)' "$@"
}
container_name="cre-building-facts-$(uuidgen | tr '[:upper:]' '[:lower:]')"
owner_token="$(uuidgen | tr '[:upper:]' '[:lower:]')"
container_id=""
cleanup() {
  local identity inspected_id inspected_name inspected_token target
  target="$container_name"
  if [[ "$container_id" =~ ^[0-9a-f]{64}$ ]]; then target="$container_id"; fi
  if ! identity="$(bounded docker inspect --format '{{.Id}} {{.Name}} {{index .Config.Labels "com.agenticassets.fixture-token"}}' "$target" 2>/dev/null)"; then
    echo "Fixture container cleanup not confirmed: $container_name" >&2
    return 1
  fi
  read -r inspected_id inspected_name inspected_token <<< "$identity"
  if [[ ! "$inspected_id" =~ ^[0-9a-f]{64}$ || "$inspected_name" != "/$container_name" || "$inspected_token" != "$owner_token" ]]; then
    echo "Fixture container ownership mismatch; left untouched: $container_name" >&2
    return 1
  fi
  if [[ "$container_id" =~ ^[0-9a-f]{64}$ && "$inspected_id" != "$container_id" ]]; then
    echo "Fixture container identity mismatch; left untouched: $container_name" >&2
    return 1
  fi
  if ! bounded docker rm -f "$inspected_id" >/dev/null 2>&1; then
    echo "Fixture container cleanup not confirmed: $container_name" >&2
    return 1
  fi
}
trap cleanup EXIT
container_id="$(bounded docker run -d --name "$container_name" --label "com.agenticassets.fixture-token=$owner_token" --network none -e POSTGRES_HOST_AUTH_METHOD=trust postgres:17-alpine)"
if [[ ! "$container_id" =~ ^[0-9a-f]{64}$ ]]; then
  echo 'Fixture container returned invalid ID; refusing to execute' >&2
  exit 1
fi
identity="$(bounded docker inspect --format '{{.Id}} {{.Name}} {{index .Config.Labels "com.agenticassets.fixture-token"}}' "$container_id")"
read -r inspected_id inspected_name inspected_token <<< "$identity"
if [[ "$inspected_id" != "$container_id" || "$inspected_name" != "/$container_name" || "$inspected_token" != "$owner_token" ]]; then
  echo 'Fixture container identity/ownership mismatch; refusing to execute' >&2
  exit 1
fi
for i in {1..50}; do
  if bounded docker exec "$container_id" psql -X -U postgres -d postgres -Atc 'select 1' >/dev/null 2>&1; then
    sleep 1
    if bounded docker exec "$container_id" psql -X -U postgres -d postgres -Atc 'select 1' >/dev/null 2>&1; then break; fi
  fi
  sleep 0.2
done
pg_raw=(bounded docker exec -i "$container_id" psql -X -U postgres -d postgres -v ON_ERROR_STOP=1)
pg=("${pg_raw[@]}" -v CRE_APPROVE_BUILDING_SOURCE_FACTS=1 -v CRE_BUILDING_SOURCE_FACTS_OPERATOR=cayman -v CRE_BUILDING_SOURCE_FACTS_APPROVAL_REF=AGENTIC-1 -v 'CRE_BUILDING_SOURCE_FACTS_CONFIRM=APPLY 018_cre_building_source_facts')
"${pg_raw[@]}" -c "CREATE SCHEMA credeals; CREATE TABLE credeals.cre_listings(id integer PRIMARY KEY, raw_data jsonb); INSERT INTO credeals.cre_listings VALUES(1,'{\"source\":\"synthetic\"}');"
refusal() {
  local label="$1"; shift
  if "${pg_raw[@]}" "$@" < "$migration" > "$result_dir/refusal-$label.log" 2>&1; then echo "FAIL gate $label"; exit 1; fi
  grep -q 'migration 018 requires the approval-gated psql contract' "$result_dir/refusal-$label.log" || { cat "$result_dir/refusal-$label.log"; exit 1; }
  [[ "$("${pg_raw[@]}" -Atc "SELECT count(*) FROM information_schema.columns WHERE table_schema='credeals' AND table_name='cre_listings' AND column_name IN ('elevator_count','typical_floor_size_text')")" == 0 ]] || { echo 'FAIL gate performed DDL'; exit 1; }
}
refusal all_missing
refusal missing_approval -v CRE_BUILDING_SOURCE_FACTS_OPERATOR=cayman -v CRE_BUILDING_SOURCE_FACTS_APPROVAL_REF=AGENTIC-1 -v 'CRE_BUILDING_SOURCE_FACTS_CONFIRM=APPLY 018_cre_building_source_facts'
refusal missing_operator -v CRE_APPROVE_BUILDING_SOURCE_FACTS=1 -v CRE_BUILDING_SOURCE_FACTS_APPROVAL_REF=AGENTIC-1 -v 'CRE_BUILDING_SOURCE_FACTS_CONFIRM=APPLY 018_cre_building_source_facts'
refusal missing_issue -v CRE_APPROVE_BUILDING_SOURCE_FACTS=1 -v CRE_BUILDING_SOURCE_FACTS_OPERATOR=cayman -v 'CRE_BUILDING_SOURCE_FACTS_CONFIRM=APPLY 018_cre_building_source_facts'
refusal missing_confirmation -v CRE_APPROVE_BUILDING_SOURCE_FACTS=1 -v CRE_BUILDING_SOURCE_FACTS_OPERATOR=cayman -v CRE_BUILDING_SOURCE_FACTS_APPROVAL_REF=AGENTIC-1
refusal invalid_operator -v CRE_APPROVE_BUILDING_SOURCE_FACTS=1 -v CRE_BUILDING_SOURCE_FACTS_OPERATOR=unknown -v CRE_BUILDING_SOURCE_FACTS_APPROVAL_REF=AGENTIC-1 -v 'CRE_BUILDING_SOURCE_FACTS_CONFIRM=APPLY 018_cre_building_source_facts'
refusal invalid_issue -v CRE_APPROVE_BUILDING_SOURCE_FACTS=1 -v CRE_BUILDING_SOURCE_FACTS_OPERATOR=cayman -v CRE_BUILDING_SOURCE_FACTS_APPROVAL_REF=unknown -v 'CRE_BUILDING_SOURCE_FACTS_CONFIRM=APPLY 018_cre_building_source_facts'
echo 'PASS approval gates before DDL'
"${pg[@]}" < "$migration"
# Re-running is safe refusal, not an idempotent success: the existing column
# could have an unknown owner or unvalidated historical negative values.
if "${pg[@]}" < "$migration" > "$result_dir/rerun.log" 2>&1; then echo 'FAIL existing column accepted'; exit 1; fi
grep -q 'preexisting column requires separate review: elevator_count' "$result_dir/rerun.log"
"${pg[@]}" <<'SQL'
DO $$ BEGIN
 IF (SELECT count(*) FROM information_schema.columns WHERE table_schema='credeals' AND table_name='cre_listings' AND ((column_name='elevator_count' AND data_type='integer') OR (column_name='typical_floor_size_text' AND data_type='text')) AND is_nullable='YES') <> 2 THEN RAISE EXCEPTION 'column contract mismatch'; END IF;
 IF (SELECT elevator_count IS NOT NULL OR typical_floor_size_text IS NOT NULL OR raw_data <> '{"source":"synthetic"}'::jsonb FROM credeals.cre_listings WHERE id=1) THEN RAISE EXCEPTION 'backfill or source mutation'; END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='credeals.cre_listings'::regclass AND conname='cre_listings_elevator_count_nonnegative' AND NOT convalidated AND pg_get_constraintdef(oid) LIKE '%elevator_count >= 0%') THEN RAISE EXCEPTION 'check missing'; END IF;
END $$;
INSERT INTO credeals.cre_listings(id,elevator_count,typical_floor_size_text) VALUES (2,0,'24,990'),(3,1,'source literal, unit unstated');
DO $$ BEGIN
 BEGIN INSERT INTO credeals.cre_listings(id,elevator_count) VALUES(4,-1); RAISE EXCEPTION 'negative accepted'; EXCEPTION WHEN check_violation THEN NULL; END;
END $$;
ALTER TABLE credeals.cre_listings DROP COLUMN elevator_count, DROP COLUMN typical_floor_size_text;
DO $$ BEGIN IF (SELECT raw_data <> '{"source":"synthetic"}'::jsonb FROM credeals.cre_listings WHERE id=1) THEN RAISE EXCEPTION 'rollback altered source evidence'; END IF; END $$;
SQL
# Existing compatible columns and comments are owned by someone else. Refuse
# without changing either the prior comment or any existing negative value.
"${pg[@]}" -c "ALTER TABLE credeals.cre_listings ADD COLUMN elevator_count integer; COMMENT ON COLUMN credeals.cre_listings.elevator_count IS 'existing owner elevator comment'; UPDATE credeals.cre_listings SET elevator_count=-1 WHERE id=1;"
if "${pg[@]}" < "$migration" > "$result_dir/elevator-existing.log" 2>&1; then echo 'FAIL existing elevator accepted'; exit 1; fi
grep -q 'preexisting column requires separate review: elevator_count' "$result_dir/elevator-existing.log"
"${pg[@]}" <<'SQL'
DO $$ BEGIN
 IF (SELECT elevator_count FROM credeals.cre_listings WHERE id=1) <> -1 THEN RAISE EXCEPTION 'existing negative changed'; END IF;
 IF col_description('credeals.cre_listings'::regclass,(SELECT attnum FROM pg_attribute WHERE attrelid='credeals.cre_listings'::regclass AND attname='elevator_count')) <> 'existing owner elevator comment' THEN RAISE EXCEPTION 'existing elevator comment changed'; END IF;
 IF EXISTS (SELECT FROM information_schema.columns WHERE table_schema='credeals' AND table_name='cre_listings' AND column_name='typical_floor_size_text') THEN RAISE EXCEPTION 'partial column added'; END IF;
END $$;
ALTER TABLE credeals.cre_listings DROP COLUMN elevator_count;
ALTER TABLE credeals.cre_listings ADD COLUMN typical_floor_size_text text;
COMMENT ON COLUMN credeals.cre_listings.typical_floor_size_text IS 'existing owner floor comment';
SQL
if "${pg[@]}" < "$migration" > "$result_dir/floor-existing.log" 2>&1; then echo 'FAIL existing floor accepted'; exit 1; fi
grep -q 'preexisting column requires separate review: typical_floor_size_text' "$result_dir/floor-existing.log"
"${pg[@]}" <<'SQL'
DO $$ BEGIN
 IF col_description('credeals.cre_listings'::regclass,(SELECT attnum FROM pg_attribute WHERE attrelid='credeals.cre_listings'::regclass AND attname='typical_floor_size_text')) <> 'existing owner floor comment' THEN RAISE EXCEPTION 'existing floor comment changed'; END IF;
 IF EXISTS (SELECT FROM information_schema.columns WHERE table_schema='credeals' AND table_name='cre_listings' AND column_name='elevator_count') THEN RAISE EXCEPTION 'partial column added'; END IF;
END $$;
ALTER TABLE credeals.cre_listings DROP COLUMN typical_floor_size_text;
SQL
# An exclusive DDL lock held elsewhere must be refused within lock_timeout.
"${pg_raw[@]}" > "$result_dir/hold.log" 2>&1 <<'SQL' &
BEGIN; LOCK TABLE credeals.cre_listings IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(6); COMMIT;
SQL
holder=$!; locked=0
for i in {1..40}; do
 if [[ "$("${pg_raw[@]}" -Atc "SELECT EXISTS(SELECT FROM pg_locks WHERE relation='credeals.cre_listings'::regclass AND mode='AccessExclusiveLock' AND granted)")" == t ]]; then locked=1; break; fi
 sleep 0.1
done
[[ "$locked" == 1 ]] || { echo 'FAIL lock was not acquired'; exit 1; }
if "${pg[@]}" < "$migration" > "$result_dir/lock-collision.log" 2>&1; then echo 'FAIL competing lock accepted'; exit 1; fi
grep -q 'lock timeout' "$result_dir/lock-collision.log"
wait "$holder"
[[ "$("${pg_raw[@]}" -Atc "SELECT count(*) FROM information_schema.columns WHERE table_schema='credeals' AND table_name='cre_listings' AND column_name IN ('elevator_count','typical_floor_size_text')")" == 0 ]] || { echo 'FAIL lock timeout caused partial DDL'; exit 1; }
echo 'PASS clean apply, safe rerun refusal, zero, missing, no backfill, rollback, existing metadata and value preservation, and lock refusal'
printf 'Evidence directory: %s\n' "$result_dir"
