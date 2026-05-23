#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FC_DIR="${FC_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
API_URL="${FIRECRAWL_API_URL:-${API_URL:-http://localhost:3002}}"
CLI_PACKAGE="${FIRECRAWL_CLI_PACKAGE:-firecrawl-cli@latest}"

cd "$FC_DIR"
exec npx -y "$CLI_PACKAGE" --api-url "$API_URL" "$@"
