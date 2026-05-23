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
PROFILE="${LOCAL_FIREPDF_PROFILE:-default}"
CAPTURE_JSON="${LOCAL_FIREPDF_CAPTURE_DOCLING_JSON:-}"
CAPTURE_OUTPUT_DIR="${LOCAL_FIREPDF_OUTPUT_DIR:-}"
REPLACE_ADAPTER=0

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
  restart-adapter    Rebuild/restart only the local adapter, applying current settings env.
  restart            Stop/start Docling Serve and the adapter.
  health             Check Docling Serve and the adapter.
  doctor             Run a fuller local OCR + Firecrawl readiness check.
  smoke [pdf]        Parse one local PDF through Firecrawl OCR mode.
  benchmark [pdf...] Run the PDF parser/OCR benchmark helper.
  profiles           List named Docling OCR profiles.
  profile-env <name> Print export commands for a named OCR profile.
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

Common flags after commands that start/restart the adapter:
  --profile <name>       Use a named profile from pdf_ocr_profiles.json.
  --capture-json         Save raw Docling JSON/settings artifacts.
  --no-capture-json      Disable raw Docling JSON capture for this run.
  --output-dir <path>    Host directory for raw Docling JSON artifacts.
  --replace              For start-adapter/start, replace an already-running adapter.
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

pretty_json() {
  if command -v jq >/dev/null 2>&1; then
    jq .
  else
    cat
  fi
}

profile_file() {
  local fc_dir
  fc_dir="$(resolve_fc_dir)"
  printf '%s\n' "$fc_dir/scripts/firecrawl-ops/pdf_ocr_profiles.json"
}

parse_adapter_flags() {
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --profile)
        if [[ "$#" -lt 2 ]]; then
          echo "--profile requires a value" >&2
          exit 2
        fi
        PROFILE="$2"
        export LOCAL_FIREPDF_PROFILE="$PROFILE"
        shift 2
        ;;
      --capture-json)
        CAPTURE_JSON=true
        export LOCAL_FIREPDF_CAPTURE_DOCLING_JSON=true
        shift
        ;;
      --no-capture-json)
        CAPTURE_JSON=false
        export LOCAL_FIREPDF_CAPTURE_DOCLING_JSON=false
        shift
        ;;
      --output-dir)
        if [[ "$#" -lt 2 ]]; then
          echo "--output-dir requires a path" >&2
          exit 2
        fi
        CAPTURE_OUTPUT_DIR="$2"
        export LOCAL_FIREPDF_OUTPUT_DIR="$CAPTURE_OUTPUT_DIR"
        shift 2
        ;;
      --replace)
        REPLACE_ADAPTER=1
        shift
        ;;
      *)
        echo "Unknown adapter flag: $1" >&2
        exit 2
        ;;
    esac
  done
}

profile_capture_enabled() {
  local profile_name="$1"
  local file
  file="$(profile_file)"
  python3 - "$file" "$profile_name" <<'PY'
import json
import sys
from pathlib import Path

profiles = json.loads(Path(sys.argv[1]).read_text())
name = sys.argv[2]

def merge(profile, stack=None):
    stack = stack or []
    if profile not in profiles:
        raise SystemExit(f"Unknown profile: {profile}")
    if profile in stack:
        raise SystemExit(f"Profile cycle: {' -> '.join(stack + [profile])}")
    current = dict(profiles[profile])
    parent = current.get("extends")
    if parent:
        base = merge(parent, stack + [profile])
    else:
        base = {}
    base.update(current)
    return base

resolved = merge(name)
print("true" if resolved.get("capture_docling_json") is True else "false")
PY
}

list_profiles() {
  local file
  file="$(profile_file)"
  python3 - "$file" <<'PY'
import json
import sys
from pathlib import Path

profiles = json.loads(Path(sys.argv[1]).read_text())
for name in sorted(profiles):
    desc = profiles[name].get("description", "")
    print(f"{name}\t{desc}")
PY
}

profile_env() {
  local profile_name="${1:-}"
  if [[ -z "$profile_name" ]]; then
    echo "Usage: local_firepdf_ocr.sh profile-env <profile>" >&2
    exit 2
  fi
  profile_capture_enabled "$profile_name" >/dev/null
  printf 'export LOCAL_FIREPDF_PROFILE=%q\n' "$profile_name"
  if [[ "$(profile_capture_enabled "$profile_name")" == "true" ]]; then
    printf 'export LOCAL_FIREPDF_CAPTURE_DOCLING_JSON=true\n'
    printf 'export LOCAL_FIREPDF_OUTPUT_DIR=%q\n' "${CAPTURE_OUTPUT_DIR:-tasks/tmp/firecrawl-docling-debug}"
  fi
}

add_env_if_set() {
  local name="$1"
  if [[ -n "${!name+x}" ]]; then
    DOCKER_ENV_ARGS+=("-e" "${name}=${!name}")
  fi
}

absolute_path() {
  python3 - "$1" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).expanduser().resolve())
PY
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
    if [[ "$REPLACE_ADAPTER" == "1" ]]; then
      docker rm -f "$ADAPTER_CONTAINER" >/dev/null
    else
      echo "Adapter already running: $ADAPTER_CONTAINER"
      echo "Existing container settings stay in effect. Use restart-adapter --profile <name> to apply profile/env changes."
      curl -fsS "http://127.0.0.1:${ADAPTER_PORT}/settings" 2>/dev/null | pretty_json || true
      return 0
    fi
  elif docker ps -a --format '{{.Names}}' | grep -qx "$ADAPTER_CONTAINER"; then
    docker rm "$ADAPTER_CONTAINER" >/dev/null
  fi

  local fc_dir capture_enabled host_output_dir container_output_dir
  fc_dir="$(resolve_fc_dir)"
  profile_capture_enabled "$PROFILE" >/dev/null
  capture_enabled="${CAPTURE_JSON:-$(profile_capture_enabled "$PROFILE")}"
  host_output_dir=""
  container_output_dir=""
  if [[ "$capture_enabled" == "true" ]]; then
    host_output_dir="$(absolute_path "${CAPTURE_OUTPUT_DIR:-$fc_dir/tasks/tmp/firecrawl-docling-debug}")"
    container_output_dir="/firepdf-output"
    mkdir -p "$host_output_dir"
  fi

  local volume_args=()
  if [[ -n "$host_output_dir" ]]; then
    volume_args+=("-v" "${host_output_dir}:${container_output_dir}")
  fi

  DOCKER_ENV_ARGS=(
    "-e" "LOCAL_FIREPDF_HOST=0.0.0.0"
    "-e" "LOCAL_FIREPDF_PORT=31337"
    "-e" "LOCAL_FIREPDF_ENGINE=${LOCAL_FIREPDF_ENGINE:-docling}"
    "-e" "LOCAL_FIREPDF_DOCLING_URL=http://host.docker.internal:${DOCLING_PORT}"
    "-e" "LOCAL_FIREPDF_PROFILE=${PROFILE}"
    "-e" "LOCAL_FIREPDF_PROFILES_PATH=/app/pdf_ocr_profiles.json"
    "-e" "LOCAL_FIREPDF_CAPTURE_DOCLING_JSON=${capture_enabled:-false}"
    "-e" "LOCAL_FIREPDF_TIMEOUT_SECONDS=${LOCAL_FIREPDF_TIMEOUT_SECONDS:-600}"
  )
  if [[ -n "$container_output_dir" ]]; then
    DOCKER_ENV_ARGS+=("-e" "LOCAL_FIREPDF_OUTPUT_DIR=${container_output_dir}")
  fi

  add_env_if_set LOCAL_FIREPDF_DOCLING_DO_OCR
  add_env_if_set LOCAL_FIREPDF_DOCLING_FORCE_OCR
  add_env_if_set LOCAL_FIREPDF_DOCLING_TO_FORMATS
  add_env_if_set LOCAL_FIREPDF_DOCLING_OCR_PRESET
  add_env_if_set LOCAL_FIREPDF_DOCLING_OCR_LANG
  add_env_if_set LOCAL_FIREPDF_DOCLING_PDF_BACKEND
  add_env_if_set LOCAL_FIREPDF_DOCLING_TABLE_MODE
  add_env_if_set LOCAL_FIREPDF_DOCLING_TABLE_CELL_MATCHING
  add_env_if_set LOCAL_FIREPDF_DOCLING_DO_TABLE_STRUCTURE
  add_env_if_set LOCAL_FIREPDF_DOCLING_INCLUDE_IMAGES
  add_env_if_set LOCAL_FIREPDF_DOCLING_IMAGES_SCALE
  add_env_if_set LOCAL_FIREPDF_DOCLING_IMAGE_EXPORT_MODE
  add_env_if_set LOCAL_FIREPDF_DOCLING_MD_PAGE_BREAK
  add_env_if_set LOCAL_FIREPDF_DOCLING_DO_CODE_ENRICHMENT
  add_env_if_set LOCAL_FIREPDF_DOCLING_DO_FORMULA_ENRICHMENT
  add_env_if_set LOCAL_FIREPDF_DOCLING_DO_PICTURE_CLASSIFICATION
  add_env_if_set LOCAL_FIREPDF_DOCLING_DO_CHART_EXTRACTION
  add_env_if_set LOCAL_FIREPDF_DOCLING_DO_PICTURE_DESCRIPTION
  add_env_if_set LOCAL_FIREPDF_DOCLING_VLM_PIPELINE_PRESET
  add_env_if_set LOCAL_FIREPDF_DOCLING_PICTURE_DESCRIPTION_PRESET
  add_env_if_set LOCAL_FIREPDF_DOCLING_CODE_FORMULA_PRESET
  add_env_if_set LOCAL_FIREPDF_DOCLING_TABLE_STRUCTURE_PRESET
  add_env_if_set LOCAL_FIREPDF_DOCLING_LAYOUT_PRESET

  docker build -t "$ADAPTER_IMAGE" -f "$SCRIPT_DIR/local-firepdf-adapter.Dockerfile" "$fc_dir"
  docker run -d \
    --name "$ADAPTER_CONTAINER" \
    -p "127.0.0.1:${ADAPTER_PORT}:31337" \
    "${volume_args[@]}" \
    "${DOCKER_ENV_ARGS[@]}" \
    "$ADAPTER_IMAGE" >/dev/null
  rm -f "$ADAPTER_PID"
  echo "Started local FirePDF adapter container: $ADAPTER_CONTAINER"
  echo "Profile: $PROFILE"
  echo "Raw Docling JSON capture: ${capture_enabled:-false}"
  if [[ -n "$host_output_dir" ]]; then
    echo "Raw Docling JSON output dir: $host_output_dir"
  fi
  wait_for_url "http://127.0.0.1:${ADAPTER_PORT}/health" "Local FirePDF adapter" 30
  curl -fsS "http://127.0.0.1:${ADAPTER_PORT}/settings" 2>/dev/null | pretty_json || true
  return 0
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
# This is an env template. Use "local_firepdf_ocr.sh health" to inspect the running adapter profile/options.
LOCAL_FIREPDF_ENGINE=${LOCAL_FIREPDF_ENGINE:-docling}
LOCAL_FIREPDF_PORT=${LOCAL_FIREPDF_PORT:-31337}
LOCAL_FIREPDF_PROFILE=${LOCAL_FIREPDF_PROFILE:-default}
LOCAL_FIREPDF_CAPTURE_DOCLING_JSON=${LOCAL_FIREPDF_CAPTURE_DOCLING_JSON:-false}
LOCAL_FIREPDF_OUTPUT_DIR=${LOCAL_FIREPDF_OUTPUT_DIR:-tasks/tmp/firecrawl-docling-debug}
LOCAL_FIREPDF_TIMEOUT_SECONDS=${LOCAL_FIREPDF_TIMEOUT_SECONDS:-600}
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
# scripts/firecrawl-ops/local_firepdf_ocr.sh profiles
# scripts/firecrawl-ops/local_firepdf_ocr.sh restart-adapter --profile research-page-aware
# scripts/firecrawl-ops/local_firepdf_ocr.sh restart-adapter --profile qa-debug --capture-json
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
  curl -fsS "http://127.0.0.1:${ADAPTER_PORT}/health" | pretty_json
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

doctor() {
  local fc_dir env_path
  fc_dir="$(resolve_fc_dir)"
  env_path="${ENV_PATH:-$fc_dir/.env}"
  echo "== docker context =="
  docker context show
  if [[ "$(docker context show 2>/dev/null || true)" != "orbstack" ]]; then
    echo "Warning: expected docker context 'orbstack' for this Mac."
  fi
  echo
  echo "== containers =="
  status
  echo
  echo "== OCR profiles =="
  echo "Profile file: $(profile_file)"
  list_profiles
  echo
  echo "== adapter health =="
  health
  echo
  echo "== Firecrawl .env OCR wiring =="
  if [[ -f "$env_path" ]]; then
    grep -E '^(FIRE_PDF_ENABLE|FIRE_PDF_PERCENT|FIRE_PDF_BASE_URL|FIRE_PDF_API_KEY|PDF_RUST_EXTRACT_ENABLE|MINERU_PERCENT|RUNPOD_MU_API_KEY|RUNPOD_MU_POD_ID)=' "$env_path" || true
  else
    echo "Missing $env_path"
  fi
  echo
  echo "== Firecrawl API =="
  curl -fsS "http://127.0.0.1:3002/" | pretty_json
  echo
  echo "== local stack smoke =="
  "$fc_dir/scripts/firecrawl-ops/firecrawl_healthcheck.sh"
}

smoke() {
  local fc_dir pdf
  fc_dir="$(resolve_fc_dir)"
  pdf="${1:-$fc_dir/apps/test-site/public/example.pdf}"
  if [[ ! -f "$pdf" ]]; then
    echo "Missing smoke PDF: $pdf" >&2
    exit 1
  fi
  "$fc_dir/scripts/firecrawl-ops/firecrawl_request.py" parse "$pdf" \
    --formats markdown,html \
    --pdf-mode ocr \
    --max-pages "${LOCAL_FIREPDF_SMOKE_MAX_PAGES:-1}" \
    --out "${LOCAL_FIREPDF_SMOKE_OUT:-${TMPDIR:-/tmp}/firecrawl-local-ocr-smoke.json}" \
    --pretty \
    --quiet \
    --print-paths \
    --timeout "${LOCAL_FIREPDF_SMOKE_TIMEOUT:-300}"
}

benchmark() {
  local fc_dir
  fc_dir="$(resolve_fc_dir)"
  if [[ "$#" -eq 0 ]]; then
    set -- "$fc_dir/apps/test-site/public/example.pdf"
  fi
  "$fc_dir/scripts/firecrawl-ops/pdf_ocr_benchmark.py" "$@"
}

case "${1:-}" in
  start-docling)
    shift || true
    start_docling
    ;;
  start-adapter)
    shift || true
    parse_adapter_flags "$@"
    start_adapter
    ;;
  start)
    shift || true
    parse_adapter_flags "$@"
    start_docling
    start_adapter
    ;;
  restart-adapter)
    shift || true
    parse_adapter_flags "$@"
    stop_adapter
    start_adapter
    ;;
  restart)
    shift || true
    parse_adapter_flags "$@"
    stop_adapter
    stop_docling
    start_docling
    start_adapter
    ;;
  health)
    health
    ;;
  doctor)
    doctor
    ;;
  smoke)
    shift || true
    smoke "$@"
    ;;
  benchmark)
    shift || true
    benchmark "$@"
    ;;
  profiles)
    list_profiles
    ;;
  profile-env)
    shift || true
    profile_env "$@"
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
