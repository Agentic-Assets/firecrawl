#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

resolve_fc_dir() {
  if [[ -n "${FC_DIR:-}" ]]; then
    printf '%s\n' "$FC_DIR"
    return
  fi

  local from_script
  from_script="$(cd "$SCRIPT_DIR/../.." && pwd)"
  if [[ -f "$from_script/docker-compose.yaml" && -d "$from_script/apps/api" ]]; then
    printf '%s\n' "$from_script"
    return
  fi

  if [[ -f "$PWD/docker-compose.yaml" && -d "$PWD/apps/api" ]]; then
    printf '%s\n' "$PWD"
    return
  fi

  local common="$HOME/Documents/GitHub/agentic-assets/firecrawl"
  if [[ -f "$common/docker-compose.yaml" && -d "$common/apps/api" ]]; then
    printf '%s\n' "$common"
    return
  fi

  echo "Could not find the Firecrawl repo. Set FC_DIR=/path/to/firecrawl and rerun." >&2
  exit 1
}

FC_DIR="$(resolve_fc_dir)"
API_URL="${API_URL:-http://localhost:3002}"

cd "$FC_DIR"

echo "[1/4] docker compose ps"
docker compose ps

echo "[2/4] API root check"
curl -fsS "$API_URL/" | head -c 200 && echo

echo "[3/4] scrape smoke test"
RESP=$(curl -fsS -X POST "$API_URL/v2/scrape" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","formats":["markdown"]}')
export RESP

python3 - <<'PY'
import json, os
j=json.loads(os.environ['RESP'])
ok=bool(j.get('success'))
md=(j.get('data') or {}).get('markdown','')
print({'success': ok, 'markdown_len': len(md)})
if not ok:
    raise SystemExit(1)
PY

echo "[4/4] done"
