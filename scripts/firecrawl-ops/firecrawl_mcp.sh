#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API_URL="${FIRECRAWL_API_URL:-${API_URL:-http://localhost:3002}}"
API_KEY="${FIRECRAWL_API_KEY:-${TEST_API_KEY:-local-dev}}"
COMPATIBILITY_DOCTOR="$SCRIPT_DIR/firecrawl_compatibility_doctor.py"
PACKAGE=""

export NPM_CONFIG_LOGLEVEL="${NPM_CONFIG_LOGLEVEL:-error}"
export FIRECRAWL_API_URL="$API_URL"
export FIRECRAWL_API_KEY="$API_KEY"

resolve_mcp_package() {
  local override="${FIRECRAWL_MCP_PACKAGE:-}"

  if [[ -z "$override" ]]; then
    python3 "$COMPATIBILITY_DOCTOR" --print-default-spec mcp
    return
  fi

  python3 "$COMPATIBILITY_DOCTOR" --validate-package-spec mcp "$override"
}

PACKAGE="$(resolve_mcp_package)"

exec npx -y "$PACKAGE"
