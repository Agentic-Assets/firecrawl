#!/usr/bin/env bash
# Native psql timing contract for cre_validate's isolated result/timing channels.
#
# This is opt-in so the regular pytest suite remains pure. It uses the host
# psql client against a loopback-only disposable PostgreSQL container, invokes
# the production Python runner, and always removes the container.

set -euo pipefail

COLLECTOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG_IMAGE="${PG_IMAGE:-postgres:18}"
CONTAINER="firecrawl-cre-validate-timing-contract-$$"
PSQL_BIN="${PSQL_BIN:-$(command -v psql || true)}"

cleanup() {
  docker rm --force "$CONTAINER" >/dev/null 2>&1 || true
}

if [[ -z "$PSQL_BIN" || ! -x "$PSQL_BIN" ]]; then
  echo "A native psql client is required for the validation timing contract." >&2
  exit 78
fi
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "Docker must be available for the validation timing contract." >&2
  exit 78
fi
if ! docker image inspect "$PG_IMAGE" >/dev/null 2>&1; then
  echo "The PostgreSQL test image must already be cached: $PG_IMAGE" >&2
  exit 78
fi

trap cleanup EXIT
docker run --detach --rm --pull never --name "$CONTAINER" \
  --tmpfs /var/lib/postgresql \
  --env POSTGRES_HOST_AUTH_METHOD=trust \
  --publish 127.0.0.1::5432 \
  "$PG_IMAGE" >/dev/null

ready=0
for _ in {1..30}; do
  if docker exec "$CONTAINER" pg_isready -U postgres >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" -ne 1 ]]; then
  echo "Disposable PostgreSQL did not become ready within 30 seconds." >&2
  exit 1
fi

port_spec="$(docker port "$CONTAINER" 5432/tcp)"
port="${port_spec##*:}"
if [[ ! "$port" =~ ^[0-9]+$ ]]; then
  echo "Could not resolve the disposable PostgreSQL loopback port." >&2
  exit 1
fi

nice -n 10 python3 - "$COLLECTOR_DIR" "$PSQL_BIN" "$port" <<'PY'
import contextlib
import io
import json
import subprocess
import sys

collector_dir = sys.argv[1]
sys.path.insert(0, collector_dir)

import cre_validate

psql = sys.argv[2]
db_url = f"postgresql://postgres@127.0.0.1:{sys.argv[3]}/postgres"
setup_sql = r'''
CREATE SCHEMA credeals;
CREATE TABLE credeals.cre_brokerages (id text PRIMARY KEY, slug text NOT NULL);
CREATE TABLE credeals.cre_listings (
  id text PRIMARY KEY, brokerage_id text NOT NULL REFERENCES credeals.cre_brokerages(id),
  deleted_at timestamptz
);
CREATE TABLE credeals.cre_listing_images (
  id bigint PRIMARY KEY, listing_id text NOT NULL REFERENCES credeals.cre_listings(id),
  url text NOT NULL, is_primary boolean, display_order integer, alt_text text
);
INSERT INTO credeals.cre_brokerages VALUES ('tw', 'transwestern'), ('other', 'other');
INSERT INTO credeals.cre_listings VALUES
  ('tw-active', 'tw', NULL), ('other-active', 'other', NULL),
  ('tw-deleted', 'tw', now());
INSERT INTO credeals.cre_listing_images VALUES
  (1, 'tw-active', 'https://images.example.test/property.jpg', true, 0, 'Property'),
  (2, 'other-active', 'https://images.example.test/other.jpg', true, 0, 'Other'),
  (3, 'tw-deleted', 'https://images.example.test/deleted.jpg', true, 0, 'Deleted');
'''
subprocess.run(
    [psql, "-X", "-v", "ON_ERROR_STOP=1", db_url, "-c", setup_sql],
    check=True,
    capture_output=True,
    text=True,
)

cre_validate.load_db_url = lambda _env_file: (db_url, "<disposable-local-postgres>")
cre_validate.assert_expected_database_target = lambda *_args: None
cre_validate.find_psql = lambda: psql
cre_validate.queries_for_profile = lambda *_args, **_kwargs: {
    "under_one_second": (
        "SELECT 'Time: 1234.567 ms (00:01.235)'::text "
        "AS result_like_timing_text;"
    ),
    "over_one_second": (
        "SELECT 'slow'::text AS label FROM (SELECT pg_sleep(1.1)) AS slept;"
    ),
    "historical_primary_child_conflicts": (
        "SELECT 0::bigint AS conflict_count;"
    ),
}
sys.argv = [
    "cre_validate.py",
    "--format",
    "json",
    "--transwestern-image-state",
    "--expected-artifact-run-key",
    f"ingest:v1:{'a' * 64}",
    "--expected-db-target-sha256",
    "b" * 64,
]

with contextlib.redirect_stdout(io.StringIO()) as output:
    cre_validate.main()
report = json.loads(output.getvalue())
assert report["queries"]["under_one_second"] == [
    {"result_like_timing_text": "Time: 1234.567 ms (00:01.235)"}
]
assert report["queries"]["over_one_second"] == [{"label": "slow"}]
assert report["historical_primary_child_conflicts"] == [{"conflict_count": "0"}]
assert report["transwestern_image_state"][0]["image_count"] == "1"
digest = report["transwestern_image_state"][0]["image_state_sha256"]
assert len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest)
assert report["query_timings"]["under_one_second"]["status"] == "available"
assert 0 <= report["query_timings"]["under_one_second"]["elapsed_ms"] < 1000
assert report["query_timings"]["over_one_second"]["status"] == "available"
assert report["query_timings"]["over_one_second"]["elapsed_ms"] >= 1000
assert report["query_timings"]["transwestern_image_state"]["status"] == "available"
assert all(
    timing["scope"] == "psql_client_elapsed"
    for timing in report["query_timings"].values()
)
print("CRE validation timed Transwestern optional-query and rollback contract passed")
PY
