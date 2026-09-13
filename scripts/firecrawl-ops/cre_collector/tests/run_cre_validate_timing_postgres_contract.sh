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
import sys
from pathlib import Path

collector_dir = Path(sys.argv[1])
sys.path.insert(0, str(collector_dir))

from cre_validate import run_queries_with_timings

rows, timings, stderr = run_queries_with_timings(
    sys.argv[2],
    f"postgresql://postgres@127.0.0.1:{sys.argv[3]}/postgres",
    {
        "under_one_second": (
            "SELECT 'Time: 1234.567 ms (00:01.235)'::text "
            "AS result_like_timing_text;"
        ),
        "over_one_second": (
            "SELECT 'slow'::text AS label "
            "FROM (SELECT pg_sleep(1.1)) AS slept;"
        ),
    },
)

assert stderr == ""
assert rows == {
    "under_one_second": [
        {"result_like_timing_text": "Time: 1234.567 ms (00:01.235)"}
    ],
    "over_one_second": [{"label": "slow"}],
}
assert timings["under_one_second"]["status"] == "available"
assert 0 <= timings["under_one_second"]["elapsed_ms"] < 1000
assert timings["over_one_second"]["status"] == "available"
assert timings["over_one_second"]["elapsed_ms"] >= 1000
assert all(
    timing["scope"] == "psql_client_elapsed" for timing in timings.values()
)
print("CRE validation native psql timing contract passed")
PY
