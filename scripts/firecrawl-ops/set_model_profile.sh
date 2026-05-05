#!/usr/bin/env bash
set -euo pipefail

PROFILE="${1:-budget}"
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

# always keep OpenRouter compatible base URL for these profiles
set_kv OPENAI_BASE_URL "https://openrouter.ai/api/v1"

case "$PROFILE" in
  budget)
    set_kv MODEL_NAME "openrouter/minimax/minimax-m2.5"
    ;;
  escalated)
    set_kv MODEL_NAME "moonshotai/kimi-k2.5"
    ;;
  *)
    echo "Unknown profile: $PROFILE" >&2
    echo "Use one of: budget | escalated" >&2
    exit 2
    ;;
esac

echo "Applied profile '$PROFILE' to $ENV_PATH"
echo "MODEL_NAME=$(grep '^MODEL_NAME=' "$ENV_PATH" | cut -d= -f2-)"
echo "Next: cd ~/Documents/GitHub/firecrawl && docker compose down && docker compose up -d"
