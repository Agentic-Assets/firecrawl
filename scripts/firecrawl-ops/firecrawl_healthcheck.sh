#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVIDENCE_DIR="${FIRECRAWL_HEALTHCHECK_EVIDENCE_DIR:-}"

usage() {
  printf '%s\n' \
    'Usage: firecrawl_healthcheck.sh [--evidence-dir DIR]' \
    '' \
    'Checks the local Firecrawl Docker/API stack. When --evidence-dir is provided,' \
    'also writes timestamped JSON and Markdown evidence files.'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --evidence-dir)
      EVIDENCE_DIR="${2:-}"
      if [[ -z "$EVIDENCE_DIR" ]]; then
        echo "--evidence-dir requires a value" >&2
        exit 2
      fi
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

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

  local candidate
  for candidate in \
    "$HOME/Github/agentic-assets/firecrawl" \
    "$HOME/Documents/GitHub/agentic-assets/firecrawl"
  do
    if [[ -f "$candidate/docker-compose.yaml" && -d "$candidate/apps/api" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done

  echo "Could not find the Firecrawl repo. Set FC_DIR=/path/to/firecrawl and rerun." >&2
  exit 1
}

FC_DIR="$(resolve_fc_dir)"
API_URL="${API_URL:-http://localhost:3002}"
CURL_CONNECT_TIMEOUT="${FIRECRAWL_HEALTHCHECK_CONNECT_TIMEOUT:-10}"
CURL_MAX_TIME="${FIRECRAWL_HEALTHCHECK_MAX_TIME:-90}"
CURL_RETRIES="${FIRECRAWL_HEALTHCHECK_RETRIES:-12}"
CURL_RETRY_DELAY="${FIRECRAWL_HEALTHCHECK_RETRY_DELAY:-2}"

cd "$FC_DIR"

STATUS="pass"
ERRORS=()

mark_failure() {
  STATUS="fail"
  ERRORS+=("$1")
}

echo "[1/4] docker compose ps"
if ! DOCKER_PS="$(docker compose ps --no-trunc 2>&1)"; then
  echo "$DOCKER_PS"
  mark_failure "docker compose ps failed"
else
  echo "$DOCKER_PS"
fi

IMAGE_ID="$(docker image inspect firecrawl-api:latest --format '{{.Id}}' 2>/dev/null || true)"
if [[ -z "$IMAGE_ID" ]]; then
  IMAGE_ID="$(docker compose images api 2>/dev/null || true)"
fi

echo "[2/4] API root check"
ROOT_RESP=""
ROOT_OK=0
for ((attempt = 1; attempt <= CURL_RETRIES; attempt++)); do
  if ROOT_RESP="$(curl -fsS --connect-timeout "$CURL_CONNECT_TIMEOUT" --max-time "$CURL_MAX_TIME" "$API_URL/" 2>&1)"; then
    ROOT_OK=1
    break
  fi
  if (( attempt < CURL_RETRIES )); then
    sleep "$CURL_RETRY_DELAY"
  fi
done
if [[ "$ROOT_OK" != "1" ]]; then
  echo "$ROOT_RESP"
  mark_failure "API root check failed after ${CURL_RETRIES} attempts"
else
  printf '%s' "$ROOT_RESP" | head -c 200 && echo
fi

echo "[3/4] scrape smoke test"
if ! RESP=$(curl -fsS --connect-timeout "$CURL_CONNECT_TIMEOUT" --max-time "$CURL_MAX_TIME" -X POST "$API_URL/v2/scrape" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","formats":["markdown"]}' 2>&1); then
  echo "$RESP"
  SCRAPE_SUMMARY="{}"
  mark_failure "scrape smoke request failed"
else
  if ! SCRAPE_SUMMARY="$(printf '%s' "$RESP" | python3 -c '
import json
import sys

j = json.load(sys.stdin)
ok = bool(j.get("success"))
md = (j.get("data") or {}).get("markdown", "")
print(json.dumps({"success": ok, "markdown_len": len(md)}))
if not ok:
    raise SystemExit(1)
' 2>&1)"; then
    echo "$SCRAPE_SUMMARY"
    mark_failure "scrape smoke response validation failed"
  else
    echo "$SCRAPE_SUMMARY"
  fi
fi

echo "[4/4] done"

if [[ -n "$EVIDENCE_DIR" ]]; then
  mkdir -p "$EVIDENCE_DIR"
  STAMP="$(date +%Y%m%d-%H%M%S)"
  JSON_PATH="$EVIDENCE_DIR/${STAMP}-firecrawl-healthcheck.json"
  MD_PATH="$EVIDENCE_DIR/${STAMP}-firecrawl-healthcheck.md"
  export STATUS API_URL FC_DIR DOCKER_PS ROOT_RESP RESP SCRAPE_SUMMARY IMAGE_ID JSON_PATH MD_PATH
  ERRORS_JSON="$(printf '%s\n' "${ERRORS[@]}" | python3 -c 'import json,sys; print(json.dumps([line for line in sys.stdin.read().splitlines() if line]))')"
  export ERRORS_JSON
  python3 "$SCRIPT_DIR/firecrawl_healthcheck_evidence.py"
  echo "wrote $JSON_PATH"
  echo "wrote $MD_PATH"
fi

if [[ "$STATUS" != "pass" ]]; then
  exit 1
fi
