#!/usr/bin/env bash
set -euo pipefail

PROFILE="${1:-gateway}"
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
ENV_PATH="${ENV_PATH:-$FC_DIR/.env}"

if [[ ! -f "$ENV_PATH" ]]; then
  cat > "$ENV_PATH" <<'EOF'
# Local Firecrawl env. Gitignored; do not commit secrets.
PORT=3002
HOST=0.0.0.0
USE_DB_AUTHENTICATION=false

# CLI convenience for local agents.
FIRECRAWL_API_URL=http://localhost:3002

# For OpenRouter, Vercel AI Gateway, and OpenAI-compatible providers,
# put the provider key in OPENAI_API_KEY and set OPENAI_BASE_URL/MODEL_NAME
# with scripts/firecrawl-ops/set_model_profile.sh.
OPENAI_API_KEY=
OPENAI_BASE_URL=
MODEL_NAME=
MODEL_EMBEDDING_NAME=

# Optional direct OpenRouter provider path. Most local flows use OPENAI_API_KEY
# with OPENAI_BASE_URL=https://openrouter.ai/api/v1 instead.
OPENROUTER_API_KEY=

# Local PDF parser defaults. Rust extraction is local and does not use credits.
PDF_RUST_EXTRACT_ENABLE=true
PDF_SHADOW_COMPARISON_ENABLE=false
MINERU_PERCENT=0
FIRE_PDF_ENABLE=false
FIRE_PDF_PERCENT=10
FIRE_PDF_BASE_URL=
FIRE_PDF_API_KEY=
RUNPOD_MU_API_KEY=
RUNPOD_MU_POD_ID=
EOF
  echo "Created local env file: $ENV_PATH"
fi

set_kv() {
  local key="$1"; local val="$2"
  if grep -q "^${key}=" "$ENV_PATH"; then
    sed -i '' "s|^${key}=.*|${key}=${val}|" "$ENV_PATH"
  else
    printf "\n%s=%s\n" "$key" "$val" >> "$ENV_PATH"
  fi
}

set_kv FIRECRAWL_API_URL "http://localhost:3002"
set_kv PDF_RUST_EXTRACT_ENABLE "${PDF_RUST_EXTRACT_ENABLE:-true}"
set_kv PDF_SHADOW_COMPARISON_ENABLE "${PDF_SHADOW_COMPARISON_ENABLE:-false}"
set_kv MINERU_PERCENT "${MINERU_PERCENT:-0}"
set_kv FIRE_PDF_ENABLE "${FIRE_PDF_ENABLE:-false}"
set_kv FIRE_PDF_PERCENT "${FIRE_PDF_PERCENT:-10}"

case "$PROFILE" in
  gateway)
    # Vercel AI Gateway → deepseek/deepseek-v4-flash.
    # Requires OPENAI_API_KEY=<vercel-ai-gateway-key> in .env.
    set_kv OPENAI_BASE_URL "https://ai-gateway.vercel.sh/v1"
    set_kv MODEL_NAME "deepseek/deepseek-v4-flash"
    ;;
  gateway-codex)
    # Vercel AI Gateway → openai/gpt-5.4-mini (premium fallback).
    set_kv OPENAI_BASE_URL "https://ai-gateway.vercel.sh/v1"
    set_kv MODEL_NAME "openai/gpt-5.4-mini"
    ;;
  openai-direct)
    # OpenAI Platform key (separate ledger from ChatGPT subscription).
    # Requires OPENAI_API_KEY=sk-... already populated in .env.
    set_kv OPENAI_BASE_URL "https://api.openai.com/v1"
    set_kv MODEL_NAME "gpt-5.4-mini"
    ;;
  budget)
    # OpenRouter through the OpenAI-compatible API.
    # Requires OPENAI_API_KEY=<openrouter-key> in .env.
    set_kv OPENAI_BASE_URL "https://openrouter.ai/api/v1"
    set_kv MODEL_NAME "deepseek/deepseek-v4-flash"
    ;;
  escalated)
    # OpenRouter through the OpenAI-compatible API.
    # Requires OPENAI_API_KEY=<openrouter-key> in .env.
    set_kv OPENAI_BASE_URL "https://openrouter.ai/api/v1"
    set_kv MODEL_NAME "deepseek/deepseek-v4-pro"
    ;;
  *)
    echo "Unknown profile: $PROFILE" >&2
    echo "Use one of: gateway | gateway-codex | openai-direct | budget | escalated" >&2
    exit 2
    ;;
esac

echo "Applied profile '$PROFILE' to $ENV_PATH"
echo "OPENAI_BASE_URL=$(grep '^OPENAI_BASE_URL=' "$ENV_PATH" | cut -d= -f2-)"
echo "MODEL_NAME=$(grep '^MODEL_NAME=' "$ENV_PATH" | cut -d= -f2-)"
if ! grep -q '^OPENAI_API_KEY=.\+' "$ENV_PATH"; then
  echo "OPENAI_API_KEY is empty; AI-backed summary/json/extract/query calls will fail until you add a provider key."
fi
echo "Next: cd \"$FC_DIR\" && docker compose up -d --force-recreate api"
