#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API_URL="${FIRECRAWL_API_URL:-${API_URL:-http://localhost:3002}}"
CLI_PACKAGE="${FIRECRAWL_CLI_PACKAGE:-firecrawl-cli@latest}"
MODEL_PROFILE=""
RECREATE_API=1
RUN_HEALTHCHECK=0

export NPM_CONFIG_LOGLEVEL="${NPM_CONFIG_LOGLEVEL:-error}"

usage() {
  cat <<'EOF'
Usage: firecrawl_cli.sh [wrapper-options] <firecrawl-command> [args...]

Wrapper options:
  --firecrawl-model-profile <profile>  Apply a local model profile before running.
  --firecrawl-no-recreate-api          Write the profile to .env but do not recreate api.
  --firecrawl-healthcheck              Run local healthcheck after profile recreation.
  --firecrawl-help                     Show this wrapper help.

Profiles are handled by scripts/firecrawl-ops/set_model_profile.sh:
  budget | escalated | gateway | gateway-codex | openai-direct

Model profiles affect Firecrawl AI-backed formats such as summary, query, JSON,
and extract. Plain PDF markdown parsing uses the local PDF parser path and does
not call the LLM model.
EOF
}

resolve_fc_dir() {
  if [[ -n "${FC_DIR:-}" ]]; then
    printf '%s\n' "$FC_DIR"
    return
  fi

  local from_script
  from_script="$(cd "$SCRIPT_DIR/../.." && pwd)"
  if [[ -f "$from_script/docker-compose.yaml" && -d "$from_script/scripts/firecrawl-ops" ]]; then
    printf '%s\n' "$from_script"
    return
  fi

  if [[ -f "$PWD/docker-compose.yaml" && -d "$PWD/scripts/firecrawl-ops" ]]; then
    printf '%s\n' "$PWD"
    return
  fi

  local common="$HOME/Documents/GitHub/agentic-assets/firecrawl"
  if [[ -f "$common/docker-compose.yaml" && -d "$common/scripts/firecrawl-ops" ]]; then
    printf '%s\n' "$common"
    return
  fi

  echo "Could not find the Firecrawl repo. Set FC_DIR=/path/to/firecrawl and rerun." >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --firecrawl-model-profile)
      MODEL_PROFILE="${2:-}"
      if [[ -z "$MODEL_PROFILE" ]]; then
        echo "--firecrawl-model-profile requires a value" >&2
        exit 2
      fi
      shift 2
      ;;
    --firecrawl-no-recreate-api)
      RECREATE_API=0
      shift
      ;;
    --firecrawl-healthcheck)
      RUN_HEALTHCHECK=1
      shift
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

if [[ -n "$MODEL_PROFILE" ]]; then
  FC_ROOT="$(resolve_fc_dir)"
  "$FC_ROOT/scripts/firecrawl-ops/set_model_profile.sh" "$MODEL_PROFILE"
  if [[ "$RECREATE_API" == "1" ]]; then
    docker compose --project-directory "$FC_ROOT" up -d --force-recreate api
    if [[ "$RUN_HEALTHCHECK" == "1" ]]; then
      "$FC_ROOT/scripts/firecrawl-ops/firecrawl_healthcheck.sh"
    fi
  else
    echo "Profile written, but running api was not recreated. Recreate api before AI-backed calls." >&2
  fi
fi

exec npx -y "$CLI_PACKAGE" --api-url "$API_URL" "$@"
