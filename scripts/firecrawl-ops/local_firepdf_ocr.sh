#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${LOCAL_FIREPDF_STATE_DIR:-${TMPDIR:-/tmp}/firecrawl-local-ocr}"
ADAPTER_PORT="${LOCAL_FIREPDF_PORT:-31337}"
DOCLING_PORT="${LOCAL_FIREPDF_DOCLING_PORT:-5001}"
DOCLING_IMAGE="${LOCAL_FIREPDF_DOCLING_IMAGE:-quay.io/docling-project/docling-serve-cpu@sha256:528f52f5ce56be1739df117560512ffaa483be53934bf5b0458b4ca088b1a6b0}"
DOCLING_CONTAINER="${LOCAL_FIREPDF_DOCLING_CONTAINER:-firecrawl-docling-serve}"
ADAPTER_IMAGE="${LOCAL_FIREPDF_ADAPTER_IMAGE:-firecrawl-local-firepdf-adapter:latest}"
ADAPTER_CONTAINER="${LOCAL_FIREPDF_ADAPTER_CONTAINER:-firecrawl-local-firepdf-adapter}"
ADAPTER_LOG="$STATE_DIR/adapter.log"
ADAPTER_PID="$STATE_DIR/adapter.pid"
DOCLING_URL="${LOCAL_FIREPDF_DOCLING_URL:-http://127.0.0.1:${DOCLING_PORT}}"

mkdir -p "$STATE_DIR"

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

usage() {
  cat <<EOF
Usage: local_firepdf_ocr.sh <command>

Commands:
  start-docling      Start Docling Serve in OrbStack/Docker.
  start-adapter      Build/start the local FirePDF-compatible adapter container on port $ADAPTER_PORT.
  start              Start Docling Serve and the adapter.
  health             Check Docling Serve and the adapter.
  env                Print .env entries needed by Firecrawl.
  settings           Print adapter/Docling tuning env vars.
  enable-firecrawl   Write local OCR routing entries into repo-root .env.
  stop-adapter       Stop the local adapter.
  stop-docling       Stop the Docling Serve container.
  stop               Stop adapter and Docling Serve.
  logs               Tail adapter and Docling logs.
  status             Show process/container status.

Environment:
  LOCAL_FIREPDF_PORT=$ADAPTER_PORT
  LOCAL_FIREPDF_DOCLING_PORT=$DOCLING_PORT
  LOCAL_FIREPDF_DOCLING_IMAGE=$DOCLING_IMAGE
  LOCAL_FIREPDF_ADAPTER_IMAGE=$ADAPTER_IMAGE
  LOCAL_FIREPDF_STATE_DIR=$STATE_DIR
  LOCAL_FIREPDF_DOCLING_OCR_PRESET=auto|easyocr|tesseract
  LOCAL_FIREPDF_DOCLING_OCR_LANG=en[,de,...]
  LOCAL_FIREPDF_DOCLING_PDF_BACKEND=docling_parse|pypdfium2|dlparse_v4
  LOCAL_FIREPDF_DOCLING_TABLE_MODE=accurate|fast
  LOCAL_FIREPDF_DOCLING_TO_FORMATS=md,json,html
EOF
}

wait_for_url() {
  local url="$1"
  local label="$2"
  local attempts="${3:-120}"
  local i
  for ((i = 1; i <= attempts; i++)); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      echo "$label is ready: $url"
      return 0
    fi
    sleep 2
  done
  echo "$label did not become ready: $url" >&2
  return 1
}

start_docling() {
  if docker ps --format '{{.Names}}' | grep -qx "$DOCLING_CONTAINER"; then
    echo "Docling Serve already running: $DOCLING_CONTAINER"
  else
    if docker ps -a --format '{{.Names}}' | grep -qx "$DOCLING_CONTAINER"; then
      docker rm "$DOCLING_CONTAINER" >/dev/null
    fi
    docker run -d \
      --name "$DOCLING_CONTAINER" \
      -p "127.0.0.1:${DOCLING_PORT}:5001" \
      -e DOCLING_SERVE_ENABLE_UI=1 \
      "$DOCLING_IMAGE" >/dev/null
    echo "Started Docling Serve container: $DOCLING_CONTAINER"
  fi
  wait_for_url "http://127.0.0.1:${DOCLING_PORT}/docs" "Docling Serve"
}

adapter_running() {
  docker ps --format '{{.Names}}' | grep -qx "$ADAPTER_CONTAINER"
}

start_adapter() {
  if adapter_running; then
    echo "Adapter already running: $ADAPTER_CONTAINER"
    return 0
  fi
  local fc_dir
  fc_dir="$(resolve_fc_dir)"
  if docker ps -a --format '{{.Names}}' | grep -qx "$ADAPTER_CONTAINER"; then
    docker rm "$ADAPTER_CONTAINER" >/dev/null
  fi
  docker build -t "$ADAPTER_IMAGE" -f "$SCRIPT_DIR/local-firepdf-adapter.Dockerfile" "$fc_dir"
  docker run -d \
    --name "$ADAPTER_CONTAINER" \
    -p "127.0.0.1:${ADAPTER_PORT}:31337" \
    -e LOCAL_FIREPDF_HOST=0.0.0.0 \
    -e LOCAL_FIREPDF_PORT=31337 \
    -e LOCAL_FIREPDF_ENGINE="${LOCAL_FIREPDF_ENGINE:-docling}" \
    -e LOCAL_FIREPDF_DOCLING_URL="http://host.docker.internal:${DOCLING_PORT}" \
    -e LOCAL_FIREPDF_DOCLING_DO_OCR="${LOCAL_FIREPDF_DOCLING_DO_OCR:-true}" \
    -e LOCAL_FIREPDF_DOCLING_FORCE_OCR="${LOCAL_FIREPDF_DOCLING_FORCE_OCR:-true}" \
    -e LOCAL_FIREPDF_DOCLING_TO_FORMATS="${LOCAL_FIREPDF_DOCLING_TO_FORMATS:-md,json,html}" \
    -e LOCAL_FIREPDF_DOCLING_OCR_PRESET="${LOCAL_FIREPDF_DOCLING_OCR_PRESET:-auto}" \
    -e LOCAL_FIREPDF_DOCLING_OCR_LANG="${LOCAL_FIREPDF_DOCLING_OCR_LANG:-}" \
    -e LOCAL_FIREPDF_DOCLING_PDF_BACKEND="${LOCAL_FIREPDF_DOCLING_PDF_BACKEND:-docling_parse}" \
    -e LOCAL_FIREPDF_DOCLING_TABLE_MODE="${LOCAL_FIREPDF_DOCLING_TABLE_MODE:-accurate}" \
    -e LOCAL_FIREPDF_DOCLING_TABLE_CELL_MATCHING="${LOCAL_FIREPDF_DOCLING_TABLE_CELL_MATCHING:-true}" \
    -e LOCAL_FIREPDF_DOCLING_DO_TABLE_STRUCTURE="${LOCAL_FIREPDF_DOCLING_DO_TABLE_STRUCTURE:-true}" \
    -e LOCAL_FIREPDF_DOCLING_INCLUDE_IMAGES="${LOCAL_FIREPDF_DOCLING_INCLUDE_IMAGES:-true}" \
    -e LOCAL_FIREPDF_DOCLING_IMAGES_SCALE="${LOCAL_FIREPDF_DOCLING_IMAGES_SCALE:-2.0}" \
    -e LOCAL_FIREPDF_DOCLING_IMAGE_EXPORT_MODE="${LOCAL_FIREPDF_DOCLING_IMAGE_EXPORT_MODE:-placeholder}" \
    -e LOCAL_FIREPDF_DOCLING_MD_PAGE_BREAK="${LOCAL_FIREPDF_DOCLING_MD_PAGE_BREAK:-}" \
    -e LOCAL_FIREPDF_DOCLING_DO_CODE_ENRICHMENT="${LOCAL_FIREPDF_DOCLING_DO_CODE_ENRICHMENT:-false}" \
    -e LOCAL_FIREPDF_DOCLING_DO_FORMULA_ENRICHMENT="${LOCAL_FIREPDF_DOCLING_DO_FORMULA_ENRICHMENT:-false}" \
    -e LOCAL_FIREPDF_DOCLING_DO_PICTURE_CLASSIFICATION="${LOCAL_FIREPDF_DOCLING_DO_PICTURE_CLASSIFICATION:-false}" \
    -e LOCAL_FIREPDF_DOCLING_DO_CHART_EXTRACTION="${LOCAL_FIREPDF_DOCLING_DO_CHART_EXTRACTION:-false}" \
    -e LOCAL_FIREPDF_DOCLING_DO_PICTURE_DESCRIPTION="${LOCAL_FIREPDF_DOCLING_DO_PICTURE_DESCRIPTION:-false}" \
    -e LOCAL_FIREPDF_DOCLING_VLM_PIPELINE_PRESET="${LOCAL_FIREPDF_DOCLING_VLM_PIPELINE_PRESET:-}" \
    -e LOCAL_FIREPDF_DOCLING_PICTURE_DESCRIPTION_PRESET="${LOCAL_FIREPDF_DOCLING_PICTURE_DESCRIPTION_PRESET:-}" \
    -e LOCAL_FIREPDF_DOCLING_CODE_FORMULA_PRESET="${LOCAL_FIREPDF_DOCLING_CODE_FORMULA_PRESET:-}" \
    -e LOCAL_FIREPDF_DOCLING_TABLE_STRUCTURE_PRESET="${LOCAL_FIREPDF_DOCLING_TABLE_STRUCTURE_PRESET:-}" \
    -e LOCAL_FIREPDF_DOCLING_LAYOUT_PRESET="${LOCAL_FIREPDF_DOCLING_LAYOUT_PRESET:-}" \
    -e LOCAL_FIREPDF_TIMEOUT_SECONDS="${LOCAL_FIREPDF_TIMEOUT_SECONDS:-240}" \
    "$ADAPTER_IMAGE" >/dev/null
  rm -f "$ADAPTER_PID"
  echo "Started local FirePDF adapter container: $ADAPTER_CONTAINER"
  wait_for_url "http://127.0.0.1:${ADAPTER_PORT}/health" "Local FirePDF adapter" 30
}

stop_adapter() {
  if adapter_running; then
    docker rm -f "$ADAPTER_CONTAINER" >/dev/null
    echo "Stopped local FirePDF adapter container"
  else
    rm -f "$ADAPTER_PID"
    if docker ps -a --format '{{.Names}}' | grep -qx "$ADAPTER_CONTAINER"; then
      docker rm -f "$ADAPTER_CONTAINER" >/dev/null
      echo "Removed stopped local FirePDF adapter container"
    else
      echo "Adapter is not running"
    fi
  fi
}

stop_docling() {
  if docker ps -a --format '{{.Names}}' | grep -qx "$DOCLING_CONTAINER"; then
    docker rm -f "$DOCLING_CONTAINER" >/dev/null
    echo "Stopped Docling Serve container"
  else
    echo "Docling Serve container is not present"
  fi
}

print_env() {
  cat <<EOF
FIRE_PDF_ENABLE=true
FIRE_PDF_PERCENT=100
FIRE_PDF_BASE_URL=http://host.docker.internal:${ADAPTER_PORT}
FIRE_PDF_API_KEY=
PDF_RUST_EXTRACT_ENABLE=true
MINERU_PERCENT=0
RUNPOD_MU_API_KEY=
RUNPOD_MU_POD_ID=
EOF
}

print_settings() {
  cat <<EOF
# Local Docling adapter settings. Export before local_firepdf_ocr.sh start-adapter/start.
LOCAL_FIREPDF_ENGINE=${LOCAL_FIREPDF_ENGINE:-docling}
LOCAL_FIREPDF_PORT=${LOCAL_FIREPDF_PORT:-31337}
LOCAL_FIREPDF_TIMEOUT_SECONDS=${LOCAL_FIREPDF_TIMEOUT_SECONDS:-240}
LOCAL_FIREPDF_DOCLING_PORT=${LOCAL_FIREPDF_DOCLING_PORT:-5001}
LOCAL_FIREPDF_DOCLING_IMAGE=${LOCAL_FIREPDF_DOCLING_IMAGE:-$DOCLING_IMAGE}
LOCAL_FIREPDF_DOCLING_TO_FORMATS=${LOCAL_FIREPDF_DOCLING_TO_FORMATS:-md,json,html}
LOCAL_FIREPDF_DOCLING_DO_OCR=${LOCAL_FIREPDF_DOCLING_DO_OCR:-true}
LOCAL_FIREPDF_DOCLING_FORCE_OCR=${LOCAL_FIREPDF_DOCLING_FORCE_OCR:-true}
LOCAL_FIREPDF_DOCLING_OCR_PRESET=${LOCAL_FIREPDF_DOCLING_OCR_PRESET:-auto}
LOCAL_FIREPDF_DOCLING_OCR_LANG=${LOCAL_FIREPDF_DOCLING_OCR_LANG:-}
LOCAL_FIREPDF_DOCLING_PDF_BACKEND=${LOCAL_FIREPDF_DOCLING_PDF_BACKEND:-docling_parse}
LOCAL_FIREPDF_DOCLING_TABLE_MODE=${LOCAL_FIREPDF_DOCLING_TABLE_MODE:-accurate}
LOCAL_FIREPDF_DOCLING_TABLE_CELL_MATCHING=${LOCAL_FIREPDF_DOCLING_TABLE_CELL_MATCHING:-true}
LOCAL_FIREPDF_DOCLING_DO_TABLE_STRUCTURE=${LOCAL_FIREPDF_DOCLING_DO_TABLE_STRUCTURE:-true}
LOCAL_FIREPDF_DOCLING_INCLUDE_IMAGES=${LOCAL_FIREPDF_DOCLING_INCLUDE_IMAGES:-true}
LOCAL_FIREPDF_DOCLING_IMAGES_SCALE=${LOCAL_FIREPDF_DOCLING_IMAGES_SCALE:-2.0}
LOCAL_FIREPDF_DOCLING_IMAGE_EXPORT_MODE=${LOCAL_FIREPDF_DOCLING_IMAGE_EXPORT_MODE:-placeholder}
LOCAL_FIREPDF_DOCLING_MD_PAGE_BREAK=${LOCAL_FIREPDF_DOCLING_MD_PAGE_BREAK:-}
LOCAL_FIREPDF_DOCLING_DO_CODE_ENRICHMENT=${LOCAL_FIREPDF_DOCLING_DO_CODE_ENRICHMENT:-false}
LOCAL_FIREPDF_DOCLING_DO_FORMULA_ENRICHMENT=${LOCAL_FIREPDF_DOCLING_DO_FORMULA_ENRICHMENT:-false}
LOCAL_FIREPDF_DOCLING_DO_PICTURE_CLASSIFICATION=${LOCAL_FIREPDF_DOCLING_DO_PICTURE_CLASSIFICATION:-false}
LOCAL_FIREPDF_DOCLING_DO_CHART_EXTRACTION=${LOCAL_FIREPDF_DOCLING_DO_CHART_EXTRACTION:-false}
LOCAL_FIREPDF_DOCLING_DO_PICTURE_DESCRIPTION=${LOCAL_FIREPDF_DOCLING_DO_PICTURE_DESCRIPTION:-false}
LOCAL_FIREPDF_DOCLING_VLM_PIPELINE_PRESET=${LOCAL_FIREPDF_DOCLING_VLM_PIPELINE_PRESET:-}
LOCAL_FIREPDF_DOCLING_PICTURE_DESCRIPTION_PRESET=${LOCAL_FIREPDF_DOCLING_PICTURE_DESCRIPTION_PRESET:-}
LOCAL_FIREPDF_DOCLING_CODE_FORMULA_PRESET=${LOCAL_FIREPDF_DOCLING_CODE_FORMULA_PRESET:-}
LOCAL_FIREPDF_DOCLING_TABLE_STRUCTURE_PRESET=${LOCAL_FIREPDF_DOCLING_TABLE_STRUCTURE_PRESET:-}
LOCAL_FIREPDF_DOCLING_LAYOUT_PRESET=${LOCAL_FIREPDF_DOCLING_LAYOUT_PRESET:-}

# Examples:
# LOCAL_FIREPDF_DOCLING_TABLE_MODE=fast scripts/firecrawl-ops/local_firepdf_ocr.sh start-adapter
# LOCAL_FIREPDF_DOCLING_OCR_PRESET=tesseract LOCAL_FIREPDF_DOCLING_OCR_LANG=en scripts/firecrawl-ops/local_firepdf_ocr.sh start-adapter
EOF
}

set_kv() {
  local env_path="$1"
  local key="$2"
  local val="$3"
  if grep -q "^${key}=" "$env_path"; then
    sed -i '' "s|^${key}=.*|${key}=${val}|" "$env_path"
  else
    printf "\n%s=%s\n" "$key" "$val" >> "$env_path"
  fi
}

enable_firecrawl() {
  local fc_dir env_path
  fc_dir="$(resolve_fc_dir)"
  env_path="${ENV_PATH:-$fc_dir/.env}"
  if [[ ! -f "$env_path" ]]; then
    "$SCRIPT_DIR/set_model_profile.sh" budget >/dev/null
  fi
  set_kv "$env_path" FIRE_PDF_ENABLE true
  set_kv "$env_path" FIRE_PDF_PERCENT 100
  set_kv "$env_path" FIRE_PDF_BASE_URL "http://host.docker.internal:${ADAPTER_PORT}"
  set_kv "$env_path" FIRE_PDF_API_KEY ""
  set_kv "$env_path" PDF_RUST_EXTRACT_ENABLE true
  set_kv "$env_path" MINERU_PERCENT 0
  set_kv "$env_path" RUNPOD_MU_API_KEY ""
  set_kv "$env_path" RUNPOD_MU_POD_ID ""
  echo "Enabled local OCR routing in $env_path"
  echo "Next: cd \"$fc_dir\" && docker compose up -d --force-recreate api"
}

health() {
  curl -fsS "http://127.0.0.1:${DOCLING_PORT}/docs" >/dev/null
  curl -fsS "http://127.0.0.1:${ADAPTER_PORT}/health" | jq .
}

status() {
  docker ps --filter "name=$DOCLING_CONTAINER"
  docker ps --filter "name=$ADAPTER_CONTAINER"
  if adapter_running; then
    echo "Adapter running: $ADAPTER_CONTAINER"
  else
    echo "Adapter not running"
  fi
}

case "${1:-}" in
  start-docling)
    start_docling
    ;;
  start-adapter)
    start_adapter
    ;;
  start)
    start_docling
    start_adapter
    ;;
  health)
    health
    ;;
  env)
    print_env
    ;;
  settings)
    print_settings
    ;;
  enable-firecrawl)
    enable_firecrawl
    ;;
  stop-adapter)
    stop_adapter
    ;;
  stop-docling)
    stop_docling
    ;;
  stop)
    stop_adapter
    stop_docling
    ;;
  logs)
    echo "== adapter logs =="
    docker logs "$ADAPTER_CONTAINER" --tail 80 2>/dev/null || true
    echo "== docling logs =="
    docker logs "$DOCLING_CONTAINER" --tail 80 2>/dev/null || true
    ;;
  status)
    status
    ;;
  -h|--help|help|"")
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
