#!/usr/bin/env bash
set -euo pipefail

# Apply a deliberately small, reversible local resource profile for CRE runs.
# It only reads/writes the resource keys below and never prints .env values
# outside that allowlist. Existing running containers are not restarted.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: set_cre_resource_profile.sh <apply|show|restore> [--with-pids]

Manage a reversible, local CRE resource profile in root .env:
  PLAYWRIGHT_MAX_CONCURRENT_PAGES=1
  PLAYWRIGHT_CPUS=1.0
  API_CPUS=1.0

Use `apply --with-pids` to add PLAYWRIGHT_PIDS_LIMIT=192. The PID backstop
is optional because it may be too restrictive for other local workflows.

The script saves only the prior values of these four resource keys in an
ignored task-local state file. It never prints, copies, or changes secrets.
It does not restart Docker services; after an explicit operator review, apply
the profile with `docker compose up -d --force-recreate api playwright-service`.

Environment overrides:
  FC_DIR                       Firecrawl repository root.
  ENV_PATH                     Local env file (default: FC_DIR/.env).
  CRE_RESOURCE_PROFILE_STATE   State-file path (default: tasks/tmp/.../cre-safe-profile.state).
EOF
}

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

  echo "Could not find the Firecrawl repository. Set FC_DIR=/path/to/firecrawl and rerun." >&2
  exit 1
}

FC_DIR="$(resolve_fc_dir)"
ENV_PATH="${ENV_PATH:-$FC_DIR/.env}"
STATE_PATH="${CRE_RESOURCE_PROFILE_STATE:-$FC_DIR/tasks/tmp/firecrawl-cre-resource-profile/cre-safe-profile.state}"
readonly RESOURCE_KEYS=(
  PLAYWRIGHT_MAX_CONCURRENT_PAGES
  PLAYWRIGHT_CPUS
  API_CPUS
  PLAYWRIGHT_PIDS_LIMIT
)

require_env_file() {
  if [[ ! -f "$ENV_PATH" ]]; then
    echo "Local env file not found: $ENV_PATH" >&2
    echo "Create it from apps/api/.env.example or run set_model_profile.sh first." >&2
    exit 1
  fi
}

last_value() {
  local key="$1"
  awk -v key="$key" '
    index($0, key "=") == 1 {
      value = substr($0, length(key) + 2)
      found = 1
    }
    END { if (found) print value }
  ' "$ENV_PATH"
}

has_key() {
  local key="$1"
  grep -q "^${key}=" "$ENV_PATH"
}

replace_key() {
  local key="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp "${ENV_PATH}.cre-resource.XXXXXX")"
  awk -v key="$key" -v value="$value" '
    index($0, key "=") != 1 { print }
    END { print key "=" value }
  ' "$ENV_PATH" > "$tmp"
  mv "$tmp" "$ENV_PATH"
}

remove_key() {
  local key="$1"
  local tmp
  tmp="$(mktemp "${ENV_PATH}.cre-resource.XXXXXX")"
  awk -v key="$key" 'index($0, key "=") != 1 { print }' "$ENV_PATH" > "$tmp"
  mv "$tmp" "$ENV_PATH"
}

write_state() {
  mkdir -p "$(dirname "$STATE_PATH")"
  umask 077
  : > "$STATE_PATH"
  local key value
  for key in "${RESOURCE_KEYS[@]}"; do
    if has_key "$key"; then
      value="$(last_value "$key")"
      printf '%s\tpresent\t%s\n' "$key" "$value" >> "$STATE_PATH"
    else
      printf '%s\tabsent\t\n' "$key" >> "$STATE_PATH"
    fi
  done
}

state_value() {
  local key="$1"
  awk -F '\t' -v key="$key" '$1 == key { print $2 "\t" $3; exit }' "$STATE_PATH"
}

show() {
  echo "CRE resource profile (no running containers are changed)"
  printf 'env_file=%s\n' "$ENV_PATH"
  printf 'state_file=%s\n' "$STATE_PATH"
  local key value
  for key in "${RESOURCE_KEYS[@]}"; do
    value="$(last_value "$key")"
    if [[ -n "$value" ]] || has_key "$key"; then
      printf '%s=%s\n' "$key" "$value"
    else
      printf '%s=<compose-default>\n' "$key"
    fi
  done
  if [[ -f "$STATE_PATH" ]]; then
    echo "restore_available=yes"
  else
    echo "restore_available=no"
  fi
}

apply() {
  local with_pids="$1"
  require_env_file
  if [[ -f "$STATE_PATH" ]]; then
    echo "A CRE resource profile is already active; use show or restore before applying again." >&2
    exit 2
  fi

  write_state
  replace_key PLAYWRIGHT_MAX_CONCURRENT_PAGES 1
  replace_key PLAYWRIGHT_CPUS 1.0
  replace_key API_CPUS 1.0
  if [[ "$with_pids" == "true" ]]; then
    replace_key PLAYWRIGHT_PIDS_LIMIT 192
  fi

  echo "Applied CRE resource profile to $ENV_PATH"
  if [[ "$with_pids" == "true" ]]; then
    echo "PLAYWRIGHT_PIDS_LIMIT=192"
  else
    echo "PLAYWRIGHT_PIDS_LIMIT was left unchanged (use --with-pids to set 192)."
  fi
  echo "No containers were restarted. Review with '$0 show' before recreating services."
}

restore() {
  require_env_file
  if [[ ! -f "$STATE_PATH" ]]; then
    echo "No CRE resource-profile state exists at $STATE_PATH; refusing to guess prior values." >&2
    exit 2
  fi

  local key record state value
  for key in "${RESOURCE_KEYS[@]}"; do
    record="$(state_value "$key")"
    state="${record%%$'\t'*}"
    value="${record#*$'\t'}"
    case "$state" in
      present) replace_key "$key" "$value" ;;
      absent) remove_key "$key" ;;
      *)
        echo "Invalid state for $key in $STATE_PATH; refusing partial restore." >&2
        exit 1
        ;;
    esac
  done
  rm -f "$STATE_PATH"
  echo "Restored pre-profile CRE resource values in $ENV_PATH"
  echo "No containers were restarted. Recreate services only after explicit operator review."
}

COMMAND="${1:-}"
shift || true
WITH_PIDS=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-pids) WITH_PIDS=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

case "$COMMAND" in
  apply) apply "$WITH_PIDS" ;;
  show) show ;;
  restore) restore ;;
  -h|--help|help|'') usage; [[ -n "$COMMAND" ]] || exit 2 ;;
  *) echo "Unknown command: $COMMAND" >&2; usage >&2; exit 2 ;;
esac
