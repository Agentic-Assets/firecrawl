#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

resolve_fc_dir() {
  if [[ -n "${FC_DIR:-}" ]]; then
    printf '%s\n' "$FC_DIR"
    return
  fi

  local from_script
  from_script="$(cd "$SCRIPT_DIR/../.." && pwd)"
  if [[ -f "$from_script/docker-compose.yaml" && -d "$from_script/.git" ]]; then
    printf '%s\n' "$from_script"
    return
  fi

  if [[ -f "$PWD/docker-compose.yaml" && -d "$PWD/.git" ]]; then
    printf '%s\n' "$PWD"
    return
  fi

  local common="$HOME/Documents/GitHub/agentic-assets/firecrawl"
  if [[ -f "$common/docker-compose.yaml" && -d "$common/.git" ]]; then
    printf '%s\n' "$common"
    return
  fi

  echo "Could not find the Firecrawl repo. Set FC_DIR=/path/to/firecrawl and rerun." >&2
  exit 1
}

FC_DIR="$(resolve_fc_dir)"
DATE_STAMP="${DATE_STAMP:-$(date +%F)}"
BRANCH="${1:-codex/sync-upstream-$DATE_STAMP}"
PROTECTED_PATHS=(
  ".agents"
  "docs/firecrawl-ops"
  "scripts/firecrawl-ops"
  "LOCAL_DEVELOPMENT_GUIDE.md"
  "AGENTS.md"
)

cd "$FC_DIR"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Working tree has local changes. Commit/stash them before syncing upstream." >&2
  exit 1
fi

if ! git remote get-url upstream >/dev/null 2>&1; then
  git remote add upstream https://github.com/firecrawl/firecrawl.git
fi

git fetch upstream main --prune
git fetch origin main --prune
git switch main
git switch -c "$BRANCH"
git merge --no-ff upstream/main -m "chore: sync upstream firecrawl/main ($DATE_STAMP)"

echo
echo "Protected fork path diff from main...HEAD:"
git diff --name-status main...HEAD -- "${PROTECTED_PATHS[@]}" || true
echo
echo "Next:"
echo "  run focused API/SDK tests"
echo "  git push -u origin $BRANCH"
echo "  open a PR into Agentic-Assets/firecrawl:main"
