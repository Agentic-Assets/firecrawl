#!/bin/bash
set -euo pipefail
scratch_dir="$(cd "$(dirname "$0")" && pwd)"
test_root="$(mktemp -d "${TMPDIR:-/tmp}/cre-space-count-pgtest.XXXXXX")"
migration="$scratch_dir/../../sql/017_cre_available_space_count.sql"
include_pattern='^[[:space:]]*\\(i|ir|include|include_relative)[[:space:]]+.*017_cre_available_space_count[.]sql'
for include in '\i 017_cre_available_space_count.sql' '\ir ../sql/017_cre_available_space_count.sql' '\include ./sql/017_cre_available_space_count.sql' '\include_relative ../sql/017_cre_available_space_count.sql'; do
  if ! printf '%s\n' "$include" | grep -Eq "$include_pattern"; then
    echo "FAIL inclusion detector missed $include"; exit 1
  fi
done
if grep -Eq "$include_pattern" "$scratch_dir/../../sql/000_run_all.sql"; then
  echo "FAIL generic runner includes gated migration 017"; exit 1
fi
container_id=""
cleanup() {
  if [[ -n "$container_id" ]]; then docker rm -f "$container_id" >/dev/null || true; fi
}
trap cleanup EXIT
container_id="$(docker run -d --network none -e POSTGRES_HOST_AUTH_METHOD=trust postgres:17-alpine)"
for i in {1..50}; do
  if docker exec "$container_id" psql -X -U postgres -d postgres -Atc "SELECT 1" >/dev/null 2>&1; then
    sleep 1
    if docker exec "$container_id" psql -X -U postgres -d postgres -Atc "SELECT 1" >/dev/null 2>&1; then break; fi
  fi
  sleep 0.2
done
docker exec "$container_id" pg_isready -U postgres >/dev/null
pg_raw=(docker exec -i "$container_id" psql -X -U postgres -d postgres -v ON_ERROR_STOP=1)
pg=("${pg_raw[@]}" -v CRE_APPROVE_AVAILABLE_SPACE_COUNT=1 -v CRE_AVAILABLE_SPACE_COUNT_OPERATOR=cayman -v CRE_AVAILABLE_SPACE_COUNT_APPROVAL_REF=AGENTIC-1 -v "CRE_AVAILABLE_SPACE_COUNT_CONFIRM=APPLY 017_cre_available_space_count")
"${pg[@]}" -c 'CREATE SCHEMA credeals; CREATE TABLE credeals.cre_listings (id integer PRIMARY KEY, extra_facts jsonb);'
# Fake approval data is test-only and does not authorize any actual system.
# Exercise every missing flag individually and invalid values against the
# disposable container. Refusal must happen before the first DDL.
test_refusal() {
  local label="$1"; shift
  if "${pg_raw[@]}" "$@" < "$migration" > "$test_root/refused-$label.log" 2>&1; then
    echo "FAIL approval gate accepted $label"; exit 1
  fi
  if ! grep -q 'migration 017 requires the approval-gated psql contract' "$test_root/refused-$label.log"; then
    cat "$test_root/refused-$label.log"; exit 1
  fi
  if [[ "$("${pg_raw[@]}" -Atc "SELECT count(*) FROM information_schema.columns WHERE table_schema='credeals' AND table_name='cre_listings' AND column_name='available_space_count'")" != 0 ]]; then
    echo "FAIL $label performed DDL"; exit 1
  fi
}
test_refusal all_missing
test_refusal missing_approval -v CRE_AVAILABLE_SPACE_COUNT_OPERATOR=cayman -v CRE_AVAILABLE_SPACE_COUNT_APPROVAL_REF=AGENTIC-1 -v 'CRE_AVAILABLE_SPACE_COUNT_CONFIRM=APPLY 017_cre_available_space_count'
test_refusal missing_operator -v CRE_APPROVE_AVAILABLE_SPACE_COUNT=1 -v CRE_AVAILABLE_SPACE_COUNT_APPROVAL_REF=AGENTIC-1 -v 'CRE_AVAILABLE_SPACE_COUNT_CONFIRM=APPLY 017_cre_available_space_count'
test_refusal missing_issue -v CRE_APPROVE_AVAILABLE_SPACE_COUNT=1 -v CRE_AVAILABLE_SPACE_COUNT_OPERATOR=cayman -v 'CRE_AVAILABLE_SPACE_COUNT_CONFIRM=APPLY 017_cre_available_space_count'
test_refusal missing_confirmation -v CRE_APPROVE_AVAILABLE_SPACE_COUNT=1 -v CRE_AVAILABLE_SPACE_COUNT_OPERATOR=cayman -v CRE_AVAILABLE_SPACE_COUNT_APPROVAL_REF=AGENTIC-1
test_refusal invalid_operator -v CRE_APPROVE_AVAILABLE_SPACE_COUNT=1 -v CRE_AVAILABLE_SPACE_COUNT_OPERATOR=other -v CRE_AVAILABLE_SPACE_COUNT_APPROVAL_REF=AGENTIC-1 -v 'CRE_AVAILABLE_SPACE_COUNT_CONFIRM=APPLY 017_cre_available_space_count'
test_refusal invalid_issue -v CRE_APPROVE_AVAILABLE_SPACE_COUNT=1 -v CRE_AVAILABLE_SPACE_COUNT_OPERATOR=cayman -v CRE_AVAILABLE_SPACE_COUNT_APPROVAL_REF=other -v 'CRE_AVAILABLE_SPACE_COUNT_CONFIRM=APPLY 017_cre_available_space_count'
test_refusal invalid_confirmation -v CRE_APPROVE_AVAILABLE_SPACE_COUNT=1 -v CRE_AVAILABLE_SPACE_COUNT_OPERATOR=cayman -v CRE_AVAILABLE_SPACE_COUNT_APPROVAL_REF=AGENTIC-1 -v 'CRE_AVAILABLE_SPACE_COUNT_CONFIRM=APPLY 016_cre_listing_lifecycle'
echo 'PASS approval refusals before DDL'

"${pg[@]}" < "$migration"
"${pg[@]}" < "$migration"
"${pg[@]}" <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM information_schema.columns WHERE table_schema='credeals' AND table_name='cre_listings' AND column_name='available_space_count' AND data_type='integer' AND is_nullable='YES') THEN RAISE EXCEPTION 'column missing or wrong type'; END IF;
  IF NOT EXISTS (SELECT FROM pg_constraint WHERE conrelid='credeals.cre_listings'::regclass AND conname='cre_listings_available_space_count_nonnegative' AND NOT convalidated AND pg_get_constraintdef(oid) LIKE '%available_space_count >= 0%') THEN RAISE EXCEPTION 'constraint missing, wrong, or unexpectedly validated'; END IF;
  IF (SELECT count(*) FROM pg_constraint WHERE conrelid='credeals.cre_listings'::regclass AND conname='cre_listings_available_space_count_nonnegative') <> 1 THEN RAISE EXCEPTION 'constraint not idempotent'; END IF;
  IF col_description('credeals.cre_listings'::regclass, (SELECT attnum FROM pg_attribute WHERE attrelid='credeals.cre_listings'::regclass AND attname='available_space_count')) NOT LIKE '%Not building units%' THEN RAISE EXCEPTION 'comment missing'; END IF;
END $$;
INSERT INTO credeals.cre_listings VALUES (1, '{"number_of_spaces":"0 Spaces"}', 0), (2, '{"number_of_spaces":"1 Space"}', 1), (3, NULL, NULL), (4, NULL, 2147483647);
DO $$ BEGIN
  BEGIN
    INSERT INTO credeals.cre_listings VALUES (5, NULL, -1);
    RAISE EXCEPTION 'negative unexpectedly accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  IF (SELECT extra_facts->>'number_of_spaces' FROM credeals.cre_listings WHERE id=1) <> '0 Spaces' THEN RAISE EXCEPTION 'source evidence changed'; END IF;
END $$;
ALTER TABLE credeals.cre_listings VALIDATE CONSTRAINT cre_listings_available_space_count_nonnegative;
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_constraint WHERE conrelid='credeals.cre_listings'::regclass AND conname='cre_listings_available_space_count_nonnegative' AND convalidated) THEN RAISE EXCEPTION 'constraint validation failed'; END IF;
END $$;
SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_schema='credeals' AND table_name='cre_listings' ORDER BY ordinal_position;
SELECT conname, convalidated, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='credeals.cre_listings'::regclass ORDER BY conname;
SELECT id, extra_facts, available_space_count FROM credeals.cre_listings ORDER BY id;
-- Simulate a separately approved destructive rollback after dependent consumers
-- are stopped. This proves basic mechanics on a disposable minimal schema only.
ALTER TABLE credeals.cre_listings DROP COLUMN available_space_count;
DO $$ BEGIN
  IF EXISTS (SELECT FROM information_schema.columns WHERE table_schema='credeals' AND table_name='cre_listings' AND column_name='available_space_count') THEN RAISE EXCEPTION 'rollback failed'; END IF;
  IF (SELECT extra_facts->>'number_of_spaces' FROM credeals.cre_listings WHERE id=1) <> '0 Spaces' THEN RAISE EXCEPTION 'rollback harmed source evidence'; END IF;
END $$;
SELECT 'PASS clean install, rerun, catalog, constraint, validation, rollback, evidence retention' AS result;
SQL
# Wrong existing type must abort without adding a constraint.
"${pg[@]}" -c 'ALTER TABLE credeals.cre_listings ADD COLUMN available_space_count text;'
if "${pg[@]}" < "$migration" > "$test_root/type-collision.log" 2>&1; then
  echo 'FAIL incompatible type accepted'; exit 1
fi
if ! grep -q 'incompatible existing available_space_count column' "$test_root/type-collision.log"; then
  cat "$test_root/type-collision.log"; exit 1
fi
"${pg[@]}" -c 'ALTER TABLE credeals.cre_listings DROP COLUMN available_space_count; ALTER TABLE credeals.cre_listings ADD COLUMN available_space_count integer DEFAULT 0;'
if "${pg[@]}" < "$migration" > "$test_root/default-collision.log" 2>&1; then
  echo 'FAIL incompatible default accepted'; exit 1
fi
if ! grep -q 'incompatible existing available_space_count column' "$test_root/default-collision.log"; then
  cat "$test_root/default-collision.log"; exit 1
fi
"${pg[@]}" -c 'ALTER TABLE credeals.cre_listings DROP COLUMN available_space_count; ALTER TABLE credeals.cre_listings ADD COLUMN available_space_count integer; ALTER TABLE credeals.cre_listings ADD CONSTRAINT cre_listings_available_space_count_nonnegative CHECK (available_space_count > 0) NOT VALID;'
if "${pg[@]}" < "$migration" > "$test_root/constraint-collision.log" 2>&1; then
  echo 'FAIL incompatible constraint accepted'; exit 1
fi
if ! grep -q 'incompatible existing available_space_count constraint' "$test_root/constraint-collision.log"; then
  cat "$test_root/constraint-collision.log"; exit 1
fi
"${pg[@]}" <<'SQL'
DO $$ BEGIN
 IF pg_get_expr((SELECT conbin FROM pg_constraint WHERE conrelid='credeals.cre_listings'::regclass AND conname='cre_listings_available_space_count_nonnegative'), 'credeals.cre_listings'::regclass) <> '(available_space_count > 0)' THEN RAISE EXCEPTION 'constraint collision changed'; END IF;
END $$;
SELECT 'PASS fail-closed wrong type, default, and constraint collisions' AS result;
SQL
# An additional differently named check must not silently reject valid zero.
"${pg[@]}" -c 'ALTER TABLE credeals.cre_listings DROP CONSTRAINT cre_listings_available_space_count_nonnegative; ALTER TABLE credeals.cre_listings ADD CONSTRAINT stricter_count CHECK (available_space_count > 0) NOT VALID;'
if "${pg[@]}" < "$migration" > "$test_root/other-constraint.log" 2>&1; then
  echo 'FAIL additional constraint accepted'; exit 1
fi
if ! grep -q 'additional constraint references available_space_count' "$test_root/other-constraint.log"; then
  cat "$test_root/other-constraint.log"; exit 1
fi
"${pg[@]}" <<'SQL'
DO $$ BEGIN
 IF EXISTS (SELECT FROM pg_constraint WHERE conrelid='credeals.cre_listings'::regclass AND conname='cre_listings_available_space_count_nonnegative') THEN RAISE EXCEPTION 'failed migration partially applied intended constraint'; END IF;
 IF NOT EXISTS (SELECT FROM pg_constraint WHERE conrelid='credeals.cre_listings'::regclass AND conname='stricter_count') THEN RAISE EXCEPTION 'failed migration changed pre-existing constraint'; END IF;
END $$;
ALTER TABLE credeals.cre_listings DROP CONSTRAINT stricter_count;
SQL
# Standalone uniqueness can restrict otherwise valid counts without a table
# constraint. Check direct, expression, and predicate references independently.
test_unique_index_refusal() {
  local label="$1"; shift
  "${pg[@]}" -c "$1"
  if "${pg[@]}" < "$migration" > "$test_root/unique-$label.log" 2>&1; then
    echo "FAIL unique $label index accepted"; exit 1
  fi
  if ! grep -q 'unique index references available_space_count' "$test_root/unique-$label.log"; then
    cat "$test_root/unique-$label.log"; exit 1
  fi
  if [[ "$("${pg[@]}" -Atc "SELECT count(*) FROM pg_constraint WHERE conrelid='credeals.cre_listings'::regclass AND conname='cre_listings_available_space_count_nonnegative'")" != 0 ]]; then
    echo "FAIL unique $label index allowed partial DDL"; exit 1
  fi
  "${pg[@]}" -c 'DROP INDEX credeals.test_available_count_unique'
}
test_unique_index_refusal direct 'CREATE UNIQUE INDEX test_available_count_unique ON credeals.cre_listings (available_space_count)'
test_unique_index_refusal expression 'CREATE UNIQUE INDEX test_available_count_unique ON credeals.cre_listings ((available_space_count + 1))'
test_unique_index_refusal predicate 'CREATE UNIQUE INDEX test_available_count_unique ON credeals.cre_listings (id) WHERE available_space_count > 0'
echo 'PASS unique-index collisions fail closed'
# A competing session holds the table's DDL lock: the migration must fail at
# bounded lock acquisition before reading catalog or altering anything.
"${pg[@]}" > "$test_root/holding-lock.log" 2>&1 <<'SQL' &
BEGIN;
LOCK TABLE credeals.cre_listings IN ACCESS EXCLUSIVE MODE;
SELECT pg_sleep(6);
COMMIT;
SQL
locker_pid=$!
locked=0
for i in {1..40}; do
  if [[ "$("${pg[@]}" -Atc "SELECT EXISTS(SELECT FROM pg_locks WHERE relation='credeals.cre_listings'::regclass AND mode='AccessExclusiveLock' AND granted)")" == "t" ]]; then locked=1; break; fi
  sleep 0.1
done
if [[ "$locked" != 1 ]]; then echo 'FAIL competing lock did not start'; exit 1; fi
if "${pg[@]}" < "$migration" > "$test_root/lock-collision.log" 2>&1; then
  echo 'FAIL competing table lock accepted'; exit 1
fi
if ! grep -q 'lock timeout' "$test_root/lock-collision.log"; then cat "$test_root/lock-collision.log"; exit 1; fi
wait "$locker_pid"
"${pg[@]}" <<'SQL'
DO $$ BEGIN
 IF EXISTS (SELECT FROM pg_constraint WHERE conrelid='credeals.cre_listings'::regclass AND conname='cre_listings_available_space_count_nonnegative') THEN RAISE EXCEPTION 'lock failure partially applied'; END IF;
END $$;
SELECT 'PASS additional constraint and bounded competing-lock refusal' AS result;
SQL
printf 'Evidence directory: %s\n' "$test_root"
