#!/usr/bin/env bash
set -euo pipefail

PROFILE="${1:-gateway}"
ENV_PATH="${ENV_PATH:-$HOME/Documents/GitHub/firecrawl/.env}"

if [[ ! -f "$ENV_PATH" ]]; then
  echo "Missing env file: $ENV_PATH" >&2
  exit 1
fi

set_kv() {
  local key="$1"; local val="$2"
  if grep -q "^${key}=" "$ENV_PATH"; then
    sed -i '' "s|^${key}=.*|${key}=${val}|" "$ENV_PATH"
  else
    printf "\n%s=%s\n" "$key" "$val" >> "$ENV_PATH"
  fi
}

case "$PROFILE" in
  gateway)
    # Vercel AI Gateway → deepseek/deepseek-v4-flash.
    # Verified end-to-end: plain scrape + structured extract both pass.
    # Requires OPENAI_API_KEY=<vercel-ai-gateway-key> in .env.
    # Get a key at https://vercel.com/<team>/~/ai/api-keys (team: blightlens).
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
    set_kv OPENAI_BASE_URL "https://openrouter.ai/api/v1"
    set_kv MODEL_NAME "openrouter/minimax/minimax-m2.5"
    ;;
  escalated)
    set_kv OPENAI_BASE_URL "https://openrouter.ai/api/v1"
    set_kv MODEL_NAME "moonshotai/kimi-k2.5"
    ;;
  *)
    echo "Unknown profile: $PROFILE" >&2
    echo "Use one of: gateway | gateway-codex | openai-direct | budget | escalated" >&2
    exit 2
    ;;
esac

echo "Applied profile '$PROFILE' to $ENV_PATH"
echo "MODEL_NAME=$(grep '^MODEL_NAME=' "$ENV_PATH" | cut -d= -f2-)"
echo "Next: cd ~/Documents/GitHub/firecrawl && docker compose down && docker compose up -d"
