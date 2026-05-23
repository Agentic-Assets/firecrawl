#!/usr/bin/env bash
set -euo pipefail

API_URL="${FIRECRAWL_API_URL:-${API_URL:-http://localhost:3002}}"
CLI_PACKAGE="${FIRECRAWL_CLI_PACKAGE:-firecrawl-cli@latest}"

export NPM_CONFIG_LOGLEVEL="${NPM_CONFIG_LOGLEVEL:-error}"

exec npx -y "$CLI_PACKAGE" --api-url "$API_URL" "$@"
