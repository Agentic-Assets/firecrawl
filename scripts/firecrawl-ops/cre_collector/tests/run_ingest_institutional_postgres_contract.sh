#!/usr/bin/env bash
# Opt-in contract: exact generated optional UPDATE, synthetic data, owned PG17.
# No external database URL, network, image pull, or persistent volume is used.
set -euo pipefail

COLLECTOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG_IMAGE="${PG_IMAGE:-postgres:17}"
CONTAINER="firecrawl-institutional-contract-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
CONTAINER_ID=""

cleanup() {
  local exit_status=$?
  if [[ -n "$CONTAINER_ID" ]]; then
    if ! python3 - "$CONTAINER_ID" <<'CLEANUP'
import subprocess
import sys

try:
    subprocess.run(
        ["docker", "rm", "--force", sys.argv[1]],
        capture_output=True, timeout=15, check=True,
    )
except (OSError, subprocess.SubprocessError):
    print("Disposable PostgreSQL cleanup could not be confirmed.", file=sys.stderr)
    sys.exit(1)
CLEANUP
    then
      exit_status=1
    fi
  fi
  trap - EXIT
  exit "$exit_status"
}

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "Docker is required for this opt-in disposable contract." >&2
  exit 78
fi
if ! docker image inspect "$PG_IMAGE" >/dev/null 2>&1; then
  echo "The PostgreSQL image must already be cached." >&2
  exit 78
fi

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
CONTAINER_ID="$(docker create --pull never --name "$CONTAINER" \
  --network none --cpus 1 --memory 512m --pids-limit 128 \
  --tmpfs /var/lib/postgresql/data \
  --env POSTGRES_HOST_AUTH_METHOD=trust "$PG_IMAGE")"
docker start "$CONTAINER_ID" >/dev/null

# The image's bootstrap server uses only a Unix socket; wait for final TCP.
ready=0
for _ in {1..30}; do
  if docker exec "$CONTAINER_ID" pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" -ne 1 ]]; then
  echo "Disposable PostgreSQL did not become ready within 30 seconds." >&2
  exit 1
fi

python3 - "$COLLECTOR_DIR" <<'PY' | docker exec -i "$CONTAINER_ID" \
  psql -X -h 127.0.0.1 -U postgres -v ON_ERROR_STOP=1 --quiet
import sys

sys.path.insert(0, sys.argv[1])
import cre_ingest

sql = cre_ingest.build_sql([], [], "2026-10-12T00:00:00Z", set())
stage_start = sql.index("CREATE TEMP TABLE _stage (")
stage_end = sql.index("ON COMMIT DROP;", stage_start) + len("ON COMMIT DROP;")
stage_ddl = sql[stage_start:stage_end]
update_start = sql.index("DO $$ BEGIN", sql.index("-- Phase-2 data-lift: write the new institutional columns"))
update_end = sql.index("END $$;", update_start) + len("END $$;")
update = sql[update_start:update_end]

print("BEGIN; SET LOCAL statement_timeout = '10s';")
print("CREATE SCHEMA credeals;")
print(stage_ddl)
print("""
CREATE TABLE credeals.cre_brokerages (id uuid PRIMARY KEY, slug text UNIQUE);
CREATE TABLE credeals.cre_listings (LIKE _stage, brokerage_id uuid, updated_at timestamptz DEFAULT now());
CREATE TEMP TABLE _update_hits (external_id text);
CREATE FUNCTION credeals.count_institutional_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO _update_hits VALUES (NEW.external_id);
  NEW.updated_at = now();
  RETURN NEW;
END $$;
CREATE TRIGGER institutional_update_probe BEFORE UPDATE ON credeals.cre_listings
FOR EACH ROW EXECUTE FUNCTION credeals.count_institutional_update();
CREATE TEMP TABLE _src (LIKE credeals.cre_listings) ON COMMIT DROP;
INSERT INTO credeals.cre_brokerages VALUES
 ('10000000-0000-4000-8000-000000000001', 'cbre'),
 ('10000000-0000-4000-8000-000000000002', 'svn'),
 ('10000000-0000-4000-8000-000000000003', 'jll');
INSERT INTO _src
 (brokerage_id, external_id, raw_data, price_per_unit, grm, price_per_acre, revpar)
VALUES
 ('10000000-0000-4000-8000-000000000001', 'cbre-visible',
  '{"sourceKey":"cbre"}', 200, 200, 200, 200),
 ('10000000-0000-4000-8000-000000000002', 'svn-collision',
  '{"sourceKey":"svn","jllPriceWithheld":true}', NULL, NULL, NULL, NULL),
 ('10000000-0000-4000-8000-000000000003', 'jll-hidden',
  '{"sourceKey":"jll","jllPriceWithheld":true}', NULL, NULL, NULL, NULL),
 ('10000000-0000-4000-8000-000000000003', 'jll-visible-sparse',
  '{"sourceKey":"jll"}', NULL, NULL, NULL, NULL),
 ('10000000-0000-4000-8000-000000000003', 'investor:collision',
  '{"sourceKey":"jll-investor","jllPriceWithheld":true}', NULL, NULL, NULL, NULL);
INSERT INTO credeals.cre_listings
 (brokerage_id, external_id, raw_data, building_class,
  price_per_unit, grm, price_per_acre, revpar, extra_facts)
SELECT brokerage_id, external_id, raw_data, 'A',
       500, 500, 500, 500, '{"retained":true}'::jsonb
FROM _src;
""")
# Execute the exact production DO block twice to check repeat application too.
print(update)
print("TRUNCATE _update_hits;")
print(update)
print("""DO $$ BEGIN
 IF (SELECT count(*) FROM _update_hits) <> 0 THEN
  RAISE EXCEPTION 'unchanged repeat rewrote % institutional rows', (SELECT count(*) FROM _update_hits);
 END IF;
 IF EXISTS (SELECT 1 FROM credeals.cre_listings WHERE updated_at IS DISTINCT FROM now()) THEN
  RAISE EXCEPTION 'same-transaction updated_at changed';
 END IF;
END $$;""")
print("""
DO $$
DECLARE r record; expected numeric;
BEGIN
  IF (SELECT count(*) FROM credeals.cre_listings) <> 5 THEN
    RAISE EXCEPTION 'institutional update changed parent cardinality';
  END IF;
  FOR r IN SELECT * FROM credeals.cre_listings LOOP
    expected := CASE r.external_id WHEN 'cbre-visible' THEN 200
                WHEN 'jll-hidden' THEN NULL ELSE 500 END;
    IF r.price_per_unit IS DISTINCT FROM expected
       OR r.grm IS DISTINCT FROM expected
       OR r.price_per_acre IS DISTINCT FROM expected
       OR r.revpar IS DISTINCT FROM expected THEN
      RAISE EXCEPTION 'monetary privacy/sparse-preservation contract failed';
    END IF;
    IF r.external_id = 'jll-hidden' THEN
      IF r.extra_facts IS NOT NULL THEN
        RAISE EXCEPTION 'withheld JLL extra facts survived';
      END IF;
    ELSIF r.extra_facts IS DISTINCT FROM '{"retained":true}'::jsonb THEN
      RAISE EXCEPTION 'non-withheld extra facts were cleared';
    END IF;
  END LOOP;
END $$;
""")


def check_update(label, expected_hits):
    print("TRUNCATE _update_hits;")
    print(update)
    print(f"""DO $$ BEGIN
      IF (SELECT count(*) FROM _update_hits) <> {expected_hits} THEN
        RAISE EXCEPTION '{label}: expected {expected_hits} writes, got %',
          (SELECT count(*) FROM _update_hits);
      END IF;
      IF EXISTS (SELECT 1 FROM credeals.cre_listings WHERE updated_at IS DISTINCT FROM now()) THEN
        RAISE EXCEPTION '{label}: same-transaction timestamp changed';
      END IF;
    END $$;""")


# Every assignment must still update real differences and keep sparse values.
# Use exact generated SQL; only inputs and expected write counts are synthetic.
values = {
    "canonical_url": "'https://example.invalid/current'",
    "building_class": "'B'",
    "property_subtype": "'Warehouse'",
    "apn": "'APN-contract'",
    "tenant_name": "'Synthetic tenant'",
    "guarantor": "'Synthetic guarantor'",
    "lease_years_remaining": "7.5",
    "price_per_unit": "201",
    "grm": "8",
    "price_per_acre": "10000",
    "num_rooms": "3",
    "revpar": "101",
    "clear_height_ft": "20",
    "dock_doors": "2",
    "drive_in_doors": "3",
    "power_service": "'400A'",
    "rail_served": "false",
    "extra_facts": "'{\"new\":true}'::jsonb",
}
for column, value in values.items():
    print(f"UPDATE _src SET {column} = {value} WHERE external_id = 'cbre-visible';")
    check_update(column + " changed", 1)
    check_update(column + " repeat", 0)
    if column != "extra_facts":
        print(f"UPDATE _src SET {column} = NULL WHERE external_id = 'cbre-visible';")
        check_update(column + " sparse keep", 0)
        print(f"UPDATE _src SET {column} = {value} WHERE external_id = 'cbre-visible';")

json_cases = [
    ("SQL NULL normalizes to object", "NULL", "NULL", "'{}'", 1),
    ("JSON null with sparse source", "'null'", "NULL", "'null'", 0),
    ("JSON null with empty object", "'null'", "'{}'", "'null'", 0),
    ("array concatenation", "'[1]'", "'[2]'", "'[1,2]'", 1),
    ("scalar concatenation", "'7'", "'9'", "'[7,9]'", 1),
    ("array object concatenation", "'[1]'", "'{\"x\":2}'", "'[1,{\"x\":2}]'", 1),
    ("object scalar concatenation", "'{\"a\":1}'", "'true'", "'[{\"a\":1},true]'", 1),
    ("boolean versus number", "'{\"v\":false}'", "'{\"v\":0}'", "'{\"v\":0}'", 1),
    ("numeric JSON scale preserved", "'{\"v\":1}'", "'{\"v\":1.0}'", "'{\"v\":1.0}'", 1),
    ("string versus number", "'\"1\"'", "'1'", "'[\"1\",1]'", 1),
    ("empty array is not empty object", "'{}'", "'[]'", "'[{}]'", 1),
    ("empty array keeps array", "'[{}]'", "'[]'", "'[{}]'", 0),
]
for label, old, incoming, expected, hits in json_cases:
    print(f"UPDATE credeals.cre_listings SET extra_facts={old}::jsonb WHERE external_id='cbre-visible';")
    print(f"UPDATE _src SET extra_facts={incoming}::jsonb WHERE external_id='cbre-visible';")
    check_update(label, hits)
    print(f"""DO $$ BEGIN
      IF (SELECT extra_facts FROM credeals.cre_listings WHERE external_id='cbre-visible')
         IS DISTINCT FROM {expected}::jsonb THEN
        RAISE EXCEPTION '{label}: JSONB value changed';
      END IF;
    END $$;""")
    if label == "numeric JSON scale preserved":
        print(f"""DO $$ BEGIN
          IF (SELECT extra_facts::text FROM credeals.cre_listings WHERE external_id='cbre-visible')
             IS DISTINCT FROM ({expected}::jsonb)::text THEN
            RAISE EXCEPTION 'numeric JSON scale text changed';
          END IF;
        END $$;""")
        check_update("numeric JSON scale identical repeat", 0)

# Existing unconstrained NUMERIC scale is observable in serialized output.
print("UPDATE credeals.cre_listings SET price_per_unit=1.10 WHERE external_id='cbre-visible';")
print("UPDATE _src SET price_per_unit=1.1 WHERE external_id='cbre-visible';")
check_update("numeric scalar scale preserved", 1)
print("""DO $$ BEGIN
  IF (SELECT price_per_unit::text FROM credeals.cre_listings WHERE external_id='cbre-visible') <> '1.1' THEN
    RAISE EXCEPTION 'numeric scalar scale output changed';
  END IF;
END $$;""")
check_update("numeric scalar scale identical repeat", 0)

# SQL numeric zero and boolean false are values, not sparse/missing evidence.
print("UPDATE _src SET grm=0,rail_served=false WHERE external_id='cbre-visible';")
check_update("numeric zero remains a value", 1)
check_update("numeric zero repeat", 0)
# Pre-012 compatibility stays a clean no-op, including no trigger invocation.
print("ALTER TABLE credeals.cre_listings DROP COLUMN building_class;")
check_update("missing optional schema", 0)
print("ROLLBACK; SELECT 'institutional PostgreSQL contract passed: 5 source cases, all18fields, sparse/privacy/JSONB/clock/write-count contracts';")
PY
