#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API_URL="${FIRECRAWL_API_URL:-${API_URL:-http://localhost:3002}}"
COMPATIBILITY_DOCTOR="$SCRIPT_DIR/firecrawl_compatibility_doctor.py"
CLI_PACKAGE=""

export NPM_CONFIG_LOGLEVEL="${NPM_CONFIG_LOGLEVEL:-error}"

resolve_cli_package() {
  local override="${FIRECRAWL_CLI_PACKAGE:-}"

  if [[ -z "$override" ]]; then
    python3 "$COMPATIBILITY_DOCTOR" --print-default-spec cli
    return
  fi

  python3 "$COMPATIBILITY_DOCTOR" --validate-package-spec cli "$override"
}

usage() {
  printf '%s\n' \
    'Usage: firecrawl_cli.sh [wrapper-options] <firecrawl-command> [args...]' \
    '' \
    'Wrapper options:' \
    '  --firecrawl-help                     Show this wrapper help.' \
    '' \
    'Model, OCR, and Docker configuration changes are operator-only.' \
    'Use scripts/firecrawl-ops/firecrawl_operator_handoff.py for a guarded dry-run plan.' \
    '' \
    'The wrapper never writes .env, changes a profile, recreates Docker, or runs healthchecks.'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --firecrawl-model-profile|--firecrawl-model-profile=*|--firecrawl-no-recreate-api|--firecrawl-healthcheck)
      echo "Model-profile, Docker, and healthcheck wrapper options are disabled." >&2
      echo "Use scripts/firecrawl-ops/firecrawl_operator_handoff.py for an operator-approved transition." >&2
      exit 2
      ;;
    --firecrawl-help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    *)
      break
      ;;
  esac
done

CLI_PACKAGE="$(resolve_cli_package)"

exec npx -y "$CLI_PACKAGE" --api-url "$API_URL" "$@"
