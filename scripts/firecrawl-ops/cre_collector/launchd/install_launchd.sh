#!/usr/bin/env bash
# =============================================================================
# install_launchd.sh - render portable CRE launchd plists for THIS machine.
#
# The committed *.plist.template files are path-agnostic (tokens __COLLECTOR_DIR__,
# __BIN_PATH__, __ENV_EXTRA__). This script self-locates the collector dir,
# resolves node/python3 onto PATH, optionally injects CRE_ENV_FILE, renders each
# template, validates it with plutil, and installs it to ~/Library/LaunchAgents.
#
# It NEVER loads a job unless you pass --load (loading is gated per tier; see
# README.md). Rendering + installing is always safe.
#
# Usage:
#   bash install_launchd.sh <monitor|enrich|weekly|daily|all>          # render + install (no load)
#   bash install_launchd.sh --load <monitor|enrich|weekly|daily|all>   # also launchctl load -w
#   bash install_launchd.sh --print <monitor|enrich|weekly|daily>      # print rendered plist, install nothing
#   bash install_launchd.sh --env-file /path/.env.local all            # inject CRE_ENV_FILE into the plists
#   bash install_launchd.sh --uninstall <monitor|enrich|weekly|daily|all>
#
# Tiers: monitor (2x/day), enrich (every 4h), weekly (additive backstop). The
# daily tier is RETIRED (replaced by monitor+enrich) but its template is kept for
# rollback; `all` no longer includes it. Pass `daily` explicitly to render it.
#
# CRE_ENV_FILE: if set in the environment (or via --env-file), it is baked into
# the rendered plists so the ingestor finds POSTGRES_URL regardless of where the
# EQUIRE repo lives. If unset, the ingestor falls back to its ~/Documents defaults.
# =============================================================================
set -euo pipefail

LAUNCHD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COLLECTOR_DIR="$(cd "${LAUNCHD_DIR}/.." && pwd)"
LA_DIR="${HOME}/Library/LaunchAgents"

MODE="install"   # install | load | print | uninstall
CRE_ENV_FILE_ARG="${CRE_ENV_FILE:-}"
TIERS=()

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --load)      MODE="load" ;;
    --print)     MODE="print" ;;
    --uninstall) MODE="uninstall" ;;
    --env-file)
      shift
      [ $# -gt 0 ] || { echo "error: --env-file requires a path argument" >&2; exit 2; }
      CRE_ENV_FILE_ARG="$1" ;;
    monitor|enrich|weekly|daily) TIERS+=("$1") ;;
    all)         TIERS=(monitor enrich weekly) ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ "${#TIERS[@]}" -eq 0 ]; then
  echo "usage: install_launchd.sh [--load|--print|--uninstall] [--env-file PATH] <monitor|enrich|weekly|daily|all>" >&2
  exit 2
fi

label_for() { echo "ai.agentic.cre-$1"; }

# ---------------------------------------------------------------------------
# Uninstall path (unload + remove the installed copy; never touches templates)
# ---------------------------------------------------------------------------
if [ "$MODE" = "uninstall" ]; then
  for tier in "${TIERS[@]}"; do
    label="$(label_for "$tier")"
    dest="${LA_DIR}/${label}.plist"
    launchctl unload "$dest" 2>/dev/null || true
    rm -f "$dest"
    echo "uninstalled ${label}"
  done
  exit 0
fi

# ---------------------------------------------------------------------------
# Compute BIN_PATH: dirs of node + python3, then the standard dirs (deduped).
# launchd jobs start with a minimal PATH; the daily script calls npx + python3.
# ---------------------------------------------------------------------------
bin_path=""
add_dir() {
  local d="$1"
  [ -z "$d" ] && return 0
  case ":${bin_path}:" in
    *":${d}:"*) ;;                       # already present
    *) bin_path="${bin_path:+${bin_path}:}${d}" ;;
  esac
}
for b in node python3; do
  p="$(command -v "$b" 2>/dev/null || true)"
  [ -n "$p" ] && add_dir "$(cd "$(dirname "$p")" && pwd)"
done
for d in /opt/homebrew/bin /opt/homebrew/sbin /usr/local/bin /usr/bin /bin; do
  add_dir "$d"
done

# ---------------------------------------------------------------------------
# Compute __ENV_EXTRA__ (optional CRE_ENV_FILE injection).
# ---------------------------------------------------------------------------
env_extra=""
if [ -n "$CRE_ENV_FILE_ARG" ]; then
  env_extra=$'\n        <key>CRE_ENV_FILE</key>\n        <string>'"${CRE_ENV_FILE_ARG}"$'</string>'
fi

# ---------------------------------------------------------------------------
# Ensure the log directory exists (launchd opens StandardOut/ErrorPath at load).
# ---------------------------------------------------------------------------
mkdir -p "${COLLECTOR_DIR}/out/daily"

# ---------------------------------------------------------------------------
# TCC advisory: a launchd user-agent cannot read ~/Documents without a manual
# Full Disk Access grant. Warn so the user relocates the clone or grants FDA.
# ---------------------------------------------------------------------------
case "$COLLECTOR_DIR" in
  "$HOME"/Documents/*)
    echo "WARNING: this clone is under ~/Documents (TCC-protected)." >&2
    echo "  Scheduled launchd runs will exit 126 until you either move the repo" >&2
    echo "  outside ~/Documents or grant Full Disk Access to /bin/bash." >&2
    echo "  See ../SETUP.md and README.md." >&2
    ;;
esac

render() {
  # $1 = tier ; prints rendered plist to stdout
  local tier="$1" tmpl content
  tmpl="${LAUNCHD_DIR}/$(label_for "$tier").plist.template"
  [ -f "$tmpl" ] || { echo "missing template: $tmpl" >&2; return 1; }
  content="$(cat "$tmpl")"
  content="${content//__COLLECTOR_DIR__/$COLLECTOR_DIR}"
  content="${content//__BIN_PATH__/$bin_path}"
  content="${content//__ENV_EXTRA__/$env_extra}"
  printf '%s\n' "$content"
}

for tier in "${TIERS[@]}"; do
  label="$(label_for "$tier")"
  rendered="$(render "$tier")"

  if [ "$MODE" = "print" ]; then
    echo "# ----- rendered ${label}.plist -----" >&2
    printf '%s\n' "$rendered"
    continue
  fi

  dest="${LA_DIR}/${label}.plist"
  mkdir -p "$LA_DIR"
  printf '%s\n' "$rendered" > "$dest"

  # Validate; a malformed plist is worse than none.
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$dest" >/dev/null || { echo "plutil rejected ${dest}" >&2; exit 1; }
  fi
  echo "installed ${dest}"

  if [ "$MODE" = "load" ]; then
    launchctl unload "$dest" 2>/dev/null || true
    launchctl load -w "$dest"
    echo "loaded ${label} (launchctl load -w)"
  else
    echo "  not loaded (gated). To load when the tier's gate is met:"
    echo "    launchctl load -w \"$dest\""
  fi
done

if [ "$MODE" != "print" ]; then
  echo ""
  echo "Verify: launchctl list | grep ai.agentic.cre   (col 2 == 0 means last run OK)"
fi
