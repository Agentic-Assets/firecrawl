#!/usr/bin/env bash
set -euo pipefail

API_URL="${FIRECRAWL_API_URL:-${API_URL:-http://localhost:3002}}"
API_KEY="${FIRECRAWL_API_KEY:-${TEST_API_KEY:-local-dev}}"
PACKAGE="${FIRECRAWL_MCP_PACKAGE:-firecrawl-mcp@latest}"

export FIRECRAWL_API_URL="$API_URL"
export FIRECRAWL_API_KEY="$API_KEY"

exec npx -y "$PACKAGE"
