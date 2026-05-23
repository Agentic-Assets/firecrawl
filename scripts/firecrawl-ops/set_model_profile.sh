#!/usr/bin/env bash
set -euo pipefail

PROFILE="${1:-gateway}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FC_DIR="${FC_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
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
