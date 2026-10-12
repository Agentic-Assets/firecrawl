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

ready=0
for _ in {1..30}; do
  if docker exec "$CONTAINER_ID" pg_isready -U postgres >/dev/null 2>&1; then
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
  psql -X -U postgres -v ON_ERROR_STOP=1 --quiet
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
CREATE TABLE credeals.cre_listings (LIKE _stage, brokerage_id uuid);
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
print(update)
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
ROLLBACK;
SELECT 'institutional PostgreSQL contract passed: 5 cases, 2 applications';
""")
PY
