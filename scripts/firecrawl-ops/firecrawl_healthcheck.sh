#!/usr/bin/env bash
set -euo pipefail

FC_DIR="${FC_DIR:-$HOME/Documents/GitHub/firecrawl}"
API_URL="${API_URL:-http://localhost:3002}"

cd "$FC_DIR"

echo "[1/4] docker compose ps"
docker compose ps

echo "[2/4] API root check"
curl -fsS "$API_URL/" | head -c 200 && echo

echo "[3/4] scrape smoke test"
RESP=$(curl -fsS -X POST "$API_URL/v1/scrape" \
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
